"""Clonoth TUI 应用主入口。

Textual 8.x 的 push_screen 不可用，全部组件通过 compose 挂载，
用 CSS class 控制显示/隐藏。
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll, Center
from textual.widgets import Static, Button, Label

from .styles import DEFAULT_CSS
from .services.supervisor_client import SupervisorClient
from .services.event_poller import EventPoller
from .services.node_scanner import scan_nodes, find_root_nodes, NodeInfo
from .widgets.message_list import MessageList
from .widgets.input_box import InputBox
from .widgets.status_bar import StatusBar
from .widgets.thinking import ThinkingIndicator
from .widgets.search_bar import SearchBar
from .models import (
    ApprovalRequested,
    AssistantReply,
    ConfigUpdated,
    ToolActivity,
    NodeCompleted,
    NodeStarted,
    SlashCommand,
    StreamEnd,
    StreamText,
    StreamThinking,
    TaskCancelled,
    UserSubmit,
)


class ClonothApp(App):

    TITLE = "Clonoth"
    CSS = DEFAULT_CSS + """
    #welcome-overlay {
        align: center middle;
        width: 100%;
        height: 100%;
        layer: overlay;
    }
    #welcome-overlay.hidden { display: none; }
    #welcome-box {
        width: 64;
        height: auto;
        max-height: 24;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    #welcome-title {
        text-style: bold;
        text-align: center;
        width: 100%;
        margin-bottom: 1;
    }
    #welcome-subtitle {
        color: $text-muted;
        text-align: center;
        width: 100%;
        margin-bottom: 1;
    }
    .node-btn { width: 100%; margin-bottom: 1; }
    .node-desc { color: $text-muted; text-style: italic; margin: 0 2 1 2; height: auto; }

    #approval-overlay {
        align: center middle;
        width: 100%; height: 100%;
        layer: overlay;
        display: none;
    }
    #approval-overlay.visible { display: block; }
    #approval-box {
        width: 70; height: auto; max-height: 30;
        border: thick $warning; background: $surface; padding: 1 2;
    }
    #approval-title { text-style: bold; color: $warning; margin-bottom: 1; }
    #approval-scroll { max-height: 20; margin-bottom: 1; }
    #approval-info { height: auto; }
    """

    # Ctrl+Q 和 Ctrl+C 在 VS Code 终端中被拦截，用 Ctrl+D 退出
    BINDINGS = [
        Binding("ctrl+d", "quit", "退出", priority=True),
        Binding("ctrl+l", "clear_chat", "新对话"),
        Binding("ctrl+a", "toggle_approval_mode", "审批模式"),
        Binding("ctrl+f", "toggle_search", "搜索"),
    ]

    def __init__(
        self, *,
        supervisor_url: str = "http://127.0.0.1:8765",
        conversation_key: str | None = None,
        workspace_root: Path | None = None,
    ) -> None:
        super().__init__()
        self._supervisor_url = supervisor_url
        self.workspace_root = workspace_root or Path(__file__).resolve().parents[2]
        self.conversation_key = conversation_key or f"cli:{uuid.uuid4()}"
        self.session_id: str | None = None
        self.approval_mode: str = "normal"
        self.entry_node_id: str | None = None
        self._entry_node_name: str = ""
        self._chat_ready = False
        self._root_nodes: list[NodeInfo] = []
        self._pending_approval: dict[str, Any] | None = None
        self._thinking_buf: str = ""  # 累积 thinking 文本
        self._streamed_this_turn: bool = False  # 本轮是否已通过流式输出

        self.supervisor = SupervisorClient(supervisor_url)
        self.poller = EventPoller(self.supervisor, self)

    # ---- compose ----

    def compose(self) -> ComposeResult:
        all_nodes = scan_nodes(self.workspace_root)
        self._root_nodes = find_root_nodes(all_nodes)

        yield SearchBar()
        yield MessageList()
        yield ThinkingIndicator()
        yield InputBox()
        yield StatusBar()

        if len(self._root_nodes) > 1:
            with Center(id="welcome-overlay"):
                with Vertical(id="welcome-box"):
                    yield Label("Clonoth", id="welcome-title")
                    yield Static("选择入口节点", id="welcome-subtitle")
                    for node in self._root_nodes:
                        safe_id = node.id.replace(".", "_")
                        yield Button(
                            node.name,
                            id=f"node-{safe_id}",
                            classes="node-btn",
                        )
                        if node.description:
                            yield Static(node.description, classes="node-desc")

        with Center(id="approval-overlay"):
            with Vertical(id="approval-box"):
                yield Label("⚠ 审批请求", id="approval-title")
                with VerticalScroll(id="approval-scroll"):
                    yield Static("", id="approval-info")
                yield Button("允许 (Y)", variant="warning", id="btn-allow")
                yield Button("拒绝 (N)", variant="error", id="btn-deny")

    # ---- 启动 ----

    async def on_mount(self) -> None:
        try:
            await self.supervisor.wait_ready(timeout=15.0)
        except TimeoutError:
            self.notify("无法连接 Supervisor", severity="error", timeout=10)
            return
        except Exception:
            self.notify("连接 Supervisor 出错", severity="error", timeout=10)
            return

        # 读取当前模型名
        try:
            self._model_name = await self.supervisor.fetch_model_name()
        except Exception:
            self._model_name = ""

        global_seq = await self.supervisor.fetch_latest_global_seq()
        self.poller.start(global_seq=global_seq)

        if len(self._root_nodes) <= 1:
            if self._root_nodes:
                n = self._root_nodes[0]
                self.entry_node_id = n.id
                self._entry_node_name = n.name
            else:
                self.entry_node_id = "bootstrap.shell_orchestrator"
                self._entry_node_name = "默认入口"
            self._activate_chat()

    async def on_unmount(self) -> None:
        self.poller.stop()
        await self.supervisor.close()

    def _activate_chat(self) -> None:
        self.query_one(StatusBar).update_status(
            connected=True,
            node_name=self._entry_node_name,
            model=getattr(self, '_model_name', ''),
        )
        self.query_one(InputBox).focus()
        self._chat_ready = True
        self.call_later(self._show_welcome)

    async def _show_welcome(self) -> None:
        ml = self.query_one(MessageList)
        await ml.add_system_message(f"已连接 · 入口: {self._entry_node_name}")
        await ml.add_system_message(
            "输入消息开始对话 · /help 查看命令 · /exit 或 Ctrl+D 退出"
        )

    # ---- 节点选择 ----

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""

        if btn_id.startswith("node-"):
            safe_id = btn_id[5:]
            node_id = safe_id.replace("_", ".")
            node_name = safe_id
            for n in self._root_nodes:
                if n.id.replace(".", "_") == safe_id:
                    node_id = n.id
                    node_name = n.name
                    break
            self.entry_node_id = node_id
            self._entry_node_name = node_name
            try:
                self.query_one("#welcome-overlay").add_class("hidden")
            except Exception:
                pass
            self._activate_chat()
            return

        if btn_id == "btn-allow":
            self._resolve_approval("allow")
        elif btn_id == "btn-deny":
            self._resolve_approval("deny")

    def _resolve_approval(self, decision: str) -> None:
        if self._pending_approval:
            aid = self._pending_approval["approval_id"]
            op = self._pending_approval["operation"]
            self._pending_approval = None
            try:
                self.query_one("#approval-overlay").remove_class("visible")
            except Exception:
                pass
            self.call_later(self._do_approval, aid, decision, op)

    async def _do_approval(self, aid: str, decision: str, operation: str) -> None:
        await self.supervisor.decide_approval(aid, decision)
        try:
            await self.query_one(MessageList).add_system_message(
                f"审批: {decision} ({operation})"
            )
        except Exception:
            pass

    # ---- 用户提交 ----

    async def on_user_submit(self, event: UserSubmit) -> None:
        if not self._chat_ready:
            return
        ml = self.query_one(MessageList)
        sb = self.query_one(StatusBar)
        await ml.add_user_message(event.text)
        sb.update_status(waiting=True)
        self._streamed_this_turn = False
        try:
            sid = await self.supervisor.send_message(
                conversation_key=self.conversation_key,
                text=event.text,
                entry_node_id=self.entry_node_id,
            )
            if self.session_id != sid:
                self.session_id = sid
                self.poller.start_session(sid)
            # 同一 session 不重启 poller，已有轮询会自动获取新事件
        except Exception as e:
            await ml.add_system_message(f"发送失败: {e}")
            sb.update_status(waiting=False)

    # ---- 斜杠命令 ----

    async def on_slash_command(self, event: SlashCommand) -> None:
        if not self._chat_ready:
            return
        ml = self.query_one(MessageList)
        sb = self.query_one(StatusBar)
        cmd = event.command

        if cmd == "/clear":
            self.conversation_key = f"cli:{uuid.uuid4()}"
            self.session_id = None
            self.poller.stop_session()
            for child in list(ml.children):
                child.remove()
            await ml.add_system_message("上下文已清空")
            sb.update_status(waiting=False)
        elif cmd == "/cancel":
            if self.session_id:
                ok = await self.supervisor.cancel_session(self.session_id)
                await ml.add_system_message("已发送取消" if ok else "取消失败")
            else:
                await ml.add_system_message("无活跃会话")
        elif cmd == "/help":
            await ml.add_system_message(
                "/clear 新对话 · /cancel 取消 · /restart 重启 · /auto /normal 审批模式 · /exit 退出"
            )
            await ml.add_system_message(
                "Ctrl+D 退出 · Ctrl+L 新对话 · Ctrl+A 切换审批 · Ctrl+F 搜索"
            )
        elif cmd == "/auto":
            self.approval_mode = "auto"
            sb.update_status(mode="auto")
            await ml.add_system_message("已切换到自动审批")
        elif cmd == "/normal":
            self.approval_mode = "normal"
            sb.update_status(mode="normal")
            await ml.add_system_message("已切换到手动审批")
        elif cmd == "/exit":
            self.exit()
        elif cmd == "/restart":
            await ml.add_system_message("正在重启程序...")
            ok = await self.supervisor.restart(target="all", reason="TUI /restart")
            if ok:
                # 干净退出 TUI，让 Textual 还原终端状态
                # supervisor 的 _watch_shell 检测到退出后会执行实际重启
                self.exit()
            else:
                await ml.add_system_message("重启失败，请检查 supervisor 状态")
        elif cmd in ("/approve", "/deny"):
            aid = event.args.strip()
            if aid:
                d = "allow" if cmd == "/approve" else "deny"
                try:
                    await self.supervisor.decide_approval(aid, d)
                    await ml.add_system_message(f"审批: {d}")
                except Exception as e:
                    await ml.add_system_message(f"审批失败: {e}")
            else:
                await ml.add_system_message(f"用法: {cmd} <id>")

    # ---- 工具执行进度 ----

    async def on_tool_activity(self, event: ToolActivity) -> None:
        if not self._chat_ready:
            return
        ml = self.query_one(MessageList)
        await ml.add_tool_message(event.message, tool_name=event.tool_name)

    # ---- 流式 ----

    async def on_stream_text(self, event: StreamText) -> None:
        if not self._chat_ready:
            return
        self._streamed_this_turn = True
        ml = self.query_one(MessageList)
        if not ml.is_streaming:
            await ml.start_streaming()
        ml.append_stream(event.content)

    async def on_stream_thinking(self, event: StreamThinking) -> None:
        if not self._chat_ready:
            return
        self._thinking_buf += event.content
        self.query_one(ThinkingIndicator).show_thinking(event.content)

    async def on_stream_end(self, event: StreamEnd) -> None:
        if not self._chat_ready:
            return
        # 将累积的 thinking 内容写入聊天流（CC 风格）
        if self._thinking_buf.strip():
            ml = self.query_one(MessageList)
            # 截取前 500 字符，避免过长
            preview = self._thinking_buf.strip()[:500]
            if len(self._thinking_buf.strip()) > 500:
                preview += "…"
            await ml.add_thinking_message(preview)
        self._thinking_buf = ""
        await self.query_one(MessageList).end_streaming()
        self.query_one(ThinkingIndicator).hide()
        self.query_one(StatusBar).update_status(waiting=False)

    async def on_assistant_reply(self, event: AssistantReply) -> None:
        if not self._chat_ready:
            return
        ml = self.query_one(MessageList)
        # 如果本轮已有流式输出，outbound_message 是重复内容，跳过
        if not ml.is_streaming and not self._streamed_this_turn:
            await ml.add_assistant_message(event.text)
        self.query_one(StatusBar).update_status(waiting=False)

    # ---- 节点事件 ----

    async def on_node_started(self, event: NodeStarted) -> None:
        if not self._chat_ready:
            return
        self.query_one(StatusBar).update_status(waiting=True, node_name=event.name)
        # 如果不是入口节点，在聊天区显示委派信息
        if event.node_id != self.entry_node_id:
            ml = self.query_one(MessageList)
            await ml.add_system_message(f"▶ 委派 → {event.name}")

    async def on_node_completed(self, event: NodeCompleted) -> None:
        if not self._chat_ready:
            return
        # 恢复入口节点名
        self.query_one(StatusBar).update_status(node_name=self._entry_node_name)

    # ---- 审批 ----

    async def on_approval_requested(self, event: ApprovalRequested) -> None:
        if not self._chat_ready:
            return
        if self.approval_mode == "auto":
            await self.supervisor.decide_approval(event.approval_id, "allow")
            await self.query_one(MessageList).add_system_message(
                f"自动批准: {event.operation}"
            )
            return
        self._pending_approval = {
            "approval_id": event.approval_id,
            "operation": event.operation,
        }
        import json
        info = f"操作: {event.operation}\n指纹: {event.fingerprint}\n{json.dumps(event.details, ensure_ascii=False, indent=2)[:400]}"
        try:
            self.query_one("#approval-info", Static).update(info)
            self.query_one("#approval-overlay").add_class("visible")
        except Exception:
            pass

    async def on_task_cancelled(self, event: TaskCancelled) -> None:
        if not self._chat_ready:
            return
        await self.query_one(MessageList).add_system_message("任务已取消")
        self.query_one(StatusBar).update_status(waiting=False)

    # ---- 配置变更 ----

    async def on_config_updated(self, event: ConfigUpdated) -> None:
        """配置变更时刷新状态栏显示。"""
        try:
            model = await self.supervisor.fetch_model_name()
            if model:
                self._model_name = model
                self.query_one(StatusBar).update_status(model=model)
        except Exception:
            pass

    # ---- 搜索 ----

    async def on_search_query(self, event: Any) -> None:
        if not self._chat_ready:
            return
        ml = self.query_one(MessageList)
        matches = [w for w, t in ml.get_all_text() if event.query.lower() in t.lower()]
        if matches:
            matches[0].scroll_visible()
            await ml.add_system_message(f"找到 {len(matches)} 条匹配")
        else:
            await ml.add_system_message("未找到")

    async def on_search_close(self, event: Any) -> None:
        if self._chat_ready:
            try:
                self.query_one(InputBox).focus()
            except Exception:
                pass

    # ---- 全局动作 ----

    async def action_cancel_task(self) -> None:
        if self.session_id:
            await self.supervisor.cancel_session(self.session_id)

    async def action_clear_chat(self) -> None:
        self.conversation_key = f"cli:{uuid.uuid4()}"
        self.session_id = None
        self.poller.stop_session()
        if not self._chat_ready:
            return
        try:
            ml = self.query_one(MessageList)
            for c in list(ml.children):
                c.remove()
        except Exception:
            pass

    async def action_toggle_approval_mode(self) -> None:
        self.approval_mode = "auto" if self.approval_mode == "normal" else "normal"
        if not self._chat_ready:
            return
        try:
            self.query_one(StatusBar).update_status(mode=self.approval_mode)
            await self.query_one(MessageList).add_system_message(
                f"审批模式: {self.approval_mode}"
            )
        except Exception:
            pass

    def action_toggle_search(self) -> None:
        if not self._chat_ready:
            return
        try:
            sb = self.query_one(SearchBar)
            sb.hide() if sb.has_class("visible") else sb.show()
        except Exception:
            pass
