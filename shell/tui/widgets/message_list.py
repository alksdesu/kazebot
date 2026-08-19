"""对话消息列表。"""
from __future__ import annotations

from textual.containers import ScrollableContainer
from textual.widgets import Static, Markdown


class MessageList(ScrollableContainer):
    """可滚动的消息列表。"""

    DEFAULT_CSS = """
    MessageList {
        height: 1fr;
        scrollbar-size: 1 1;
    }
    .user-msg {
        color: $text;
        background: $primary-darken-3;
        margin: 1 8 0 2;
        padding: 0 1;
    }
    .user-prefix {
        color: dodgerblue;
        text-style: bold;
        margin: 1 8 0 2;
        height: 1;
    }
    .assistant-msg {
        color: $text;
        margin: 0 2 0 2;
        padding: 0 1;
    }
    .assistant-md {
        margin: 0 2 0 2;
        padding: 0 1;
    }
    .assistant-prefix {
        color: green;
        text-style: bold;
        margin: 1 2 0 2;
        height: 1;
    }
    .system-msg {
        color: $text-muted;
        text-style: italic;
        margin: 0 4;
        height: auto;
    }
    .tool-msg {
        margin: 0 4;
        height: auto;
    }
    .tool-msg-start {
        color: $text-muted;
        margin: 0 4;
        height: auto;
    }
    .tool-msg-done {
        color: $success;
        margin: 0 4;
        height: auto;
    }
    .divider {
        color: $text-disabled;
        margin: 0 2;
        height: 1;
    }
    .thinking-msg {
        color: $text-muted;
        text-style: italic;
        margin: 0 4;
        height: auto;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(id="message-list", **kwargs)
        self._streaming_widget: Static | None = None
        self._stream_text: str = ""
        self._auto_scroll: bool = True

    # ---- 判断内容是否含 Markdown 格式 ----

    @staticmethod
    def _has_markdown(text: str) -> bool:
        """简单检测文本中是否含有 Markdown 格式标记。"""
        indicators = (
            "```",     # 代码块
            "**",      # 粗体
            "# ",      # 标题
            "## ",
            "- ",      # 列表
            "* ",
            "1. ",     # 有序列表
            "| ",      # 表格
            "> ",      # 引用
        )
        return any(ind in text for ind in indicators)

    # ---- 追加消息 ----

    async def add_user_message(self, text: str) -> None:
        await self.mount(Static("You", classes="user-prefix"))
        await self.mount(Static(text, classes="user-msg"))
        self._scroll_to_end()

    async def add_assistant_message(self, text: str) -> None:
        await self.mount(Static("Assistant", classes="assistant-prefix"))
        if self._has_markdown(text):
            await self.mount(Markdown(text, classes="assistant-md"))
        else:
            await self.mount(Static(text, classes="assistant-msg"))
        await self.mount(Static("─" * 40, classes="divider"))
        self._scroll_to_end()

    async def add_system_message(self, text: str) -> None:
        await self.mount(Static(f"  {text}", classes="system-msg"))
        self._scroll_to_end()

    async def add_thinking_message(self, text: str) -> None:
        await self.mount(Static(f"  💭 {text}", classes="thinking-msg"))
        self._scroll_to_end()

    async def add_tool_message(self, text: str, tool_name: str = "") -> None:
        """展示工具执行进度。

        根据内容自动判断是'开始'还是'结果'：
        - 含 summary（如 'read_file: 已读取 xxx'）→ 结果样式
        - 其他（如 '执行 2 个工具' / '开始执行 read_file'）→ 开始样式
        """
        is_result = ":" in text and not text.startswith("执行")
        if is_result:
            parts = text.split(":", 1)
            name = parts[0].strip()
            summary = parts[1].strip() if len(parts) > 1 else ""
            await self.mount(Static(f"  ✓ {name} → {summary}", classes="tool-msg-done"))
        else:
            await self.mount(Static(f"  ⟳ {text}", classes="tool-msg-start"))
        self._scroll_to_end()

    # ---- 流式 ----

    async def start_streaming(self) -> None:
        self._stream_text = ""
        await self.mount(Static("Assistant", classes="assistant-prefix"))
        # 流式期间用 Static（Markdown 高频 update 会闪烁）
        self._streaming_widget = Static("", classes="assistant-msg")
        await self.mount(self._streaming_widget)
        self._scroll_to_end()

    def append_stream(self, text: str) -> None:
        if self._streaming_widget is None:
            return
        self._stream_text += text
        self._streaming_widget.update(self._stream_text)
        self._scroll_to_end()

    async def end_streaming(self) -> None:
        """流结束：如果内容含 Markdown，替换为 Markdown 组件渲染。"""
        if self._streaming_widget is not None:
            final_text = self._stream_text.strip()
            if final_text and self._has_markdown(final_text):
                # 替换 Static 为 Markdown
                md_widget = Markdown(final_text, classes="assistant-md")
                await self.mount(md_widget, after=self._streaming_widget)
                self._streaming_widget.remove()
            await self.mount(Static("─" * 40, classes="divider"))
        self._streaming_widget = None
        self._stream_text = ""
        self._scroll_to_end()

    @property
    def is_streaming(self) -> bool:
        return self._streaming_widget is not None

    # ---- 搜索 ----

    def get_all_text(self) -> list[tuple, str]:
        results = []
        for child in self.children:
            if isinstance(child, Static):
                results.append((child, str(child.renderable or "")))
            elif isinstance(child, Markdown):
                # Markdown 组件内部存储原始文本
                raw = getattr(child, '_markdown', '') or ''
                results.append((child, raw))
        return results

    # ---- 滚动 ----

    def _scroll_to_end(self) -> None:
        if self._auto_scroll:
            self.scroll_end(animate=False)
