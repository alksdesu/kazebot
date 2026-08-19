"""Clonoth SDK 配置。

Phase 1 (2026-04-17): 初始创建。
BotConfig 以 dataclass 注入方式提供 Bot 接入 Clonoth 所需的配置项。
设计理念：配置注入优于继承，避免基类锁定。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BotConfig:
    """Bot 接入 Clonoth 所需的配置。

    Attributes:
        base_url: Supervisor HTTP API 地址，如 "http://127.0.0.1:8765"
        secret: 认证密钥（保留字段，当前 Supervisor 未启用认证）
        entry_node_id: 默认入口节点 ID，如 "ereuna_main"
        conversation_key_prefix: 会话键前缀，如 "discord" 使生成的 key 为 "discord:{channel_id}"
        poll_interval: 事件轮询间隔（秒）
        max_history: 频道历史队列最大长度
        workspace_root: Clonoth 工作区根目录
            可选。留空时可在启动阶段通过 ClonothClient.get_health() 动态获取。
        extra_roots: 信任的外部根路径列表（用于审批路径分类）
    """
    base_url: str
    secret: str | None = None
    entry_node_id: str = ""
    conversation_key_prefix: str = ""
    # [AutoC] 同一适配器可能拥有多个会话前缀（如 QQ 同时有 qq_group 与
    # qq_private）。归属判定除主前缀外，还接受这里列出的附加前缀，避免
    # 私聊触发的审批因前缀不匹配被 SDK 静默丢弃、任务空等到超时。
    extra_conversation_key_prefixes: list[str] = field(default_factory=list)
    poll_interval: float = 0.8
    max_history: int = 50
    workspace_root: Path | None = None
    extra_roots: list[Path] = field(default_factory=list)
    # Phase 3 (2026-04-17): EventRouter 审批处理需要此字段。
    # 提取自 bot_adapter.py _process_approval_event 中 _is_external_operation 后的分支逻辑。
    # False（默认）：所有审批请求都交给适配器展示审批 UI，由人工决定。安全默认值。
    # True：工作区内部操作自动放行，仅外部操作需人工审批（bot_adapter.py 的现有行为）。
    # Bot 侧根据安全需求显式开启。
    auto_approve_internal: bool = False
    # Durable adapter-side outbound recovery.  The optional path preserves the
    # existing constructor API; when omitted EventRouter derives a per-prefix
    # file below data/. Retry values are bounded to avoid a hot failure loop.
    outbound_store_path: Path | None = None
    outbound_retry_initial: float = 2.0
    outbound_retry_max: float = 300.0
    outbound_retry_scan_interval: float = 1.0
    # Cross-instance delivery lease. Heartbeats run while an adapter callback is
    # active, so long platform sends cannot be stolen by another SDK process.
    outbound_lease_seconds: float = 600.0
    outbound_heartbeat_interval: float = 30.0
    # Sent tombstones retain retry dedupe for a conservative window, then their
    # complete event JSON is reclaimed. Non-sent rows are never pruned.
    outbound_sent_ttl: float = 7 * 24 * 3600.0
    outbound_sent_max_rows: int = 50_000
    outbound_prune_interval: float = 300.0
