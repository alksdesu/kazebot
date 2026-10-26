"""「不发言」伪工具 stay_silent：只在插话的任务里生效，结果带 no_reply 标记，一路传到适配层后什么也不发。

engine 的处理、supervisor 的出站标记、SDK 的投递上下文、QQ 适配层的收尾各钉一遍。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

# 先把 engine.inference 整个拉起来：只跑部分测试时，先导入 plaintext 会撞上它和 ai_step 的循环导入。
from _engine_loop_fixture import NodeRun, ScriptedRegistry, call, results_for, turn  # noqa: E402
from engine.inference import pseudo_handlers  # noqa: E402
from engine.inference.pseudo_tools import NO_REPLY_MARKER, STAY_SILENT_TOOL_NAME, _stay_silent_spec  # noqa: E402
from engine.inference.tool_format import create_tool_formatter  # noqa: E402
from engine.node import Node, ToolAccess  # noqa: E402
from engine.protocol import ACTION_FINISH  # noqa: E402
from providers.base import ToolCall  # noqa: E402

from _onebot_harness import load_runtime, set_live_config  # noqa: E402
from clonoth_sdk import types as sdk_types  # noqa: E402
from clonoth_sdk.config import BotConfig  # noqa: E402
from clonoth_sdk.event_router import EventRouter  # noqa: E402
from clonoth_sdk.state import SessionState, TriggerInfo  # noqa: E402
from clonoth_sdk.types import DeliveryContext, Event  # noqa: E402
from supervisor import task_router  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import TaskStatus  # noqa: E402
from toolbox.tool_names import PSEUDO_TOOL_NAMES, is_pseudo_tool_name  # noqa: E402

SILENT_RESULT = {"summary": "", "text": "", "attachments": [], "no_reply": True}
AMBIENT = {"route_hints": {"delivery_purpose": "ambient"}}
DIRECT = {"route_hints": {"delivery_purpose": "direct"}}
SHORT_AMBIENT = {"route_hints": {"delivery_purpose": "ambient", "response_action": "short_reply"}}
SHORT_DIRECT = {"route_hints": {"delivery_purpose": "direct", "response_action": "short_reply"}}


class TestTheName:
    def test_the_name_is_reserved_for_every_tool(self) -> None:
        assert STAY_SILENT_TOOL_NAME == "stay_silent"
        assert STAY_SILENT_TOOL_NAME in PSEUDO_TOOL_NAMES
        assert is_pseudo_tool_name(STAY_SILENT_TOOL_NAME)

    def test_the_spec_takes_only_an_optional_reason(self) -> None:
        spec = _stay_silent_spec()
        function = spec["function"]

        assert spec["type"] == "function" and function["name"] == STAY_SILENT_TOOL_NAME
        assert set(function["parameters"]["properties"]) == {"reason"}
        assert function["parameters"].get("required", []) == []

    def test_every_layer_spells_the_marker_the_same_way(self) -> None:
        # supervisor 和 SDK 各写了一份，没有从 engine 导入：三处只能靠这条对齐。
        assert NO_REPLY_MARKER == task_router.OUTBOUND_NO_REPLY == sdk_types.OUTBOUND_NO_REPLY == "no_reply"


def _loop_state(
    tmp_path: Path,
    *,
    task_context: dict[str, Any] | None = None,
    enabled: bool = True,
    preempted: dict[str, Any] | None = None,
) -> SimpleNamespace:
    rctx = SimpleNamespace(
        workspace_root=tmp_path, supervisor_url="http://sup", session_id="branch", parent_session_id="parent",
        task_id="task", task_context=dict(task_context if task_context is not None else AMBIENT),
        emit_event=AsyncMock(return_value=True), current_llm_request_id="req-1",
        check_preempted=AsyncMock(return_value=preempted or {"preempted": False}),
    )
    node = SimpleNamespace(id="qq.orchestrator", tool_mode="native", extra={"stay_silent": True} if enabled else {})
    return SimpleNamespace(
        rctx=rctx, node=node, messages=[], collected_attachments=[], tool_produced_attachments=[],
        formatter=create_tool_formatter("native"), preempt_inject_info=None, preempt_after_step=False,
    )


def _handle(state: SimpleNamespace, name: str = STAY_SILENT_TOOL_NAME, **arguments: Any):
    return asyncio.run(pseudo_handlers._handle_pseudo_tool(state, ToolCall(id="c1", name=name, arguments=arguments), 0))


def _answer(state: SimpleNamespace) -> str:
    result = state.messages[-1]
    assert result["tool_call_id"] == "c1"
    return str(result["content"])


@pytest.fixture()
def persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pseudo_handlers, "_persist_ctx", lambda *_args: "ctx")


@pytest.mark.usefixtures("persisted")
class TestTheHandler:
    def test_an_interjection_ends_the_task_without_a_reply(self, tmp_path: Path) -> None:
        state = _loop_state(tmp_path)

        action = _handle(state, reason="和我无关")

        assert action is not None and action.action == ACTION_FINISH
        assert action.result == SILENT_RESULT
        assert action.context_ref == "ctx" and action.llm_request_id == "req-1"
        assert _answer(state) == "好的，这一轮不发言。"
        state.rctx.emit_event.assert_not_awaited()

    def test_the_reason_is_optional(self, tmp_path: Path) -> None:
        action = _handle(_loop_state(tmp_path))

        assert action is not None and action.result == SILENT_RESULT

    @pytest.mark.parametrize("task_context", [DIRECT, {}], ids=["direct", "no-purpose"])
    def test_someone_calling_the_bot_gets_an_answer(self, tmp_path: Path, task_context: dict[str, Any]) -> None:
        # 没带用途的老任务按直接找 bot 算。
        state = _loop_state(tmp_path, task_context=task_context)

        assert _handle(state) is None
        assert _answer(state).startswith("Error:") and "直接找你" in _answer(state)

    def test_a_node_without_the_switch_cannot_stay_silent(self, tmp_path: Path) -> None:
        state = _loop_state(tmp_path, enabled=False)

        assert _handle(state) is None
        assert "这个节点不能选择不发言" in _answer(state)

    def test_a_system_task_cannot_stay_silent_even_if_it_tries(self, tmp_path: Path) -> None:
        # 系统任务的工具表里没有它；模型硬调也不认，系统任务的结果不能被换成空的。
        state = _loop_state(tmp_path, task_context={**AMBIENT, "is_system_task": True})

        assert _handle(state) is None
        assert "这个节点不能选择不发言" in _answer(state)

    def test_a_short_interjection_can_stay_silent(self, tmp_path: Path) -> None:
        action = _handle(_loop_state(tmp_path, task_context=SHORT_AMBIENT))

        assert action is not None and action.result == SILENT_RESULT

    def test_a_short_interjection_is_told_about_both_exits(self, tmp_path: Path) -> None:
        state = _loop_state(tmp_path, task_context=SHORT_AMBIENT)

        assert _handle(state, "reply", text="hi") is None
        assert _answer(state) == "本轮为简短回应：要说就调用 finish，不想接话就调用 stay_silent。"
        state.rctx.emit_event.assert_not_awaited()

    def test_a_short_direct_reply_is_told_to_finish(self, tmp_path: Path) -> None:
        state = _loop_state(tmp_path, task_context=SHORT_DIRECT)

        assert _handle(state, "reply", text="hi") is None
        assert _answer(state) == "本轮为简短回应，请仅调用 finish。"
        assert _handle(state) is None
        assert "直接找你" in _answer(state)

    def test_a_short_reply_on_a_node_without_the_switch_keeps_the_old_wording(self, tmp_path: Path) -> None:
        state = _loop_state(tmp_path, task_context=SHORT_AMBIENT, enabled=False)

        assert _handle(state, "reply", text="hi") is None
        assert _answer(state) == "本轮为简短回应，请仅调用 finish。"

    def test_a_new_message_waiting_cancels_the_silence(self, tmp_path: Path) -> None:
        # 直接结束的话，刚进来的那条就没人看了。
        pending = {"preempted": True, "message": "新来的一句"}
        state = _loop_state(tmp_path, preempted=pending)

        assert _handle(state) is None
        assert state.preempt_inject_info == pending
        assert _answer(state) == "刚有新消息进来，这次不发言没有生效；先看新消息再决定。"

    def test_a_message_already_waiting_to_be_injected_cancels_it_too(self, tmp_path: Path) -> None:
        state = _loop_state(tmp_path)
        state.preempt_inject_info = {"preempted": True, "message": "新来的一句"}

        assert _handle(state) is None
        state.rctx.check_preempted.assert_not_awaited()

    def test_a_preempt_without_a_message_still_ends_silently(self, tmp_path: Path) -> None:
        state = _loop_state(tmp_path, preempted={"preempted": True})

        action = _handle(state)

        assert action is not None and action.result == SILENT_RESULT
        assert state.preempt_after_step is True


def _node(**extra: Any) -> Node:
    return Node(
        id="qq.orchestrator", type="ai", prompt="在群里聊天。", tool_mode="native",
        tool_access=ToolAccess(mode="all"), extra=dict(extra),
    )


def _finish(text: str = "嗯", call_id: str = "f1"):
    return turn(call("finish", {"text": text}, call_id=call_id))


class TestTheLoop:
    def test_both_kinds_of_turn_see_the_same_tool_list(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # 工具表一变，前缀缓存整段失效：插话和直接找 bot 必须看到一模一样的工具表。
        ambient = NodeRun(monkeypatch, tmp_path / "ambient", registry=ScriptedRegistry(), node=_node(stay_silent=True),
                          task_context=AMBIENT)
        ambient.run(_finish())
        direct = NodeRun(monkeypatch, tmp_path / "direct", registry=ScriptedRegistry(), node=_node(stay_silent=True),
                         task_context=DIRECT)
        direct.run(_finish())

        assert STAY_SILENT_TOOL_NAME in ambient.tool_names(0)
        assert ambient.requests[0]["tools"] == direct.requests[0]["tools"]

    @pytest.mark.parametrize("extra", [{}, {"stay_silent": False}, {"stay_silent": "false"}], ids=["unset", "off", "quoted-off"])
    def test_nodes_without_the_switch_never_see_the_tool(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: dict[str, Any],
    ) -> None:
        run = NodeRun(monkeypatch, tmp_path, registry=ScriptedRegistry(), node=_node(**extra), task_context=AMBIENT)
        run.run(_finish())

        assert STAY_SILENT_TOOL_NAME not in run.tool_names(0)

    def test_system_tasks_never_see_the_tool(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        run = NodeRun(
            monkeypatch, tmp_path, registry=ScriptedRegistry(), node=_node(stay_silent=True),
            task_context={**AMBIENT, "is_system_task": True},
        )
        run.run(_finish())

        assert STAY_SILENT_TOOL_NAME not in run.tool_names(0)

    def test_an_interjection_ends_silently_through_the_loop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        run = NodeRun(monkeypatch, tmp_path, registry=ScriptedRegistry(), node=_node(stay_silent=True), task_context=AMBIENT)

        action = run.run(turn(call(STAY_SILENT_TOOL_NAME, {"reason": "插不上话"}, call_id="s1")))

        assert action.action == ACTION_FINISH
        assert action.result.get(NO_REPLY_MARKER) is True
        assert action.result["text"] == "" and action.result["summary"] == ""
        assert len(run.requests) == 1

    def test_a_short_interjection_keeps_the_marker_past_the_end_of_the_task(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 短答收尾会重写 text 和 attachments，标记不能跟着丢。
        run = NodeRun(
            monkeypatch, tmp_path, registry=ScriptedRegistry(), node=_node(stay_silent=True), task_context=SHORT_AMBIENT,
        )

        action = run.run(turn(call(STAY_SILENT_TOOL_NAME, call_id="s1")))

        assert action.action == ACTION_FINISH
        assert action.result[NO_REPLY_MARKER] is True
        assert action.result["text"] == "" and action.result["attachments"] == []

    def test_a_direct_turn_is_told_to_answer_and_carries_on(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        run = NodeRun(monkeypatch, tmp_path, registry=ScriptedRegistry(), node=_node(stay_silent=True), task_context=DIRECT)

        action = run.run(turn(call(STAY_SILENT_TOOL_NAME, call_id="s1")), _finish("在的"))

        assert action.result["text"] == "在的"
        assert NO_REPLY_MARKER not in action.result
        rejected = results_for(run.requests[1]["messages"], "s1")
        assert len(rejected) == 1 and "直接找你" in str(rejected[0]["content"])


@pytest.fixture()
def supervisor_state(tmp_path: Path) -> SupervisorState:
    result = SupervisorState(
        workspace_root=tmp_path, eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="stay-silent"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    result._defer_post_work_enqueue = True
    return result


def _complete(state: SupervisorState, result: dict[str, Any]) -> list[dict[str, Any]]:
    """建一个入站任务、按给定结果结束它，返回它产出的出站消息。"""
    session = state.get_or_create_session(channel="qq_private", conversation_key="qq_private:123")
    event = state.eventlog.append(
        session_id=session, component="shell", type_="inbound_message",
        payload={"channel": "qq_private", "conversation_key": "qq_private:123", "text": "在吗"},
    )
    state.record_inbound_message_event(event)
    task = next(task for task in state.tasks.values() if task.source_inbound_seq == event["seq"])
    task.status = TaskStatus.running
    task.worker_id = "worker"
    state.complete_task(task_id=task.task_id, worker_id="worker", result=result)
    return [
        dict(item.get("payload") or {}) for item in state.eventlog.events
        if item.get("type") == "outbound_message"
        and (item.get("payload") or {}).get("source_inbound_seq") == task.source_inbound_seq
    ]


class TestTheSupervisor:
    def test_a_silent_finish_goes_out_marked_no_reply(self, supervisor_state: SupervisorState) -> None:
        outbound = _complete(supervisor_state, {"action": "finish", "result": dict(SILENT_RESULT)})

        assert len(outbound) == 1
        assert outbound[0]["action_type"] == "no_reply"
        # 没有补「（无回复）」这类兜底：空文本照样出站，适配层据标记收尾。
        assert outbound[0]["text"] == ""

    def test_an_ordinary_finish_keeps_its_action_type(self, supervisor_state: SupervisorState) -> None:
        outbound = _complete(supervisor_state, {"action": "finish", "result": {"text": "在的"}})

        assert outbound[0]["action_type"] == "finish" and outbound[0]["text"] == "在的"

    @pytest.mark.parametrize("marker", ["true", 1, False])
    def test_only_a_real_true_counts(self, supervisor_state: SupervisorState, marker: Any) -> None:
        outbound = _complete(supervisor_state, {"action": "finish", "result": {"text": "在的", "no_reply": marker}})

        assert outbound[0]["action_type"] == "finish"


class TestTheSdk:
    def test_the_delivery_context_reads_the_marker(self) -> None:
        assert DeliveryContext(action_type="no_reply").no_reply is True
        assert DeliveryContext(action_type="finish").no_reply is False
        assert DeliveryContext().no_reply is False

    def test_a_derived_context_keeps_it(self) -> None:
        assert DeliveryContext(event_id="e1", idempotency_key="k1", action_type="no_reply").child("part").no_reply is True

    @pytest.mark.parametrize(("action_type", "silent"), [("no_reply", True), ("finish", False), ("", False)])
    def test_the_router_hands_the_marker_to_send_reply(self, tmp_path: Path, action_type: str, silent: bool) -> None:
        async def exercise() -> AsyncMock:
            send_reply = AsyncMock()
            state = SessionState()
            state.register_trigger(TriggerInfo(inbound_seq=5, conversation_key="qq_group:one", session_id="session", is_dm=False))
            config = BotConfig(base_url="http://local", workspace_root=tmp_path, conversation_key_prefix="qq_group")
            router = EventRouter(SimpleNamespace(), state, SimpleNamespace(send_reply=send_reply), config)
            payload = {"text": "", "conversation_key": "qq_group:one", "source_inbound_seq": 5}
            if action_type:
                payload["action_type"] = action_type
            await router._dispatch_durable_outbound(Event(
                seq=1, event_id="event-1", ts="", run_id="run", session_id="session", component="supervisor",
                type="outbound_message", payload=payload,
            ))
            return send_reply

        send_reply = asyncio.run(exercise())

        assert send_reply.await_count == 1
        assert send_reply.call_args.kwargs["delivery_context"].no_reply is silent


GROUP_ID = 7
USER_QQ = 10001
BOT_QQ = "900001"
INBOUND_SEQ = 41
GROUP_KEY = f"qq_group:{GROUP_ID}"


class _Event:
    def __init__(self) -> None:
        self.group_id = GROUP_ID
        self.user_id = USER_QQ
        self.message_type = "group"
        self.message_id = 42
        self.time = 1_700_000_000
        self.sender = SimpleNamespace(user_id=USER_QQ, card="小明", nickname="小明", role="member")
        self.to_me = False
        self.reply = None

    def get_message(self):
        return [SimpleNamespace(type="text", data={"text": "今天好热"})]


class _Bot:
    self_id = BOT_QQ

    def __init__(self) -> None:
        self.sent: list[list[Any]] = []

    async def call_api(self, api: str, **kwargs: Any) -> Any:
        if api == "get_group_member_list":
            return [{"user_id": USER_QQ, "nickname": "小明", "card": ""}]
        raise AssertionError(f"unexpected API call: {api}")

    async def send_group_msg(self, *, group_id: int, message: Any) -> dict[str, str]:
        self.sent.append(list(message))
        return {"message_id": str(100 + len(self.sent))}


@pytest.fixture()
def qq(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    module = load_runtime(monkeypatch, tmp_path)
    set_live_config(module, allowed_groups=[GROUP_ID])
    yield module
    module.state._adapter.outbound_idempotency.close()


def _deliver(qq: Any, *, action_type: str, text: str) -> tuple[_Bot, dict[str, Any], Any]:
    """用一个在等回复的队列和一笔主动回复，把一次最终回复交给 send_reply。"""
    bot = _Bot()
    presence = qq.state._adapter.trigger_presence
    stamp = presence.record_proactive(GROUP_ID, time.time())
    waiter = qq.models.QueueReplyWaiter(asyncio.Event(), INBOUND_SEQ)
    qq.state._adapter.qq_waiting_replies[GROUP_KEY] = waiter
    platform = {
        "type": "group", "group_id": GROUP_ID, "conversation_key": GROUP_KEY, "bot": bot, "event": _Event(),
        "_trigger_source": "llm_intent", "_response_purpose": "ambient", "_proactive_stamp": stamp,
    }
    trigger = qq.queueing.TriggerInfo(INBOUND_SEQ, GROUP_KEY, "session", False, platform_data=platform)
    context = DeliveryContext(
        event_id=f"event-{action_type}", idempotency_key=f"delivery:{action_type}",
        source_inbound_seq=INBOUND_SEQ, conversation_key=GROUP_KEY, purpose="ambient", action_type=action_type,
    )
    asyncio.run(qq.callbacks.TangQiuCallbacks().send_reply(trigger, text, [], delivery_context=context))
    return bot, platform, waiter


class TestTheQqAdapter:
    def test_a_silent_turn_sends_nothing_and_leaves_no_trace(self, qq) -> None:
        presence = qq.state._adapter.trigger_presence

        bot, platform, _ = _deliver(qq, action_type="no_reply", text="")

        now = time.time()
        assert bot.sent == []
        assert list(qq.state._adapter.group_history[GROUP_ID]) == []
        assert presence.since_bot_spoke(GROUP_ID, now) is None
        assert presence.is_present(GROUP_ID, now, 90.0) is False
        # 判了要回才记的那笔主动回复退回去：这一轮没开口，不该占额度。
        assert presence.proactive_count(GROUP_ID, now, 300.0) == 0
        assert platform["_qq_final_reply_sent"] is True

    def test_the_queue_waiting_on_the_reply_is_released(self, qq) -> None:
        # 不放行的话，开着 wait_for_reply 的队列要白等到超时。
        _, _, waiter = _deliver(qq, action_type="no_reply", text="")

        assert waiter.event.is_set()
        assert INBOUND_SEQ in qq.state._adapter.qq_completed_replies

    def test_the_marker_wins_over_stray_text(self, qq) -> None:
        bot, _, _ = _deliver(qq, action_type="no_reply", text="这句不该发出去")

        assert bot.sent == []
        assert list(qq.state._adapter.group_history[GROUP_ID]) == []

    def test_an_ordinary_reply_still_goes_out_and_counts(self, qq) -> None:
        presence = qq.state._adapter.trigger_presence

        bot, _, waiter = _deliver(qq, action_type="finish", text="是挺热的")

        now = time.time()
        assert len(bot.sent) == 1
        assert presence.since_bot_spoke(GROUP_ID, now) is not None
        assert presence.is_present(GROUP_ID, now, 90.0) is True
        assert presence.proactive_count(GROUP_ID, now, 300.0) == 1
        assert waiter.event.is_set()
