from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from clonoth_runtime import env_bool, get_float, resolve_channel

from .node import Node


@dataclass
class ResolvedProvider:
    model: str
    # ProviderRegistry 的 key，也就是线格式。渠道名在 resolve_provider 里已经翻译掉了。
    provider_type: str = "openai"
    api_key: str | None = None   # None 表示使用全局默认
    base_url: str | None = None  # None 表示使用全局默认
    # 渠道块里写的 provider_options，比 runtime.yaml 的同名项更具体。
    channel_options: dict[str, Any] = field(default_factory=dict)
    channel_name: str = ""


def resolve_provider(
    workspace_root: Path,
    node: Node,
    provider_default: str,
    provider_default_type: str = "openai",
    session_override: dict[str, Any] | None = None,
    provider_default_channel: str = "",
    provider_default_options: Mapping[str, Any] | None = None,
) -> ResolvedProvider:
    """根据全局、节点和 session 配置解析模型和可选 api_key/base_url。

    provider 字段可以是 models.yaml 里的渠道名，也可以是裸线格式名。命中渠道块时
    连同它的 base_url/api_key/model/options 一起继承，自己写了的那几项仍然压过它。
    节点和 session 都没点名渠道时跟随主渠道，options 用主渠道块的 provider_default_options。
    """
    model = node.model.strip() if node.model else ""
    api_key = node.api_key.strip() if node.api_key else None
    base_url = node.base_url.strip() if node.base_url else None
    channel_name = node.provider.strip() if node.provider else ""

    override = session_override if isinstance(session_override, dict) else {}
    # 优先级 session > node > 渠道块 > 全局活跃渠道。session 层要在渠道继承之前落地，
    # 否则下面的 `or channel.base_url` 会把它顶掉。
    override_provider = str(override.get("provider") or override.get("provider_type") or "").strip()
    override_model = str(override.get("model") or "").strip()
    override_api_key = str(override.get("api_key") or "").strip()
    override_base_url = str(override.get("base_url") or "").strip()
    if override_provider:
        channel_name = override_provider
    if override_model:
        model = override_model
    if override_api_key:
        api_key = override_api_key
    if override_base_url:
        base_url = override_base_url

    channel = resolve_channel(workspace_root, channel_name)
    provider_type = channel.wire or provider_default_type or "openai"
    if channel.known:
        base_url = base_url or channel.base_url or None
        api_key = api_key or channel.api_key or None
        model = model or channel.model

    return ResolvedProvider(
        model=model or provider_default or "gpt-4o-mini",
        provider_type=provider_type,
        api_key=api_key or None,
        base_url=base_url or None,
        channel_options=dict(channel.options) if channel_name else dict(provider_default_options or {}),
        channel_name=channel.name or provider_default_channel or provider_default_type,
    )


def merge_provider_options(base: Mapping[str, Any] | None, overlay: Mapping[str, Any] | None) -> dict[str, Any]:
    """两层 provider options 合成一份，overlay 的值优先；两边都是映射的键逐层往下合，传进来的对象都不改。"""
    merged: dict[str, Any] = dict(base or {})
    for key, value in (overlay or {}).items():
        base_value = merged.get(key)
        if isinstance(base_value, dict) and isinstance(value, dict):
            merged[key] = merge_provider_options(base_value, value)
        else:
            merged[key] = value
    return merged


def layered_provider_options(
    runtime_cfg: Mapping[str, Any] | None,
    wire: str,
    *layers: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """一次请求最终用的 provider options：运行参数 providers.<wire>.options 垫底，layers 由宽到窄依次往上盖。

    不是映射的层按没写算。
    """
    providers = runtime_cfg.get("providers") if isinstance(runtime_cfg, Mapping) else None
    section = providers.get(wire or "openai") if isinstance(providers, Mapping) else None
    runtime_options = section.get("options") if isinstance(section, Mapping) else None
    merged = dict(runtime_options) if isinstance(runtime_options, Mapping) else {}
    for layer in layers:
        if isinstance(layer, Mapping):
            merged = merge_provider_options(merged, layer)
    return merged


def instantiate_provider(provider_cls: type, init_kwargs: Mapping[str, Any]) -> Any:
    """按构造函数收的参数挑着传：几家内置 provider 收的参数不完全一样，声明了 **kwargs 或看不出签名的拿全套。"""
    try:
        parameters = inspect.signature(provider_cls).parameters
    except (TypeError, ValueError):
        return provider_cls(**init_kwargs)
    if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in parameters.values()):
        return provider_cls(**init_kwargs)
    allowed = {
        name
        for name, param in parameters.items()
        if param.kind in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
    }
    return provider_cls(**{key: value for key, value in init_kwargs.items() if key in allowed})


def llm_request_timeout(runtime_cfg: dict[str, Any] | None) -> float:
    """模型请求客户端的超时：运行参数 providers.openai.timeout_sec。"""
    return get_float(runtime_cfg or {}, "providers.openai.timeout_sec", 600.0, min_value=5.0, max_value=600.0)


def create_llm_http_client(timeout: float) -> httpx.AsyncClient:
    """发模型请求的 HTTP 客户端。主路径、备选渠道和诊断里的模型连通性检查都从这里拿，代理和 UA 才不会各走各的。

    默认直连；CLONOTH_LLM_TRUST_ENV 打开了才认 HTTP_PROXY 这些系统代理设置。
    """
    return httpx.AsyncClient(
        timeout=timeout,
        trust_env=env_bool("CLONOTH_LLM_TRUST_ENV", False),
        headers={"User-Agent": "Clonoth"},
    )
