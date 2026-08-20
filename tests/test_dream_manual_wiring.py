"""手动触发 dream 的真实接线：hook 上下文、投递回调、schedule_type 分派。

两端各自用 stub 测得再细，中间这层签名对不上也照样绿 —— 参数名写错要到生产
才炸，而炸法是「指令回了已开始，然后再也没有下文」。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402

_CONV = "qq_group:bc3f12b0621298e191a49fa7"


@pytest.fixture()
def state(tmp_path: Path) -> SupervisorState:
    return SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="run-dream-wiring"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )


class TestOutboundCallback:
    def test_the_hook_context_exposes_it(self, state: SupervisorState) -> None:
        assert callable(state._build_supervisor_hook_ctx().get("post_outbound"))

    def test_dream_calls_it_the_way_state_declares_it(self, state: SupervisorState) -> None:
        # dream 用的是关键字 session_id= / text=，签名漂了这里就 TypeError。
        session_id = state.get_or_create_session(channel="qq", conversation_key=_CONV)
        post = state._build_supervisor_hook_ctx()["post_outbound"]

        assert post(session_id=session_id, text="记忆整理完成。") is True

    def test_the_message_actually_lands_in_the_session(self, state: SupervisorState) -> None:
        session_id = state.get_or_create_session(channel="qq", conversation_key=_CONV)
        post = state._build_supervisor_hook_ctx()["post_outbound"]
        post(session_id=session_id, text="记忆整理完成。")

        events = state.list_events(session_id=session_id, after_seq=0)
        bodies = [
            str((evt.get("payload") or {}).get("text") or "")
            for evt in events
            if evt.get("type") == "outbound_message"
        ]
        assert "记忆整理完成。" in bodies

    def test_an_unknown_session_is_refused_not_raised(self, state: SupervisorState) -> None:
        # 会话早被回收也不该把整条 dream 收尾流程炸掉。
        post = state._build_supervisor_hook_ctx()["post_outbound"]

        assert post(session_id="no-such-session", text="x") is False

    def test_an_empty_target_is_refused(self, state: SupervisorState) -> None:
        assert state._build_supervisor_hook_ctx()["post_outbound"](session_id="", text="x") is False


class TestManualSchedule:
    def test_the_dream_handler_actually_answers(self, state: SupervisorState) -> None:
        # 真实 hook 注册表 + 真实 DreamHandler：没接上就拿不到 status。
        outcome = state.fire_manual_schedule("dream", notify_session_id="")

        assert outcome.get("status") == "started"

    def test_a_second_call_is_told_it_is_busy(self, state: SupervisorState) -> None:
        state.fire_manual_schedule("dream", notify_session_id="")

        assert state.fire_manual_schedule("dream", notify_session_id="").get("status") == "busy"

    def test_an_unknown_schedule_type_reaches_nobody(self, state: SupervisorState) -> None:
        assert state.fire_manual_schedule("not-a-feature") == {}

    def test_the_notify_target_is_carried_through(self, state: SupervisorState) -> None:
        session_id = state.get_or_create_session(channel="qq", conversation_key=_CONV)
        state.fire_manual_schedule("dream", notify_session_id=session_id)

        # 注册表交出来的是绑定方法，__self__ 才是那个 handler 实例。
        callback = next(
            item["callback"]
            for item in state.hook_registry.handlers_for("on_schedule_tick")
            if item.get("name") == "dream"
        )
        pending = getattr(callback.__self__, "_dream_pending", None) or {}
        assert pending.get("notify_session_id") == session_id
