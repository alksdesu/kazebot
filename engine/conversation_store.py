"""Session-level conversation store.

Phase 0 + Phase 1 of the Session Conversation Store design.
Provides a typed Message model and a JSONL-backed ConversationStore
that serves as the session-level single source of truth for conversation history.

See: data/session_conversation_store_design.md
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
#  MessageType: 消息细分类型常量
#  与 LLM API 的 role 分离，用于去重、剥离、格式转换等内部逻辑。
#  例如 role=user + message_type=tool_result 表示工具结果而非真实用户输入。
# ---------------------------------------------------------------------------

class MessageType:
    USER_INPUT = "user_input"
    ASSISTANT = "assistant"
    TOOL_RESULT = "tool_result"
    SYSTEM = "system"
    SUMMARY = "summary"


# ---------------------------------------------------------------------------
#  Message: 标准化消息对象，替代裸 dict
#  所有消息（user/assistant/system/tool_result）统一用此类表示。
#  to_dict / from_dict 实现 JSONL 序列化/反序列化。
# ---------------------------------------------------------------------------

@dataclass
class Message:
    id: str                          # UUID，全局唯一
    # [2026-05-01] role 增加 tool。
    # 原因：真 native formatter 会生成 role=tool 消息；存储层必须原样保存，
    # 目的：下一轮恢复历史时仍能满足原生工具调用的 tool_call_id 配对要求。
    role: str                        # user / assistant / system / tool
    # [AutoC 2026-06-01] Why: task-local image retention requires persisted
    # user messages to keep provider-style multimodal blocks. How: allow either
    # plain text or a list of content-part dictionaries. Purpose: JSONL history
    # can round-trip images for the active task while runner strips them for
    # cross-task reloads.
    content: str | list[dict[str, Any]]  # 消息文本内容或多模态内容块
    message_type: str = ""           # MessageType 常量，细分类型
    created_at: str = ""             # ISO 8601 时间戳
    meta: dict[str, Any] = field(default_factory=dict)  # 元数据（provider, tool_mode 等），用 dict 存储不依赖 MessageMeta 类
    source_node_id: str = ""         # 产生该消息的节点 ID
    source_task_id: str = ""         # 产生该消息的 task ID
    ephemeral: bool = False          # 临时消息（dynamic context 等），不持久化到 store
    tool_calls: list = field(default_factory=list)  # assistant 消息的工具调用列表
    # [2026-05-01] 新增 role=tool 的原生工具结果字段。
    # 怎么改：把 tool_call_id 与 name 作为 Message 的一等字段参与 JSONL 序列化。
    # 目的：ConversationStore 读取后可以还原 OpenAI/Responses 等原生工具历史。
    tool_call_id: str = ""           # role=tool 消息关联的 assistant.tool_calls[].id
    name: str = ""                   # role=tool 消息关联的工具名

    def to_dict(self) -> dict[str, Any]:
        """序列化为存储格式。仅包含非空字段以减小 JSONL 体积。"""
        # [AutoC 2026-06-01] Why: list content is already JSON-serializable and
        # must not be coerced to text before storage. How: assign self.content
        # unchanged to the dict. Purpose: from_dict can restore multimodal user
        # input for same-task history reconstruction.
        d: dict[str, Any] = {"id": self.id, "role": self.role, "content": self.content}
        if self.message_type:
            d["message_type"] = self.message_type
        if self.created_at:
            d["created_at"] = self.created_at
        if self.meta:
            d["meta"] = self.meta
        if self.source_node_id:
            d["source_node_id"] = self.source_node_id
        if self.source_task_id:
            d["source_task_id"] = self.source_task_id
        if self.ephemeral:
            d["ephemeral"] = True
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        # [2026-05-01] 持久化原生 role=tool 结果的配对字段。
        # 原因：只保存 role/content 会丢失 tool_call_id，下一轮发送给 LLM 时无法配对。
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.name:
            d["name"] = self.name
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        """从 JSONL 行反序列化。对缺失字段提供安全默认值。"""
        return cls(
            id=data.get("id", ""),
            role=data.get("role", "user"),
            # [AutoC 2026-06-01] Why: stored multimodal content must survive a
            # JSONL reload. How: pass through str or list content unchanged;
            # invalid/missing content still falls back to an empty string via
            # data.get. Purpose: Message remains compatible with legacy text
            # records and new attachment records.
            content=data.get("content", ""),
            message_type=data.get("message_type", ""),
            created_at=data.get("created_at", ""),
            meta=data.get("meta", {}),
            source_node_id=data.get("source_node_id", ""),
            source_task_id=data.get("source_task_id", ""),
            ephemeral=data.get("ephemeral", False),
            tool_calls=data.get("tool_calls", []),
            # [2026-05-01] 反序列化 role=tool 的配对字段。
            # 目的：从 JSONL 恢复的历史与运行时内存消息保持同一结构。
            tool_call_id=str(data.get("tool_call_id") or ""),
            name=str(data.get("name") or ""),
        )


# ---------------------------------------------------------------------------
#  ConversationStore: Session 级别的 JSONL 对话存储
#  每个 session 一个 .jsonl 文件，追加写入，不覆盖。
#  带内存缓存，cache miss 时从文件加载。
# ---------------------------------------------------------------------------

class ConversationStore:
    """Session-level conversation store backed by JSONL files.

    Storage layout:  {data_dir}/{session_id}.jsonl
    Each line is one Message serialized as JSON.
    """

    def __init__(self, data_dir: str | Path):
        self._data_dir = Path(data_dir)
        # 确保存储目录存在
        self._data_dir.mkdir(parents=True, exist_ok=True)
        # session_id -> list[Message] 的内存缓存
        self._cache: dict[str, list[Message]] = {}
        # 缓存对应的文件指纹 (mtime_ns, size)，用于发现别的进程改过这个文件
        self._cache_stamps: dict[str, tuple[int, int]] = {}

    # ── 写入 ──

    def append(self, session_id: str, message: Message) -> None:
        """追加一条消息到 session 的 JSONL 文件。

        ephemeral 消息跳过写入（不持久化）。
        写入后同步更新内存缓存（如果已加载）。
        """
        if message.ephemeral:
            return
        path = self._data_dir / f"{session_id}.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(message.to_dict(), ensure_ascii=False) + "\n")
        # 如果缓存中有该 session，同步追加。指纹要跟着更新，否则自己刚写的这一条
        # 会让下一次 load 认为文件被别人改了而整文件重读。
        if session_id in self._cache:
            self._cache[session_id].append(message)
            self._cache_stamps[session_id] = self._file_stamp(path)

    def append_batch(self, session_id: str, messages: list[Message]) -> None:
        """批量追加（一次文件 I/O）。写入后使缓存失效以保证一致性。"""
        path = self._data_dir / f"{session_id}.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            for msg in messages:
                if not msg.ephemeral:
                    f.write(json.dumps(msg.to_dict(), ensure_ascii=False) + "\n")
        # 批量写入后使缓存失效，下次 load 时重新从文件读取
        self.invalidate_cache(session_id)

    # ── 读取 ──

    def _file_stamp(self, path: Path) -> tuple[int, int]:
        """文件指纹；不存在时返回 (0, 0)。"""
        try:
            st = path.stat()
        except OSError:
            return (0, 0)
        return (st.st_mtime_ns, st.st_size)

    def load(self, session_id: str) -> list[Message]:
        """加载 session 的全部消息。缓存命中前先确认文件没被别的进程改过。

        supervisor 的后台压缩和 engine worker 的 L2 snip 各持一个 store 实例，改的是
        同一个文件。只按「缓存里有没有」判断，engine 就会拿压缩前的快照去 replace_all，
        把 supervisor 刚写进去的摘要整段覆盖回去 —— 那次 compactor 调用白花，历史回到
        压缩前，下一轮又超阈值。一次 stat 换掉这个。
        """
        path = self._data_dir / f"{session_id}.jsonl"
        stamp = self._file_stamp(path)
        cached = self._cache.get(session_id)
        if cached is not None and self._cache_stamps.get(session_id) == stamp:
            return cached
        messages: list[Message] = []
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            messages.append(Message.from_dict(json.loads(line)))
                        except Exception:
                            pass  # 跳过损坏行，不中断加载
        self._cache[session_id] = messages
        self._cache_stamps[session_id] = self._file_stamp(path)
        return messages

    # ── 清理 ──

    def delete(self, session_id: str) -> None:
        """删除 session 的对话存储文件和缓存。用于 context_reset reason=clear。"""
        path = self._data_dir / f"{session_id}.jsonl"
        if path.exists():
            path.unlink()
        self.invalidate_cache(session_id)

    def replace_all(self, session_id: str, messages: list[Message]) -> None:
        """原子替换 session 的全部消息。

        Step 2（2026-04-16）修复 compact：主节点/child session 切到 ConversationStore
        后，压缩需要用 summary 消息替换旧消息，而不是追加。此方法以 delete + append_batch
        实现。不是真正的文件级 atomic，但 compact 结果写入失败时下次还能重试。
        """
        self.delete(session_id)
        if messages:
            self.append_batch(session_id, messages)

    # ── 查询 ──

    def message_count(self, session_id: str) -> int:
        """返回 session 中的消息总数。"""
        return len(self.load(session_id))

    def fork(self, source_session_id: str, target_session_id: str) -> int:
        """将 source session 的非 system 消息复制到 target session。

        用于 context_mode=fork 场景：子节点从父 session 复制历史后独立演化。
        target session 必须是空的（新创建的），已有内容则跳过，防止重复复制。
        返回实际复制的消息数量。
        """
        if self.message_count(target_session_id) > 0:
            return 0  # target 已有内容，不重复复制
        source_msgs = self.load(source_session_id)
        if not source_msgs:
            return 0
        # 过滤 system 消息——子节点有自己的 system prompt，不需要父节点的
        non_system = [m for m in source_msgs if m.role != "system"]
        if non_system:
            self.append_batch(target_session_id, non_system)
        return len(non_system)

    def invalidate_cache(self, session_id: str) -> None:
        """手动使缓存失效，下次 load 时强制从文件重新读取。"""
        self._cache.pop(session_id, None)
        self._cache_stamps.pop(session_id, None)
