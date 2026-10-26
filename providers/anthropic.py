"""Anthropic native API provider for Clonoth.

Converts Clonoth's internal OpenAI-style messages to the Messages API format and
back, covering streaming, tool use and thinking blocks.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Awaitable

import httpx

from plugin_sdk import PluginExecutionError
from tools._wire import WIRE_ANTHROPIC, headers as wire_headers

from .assembly import (
    ToolCallAssembler, conclude, int_field, new_call_id, parse_tool_arguments, reported_int, usage_dict,
)
from .base import (
    BaseProvider, ProviderResponse, ToolCall, content_parts, content_text, image_part_url, provider_metadata,
    split_data_url, tool_call_fields,
)
from .degrade import DegradeRule, degrade, unset_options
from .failures import exception_failure, failed, read_error, upstream_failure
from .options import BOOL, ENUM, FLOAT, INT, OptionSet, OptionSpec
from .plugin_transport import iter_sse_events, wrap_client

log = logging.getLogger(__name__)

# 单次请求最多降级几轮。踩完思考模式再踩回复上限是真实存在的组合，留够余量。
_MAX_DEGRADE_ROUNDS = 3
# 实际报错长这样：`max_tokens: 64000 > 32000, which is the maximum allowed number of
# output tokens for <model>` —— 上限在 `>` 后面，不在 "maximum" 后面。
_MAX_TOKENS_CEILING = (
    re.compile(r">\s*(\d{2,7})"),
    re.compile(r"maximum[^\d]{0,60}?(\d{2,7})"),
)

# stop_reason → 统一的 finish_reason。pause_turn 是服务端工具没跑完先交回来的半截，不算完整。
_STOP_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "tool_use": "tool_calls",
    "refusal": "refusal",
    "pause_turn": "incomplete",
}


def _anthropic_response(
    *,
    status: int,
    terminated: bool,
    stop_reason: Any,
    text: str,
    reasoning: str,
    tool_calls: list[ToolCall],
    usage: dict[str, int] | None,
    thinking_blocks: list[dict[str, Any]],
    raw: dict[str, Any] | None = None,
) -> ProviderResponse:
    """流式和非流式共用的收尾判定。含签名的 thinking blocks 进 provider_meta，下一轮原样回传。"""
    finish_reason = _STOP_REASONS.get(str(stop_reason or "").strip().lower())
    return conclude(
        terminated=terminated,
        finish_reason=finish_reason,
        status_code=status,
        text=text,
        reasoning=reasoning,
        tool_calls=tool_calls,
        usage=usage,
        raw=raw,
        provider_meta={"thinking_blocks": thinking_blocks} if thinking_blocks else None,
        empty_kind="unknown" if stop_reason and finish_reason is None else None,
        detail=f"upstream stopped (stop_reason={stop_reason})" if stop_reason else "",
    )


def _lower_max_tokens(options: OptionSet, error: str) -> bool:
    """按错误里报的上限压 max_tokens。取不到数字就折半，总能收敛。"""
    current = options.get("max_tokens")
    if not isinstance(current, int) or current <= 1024:
        return False
    ceiling = 0
    for pattern in _MAX_TOKENS_CEILING:
        found = pattern.search(error)
        if found and 0 < int(found.group(1)) < current:
            ceiling = int(found.group(1))
            break
    # 一个数字都读不出来就折半，多撞几次也能收敛到能过的值。
    return options.override("max_tokens", max(ceiling or current // 2, 1024))


def _thinking_to_adaptive(options: OptionSet, _error: str) -> bool:
    if options.get("thinking_mode") == "adaptive":
        return False
    return options.override("thinking_mode", "adaptive")


def _thinking_to_budget(options: OptionSet, _error: str) -> bool:
    if options.get("thinking_mode") == "budget":
        return False
    return options.override("thinking_mode", "budget")


def _convert_image_part(part: dict) -> dict:
    """Convert an OpenAI vision image_url part to Anthropic image source format.

    OpenAI format:
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,ABC..."}}
    Anthropic format:
      {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "ABC..."}}
    """
    url = image_part_url(part)
    parsed = split_data_url(url)
    if parsed:
        media_type, data = parsed
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": data,
            },
        }
    # If it's a plain URL (not data URI), use url source type
    if url:
        return {
            "type": "image",
            "source": {
                "type": "url",
                "url": url,
            },
        }
    # Fallback: return as text describing the issue
    return {"type": "text", "text": "[image could not be converted]"}


def _content_to_blocks(content: Any) -> list[dict]:
    """Convert OpenAI message content (string or list) to Anthropic content blocks."""
    blocks = []
    for part in content_parts(content):
        ptype = part.get("type", "")
        if ptype == "text":
            if part.get("text"):
                blocks.append({"type": "text", "text": str(part["text"])})
        elif ptype == "image_url":
            blocks.append(_convert_image_part(part))
        else:
            # Unknown part type — pass text representation
            blocks.append({"type": "text", "text": str(part)})
    return blocks


def _sanitize_input_schema(schema: Any) -> Any:
    """递归清洗 JSON Schema，使其符合 Anthropic 要求的 draft 2020-12。

    MCP 工具的 input_schema 常带 draft-07 的 `$schema` 声明，Anthropic 见到旧 draft 直接回 400
    (tools.N.custom.input_schema invalid)：剥掉 `$schema`，`definitions` 改名为 `$defs`。
    """
    if isinstance(schema, dict):
        cleaned: dict[str, Any] = {}
        for k, v in schema.items():
            # 剥除显式 draft 声明（draft-07 等与 2020-12 不兼容）
            if k == "$schema":
                continue
            # draft-07 的 definitions → 2020-12 的 $defs
            if k == "definitions":
                cleaned["$defs"] = _sanitize_input_schema(v)
                continue
            cleaned[k] = _sanitize_input_schema(v)
        return cleaned
    if isinstance(schema, list):
        return [_sanitize_input_schema(item) for item in schema]
    return schema


def _convert_tools(tools: list[dict] | None) -> list[dict]:
    """Convert OpenAI function-calling tool definitions to Anthropic tool format.

    OpenAI: {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
    Anthropic: {"name": ..., "description": ..., "input_schema": ...}
    """
    if not tools:
        return []
    result = []
    for t in tools:
        fn = t.get("function", {})
        raw_schema = fn.get("parameters", {"type": "object", "properties": {}})
        converted = {
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": _sanitize_input_schema(raw_schema),
        }
        result.append(converted)
    return result


def _convert_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """Convert OpenAI-format messages to Anthropic format.

    Returns (system_text, converted_messages).

    Key transformations:
    1. system messages -> extracted into a single system string
    2. tool result messages (role=tool) -> grouped into user messages with tool_result blocks
    3. assistant messages with tool_calls -> content blocks with tool_use entries
    4. Consecutive same-role messages are merged (Anthropic requires strict alternation)
    """
    system_parts: list[str] = []
    converted: list[dict] = []

    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role", "")
        content = msg.get("content")

        if role == "system":
            text = content_text(content)
            if text:
                system_parts.append(text)
            i += 1
            continue

        if role == "tool":
            # Collect consecutive tool result messages into one user message
            # Anthropic represents tool results as user messages with tool_result content blocks
            tool_results: list[dict] = []
            while i < len(messages) and messages[i].get("role") == "tool":
                tm = messages[i]
                tool_result_content = tm.get("content", "")
                # tool result content can be string or structured
                if isinstance(tool_result_content, str):
                    tr_content = tool_result_content
                else:
                    tr_content = json.dumps(tool_result_content, ensure_ascii=False)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tm.get("tool_call_id", ""),
                    "content": tr_content,
                })
                i += 1
            converted.append({"role": "user", "content": tool_results})
            continue

        if role == "assistant":
            blocks: list[dict] = []

            # extended thinking 要求上一轮的 thinking / redacted_thinking 块连同签名原样放回
            # assistant 内容的最前面。
            saved_blocks = provider_metadata(msg, "anthropic").get("thinking_blocks") or []
            for tb in saved_blocks if isinstance(saved_blocks, list) else []:
                if not isinstance(tb, dict):
                    continue
                tb_type = tb.get("type", "")
                if tb_type == "thinking":
                    blocks.append({
                        "type": "thinking",
                        "thinking": tb.get("thinking", ""),
                        "signature": tb.get("signature", ""),
                    })
                elif tb_type == "redacted_thinking":
                    blocks.append({
                        "type": "redacted_thinking",
                        "data": tb.get("data", ""),
                    })

            if content:
                blocks.extend(_content_to_blocks(content))
            # chat/completions 形状和本项目存储形状的 tool_calls 都认。
            for tc in msg.get("tool_calls") or []:
                fields = tool_call_fields(tc)
                if fields is None:
                    continue
                call_id, name, arguments = fields
                blocks.append({
                    "type": "tool_use",
                    "id": call_id,
                    "name": name,
                    "input": parse_tool_arguments(arguments),
                })
            # Anthropic requires non-empty content; if empty, add a placeholder
            if not blocks:
                blocks = [{"type": "text", "text": "(empty)"}]
            converted.append({"role": "assistant", "content": blocks})
            i += 1
            continue

        if role == "user":
            blocks = _content_to_blocks(content)
            if not blocks:
                blocks = [{"type": "text", "text": "(empty)"}]
            converted.append({"role": "user", "content": blocks})
            i += 1
            continue

        # Unknown role — treat as user
        log.warning("Unknown message role %r, treating as user", role)
        blocks = _content_to_blocks(content) or [{"type": "text", "text": str(content)}]
        converted.append({"role": "user", "content": blocks})
        i += 1

    # Merge consecutive same-role messages (Anthropic requires strict user/assistant alternation)
    # IMPORTANT: tool_result blocks and text blocks MUST NOT be mixed in the
    # same user message — Anthropic rejects any user message containing both.
    # When a user-role tool_result group is followed by a user-role text message
    # (or vice versa), we insert a synthetic assistant separator instead of merging.
    merged: list[dict] = []
    for msg in converted:
        if merged and merged[-1]["role"] == msg["role"]:
            prev_content = merged[-1]["content"]
            curr_content = msg["content"]
            prev_blocks = prev_content if isinstance(prev_content, list) else _content_to_blocks(prev_content)
            curr_blocks = curr_content if isinstance(curr_content, list) else _content_to_blocks(curr_content)
            prev_has_tr = any(b.get("type") == "tool_result" for b in prev_blocks)
            curr_has_tr = any(b.get("type") == "tool_result" for b in curr_blocks)
            # Both have tool_result, or neither has — safe to merge
            if prev_has_tr == curr_has_tr:
                if isinstance(prev_content, list) and isinstance(curr_content, list):
                    prev_content.extend(curr_content)
                elif isinstance(prev_content, list):
                    prev_content.extend(_content_to_blocks(curr_content))
                else:
                    merged[-1]["content"] = _content_to_blocks(prev_content) + _content_to_blocks(curr_content)
            else:
                # Mixing tool_result with text — insert assistant separator
                merged.append({"role": "assistant", "content": [{"type": "text", "text": "(continued)"}]})
                merged.append(msg)
        else:
            merged.append(msg)

    # Anthropic requires the first message to be from 'user'.
    # If the first message is 'assistant', prepend a placeholder user message.
    if merged and merged[0]["role"] == "assistant":
        merged.insert(0, {"role": "user", "content": [{"type": "text", "text": "(start)"}]})

    system_text = "\n\n".join(system_parts)
    return system_text, merged


def _parse_response_content(content_blocks: list[dict]) -> tuple[str, str, list[ToolCall], list[dict]]:
    """Parse Anthropic response content blocks into (text, reasoning, tool_calls, thinking_blocks).

    Content block types:
    - {"type": "thinking", "thinking": "...", "signature": "..."} -> reasoning + thinking_blocks
    - {"type": "redacted_thinking", "data": "..."} -> thinking_blocks (opaque, must round-trip)
    - {"type": "text", "text": "..."} -> text
    - {"type": "tool_use", "id": "...", "name": "...", "input": {...}} -> tool_calls

    thinking_blocks 是含签名的原始块，多轮对话时要原样回传，extended thinking 才验得过签名。
    """
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    thinking_blocks: list[dict] = []

    for block in content_blocks if isinstance(content_blocks, list) else []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")
        if btype == "thinking":
            thinking_text = block.get("thinking", "")
            if thinking_text:
                reasoning_parts.append(thinking_text)
            thinking_blocks.append(block)
        elif btype == "redacted_thinking":
            thinking_blocks.append(block)
        elif btype == "text":
            t = block.get("text", "")
            if t:
                text_parts.append(t)
        elif btype == "tool_use":
            name = str(block.get("name") or "").strip()
            if name:
                tool_calls.append(ToolCall(
                    id=str(block.get("id") or "") or new_call_id(),
                    name=name,
                    arguments=parse_tool_arguments(block.get("input")),
                ))

    return "\n".join(text_parts), "\n".join(reasoning_parts), tool_calls, thinking_blocks


def _parse_usage(data: Any) -> dict[str, int] | None:
    """Convert Anthropic usage to OpenAI-style usage dict. 没报用量时为 None。"""
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict) or not usage:
        return None
    # input_tokens 只是未命中缓存的那部分，缓存读写要单独加回来，否则开了缓存
    # 之后 prompt_tokens 会突然掉一个数量级，看着像省钱其实是漏算。
    cache_read = reported_int(usage, "cache_read_input_tokens")
    cache_write = reported_int(usage, "cache_creation_input_tokens")
    return usage_dict(
        prompt=int_field(usage, "input_tokens") + (cache_read or 0) + (cache_write or 0),
        completion=int_field(usage, "output_tokens"),
        cache_read=cache_read,
        cache_write=cache_write,
        reasoning=int_field(usage, "output_tokens_details", "thinking_tokens"),
    )


class AnthropicProvider(BaseProvider):
    """Native Anthropic Messages API provider.

    Accepts OpenAI-format messages from the engine and converts them to
    Anthropic format internally. Supports non-streaming (chat) and
    streaming (chat_stream) modes, tool use, and thinking/reasoning blocks.
    """

    provider_name = "anthropic"
    official_hosts = ("api.anthropic.com",)
    default_base_url = "https://api.anthropic.com"

    @classmethod
    def catalog_request(cls, *, base_url: str, api_key: str):
        base = cls.resolve_base_url(base_url)
        # 鉴权头和 _headers() 同一份判定，否则拉得到列表却聊不了天。
        return f"{base}/v1/models", wire_headers(WIRE_ANTHROPIC, base, api_key)

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

    OPTIONS: tuple[OptionSpec, ...] = (
        OptionSpec(
            key="thinking_mode", label="思考模式", kind=ENUM, default="auto",
            choices=(
                ("auto", "跟随模型默认"),
                ("adaptive", "自适应"),
                ("budget", "固定预算"),
                ("off", "关闭"),
            ),
            desc="自适应与固定预算按模型二选一：4.7 及以后只认自适应，4.5 及以前只认固定预算。"
                 "拿不准就留「跟随模型默认」——那样根本不发这个字段，由对面决定。",
        ),
        OptionSpec(
            key="thinking_budget_tokens", label="思考预算", kind=INT, default=10000,
            minimum=1024, maximum=128000, depends_on=("thinking_mode", "budget"),
            desc="固定预算模式下的思考上限，必须小于回复上限。",
        ),
        OptionSpec(
            key="effort", label="思考档位", kind=ENUM, default="default",
            choices=(
                ("default", "不指定"),
                ("low", "低"),
                ("medium", "中"),
                ("high", "高"),
                ("xhigh", "很高"),
                ("max", "最高"),
            ),
            desc="自适应模式下用它控制思考深度，取代了固定预算的角色。",
        ),
        OptionSpec(
            key="max_tokens", label="回复上限", kind=INT, default=64000,
            minimum=1024, maximum=200000,
            desc="必填项，各模型上限不同。设太高会被直接拒，被拒后会自动按对面报的上限压一档。",
        ),
        OptionSpec(
            key="temperature", label="随机度", kind=FLOAT, default=None,
            minimum=0.0, maximum=1.0,
            desc="留空则整个字段都不发。新版 Claude 自适应采样，请求里只要出现这个键就会被拒。",
        ),
        OptionSpec(
            key="top_p", label="核采样", kind=FLOAT, default=None,
            minimum=0.0, maximum=1.0,
            desc="留空则不发。与随机度不能同时给。",
        ),
        OptionSpec(
            key="cache", label="提示缓存", kind=ENUM, default="off",
            choices=(("off", "关"), ("5m", "5 分钟"), ("1h", "1 小时")),
            desc="开启后自动缓存请求里最后一个可缓存块。命中按一折计费，写入要加价两成半，"
                 "所以只有前缀稳定复用时才划算；1 小时档写入翻倍，一小时内要读满三次才回本。",
        ),
        OptionSpec(
            key="service_tier", label="服务档", kind=ENUM, default="default",
            choices=(("default", "不指定"), ("auto", "优先级优先"), ("standard_only", "仅标准")),
        ),
        OptionSpec(
            key="interleaved_thinking", label="工具间思考", kind=BOOL, default=False,
            desc="让模型在两次工具调用之间继续思考。仅 4.5 及以前的固定预算模式需要它，"
                 "自适应模式本来就会这么做，开了也是白开。",
        ),
        OptionSpec(
            key="timeout_sec", label="超时", kind=FLOAT, default=300.0,
            minimum=10.0, maximum=1800.0,
        ),
    )

    DEGRADE_RULES: tuple[DegradeRule, ...] = (
        DegradeRule(
            name="改用自适应思考",
            match=('"thinking.type.enabled"', "thinking.type.enabled", "thinking.type.disabled"),
            apply=_thinking_to_adaptive,
            advice="这个模型不认固定预算，把思考模式改成「自适应」。",
        ),
        DegradeRule(
            name="改用固定预算思考",
            match=('"thinking.type.adaptive"', "thinking.type.adaptive"),
            apply=_thinking_to_budget,
            advice="这个模型还不支持自适应，把思考模式改成「固定预算」。",
        ),
        DegradeRule(
            name="去掉核采样",
            match=("cannot both be specified",),
            apply=unset_options("top_p"),
            advice="随机度和核采样只能给一个。",
        ),
        DegradeRule(
            name="去掉采样参数",
            match=("temperature", "top_p", "top_k"),
            apply=unset_options("temperature", "top_p"),
            advice="这个模型自适应采样，采样参数一个都不能给，请把随机度和核采样都留空。",
        ),
        DegradeRule(
            name="下调回复上限",
            match=("max_tokens",),
            apply=_lower_max_tokens,
            advice="回复上限超过了这个模型允许的值。",
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
    ):
        super().__init__(model=model, name=self.provider_name)
        self._http = wrap_client(http, self)
        self._api_key = api_key
        self._base_url = self.resolve_base_url(base_url)
        self._options = OptionSet(self.OPTIONS, provider_options)
        self._timeout = float(self._options.get("timeout_sec"))
        unknown = self._options.unknown_keys()
        if unknown:
            log.warning("anthropic 不认识这些 provider_options，已忽略：%s", ", ".join(unknown))

    def _headers(self) -> dict[str, str]:
        """Official Anthropic API uses x-api-key; reverse proxies use Bearer token."""
        h = wire_headers(WIRE_ANTHROPIC, self._base_url, self._api_key)
        if self._options.get("interleaved_thinking"):
            h["anthropic-beta"] = "interleaved-thinking-2025-05-14"
        return h

    @property
    def _endpoint(self) -> str:
        return f"{self._base_url}/v1/messages"

    def _thinking_block(self, max_tokens: int) -> dict[str, Any] | None:
        mode = self._options.get("thinking_mode")
        if mode == "auto":
            return None
        if mode == "off":
            return {"type": "disabled"}
        if mode == "adaptive":
            return {"type": "adaptive"}
        budget = int(self._options.get("thinking_budget_tokens"))
        return {"type": "enabled", "budget_tokens": max(min(budget, max_tokens - 1), 1024)}

    def _build_payload(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict:
        """Build the Anthropic API request payload from OpenAI-format inputs."""
        system_text, converted = _convert_messages(messages)
        converted_tools = _convert_tools(tools)
        max_tokens = int(self._options.get("max_tokens"))

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": converted,
            "max_tokens": max_tokens,
        }
        if system_text:
            payload["system"] = system_text
        if converted_tools:
            payload["tools"] = converted_tools
        # 采样参数按「配了才发」处理：新版模型只要键在就拒，发一个默认值等于自杀。
        for key in ("temperature", "top_p"):
            if self._options.is_set(key):
                payload[key] = self._options.get(key)
        if stream:
            payload["stream"] = True
        thinking = self._thinking_block(max_tokens)
        if thinking is not None:
            payload["thinking"] = thinking
        effort = self._options.get("effort")
        if effort != "default":
            payload["output_config"] = {"effort": effort}
        cache = self._options.get("cache")
        if cache != "off":
            # 顶层这一个字段就够：由服务端给最后一个可缓存块打点，不用自己逐块摆断点。
            payload["cache_control"] = {"type": "ephemeral", "ttl": cache}
        tier = self._options.get("service_tier")
        if tier != "default":
            payload["service_tier"] = tier
        return payload

    def _degrade(self, error: str) -> bool:
        return degrade(
            self.DEGRADE_RULES, self._options, error, provider="anthropic", model=self.model,
        ) is not None

    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> ProviderResponse:
        """Non-streaming request to Anthropic Messages API."""
        for attempt in range(_MAX_DEGRADE_ROUNDS + 1):
            payload = self._build_payload(messages, tools=tools, stream=False)
            try:
                resp = await self._http.post(
                    self._endpoint,
                    headers=self._headers(),
                    json=payload,
                    timeout=self._timeout,
                )
            except PluginExecutionError:
                raise
            except Exception as exc:
                return exception_failure(exc)

            if resp.status_code < 400:
                break
            error = read_error(resp.content, fallback=f"HTTP {resp.status_code}")
            if resp.status_code == 400 and attempt < _MAX_DEGRADE_ROUNDS and self._degrade(error.message):
                continue
            return upstream_failure(resp.status_code, error)

        try:
            data = resp.json()
        except Exception:
            data = None
        if not isinstance(data, dict):
            return failed("invalid_response", "invalid JSON response", status_code=resp.status_code)
        if data.get("type") == "error" or data.get("error"):
            return upstream_failure(resp.status_code, read_error(data), raw=data)

        text, reasoning, tool_calls, thinking_blocks = _parse_response_content(data.get("content"))
        return _anthropic_response(
            status=resp.status_code,
            terminated=True,
            stop_reason=data.get("stop_reason"),
            text=text,
            reasoning=reasoning,
            tool_calls=tool_calls,
            usage=_parse_usage(data),
            thinking_blocks=thinking_blocks,
            raw=data,
        )

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
        """Streaming request to Anthropic Messages API via SSE.

        Anthropic SSE event types:
        - message_start: message metadata
        - content_block_start: new content block (text / thinking / tool_use)
        - content_block_delta: incremental content (text_delta / thinking_delta / input_json_delta)
        - content_block_stop: block finished
        - message_delta: stop_reason and final usage
        - message_stop: end of message
        """
        payload = self._build_payload(messages, tools=tools, stream=True)
        try:
            async with self._http.stream(
                "POST",
                self._endpoint,
                headers=self._headers(),
                json=payload,
                timeout=self._timeout,
            ) as resp:
                if resp.status_code >= 400:
                    error = read_error(await resp.aread(), fallback=f"HTTP {resp.status_code}")
                    if (
                        resp.status_code == 400
                        and _attempt < _MAX_DEGRADE_ROUNDS
                        and self._degrade(error.message)
                    ):
                        # 状态码先于任何 SSE 事件到达，此时一个 on_text 都还没回调过，
                        # 重来一遍不会让调用方看到半截内容。
                        return await self.chat_stream(
                            messages=messages, tools=tools, on_text=on_text,
                            on_thinking=on_thinking, on_tool_delta=on_tool_delta,
                            _attempt=_attempt + 1,
                        )
                    return upstream_failure(resp.status_code, error)
                return await self._read_stream(
                    resp, on_text=on_text, on_thinking=on_thinking, on_tool_delta=on_tool_delta,
                )
        except PluginExecutionError:
            raise
        except Exception as exc:
            return exception_failure(exc)

    async def _read_stream(
        self,
        resp: httpx.Response,
        *,
        on_text: Callable[[str], Awaitable[None]] | None,
        on_thinking: Callable[[str], Awaitable[None]] | None,
        on_tool_delta: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> ProviderResponse:
        status = int(resp.status_code)
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls = ToolCallAssembler(on_tool_delta)
        output_usage: dict[str, Any] = {}
        input_usage: dict[str, Any] = {}
        # 正在接收的 thinking block 按 index 记着，content_block_stop 时连同签名组装成完整块。
        active_thinking: dict[Any, dict[str, Any]] = {}
        thinking_blocks: list[dict[str, Any]] = []
        stop_reason: Any = None
        # 见到 message_stop、stop_reason 或 [DONE] 才算收尾；连接自己断了的流走不到这一步。
        terminated = False

        def merged_usage() -> dict[str, int] | None:
            merged = dict(input_usage)
            for key, value in output_usage.items():
                # message_delta 只补 output 侧；它带的 0 不能盖掉 message_start 报过的输入量。
                if key not in merged or value not in (None, 0):
                    merged[key] = value
            return _parse_usage({"usage": merged})

        def partial() -> dict[str, Any]:
            return {"text": "".join(text_parts), "reasoning": "".join(reasoning_parts), "usage": merged_usage()}

        try:
            async for sse_event in iter_sse_events(resp):
                raw_data = sse_event["data"]
                if raw_data.strip() == "[DONE]":
                    terminated = True
                    break
                try:
                    data = json.loads(raw_data)
                except ValueError:
                    log.warning("Anthropic SSE: bad JSON: %s", raw_data[:200])
                    continue
                if not isinstance(data, dict):
                    continue

                evt = data.get("type") or sse_event["event"]
                idx = data.get("index", 0)

                if evt == "message_start":
                    message = data.get("message") if isinstance(data.get("message"), dict) else {}
                    if isinstance(message.get("usage"), dict):
                        input_usage = message["usage"]

                elif evt == "content_block_start":
                    block = data.get("content_block") if isinstance(data.get("content_block"), dict) else {}
                    btype = block.get("type")
                    if btype == "tool_use":
                        await calls.open(idx, call_id=str(block.get("id") or ""), name=str(block.get("name") or ""))
                    elif btype == "thinking":
                        active_thinking[idx] = {"type": "thinking", "thinking_parts": []}
                    elif btype == "redacted_thinking":
                        # 不透明的加密块，开头就带着完整 data。
                        thinking_blocks.append({"type": "redacted_thinking", "data": block.get("data", "")})

                elif evt == "content_block_delta":
                    delta = data.get("delta") if isinstance(data.get("delta"), dict) else {}
                    dtype = delta.get("type", "")
                    if dtype == "thinking_delta":
                        chunk = delta.get("thinking", "")
                        if chunk:
                            reasoning_parts.append(chunk)
                            if idx in active_thinking:
                                active_thinking[idx]["thinking_parts"].append(chunk)
                            if on_thinking:
                                await on_thinking(chunk)
                    elif dtype == "text_delta":
                        chunk = delta.get("text", "")
                        if chunk:
                            text_parts.append(chunk)
                            if on_text:
                                await on_text(chunk)
                    elif dtype == "signature_delta":
                        # 签名单独一个事件，在该块最后一个 thinking_delta 之后、content_block_stop 之前。
                        sig = delta.get("signature", "")
                        if sig and idx in active_thinking:
                            active_thinking[idx]["signature"] = sig
                    elif dtype == "input_json_delta":
                        await calls.append(idx, delta.get("partial_json"))

                elif evt == "content_block_stop":
                    if idx in active_thinking:
                        info = active_thinking.pop(idx)
                        tb: dict[str, Any] = {"type": "thinking", "thinking": "".join(info["thinking_parts"])}
                        if "signature" in info:
                            tb["signature"] = info["signature"]
                        thinking_blocks.append(tb)
                    else:
                        await calls.close(idx)

                elif evt == "message_delta":
                    delta = data.get("delta") if isinstance(data.get("delta"), dict) else {}
                    if delta.get("stop_reason"):
                        stop_reason = delta["stop_reason"]
                        terminated = True
                    if isinstance(data.get("usage"), dict) and data["usage"]:
                        output_usage = data["usage"]

                elif evt == "message_stop":
                    terminated = True

                elif evt == "error":
                    error = read_error(data, fallback="stream error")
                    log.warning("Anthropic stream error event: %s", error.message)
                    return upstream_failure(status, error, **partial())
            if terminated:
                # 没收到 content_block_stop 的 tool_use 也一并收尾，参数解析不了的会以 _raw 交出去。
                await calls.finish()
        except PluginExecutionError:
            raise
        except Exception as exc:
            return exception_failure(exc, mid_stream=True, **partial())

        return _anthropic_response(
            status=status,
            terminated=terminated,
            stop_reason=stop_reason,
            text="".join(text_parts),
            reasoning="".join(reasoning_parts),
            tool_calls=calls.tool_calls(),
            usage=merged_usage(),
            thinking_blocks=thinking_blocks,
        )
