"""
Google Gemini native API provider.

Talks to generateContent / streamGenerateContent, converting OpenAI-format
messages and tools to Gemini's native format.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

import httpx

from plugin_sdk import PluginExecutionError
from tools._wire import WIRE_GEMINI, headers as wire_headers

from .assembly import ToolCallAssembler, conclude, int_field, parse_tool_arguments, usage_dict
from .base import (
    BaseProvider, ProviderResponse, content_parts, content_text, image_part_url, provider_metadata,
    split_data_url, tool_call_fields,
)
from .degrade import DegradeRule, degrade, set_option
from .failures import exception_failure, failed, read_error, upstream_failure
from .options import BOOL, ENUM, FLOAT, INT, OptionSet, OptionSpec
from .plugin_transport import iter_sse_events, wrap_client

log = logging.getLogger(__name__)

_MAX_DEGRADE_ROUNDS = 3

# 放开安全过滤时要逐类声明，Gemini 没有"全部"这种写法。
_HARM_CATEGORIES = (
    "HARM_CATEGORY_HARASSMENT",
    "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    "HARM_CATEGORY_DANGEROUS_CONTENT",
    "HARM_CATEGORY_JAILBREAK",
)

# finishReason → 统一的 finish_reason。
_FINISH_REASONS = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    "IMAGE_SAFETY": "content_filter",
    "IMAGE_PROHIBITED_CONTENT": "content_filter",
    "IMAGE_RECITATION": "content_filter",
}
# 模型想调工具却写坏了：什么都没产出时算一次无效响应，换一次大概率就好。
_EMPTY_KINDS = {
    "MALFORMED_FUNCTION_CALL": "invalid_response",
    "UNEXPECTED_TOOL_CALL": "invalid_response",
    "TOO_MANY_TOOL_CALLS": "invalid_response",
}


class _Collected:
    """一次响应里各类 part 的累积。流式和非流式共用，回调只有流式才给。"""

    def __init__(
        self,
        *,
        on_text: Callable[[str], Awaitable[None]] | None = None,
        on_thinking: Callable[[str], Awaitable[None]] | None = None,
        on_tool_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.text: list[str] = []
        self.thinking: list[str] = []
        self.inline_data: list[dict[str, Any]] = []
        # Gemini 3 要求整个 response 的 parts 原样回传，任何一块都可能带 thoughtSignature。
        self.raw_parts: list[dict[str, Any]] = []
        self.calls = ToolCallAssembler(on_tool_delta)
        self._on_text = on_text
        self._on_thinking = on_thinking

    async def take(self, parts: Any) -> None:
        for part in parts if isinstance(parts, list) else []:
            if not isinstance(part, dict):
                continue
            self.raw_parts.append(part)
            if part.get("thought"):
                chunk = part.get("text")
                if isinstance(chunk, str) and chunk:
                    self.thinking.append(chunk)
                    if self._on_thinking:
                        await self._on_thinking(chunk)
            elif "text" in part:
                chunk = part.get("text")
                if isinstance(chunk, str) and chunk:
                    self.text.append(chunk)
                    if self._on_text:
                        await self._on_text(chunk)
            elif isinstance(part.get("functionCall"), dict):
                call = part["functionCall"]
                # 没有 id 时由 assembler 补一个全局唯一的；各轮都从 call_1 编号会让审批和结果配错对象。
                await self.calls.add_complete(
                    call_id=str(call.get("id") or ""), name=str(call.get("name") or ""), arguments=call.get("args"),
                )
            elif isinstance(part.get("inlineData"), dict):
                self.inline_data.append(part["inlineData"])


def _gemini_response(
    collected: _Collected,
    *,
    status: int | None,
    terminated: bool,
    finish_raw: Any,
    block_reason: Any,
    usage: dict[str, int] | None,
    raw: dict[str, Any] | None = None,
) -> ProviderResponse:
    """流式和非流式共用的收尾判定。"""
    text = "".join(collected.text)
    tool_calls = collected.calls.tool_calls()
    if block_reason and not text and not tool_calls and not collected.inline_data:
        return failed(
            "content_filter", f"Blocked by safety filter: {block_reason}",
            status_code=status, finish_reason="content_filter", usage=usage, raw=raw,
        )
    key = str(finish_raw or "").strip().upper()
    finish_reason = _FINISH_REASONS.get(key)
    return conclude(
        terminated=terminated,
        finish_reason=finish_reason,
        status_code=status,
        text=text,
        reasoning="".join(collected.thinking),
        tool_calls=tool_calls,
        usage=usage,
        inline_data=collected.inline_data,
        provider_meta={"raw_parts": collected.raw_parts} if collected.raw_parts else None,
        raw=raw,
        empty_kind=_EMPTY_KINDS.get(key) or ("unknown" if key and finish_reason is None else None),
        detail=f"upstream stopped (finishReason={finish_raw})" if finish_raw else "",
    )


class GeminiProvider(BaseProvider):
    """Provider for Google Gemini (generativelanguage.googleapis.com)."""

    provider_name = "gemini"
    official_hosts = ("generativelanguage.googleapis.com",)
    default_base_url = "https://generativelanguage.googleapis.com"

    @classmethod
    def catalog_request(cls, *, base_url: str, api_key: str):
        base = cls.resolve_base_url(base_url)
        # 鉴权头和 _headers() 同一份判定，否则拉得到列表却聊不了天。
        return f"{base}/v1beta/models", wire_headers(WIRE_GEMINI, base, api_key)

    @staticmethod
    def parse_catalog(payload):
        items = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        out: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            # 这里的 name 形如 models/gemini-2.0-flash，前缀不是模型名的一部分。
            name = str(item.get("name") or "").strip().rsplit("/", 1)[-1]
            if name:
                out.append(name)
        return out

    OPTIONS: tuple[OptionSpec, ...] = (
        OptionSpec(
            key="thinking_mode", label="思考模式", kind=ENUM, default="auto",
            choices=(
                ("auto", "跟随模型默认"),
                ("level", "按档位"),
                ("budget", "按预算"),
            ),
            desc="档位和预算按代二选一，同时给会被拒：3 系认档位，2.5 系认预算。"
                 "拿不准就留「跟随模型默认」——那样不发这个字段。",
        ),
        OptionSpec(
            key="thinking_level", label="思考档位", kind=ENUM, default="medium",
            choices=(("minimal", "最少"), ("low", "低"), ("medium", "中"), ("high", "高")),
            depends_on=("thinking_mode", "level"),
            desc="部分 Flash 机型不支持「最少」。另外「最少」仍然要求带上思维签名。",
        ),
        OptionSpec(
            key="thinking_budget", label="思考预算", kind=INT, default=8192,
            minimum=0, maximum=32768, depends_on=("thinking_mode", "budget"),
            desc="0 表示不思考，但 3 系的 Pro 机型不接受 0。",
        ),
        OptionSpec(
            key="include_thoughts", label="返回思考摘要", kind=BOOL, default=True,
            desc="关掉就只拿最终答案，界面上不再有思考过程。",
        ),
        OptionSpec(
            key="max_output_tokens", label="回复上限", kind=INT, default=65536,
            minimum=256, maximum=131072,
        ),
        OptionSpec(
            key="temperature", label="随机度", kind=FLOAT, default=None,
            minimum=0.0, maximum=2.0, desc="留空则不发，由模型默认值决定。",
        ),
        OptionSpec(
            key="top_p", label="核采样", kind=FLOAT, default=None, minimum=0.0, maximum=1.0,
        ),
        OptionSpec(
            key="safety_threshold", label="安全过滤", kind=ENUM, default="default",
            choices=(
                ("default", "跟随默认"),
                ("block_only_high", "只拦最高危"),
                ("block_none", "不拦"),
                ("off", "整个关掉"),
            ),
            desc="Gemini 默认会拦掉不少角色扮演内容，被拦时回复为空且没有报错。",
        ),
        OptionSpec(
            key="timeout_sec", label="超时", kind=FLOAT, default=300.0,
            minimum=10.0, maximum=1800.0,
        ),
    )

    DEGRADE_RULES: tuple[DegradeRule, ...] = (
        DegradeRule(
            name="改用思考档位",
            match=("not supported together",),
            apply=set_option("thinking_mode", "level"),
            advice="档位和预算只能给一个。",
        ),
        DegradeRule(
            name="改用思考预算",
            match=("thinking level is not supported", "thinking_level"),
            apply=set_option("thinking_mode", "budget"),
            advice="这个模型只认预算，把思考模式改成「按预算」。",
        ),
        DegradeRule(
            name="改用思考档位",
            match=("budget 0 is invalid", "only works in thinking mode"),
            apply=set_option("thinking_mode", "level"),
            advice="这个模型不能不思考。",
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
            log.warning("gemini 不认识这些 provider_options，已忽略：%s", ", ".join(unknown))

    def _degrade(self, error: str) -> bool:
        return degrade(
            self.DEGRADE_RULES, self._options, error, provider="gemini", model=self.model,
        ) is not None

    def _endpoint(self, stream: bool = False) -> str:
        """Build the request URL for generateContent / streamGenerateContent."""
        action = "streamGenerateContent?alt=sse" if stream else "generateContent"
        return f"{self._base_url}/v1beta/models/{self.model}:{action}"

    def _headers(self) -> dict[str, str]:
        """Official googleapis.com takes x-goog-api-key; reverse proxies take a Bearer token."""
        return wire_headers(WIRE_GEMINI, self._base_url, self._api_key)

    @staticmethod
    def _convert_messages(
        messages: list[dict[str, Any]],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert OpenAI-format messages → Gemini contents + system instruction.

        Returns (system_text_or_None, gemini_contents_list).

        Conversion rules:
        - role=system  → extracted and merged into a single systemInstruction
        - role=user    → role="user", text/image parts
        - role=assistant → role="model", text + optional functionCall parts
        - role=tool     → role="function", functionResponse parts
                          (name is resolved from prior assistant tool_calls via
                           a tool_call_id→name mapping built on the fly; one
                           turn's results follow the prior functionCall order)
        """
        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []
        # Map tool_call_id → function name so we can fill functionResponse.name
        # when processing role=tool messages (which only carry tool_call_id).
        tc_id_to_name: dict[str, str] = {}
        # 上一条 model 消息里 functionCall 的 tool_call_id 顺序，和攒着还没交出去的这一轮工具结果
        call_order: list[str] = []
        pending_responses: list[tuple[str, dict[str, Any]]] = []

        def flush_responses() -> None:
            # Gemini requires all function responses for the same turn to be
            # grouped in a single role="function" entry.  Merge into the
            # previous entry if it's also a function response.
            if not pending_responses:
                return
            parts = _ordered_function_responses(pending_responses, call_order)
            pending_responses.clear()
            if contents and contents[-1].get("role") == "function":
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": "function", "parts": parts})

        for msg in messages:
            role = msg.get("role", "")
            if role not in ("tool", "system"):
                flush_responses()

            if role == "system":
                txt = _extract_text(msg)
                if txt:
                    system_parts.append(txt)
                continue

            if role == "user":
                contents.append({"role": "user", "parts": _user_parts(msg)})
                continue

            if role == "assistant":
                calls = [fields for fields in map(tool_call_fields, msg.get("tool_calls") or []) if fields]
                positional_ids: list[str] = []
                # Gemini 3 要求整条回复的 parts 原样回传：任何一块都可能带 thoughtSignature，不能合并或重建。
                raw_parts = provider_metadata(msg, "gemini").get("raw_parts")
                if raw_parts and isinstance(raw_parts, list):
                    # 早先的 id 是按 functionCall 出现顺序编的 call_1、call_2，没存 tool_calls 的旧历史只能这样对回名字。
                    position = 0
                    for part in raw_parts:
                        call = part.get("functionCall") if isinstance(part, dict) else None
                        if isinstance(call, dict):
                            position += 1
                            tc_id_to_name[f"call_{position}"] = str(call.get("name") or "")
                            positional_ids.append(f"call_{position}")
                    contents.append({"role": "model", "parts": raw_parts})
                else:
                    # 旧消息或别家 provider 产生的历史：从 tool_calls 重建。
                    parts: list[dict[str, Any]] = []
                    txt = _extract_text(msg)
                    if txt:
                        parts.append({"text": txt})
                    for _call_id, name, arguments in calls:
                        parts.append({"functionCall": {"name": name, "args": parse_tool_arguments(arguments)}})
                    if parts:
                        contents.append({"role": "model", "parts": parts})
                for call_id, name, _arguments in calls:
                    if call_id:
                        tc_id_to_name[call_id] = name
                call_order = [call_id for call_id, _name, _arguments in calls if call_id] or positional_ids
                continue

            if role == "tool":
                tc_id = msg.get("tool_call_id", "")
                # Resolve function name: prefer msg["name"] if present,
                # otherwise look up from the mapping we built.
                name = msg.get("name") or tc_id_to_name.get(tc_id, "unknown")
                raw_content = msg.get("content", "")
                # Gemini expects response to be an object, not a bare string.
                # Try to parse as JSON; fall back to wrapping in {"result": ...}.
                try:
                    resp_obj = json.loads(raw_content) if isinstance(raw_content, str) else raw_content
                    if not isinstance(resp_obj, dict):
                        resp_obj = {"result": resp_obj}
                except (json.JSONDecodeError, TypeError):
                    resp_obj = {"result": raw_content}
                pending_responses.append((str(tc_id or ""), {"functionResponse": {"name": name, "response": resp_obj}}))
                continue

            # 认不出的角色按用户消息发。
            txt = _extract_text(msg)
            if txt:
                contents.append({"role": "user", "parts": [{"text": txt}]})

        flush_responses()
        system_text = "\n\n".join(system_parts) if system_parts else None
        return system_text, contents

    @staticmethod
    def _convert_tools(
        tools: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]] | None:
        """Convert OpenAI tool declarations → Gemini functionDeclarations.

        OpenAI format:
          [{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}]
        Gemini format:
          [{"functionDeclarations": [{"name": ..., "description": ..., "parameters": ...}]}]
        """
        if not tools:
            return None
        decls: list[dict[str, Any]] = []
        for t in tools:
            fn = t.get("function") or t  # tolerate flat format too
            decl: dict[str, Any] = {"name": fn.get("name", "")}
            if fn.get("description"):
                decl["description"] = fn["description"]
            if fn.get("parameters"):
                decl["parameters"] = _clean_schema(fn["parameters"])
            decls.append(decl)
        return [{"functionDeclarations": decls}] if decls else None

    def _build_body(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        system_text, contents = self._convert_messages(messages)
        body: dict[str, Any] = {"contents": contents}
        if system_text:
            body["systemInstruction"] = {"parts": [{"text": system_text}]}
        gem_tools = self._convert_tools(tools)
        if gem_tools:
            body["tools"] = gem_tools
        gen_config: dict[str, Any] = {"maxOutputTokens": int(self._options.get("max_output_tokens"))}
        for key, field in (("temperature", "temperature"), ("top_p", "topP")):
            if self._options.is_set(key):
                gen_config[field] = self._options.get(key)
        thinking = self._thinking_config()
        if thinking:
            gen_config["thinkingConfig"] = thinking
        body["generationConfig"] = gen_config
        threshold = self._options.get("safety_threshold")
        if threshold != "default":
            body["safetySettings"] = [
                {"category": category, "threshold": threshold.upper()}
                for category in _HARM_CATEGORIES
            ]
        return body

    def _thinking_config(self) -> dict[str, Any]:
        mode = self._options.get("thinking_mode")
        config: dict[str, Any] = {}
        if mode == "level":
            config["thinkingLevel"] = str(self._options.get("thinking_level")).upper()
        elif mode == "budget":
            config["thinkingBudget"] = int(self._options.get("thinking_budget"))
        # includeThoughts 与档位/预算无关，但只在真的开了思考时才有意义。
        if config and self._options.get("include_thoughts"):
            config["includeThoughts"] = True
        return config

    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ProviderResponse:
        for attempt in range(_MAX_DEGRADE_ROUNDS + 1):
            body = self._build_body(messages, tools)
            try:
                resp = await self._http.post(
                    self._endpoint(stream=False),
                    headers=self._headers(),
                    json=body,
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
            return failed("invalid_response", "Gemini returned non-JSON response", status_code=resp.status_code)
        return await _parse_response(data, status_code=resp.status_code)

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
        body = self._build_body(messages, tools)
        try:
            req = self._http.build_request(
                "POST",
                self._endpoint(stream=True),
                headers=self._headers(),
                json=body,
                timeout=self._timeout,
            )
            resp = await self._http.send(req, stream=True)
        except PluginExecutionError:
            raise
        except Exception as exc:
            return exception_failure(exc)

        try:
            if resp.status_code < 400:
                return await self._read_stream(
                    resp, on_text=on_text, on_thinking=on_thinking, on_tool_delta=on_tool_delta,
                )
            try:
                error_body: bytes = await resp.aread()
            except PluginExecutionError:
                raise
            except Exception:
                error_body = b""
        finally:
            await resp.aclose()

        error = read_error(error_body, fallback=f"HTTP {resp.status_code}")
        if resp.status_code == 400 and _attempt < _MAX_DEGRADE_ROUNDS and self._degrade(error.message):
            # 状态码先于任何流式分片到达，重来不会让调用方看到半截内容。
            return await self.chat_stream(
                messages=messages, tools=tools, on_text=on_text,
                on_thinking=on_thinking, on_tool_delta=on_tool_delta,
                _attempt=_attempt + 1,
            )
        return upstream_failure(resp.status_code, error)

    async def _read_stream(
        self,
        resp: httpx.Response,
        *,
        on_text: Callable[[str], Awaitable[None]] | None,
        on_thinking: Callable[[str], Awaitable[None]] | None,
        on_tool_delta: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> ProviderResponse:
        status = int(resp.status_code)
        collected = _Collected(on_text=on_text, on_thinking=on_thinking, on_tool_delta=on_tool_delta)
        usage: dict[str, int] | None = None
        finish_raw: Any = None
        block_reason: Any = None
        # 见到 finishReason、提示被拦或 [DONE] 才算收尾；连接自己断了的流走不到这一步。
        terminated = False

        def partial() -> dict[str, Any]:
            return {"text": "".join(collected.text), "reasoning": "".join(collected.thinking), "usage": usage}

        try:
            async for sse_event in iter_sse_events(resp):
                data = sse_event["data"].strip()
                if not data:
                    continue
                if data == "[DONE]":
                    terminated = True
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if isinstance(chunk, list):
                    chunk = next((item for item in chunk if isinstance(item, dict)), None)
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("error"):
                    return upstream_failure(status, read_error(chunk), **partial())

                feedback = chunk.get("promptFeedback")
                if isinstance(feedback, dict) and feedback.get("blockReason"):
                    block_reason = feedback["blockReason"]
                    terminated = True
                candidates = chunk.get("candidates")
                candidate = candidates[0] if isinstance(candidates, list) and candidates else None
                if isinstance(candidate, dict):
                    content = candidate.get("content") if isinstance(candidate.get("content"), dict) else {}
                    await collected.take(content.get("parts"))
                    if candidate.get("finishReason"):
                        finish_raw = candidate["finishReason"]
                        terminated = True
                if isinstance(chunk.get("usageMetadata"), dict):
                    usage = _parse_usage(chunk["usageMetadata"]) or usage
            if terminated:
                await collected.calls.finish()
        except PluginExecutionError:
            raise
        except Exception as exc:
            return exception_failure(exc, mid_stream=True, **partial())

        return _gemini_response(
            collected,
            status=status,
            terminated=terminated,
            finish_raw=finish_raw,
            block_reason=block_reason,
            usage=usage,
        )



def _extract_text(msg: dict[str, Any]) -> str | None:
    """Extract text from a message's 'content' field.

    Handles both plain-string content and the OpenAI multi-part
    [{"type": "text", "text": "..."}] format.
    """
    return content_text(msg.get("content")) or None


def _user_parts(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """Build Gemini 'parts' array for a user message (text + image_url with base64 data URIs)."""
    parts: list[dict[str, Any]] = []
    for item in content_parts(msg.get("content")):
        if item.get("type") == "text":
            parts.append({"text": str(item.get("text") or "")})
        elif item.get("type") == "image_url":
            url = image_part_url(item)
            parsed = split_data_url(url)
            if parsed:
                mime, b64data = parsed
                parts.append({"inlineData": {"mimeType": mime, "data": b64data}})
            elif url:
                # Gemini 不收任意 http(s) 地址，只能把地址本身作为文本交出去。
                parts.append({"text": f"[Image: {url}]"})
            else:
                parts.append({"text": "[image could not be converted]"})
    return parts or [{"text": ""}]


def _ordered_function_responses(
    responses: list[tuple[str, dict[str, Any]]], call_order: list[str],
) -> list[dict[str, Any]]:
    """同一轮的 functionResponse 按上一条 model 消息里 functionCall 的顺序排，对不上的按原顺序跟在后面。

    Gemini 只按名字和位置配对，不认 id：同一轮里同名调用的结果先后颠倒就会配错，所以按 tool_call_id 对回调用的位置。
    """
    position = {call_id: index for index, call_id in enumerate(call_order)}
    matched: dict[int, dict[str, Any]] = {}
    rest: list[dict[str, Any]] = []
    for call_id, part in responses:
        index = position.get(call_id) if call_id else None
        if index is None or index in matched:
            rest.append(part)
        else:
            matched[index] = part
    return [matched[index] for index in sorted(matched)] + rest


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Clean a JSON Schema for Gemini compatibility.

    Gemini's function-calling schema support is stricter than OpenAI's.
    Remove unsupported keys like 'additionalProperties', 'default', '$schema'.
    Recursively clean nested 'properties' and 'items'.
    """
    # Keys that Gemini's schema validator rejects
    UNSUPPORTED = {"additionalProperties", "default", "$schema", "$id", "$ref", "$defs", "definitions"}
    cleaned: dict[str, Any] = {}
    for k, v in schema.items():
        if k in UNSUPPORTED:
            continue
        if k == "properties" and isinstance(v, dict):
            cleaned[k] = {pk: _clean_schema(pv) if isinstance(pv, dict) else pv for pk, pv in v.items()}
        elif k == "items" and isinstance(v, dict):
            cleaned[k] = _clean_schema(v)
        elif k in ("anyOf", "oneOf", "allOf") and isinstance(v, list):
            cleaned[k] = [_clean_schema(item) if isinstance(item, dict) else item for item in v]
        elif k == "enum" and isinstance(v, list):
            # 生成出来的工具 enum 里可能混进 None 或空串，Gemini 见到就整个请求拒收。
            enum_values: list[Any] = []
            for item in v:
                if item is None:
                    continue
                if isinstance(item, str) and not item.strip():
                    continue
                if item not in enum_values:
                    enum_values.append(item)
            if enum_values:
                cleaned[k] = enum_values
        else:
            cleaned[k] = v
    return cleaned


def _parse_usage(um: Any) -> dict[str, int] | None:
    """Gemini 的 usageMetadata → 统一形态。没报用量时为 None。

    candidatesTokenCount 不含思考消耗、promptTokenCount 不含工具调用带回的输入，都加回来才和别家同一口径。
    """
    if not isinstance(um, dict) or not um:
        return None
    thoughts = int_field(um, "thoughtsTokenCount")
    return usage_dict(
        prompt=int_field(um, "promptTokenCount") + int_field(um, "toolUsePromptTokenCount"),
        completion=int_field(um, "candidatesTokenCount") + thoughts,
        total=int_field(um, "totalTokenCount"),
        # proto3 JSON 不输出值为 0 的字段，没命中时这一项根本不出现：缺了就是 0
        cache_read=int_field(um, "cachedContentTokenCount"),
        reasoning=thoughts,
    )


async def _parse_response(data: dict[str, Any], *, status_code: int | None = None) -> ProviderResponse:
    """Parse a non-streaming Gemini generateContent response into ProviderResponse."""
    if data.get("error"):
        return upstream_failure(status_code, read_error(data), raw=data)

    usage = _parse_usage(data.get("usageMetadata"))
    feedback = data.get("promptFeedback")
    block_reason = feedback.get("blockReason") if isinstance(feedback, dict) else None
    candidates = data.get("candidates")
    candidate = candidates[0] if isinstance(candidates, list) and candidates else None
    if not isinstance(candidate, dict):
        if block_reason:
            return failed(
                "content_filter", f"Blocked by safety filter: {block_reason}",
                status_code=status_code, finish_reason="content_filter", usage=usage, raw=data,
            )
        return failed("invalid_response", "Gemini returned no candidates", status_code=status_code, raw=data)

    collected = _Collected()
    content = candidate.get("content") if isinstance(candidate.get("content"), dict) else {}
    await collected.take(content.get("parts"))
    return _gemini_response(
        collected,
        status=status_code,
        terminated=True,
        finish_raw=candidate.get("finishReason"),
        block_reason=block_reason,
        usage=usage,
        raw=data,
    )
