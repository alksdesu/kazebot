from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clonoth_runtime import resolve_channel

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


def resolve_provider(
    workspace_root: Path,
    node: Node,
    provider_default: str,
    provider_default_type: str = "openai",
    session_override: dict[str, Any] | None = None,
) -> ResolvedProvider:
    """根据全局、节点和 session 配置解析模型和可选 api_key/base_url。

    provider 字段可以是 config.yaml 里的渠道名，也可以是裸线格式名。命中渠道块时
    连同它的 base_url/api_key/model 一起继承，自己写了的那几项仍然压过它。
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
        channel_options=dict(channel.options),
    )
