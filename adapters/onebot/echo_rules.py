"""QQ 这一侧的复读边界：CQ 码带着指向，渲染出来的占位符不是内容。"""
from __future__ import annotations

import re

from adapters._shared.echo_policy import EchoRules

QQ_ECHO_RULES = EchoRules(
    unsafe_markers=("[CQ:at", "[CQ:reply", "[CQ:json", "[CQ:xml", "[CQ:file", "[CQ:forward"),
    placeholder=re.compile(r"^\[(图片|表情包|QQ表情|语音|视频|合并转发|文件)\]$"),
)
