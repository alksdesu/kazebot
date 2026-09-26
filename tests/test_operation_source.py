"""外部平台的硬规则按来源白名单判定，不再只认 QQ。

内部入口是白名单，没登记过的来源一律按外部平台处理：新平台的普通用户不能因为
「不是 QQ」就绕开禁止执行命令、读写文件、重启这几道硬拒。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from platforms.builtin import QQ_NAPCAT  # noqa: E402
from platforms.registry import find_platform  # noqa: E402
from platforms.sources import (  # noqa: E402
    INTERNAL_SOURCES,
    classify_operation_source,
    classify_source_name,
    is_external_platform,
)
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import SafetyLevel, TaskKind  # noqa: E402

_QQ_KEY = "qq_group:bc3f12b0621298e191a49fa7"


def _state(workspace: Path) -> SupervisorState:
    return SupervisorState(
        workspace_root=workspace,
        eventlog=EventLog(workspace / "data" / "events.jsonl", run_id="run-source"),
        policy=PolicyEngine(workspace_root=workspace),
    )


def _request(state: SupervisorState, *, channel: str, key: str, op: str, parameters: dict, task_context: dict | None = None):
    sid = state.get_or_create_session(channel=channel, conversation_key=key)
    task_id = None
    if task_context is not None:
        with state._lock:
            task = state._create_task_locked(
                session_id=sid,
                session_generation=1,
                kind=TaskKind.node,
                node_id="system.dream",
                input_data={"instruction": "x", "task_context": task_context},
                continuation={},
                source_inbound_seq=None,
                caller_task_id=None,
            )
        task_id = task.task_id
    return state.request_operation(session_id=sid, op=op, parameters=parameters, task_id=task_id)


class TestClassification:
    @pytest.mark.parametrize("name", ["qq", "QQ", "onebot", "onebot11", "qq_group", "qq_private", "qq_forward"])
    def test_every_historical_qq_name_resolves_to_the_same_platform(self, name: str) -> None:
        source = classify_source_name(name)
        assert source is not None
        assert source.external and source.declared
        assert source.label == QQ_NAPCAT.id == "qq"

    @pytest.mark.parametrize("name", sorted(INTERNAL_SOURCES))
    def test_internal_entries_are_not_external(self, name: str) -> None:
        source = classify_source_name(name)
        assert source is not None and not source.external

    def test_moka_is_an_internal_entry(self) -> None:
        # MOKAMOKA 桌面端走 moka 通道；漏了它，桌面端会被当成外部平台的普通用户。
        assert "moka" in INTERNAL_SOURCES
        assert not is_external_platform("moka")

    def test_an_undeclared_source_counts_as_external(self) -> None:
        source = classify_source_name("telegram")
        assert source is not None
        assert source.external and not source.declared

    def test_no_signal_at_all_is_external(self) -> None:
        source = classify_operation_source()
        assert source.external and source.label == "unknown"

    def test_a_prefixless_key_falls_back_to_the_channel(self) -> None:
        # Dream 的会话键是记忆命名空间，只有通道能说明它是系统任务。
        assert not classify_operation_source(conversation_key="conv_abc123", channel="system").external

    def test_the_key_prefix_wins_over_the_channel(self) -> None:
        assert not classify_operation_source(conversation_key="cli:default", channel="qq_group").external

    def test_any_external_signal_makes_it_external(self) -> None:
        source = classify_operation_source(conversation_key="web:x", platform="qq")
        assert source.external and source.label == "qq"

    def test_registry_lookup_is_case_insensitive(self) -> None:
        assert find_platform(" OneBot11 ") is QQ_NAPCAT
        assert find_platform("") is None


class TestHardRules:
    def test_an_unknown_platform_member_cannot_run_commands(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        out = _request(
            state, channel="tg_group", key="tg_group:abc", op="execute_command",
            parameters={"command": "python -c 'print(1)'", "platform_auth": {"platform": "telegram", "is_admin": False}},
        )
        assert out.safety_level == SafetyLevel.deny
        assert out.reason == "telegram non-admin users cannot execute sensitive operations"

    @pytest.mark.parametrize("op,params", [
        ("write_file", {"path": "notes.txt"}),
        ("read_file", {"path": "README.md"}),
        ("restart", {}),
    ])
    def test_an_unknown_platform_member_hits_every_hard_rule(self, tmp_path: Path, op: str, params: dict) -> None:
        state = _state(tmp_path)
        out = _request(
            state, channel="tg_group", key="tg_group:abc", op=op,
            parameters={**params, "platform_auth": {"platform": "telegram", "is_admin": False}},
        )
        assert out.safety_level == SafetyLevel.deny

    def test_qq_wording_is_unchanged(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        out = _request(
            state, channel="qq_group", key=_QQ_KEY, op="write_file",
            parameters={"path": "notes.txt", "platform_auth": {"platform": "qq", "is_admin": False}},
        )
        assert out.safety_level == SafetyLevel.deny
        assert out.reason == "qq non-admin users cannot modify files"

    @pytest.mark.parametrize("channel,key", [("moka", "moka:desktop"), ("web", "web:console"), ("cli", "cli:default")])
    def test_internal_entries_still_go_through_approval(self, tmp_path: Path, channel: str, key: str) -> None:
        state = _state(tmp_path)
        out = _request(state, channel=channel, key=key, op="execute_command", parameters={"command": "echo hi"})
        assert out.safety_level == SafetyLevel.approval_required

    def test_a_dream_task_can_still_write_memory(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        out = _request(
            state, channel="system", key="conv_bc3f12b0621298e191a49fa7", op="write_file",
            parameters={"path": "data/memory/conv_bc3f12b0621298e191a49fa7/note.md"},
            task_context={"conversation_key": "conv_bc3f12b0621298e191a49fa7", "channel": "system"},
        )
        assert "non-admin users" not in out.reason

    def test_an_unknown_platform_admin_gets_the_admin_shortcut(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        out = _request(
            state, channel="tg_group", key="tg_group:abc", op="execute_command",
            parameters={"command": "echo hi", "platform_auth": {"platform": "telegram", "is_admin": True}},
        )
        assert out.safety_level == SafetyLevel.auto
        assert out.reason.startswith("auto-approved: telegram admin task")
