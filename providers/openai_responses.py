"""OpenAI Responses API provider (/v1/responses).

Converts chat/completions-style engine messages into Responses 'input' items
(system → instructions, typed message / function_call / function_call_output)
and parses streaming and non-streaming replies into ProviderResponse.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable

import httpx

from plugin_sdk import PluginExecutionError

from .assembly import (
    ToolCallAssembler, arguments_json, conclude, int_field, new_call_id, parse_tool_arguments, reported_int,
    usage_dict,
)
from .base import (
    BaseProvider, ProviderResponse, ToolCall, content_parts, content_text, image_part_url, provider_metadata,
    tool_call_fields,
)
from .degrade import DegradeRule, degrade, unset_options
from .failures import UpstreamError, exception_failure, failed, read_error, upstream_failure
from .openai import OpenAIProvider
from .options import ENUM, FLOAT, INT, TEXT, OptionSet, OptionSpec
from .plugin_transport import iter_sse_events, wrap_client

_MAX_DEGRADE_ROUNDS = 3

log = logging.getLogger(__name__)

# 流里遇到加密推理校验失败、需要剥掉加密内容重开一次时，_read_stream 用它告诉调用方。
_RETRY_WITHOUT_ENCRYPTED = object()

# response.incomplete 的原因 → 统一的 finish_reason，其余原因一律算 incomplete。
_INCOMPLETE_REASONS = {"max_output_tokens": "length", "content_filter": "content_filter"}

# NativeToolFormatter 把工具结果存成用户文本，更早的历史把工具调用存成文字记录：两种都认出来才能还原成原生 input 项。
_TOOL_RESULT_RE = re.compile(r'^Tool result for "(?P<name>[^"]+)":\r?\n(?P<output>.*)\Z', re.DOTALL)
# 按目标派发的伪工具名形如 dispatch:target.id，名字里要放行冒号、点和连字符。
_ENGINE_TOOL_RESULT_RE = re.compile(
    r'^\[(?P<name>[A-Za-z_][A-Za-z0-9_:\.-]*) result: (?P<output>.*)\]\Z',
    re.DOTALL,
)
_LEGACY_TOOL_RECORD_RE = re.compile(
    r'^\[Tool call history record: (?P<name>.+?) was executed with args: (?P<arguments>\{.*\})\]$'
)
_SYNTHETIC_TOOL_OUTPUT = "[No tool result was recorded by the engine.]"


def _strip_encrypted_content(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove encrypted_content from reasoning items, keeping summary."""
    cleaned: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict) and item.get("type") == "reasoning":
            # 旧轮次的加密推理 OpenAI 可能验不过：只留 summary，加密块不再回传。
            new_item = {k: v for k, v in item.items() if k != "encrypted_content"}
            if "summary" not in new_item:
                new_item["summary"] = []
            cleaned.append(new_item)
        else:
            cleaned.append(item)
    return cleaned


def _is_encrypted_content_error(error_msg: str) -> bool:
    """Return True for OpenAI encrypted reasoning verification failures."""
    lower = str(error_msg or "").lower()
    # 一般的验证/解密失败可能和推理块无关，两个标记都在才走去掉加密块重试这条路。
    has_encrypted_marker = "encrypted content" in lower or "encrypted_content" in lower
    has_verification_failure = "could not be verified" in lower or "could not be decrypted" in lower
    return has_encrypted_marker and has_verification_failure


def _parse_usage(usage: Any) -> dict[str, int] | None:
    """Responses API 的 usage → 统一形态。缓存与推理量都在 *_details 里；没报用量时为 None。"""
    if not isinstance(usage, dict) or not usage:
        return None
    return usage_dict(
        prompt=int_field(usage, "input_tokens"),
        completion=int_field(usage, "output_tokens"),
        total=int_field(usage, "total_tokens"),
        cache_read=reported_int(usage, "input_tokens_details", "cached_tokens"),
        reasoning=int_field(usage, "output_tokens_details", "reasoning_tokens"),
    )


def _terminal_state(response: dict[str, Any], event_type: str = "") -> tuple[str | None, str]:
    """一个收尾的 response 对象 → (统一的 finish_reason, 上游原话)。"""
    status = str(response.get("status") or "").strip().lower()
    if event_type == "response.incomplete" or status == "incomplete":
        details = response.get("incomplete_details")
        reason = str(details.get("reason") or "") if isinstance(details, dict) else ""
        return _INCOMPLETE_REASONS.get(reason, "incomplete"), f"response incomplete ({reason or 'no reason given'})"
    if status in ("cancelled", "queued", "in_progress"):
        return "incomplete", f"response {status}"
    return "stop", ""


def _output_index(event: dict[str, Any]) -> int:
    value = event.get("output_index")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _stream_error(event: dict[str, Any]) -> UpstreamError:
    """流里 type=error 事件的错误。字段平铺在事件顶层，个别中转又套了一层 error。"""
    error = read_error(event, fallback="stream error")
    code = event.get("code")
    if not error.code and isinstance(code, str) and code:
        return UpstreamError(error.message, error.type, code)
    return error


class OpenAIResponsesProvider(BaseProvider):
    """Provider targeting the OpenAI Responses API (`POST /v1/responses`)."""

    provider_name = "openai-responses"
    # 和 openai 同域名不同请求体，所以自成一族：Responses API 收不了 chat/completions 的 body。
    official_hosts = ("api.openai.com",)
    default_base_url = "https://api.openai.com/v1"

    # /models 与 chat/completions 同一套地址和认证，没有第二种写法。
    catalog_request = OpenAIProvider.catalog_request
    parse_catalog = OpenAIProvider.parse_catalog

    OPTIONS: tuple[OptionSpec, ...] = (
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
        ),
        OptionSpec(
            key="reasoning_summary", label="思考摘要", kind=ENUM, default="default",
            choices=(("default", "不要"), ("auto", "自动"), ("concise", "简"), ("detailed", "详")),
            desc="推理过程本身不外发，这里决定要不要一份可读的摘要。",
        ),
        OptionSpec(
            key="max_output_tokens", label="回复上限", kind=INT, default=None,
            minimum=16, maximum=200000, desc="留空则由模型默认值决定。",
        ),
        OptionSpec(
            key="verbosity", label="回复详尽度", kind=ENUM, default="default",
            choices=(("default", "不指定"), ("low", "简"), ("medium", "中"), ("high", "详")),
        ),
        OptionSpec(
            key="store", label="服务端留存", kind=ENUM, default="default",
            choices=(("default", "跟随默认"), ("true", "留存"), ("false", "不留存")),
            desc="对面默认会存一份对话。本项目自己管历史，也已经在回传加密的推理内容，"
                 "所以设成「不留存」不会掉功能。",
        ),
        OptionSpec(
            key="temperature", label="随机度", kind=FLOAT, default=None,
            minimum=0.0, maximum=2.0, desc="留空则不发。推理系模型不接受这个参数。",
        ),
        OptionSpec(key="top_p", label="核采样", kind=FLOAT, default=None, minimum=0.0, maximum=1.0),
        OptionSpec(
            key="tool_choice", label="工具选择", kind=ENUM, default="default",
            choices=(("default", "不指定"), ("auto", "自行决定"), ("required", "必须调用"), ("none", "禁止调用")),
        ),
        OptionSpec(
            key="prompt_cache_key", label="缓存分组", kind=TEXT, default="",
            desc="同一个值的请求会被归到一组做前缀缓存。",
        ),
        OptionSpec(
            key="service_tier", label="服务档", kind=ENUM, default="default",
            choices=(("default", "不指定"), ("auto", "自动"), ("flex", "省钱优先"), ("priority", "速度优先")),
        ),
        OptionSpec(
            key="timeout_sec", label="超时", kind=FLOAT, default=600.0,
            minimum=10.0, maximum=1800.0,
        ),
    )

    DEGRADE_RULES: tuple[DegradeRule, ...] = (
        DegradeRule(
            name="去掉思考设置",
            match=("reasoning",),
            apply=unset_options("reasoning_effort", "reasoning_summary"),
            advice="这个模型不支持思考设置。",
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
            advice="推理系模型不接受采样参数。",
        ),
        DegradeRule(
            name="去掉服务档",
            match=("service_tier",),
            apply=unset_options("service_tier"),
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
            log.warning("openai-responses 不认识这些 provider_options，已忽略：%s", ", ".join(unknown))

    def _degrade(self, error: str) -> bool:
        return degrade(
            self.DEGRADE_RULES, self._options, error, provider=self.name, model=self.model,
        ) is not None

    def _endpoint(self) -> str:
        """Return the full POST URL for /v1/responses."""
        return self._base_url.rstrip("/") + "/responses"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _convert_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        """Convert engine messages to Responses API format.

        Returns (instructions, input_items):
          - instructions: concatenated system message text (may be empty)
          - input_items: list of Responses-API items

        Accepts both chat/completions-style tool messages and Clonoth's storage
        format (assistant.tool_calls plus tool results stored as user text), so
        Responses receives native function_call / function_call_output pairs.
        """
        instructions_parts: list[str] = []
        items: list[dict[str, Any]] = []
        pending_calls: list[dict[str, str]] = []
        synthetic_call_index = 0

        def _next_call_id() -> str:
            nonlocal synthetic_call_index
            synthetic_call_index += 1
            return f"call_{synthetic_call_index}"

        def _append_function_call(call: dict[str, str]) -> None:
            call_id = call.get("call_id") or _next_call_id()
            item = {
                "type": "function_call",
                "name": call.get("name", ""),
                "call_id": call_id,
                "arguments": call.get("arguments") or "{}",
            }
            items.append(item)
            pending_calls.append({"call_id": call_id, "name": item["name"]})

        for msg in messages:
            role = msg.get("role", "")

            if role == "system":
                # System messages are extracted into the top-level instructions field.
                instructions_parts.append(_extract_text(msg))
                continue

            if role == "user":
                text = _extract_text(msg)

                # Clonoth native tool results are stored as user text.  Pair the
                # result with the nearest pending tool call so Responses receives
                # the required function_call_output item instead of plain text.
                tool_result = _parse_native_tool_result(text) or _parse_engine_tool_result(text)
                if tool_result:
                    pending = _pop_pending_call(pending_calls, tool_result["name"])
                    if pending:
                        items.append({
                            "type": "function_call_output",
                            "call_id": pending["call_id"],
                            "output": tool_result["output"],
                        })
                        continue

                # Older fake-native history may already contain textual tool-call
                # records.  Rehydrate those records into native function_call
                # items while preserving any non-record text as a user message.
                remaining_text, legacy_calls = _split_legacy_tool_records(text)
                if legacy_calls:
                    if remaining_text.strip():
                        _flush_unpaired_function_calls(pending_calls, items)
                        items.append({
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": remaining_text}],
                        })
                    for legacy_call in legacy_calls:
                        _append_function_call(legacy_call)
                    continue

                # Normal user message → {type: message, role: user, content: [...]}.
                # If a previous terminal pseudo tool did not record a tool result,
                # synthesize one before the real user input so Responses receives
                # valid function_call/function_call_output pairing.
                _flush_unpaired_function_calls(pending_calls, items)
                items.append({
                    "type": "message",
                    "role": "user",
                    "content": _build_user_content(msg),
                })
                continue

            if role == "assistant":
                _flush_unpaired_function_calls(pending_calls, items)
                # 优先原样回放存下的 output，加密推理才能逐字回传；其中的 function_call
                # 同时登记进 pending_calls，等着和后面的工具结果配对。
                _raw_output = provider_metadata(msg, "openai-responses").get("raw_output")

                if _raw_output and isinstance(_raw_output, list):
                    for _raw_item in _raw_output:
                        if not isinstance(_raw_item, dict):
                            continue
                        # Copy before adding a missing summary so stored history is not mutated.
                        _item = dict(_raw_item)
                        if _item.get("type") == "reasoning" and "summary" not in _item:
                            _item["summary"] = []
                        items.append(_item)
                        if _item.get("type") == "function_call" and _item.get("call_id"):
                            pending_calls.append({
                                "call_id": str(_item.get("call_id") or ""),
                                "name": str(_item.get("name") or ""),
                            })
                    continue

                # No raw output: rebuild from content plus either chat/completions
                # tool_calls or Clonoth's simplified storage tool_calls.
                text = _extract_text(msg)
                if text:
                    items.append({
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    })
                for tc in msg.get("tool_calls") or []:
                    normalized = _normalize_tool_call(tc)
                    if normalized:
                        _append_function_call(normalized)
                continue

            if role == "tool":
                # Chat/completions-style tool result.  Keep explicit tool_call_id
                # when present; otherwise fall back to the pending-call queue.
                call_id = str(msg.get("tool_call_id") or "")
                if call_id:
                    pending_calls[:] = [p for p in pending_calls if p.get("call_id") != call_id]
                else:
                    pending = _pop_pending_call(pending_calls, "")
                    call_id = pending["call_id"] if pending else ""
                items.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": _extract_text(msg),
                })

        _flush_unpaired_function_calls(pending_calls, items)
        return "\n\n".join(instructions_parts), items

    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        _attempt: int = 0,
    ) -> ProviderResponse:
        instructions, input_items = self._convert_messages(messages)
        payload = self._build_payload(
            input_items=input_items,
            instructions=instructions,
            tools=tools,
            stream=False,
        )

        # 存档里的加密推理可能已经验不过；剥掉之后再发一次，第二次还被拒就照实报错。
        for attempt in range(2):
            try:
                resp = await self._http.post(
                    self._endpoint(),
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
            encrypted = resp.status_code == 400 and _is_encrypted_content_error(error.message)
            if encrypted and attempt == 0:
                log.warning("Encrypted content verification failed, retrying without encrypted reasoning")
                payload["input"] = _strip_encrypted_content(payload["input"])
                continue
            if (
                resp.status_code == 400
                and not encrypted
                and _attempt < _MAX_DEGRADE_ROUNDS
                and self._degrade(error.message)
            ):
                return await self.chat(messages=messages, tools=tools, _attempt=_attempt + 1)
            return upstream_failure(resp.status_code, error)

        try:
            body = resp.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            return failed("invalid_response", "invalid JSON response", status_code=resp.status_code)
        return self._parse_response(body, status_code=resp.status_code)

    async def chat_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_text: Callable[[str], Awaitable[None]] | None = None,
        on_thinking: Callable[[str], Awaitable[None]] | None = None,
        on_tool_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        _degrade_attempt: int = 0,
    ) -> ProviderResponse:
        instructions, input_items = self._convert_messages(messages)
        payload = self._build_payload(
            input_items=input_items,
            instructions=instructions,
            tools=tools,
            stream=True,
        )

        # 断掉的流续不上，加密推理被拒时只能整条请求重开；最多重开一次，每次重新累计。
        for attempt in range(2):
            try:
                async with self._http.stream(
                    "POST",
                    self._endpoint(),
                    headers=self._headers(),
                    json=payload,
                    timeout=self._timeout,
                ) as stream:
                    status = stream.status_code
                    if status >= 400:
                        error = read_error(await stream.aread(), fallback=f"HTTP {status}")
                        encrypted = status == 400 and _is_encrypted_content_error(error.message)
                        if encrypted and attempt == 0:
                            log.warning(
                                "Encrypted content verification failed, retrying stream without encrypted reasoning"
                            )
                            payload["input"] = _strip_encrypted_content(payload["input"])
                            continue
                        if (
                            status == 400
                            and not encrypted
                            and _degrade_attempt < _MAX_DEGRADE_ROUNDS
                            and self._degrade(error.message)
                        ):
                            # 状态码先于任何 SSE 事件到达，重来不会让调用方看到半截内容。
                            return await self.chat_stream(
                                messages=messages, tools=tools, on_text=on_text,
                                on_thinking=on_thinking, on_tool_delta=on_tool_delta,
                                _degrade_attempt=_degrade_attempt + 1,
                            )
                        return upstream_failure(status, error)
                    outcome = await self._read_stream(
                        stream, status,
                        on_text=on_text, on_thinking=on_thinking, on_tool_delta=on_tool_delta,
                        may_retry_encrypted=attempt == 0,
                    )
            except PluginExecutionError:
                raise
            except Exception as exc:
                return exception_failure(exc)

            if outcome is _RETRY_WITHOUT_ENCRYPTED:
                payload["input"] = _strip_encrypted_content(payload["input"])
                continue
            return outcome

        return failed("bad_request", "Encrypted content retry failed before receiving a response")

    async def _read_stream(
        self,
        stream: httpx.Response,
        status: int,
        *,
        on_text: Callable[[str], Awaitable[None]] | None,
        on_thinking: Callable[[str], Awaitable[None]] | None,
        on_tool_delta: Callable[[dict[str, Any]], Awaitable[None]] | None,
        may_retry_encrypted: bool,
    ) -> Any:
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        refusal_parts: list[str] = []
        calls = ToolCallAssembler(on_tool_delta)
        usage: dict[str, int] | None = None
        raw_output_items: list[dict[str, Any]] = []
        finish_reason: str | None = None
        detail = ""
        # 见到 response.completed / incomplete 或 [DONE] 才算收尾；连接自己断了的流走不到这一步。
        terminated = False

        def partial() -> dict[str, Any]:
            return {"text": "".join(text_parts), "reasoning": "".join(thinking_parts), "usage": usage}

        try:
            async for sse_event in iter_sse_events(stream):
                raw = sse_event["data"]
                if raw.strip() == "[DONE]":
                    terminated = True
                    break
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                etype = event.get("type") or sse_event["event"]

                if etype == "response.output_text.delta":
                    delta = event.get("delta")
                    if isinstance(delta, str) and delta:
                        text_parts.append(delta)
                        if on_text:
                            await on_text(delta)

                elif etype in ("response.reasoning_text.delta", "response.reasoning_summary_text.delta"):
                    delta = event.get("delta")
                    if isinstance(delta, str) and delta:
                        thinking_parts.append(delta)
                        if on_thinking:
                            await on_thinking(delta)

                elif etype == "response.refusal.delta":
                    delta = event.get("delta")
                    if isinstance(delta, str) and delta:
                        refusal_parts.append(delta)

                elif etype == "response.output_item.added":
                    item = event.get("item")
                    if isinstance(item, dict) and item.get("type") == "function_call":
                        await calls.open(
                            _output_index(event),
                            call_id=str(item.get("call_id") or ""),
                            name=str(item.get("name") or ""),
                        )

                elif etype == "response.function_call_arguments.delta":
                    await calls.append(_output_index(event), event.get("delta"))

                elif etype == "response.function_call_arguments.done":
                    key = _output_index(event)
                    arguments = event.get("arguments")
                    calls.amend(key, arguments=arguments if isinstance(arguments, str) else None)
                    await calls.close(key)

                elif etype == "response.output_item.done":
                    item = event.get("item")
                    if isinstance(item, dict) and item.get("type") == "function_call":
                        key = _output_index(event)
                        call_id, name = str(item.get("call_id") or ""), str(item.get("name") or "")
                        if not calls.is_open(key):
                            # 有的中转只发 output_item.done，不发 added。
                            await calls.open(key, call_id=call_id, name=name)
                        arguments = item.get("arguments")
                        calls.amend(
                            key, call_id=call_id, name=name,
                            arguments=arguments if isinstance(arguments, str) else None,
                        )
                        await calls.close(key)

                elif etype in ("response.completed", "response.done", "response.incomplete"):
                    response = event.get("response") if isinstance(event.get("response"), dict) else {}
                    usage = _parse_usage(response.get("usage")) or usage
                    output = response.get("output")
                    if isinstance(output, list) and output:
                        raw_output_items = output
                    finish_reason, detail = _terminal_state(response, etype)
                    terminated = True

                elif etype == "response.failed":
                    response = event.get("response") if isinstance(event.get("response"), dict) else {}
                    error = read_error(response, fallback="response failed")
                    if may_retry_encrypted and _is_encrypted_content_error(error.message):
                        # 加密推理被拒也可能在连接建立之后才以 response.failed 报出来。
                        log.warning(
                            "Encrypted content verification failed, retrying stream without encrypted reasoning"
                        )
                        return _RETRY_WITHOUT_ENCRYPTED
                    return upstream_failure(
                        status, UpstreamError(f"API failed: {error.message}", error.type, error.code), **partial(),
                    )

                elif etype == "error":
                    error = _stream_error(event)
                    return upstream_failure(
                        status, UpstreamError(f"Stream error: {error.message}", error.type, error.code), **partial(),
                    )
            if terminated:
                await calls.finish()
        except PluginExecutionError:
            raise
        except Exception as exc:
            return exception_failure(exc, mid_stream=True, **partial())

        text = "".join(text_parts)
        refusal = "".join(refusal_parts)
        tool_calls = calls.tool_calls()
        if refusal and not text and not tool_calls and finish_reason in (None, "stop"):
            finish_reason = "refusal"
        return conclude(
            terminated=terminated,
            finish_reason=finish_reason,
            status_code=status,
            text=text,
            reasoning="".join(thinking_parts),
            tool_calls=tool_calls,
            usage=usage,
            provider_meta={"raw_output": raw_output_items} if raw_output_items else None,
            detail=refusal or detail,
        )

    def _build_payload(
        self,
        *,
        input_items: list[dict[str, Any]],
        instructions: str,
        tools: list[dict[str, Any]] | None,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "input": input_items,
            "stream": stream,
            # Request encrypted reasoning tokens for multi-turn round-trip
            "include": ["reasoning.encrypted_content"],
        }
        if instructions:
            payload["instructions"] = instructions
        if self._options.is_set("max_output_tokens"):
            payload["max_output_tokens"] = int(self._options.get("max_output_tokens"))
        for key in ("temperature", "top_p"):
            if self._options.is_set(key):
                payload[key] = self._options.get(key)
        verbosity = self._options.get("verbosity")
        if verbosity and verbosity != "default":
            payload["text"] = {"verbosity": verbosity}
        store = self._options.get("store")
        if store in ("true", "false"):
            payload["store"] = store == "true"
        choice = self._options.get("tool_choice")
        if tools and choice and choice != "default":
            payload["tool_choice"] = choice
        cache_key = str(self._options.get("prompt_cache_key") or "")
        if cache_key:
            payload["prompt_cache_key"] = cache_key
        tier = self._options.get("service_tier")
        if tier and tier != "default":
            payload["service_tier"] = tier
        if tools:
            # Responses API tools format: flatten the {type, function: {name, ...}} wrapper
            # chat/completions: [{type: "function", function: {name, description, parameters}}]
            # Responses API: [{type: "function", name, description, parameters}]
            resp_tools = []
            for t in tools:
                fn = t.get("function", {})
                if fn:
                    resp_tools.append({
                        "type": "function",
                        "name": fn.get("name", ""),
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters", {}),
                    })
                else:
                    resp_tools.append(t)
            payload["tools"] = resp_tools
        reasoning: dict[str, Any] = {}
        effort = self._options.get("reasoning_effort")
        if effort and effort != "default":
            reasoning["effort"] = effort
        summary = self._options.get("reasoning_summary")
        if summary and summary != "default":
            reasoning["summary"] = summary
        if reasoning:
            payload["reasoning"] = reasoning
        return payload

    @staticmethod
    def _parse_response(body: dict[str, Any], status_code: int | None = None) -> ProviderResponse:
        """Parse a non-streaming Responses API JSON body into ProviderResponse."""
        if body.get("error") or str(body.get("status") or "").strip().lower() == "failed":
            return upstream_failure(status_code, read_error(body, fallback="response failed"))

        text_parts: list[str] = []
        thinking_parts: list[str] = []
        refusal_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        output = body.get("output")
        for item in output if isinstance(output, list) else []:
            if not isinstance(item, dict):
                continue
            itype = item.get("type", "")
            if itype == "message":
                for block in item.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "output_text":
                        text_parts.append(str(block.get("text") or ""))
                    elif block.get("type") == "refusal":
                        refusal_parts.append(str(block.get("refusal") or ""))
            elif itype == "reasoning":
                for block in item.get("summary") or []:
                    if isinstance(block, dict) and block.get("type") == "summary_text":
                        thinking_parts.append(str(block.get("text") or ""))
            elif itype == "function_call":
                name = str(item.get("name") or "").strip()
                if name:
                    tool_calls.append(ToolCall(
                        id=str(item.get("call_id") or "") or new_call_id(),
                        name=name,
                        arguments=parse_tool_arguments(item.get("arguments")),
                    ))

        text = "".join(text_parts)
        refusal = "".join(refusal_parts)
        finish_reason, detail = _terminal_state(body)
        if refusal and not text and not tool_calls and finish_reason == "stop":
            finish_reason = "refusal"
        return conclude(
            terminated=True,
            finish_reason=finish_reason,
            status_code=status_code,
            text=text,
            reasoning="".join(thinking_parts),
            tool_calls=tool_calls,
            usage=_parse_usage(body.get("usage")),
            # 原始 output 带着加密推理，下一轮要原样回传。
            provider_meta={"raw_output": output} if isinstance(output, list) and output else None,
            detail=refusal or detail,
        )


def _extract_text(msg: dict[str, Any]) -> str:
    """Extract plain text from a chat/completions message."""
    return content_text(msg.get("content"))


def _build_user_content(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert a user message's content into Responses API content blocks (input_text / input_image)."""
    result: list[dict[str, Any]] = []
    for block in content_parts(msg.get("content")):
        btype = block.get("type", "")
        if btype == "text":
            result.append({"type": "input_text", "text": str(block.get("text") or "")})
        elif btype == "image_url":
            url = image_part_url(block)
            # 空地址发出去就是一个 400，宁可让模型知道这里本该有张图。
            if url:
                result.append({"type": "input_image", "image_url": url})
            else:
                result.append({"type": "input_text", "text": "[image could not be converted]"})
    return result or [{"type": "input_text", "text": ""}]


def _normalize_tool_call(tc: Any) -> dict[str, str] | None:
    """历史里的一次工具调用 → Responses function_call 的字段。Responses 只收文本形式的参数。"""
    fields = tool_call_fields(tc)
    if fields is None:
        return None
    call_id, name, arguments = fields
    text = arguments_json(arguments)
    return {"call_id": call_id, "name": name, "arguments": text if text.strip() else "{}"}


def _parse_native_tool_result(text: str) -> dict[str, str] | None:
    """Parse NativeToolFormatter's textual tool-result storage shape.

    The engine currently stores native tool results as user messages like
    ``Tool result for "name":\n...``.  Recognizing that exact format lets this
    provider rebuild a Responses ``function_call_output`` item.
    """
    match = _TOOL_RESULT_RE.match(text or "")
    if not match:
        return None
    return {"name": match.group("name"), "output": match.group("output")}


def _parse_engine_tool_result(text: str) -> dict[str, str] | None:
    """Parse engine-generated pseudo-tool result text.

    Some pseudo tools store bracketed text such as ``[dispatch:target result: ...]``
    instead of using format_tool_result(); it must still pair with the preceding
    function_call, or the next request carries an unpaired tool call.
    """
    raw = text or ""
    match = _ENGINE_TOOL_RESULT_RE.match(raw)
    if match:
        return {"name": match.group("name"), "output": match.group("output")}
    if raw.startswith("[Context compression "):
        return {"name": "compact_context", "output": raw}
    return None


def _split_legacy_tool_records(text: str) -> tuple[str, list[dict[str, str]]]:
    """Split old fake-native tool-call records from ordinary user text.

    NativeToolFormatter used to expose tool calls as lines such as
    ``[Tool call history record: name was executed with args: {...}]``.  The
    provider rehydrates those lines into native function_call items and leaves
    all other lines as user text.
    """
    remaining: list[str] = []
    calls: list[dict[str, str]] = []
    for line in (text or "").splitlines():
        match = _LEGACY_TOOL_RECORD_RE.match(line.strip())
        if not match:
            remaining.append(line)
            continue
        calls.append({
            "call_id": "",
            "name": match.group("name"),
            "arguments": match.group("arguments") or "{}",
        })
    return "\n".join(remaining), calls


def _pop_pending_call(pending_calls: list[dict[str, str]], name: str) -> dict[str, str] | None:
    """Pop the best matching pending function call for a tool result.

    Prefer matching by function name because Clonoth's textual tool-result
    storage does not carry a call ID.  Fall back to FIFO so older histories still
    make progress when names are unavailable or mismatched.
    """
    if name:
        for idx, pending in enumerate(pending_calls):
            if pending.get("name") == name:
                return pending_calls.pop(idx)
    if pending_calls:
        return pending_calls.pop(0)
    return None


def _flush_unpaired_function_calls(
    pending_calls: list[dict[str, str]],
    items: list[dict[str, Any]],
) -> None:
    """Emit synthetic outputs for pending calls that have no stored result.

    Terminal pseudo tools such as switch_node, and some compacted histories, can
    leave a function_call without a stored result. Responses requires every
    function_call to be paired before later messages.
    """
    while pending_calls:
        pending = pending_calls.pop(0)
        items.append({
            "type": "function_call_output",
            "call_id": pending.get("call_id", ""),
            "output": _SYNTHETIC_TOOL_OUTPUT,
        })
