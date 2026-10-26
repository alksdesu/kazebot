"""策略里的 approval_grant 段：同意审批时授权的默认时长和上限。

没写这一段按代码默认；写错整份拒绝、运行期沿用上一份；控制台和原文编辑器存回去都不能把它丢掉。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config_layout  # noqa: E402
import supervisor.admin_api as admin_api  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import (  # noqa: E402
    APPROVAL_GRANT_DEFAULT_MINUTES,
    APPROVAL_GRANT_MAX_MINUTES,
    APPROVAL_GRANT_MINUTES_CEILING,
    ApprovalGrantSettings,
    PolicyEngine,
    _default_policy_dict,
    check_policy_text,
)
from supervisor.state import SupervisorState  # noqa: E402

_TOKEN = "test-approval-grant-policy-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_BASE = "version: 1\nextra_roots: []\n"
_CODE_DEFAULTS = (APPROVAL_GRANT_DEFAULT_MINUTES, APPROVAL_GRANT_MAX_MINUTES)

_INVALID_SECTIONS = [
    pytest.param("approval_grant: 30\n", id="not-a-mapping"),
    pytest.param("approval_grant: [30, 1440]\n", id="a-list"),
    pytest.param("approval_grant:\n  default_minutes: -1\n", id="negative-default"),
    pytest.param("approval_grant:\n  default_minutes: 1.5\n", id="fractional-default"),
    pytest.param("approval_grant:\n  default_minutes: '30'\n", id="quoted-default"),
    pytest.param("approval_grant:\n  default_minutes: true\n", id="boolean-default"),
    pytest.param("approval_grant:\n  max_minutes: 0\n", id="zero-ceiling"),
    pytest.param("approval_grant:\n  max_minutes: -10\n", id="negative-ceiling"),
    pytest.param(f"approval_grant:\n  max_minutes: {APPROVAL_GRANT_MINUTES_CEILING + 1}\n", id="ceiling-past-a-year"),
]


def _engine(tmp_path: Path, text: str) -> PolicyEngine:
    path = config_layout.policy_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return PolicyEngine(workspace_root=tmp_path)


def _section(default_minutes: int | None = None, max_minutes: int | None = None) -> str:
    lines = ["approval_grant:"]
    if default_minutes is not None:
        lines.append(f"  default_minutes: {default_minutes}")
    if max_minutes is not None:
        lines.append(f"  max_minutes: {max_minutes}")
    return "\n".join(lines) + "\n"


def _settings(engine: PolicyEngine) -> tuple[int, int]:
    current = engine.approval_grant_settings()
    return current.default_minutes, current.max_minutes


def _rewrite(tmp_path: Path, text: str) -> None:
    # 换文件而不是原地写：等长内容在同一个时钟刻度里写进去，只有 inode 变了才认得出来。
    config_layout.write_config_text(config_layout.policy_path(tmp_path), text)


def _on_disk(tmp_path: Path) -> dict[str, Any]:
    return yaml.safe_load(config_layout.policy_path(tmp_path).read_text(encoding="utf-8"))


def _policy(**extra: Any) -> dict[str, Any]:
    return {"version": 1, "extra_roots": [], **extra}


class TestTheSettings:
    @pytest.mark.parametrize(("requested", "expected"), [
        (None, 30),
        (120, 120),
        (5000, 1440),
        (0, 0),
        (-5, 0),
    ])
    def test_minutes_for_follows_default_and_ceiling(self, requested: int | None, expected: int) -> None:
        assert ApprovalGrantSettings(default_minutes=30, max_minutes=1440).minutes_for(requested) == expected

    def test_a_zero_default_grants_nothing_unless_asked(self) -> None:
        settings = ApprovalGrantSettings(default_minutes=0, max_minutes=60)

        assert (settings.minutes_for(None), settings.minutes_for(45)) == (0, 45)

    def test_the_code_defaults(self) -> None:
        assert (ApprovalGrantSettings().default_minutes, ApprovalGrantSettings().max_minutes) == (30, 1440)
        assert APPROVAL_GRANT_MINUTES_CEILING == 365 * 24 * 60


class TestTheSection:
    def test_without_the_section_the_code_defaults_apply(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path, _BASE)

        assert _settings(engine) == _CODE_DEFAULTS
        assert engine.load_error == ""

    def test_the_values_are_read(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path, _BASE + _section(45, 600))

        assert _settings(engine) == (45, 600)
        assert engine.load_error == ""

    def test_a_default_past_the_ceiling_is_cut_to_it(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path, _BASE + _section(2000, 60))

        assert _settings(engine) == (60, 60)

    def test_a_missing_default_takes_the_code_default_under_the_ceiling(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path, _BASE + _section(max_minutes=10))

        assert _settings(engine) == (10, 10)

    def test_a_missing_ceiling_takes_the_code_ceiling(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path, _BASE + _section(default_minutes=90))

        assert _settings(engine) == (90, APPROVAL_GRANT_MAX_MINUTES)

    def test_an_empty_section_is_the_code_defaults(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path, _BASE + "approval_grant:\n")

        assert _settings(engine) == _CODE_DEFAULTS
        assert engine.load_error == ""

    def test_a_zero_default_is_allowed(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path, _BASE + _section(0, 120))

        assert _settings(engine) == (0, 120)

    def test_the_ceiling_may_be_a_full_year(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path, _BASE + _section(60, APPROVAL_GRANT_MINUTES_CEILING))

        assert _settings(engine) == (60, APPROVAL_GRANT_MINUTES_CEILING)

    @pytest.mark.parametrize("section", _INVALID_SECTIONS)
    def test_an_invalid_section_is_refused_by_the_raw_editor_check(self, section: str) -> None:
        with pytest.raises(ValueError, match="approval_grant"):
            check_policy_text(_BASE + section)

    def test_a_valid_section_passes_the_raw_editor_check(self) -> None:
        assert check_policy_text(_BASE + _section(45, 600)) == []

    @pytest.mark.parametrize("section", _INVALID_SECTIONS)
    def test_a_broken_section_keeps_the_last_good_policy(self, tmp_path: Path, section: str) -> None:
        # 只有这一段写坏也不退回默认：别的段里自定义的 deny 规则会跟着静默消失。
        engine = _engine(tmp_path, _BASE + _section(45, 600))

        _rewrite(tmp_path, _BASE + section)

        assert _settings(engine) == (45, 600)
        assert "用不了" in engine.load_error and "approval_grant" in engine.load_error

    def test_fixing_the_section_takes_effect_again(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path, _BASE + _section(45, 600))
        _rewrite(tmp_path, _BASE + _section(max_minutes=0))
        assert _settings(engine) == (45, 600)

        _rewrite(tmp_path, _BASE + _section(15, 240))

        assert _settings(engine) == (15, 240)
        assert engine.load_error == ""

    def test_a_misspelled_key_is_named_and_ignored(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path, _BASE + "approval_grant:\n  default_minute: 45\n")

        assert _settings(engine) == _CODE_DEFAULTS
        assert "approval_grant.default_minute" in engine.load_error

    def test_the_raw_editor_check_names_a_misspelled_key(self) -> None:
        notes = check_policy_text(_BASE + "approval_grant:\n  max_minute: 45\n")

        assert len(notes) == 1 and "approval_grant.max_minute" in notes[0]

    def test_the_section_is_not_reported_as_unknown(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path, _BASE + _section(45, 600))

        assert "approval_grant" not in engine.load_error


class TestTheEditor:
    def test_the_export_fills_in_the_code_defaults(self, tmp_path: Path) -> None:
        # 没写时运行期用的是代码默认，界面上要看得到；照着存一次也不会变成别的值。
        engine = _engine(tmp_path, _BASE)

        assert engine.export_config()["approval_grant"] == {
            "default_minutes": APPROVAL_GRANT_DEFAULT_MINUTES,
            "max_minutes": APPROVAL_GRANT_MAX_MINUTES,
        }

    def test_the_export_shows_the_values_in_force(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path, _BASE + _section(2000, 60))

        assert engine.export_config()["approval_grant"] == {"default_minutes": 60, "max_minutes": 60}

    def test_saving_keeps_the_section_on_disk_and_in_force(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path, _BASE)

        saved, digest = engine.replace_config(_policy(approval_grant={"default_minutes": 120, "max_minutes": 480}))

        assert saved["approval_grant"] == {"default_minutes": 120, "max_minutes": 480}
        assert _on_disk(tmp_path)["approval_grant"] == {"default_minutes": 120, "max_minutes": 480}
        assert digest == config_layout.file_hash(config_layout.policy_path(tmp_path))
        assert _settings(engine) == (120, 480)

    def test_saving_cuts_the_default_to_the_ceiling(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path, _BASE)

        saved, _ = engine.replace_config(_policy(approval_grant={"default_minutes": 2000, "max_minutes": 60}))

        assert saved["approval_grant"] == {"default_minutes": 60, "max_minutes": 60}
        assert _on_disk(tmp_path)["approval_grant"] == {"default_minutes": 60, "max_minutes": 60}

    def test_saving_a_partial_section_writes_both_values(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path, _BASE)

        engine.replace_config(_policy(approval_grant={"max_minutes": 15}))

        assert _on_disk(tmp_path)["approval_grant"] == {"default_minutes": 15, "max_minutes": 15}

    def test_editing_something_else_keeps_the_section(self, tmp_path: Path) -> None:
        # 控制台存的是整份：读出来改别的段再存回去，这一段原样跟着回来。
        engine = _engine(tmp_path, _BASE + _section(120, 480))
        exported = engine.export_config()
        exported["extra_roots"] = ["/srv/shared"]

        saved, _ = engine.replace_config(exported)

        assert saved["extra_roots"] == ["/srv/shared"]
        assert saved["approval_grant"] == {"default_minutes": 120, "max_minutes": 480}
        assert _settings(engine) == (120, 480)

    def test_saving_the_export_of_a_file_without_the_section_writes_the_defaults(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path, _BASE)

        engine.replace_config(engine.export_config())

        assert _on_disk(tmp_path)["approval_grant"] == {
            "default_minutes": APPROVAL_GRANT_DEFAULT_MINUTES,
            "max_minutes": APPROVAL_GRANT_MAX_MINUTES,
        }
        assert _settings(engine) == _CODE_DEFAULTS

    def test_saving_without_the_section_returns_to_the_code_defaults(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path, _BASE + _section(120, 480))

        saved, _ = engine.replace_config(_policy())

        assert "approval_grant" not in _on_disk(tmp_path)
        assert saved["approval_grant"] == {
            "default_minutes": APPROVAL_GRANT_DEFAULT_MINUTES,
            "max_minutes": APPROVAL_GRANT_MAX_MINUTES,
        }
        assert _settings(engine) == _CODE_DEFAULTS

    @pytest.mark.parametrize("bad", [
        pytest.param("30", id="not-a-mapping"),
        pytest.param({"default_minutes": -1}, id="negative-default"),
        pytest.param({"default_minutes": 2.5}, id="fractional-default"),
        pytest.param({"default_minutes": True}, id="boolean-default"),
        pytest.param({"max_minutes": 0}, id="zero-ceiling"),
        pytest.param({"max_minutes": APPROVAL_GRANT_MINUTES_CEILING + 1}, id="ceiling-past-a-year"),
    ])
    def test_an_invalid_section_writes_nothing(self, tmp_path: Path, bad: Any) -> None:
        engine = _engine(tmp_path, _BASE + _section(45, 600))
        before = config_layout.policy_path(tmp_path).read_bytes()

        with pytest.raises(ValueError, match="approval_grant"):
            engine.replace_config(_policy(approval_grant=bad))

        assert config_layout.policy_path(tmp_path).read_bytes() == before
        assert _settings(engine) == (45, 600)

    def test_the_example_and_the_built_in_default_carry_the_section(self) -> None:
        example = yaml.safe_load((_ROOT / "defaults" / "policy.example.yaml").read_text(encoding="utf-8"))
        expected = {"default_minutes": APPROVAL_GRANT_DEFAULT_MINUTES, "max_minutes": APPROVAL_GRANT_MAX_MINUTES}

        assert example["approval_grant"] == expected
        assert _default_policy_dict()["approval_grant"] == expected

    def test_a_fresh_instance_writes_the_section(self, tmp_path: Path) -> None:
        engine = PolicyEngine(workspace_root=tmp_path)

        assert _on_disk(tmp_path)["approval_grant"] == {
            "default_minutes": APPROVAL_GRANT_DEFAULT_MINUTES,
            "max_minutes": APPROVAL_GRANT_MAX_MINUTES,
        }
        assert engine.load_error == ""


@pytest.fixture
def state(tmp_path: Path) -> SupervisorState:
    return SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="run-grant-policy"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )


@pytest.fixture
def api(tmp_path: Path, state: SupervisorState, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", type(admin_api._auth_failures)())
    app = create_app(
        state=state,
        process_manager=None,
        config_store=ConfigStore(path=tmp_path / "config" / "models.yaml", workspace_root=tmp_path),
    )

    def call(method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                return await http.request(method, url, headers=_AUTH, **kwargs)

        return asyncio.run(go())

    return call


class TestThePolicyEndpoints:
    def test_the_reader_shows_the_section(self, api: Any) -> None:
        response = api("GET", "/v1/config/policy")

        assert response.status_code == 200, response.text
        assert response.json()["policy"]["approval_grant"] == {
            "default_minutes": APPROVAL_GRANT_DEFAULT_MINUTES,
            "max_minutes": APPROVAL_GRANT_MAX_MINUTES,
        }

    def test_a_save_lands_and_takes_effect(self, api: Any, state: SupervisorState) -> None:
        policy = api("GET", "/v1/config/policy").json()["policy"]
        policy["approval_grant"] = {"default_minutes": 90, "max_minutes": 300}

        response = api("PUT", "/v1/config/policy", json={"policy": policy})

        assert response.status_code == 200, response.text
        assert response.json()["policy"]["approval_grant"] == {"default_minutes": 90, "max_minutes": 300}
        assert api("GET", "/v1/config/policy").json()["policy"]["approval_grant"]["default_minutes"] == 90
        assert _settings(state.policy) == (90, 300)

    def test_an_invalid_section_is_four_hundred_and_writes_nothing(
        self, api: Any, state: SupervisorState, tmp_path: Path,
    ) -> None:
        before = config_layout.policy_path(tmp_path).read_bytes()

        response = api("PUT", "/v1/config/policy", json={"policy": _policy(approval_grant={"max_minutes": 0})})

        assert response.status_code == 400, response.text
        assert "approval_grant.max_minutes" in response.json()["detail"]
        assert config_layout.policy_path(tmp_path).read_bytes() == before
        assert _settings(state.policy) == _CODE_DEFAULTS

    def test_the_raw_editor_saves_the_section(self, api: Any, state: SupervisorState) -> None:
        response = api("PUT", "/v1/admin/config/policy/raw", json={"content": _BASE + _section(20, 200)})

        assert response.status_code == 200, response.text
        assert "warnings" not in response.json()
        assert _settings(state.policy) == (20, 200)

    def test_the_raw_editor_refuses_an_invalid_section(self, api: Any, state: SupervisorState, tmp_path: Path) -> None:
        before = config_layout.policy_path(tmp_path).read_bytes()

        response = api("PUT", "/v1/admin/config/policy/raw", json={"content": _BASE + _section(default_minutes=-5)})

        assert response.status_code == 400, response.text
        assert "approval_grant.default_minutes" in response.json()["detail"]
        assert config_layout.policy_path(tmp_path).read_bytes() == before
        assert _settings(state.policy) == _CODE_DEFAULTS

    def test_the_raw_editor_warns_about_a_misspelled_key(self, api: Any) -> None:
        response = api("PUT", "/v1/admin/config/policy/raw", json={"content": _BASE + "approval_grant:\n  max_minute: 5\n"})

        assert response.status_code == 200, response.text
        assert any("approval_grant.max_minute" in note for note in response.json()["warnings"])
