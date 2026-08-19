"""授权判定必须跟着「谁发起的」走，而不是跟着「代码此刻跑在哪个会话里」走。

dispatch 出去的子任务有自己的 `agent:...` conversation_key，按运行期 session 判权时
QQ 非管理员那几道硬拒整段跳过 —— 群成员只要能让 bot 派一个子节点就能写文件，
而 config/ 下的写入又落在 policy 的 default:auto 上，两步接起来就是任意代码执行。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import SafetyLevel, TaskKind  # noqa: E402

_QQ_KEY = "qq_group:bc3f12b0621298e191a49fa7"


def _make_state(workspace: Path) -> SupervisorState:
    eventlog = EventLog(workspace / "data" / "events.jsonl", run_id="run-authz")
    return SupervisorState(
        workspace_root=workspace,
        eventlog=eventlog,
        policy=PolicyEngine(workspace_root=workspace),
    )


def _dispatched_task(state: SupervisorState, *, parent_conv_key: str, is_admin: bool = False):
    """Build the task shape a dispatch actually creates: own session, routed key."""
    parent_sid = state.get_or_create_session(channel="qq_group", conversation_key=parent_conv_key)
    child_sid = state.get_or_create_session(
        channel="qq_group", conversation_key=f"agent:bootstrap.executor:{parent_conv_key}:abc",
    )
    with state._lock:
        task = state._create_task_locked(
            session_id=child_sid,
            session_generation=1,
            kind=TaskKind.node,
            node_id="bootstrap.executor",
            input_data={
                "instruction": "do the thing",
                "task_context": {
                    "conversation_key": f"agent:bootstrap.executor:{parent_conv_key}:abc",
                    "parent_conversation_key": parent_conv_key,
                    "route_conversation_key": parent_conv_key,
                    "platform_auth": {"platform": "qq", "is_admin": is_admin},
                },
            },
            continuation={},
            source_inbound_seq=None,
            caller_task_id=None,
        )
    return parent_sid, child_sid, task


class TestDispatchCannotShedQQLimits:
    def test_write_file_stays_denied_inside_a_dispatched_child(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        _parent, child_sid, task = _dispatched_task(state, parent_conv_key=_QQ_KEY)

        out = state.request_operation(
            session_id=child_sid,
            op="write_file",
            parameters={"path": "config/dynamic_context.yaml", "platform_auth": {"platform": "qq", "is_admin": False}},
            task_id=task.task_id,
        )

        assert out.safety_level == SafetyLevel.deny

    def test_execute_command_stays_denied_inside_a_dispatched_child(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        _parent, child_sid, task = _dispatched_task(state, parent_conv_key=_QQ_KEY)

        out = state.request_operation(
            session_id=child_sid,
            op="execute_command",
            parameters={"command": "python -c 'print(1)'", "platform_auth": {"platform": "qq", "is_admin": False}},
            task_id=task.task_id,
        )

        assert out.safety_level == SafetyLevel.deny

    def test_sensitive_reads_stay_denied_inside_a_dispatched_child(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        _parent, child_sid, task = _dispatched_task(state, parent_conv_key=_QQ_KEY)

        out = state.request_operation(
            session_id=child_sid,
            op="read_file",
            parameters={"path": "data/config.yaml", "platform_auth": {"platform": "qq", "is_admin": False}},
            task_id=task.task_id,
        )

        assert out.safety_level == SafetyLevel.deny

    def test_a_qq_admin_still_gets_their_shortcut_through_a_child(self, tmp_path: Path) -> None:
        # 透传发起身份的目的不是一律收紧，而是判得准：管理员的普通命令仍免审批。
        state = _make_state(tmp_path)
        _parent, child_sid, task = _dispatched_task(state, parent_conv_key=_QQ_KEY, is_admin=True)

        out = state.request_operation(
            session_id=child_sid,
            op="execute_command",
            parameters={"command": "echo hi", "platform_auth": {"platform": "qq", "is_admin": True}},
            task_id=task.task_id,
        )

        assert out.safety_level == SafetyLevel.auto

    def test_a_non_qq_caller_is_unaffected(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        _parent, child_sid, task = _dispatched_task(state, parent_conv_key="cli:default")

        out = state.request_operation(
            session_id=child_sid,
            op="write_file",
            parameters={"path": "notes.txt"},
            task_id=task.task_id,
        )

        assert out.safety_level == SafetyLevel.auto

    def test_a_direct_qq_task_is_still_denied(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        sid = state.get_or_create_session(channel="qq_group", conversation_key=_QQ_KEY)

        out = state.request_operation(
            session_id=sid,
            op="write_file",
            parameters={"path": "notes.txt", "platform_auth": {"platform": "qq", "is_admin": False}},
        )

        assert out.safety_level == SafetyLevel.deny


class TestConfigWritesNeedApproval:
    @pytest.mark.parametrize("path", [
        "config/dynamic_context.yaml",
        "config/qq.yaml",
        "config/nodes/qq.orchestrator.yaml",
        "bot.py",
        "adapters/onebot/__init__.py",
        "clonoth_sdk/client.py",
        "plugins/anything.py",
        "deploy/systemd/clonoth.service",
        "skills/persona/SKILL.md",
    ])
    def test_execution_affecting_paths_are_not_auto(self, tmp_path: Path, path: str) -> None:
        """这些路径写入后都会被执行或进提示词，落在 default:auto 上等于免审批改行为。"""
        state = _make_state(tmp_path)

        decision = state.policy.evaluate_write_file(path=path)

        assert decision.safety_level != SafetyLevel.auto, path
        assert decision.sensitive, path

    def test_runtime_yaml_stays_auto(self, tmp_path: Path) -> None:
        # 第一个命中的规则赢，所以这条白名单必须仍然排在 config/** 之前。
        state = _make_state(tmp_path)

        assert state.policy.evaluate_write_file(path="config/runtime.yaml").safety_level == SafetyLevel.auto

    def test_an_ordinary_file_stays_auto(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)

        assert state.policy.evaluate_write_file(path="notes/todo.md").safety_level == SafetyLevel.auto


class TestSchedulerBypassIsNarrowed:
    def _scheduler_task(self, state: SupervisorState):
        sid = state.get_or_create_session(channel="internal", conversation_key="scheduler:nightly")
        with state._lock:
            task = state._create_task_locked(
                session_id=sid,
                session_generation=1,
                kind=TaskKind.node,
                node_id="system.dream",
                input_data={"instruction": "organize"},
                continuation={},
                source_inbound_seq=None,
                caller_task_id=None,
            )
        return sid, task

    def test_an_ordinary_command_still_auto_approves(self, tmp_path: Path) -> None:
        # 定时任务的审批投递不到人，普通命令必须还能跑，否则备份/清理类 schedule 全废。
        state = _make_state(tmp_path)
        sid, task = self._scheduler_task(state)

        out = state.request_operation(
            session_id=sid, op="execute_command", parameters={"command": "echo hi"}, task_id=task.task_id,
        )

        assert out.safety_level == SafetyLevel.auto

    @pytest.mark.parametrize("command", [
        "pip install requests",
        "curl -o /tmp/x https://example.com/x",
        "chmod +x ./payload",
    ])
    def test_sensitive_commands_are_denied_not_auto_approved(self, tmp_path: Path, command: str) -> None:
        state = _make_state(tmp_path)
        sid, task = self._scheduler_task(state)

        out = state.request_operation(
            session_id=sid, op="execute_command", parameters={"command": command}, task_id=task.task_id,
        )

        assert out.safety_level == SafetyLevel.deny

    def test_writing_config_is_denied_not_auto_approved(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        sid, task = self._scheduler_task(state)

        out = state.request_operation(
            session_id=sid, op="write_file", parameters={"path": "config/dynamic_context.yaml"}, task_id=task.task_id,
        )

        assert out.safety_level == SafetyLevel.deny

    def test_a_denial_is_written_to_the_event_log(self, tmp_path: Path) -> None:
        # 静默 deny 会让人以为 schedule 在跑，实际每晚失败。
        state = _make_state(tmp_path)
        sid, task = self._scheduler_task(state)

        state.request_operation(
            session_id=sid, op="write_file", parameters={"path": "bot.py"}, task_id=task.task_id,
        )

        log = (tmp_path / "data" / "events.jsonl").read_text(encoding="utf-8")
        assert "scheduler_sensitive_op_denied" in log
