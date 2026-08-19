"""TUI 数据模型与 Textual 自定义消息。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from textual.message import Message


# ---------------------------------------------------------------------------
#  数据模型
# ---------------------------------------------------------------------------

@dataclass
class ChatMessage:
    role: str  # "user" | "assistant" | "system"
    content: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_streaming: bool = False


@dataclass
class NodeStatus:
    node_id: str
    name: str
    status: str = "running"  # running | completed | failed
    summary: str = ""


# ---------------------------------------------------------------------------
#  Textual 自定义消息（EventPoller → Widget 事件分发）
# ---------------------------------------------------------------------------

class StreamText(Message):
    """流式文本片段。"""
    def __init__(self, content: str) -> None:
        super().__init__()
        self.content = content


class StreamThinking(Message):
    """流式 thinking 片段。"""
    def __init__(self, content: str) -> None:
        super().__init__()
        self.content = content


class StreamEnd(Message):
    """流式输出结束。"""


class AssistantReply(Message):
    """完整的 assistant 回复（非流式 outbound_message）。"""
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class NodeStarted(Message):
    def __init__(self, name: str, node_id: str) -> None:
        super().__init__()
        self.name = name
        self.node_id = node_id


class NodeCompleted(Message):
    def __init__(self, name: str, outcome: str, summary: str) -> None:
        super().__init__()
        self.name = name
        self.outcome = outcome
        self.summary = summary


class ApprovalRequested(Message):
    def __init__(self, approval_id: str, operation: str, details: dict[str, Any], fingerprint: str) -> None:
        super().__init__()
        self.approval_id = approval_id
        self.operation = operation
        self.details = details
        self.fingerprint = fingerprint


class TaskCancelled(Message):
    """任务已取消。"""


class ToolActivity(Message):
    """工具执行进度（handoff_progress）。"""
    def __init__(self, message: str, tool_name: str = "", node_id: str = "") -> None:
        super().__init__()
        self.message = message
        self.tool_name = tool_name
        self.node_id = node_id


class UserSubmit(Message):
    """用户在 InputBox 中提交了文本。"""
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class SlashCommand(Message):
    """用户输入了斜杠命令。"""
    def __init__(self, command: str, args: str = "") -> None:
        super().__init__()
        self.command = command
        self.args = args


class ConfigUpdated(Message):
    """配置已变更（如模型切换、引擎重启）。"""
    def __init__(self, event_type: str = "") -> None:
        super().__init__()
        self.event_type = event_type
