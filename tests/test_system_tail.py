"""尾部片段在框架注入之后进入原生 System。"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from engine.attachments import prepare_messages_for_llm
from engine.builtin import fallback_provider
from engine.builtin.knowledge_inject import _rebuild_prompt_messages
from engine.builtin.preempt import _inject_preempt_message
from engine.compact import total_content_chars
from engine.conversation_store import ConversationStore
from engine.inference.llm_call import _build_messages_for_provider, _call_llm_with_retry
from engine.inference.message_assembly import assemble_messages_with_injections
from engine.inference.tool_format import create_tool_formatter
from engine.node import Node
from engine.prompt import SYSTEM_TAIL_KEY, assemble_prompt, finalize_system_tail
from providers.anthropic import AnthropicProvider
from providers.base import ProviderResponse
from providers.gemini import GeminiProvider
from providers.openai import OpenAIProvider
from providers.openai_responses import OpenAIResponsesProvider


TAIL = "尾部系统正文 {{node_id}}"
RENDERED_TAIL = "尾部系统正文 entry"
DIRECTIVE = "{{include:_persona-tail.md}}"
GUIDE = {"sections": [
    {"key": "stable", "text": "平台固定说明", "volatile": False},
    {"key": "turn", "text": "平台本轮说明", "volatile": True},
]}
TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "读取文件", "parameters": {"type": "object"},
}}]


def write_fragment(root: Path, content: str = TAIL, name: str = "_persona-tail.md") -> None:
    directory = root / "config/nodes"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(content, encoding="utf-8")


def render(root: Path, prompt: str | list) -> list[dict]:
    return assemble_prompt(root, Node(id="entry", type="ai", prompt=prompt))


def assemble(root: Path, prompt: str | list, mode: str):
    system = render(root, prompt)
    messages, _ = assemble_messages_with_injections(
        workspace_root=root, system_prompt=system,
        history=[{"role": "user", "content": "历史问题"}], instruction="本轮问题",
        skill_static=[{"role": "system", "content": "常驻技能"}],
        memory_static=[{"role": "system", "content": "常驻记忆"}],
        skill_dynamic=[{"role": "system", "content": "动态技能"}],
        output_guide=GUIDE,
    )
    formatter = create_tool_formatter(mode)
    first = next(message for message in messages if message["role"] == "system")
    first["content"], api_tools = formatter.inject_tool_definitions(TOOLS, first["content"])
    return messages, formatter, api_tools


def wire_system(provider, messages, tools) -> str:
    if provider.name == "gemini":
        body = provider._build_body(messages, tools)
        return body["systemInstruction"]["parts"][0]["text"]
    if provider.name == "anthropic":
        return provider._build_payload(messages)["system"]
    if provider.name == "openai-responses":
        instructions, _ = provider._convert_messages(messages)
        return instructions
    body = provider._build_payload(messages, tools)
    return "\n\n".join(message["content"] for message in body["messages"] if message["role"] == "system")


@pytest.mark.parametrize("mode", ["json", "native", "fake-native"])
@pytest.mark.parametrize("provider_type", [GeminiProvider, AnthropicProvider, OpenAIProvider, OpenAIResponsesProvider])
@pytest.mark.parametrize("prompt", [
    f"节点规则\n{DIRECTIVE}",
    f"节点规则\n# %%DYNAMIC%%\n时间: {{{{now}}}}\n{DIRECTIVE}",
    [{"role": "system", "content": "节点规则"}, {"role": "history"},
     {"role": "system", "content": DIRECTIVE}, {"role": "system", "content": "后置规则", "depth": 0}],
])
def test_tail_is_last_in_actual_provider_system(tmp_path: Path, mode, provider_type, prompt) -> None:
    write_fragment(tmp_path)
    messages, formatter, tools = assemble(tmp_path, prompt, mode)
    before = copy.deepcopy(messages)
    provider = provider_type(http=None, api_key="test", base_url=None, model="test")

    for _ in range(2):
        view = prepare_messages_for_llm(_build_messages_for_provider(messages, formatter, provider), tmp_path)
        system = wire_system(provider, view, tools)
        assert system.endswith(RENDERED_TAIL)
        assert system.count(RENDERED_TAIL) == 1
        for text in ("节点规则", "常驻技能", "常驻记忆", "[OUTPUT_GUIDE:CONSTANT]"):
            assert system.index(text) < system.index(RENDERED_TAIL)
        if mode == "json":
            assert system.index("[TOOL_USE_INSTRUCTIONS]") < system.index(RENDERED_TAIL)
        assert "动态技能" not in system and "平台本轮说明" not in system
        assert SYSTEM_TAIL_KEY not in json.dumps(view)
        assert all(RENDERED_TAIL not in str(message.get("content")) for message in view if message["role"] != "system")
        assert messages == before


@pytest.mark.parametrize("node_id", ["qq.orchestrator", "qq.orchestrator.example", "bootstrap.shell_orchestrator"])
def test_bundled_entries_keep_tail_out_of_dynamic_user_content(tmp_path: Path, node_id: str) -> None:
    write_fragment(tmp_path)
    path = Path(__file__).resolve().parents[1] / "defaults/nodes" / f"{node_id}.yaml"
    prompt = yaml.safe_load(path.read_text(encoding="utf-8"))["prompt"]
    messages, formatter, tools = assemble(tmp_path, prompt, "json")
    provider = GeminiProvider(http=None, api_key="test", base_url=None, model="test")
    system = wire_system(provider, _build_messages_for_provider(messages, formatter, provider), tools)
    assert system.endswith(RENDERED_TAIL)
    assert system.count(RENDERED_TAIL) == 1


def test_nested_and_repeated_includes_inject_once(tmp_path: Path) -> None:
    write_fragment(tmp_path)
    write_fragment(tmp_path, f"共享规则\n{DIRECTIVE}", "_nested.md")
    messages = render(tmp_path, f"{{{{include:_nested.md}}}}\n{DIRECTIVE}")
    assert messages[0][SYSTEM_TAIL_KEY] == RENDERED_TAIL
    assert TAIL not in messages[0]["content"]


@pytest.mark.parametrize("tail", ["", " \n "])
def test_empty_fragment_is_disabled(tmp_path: Path, tail: str) -> None:
    write_fragment(tmp_path, tail)
    messages = render(tmp_path, f"规则\n{DIRECTIVE}")
    assert SYSTEM_TAIL_KEY not in messages[0]
    assert DIRECTIVE not in messages[0]["content"]


def test_unreferenced_fragment_does_not_affect_other_nodes(tmp_path: Path) -> None:
    write_fragment(tmp_path)
    assert render(tmp_path, "普通规则") == [{"role": "system", "content": "普通规则"}]


def test_tail_whitespace_and_indentation_are_preserved(tmp_path: Path) -> None:
    tail = "    print('保留缩进')\n\n"
    write_fragment(tmp_path, tail)
    messages = render(tmp_path, f"规则\n{DIRECTIVE}")
    assert messages[0][SYSTEM_TAIL_KEY] == tail
    assert finalize_system_tail(messages)[0]["content"].endswith(tail)


def test_tail_is_not_taken_from_user_template_variables(tmp_path: Path) -> None:
    write_fragment(tmp_path)
    messages = assemble_prompt(tmp_path, Node(id="entry", type="ai", prompt="{{instruction}}"), variables={"instruction": DIRECTIVE})
    assert messages == [{"role": "system", "content": DIRECTIVE}]


def test_missing_optional_fragment_has_no_unexpanded_directive(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("engine.prompt._include_file", lambda *_args: None)
    assert render(tmp_path, f"规则\n{DIRECTIVE}") == [{"role": "system", "content": "规则\n"}]


def test_user_include_is_not_promoted_to_system(tmp_path: Path) -> None:
    write_fragment(tmp_path)
    messages = render(tmp_path, [{"role": "user", "content": DIRECTIVE}])
    assert not any(SYSTEM_TAIL_KEY in message for message in messages)
    assert messages[0] == {"role": "user", "content": RENDERED_TAIL}


def test_marker_inside_tail_does_not_split_node_prompt(tmp_path: Path) -> None:
    write_fragment(tmp_path, "尾部\n# %%DYNAMIC%%\n完整正文")
    messages = render(tmp_path, f"节点规则\n{DIRECTIVE}")
    assert len(messages) == 1
    assert messages[0][SYSTEM_TAIL_KEY] == "尾部\n# %%DYNAMIC%%\n完整正文"


def test_tail_only_template_and_dynamic_section_stay_distinct(tmp_path: Path) -> None:
    write_fragment(tmp_path)
    system = render(tmp_path, f"{DIRECTIVE}\n# %%DYNAMIC%%\n本轮动态信息")
    assert system[0]["content"] == ""
    assert system[1]["content"] == "本轮动态信息"
    assert system[0][SYSTEM_TAIL_KEY] == RENDERED_TAIL


def test_finalization_without_formatter_supports_multimodal_system_and_is_idempotent() -> None:
    messages = [
        {"role": "system", "content": "基础", SYSTEM_TAIL_KEY: "尾部"},
        {"role": "system", "content": [{"type": "text", "text": "规则"}]},
        {"role": "user", "content": "输入"},
    ]
    before = copy.deepcopy(messages)
    view = _build_messages_for_provider(messages, None, SimpleNamespace(name="test"))
    assert view[1]["content"][-1] == {"type": "text", "text": "尾部"}
    assert finalize_system_tail(view) == view
    assert messages == before
    assert total_content_chars(messages) == total_content_chars(view)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("failure_status", [502, 413])
async def test_live_call_retries_keep_tail_at_system_end(tmp_path: Path, streaming: bool, failure_status: int) -> None:
    write_fragment(tmp_path)
    messages, formatter, tools = assemble(tmp_path, f"节点规则\n{DIRECTIVE}", "json")
    messages.extend([
        {"role": "assistant", "content": "", "tool_calls": [{"id": "read-1", "name": "read_file", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "read-1", "name": "read_file", "content": "x" * 20_000},
    ])
    before = copy.deepcopy(messages)
    requests = []

    class Provider:
        name = "gemini"
        model = "test"

        async def chat(self, *, messages, tools):
            requests.append(GeminiProvider._convert_messages(messages)[0])
            if len(requests) == 1:
                return ProviderResponse(ok=False, error="upstream failure", status_code=failure_status)
            return ProviderResponse(ok=True, text="done")

        async def chat_stream(self, *, messages, tools, **callbacks):
            return await self.chat(messages=messages, tools=tools)

    loop = SimpleNamespace(
        messages=messages, formatter=formatter, provider=Provider(), node=Node(id="entry", type="ai"),
        openai_tools=tools, use_stream=streaming, retry_max=1, retry_initial_delay=0, retry_backoff=1, retry_max_delay=0,
        rctx=SimpleNamespace(workspace_root=tmp_path, task_id="task", begin_llm_request=lambda: "request", emit_event=AsyncMock()),
    )
    response = await _call_llm_with_retry(loop, 0)
    assert response.ok and len(requests) == 2
    assert all(system.endswith(RENDERED_TAIL) and system.count(RENDERED_TAIL) == 1 for system in requests)
    assert messages == before


@pytest.mark.asyncio
async def test_fallback_gets_the_same_tail_order(tmp_path: Path, monkeypatch) -> None:
    write_fragment(tmp_path)
    messages, formatter, tools = assemble(tmp_path, f"基础\n{DIRECTIVE}", "json")
    captured = {}

    class Backup:
        name = "gemini"
        model = "backup"

        async def chat(self, *, messages, tools):
            captured["messages"] = messages
            return ProviderResponse(ok=True, text="done")

    monkeypatch.setattr(fallback_provider, "_load_config", lambda _root: {"fallbacks": [
        {"provider": "gemini", "base_url": "https://example.test", "api_key": "test", "model": "backup"},
    ]})
    monkeypatch.setattr(fallback_provider, "_create_fallback_provider", lambda **_kwargs: Backup())
    ctx = SimpleNamespace(
        response=ProviderResponse(ok=False, error="temporary", status_code=502), messages=messages, tools=tools,
        provider=SimpleNamespace(name="gemini", model="primary"), node=Node(id="entry", type="ai"),
        rctx=SimpleNamespace(workspace_root=tmp_path), extra={"loop_state": SimpleNamespace(formatter=formatter)},
    )
    await fallback_provider.FallbackProviderHandler().handle(ctx)
    assert ctx.response.ok
    assert GeminiProvider._convert_messages(captured["messages"])[0].endswith(RENDERED_TAIL)


@pytest.mark.asyncio
async def test_knowledge_rebuild_and_preempt_keep_tail_after_replaced_guide(tmp_path: Path) -> None:
    write_fragment(tmp_path)
    system = render(tmp_path, f"节点规则\n{DIRECTIVE}")
    history = [{"role": "user", "content": "原始问题"}]
    rctx = SimpleNamespace(
        workspace_root=tmp_path, task_context={}, session_id="branch", child_session_id="",
        task_id="task", conversation_store=ConversationStore(tmp_path / "data/conversations"),
        consume_preempt=AsyncMock(), emit_event=AsyncMock(),
    )
    ctx = SimpleNamespace(messages=[], rctx=rctx, extra={
        "system_prompt": system, "history": history, "instruction_text": "原始问题",
        "output_guide": GUIDE,
    })
    rctx.task_context["output_guide"] = GUIDE
    _rebuild_prompt_messages(ctx)
    loop = SimpleNamespace(
        rctx=rctx, messages=ctx.messages, history=history, system_prompt=system, is_block_mode=False,
        node=Node(id="entry", type="ai"), runtime_cfg={},
        preempt_inject_info={"message": "追加问题", "output_guide": GUIDE},
    )
    await _inject_preempt_message(SimpleNamespace(messages=loop.messages, step=1), loop)
    view = _build_messages_for_provider(loop.messages, create_tool_formatter("json"), SimpleNamespace(name="gemini"))
    system_text = GeminiProvider._convert_messages(view)[0]
    assert system_text.endswith(RENDERED_TAIL) and system_text.count(RENDERED_TAIL) == 1
    assert "[OUTPUT_GUIDE:CONSTANT]" in system_text
    assert all(RENDERED_TAIL not in message.content for message in rctx.conversation_store.load("branch"))
