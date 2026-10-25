"""把各家零碎的流式片段和字段拼成统一的 ProviderResponse：工具调用与参数、用量、收尾判定。"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Hashable

from .base import ProviderResponse, ToolCall
from .failures import failed

ToolDeltaCallback = Callable[[dict[str, Any]], Awaitable[None]]


def new_call_id() -> str:
    """上游没给 id 时补一个。同一会话里各轮的 id 不能撞，engine 和前端都按它配对结果与审批。"""
    return f"call_{uuid.uuid4().hex[:24]}"


def parse_tool_arguments(raw: Any) -> dict[str, Any]:
    """工具参数统一成 dict。解析不出 JSON 对象时原文放进 {"_raw": 文本}，由 engine 决定怎么处理。"""
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    if not isinstance(raw, str):
        return {"_raw": json.dumps(raw, ensure_ascii=False, default=str)}
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {"_raw": raw}
    return parsed if isinstance(parsed, dict) else {"_raw": raw}


def arguments_json(arguments: Any) -> str:
    """参数写成 JSON 文本；本来就是文本的原样返回。"""
    if isinstance(arguments, str):
        return arguments
    return json.dumps({} if arguments is None else arguments, ensure_ascii=False, default=str)


def int_field(holder: Any, *path: str) -> int:
    """按路径取一个整数，任何一层缺了或类型不对都当 0。"""
    value = holder
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def usage_dict(
    *,
    prompt: int,
    completion: int,
    total: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    reasoning: int = 0,
) -> dict[str, int]:
    """统一的用量形状：prompt 含缓存读写，completion 含推理消耗，可选项只在非零时出现。"""
    usage = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total or prompt + completion,
    }
    if cache_read:
        usage["cache_read_tokens"] = cache_read
    if cache_write:
        usage["cache_write_tokens"] = cache_write
    if reasoning:
        usage["reasoning_tokens"] = reasoning
    return usage


@dataclass
class _PendingCall:
    ordinal: int
    call_id: str
    name: str = ""
    upstream_id: bool = False
    fragments: list[str] = field(default_factory=list)
    # 上游整段给出的参数（Gemini 的 functionCall、Responses 收尾事件里的全文）。
    whole: Any = None
    done: bool = False

    def arguments(self) -> Any:
        return self.whole if self.whole is not None else "".join(self.fragments)

    def looks_complete(self) -> bool:
        if self.whole is not None:
            return True
        text = "".join(self.fragments).strip()
        if not text:
            return False
        try:
            return isinstance(json.loads(text), dict)
        except ValueError:
            return False


class ToolCallAssembler:
    """一次响应里的工具调用。

    按出现顺序编号，这个序号就是发给前端的 index：上游自己的 index 可能缺失、为 null 或者全是 0，
    拿它当 index 会让前端把几个调用的参数拼进同一张卡片。
    """

    def __init__(self, on_tool_delta: ToolDeltaCallback | None = None) -> None:
        self._emit = on_tool_delta
        self._calls: list[_PendingCall] = []
        self._by_key: dict[Hashable, _PendingCall] = {}
        self._by_id: dict[str, _PendingCall] = {}

    async def open(self, key: Hashable, *, call_id: str = "", name: str = "") -> None:
        """有明确开头的调用：Responses 的 output_item.added、Anthropic 的 content_block_start。"""
        call = self._new(call_id, name)
        self._by_key[key] = call
        await self._event("tool_call_start", call)

    async def append(self, key: Hashable, fragment: Any) -> None:
        call = self._by_key.get(key)
        if call is None or not isinstance(fragment, str) or not fragment:
            return
        call.fragments.append(fragment)
        await self._event("tool_call_args_delta", call, delta=fragment)

    def amend(self, key: Hashable, *, call_id: str = "", name: str = "", arguments: Any = None) -> None:
        """收尾事件里补全的字段。整段参数只在一片增量都没收到时才采用，免得同一份参数算两遍。"""
        call = self._by_key.get(key)
        if call is None:
            return
        self._adopt_id(call, call_id)
        if name:
            call.name = name
        if arguments is not None and not call.fragments and call.whole is None:
            call.whole = arguments

    async def close(self, key: Hashable) -> None:
        call = self._by_key.get(key)
        if call is not None:
            await self._finish(call)

    def is_open(self, key: Hashable) -> bool:
        return key in self._by_key

    async def add_complete(self, *, call_id: str = "", name: str, arguments: Any) -> None:
        """整段给出的调用（Gemini 的 functionCall）：开头、参数、结束三个事件一次发完。"""
        call = self._new(call_id, name)
        call.whole = arguments
        await self._event("tool_call_start", call)
        await self._event("tool_call_args_delta", call, delta=arguments_json(arguments))
        await self._finish(call)

    async def feed_chat_delta(self, item: Any) -> None:
        """OpenAI chat/completions 的一项 delta.tool_calls。

        正常的流按 index 归并；可有的中转不带 index、或者每个调用都写 0，只能靠 id 区分。
        所以带着新 id 和名字的片段一律算新调用，没有 id 的片段才按 index 或接在上一个调用后面。
        """
        if not isinstance(item, dict):
            return
        index = item.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            index = None
        call_id = item.get("id") if isinstance(item.get("id"), str) else ""
        function = item.get("function") if isinstance(item.get("function"), dict) else {}
        name = function.get("name") if isinstance(function.get("name"), str) else ""
        fragment = function.get("arguments")
        if fragment is not None and not isinstance(fragment, str):
            fragment = arguments_json(fragment)

        call = self._chat_target(index, call_id, name)
        if call is None:
            call = self._new(call_id, name)
            if index is not None:
                self._by_key[index] = call
            await self._event("tool_call_start", call)
        else:
            self._adopt_id(call, call_id)
            if name:
                call.name = name
        if fragment:
            call.fragments.append(fragment)
            await self._event("tool_call_args_delta", call, delta=fragment)

    async def finish(self) -> None:
        """响应完整收尾时，给还没说过结束的调用补一个 done。"""
        for call in self._calls:
            await self._finish(call)

    def tool_calls(self) -> list[ToolCall]:
        return [
            ToolCall(id=call.call_id, name=call.name.strip(), arguments=parse_tool_arguments(call.arguments()))
            for call in self._calls
            if call.name.strip()
        ]

    def _chat_target(self, index: int | None, call_id: str, name: str) -> _PendingCall | None:
        if call_id and call_id in self._by_id:
            return self._by_id[call_id]
        if call_id and name:
            return None
        if index is not None:
            return self._by_key.get(index)
        if not self._calls:
            return None
        last = self._calls[-1]
        # 既没 id 也没 index：带着名字、而上一个调用已有别的名字或参数已经完整，就是新调用的开头。
        if name and last.name and (name != last.name or last.looks_complete()):
            return None
        return last

    def _new(self, call_id: str, name: str) -> _PendingCall:
        call = _PendingCall(
            ordinal=len(self._calls), call_id=call_id or new_call_id(), name=name or "", upstream_id=bool(call_id),
        )
        self._calls.append(call)
        if call_id:
            self._by_id[call_id] = call
        return call

    def _adopt_id(self, call: _PendingCall, call_id: str) -> None:
        if not call_id or call.upstream_id:
            return
        call.call_id = call_id
        call.upstream_id = True
        self._by_id[call_id] = call

    async def _finish(self, call: _PendingCall) -> None:
        if call.done:
            return
        call.done = True
        await self._event("tool_call_done", call)

    async def _event(self, kind: str, call: _PendingCall, **extra: Any) -> None:
        if self._emit is None:
            return
        await self._emit({"event": kind, "index": call.ordinal, "id": call.call_id, "name": call.name, **extra})


def conclude(
    *,
    terminated: bool,
    finish_reason: str | None,
    status_code: int | None,
    text: str | None = None,
    reasoning: str | None = None,
    tool_calls: list[ToolCall] | None = None,
    usage: dict[str, int] | None = None,
    inline_data: list[dict[str, Any]] | None = None,
    provider_meta: dict[str, Any] | None = None,
    raw: dict[str, Any] | None = None,
    empty_kind: str | None = None,
    detail: str = "",
) -> ProviderResponse:
    """按收尾情况给出最终结果。

    见到上游的收尾才算完整；被输出上限截断但已经有内容的照样交出去，finish_reason="length"
    由 engine 决定怎么用。detail 是上游的原话，失败时用作报错；empty_kind 是什么都没产出时该报的失败种类。
    """
    text = text or None
    reasoning = reasoning or None
    calls = list(tool_calls or [])
    produced = bool(text or calls or inline_data)
    kept = {"text": text, "reasoning": reasoning, "usage": usage, "raw": raw, "provider_meta": provider_meta}
    if not terminated:
        return failed(
            "incomplete", "stream ended before the upstream finished the response",
            finish_reason="incomplete", **kept,
        )
    if finish_reason == "incomplete":
        return failed(
            "incomplete", detail or "upstream ended the response before it was complete",
            status_code=status_code, finish_reason="incomplete", **kept,
        )
    if not produced:
        if finish_reason == "length":
            return failed(
                "incomplete", detail or "output hit the token limit before any content was produced",
                status_code=status_code, finish_reason="length", **kept,
            )
        if finish_reason in ("content_filter", "refusal"):
            return failed(
                "content_filter", detail or "the upstream blocked this response",
                status_code=status_code, finish_reason=finish_reason, **kept,
            )
        if empty_kind:
            return failed(
                empty_kind, detail or "upstream returned no content",
                status_code=status_code, finish_reason=finish_reason, **kept,
            )
    if finish_reason == "stop" and calls:
        finish_reason = "tool_calls"
    return ProviderResponse(
        ok=True,
        text=text,
        tool_calls=calls,
        reasoning=reasoning,
        status_code=status_code,
        raw=raw,
        usage=usage,
        inline_data=list(inline_data or []),
        provider_meta=dict(provider_meta or {}),
        finish_reason=finish_reason,
    )
