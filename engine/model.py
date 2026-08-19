from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .node import Node


@dataclass
class ResolvedProvider:
    model: str
    # [provider-registry 2026-05-03] provider_type 是 ProviderRegistry 的 key。
    # 原因：provider 名称已插件化；做法：这里只保存字符串，不列死内置集合；目的：保持模型解析层与具体 provider 解耦。
    provider_type: str = "openai"
    api_key: str | None = None   # None 表示使用全局默认
    base_url: str | None = None  # None 表示使用全局默认


def resolve_provider(
    workspace_root: Path,
    node: Node,
    provider_default: str,
    provider_default_type: str = "openai",
    session_override: dict[str, Any] | None = None,
) -> ResolvedProvider:
    """根据全局、节点和 session 配置解析模型和可选 api_key/base_url。"""
    model = node.model.strip() if node.model else ""
    if not model:
        model = provider_default or "gpt-4o-mini"

    api_key = node.api_key.strip() if node.api_key else None
    base_url = node.base_url.strip() if node.base_url else None

    # 节点没写就跟随全局活跃渠道；两者都空才回落 openai。
    provider_type = (node.provider.strip() if node.provider else "") or provider_default_type or "openai"

    override = session_override if isinstance(session_override, dict) else {}
    # [AutoC 2026-06-01] Why: provider selection now has a session-scoped layer
    # used by the supervisor API. How: compute the existing global→node result
    # first, then overlay non-empty session_override fields for provider/model,
    # api_key, and base_url. Purpose: the priority is session > node > global
    # without changing older resolve_provider call sites.
    override_provider = str(override.get("provider") or override.get("provider_type") or "").strip().lower()
    override_model = str(override.get("model") or "").strip()
    override_api_key = str(override.get("api_key") or "").strip()
    override_base_url = str(override.get("base_url") or "").strip()
    if override_provider:
        provider_type = override_provider
    if override_model:
        model = override_model
    if override_api_key:
        api_key = override_api_key
    if override_base_url:
        base_url = override_base_url

    return ResolvedProvider(
        model=model,
        provider_type=provider_type,
        api_key=api_key or None,
        base_url=base_url or None,
    )
