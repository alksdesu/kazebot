"""社区设置里的两项插话覆盖：在场开关 interject_presence、离场时去问模型的概率 interject_absent_probability。

没写（None）就是跟随平台配置 trigger.presence.*；写了值的按「本群覆盖 > 实例默认」叠，判定时再盖过平台配置。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adapters._shared.trigger_policy import TriggerConfig, with_group_settings  # noqa: E402
from supervisor.community.service import CommunityService, POLICY_FIELDS  # noqa: E402
from supervisor.feature_auth import FeatureActor  # noqa: E402

GROUP = "qq_group:interject"
KEYS = ("interject_presence", "interject_absent_probability")


@pytest.fixture()
def admin() -> FeatureActor:
    return FeatureActor(owner="console:admin", is_admin=True, role="admin", channel="web")


@pytest.fixture()
def service(tmp_path: Path) -> CommunityService:
    return CommunityService(tmp_path)


def _override(service: CommunityService, group: FeatureActor, **body: Any) -> dict[str, Any]:
    state = service.state(group)
    return service.update_overrides(group, {
        **body, "expected_revision": state["revision"], "expected_defaults_revision": state["defaults_revision"],
    })


class TestTheFactoryValues:
    def test_both_keys_are_policy_fields(self) -> None:
        assert set(KEYS) <= set(POLICY_FIELDS)

    def test_a_fresh_instance_follows_the_platform(self, service: CommunityService, admin: FeatureActor) -> None:
        defaults = service.defaults(admin)

        assert {key: defaults["settings"][key] for key in KEYS} == dict.fromkeys(KEYS)
        assert defaults["values"] == {}

    def test_a_group_inherits_the_unset_value(self, service: CommunityService, admin: FeatureActor) -> None:
        state = service.state(replace(admin, scope=GROUP))

        assert {key: state["settings"][key] for key in KEYS} == dict.fromkeys(KEYS)
        assert set(KEYS) <= set(state["inherited_fields"])
        assert state["overrides"] == {}

    def test_an_unset_value_changes_nothing_in_the_decision(self, service: CommunityService, admin: FeatureActor) -> None:
        platform = TriggerConfig(presence=True, absent_judge_probability=0.1)

        settings = service.state(replace(admin, scope=GROUP))["settings"]

        assert with_group_settings(platform, settings) == platform


class TestInstanceDefaults:
    def test_a_value_is_saved_inherited_and_reset(self, service: CommunityService, admin: FeatureActor) -> None:
        saved = service.update_defaults(admin, {
            "values": {"interject_presence": True, "interject_absent_probability": 0.25}, "expected_revision": 0,
        })

        assert saved["values"] == {"interject_presence": True, "interject_absent_probability": 0.25}
        group = service.state(replace(admin, scope=GROUP))
        assert group["settings"]["interject_presence"] is True
        assert group["settings"]["interject_absent_probability"] == pytest.approx(0.25)

        reset = service.update_defaults(admin, {"reset_fields": ["interject_presence"], "expected_revision": 1})

        assert reset["values"] == {"interject_absent_probability": 0.25}
        assert reset["settings"]["interject_presence"] is None

    def test_a_whole_number_probability_is_stored_as_a_number_between_0_and_1(
        self, service: CommunityService, admin: FeatureActor,
    ) -> None:
        saved = service.update_defaults(admin, {"values": {"interject_absent_probability": 1}, "expected_revision": 0})

        assert saved["values"]["interject_absent_probability"] == 1.0
        assert isinstance(saved["values"]["interject_absent_probability"], float)

    @pytest.mark.parametrize("key", KEYS)
    def test_null_is_refused_with_a_pointer_to_reset_fields(
        self, service: CommunityService, admin: FeatureActor, key: str,
    ) -> None:
        # 服务端不收 null：「跟随平台配置」只能靠撤掉写下的值，免得 null 和没写成了两种状态。
        with pytest.raises(ValueError, match="reset_fields"):
            service.update_defaults(admin, {"values": {key: None}, "expected_revision": 0})

    @pytest.mark.parametrize(("key", "value"), [
        ("interject_presence", "yes"),
        ("interject_presence", 1),
        ("interject_absent_probability", 1.5),
        ("interject_absent_probability", -0.1),
        ("interject_absent_probability", float("nan")),
        ("interject_absent_probability", float("inf")),
        ("interject_absent_probability", True),
        ("interject_absent_probability", "0.3"),
    ])
    def test_bad_values_are_refused_and_nothing_is_written(
        self, service: CommunityService, admin: FeatureActor, key: str, value: Any,
    ) -> None:
        before = service.defaults(admin)

        with pytest.raises(ValueError):
            service.update_defaults(admin, {
                "values": {"response_policy_enabled": True, key: value}, "expected_revision": 0,
            })

        assert service.defaults(admin) == before


class TestGroupOverrides:
    def test_a_group_override_beats_the_instance_default(self, service: CommunityService, admin: FeatureActor) -> None:
        service.update_defaults(admin, {"values": {"interject_absent_probability": 0.25}, "expected_revision": 0})
        group = replace(admin, scope=GROUP)

        updated = _override(service, group, values={"interject_absent_probability": 0.5, "interject_presence": False})

        assert updated["settings"]["interject_absent_probability"] == pytest.approx(0.5)
        assert updated["settings"]["interject_presence"] is False
        assert updated["overrides"] == {"interject_absent_probability": 0.5, "interject_presence": False}
        assert not set(KEYS) & set(updated["inherited_fields"])
        assert service.settings_scopes(admin) == [GROUP]

    def test_the_override_reaches_the_decision(self, service: CommunityService, admin: FeatureActor) -> None:
        group = replace(admin, scope=GROUP)
        settings = _override(service, group, values={"interject_presence": False, "interject_absent_probability": 0.5})["settings"]

        config = with_group_settings(TriggerConfig(presence=True, absent_judge_probability=0.1), settings)

        assert config.presence is False
        assert config.absent_judge_probability == pytest.approx(0.5)

    def test_resetting_the_override_inherits_again(self, service: CommunityService, admin: FeatureActor) -> None:
        service.update_defaults(admin, {"values": {"interject_absent_probability": 0.25}, "expected_revision": 0})
        group = replace(admin, scope=GROUP)
        _override(service, group, values={"interject_absent_probability": 0.5})

        reset = _override(service, group, reset_fields=["interject_absent_probability"])

        assert reset["overrides"] == {}
        assert reset["settings"]["interject_absent_probability"] == pytest.approx(0.25)
        assert "interject_absent_probability" in reset["inherited_fields"]

    def test_a_null_override_is_refused(self, service: CommunityService, admin: FeatureActor) -> None:
        with pytest.raises(ValueError, match="reset_fields"):
            _override(service, replace(admin, scope=GROUP), values={"interject_presence": None})

    def test_setting_and_resetting_the_same_field_at_once_is_refused(
        self, service: CommunityService, admin: FeatureActor,
    ) -> None:
        with pytest.raises(ValueError, match="同时设置和恢复"):
            _override(
                service, replace(admin, scope=GROUP),
                values={"interject_presence": True}, reset_fields=["interject_presence"],
            )


class TestTheLegacyUpdate:
    """旧接口按「合进去」更新、没有 reset_fields：null 什么也不改，字符串概率照数字读。"""

    def test_null_leaves_the_override_alone(self, service: CommunityService, admin: FeatureActor) -> None:
        group = replace(admin, scope=GROUP)
        service.update_settings(group, {"interject_presence": True})

        state = service.update_settings(group, {"interject_presence": None})

        assert state["overrides"] == {"interject_presence": True}

    def test_a_string_probability_is_read_as_a_number(self, service: CommunityService, admin: FeatureActor) -> None:
        state = service.update_settings(replace(admin, scope=GROUP), {"interject_absent_probability": "0.3"})

        assert state["settings"]["interject_absent_probability"] == pytest.approx(0.3)

    @pytest.mark.parametrize("value", ["often", "2", "nan"])
    def test_a_bad_string_is_refused(self, service: CommunityService, admin: FeatureActor, value: str) -> None:
        with pytest.raises(ValueError):
            service.update_settings(replace(admin, scope=GROUP), {"interject_absent_probability": value})


class TestStoredData:
    def test_a_stored_null_reads_as_unset(self, service: CommunityService, admin: FeatureActor) -> None:
        group = replace(admin, scope=GROUP)
        service.state(group)
        with sqlite3.connect(service.path) as db:
            db.execute(
                "INSERT OR REPLACE INTO groups(scope, settings) VALUES (?, ?)",
                (GROUP, json.dumps({"interject_presence": None, "interject_absent_probability": None})),
            )

        state = service.state(group)

        assert state["overrides"] == {}
        assert state["settings"]["interject_presence"] is None
        assert service.settings_scopes(admin) == []

    def test_a_stored_null_default_reads_as_unset(self, service: CommunityService, admin: FeatureActor) -> None:
        service.defaults(admin)
        with sqlite3.connect(service.path) as db:
            db.execute(
                "UPDATE conversation_defaults SET settings=? WHERE id=1",
                (json.dumps({"interject_presence": None}),),
            )

        defaults = service.defaults(admin)

        assert defaults["values"] == {}
        assert defaults["settings"]["interject_presence"] is None
