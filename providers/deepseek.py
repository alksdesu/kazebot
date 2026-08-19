"""DeepSeek provider — OpenAI 兼容端点，外加思考模式与思维链回传。

与 OpenAI 的差别只在请求体和默认地址上，所以只覆写 _build_payload：
SSE 解析、错误对象、异常 finish_reason 全部复用父类，不再各写一份。

Ref: https://api-docs.deepseek.com/guides/thinking_mode
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .degrade import DegradeRule, unset_options
from .openai import OpenAIProvider
from .options import BOOL, ENUM, OptionSet, OptionSpec

log = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.deepseek.com"
# 这几项 DeepSeek 不认：详尽度没有，缓存分组是自动的不接受手动分组。
_UNSUPPORTED = frozenset({"verbosity", "prompt_cache_key"})


class DeepSeekProvider(OpenAIProvider):
    """DeepSeek provider with native thinking mode support."""

    provider_name = "deepseek"
    official_hosts = ("api.deepseek.com",)
    # DeepSeek 至今没有收图的模型，带图请求会 400。
    default_supports_vision = False

    OPTIONS: tuple[OptionSpec, ...] = tuple(
        spec for spec in OpenAIProvider.OPTIONS
        if spec.key not in _UNSUPPORTED and spec.key != "reasoning_effort"
    ) + (
        OptionSpec(
            key="thinking", label="思考模式", kind=BOOL, default=True,
            desc="开启后思维链走 reasoning_content 单独返回。",
        ),
        OptionSpec(
            key="reasoning_effort", label="思考档位", kind=ENUM, default="high",
            choices=(("high", "高"), ("max", "最高")),
            depends_on=("thinking", True),
            desc="只有这两档；「最高」需要更长的输出窗口。",
        ),
    )

    DEGRADE_RULES: tuple[DegradeRule, ...] = tuple(
        rule for rule in OpenAIProvider.DEGRADE_RULES if rule.name != "去掉详尽度"
    ) + (
        DegradeRule(
            name="关掉思考模式",
            match=("thinking",),
            apply=unset_options("thinking"),
            advice="这个模型或端点不支持思考模式。",
        ),
    )

    def __init__(
        self,
        *,
        http: httpx.AsyncClient | None = None,
        api_key: str,
        base_url: str | None = None,
        model: str = "deepseek-v4-pro",
        provider_options: dict[str, Any] | None = None,
    ) -> None:
        # 连接池要在 super() 之前建好，而超时值又藏在 options 里，所以先解析一次。
        timeout = float(OptionSet(self.OPTIONS, provider_options).get("timeout_sec"))
        super().__init__(
            http=http or httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0)),
            api_key=api_key,
            base_url=base_url or _DEFAULT_BASE_URL,
            model=model,
            provider_options=provider_options,
        )
        # 自己造的连接池要自己收，否则每建一个 provider 就漏一组 socket。
        self._owns_http = http is None
        # super() 把 name 设成了 "openai"，注册 key 和实例名必须对齐。
        self.name = self.provider_name

    async def aclose(self) -> None:
        if self._owns_http and not self._http.is_closed:
            await self._http.aclose()

    async def __aenter__(self) -> "DeepSeekProvider":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        stream: bool = False,
    ) -> dict[str, Any]:
        payload = super()._build_payload(messages, tools, stream=stream)
        payload["messages"] = _keep_reasoning_content(payload["messages"])

        thinking = bool(self._options.get("thinking"))
        payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
        if not thinking:
            payload.pop("reasoning_effort", None)
            return payload
        payload["reasoning_effort"] = self._options.get("reasoning_effort")
        # 思考模式下这些参数被静默忽略：不报错、也不生效。发出去只会让人
        # 以为调过了，所以这里直接不发。
        dropped = [key for key in ("temperature", "top_p") if payload.pop(key, None) is not None]
        if dropped:
            log.warning(
                "deepseek 思考模式下 %s 不起作用，已不发送；要用它们请先关掉思考模式。",
                "、".join(dropped),
            )
        return payload


def _keep_reasoning_content(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """多轮带工具调用时，assistant 的 reasoning_content 必须原样回传。"""
    result: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") != "assistant":
            result.append(msg)
            continue
        kept: dict[str, Any] = {"role": "assistant"}
        for key in ("content", "reasoning_content", "tool_calls"):
            value = msg.get(key)
            if value is not None:
                kept[key] = value
        result.append(kept)
    return result
