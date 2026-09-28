"""平台令牌住在 data/platform_secrets.yaml，顶层按平台 id 分段。

config/ 可能整份共享给别的实例，而连接令牌是这个实例的身份，所以它留在 data/。
主进程、只吃标准库的工具子进程、诊断包各读一遍，顺序和取值判断必须一致。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(_ROOT / "tools"))

from _channel import platform_secret as tool_platform_secret  # noqa: E402

import config_layout  # noqa: E402
from clonoth_runtime import (  # noqa: E402
    SECRET_FROM_CONFIG,
    SECRET_FROM_ENV,
    PlatformSecret,
    load_platform_secrets,
    platform_secret,
    stray_platforms_section,
    undeclared_platform_secrets,
)
from platforms.builtin import QQ_NAPCAT  # noqa: E402
from supervisor.operations.service import OperationsService  # noqa: E402

BRIDGE_ENV = "ONEBOT_FORWARD_BRIDGE_TOKEN"
_SECRET_ENVS = ("ONEBOT_ACCESS_TOKEN", BRIDGE_ENV, "ONEBOT_CONVERSATION_HASH_SECRET", "NAPCAT_WEBUI_TOKEN")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # 开发机上可能设着这些，留着会让「文件优先」和「没配就是空」两条测不准。
    for name in (*_SECRET_ENVS, "BRIDGE_REF"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "data").mkdir(parents=True)
    return root


@pytest.fixture
def code_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "code"
    (root / config_layout.DEFAULT_NODES_DIR).mkdir(parents=True)
    (root / config_layout.LEGACY_DRAWTOOLS_DIR).mkdir(parents=True)
    monkeypatch.setattr(config_layout, "CODE_ROOT", root)
    return root


@pytest.fixture
def no_legacy_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_layout, "_untracked_bundled_tools", lambda report: [])


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _secrets(ws: Path, **values: Any) -> Path:
    lines = [f"{QQ_NAPCAT.id}:"]
    lines.extend(f"  {name}: {json.dumps(value)}" for name, value in values.items())
    return _write(config_layout.platform_secrets_path(ws), "\n".join(lines) + "\n")


class TestTheRuntimeLookup:
    def test_the_secrets_file_wins_over_the_environment(self, ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # 运维改的是文件，一行早就忘了的 env 不该顶掉它。
        monkeypatch.setenv(BRIDGE_ENV, "from-env")
        _secrets(ws, forward_bridge_token="from-file")

        secret = platform_secret(ws, QQ_NAPCAT, "forward_bridge_token")

        assert secret == PlatformSecret("from-file", SECRET_FROM_CONFIG, env_shadowed=True)

    def test_the_same_value_in_both_places_is_not_a_shadow(self, ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(BRIDGE_ENV, "same")
        _secrets(ws, forward_bridge_token="same")

        assert platform_secret(ws, QQ_NAPCAT, "forward_bridge_token").env_shadowed is False

    def test_the_environment_is_used_when_the_file_is_silent(self, ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(BRIDGE_ENV, "  from-env  ")

        assert platform_secret(ws, QQ_NAPCAT, "forward_bridge_token") == PlatformSecret("from-env", SECRET_FROM_ENV)

    def test_nothing_configured_leaves_it_to_the_platform(self, ws: Path) -> None:
        assert platform_secret(ws, QQ_NAPCAT, "forward_bridge_token") == PlatformSecret()

    def test_a_purely_numeric_token_is_kept_as_text(self, ws: Path) -> None:
        # YAML 把 123456 读成 int，当成没配的话这个令牌就永远对不上。
        _secrets(ws, access_token=123456)

        assert platform_secret(ws, QQ_NAPCAT, "access_token").value == "123456"

    @pytest.mark.parametrize("value", [True, False, ["a"], {"a": 1}, None, ""])
    def test_a_value_that_is_not_a_token_counts_as_unset(
        self, ws: Path, monkeypatch: pytest.MonkeyPatch, value: Any,
    ) -> None:
        # True 当成令牌 "True" 会让两端都以为配上了。
        monkeypatch.setenv(BRIDGE_ENV, "from-env")
        _secrets(ws, forward_bridge_token=value)

        assert platform_secret(ws, QQ_NAPCAT, "forward_bridge_token") == PlatformSecret("from-env", SECRET_FROM_ENV)

    def test_a_reference_in_the_file_is_expanded(self, ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRIDGE_REF", "expanded")
        _secrets(ws, forward_bridge_token="${BRIDGE_REF}")

        assert platform_secret(ws, QQ_NAPCAT, "forward_bridge_token").value == "expanded"

    def test_an_explicit_environment_mapping_replaces_the_process_one(
        self, ws: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(BRIDGE_ENV, "from-process")

        secret = platform_secret(ws, QQ_NAPCAT, "forward_bridge_token", environ={BRIDGE_ENV: "from-mapping"})

        assert secret == PlatformSecret("from-mapping", SECRET_FROM_ENV)

    def test_an_already_loaded_file_is_not_read_again(self, ws: Path) -> None:
        handed = {QQ_NAPCAT.id: {"forward_bridge_token": "handed-in"}}

        secret = platform_secret(ws / "missing", QQ_NAPCAT, "forward_bridge_token", secrets=handed)

        assert secret.value == "handed-in"

    def test_another_platforms_section_is_not_read(self, ws: Path) -> None:
        _write(config_layout.platform_secrets_path(ws), "vault:\n  forward_bridge_token: wrong-platform\n")

        assert platform_secret(ws, QQ_NAPCAT, "forward_bridge_token") == PlatformSecret()

    def test_the_old_location_is_no_longer_read(self, ws: Path) -> None:
        # 搬家只复制不删；还读旧文件的话，控制台改了新位置却不见效。
        _write(ws / config_layout.LEGACY_MODELS_FILE, "platforms:\n  qq:\n    access_token: stale\n")

        assert platform_secret(ws, QQ_NAPCAT, "access_token") == PlatformSecret()

    def test_an_undeclared_name_is_a_caller_bug(self, ws: Path) -> None:
        with pytest.raises(KeyError):
            platform_secret(ws, QQ_NAPCAT, "bridge_token")

    def test_misspelled_keys_are_listed(self, ws: Path) -> None:
        # 拼错的键被静默无视，运营者还以为配上了。
        _secrets(ws, forward_bridge_tokn="x", access_token="y", Access_Token="z")

        assert undeclared_platform_secrets(ws, QQ_NAPCAT) == ["Access_Token", "forward_bridge_tokn"]

    def test_a_file_that_is_not_there_loads_as_empty(self, ws: Path) -> None:
        assert load_platform_secrets(ws) == {}

    def test_a_broken_file_loads_as_empty(self, ws: Path) -> None:
        _write(config_layout.platform_secrets_path(ws), "qq: [oops\n")

        assert load_platform_secrets(ws) == {}


class TestTheStrayLeftoverSection:
    def test_a_models_file_still_carrying_platforms_is_reported(self, ws: Path) -> None:
        # 照改造前的文档配的，那一段现在谁也不读。
        _write(config_layout.models_path(ws), "version: 1\nplatforms:\n  qq:\n    access_token: t\n")

        assert stray_platforms_section(ws) is True

    def test_a_clean_models_file_is_not_reported(self, ws: Path) -> None:
        _write(config_layout.models_path(ws), "version: 1\n")

        assert stray_platforms_section(ws) is False

    def test_the_old_file_keeping_its_section_is_not_reported(self, ws: Path) -> None:
        _write(ws / config_layout.LEGACY_MODELS_FILE, "platforms:\n  qq:\n    access_token: t\n")

        assert stray_platforms_section(ws) is False


class TestTheToolSubprocessReadsTheSameFile:
    def test_the_secrets_file_wins_over_dotenv(self, ws: Path) -> None:
        _secrets(ws, forward_bridge_token="from-file")
        _write(ws / ".env", f"{BRIDGE_ENV}=from-dotenv\n")

        assert tool_platform_secret(QQ_NAPCAT.id, "forward_bridge_token", BRIDGE_ENV, root=ws) == "from-file"

    def test_dotenv_is_read_when_the_variable_was_stripped(self, ws: Path) -> None:
        # 子进程的环境里剥掉了平台密钥，env 那一档通常是空的。
        _write(ws / ".env", f"{BRIDGE_ENV}='from-dotenv'\n")

        assert tool_platform_secret(QQ_NAPCAT.id, "forward_bridge_token", BRIDGE_ENV, root=ws) == "from-dotenv"

    def test_a_reference_is_expanded_from_dotenv(self, ws: Path) -> None:
        _secrets(ws, forward_bridge_token="${BRIDGE_REF}")
        _write(ws / ".env", "BRIDGE_REF=expanded\n")

        assert tool_platform_secret(QQ_NAPCAT.id, "forward_bridge_token", BRIDGE_ENV, root=ws) == "expanded"

    def test_the_old_location_is_no_longer_read(self, ws: Path) -> None:
        _write(ws / config_layout.LEGACY_MODELS_FILE, "platforms:\n  qq:\n    access_token: stale\n")

        assert tool_platform_secret(QQ_NAPCAT.id, "access_token", "ONEBOT_ACCESS_TOKEN", root=ws) == ""

    @pytest.mark.parametrize(("value", "expected"), [(123456, "123456"), (True, ""), (["a"], "")])
    def test_values_are_judged_like_in_the_main_process(self, ws: Path, value: Any, expected: str) -> None:
        _secrets(ws, forward_bridge_token=value)

        tool_value = tool_platform_secret(QQ_NAPCAT.id, "forward_bridge_token", BRIDGE_ENV, root=ws)

        assert tool_value == expected
        assert tool_value == platform_secret(ws, QQ_NAPCAT, "forward_bridge_token").value

    def test_nothing_configured_is_an_empty_string(self, ws: Path) -> None:
        assert tool_platform_secret(QQ_NAPCAT.id, "forward_bridge_token", BRIDGE_ENV, root=ws) == ""


class TestDiagnosticsRedactWhatIsInTheNewFiles:
    def _service(self, ws: Path) -> OperationsService:
        return OperationsService(SimpleNamespace(workspace_root=ws))

    @pytest.mark.parametrize(("rel", "body", "value"), [
        ("config/models.yaml", "openai:\n  api_key: key-in-models\n", "key-in-models"),
        ("data/platform_secrets.yaml", "qq:\n  access_token: token-in-secrets\n", "token-in-secrets"),
        ("config/mcp.yaml", "clients:\n  fs:\n    env:\n      API_TOKEN: token-in-mcp\n", "token-in-mcp"),
        ("config/drawtools/settings.yaml", "api:\n  api_key: key-in-drawtools\n", "key-in-drawtools"),
        ("data/config.yaml", "openai:\n  api_key: key-in-old-models\n", "key-in-old-models"),
        ("data/mcp_clients.yaml", "clients:\n  fs:\n    env:\n      API_TOKEN: token-in-old-mcp\n", "token-in-old-mcp"),
    ])
    def test_every_place_a_key_can_sit_is_scanned(self, ws: Path, rel: str, body: str, value: str) -> None:
        # 搬家后旧位置那几份还留着给退回旧版本用，里面可能是没轮换掉的旧密钥，一并抹。
        _write(ws / rel, body)
        service = self._service(ws)

        secrets = service._configured_secrets()

        assert value in secrets
        assert value not in service._clean(f"request failed with {value}", secrets)

    def test_a_declared_platform_secret_is_collected_by_name(self, ws: Path) -> None:
        # access_token 这种名字按 token/secret 的关键词也认得出，但声明那一遍不能少。
        _secrets(ws, access_token="declared-token")

        assert "declared-token" in self._service(ws)._configured_secrets()

    def test_no_empty_string_is_ever_redacted(self, ws: Path) -> None:
        assert "" not in self._service(ws)._configured_secrets()


@pytest.mark.usefixtures("no_legacy_tools")
class TestTheLayoutHealthCheck:
    def _check(self, ws: Path) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []

        def add(name: str, status: str, detail: str, action: str = "", **extra: Any) -> None:
            rows.append({"name": name, "status": status, "detail": detail, "action": action})

        OperationsService(SimpleNamespace(workspace_root=ws))._layout_check(add)
        assert len(rows) == 1
        return rows[0]

    def test_a_workspace_that_never_migrated_fails(self, ws: Path, code_root: Path) -> None:
        row = self._check(ws)

        assert (row["name"], row["status"]) == ("配置布局", "failed")
        assert row["action"]

    def test_a_clean_migration_passes(self, ws: Path, code_root: Path) -> None:
        config_layout.ensure_layout(ws)

        assert self._check(ws)["status"] == "passed"

    def test_a_warning_recorded_during_the_move_is_surfaced(self, ws: Path, code_root: Path) -> None:
        _write(ws / config_layout.LEGACY_MODELS_FILE, "platforms:\n  qq:\n    access_token: from-legacy\n")
        _write(config_layout.platform_secrets_path(ws), "qq:\n  access_token: already-here\n")
        config_layout.ensure_layout(ws)

        row = self._check(ws)

        assert row["status"] == "warning"
        assert config_layout.PLATFORM_SECRETS_FILE in row["detail"]

    def test_a_leftover_platforms_section_is_surfaced(self, ws: Path, code_root: Path) -> None:
        config_layout.ensure_layout(ws)
        _write(config_layout.models_path(ws), "version: 1\nplatforms:\n  qq:\n    access_token: t\n")

        row = self._check(ws)

        assert row["status"] == "warning"
        assert config_layout.MODELS_FILE in row["detail"]
        assert config_layout.PLATFORM_SECRETS_FILE in row["detail"]

    def test_an_old_file_edited_after_the_move_is_surfaced(
        self, ws: Path, code_root: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        legacy = _write(ws / config_layout.LEGACY_POLICY_FILE, "version: 1\n")
        config_layout.ensure_layout(ws)
        later = legacy.stat().st_mtime + 120
        os.utime(legacy, (later, later))

        row = self._check(ws)

        assert row["status"] == "warning"
        assert "data/policy.yaml" in row["detail"]
