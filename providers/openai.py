from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

import httpx

from .base import BaseProvider, ProviderResponse, ToolCall
from .degrade import DegradeRule, degrade, set_option, unset_options
from .options import ENUM, FLOAT, INT, TEXT, OptionSet, OptionSpec

log = logging.getLogger(__name__)

_MAX_DEGRADE_ROUNDS = 3


def _parse_first_json_object(raw: str) -> dict | None:
    """Fallback: parse the first JSON object from concatenated '{...}{...}' strings."""
    raw = raw.strip()
    if not raw.startswith("{"):
        return None
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(raw)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _normalize_base_url(base_url: str | None) -> str:
    """Normalize base_url for OpenAI-compatible APIs.

    Accepts:
    - https://api.openai.com
    - https://api.openai.com/v1
    - https://proxy.example.com/v1
    - https://api.deepseek.com

    Returns a base URL with trailing slash stripped. Does NOT force /v1 suffix
    — the caller writes the full path they intend, provider appends
    /chat/completions directly.
    """

    base = (base_url or "").strip().rstrip("/")
    if not base:
        base = "https://api.openai.com/v1"

    # 缺少协议前缀时补上 https://
    if base and not base.startswith("http://") and not base.startswith("https://"):
        base = "https://" + base

    return base


def _extract_openai_error_message(payload: Any) -> str | None:
    """Try to extract a human-readable error message from OpenAI-style JSON."""

    if not isinstance(payload, dict):
        return None

    err = payload.get("error")
    if isinstance(err, dict):
        msg = err.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()

    return None

def _nested_int(holder: Any, parent: str, key: str) -> int:
    branch = holder.get(parent) if isinstance(holder, dict) else None
    value = branch.get(key) if isinstance(branch, dict) else None
    return value if isinstance(value, int) else 0


def _extract_usage(data: Any) -> dict[str, int] | None:
    """Extract token usage dict from an OpenAI response/chunk."""
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    result: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        val = usage.get(key)
        if isinstance(val, int):
            result[key] = val
    # 缓存命中量两家放的位置不同：OpenAI 嵌在 prompt_tokens_details 里，
    # DeepSeek 平铺在 usage 上。两个都收，谁也别漏。
    cached = _nested_int(usage, "prompt_tokens_details", "cached_tokens")
    if not cached:
        hit = usage.get("prompt_cache_hit_tokens")
        cached = hit if isinstance(hit, int) else 0
    if cached:
        result["cache_read_tokens"] = cached
    reasoning = _nested_int(usage, "completion_tokens_details", "reasoning_tokens")
    if reasoning:
        result["reasoning_tokens"] = reasoning
    return result if result else None



class OpenAIProvider(BaseProvider):
    """OpenAI-compatible provider implemented with raw HTTP (no SDK).

    This adapter targets the Chat Completions API:
        POST {base_url}/chat/completions

    It supports tool calling via the `tools` field.
    """

    # [provider-registry 2026-05-03] 这是自动发现注册使用的 key。
    # 原因：engine 不再硬编码 OpenAIProvider 分支；做法：类声明 provider_name；
    # 目的：registry 能把配置里的 "openai" 映射回这个类。
    provider_name = "openai"
    wire_format = "openai"
    official_hosts = ("api.openai.com",)

    # ------------------------------------------------------------------
    #  L3 Provider 层：消息预处理
    # ------------------------------------------------------------------

    @staticmethod
    def _prepare_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """L3: 将 L2 输出转换为 OpenAI API 最终格式。

        【三层管道 L3 实现】两项职责：
        1. 防御性剥离残留的 _ 开头内部字段（_meta, _dynamic 等），
           正常情况下 L2 已经剥离，这里做二次保险。
        2. Prefill Guard：如果最后一条消息是 assistant，追加一条
           user 消息，避免 Anthropic 系模型（通过 OpenAI 兼容端点
           访问时）因连续 assistant 消息报错。
        """
        result: list[dict[str, Any]] = []
        for msg in messages:
            # 剥离残留的内部字段（_ 开头的键）
            clean = {k: v for k, v in msg.items() if not k.startswith('_')}
            result.append(clean)
        # Prefill Guard：最后一条是 assistant 时追加 user 占位消息，
        # 防止某些 API 端点拒绝以 assistant 结尾的消息列表。
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
        # [provider-registry 2026-05-03] 实例 name 复用 provider_name。
        # 原因：下游仍通过 provider.name 判断 provider 特性；做法：从类属性传入；
        # 目的：注册 key、实例名和配置 provider 字段保持一致。
        super().__init__(model=model, name=self.provider_name)

        k = (api_key or "").strip()
        if not k:
            raise RuntimeError("openai api_key is empty")

        self._http = http
        self._api_key = k
        self._base_url = _normalize_base_url(base_url)
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
        # L3: 在发送前对消息做 Provider 层预处理（剥离内部字段 + Prefill Guard）
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._prepare_messages(messages),
        }
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
            # 子类会删掉自己不支持的项，那时 get 返回 None——别把 null 发出去。
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
        headers = self._headers()

        try:
            async with self._http.stream(
                "POST", url, headers=headers, json=payload, timeout=self._timeout,
            ) as resp:
                status = int(resp.status_code)

                if status >= 400:
                    body = await resp.aread()
                    try:
                        data = json.loads(body)
                        msg = _extract_openai_error_message(data)
                    except Exception:
                        msg = body.decode("utf-8", errors="replace").strip()
                    if not msg:
                        msg = f"HTTP {status}"
                    if status == 400 and _attempt < _MAX_DEGRADE_ROUNDS and self._degrade(msg):
                        # 状态码先于任何 SSE 分片到达，重来不会让调用方看到半截内容。
                        return await self.chat_stream(
                            messages=messages, tools=tools, on_text=on_text,
                            on_thinking=on_thinking, on_tool_delta=on_tool_delta,
                            _attempt=_attempt + 1,
                        )
                    # [fix 2026-04-18] 补齐 inline_data / provider_meta，与成功路径风格一致
                    return ProviderResponse(ok=False, error=msg, status_code=status, inline_data=[], provider_meta={})

                text_parts: list[str] = []
                # [refactor 2026-04-18] thinking_parts → reasoning_parts，
                # 变量名与 ProviderResponse.reasoning 对齐
                reasoning_parts: list[str] = []
                # index -> {id, name, arguments_parts}
                tc_map: dict[int, dict[str, Any]] = {}

                stream_usage: dict[str, int] | None = None
                _stream_finish_reason: str | None = None
                _stream_error: str | None = None
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break

                    try:
                        chunk = json.loads(data_str)
                    except Exception:
                        continue

                    # [2026-05-24] Capture SSE-level error objects
                    # Proxied Gemini sends {"error": {"message": ..., "code": "content_filter"}}
                    _err_obj = chunk.get("error")
                    if isinstance(_err_obj, dict):
                        _stream_error = _err_obj.get("message") or str(_err_obj)

                    # 捕获 usage（流式最后一个 chunk 携带）
                    raw_usage = chunk.get("usage")
                    if isinstance(raw_usage, dict):
                        stream_usage = _extract_usage(chunk)

                    choices = chunk.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue
                    # [2026-05-24] Capture finish_reason for content_filter detection
                    _fr = choices[0].get("finish_reason")
                    if _fr:
                        _stream_finish_reason = _fr
                    delta = choices[0].get("delta") or {}

                    # 文本内容
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        text_parts.append(content)
                        if on_text:
                            await on_text(content)

                    # 思维链 (reasoning_content / thinking)
                    # 注意：delta 字段名是 API 定义的，不改；只改内部变量名
                    reasoning = delta.get("reasoning_content") or delta.get("thinking")
                    if isinstance(reasoning, str) and reasoning:
                        reasoning_parts.append(reasoning)
                        # on_thinking 回调签名暂不改名（engine 接口，后续再统一）
                        if on_thinking:
                            await on_thinking(reasoning)

                    # 工具调用
                    raw_tcs = delta.get("tool_calls")
                    if isinstance(raw_tcs, list):
                        for tc in raw_tcs:
                            if not isinstance(tc, dict):
                                continue
                            idx = int(tc.get("index", 0))
                            fn = tc.get("function") or {}
                            if not isinstance(fn, dict):
                                fn = {}
                            fn_name = fn.get("name")
                            if idx not in tc_map:
                                tc_map[idx] = {
                                    "id": tc.get("id") or "",
                                    "name": fn_name or "",
                                    "arg_parts": [],
                                }
                                # [tool-stream 2026-05-19] 同步发出工具调用开始事件。
                                # 原因：OpenAI SSE 的 tool_calls delta 已经携带 index/id/name，过去只进入 tc_map。
                                # 做法：新 index 第一次出现时调用可选 on_tool_delta，不改变后续聚合逻辑。
                                # 目的：前端能实时看到 tool_call 建立，同时最终 ProviderResponse 仍完整返回。
                                if on_tool_delta:
                                    await on_tool_delta({
                                        "event": "tool_call_start",
                                        "index": idx,
                                        "id": tc_map[idx]["id"],
                                        "name": tc_map[idx]["name"],
                                    })
                            else:
                                if tc.get("id"):
                                    tc_map[idx]["id"] = tc["id"]
                                if fn_name:
                                    tc_map[idx]["name"] = fn_name
                            arg_chunk = fn.get("arguments", "")
                            if arg_chunk:
                                tc_map[idx]["arg_parts"].append(arg_chunk)
                                # [tool-stream 2026-05-19] 逐片段转发工具参数。
                                # 原因：参数字符串可能很长，等完整 JSON 结束才返回会阻塞实时 UI。
                                # 做法：保留 arg_parts 组装，同时把原始片段作为 args_delta 发出。
                                # 目的：实现 tool_call 参数与文本、thinking 一致的流式通道。
                                if on_tool_delta:
                                    await on_tool_delta({
                                        "event": "tool_call_args_delta",
                                        "index": idx,
                                        "delta": arg_chunk,
                                    })

                text = "".join(text_parts) if text_parts else None
                # [refactor 2026-04-18] thinking_text 局部变量 → reasoning_text，
                # 与 ProviderResponse.reasoning 字段对齐
                reasoning_text = "".join(reasoning_parts) if reasoning_parts else None

                tool_calls: list[ToolCall] = []
                for idx in sorted(tc_map.keys()):
                    tc_data = tc_map[idx]
                    name = tc_data["name"]
                    if not name:
                        continue
                    raw_args = "".join(tc_data["arg_parts"])
                    args: dict[str, Any] = {}
                    if raw_args.strip():
                        try:
                            parsed = json.loads(raw_args)
                            args = parsed if isinstance(parsed, dict) else {"_raw": parsed}
                        except Exception:
                            fallback = _parse_first_json_object(raw_args)
                            args = fallback if fallback is not None else {"_raw": raw_args}
                    tool_calls.append(ToolCall(id=tc_data["id"], name=name.strip(), arguments=args))

                # [2026-05-24] Detect upstream errors from SSE stream.
                # Various proxied providers (Gemini, OpenAI, Anthropic via relay)
                # may return HTTP 200 but signal failure through:
                #   1. SSE error objects: data: {"error": {"message": ..., "code": ...}}
                #   2. Abnormal finish_reason with empty content (content_filter, error, etc.)
                # Normal finish_reasons: stop, tool_calls — anything else with no
                # content is treated as an upstream error.
                _normal_finish = {"stop", "tool_calls", None}
                if _stream_error:
                    return ProviderResponse(
                        ok=False, text=text, tool_calls=[],
                        reasoning=reasoning_text, status_code=status, usage=stream_usage,
                        inline_data=[], provider_meta={},
                        error=_stream_error,
                    )
                if _stream_finish_reason not in _normal_finish and not text and not tool_calls:
                    return ProviderResponse(
                        ok=False, text=None, tool_calls=[],
                        reasoning=reasoning_text, status_code=status, usage=stream_usage,
                        inline_data=[], provider_meta={},
                        error=f"Upstream error (finish_reason={_stream_finish_reason})",
                    )

                # [refactor 2026-04-18] thinking= → reasoning=，新增 inline_data / provider_meta
                return ProviderResponse(
                    ok=True, text=text, tool_calls=tool_calls,
                    reasoning=reasoning_text, status_code=status, usage=stream_usage,
                    inline_data=[], provider_meta={},
                )

        except Exception as e:
            # [fix 2026-04-18] 补齐 inline_data / provider_meta，与成功路径风格一致
            return ProviderResponse(ok=False, error=str(e) or type(e).__name__, inline_data=[], provider_meta={})


    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        _attempt: int = 0,
    ) -> ProviderResponse:
        url = f"{self._base_url}/chat/completions"
        payload = self._build_payload(messages, tools, stream=False)
        headers = self._headers()

        try:
            r = await self._http.post(url, headers=headers, json=payload, timeout=self._timeout)
        except Exception as e:
            # [fix 2026-04-18] 补齐 inline_data / provider_meta，与成功路径风格一致
            return ProviderResponse(ok=False, error=str(e) or type(e).__name__, inline_data=[], provider_meta={})

        status = int(r.status_code)

        data: Any
        try:
            data = r.json()
        except Exception:
            data = None

        if status >= 400:
            msg = _extract_openai_error_message(data)
            if not msg:
                try:
                    msg = (r.text or "").strip()
                except Exception:
                    msg = ""
            if not msg:
                msg = f"HTTP {status}"
            if status == 400 and _attempt < _MAX_DEGRADE_ROUNDS and self._degrade(msg):
                return await self.chat(messages=messages, tools=tools, _attempt=_attempt + 1)

            # [fix 2026-04-18] 补齐 inline_data / provider_meta，与成功路径风格一致
            return ProviderResponse(
                ok=False,
                error=msg,
                status_code=status,
                raw=data if isinstance(data, dict) else None,
                inline_data=[], provider_meta={},
            )

        if not isinstance(data, dict):
            return ProviderResponse(ok=False, error="invalid JSON response", status_code=status, inline_data=[], provider_meta={})

        try:
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices:
                return ProviderResponse(ok=False, error="missing choices", status_code=status, raw=data, inline_data=[], provider_meta={})

            choice0 = choices[0]
            if not isinstance(choice0, dict):
                return ProviderResponse(ok=False, error="invalid choices[0]", status_code=status, raw=data, inline_data=[], provider_meta={})

            msg = choice0.get("message")
            if not isinstance(msg, dict):
                return ProviderResponse(ok=False, error="missing message", status_code=status, raw=data, inline_data=[], provider_meta={})

            content = msg.get("content")
            text = content if isinstance(content, str) else None
            # 流式路径一直在读这个字段，非流式却不读，同一个模型换条路走思维链就没了。
            raw_reasoning = msg.get("reasoning_content") or msg.get("reasoning")
            reasoning_text = raw_reasoning if isinstance(raw_reasoning, str) and raw_reasoning else None

            tool_calls: list[ToolCall] = []
            raw_tcs = msg.get("tool_calls")
            if isinstance(raw_tcs, list):
                for tc in raw_tcs:
                    if not isinstance(tc, dict):
                        continue

                    if tc.get("type") != "function":
                        continue

                    tc_id = tc.get("id")
                    tc_id_str = tc_id if isinstance(tc_id, str) else ""

                    fn = tc.get("function")
                    if not isinstance(fn, dict):
                        continue

                    name = fn.get("name")
                    if not isinstance(name, str) or not name.strip():
                        continue

                    raw_args = fn.get("arguments")
                    args: dict[str, Any] = {}
                    if isinstance(raw_args, str) and raw_args.strip():
                        try:
                            parsed = json.loads(raw_args)
                            if isinstance(parsed, dict):
                                args = parsed
                            else:
                                args = {"_raw": parsed}
                        except Exception:
                            fallback = _parse_first_json_object(raw_args)
                            args = fallback if fallback is not None else {"_raw": raw_args}

                    tool_calls.append(ToolCall(id=tc_id_str, name=name.strip(), arguments=args))

            # 解析 usage
            usage = _extract_usage(data)

            # [2026-05-24] Detect upstream errors in non-streaming responses.
            # Same logic as chat_stream: check for error objects and abnormal
            # finish_reason (content_filter, safety blocks, etc.)
            _err_obj = data.get("error")
            if isinstance(_err_obj, dict):
                _err_msg = _err_obj.get("message") or str(_err_obj)
                return ProviderResponse(
                    ok=False, text=text, tool_calls=[],
                    reasoning=reasoning_text,
                    status_code=status, usage=usage,
                    inline_data=[], provider_meta={},
                    error=_err_msg,
                )
            _finish_reason = choice0.get("finish_reason")
            _normal_finish = {"stop", "tool_calls", None}
            if _finish_reason not in _normal_finish and not text and not tool_calls:
                return ProviderResponse(
                    ok=False, text=None, tool_calls=[],
                    reasoning=reasoning_text,
                    status_code=status, usage=usage,
                    inline_data=[], provider_meta={},
                    error=f"Upstream error (finish_reason={_finish_reason})",
                )

            # [refactor 2026-04-18] 非流式也补齐 inline_data / provider_meta（OpenAI 目前不用）
            return ProviderResponse(
                ok=True, text=text, tool_calls=tool_calls, reasoning=reasoning_text,
                status_code=status, usage=usage, raw=data, inline_data=[], provider_meta={},
            )

        except Exception as e:
            return ProviderResponse(ok=False, error=f"failed to parse response: {e}", status_code=status, raw=data, inline_data=[], provider_meta={})
