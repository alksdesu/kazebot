"""Clonoth QQ 接入插件（NoneBot2 + OneBot 11）。

纯对话模式：群成员 @Bot 或用户私聊后，插件把请求提交到 Supervisor，
拿到最终结果再发回对应 QQ 会话。中间回复会展示，工具调用、进度日志、
审批请求和子任务状态不展示。
"""
from __future__ import annotations

import sys

from .config import CLONOTH_WORKSPACE

# clonoth_sdk 随工作区分发而非 pip 安装，加载适配器本体之前先把工作区加进 sys.path。
if CLONOTH_WORKSPACE not in sys.path:
    sys.path.insert(0, CLONOTH_WORKSPACE)

# 子模块挂在入口上：运维脚本和测试按 onebot_plugin.<模块> 访问适配器本体。
from .napcat import (  # noqa: E402,F401  # sys.path 必须先插入工作区。
    common,
    models,
    identity,
    message_text,
    anonymize,
    routing,
    permissions,
    approval,
    history,
    media,
    emotes,
    output_guide,
    inbound,
    queueing,
    sending,
    outbound_attachments,
    proactive,
    commands,
    callbacks,
    forward_bridge,
    lifecycle,
    handlers,
    state,
)
