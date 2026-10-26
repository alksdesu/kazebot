"""发给模型的前缀只往后追加：短答不换工具表、不加 system，按人档案和技能清单不随本轮变，预算不淘汰 constant。

DeepSeek 只有前缀完全一致才按缓存价计费：system、工具表、静态注入块里任何一处随消息变化，后面整段历史都不命中。
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _engine_loop_fixture import (  # noqa: E402
    SESSION_ID,
    ScriptedRegistry,
    call,
    echo,
    make_rctx,
    results_for,
    stored,
    turn,
    use_hooks,
)
import config_layout  # noqa: E402
from engine.builtin import knowledge_inject  # noqa: E402
from engine.builtin.knowledge_inject import (  # noqa: E402
    SKILL_INDEX_NOTE,
    KnowledgeInjector,
    _apply_global_budget,
    _apply_memory_budget,
    _apply_skill_budget,
    build_knowledge_context,
    normalize_memory_entries,
)
from engine.builtin.preempt import _inject_preempt_message  # noqa: E402
from engine.conversation_routing import (  # noqa: E402
    SHORT_REPLY_HINT,
    is_short_reply_input,
    render_history_inputs,
    render_user_input,
    routing_meta,
    turn_input_meta,
    with_short_reply_hint,
)
from engine.conversation_store import ConversationStore, Message, MessageType  # noqa: E402
from engine.inference import ai_step  # noqa: E402
from engine.inference.llm_call import _build_messages_for_provider  # noqa: E402
from engine.inference.message_assembly import assemble_messages_with_injections  # noqa: E402
from engine.inference.tool_format import create_tool_formatter, sanitize_control_tool_history  # noqa: E402
from engine.memory_namespace import conversation_namespace  # noqa: E402
from engine.memory_subjects import record_interaction  # noqa: E402
from engine.node import Node, SkillAccess, ToolAccess  # noqa: E402
from engine.protocol import ACTION_FAIL, ACTION_FINISH  # noqa: E402
from engine.runner import _build_task_context, _message_to_history_dict, _strip_trailing_pseudo_call  # noqa: E402
from providers.base import ProviderResponse  # noqa: E402
from providers.gemini import GeminiProvider  # noqa: E402
from providers.openai import OpenAIProvider  # noqa: E402

_SHORT = {"route_hints": {"response_action": "short_reply"}}
_INSTRUCTION = "大家晚上吃什么"
_CONV_KEY = "qq_group:prefix-stability"
_PERSONA = "你是群里的朋友。"
_TAIL = "人设尾巴：说话带点口癖。"


def _node(tool_mode: str = "native", *, prompt: Any = _PERSONA) -> Node:
    """一个带真工具、委派目标和待办的入口节点：工具表越完整，短答收窄它造成的差别越明显。"""
    return Node(
        id="chat", type="ai", prompt=prompt, tool_mode=tool_mode,
        tool_access=ToolAccess(mode="all"), delegate_targets=["helper"], extra={"todo": True},
    )


def _finish(text: str = "吃火锅！", call_id: str = "f1") -> ProviderResponse:
    return turn(call("finish", {"text": text}, call_id=call_id))


def _run(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    *responses: ProviderResponse,
    node: Node | None = None,
    task_context: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
    instruction: str = _INSTRUCTION,
    task_id: str = "task-1",
    handlers: dict[str, list[Any]] | None = None,
    runtime_cfg: dict[str, Any] | None = None,
) -> SimpleNamespace:
    """跑一整个 run_ai_node，history 由测试给：每次请求时的 messages 和 tools 各留一份深拷贝。"""
    use_hooks(monkeypatch, handlers)
    monkeypatch.setattr(ai_step, "auto_discover_and_register", lambda *args, **kwargs: None)
    monkeypatch.setattr(ai_step, "load_external_plugins", lambda *args, **kwargs: None)
    monkeypatch.setattr(ai_step, "load_runtime_config", lambda *args, **kwargs: copy.deepcopy(runtime_cfg or {}))
    registry = ScriptedRegistry(tools={"probe": echo("probe output")})
    rctx = make_rctx(workspace, task_id=task_id, task_context=task_context)
    rctx.http = SimpleNamespace(post=AsyncMock())  # type: ignore[assignment]
    requests: list[dict[str, Any]] = []
    queue = list(responses)

    async def provider_call(ls: Any, step: int) -> ProviderResponse:
        requests.append({"step": step, "messages": copy.deepcopy(ls.messages), "tools": copy.deepcopy(ls.openai_tools)})
        return queue.pop(0)

    monkeypatch.setattr(ai_step, "_call_llm_with_retry", provider_call)
    action = asyncio.run(ai_step.run_ai_node(
        rctx=rctx,
        provider=SimpleNamespace(name="test"),  # type: ignore[arg-type]
        registry=registry,  # type: ignore[arg-type]
        node=node or _node(),
        instruction=instruction,
        history=list(history or []),
    ))
    return SimpleNamespace(action=action, requests=requests, rctx=rctx, registry=registry)


def _tool_names(tools: list[dict[str, Any]]) -> list[str]:
    return [str((tool.get("function") or {}).get("name") or tool.get("name") or "") for tool in tools]


def _systems(messages: list[dict[str, Any]]) -> list[Any]:
    return [message.get("content") for message in messages if message.get("role") == "system"]


def _turn_inputs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [message for message in messages if message.get("role") == "user" and not message.get("_dynamic")]


def _view(request: dict[str, Any], tool_mode: str, provider: Any) -> list[dict[str, Any]]:
    return _build_messages_for_provider(request["messages"], create_tool_formatter(tool_mode), provider)


def _gemini_system(request: dict[str, Any], tool_mode: str) -> str:
    provider = GeminiProvider(http=None, api_key="test", base_url=None, model="test")  # type: ignore[arg-type]
    body = provider._build_body(_view(request, tool_mode, provider), request["tools"] or None)
    return body["systemInstruction"]["parts"][0]["text"]


def _openai_payload(request: dict[str, Any], tool_mode: str) -> dict[str, Any]:
    provider = OpenAIProvider(http=None, api_key="test", base_url=None, model="test")  # type: ignore[arg-type]
    return provider._build_payload(_view(request, tool_mode, provider), request["tools"] or None)


def _write_tail(workspace: Path) -> None:
    directory = workspace / "config" / "nodes"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "_persona-tail.md").write_text(_TAIL, encoding="utf-8")


def _skill(workspace: Path, name: str, body: str = "", **meta: Any) -> Path:
    path = workspace / "config" / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    head = yaml.safe_dump({"name": name, **meta}, sort_keys=False, allow_unicode=True)
    path.write_text(f"---\n{head}---\n\n{body}\n", encoding="utf-8")
    return path


def _archive(workspace: Path, alias: str, entries: list[dict[str, Any]], *, book: str = "profile") -> None:
    record_interaction(workspace, alias)
    path = workspace / "data" / "memory" / f"user_{alias}" / f"{book}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"book": book, "entries": entries}, allow_unicode=True), encoding="utf-8")


def _conversation_book(workspace: Path, entries: list[dict[str, Any]], *, book: str = "people") -> None:
    path = workspace / "data" / "memory" / conversation_namespace(_CONV_KEY) / f"{book}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"book": book, "entries": entries}, allow_unicode=True), encoding="utf-8")


def _knowledge(
    workspace: Path,
    *,
    node: Node | None = None,
    instruction: str = "随便聊聊",
    subjects: list[str] | None = None,
    runtime_cfg: dict[str, Any] | None = None,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    return build_knowledge_context(
        workspace, node or Node(id="chat", type="ai"), instruction, [], runtime_cfg or {},
        task_context={"conversation_key": _CONV_KEY, "memory_hints": {"subjects": list(subjects or [])}},
    )


def _text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(str(message.get("content") or "") for message in messages)


def _reloaded_input(text: Any, task_context: dict[str, Any], *, task_id: str = "task-0", current_task_id: str = "") -> dict[str, Any]:
    """照 runner 的写法落盘一条当轮输入，再照 runner 的读法重载回来。"""
    context = _build_task_context({"task_context": task_context, "inbound_message_type": task_context.get("inbound_message_type")})
    message = Message(
        id=f"input-{task_id}", role="user", content=copy.deepcopy(text),
        message_type=str(context.get("inbound_message_type") or "") or MessageType.USER_INPUT,
        created_at="2026-10-27T00:00:00+00:00", meta=routing_meta(context),
        source_node_id="chat", source_task_id=task_id,
    )
    return _message_to_history_dict(message, current_task_id=current_task_id)


class TestShortReplyRendering:
    def test_only_a_short_reply_user_input_gets_the_hint(self) -> None:
        short_input = {"response_action": "short_reply", "message_type": MessageType.USER_INPUT}

        assert is_short_reply_input(short_input)
        assert not is_short_reply_input({"response_action": "short_reply", "message_type": MessageType.TOOL_RESULT})
        assert not is_short_reply_input({"response_action": "short_reply", "message_type": "dispatch_result"})
        assert not is_short_reply_input({"response_action": "reply", "message_type": MessageType.USER_INPUT})
        assert not is_short_reply_input(None)
        assert not is_short_reply_input("short_reply")
        assert render_user_input("原话", {"response_action": "reply", "message_type": MessageType.USER_INPUT}) == "原话"

    def test_the_hint_is_added_once_however_often_it_is_rendered(self) -> None:
        once = with_short_reply_hint(_INSTRUCTION)

        assert once == f"{_INSTRUCTION}\n\n{SHORT_REPLY_HINT}"
        assert with_short_reply_hint(once) == once
        assert with_short_reply_hint("   ") == SHORT_REPLY_HINT
        assert with_short_reply_hint(None) is None

    def test_a_multimodal_input_gets_the_hint_as_its_last_text_part(self) -> None:
        content = [{"type": "text", "text": "看图"}, {"type": "image_url", "image_url": {"url": "file://data/a.png"}}]

        rendered = with_short_reply_hint(content)

        assert rendered[:2] == content and rendered[-1] == {"type": "text", "text": SHORT_REPLY_HINT}
        assert with_short_reply_hint(rendered) is rendered
        assert len(content) == 2

    def test_the_hint_does_not_contradict_the_ambient_tail(self) -> None:
        # 插话版尾句只说「想搭话就接一句短的」，字数和交付方式只在这里说一次，而且不替模型决定开不开口
        assert "240" in SHORT_REPLY_HINT and "finish" in SHORT_REPLY_HINT
        assert "要说就" in SHORT_REPLY_HINT

    @pytest.mark.parametrize("task_context", [
        pytest.param(_SHORT, id="short-reply"),
        pytest.param({"route_hints": {"response_action": "reply"}}, id="reply"),
        pytest.param({"route_hints": {"response_action": "short_reply"}, "inbound_message_type": "dispatch_result"}, id="dispatch-result"),
        pytest.param({}, id="no-hints"),
    ])
    def test_the_turn_meta_agrees_with_what_the_runner_stores(self, task_context: dict[str, Any]) -> None:
        reloaded = _reloaded_input(_INSTRUCTION, task_context)
        expected = turn_input_meta(_build_task_context({
            "task_context": task_context, "inbound_message_type": task_context.get("inbound_message_type"),
        }))

        assert {key: reloaded["_meta"].get(key) for key in expected} == expected
        assert render_history_inputs([reloaded])[0]["content"] == render_user_input(_INSTRUCTION, expected)

    def test_earlier_history_renders_the_same_on_every_assembly(self, tmp_path: Path) -> None:
        history = [
            _reloaded_input("平常的一句", {}, task_id="t1"),
            {"role": "assistant", "content": "嗯嗯"},
            _reloaded_input("插一句", _SHORT, task_id="t2"),
            {"role": "assistant", "content": "哈哈"},
            {"role": "user", "content": "工具结果", "_meta": {"response_action": "short_reply", "message_type": "tool_result"}},
        ]
        before = copy.deepcopy(history)
        system = [{"role": "system", "content": _PERSONA}]

        first, _ = assemble_messages_with_injections(
            workspace_root=tmp_path, system_prompt=system, history=history, instruction="下一句",
            turn_meta=turn_input_meta({}),
        )
        second, _ = assemble_messages_with_injections(
            workspace_root=tmp_path, system_prompt=system, history=history, instruction="再下一句",
            turn_meta=turn_input_meta(_SHORT),
        )

        assert first[:len(history) + 1] == second[:len(history) + 1]
        assert [message["content"] for message in first[1:len(history) + 1]] == [
            "平常的一句", "嗯嗯", with_short_reply_hint("插一句"), "哈哈", "工具结果",
        ]
        assert first[-1]["content"] == "下一句"
        assert second[-1]["content"] == with_short_reply_hint("再下一句")
        assert history == before

    @pytest.mark.parametrize("prompt", [
        pytest.param([{"role": "system", "content": _PERSONA}], id="string"),
        pytest.param([{"role": "system", "content": _PERSONA}, {"role": "history"}], id="block"),
    ])
    def test_a_multimodal_turn_input_reloads_in_the_same_task_exactly_as_sent(self, tmp_path: Path, prompt: list) -> None:
        attachments = [{"type": "image", "path": "data/attachments/cat.png"}]
        sent, _ = assemble_messages_with_injections(
            workspace_root=tmp_path, system_prompt=prompt, history=[], instruction="看看这个",
            attachments=attachments, turn_meta=turn_input_meta(_SHORT),
        )
        stored_content = assemble_messages_with_injections(
            workspace_root=tmp_path, system_prompt=prompt, history=[], instruction="看看这个", attachments=attachments,
        )[0][-1]["content"]

        reloaded = render_history_inputs([_reloaded_input(stored_content, _SHORT, task_id="t", current_task_id="t")])

        assert sent[-1]["content"][-1] == {"type": "text", "text": SHORT_REPLY_HINT}
        assert reloaded[0]["content"] == sent[-1]["content"]


class TestShortReplyKeepsThePrefix:
    @pytest.mark.parametrize("tool_mode", ["native", "fake-native"])
    def test_the_tools_sent_to_the_provider_match_a_normal_turn(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tool_mode: str,
    ) -> None:
        normal = _run(monkeypatch, tmp_path / "normal", _finish(), node=_node(tool_mode))
        short = _run(monkeypatch, tmp_path / "short", _finish(), node=_node(tool_mode), task_context=_SHORT)

        assert short.requests[0]["tools"] == normal.requests[0]["tools"]
        assert _openai_payload(short.requests[0], tool_mode)["tools"] == _openai_payload(normal.requests[0], tool_mode)["tools"]
        assert {"probe", "dispatch_to_helper", "switch_node", "update_todos", "reply", "finish"} <= set(
            _tool_names(short.requests[0]["tools"])
        )

    def test_json_mode_sends_the_same_system(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        normal = _run(monkeypatch, tmp_path / "normal", _finish(), node=_node("json"))
        short = _run(monkeypatch, tmp_path / "short", _finish(), node=_node("json"), task_context=_SHORT)

        assert _systems(short.requests[0]["messages"]) == _systems(normal.requests[0]["messages"])
        assert len(_systems(short.requests[0]["messages"])) == 1
        system = _systems(short.requests[0]["messages"])[0]
        assert "dispatch_to_helper" in system and "update_todos" in system
        assert SHORT_REPLY_HINT not in system
        assert short.requests[0]["tools"] == normal.requests[0]["tools"] == []

    @pytest.mark.parametrize("tool_mode", ["native", "json"])
    def test_the_gemini_system_instruction_does_not_change(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tool_mode: str,
    ) -> None:
        for workspace in (tmp_path / "normal", tmp_path / "short"):
            _skill(workspace, "rules", "常驻规则。", strategy="constant")
            _skill(workspace, "recipes", "菜谱正文。", keywords=["菜谱"], description="菜谱")
        handlers = {"before_prompt_build": [KnowledgeInjector()]}
        normal = _run(monkeypatch, tmp_path / "normal", _finish(), node=_node(tool_mode), handlers=handlers)
        short = _run(
            monkeypatch, tmp_path / "short", _finish(), node=_node(tool_mode), handlers=handlers, task_context=_SHORT,
        )

        system = _gemini_system(short.requests[0], tool_mode)
        assert system == _gemini_system(normal.requests[0], tool_mode)
        assert "[SKILLS:CONSTANT]" in system and "[SKILLS:INDEX]" in system
        assert SHORT_REPLY_HINT not in system

    @pytest.mark.parametrize("tool_mode", ["native", "json"])
    def test_the_persona_tail_stays_at_the_end_of_the_static_system(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tool_mode: str,
    ) -> None:
        history = [{"role": "user", "content": "早上好"}, {"role": "assistant", "content": "早！"}]
        node = _node(tool_mode, prompt=f"{_PERSONA}\n{{{{include:_persona-tail.md}}}}")
        for workspace in (tmp_path / "normal", tmp_path / "short"):
            _write_tail(workspace)
        normal = _run(monkeypatch, tmp_path / "normal", _finish(), node=node, history=history)
        short = _run(monkeypatch, tmp_path / "short", _finish(), node=node, history=history, task_context=_SHORT)

        short_messages = _openai_payload(short.requests[0], tool_mode)["messages"]
        normal_messages = _openai_payload(normal.requests[0], tool_mode)["messages"]
        first_turn = next(index for index, message in enumerate(short_messages) if message["role"] != "system")
        assert short_messages[:first_turn] == normal_messages[:first_turn]
        assert short_messages[first_turn - 1]["content"].endswith(_TAIL)
        assert all(message["role"] != "system" for message in short_messages[first_turn:])
        assert _gemini_system(short.requests[0], tool_mode).endswith(_TAIL)

    @pytest.mark.parametrize("prompt", [
        pytest.param(_PERSONA, id="string"),
        pytest.param([{"role": "system", "content": _PERSONA}, {"role": "history"}], id="block"),
    ])
    def test_the_turn_input_carries_the_hint_and_its_task_meta(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, prompt: Any,
    ) -> None:
        older_image = {"role": "user", "content": [{"type": "text", "text": "旧图"}], "_meta": {"source_task_id": "old"}}
        history = [older_image, {"role": "assistant", "content": "收到"}]
        handlers = {"before_prompt_build": [KnowledgeInjector()]}
        short = _run(
            monkeypatch, tmp_path / "short", _finish(), node=_node(prompt=prompt),
            history=history, handlers=handlers, task_context=_SHORT,
        )
        normal = _run(monkeypatch, tmp_path / "normal", _finish(), node=_node(prompt=prompt), history=history, handlers=handlers)

        sent = _turn_inputs(short.requests[0]["messages"])[-1]
        assert sent["content"] == with_short_reply_hint(_INSTRUCTION)
        assert sent["_meta"]["source_task_id"] == "task-1"
        assert sent["_meta"]["response_action"] == "short_reply"
        assert _turn_inputs(short.requests[0]["messages"])[0]["_meta"] == {"source_task_id": "old"}
        assert _turn_inputs(normal.requests[0]["messages"])[-1]["content"] == _INSTRUCTION
        assert not any(message.get("role") == "system" and SHORT_REPLY_HINT in str(message.get("content"))
                       for message in short.requests[0]["messages"])

    @pytest.mark.parametrize("tool_mode", ["native", "json"])
    def test_the_next_task_reloads_the_turn_input_exactly_as_it_was_sent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tool_mode: str,
    ) -> None:
        store = ConversationStore(tmp_path / "data" / "conversations")
        store.append(SESSION_ID, Message(
            id="input-a", role="user", content=_INSTRUCTION, message_type=MessageType.USER_INPUT,
            created_at="2026-10-27T00:00:00+00:00", meta=routing_meta(_SHORT),
            source_node_id="chat", source_task_id="task-a",
        ))
        first = _run(monkeypatch, tmp_path, _finish("吃火锅！"), node=_node(tool_mode), task_context=_SHORT, task_id="task-a")

        history = [
            _message_to_history_dict(message, current_task_id="task-b")
            for message in stored(tmp_path) if message.role != "system"
        ]
        history = _strip_trailing_pseudo_call(sanitize_control_tool_history(history))
        second = _run(
            monkeypatch, tmp_path, _finish("好呀"), node=_node(tool_mode), history=history,
            instruction="那就火锅", task_id="task-b",
        )

        sent = _openai_payload(first.requests[0], tool_mode)
        resent = _openai_payload(second.requests[0], tool_mode)
        assert resent["messages"][:len(sent["messages"])] == sent["messages"]
        assert resent.get("tools") == sent.get("tools")
        assert stored(tmp_path)[0].content == _INSTRUCTION


class TestShortReplyRefusals:
    def test_a_real_tool_is_refused_and_finish_delivers_on_the_second_step(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        run = _run(
            monkeypatch, tmp_path,
            turn(call("probe", {"q": "附近的店"}, call_id="p1")), _finish("吃火锅！"),
            task_context=_SHORT,
        )

        assert run.action.action == ACTION_FINISH and run.action.result["text"] == "吃火锅！"
        assert run.registry.executed == []
        [refusal] = results_for(run.requests[1]["messages"], "p1")
        assert refusal["content"] == ai_step._SHORT_REPLY_REJECT_MESSAGE.format(tool="probe")
        assert "简短回应" in refusal["content"] and "finish" in refusal["content"]

    def test_the_second_step_sees_the_same_tools_and_no_wrap_up_hint(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        run = _run(monkeypatch, tmp_path, turn(call("probe", call_id="p1")), _finish(), task_context=_SHORT)

        assert [request["step"] for request in run.requests] == [0, 1]
        assert run.requests[1]["tools"] == run.requests[0]["tools"]
        assert not any("最后一步" in str(message.get("content")) for message in run.requests[1]["messages"])

    def test_wrap_up_stays_off_even_with_more_steps(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # 收尾那一步会把 native 的工具表收窄成只剩 finish：短答的步数将来调大也不能走到那里
        monkeypatch.setattr(ai_step, "_SHORT_REPLY_MAX_STEPS", 3)

        run = _run(
            monkeypatch, tmp_path,
            turn(call("probe", call_id="p1")), turn(call("probe", call_id="p2")), _finish(),
            task_context=_SHORT,
        )

        assert run.action.action == ACTION_FINISH
        assert run.requests[2]["tools"] == run.requests[0]["tools"]
        assert not any("最后一步" in str(message.get("content")) for message in run.requests[2]["messages"])

    def test_an_async_dispatch_is_refused_and_finish_delivers(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        run = _run(
            monkeypatch, tmp_path,
            turn(call("dispatch_to_helper", {"instruction": "查查附近的店"}, call_id="d1")), _finish("吃面"),
            task_context=_SHORT,
        )

        assert run.action.action == ACTION_FINISH and run.action.result["text"] == "吃面"
        run.rctx.http.post.assert_not_awaited()
        assert len(results_for(run.requests[1]["messages"], "d1")) == 1

    def test_a_wait_dispatch_is_refused_and_finish_delivers(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        run = _run(
            monkeypatch, tmp_path,
            turn(call("dispatch_to_helper", {"instruction": "查查", "wait": True}, call_id="w1")), _finish("吃粥"),
            task_context=_SHORT,
        )

        assert run.action.action == ACTION_FINISH and run.action.result["text"] == "吃粥"
        [refusal] = results_for(run.requests[1]["messages"], "w1")
        assert json.loads(refusal["content"])["success"] is False

    def test_text_beside_a_refused_tool_waits_for_the_second_step(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        run = _run(
            monkeypatch, tmp_path,
            turn(call("probe", call_id="p1"), text="我先查查"), _finish("吃饺子"),
            task_context=_SHORT,
        )

        assert run.action.action == ACTION_FINISH and run.action.result["text"] == "吃饺子"
        assert len(run.requests) == 2

    def test_on_the_last_step_text_beside_a_refused_tool_is_still_delivered_short(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        run = _run(
            monkeypatch, tmp_path,
            turn(call("probe", call_id="p1"), text="第一句"),
            turn(call("probe", call_id="p2"), text="第二句。" * 100),
            task_context=_SHORT,
        )

        assert run.action.action == ACTION_FINISH
        assert run.action.result["unauthorized_tool_recovery"] is True
        assert run.action.result["text"].startswith("第二句。") and len(run.action.result["text"]) <= 240
        assert run.registry.executed == []

    def test_refused_tools_on_both_steps_fail(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        run = _run(
            monkeypatch, tmp_path, turn(call("probe", call_id="p1")), turn(call("probe", call_id="p2")),
            task_context=_SHORT,
        )

        assert run.action.action == ACTION_FAIL
        assert len(run.requests) == 2
        assert run.registry.executed == []

    def test_a_node_without_real_tools_still_finishes_on_its_text_at_once(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        # 系统输出型节点的空转保护和短答无关，照旧第一步就交付
        node = Node(id="summarizer", type="ai", prompt="总结。", tool_mode="native")

        run = _run(monkeypatch, tmp_path, turn(call("probe", call_id="p1"), text="摘要正文"), node=node)

        assert run.action.action == ACTION_FINISH and run.action.result["text"] == "摘要正文"
        assert len(run.requests) == 1


def _preempt_loop(root: Path, payload: dict[str, Any], task_context: dict[str, Any]) -> SimpleNamespace:
    history = [{"role": "user", "content": "old request"}]
    return SimpleNamespace(
        node=Node(id="chat", type="ai"), runtime_cfg={}, preempt_inject_info=payload,
        rctx=SimpleNamespace(
            workspace_root=root, task_context={"platform_auth": {"is_admin": False}, **task_context},
            session_id="branch", child_session_id="", task_id="task",
            conversation_store=ConversationStore(root / "data" / "conversations"),
            consume_preempt=AsyncMock(), emit_event=AsyncMock(),
        ),
        messages=[{"role": "system", "content": _PERSONA}, *history],
        history=history, system_prompt=[{"role": "system", "content": _PERSONA}], is_block_mode=False,
    )


class TestPreemptedInput:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [
        pytest.param({"message": "那喝点什么"}, id="text"),
        pytest.param({"message": "这家呢", "attachments": [{"type": "image", "path": "data/attachments/menu.png"}]}, id="image"),
    ])
    async def test_a_short_reply_input_is_sent_as_its_reload_renders_it(self, tmp_path: Path, payload: dict[str, Any]) -> None:
        loop = _preempt_loop(tmp_path, payload, _SHORT)

        await _inject_preempt_message(SimpleNamespace(messages=loop.messages, step=1), loop)

        sent = loop.messages[-1]
        [persisted] = ConversationStore(tmp_path / "data" / "conversations").load("branch")
        reloaded = render_history_inputs([_message_to_history_dict(persisted, current_task_id="task")])[0]
        assert reloaded["content"] == sent["content"] == with_short_reply_hint(persisted.content)
        assert SHORT_REPLY_HINT not in json.dumps(persisted.content, ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_a_normal_input_is_sent_and_stored_unchanged(self, tmp_path: Path) -> None:
        loop = _preempt_loop(tmp_path, {"message": "那喝点什么"}, {"route_hints": {"response_action": "reply"}})

        await _inject_preempt_message(SimpleNamespace(messages=loop.messages, step=1), loop)

        [persisted] = ConversationStore(tmp_path / "data" / "conversations").load("branch")
        assert loop.messages[-1]["content"] == persisted.content == "那喝点什么"


class TestSubjectArchives:
    def test_constant_archive_entries_load_as_pinned_subject_entries(self) -> None:
        [pinned, plain] = normalize_memory_entries(
            [{"id": "a", "content": "x", "constant": True}, {"id": "b", "content": "y"}], always_active=True,
        )

        assert (pinned["strategy"], pinned["constant"]) == ("subject", True)
        assert (plain["strategy"], plain["constant"]) == ("subject", False)

    def test_a_quoted_false_constant_stays_off(self) -> None:
        [entry] = normalize_memory_entries([{"id": "a", "content": "x", "constant": "false", "keywords": ["k"]}])

        assert (entry["strategy"], entry["constant"]) == ("keyword", False)

    def test_a_constant_archive_entry_goes_to_the_active_block(self, tmp_path: Path) -> None:
        _archive(tmp_path, "UserA", [{"id": "lang", "content": "永远用中文回他", "constant": True}])

        _ss, _sd, static, dynamic = _knowledge(tmp_path, subjects=["UserA"])

        assert "永远用中文回他" in _text(dynamic) and "[MEMORY:ACTIVE]" in _text(dynamic)
        assert "永远用中文回他" not in _text(static)

    def test_the_static_block_does_not_depend_on_who_is_talking(self, tmp_path: Path) -> None:
        _conversation_book(tmp_path, [{"id": "group_rule", "content": "本群不聊政治", "constant": True}])
        _archive(tmp_path, "UserA", [{"id": "a_pref", "content": "甲爱吃辣", "constant": True}])
        _archive(tmp_path, "UserB", [{"id": "b_pref", "content": "乙不吃香菜", "constant": True}])

        builds = {
            who: _knowledge(tmp_path, subjects=subjects)
            for who, subjects in {"a": ["UserA"], "b": ["UserB"], "both": ["UserA", "UserB"], "none": []}.items()
        }

        statics = {who: build[2] for who, build in builds.items()}
        assert statics["a"] == statics["b"] == statics["both"] == statics["none"]
        assert "本群不聊政治" in _text(statics["a"]) and "[MEMORY:CONSTANT]" in _text(statics["a"])
        assert "甲爱吃辣" in _text(builds["a"][3]) and "乙不吃香菜" not in _text(builds["a"][3])
        assert "乙不吃香菜" in _text(builds["b"][3]) and "甲爱吃辣" not in _text(builds["b"][3])

    def test_the_active_block_does_not_depend_on_who_spoke_first(self, tmp_path: Path) -> None:
        _archive(tmp_path, "UserA", [{"id": "nickname", "content": "甲叫阿甲"}, {"id": "rule", "content": "甲的规矩", "constant": True}])
        _archive(tmp_path, "UserB", [{"id": "nickname", "content": "乙叫阿乙"}, {"id": "rule", "content": "乙的规矩", "constant": True}])

        first = _knowledge(tmp_path, subjects=["UserA", "UserB"])[3]
        second = _knowledge(tmp_path, subjects=["UserB", "UserA"])[3]

        assert first == second
        assert _text(first).count("## nickname") == 2 and _text(first).count("## rule") == 2

    @pytest.mark.parametrize("runtime_cfg", [
        pytest.param({"memory": {"max_budget_chars": 100}}, id="memory-budget"),
        pytest.param({"knowledge": {"max_budget_chars": 100}}, id="knowledge-budget"),
    ])
    def test_constant_archive_entries_survive_a_tight_budget(self, tmp_path: Path, runtime_cfg: dict[str, Any]) -> None:
        _conversation_book(tmp_path, [{"id": "group_rule", "content": "群规" * 10, "constant": True}])
        _archive(tmp_path, "UserA", [
            {"id": "pinned", "content": "甲" * 300, "constant": True},
            {"id": "recalled", "content": "乙" * 50},
        ])

        _ss, _sd, static, dynamic = _knowledge(tmp_path, subjects=["UserA"], runtime_cfg=runtime_cfg)

        assert "甲" * 300 in _text(dynamic)
        assert "乙" * 50 not in _text(dynamic)
        assert "群规" * 10 in _text(static)

    def test_pinned_archive_entries_record_no_hits(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: list[str] = []
        monkeypatch.setattr(knowledge_inject, "record_hits", lambda _root, entries: recorded.extend(e["id"] for e in entries))
        _archive(tmp_path, "UserA", [{"id": "pinned", "content": "常驻", "constant": True}, {"id": "plain", "content": "普通"}])

        _knowledge(tmp_path, subjects=["UserA"])

        assert recorded == ["plain"]

    def test_a_new_speaker_keeps_the_system_prefix_in_a_real_turn(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        handlers = {"before_prompt_build": [KnowledgeInjector()]}
        runs = []
        for alias, other in (("UserA", "UserB"), ("UserB", "UserA")):
            workspace = tmp_path / alias
            _conversation_book(workspace, [{"id": "group_rule", "content": "本群不聊政治", "constant": True}])
            _archive(workspace, alias, [{"id": "pref", "content": f"{alias} 的常驻偏好", "constant": True}])
            _archive(workspace, other, [{"id": "pref", "content": f"{other} 的常驻偏好", "constant": True}])
            context = {"conversation_key": _CONV_KEY, "memory_hints": {"subjects": [alias]}}
            runs.append(_run(monkeypatch, workspace, _finish(), handlers=handlers, task_context=context))

        assert _systems(runs[0].requests[0]["messages"]) == _systems(runs[1].requests[0]["messages"])
        assert "UserA 的常驻偏好" in _text([m for m in runs[0].requests[0]["messages"] if m.get("_dynamic")])
        assert "UserB 的常驻偏好" in _text([m for m in runs[1].requests[0]["messages"] if m.get("_dynamic")])


class TestSkillIndex:
    def _catalog(self, workspace: Path) -> None:
        _skill(workspace, "rules", "常驻规则正文", strategy="constant")
        _skill(workspace, "recipes", "菜谱正文", keywords=["菜谱"], description="做菜")
        _skill(workspace, "travel", "出行正文", keywords=["出行"], description="出门")
        _skill(workspace, "notes", "笔记正文", description="没有关键词")
        _skill(workspace, "guard", "守则正文", strategy="output", trigger="BAD")
        _skill(workspace, "retired", "停用正文", keywords=["菜谱"], enabled=False)
        _skill(workspace, "elsewhere", "别处正文", keywords=["菜谱"], node_ids=["another-node"])

    def test_the_index_is_static_and_lists_every_usable_normal_skill(self, tmp_path: Path) -> None:
        self._catalog(tmp_path)

        skill_static, skill_dynamic, _ms, _md = _knowledge(tmp_path, instruction="今天菜谱")

        index = next(message["content"] for message in skill_static if message["content"].startswith("[SKILLS:INDEX]"))
        assert [line for line in index.splitlines() if line.startswith("- name: ")] == [
            "- name: notes", "- name: recipes", "- name: travel",
        ]
        assert "[SKILLS:INDEX]" not in _text(skill_dynamic)
        assert "菜谱正文" in _text(skill_dynamic) and "常驻规则正文" in _text(skill_static)

    def test_the_static_part_is_byte_identical_whatever_skill_matches(self, tmp_path: Path) -> None:
        self._catalog(tmp_path)

        builds = [_knowledge(tmp_path, instruction=text) for text in ("今天菜谱", "明天出行", "随便聊聊")]

        assert builds[0][0] == builds[1][0] == builds[2][0]
        assert "菜谱正文" in _text(builds[0][1]) and "出行正文" in _text(builds[1][1]) and builds[2][1] == []

    def test_the_index_format_and_order(self, tmp_path: Path) -> None:
        paths = {
            "zeta": _skill(tmp_path, "zeta", "z", keywords=["zeta"], description="最后一个", order=0),
            "alpha": _skill(tmp_path, "alpha", "a", description="排在后面", order=5),
            "mid": _skill(tmp_path, "mid", "m", keywords=["mid"], description="中间", order=0),
        }

        [index] = _knowledge(tmp_path)[0]

        lines = ["[SKILLS:INDEX]", SKILL_INDEX_NOTE]
        for name, description in (("mid", "中间"), ("zeta", "最后一个"), ("alpha", "排在后面")):
            lines.extend([
                f"- name: {name}", f"  description: {description}",
                f"  path: {config_layout.logical_path(tmp_path, paths[name])}",
            ])
        lines.append("[/SKILLS:INDEX]")
        assert index == {"role": "system", "content": "\n".join(lines)}

    @pytest.mark.parametrize("runtime_cfg", [
        pytest.param({"skills": {"max_budget_chars": 10}}, id="skill-budget"),
        pytest.param({"knowledge": {"max_budget_chars": 10}}, id="knowledge-budget"),
    ])
    def test_a_skill_squeezed_out_by_the_budget_is_not_moved_into_a_dynamic_index(
        self, tmp_path: Path, runtime_cfg: dict[str, Any],
    ) -> None:
        _skill(tmp_path, "recipes", "菜谱正文" * 50, keywords=["菜谱"], description="做菜")

        skill_static, skill_dynamic, _ms, _md = _knowledge(tmp_path, instruction="今天菜谱", runtime_cfg=runtime_cfg)

        assert skill_dynamic == []
        assert "- name: recipes" in _text(skill_static)

    def test_skills_the_node_may_not_use_are_not_listed(self, tmp_path: Path) -> None:
        self._catalog(tmp_path)
        node = Node(id="chat", type="ai", skill_access=SkillAccess(mode="allowlist", allow=["recipes"]))

        skill_static, _sd, _ms, _md = _knowledge(tmp_path, node=node)

        names = [line for line in _text(skill_static).splitlines() if line.startswith("- name: ")]
        assert names == ["- name: recipes"]
        for hidden in ("notes", "travel", "guard", "retired", "elsewhere"):
            assert f"- name: {hidden}" not in _text(skill_static)

    def test_a_node_with_skills_off_gets_no_index(self, tmp_path: Path) -> None:
        self._catalog(tmp_path)

        skill_static, skill_dynamic, _ms, _md = _knowledge(tmp_path, node=Node(id="chat", type="ai", skill_access=SkillAccess(mode="none")))

        assert skill_static == [] and skill_dynamic == []

    def test_the_index_changes_only_when_the_skill_files_change(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(knowledge_inject._SkillCache, "_TTL", -1.0)
        self._catalog(tmp_path)
        before = _knowledge(tmp_path)[0]

        _skill(tmp_path, "recipes", "改过的菜谱正文", keywords=["菜谱"], description="做菜")
        assert _knowledge(tmp_path)[0] == before

        _skill(tmp_path, "recipes", "改过的菜谱正文", keywords=["菜谱"], description="做菜和甜点")
        assert _knowledge(tmp_path)[0] != before

    def test_a_different_match_keeps_the_system_prefix_in_a_real_turn(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        handlers = {"before_prompt_build": [KnowledgeInjector()]}
        runs = []
        for instruction in ("今天菜谱", "明天出行"):
            workspace = tmp_path / str(len(runs))
            self._catalog(workspace)
            runs.append(_run(monkeypatch, workspace, _finish(), handlers=handlers, instruction=instruction))

        assert _systems(runs[0].requests[0]["messages"]) == _systems(runs[1].requests[0]["messages"])
        assert "菜谱正文" in _text([m for m in runs[0].requests[0]["messages"] if m.get("_dynamic")])
        assert "出行正文" in _text([m for m in runs[1].requests[0]["messages"] if m.get("_dynamic")])


def _entry(entry_id: str, chars: int, **fields: Any) -> dict[str, Any]:
    return {"id": entry_id, "content": "x" * chars, **fields}


def _buckets(**buckets: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    empty = {"skill_constant": [], "skill_active": [], "skill_index": [], "memory_constant": [], "memory_active": []}
    return {**empty, **{name: list(entries) for name, entries in buckets.items()}}


class TestBudgetsKeepConstants:
    def test_the_skill_budget_never_evicts_a_constant_skill(self, caplog: pytest.LogCaptureFixture) -> None:
        buckets = _buckets(
            skill_constant=[_entry("rules", 500, priority=0)],
            skill_active=[_entry("big", 100, priority=9), _entry("small", 40, priority=1)],
        )

        with caplog.at_level(logging.WARNING):
            _apply_skill_budget(buckets, 200)

        assert [e["id"] for e in buckets["skill_constant"]] == ["rules"]
        assert [e["id"] for e in buckets["skill_active"]] == ["small"]
        assert buckets["skill_index"] == []
        assert any("skill budget: constant entries alone" in record.getMessage() for record in caplog.records)

    def test_matched_skills_still_compete_by_priority(self) -> None:
        buckets = _buckets(
            skill_constant=[_entry("rules", 50)],
            skill_active=[_entry("low", 100, priority=1), _entry("high", 60, priority=9)],
        )

        _apply_skill_budget(buckets, 200)

        assert [e["id"] for e in buckets["skill_active"]] == ["high"]
        assert buckets["skill_index"] == []

    def test_a_healthy_skill_budget_logs_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        buckets = _buckets(skill_constant=[_entry("rules", 50)], skill_active=[_entry("a", 50)])

        with caplog.at_level(logging.WARNING):
            _apply_skill_budget(buckets, 200)

        assert [e["id"] for e in buckets["skill_active"]] == ["a"]
        assert not [record for record in caplog.records if "constant entries alone" in record.getMessage()]

    def test_the_knowledge_budget_never_evicts_constants(self, caplog: pytest.LogCaptureFixture) -> None:
        buckets = _buckets(
            skill_constant=[_entry("skill_rules", 150, priority=0)],
            memory_constant=[_entry("group_rule", 100, priority=0)],
            skill_active=[_entry("recipes", 30, priority=9)],
            memory_active=[
                _entry("pinned", 80, strategy="subject", constant=True, subject="UserA"),
                _entry("recalled", 40, priority=1, subject="UserA"),
            ],
        )

        with caplog.at_level(logging.WARNING):
            _apply_global_budget(buckets, 200, ["UserA"])

        assert [e["id"] for e in buckets["skill_constant"]] == ["skill_rules"]
        assert [e["id"] for e in buckets["memory_constant"]] == ["group_rule"]
        assert [e["id"] for e in buckets["skill_active"]] == ["recipes"]
        assert [e["id"] for e in buckets["memory_active"]] == ["pinned"]
        assert buckets["skill_index"] == []
        assert any("knowledge budget: constant entries alone" in record.getMessage() for record in caplog.records)

    def test_the_memory_budget_keeps_pinned_archive_entries(self, caplog: pytest.LogCaptureFixture) -> None:
        buckets = _buckets(memory_active=[
            _entry("pinned", 300, strategy="subject", constant=True, subject="UserA"),
            _entry("recalled", 100, priority=9, subject="UserA"),
        ])

        with caplog.at_level(logging.WARNING):
            _apply_memory_budget(buckets, 200, ["UserA"])

        assert [e["id"] for e in buckets["memory_active"]] == ["pinned"]
        assert any("memory budget: constant entries alone" in record.getMessage() for record in caplog.records)

    def test_a_zero_budget_keeps_everything(self) -> None:
        buckets = _buckets(skill_constant=[_entry("rules", 9999)], skill_active=[_entry("a", 9999)])

        _apply_skill_budget(buckets, 0)
        _apply_global_budget(buckets, 0)

        assert [e["id"] for e in buckets["skill_constant"]] == ["rules"]
        assert [e["id"] for e in buckets["skill_active"]] == ["a"]
