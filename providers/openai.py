from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

import httpx

from plugin_sdk import PluginExecutionError

from .assembly import (
    ToolCallAssembler, conclude, int_field, new_call_id, parse_tool_arguments, reported_int, usage_dict,
)
from .base import BaseProvider, ProviderResponse, ToolCall, content_parts, content_text, tool_call_fields
from .degrade import DegradeRule, degrade, set_option, unset_options
from .failures import exception_failure, failed, read_error, upstream_failure
from .options import BOOL, ENUM, FLOAT, INT, TEXT, OptionSet, OptionSpec
from .plugin_transport import iter_sse_events, wrap_client

log = logging.getLogger(__name__)

_MAX_DEGRADE_ROUNDS = 3

# 各家放思维链的字段名不同：DeepSeek 用 reasoning_content，OpenRouter 用 reasoning。
_REASONING_KEYS = ("reasoning_content", "reasoning", "thinking")

# chat/completions 的 finish_reason → 统一取值。中转把 Claude、Gemini 的原生停止原因原样透传的也收进来。
_FINISH_REASONS = {
    "stop": "stop",
    "end_turn": "stop",
    "stop_sequence": "stop",
    "length": "length",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "tool_use": "tool_calls",
    "content_filter": "content_filter",
    "safety": "content_filter",
    "recitation": "content_filter",
    "blocklist": "content_filter",
    "prohibited_content": "content_filter",
    "spii": "content_filter",
    "image_safety": "content_filter",
    "refusal": "refusal",
}
# 上游自己掐断了这一轮：有内容也只是半截，不能当完整结果。
_ABORTED_FINISH = {
    "insufficient_system_resource": "overloaded",
    "error": "unknown",
}


def _cache_hit_tokens(usage: dict[str, Any]) -> int | None:
    """缓存命中量。两处都没报是 None，报了 0 就是 0。"""
    # 两家放的位置不同：OpenAI 嵌在 prompt_tokens_details 里，DeepSeek 平铺在 usage 上。
    reported = [
        value
        for value in (
            reported_int(usage, "prompt_tokens_details", "cached_tokens"),
            reported_int(usage, "prompt_cache_hit_tokens"),
        )
        if value is not None
    ]
    if not reported:
        return None
    return next((value for value in reported if value), 0)


def _extract_usage(data: Any) -> dict[str, int] | None:
    """Extract token usage dict from an OpenAI response/chunk."""
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return None
    if not any(isinstance(usage.get(key), int) for key in ("prompt_tokens", "completion_tokens", "total_tokens")):
        return None
    return usage_dict(
        prompt=int_field(usage, "prompt_tokens"),
        completion=int_field(usage, "completion_tokens"),
        total=int_field(usage, "total_tokens"),
        cache_read=_cache_hit_tokens(usage),
        reasoning=int_field(usage, "completion_tokens_details", "reasoning_tokens"),
    )


def _reasoning_of(holder: dict[str, Any]) -> str:
    for key in _REASONING_KEYS:
        value = holder.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _finish_verdict(raw: Any) -> tuple[str | None, str | None]:
    """(统一的 finish_reason, 被上游中途掐断时的失败种类)。认不出的原因两项都是 None。"""
    key = str(raw or "").strip().lower()
    if key in _ABORTED_FINISH:
        return "incomplete", _ABORTED_FINISH[key]
    return _FINISH_REASONS.get(key), None


def _chat_response(
    *,
    status: int,
    terminated: bool,
    finish_raw: Any,
    text: str,
    reasoning: str,
    refusal: str,
    tool_calls: list[ToolCall],
    usage: dict[str, int] | None,
    raw: dict[str, Any] | None = None,
) -> ProviderResponse:
    """流式和非流式共用的收尾判定。"""
    finish_reason, aborted = _finish_verdict(finish_raw)
    if terminated and aborted:
        return failed(
            aborted, f"upstream aborted the response (finish_reason={finish_raw})",
            status_code=status, finish_reason="incomplete", text=text, reasoning=reasoning, usage=usage, raw=raw,
        )
    if refusal and not text and not tool_calls and finish_reason in (None, "stop"):
        finish_reason = "refusal"
    unknown_finish = finish_raw is not None and finish_reason is None
    return conclude(
        terminated=terminated,
        finish_reason=finish_reason,
        status_code=status,
        text=text,
        reasoning=reasoning,
        tool_calls=tool_calls,
        usage=usage,
        raw=raw,
        empty_kind="unknown" if unknown_finish else None,
        detail=refusal or (f"Upstream error (finish_reason={finish_raw})" if finish_raw else ""),
    )


# 端点反过来是要这个字段的说法（DeepSeek「必须传回」、Kimi「缺了」）：这时关掉回传只会更糟。
_REASONING_WANTED = ("must be passed back", "missing", "required")


def _stop_replaying_reasoning(options: OptionSet, error: str) -> bool:
    """点名 reasoning_content 的 400 多是严格端点不收这个字段，说法各家不一，只排除要它的那几种。"""
    if any(fragment in error for fragment in _REASONING_WANTED):
        return False
    return options.override("replay_reasoning", False)


class OpenAIProvider(BaseProvider):
    """OpenAI-compatible provider implemented with raw HTTP (no SDK).

    This adapter targets the Chat Completions API:
        POST {base_url}/chat/completions

    It supports tool calling via the `tools` field.
    """

    provider_name = "openai"
    wire_format = "openai"
    official_hosts = ("api.openai.com",)
    default_base_url = "https://api.openai.com/v1"

    @classmethod
    def catalog_request(cls, *, base_url: str, api_key: str):
        return (
            f"{cls.resolve_base_url(base_url)}/models",
            {"Authorization": f"Bearer {api_key}"},
        )

    @staticmethod
    def parse_catalog(payload):
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        return [
            str(item.get("id") or "").strip()
            for item in items
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        ]

    @staticmethod
    def _prepare_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把 engine 交来的消息收成 API 能收的最终形状。

        剥掉残留的 _ 开头内部字段；块数组里的裸字符串折成 text 块，对面不收裸字符串。
        最后一条是 assistant 时追加一条 user，部分兼容端点（尤其经它转发的 Anthropic 系）
        会拒绝以 assistant 结尾的消息列表。
        """
        result: list[dict[str, Any]] = []
        for msg in messages:
            clean = {k: v for k, v in msg.items() if not k.startswith('_')}
            if isinstance(clean.get("content"), list):
                clean["content"] = content_parts(clean["content"])
            result.append(clean)
        if result and result[-1].get('role') == 'assistant':
            result.append({'role': 'user', 'content': '请继续。'})
        return result

    OPTIONS: tuple[OptionSpec, ...] = (
        OptionSpec(
            key="max_completion_tokens", label="回复上限", kind=INT, default=None,
            minimum=16, maximum=200000, desc="留空则由模型默认值决定。",
        ),
        OptionSpec(
            key="token_limit_field", label="上限字段名", kind=ENUM, default="max_completion_tokens",
            choices=(("max_completion_tokens", "新写法"), ("max_tokens", "旧写法")),
            desc="官方已把 max_tokens 换成 max_completion_tokens，但不少兼容端点只认旧的。"
                 "被拒一次后会自动切到旧写法。",
        ),
        OptionSpec(
            key="reasoning_effort", label="思考档位", kind=ENUM, default="default",
            choices=(
                ("default", "不指定"),
                ("none", "不思考"),
                ("minimal", "最少"),
                ("low", "低"),
                ("medium", "中"),
                ("high", "高"),
                ("xhigh", "很高"),
            ),
            desc="仅推理系模型支持。注意「不思考」在新版模型上会关掉工具调用。",
        ),
        OptionSpec(
            key="replay_reasoning", label="回传思维链", kind=BOOL, default=True,
            desc="把历史里模型的思维链（reasoning_content）随请求带回去，DeepSeek、Kimi 这类思考模型调工具时要用。"
                 "个别严格的兼容端点不收这个字段，被拒一次后会自动关掉。",
        ),
        OptionSpec(
            key="verbosity", label="回复详尽度", kind=ENUM, default="default",
            choices=(("default", "不指定"), ("low", "简"), ("medium", "中"), ("high", "详")),
        ),
        OptionSpec(
            key="temperature", label="随机度", kind=FLOAT, default=None,
            minimum=0.0, maximum=2.0, desc="留空则不发。推理系模型不接受这个参数。",
        ),
        OptionSpec(key="top_p", label="核采样", kind=FLOAT, default=None, minimum=0.0, maximum=1.0),
        OptionSpec(
            key="tool_choice", label="工具选择", kind=ENUM, default="auto",
            choices=(("auto", "自行决定"), ("required", "必须调用"), ("none", "禁止调用")),
        ),
        OptionSpec(
            key="prompt_cache_key", label="缓存分组", kind=TEXT, default="",
            desc="同一个值的请求会被归到一组做前缀缓存。留空则由对面按内容自动判断。",
        ),
        OptionSpec(
            key="timeout_sec", label="超时", kind=FLOAT, default=600.0,
            minimum=10.0, maximum=1800.0,
        ),
    )

    DEGRADE_RULES: tuple[DegradeRule, ...] = (
        DegradeRule(
            name="改用旧的上限字段",
            match=("max_completion_tokens",),
            apply=set_option("token_limit_field", "max_tokens"),
            advice="这个端点只认 max_tokens。",
        ),
        DegradeRule(
            name="去掉思考档位",
            match=("reasoning_effort", "reasoning.effort"),
            apply=unset_options("reasoning_effort"),
            advice="这个模型不支持思考档位。",
        ),
        DegradeRule(
            name="去掉详尽度",
            match=("verbosity",),
            apply=unset_options("verbosity"),
            advice="这个模型不支持详尽度。",
        ),
        DegradeRule(
            name="去掉采样参数",
            match=("temperature", "top_p"),
            apply=unset_options("temperature", "top_p"),
            advice="推理系模型不接受采样参数，请把随机度和核采样留空。",
        ),
        DegradeRule(
            name="不回传思维链",
            match=("reasoning_content",),
            apply=_stop_replaying_reasoning,
            advice="这个端点不收历史消息里的 reasoning_content。",
        ),
    )

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        api_key: str,
        base_url: str | None,
        model: str,
        provider_options: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(model=model, name=self.provider_name)

        k = (api_key or "").strip()
        if not k:
            raise RuntimeError("openai api_key is empty")

        self._http = wrap_client(http, self)
        self._api_key = k
        self._base_url = self.resolve_base_url(base_url)
        self._options = OptionSet(self.OPTIONS, provider_options)
        self._timeout = float(self._options.get("timeout_sec"))
        unknown = self._options.unknown_keys()
        if unknown:
            log.warning("%s 不认识这些 provider_options，已忽略：%s", self.name, ", ".join(unknown))

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _degrade(self, error: str) -> bool:
        return degrade(
            self.DEGRADE_RULES, self._options, error, provider=self.name, model=self.model,
        ) is not None

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        stream: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._prepare_messages(messages),
        }
        # 只在明确关掉时剥：不提供这一项的子类（DeepSeek）get 到的是 None，思维链照旧回传
        if self._options.get("replay_reasoning") is False:
            payload["messages"] = [
                {key: value for key, value in message.items() if key != "reasoning_content"}
                for message in payload["messages"]
            ]
        if stream:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = self._options.get("tool_choice")
        if self._options.is_set("max_completion_tokens"):
            payload[str(self._options.get("token_limit_field"))] = int(
                self._options.get("max_completion_tokens")
            )
        for key in ("temperature", "top_p"):
            if self._options.is_set(key):
                payload[key] = self._options.get(key)
        for key in ("reasoning_effort", "verbosity"):
            # 子类不支持的项 get 返回 None，别把 null 发出去。
            value = self._options.get(key)
            if value and value != "default":
                payload[key] = value
        cache_key = str(self._options.get("prompt_cache_key") or "")
        if cache_key:
            payload["prompt_cache_key"] = cache_key
        return payload

    async def chat_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text: Callable[[str], Awaitable[None]] | None = None,
        on_thinking: Callable[[str], Awaitable[None]] | None = None,
        on_tool_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        _attempt: int = 0,
    ) -> ProviderResponse:
        """流式聊天补全。逐块回调 on_text / on_thinking / on_tool_delta，最终返回组装好的 ProviderResponse。"""

        url = f"{self._base_url}/chat/completions"
        payload = self._build_payload(messages, tools, stream=True)

        try:
            async with self._http.stream(
                "POST", url, headers=self._headers(), json=payload, timeout=self._timeout,
            ) as resp:
                status = int(resp.status_code)
                if status >= 400:
                    error = read_error(await resp.aread(), fallback=f"HTTP {status}")
                    if status == 400 and _attempt < _MAX_DEGRADE_ROUNDS and self._degrade(error.message):
                        # 状态码先于任何 SSE 分片到达，重来不会让调用方看到半截内容。
                        return await self.chat_stream(
                            messages=messages, tools=tools, on_text=on_text,
                            on_thinking=on_thinking, on_tool_delta=on_tool_delta,
                            _attempt=_attempt + 1,
                        )
                    return upstream_failure(status, error)
                return await self._read_stream(
                    resp, status, on_text=on_text, on_thinking=on_thinking, on_tool_delta=on_tool_delta,
                )
        except PluginExecutionError:
            raise
        except Exception as exc:
            return exception_failure(exc)

    async def _read_stream(
        self,
        resp: httpx.Response,
        status: int,
        *,
        on_text: Callable[[str], Awaitable[None]] | None,
        on_thinking: Callable[[str], Awaitable[None]] | None,
        on_tool_delta: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> ProviderResponse:
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        refusal_parts: list[str] = []
        calls = ToolCallAssembler(on_tool_delta)
        usage: dict[str, int] | None = None
        finish_raw: Any = None
        # 见到 [DONE] 或 finish_reason 才算收尾；连接自己断了的流走不到这两步。
        terminated = False
        try:
            async for event in iter_sse_events(resp):
                data = event["data"]
                if data.strip() == "[DONE]":
                    terminated = True
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("error"):
                    # 中转常在 200 的流里塞一个 error 对象，比如转发 Gemini 时的内容拦截。
                    return upstream_failure(
                        status, read_error(chunk),
                        text="".join(text_parts), reasoning="".join(reasoning_parts), usage=usage,
                    )
                if isinstance(chunk.get("usage"), dict):
                    usage = _extract_usage(chunk) or usage
                choices = chunk.get("choices")
                choice = choices[0] if isinstance(choices, list) and choices else None
                if not isinstance(choice, dict):
                    continue
                if choice.get("finish_reason"):
                    finish_raw = choice["finish_reason"]
                    terminated = True
                delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}

                content = delta.get("content")
                if isinstance(content, str) and content:
                    text_parts.append(content)
                    if on_text:
                        await on_text(content)

                reasoning = _reasoning_of(delta)
                if reasoning:
                    reasoning_parts.append(reasoning)
                    if on_thinking:
                        await on_thinking(reasoning)

                refusal = delta.get("refusal")
                if isinstance(refusal, str) and refusal:
                    refusal_parts.append(refusal)

                items = delta.get("tool_calls")
                if isinstance(items, list):
                    for item in items:
                        await calls.feed_chat_delta(item)
            if terminated:
                await calls.finish()
        except PluginExecutionError:
            raise
        except Exception as exc:
            return exception_failure(
                exc, mid_stream=True,
                text="".join(text_parts), reasoning="".join(reasoning_parts), usage=usage,
            )

        return _chat_response(
            status=status,
            terminated=terminated,
            finish_raw=finish_raw,
            text="".join(text_parts),
            reasoning="".join(reasoning_parts),
            refusal="".join(refusal_parts),
            tool_calls=calls.tool_calls(),
            usage=usage,
        )

    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        _attempt: int = 0,
    ) -> ProviderResponse:
        url = f"{self._base_url}/chat/completions"
        payload = self._build_payload(messages, tools, stream=False)

        try:
            r = await self._http.post(url, headers=self._headers(), json=payload, timeout=self._timeout)
        except PluginExecutionError:
            raise
        except Exception as exc:
            return exception_failure(exc)

        status = int(r.status_code)
        try:
            data: Any = r.json()
        except Exception:
            data = None

        if status >= 400:
            error = read_error(data if data is not None else r.content, fallback=f"HTTP {status}")
            if status == 400 and _attempt < _MAX_DEGRADE_ROUNDS and self._degrade(error.message):
                return await self.chat(messages=messages, tools=tools, _attempt=_attempt + 1)
            return upstream_failure(status, error, raw=data if isinstance(data, dict) else None)

        if not isinstance(data, dict):
            return failed("invalid_response", "invalid JSON response", status_code=status)
        if data.get("error"):
            return upstream_failure(status, read_error(data), raw=data)

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return failed("invalid_response", "missing choices", status_code=status, raw=data)
        choice0 = choices[0]
        if not isinstance(choice0, dict):
            return failed("invalid_response", "invalid choices[0]", status_code=status, raw=data)
        msg = choice0.get("message")
        if not isinstance(msg, dict):
            return failed("invalid_response", "missing message", status_code=status, raw=data)

        content = msg.get("content")
        # 有些中转把 content 也拆成了块数组。
        text = content if isinstance(content, str) else content_text(content)
        refusal = msg.get("refusal")

        tool_calls: list[ToolCall] = []
        raw_calls = msg.get("tool_calls")
        for raw_call in raw_calls if isinstance(raw_calls, list) else []:
            if isinstance(raw_call, dict) and raw_call.get("type") not in (None, "function"):
                continue
            fields = tool_call_fields(raw_call)
            if fields is None:
                continue
            call_id, name, arguments = fields
            tool_calls.append(ToolCall(id=call_id or new_call_id(), name=name, arguments=parse_tool_arguments(arguments)))

        return _chat_response(
            status=status,
            terminated=True,
            finish_raw=choice0.get("finish_reason"),
            text=text,
            reasoning=_reasoning_of(msg),
            refusal=refusal if isinstance(refusal, str) else "",
            tool_calls=tool_calls,
            usage=_extract_usage(data),
            raw=data,
        )
