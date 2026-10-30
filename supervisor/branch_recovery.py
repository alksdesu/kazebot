"""supervisor 重启时还没收束的入口分支：补上缺结果的工具调用和一句说明，合并回父会话再删，不再整段丢掉。

合并走正常收束的同一套：父会话写锁、按消息 id 去重、转写一并合并；只是没有任务可以挂靠，也不再路由。
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from engine.branch_history import branch_base_count
from engine.conversation_store import ConversationStore, Message, MessageType
from engine.interrupted_calls import append_interrupted_results

from .child_retention import AGENT_KEY_PREFIX
from .task_router import BranchFinalizePlan

if TYPE_CHECKING:
    from .state import SupervisorState

log = logging.getLogger(__name__)

RESTART_MARK_KEY = "restart_interrupted"
RESTART_HISTORY_NOTICE = (
    "[SYSTEM] 服务重启打断了这一轮任务，上面是重启前已经做完的部分，没来得及写结果的工具调用标成了「中断，结果未知」。"
    "用户要接着做时，先核对实际状态（重新读相关文件、查看命令的产物），再继续没做完的部分。"
)
RESTART_USER_NOTICE = "服务重启打断了刚才的任务，已经做的部分都保留着，说「继续」就接着做。"
RESTART_ABANDONED_DISPATCH = "服务重启打断了这一轮，交回的委派没有派出。"


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def merge_base(
    branch_id: str, entry: dict[str, Any], branch_messages: Sequence[Message], parent_messages: Sequence[Message],
) -> int | None:
    """分支从第几条往后是这一轮新写的；认不出来给 None。

    依次看：分支里的合并边界标记，登记表记的 fork 条数（用最后一条的 id 核对），fork 时最后一条的 id，
    最后才按开头那段在父会话里也有的消息估。
    """
    marked = branch_base_count(list(branch_messages), branch_id, -1)
    if marked >= 0:
        return marked
    last_id = str(entry.get("base_last_id") or "").strip()
    recorded = _as_int(entry.get("base_count"))
    if recorded is not None and 0 <= recorded <= len(branch_messages):
        if recorded == 0 or not last_id or str(branch_messages[recorded - 1].id or "") == last_id:
            return recorded
    if last_id:
        for index, message in enumerate(branch_messages):
            if str(message.id or "") == last_id:
                return index + 1
    parent_ids = {str(message.id or "") for message in parent_messages}
    leading = 0
    for message in branch_messages:
        message_id = str(message.id or "")
        if not message_id or message_id not in parent_ids:
            break
        leading += 1
    # 父会话有内容、分支开头却一条都对不上：多半是父会话 fork 之后被改写过，按 0 合并会把旧历史整段再贴一遍
    if leading == 0 and parent_messages:
        return None
    return leading


def _append_history_notice(store: ConversationStore, branch_id: str) -> None:
    """分支末尾补一句说明，合并后模型下一轮看得到这一段是被重启打断的。补过就不再补。"""
    messages = store.load(branch_id)
    if any(isinstance(message.meta, dict) and message.meta.get(RESTART_MARK_KEY) == branch_id for message in messages):
        return
    source = next((message for message in reversed(messages) if message.source_task_id), None)
    store.append_batch(branch_id, [Message(
        id=str(uuid.uuid4()),
        role="user",
        content=RESTART_HISTORY_NOTICE,
        message_type=MessageType.SYSTEM,
        created_at=datetime.now(timezone.utc).isoformat(),
        meta={"message_type": MessageType.SYSTEM, RESTART_MARK_KEY: branch_id},
        source_node_id=source.source_node_id if source is not None else "",
        source_task_id=source.source_task_id if source is not None else "",
    )])


def _notify(state: "SupervisorState", parent_session_id: str, branch_id: str) -> None:
    """用户会话里发一句「重启打断了任务，说继续接着做」。异步委派的 agent: 会话没人看，不发。"""
    with state._lock:
        info = state.sessions.get(parent_session_id)
        if info is None or str(info.conversation_key or "").startswith(AGENT_KEY_PREFIX):
            return
    try:
        state.append_outbound_message(
            session_id=parent_session_id,
            text=RESTART_USER_NOTICE,
            delivery_id=f"restart-interrupted:{branch_id}",
        )
    except Exception:
        log.warning("restart notice for %s failed", parent_session_id[:12], exc_info=True)


def recover_orphan_branch(state: "SupervisorState", branch_id: str) -> bool:
    """把上一进程留下的入口分支合并回父会话并删掉，返回处理了没有。

    分支没有新内容、认不出合并起点、父会话已重置或出错时返回 False，交给原来的整段清理。
    """
    with state._lock:
        entry = state._session_store.get_entry(branch_id)
        if not isinstance(entry, dict) or entry.get("reset"):
            return False
        parent = str(state.entry_branch_parents.get(branch_id) or entry.get("parent_session_id") or "").strip()
        parent_entry = state._session_store.get_entry(parent) if parent else None
        if parent_entry is None or parent_entry.get("reset") or state._is_entry_branch_session_locked(parent):
            return False

    store = ConversationStore(state.workspace_root / "data" / "conversations")
    try:
        branch_messages = list(store.load(branch_id))
        parent_messages = list(store.load(parent))
        base = merge_base(branch_id, entry, branch_messages, parent_messages)
        if base is None:
            log.warning("startup reconcile: cannot tell where branch %s forked from %s, dropping it", branch_id, parent[:12])
            return False
        parent_ids = {str(message.id or "") for message in parent_messages}
        if not any(
            not str(message.id or "") or str(message.id or "") not in parent_ids
            for message in branch_messages[base:]
        ):
            return False
        append_interrupted_results(store, branch_id)
        _append_history_notice(store, branch_id)
    except Exception:
        log.exception("startup reconcile: preparing branch %s for merge failed", branch_id)
        return False

    with state._lock:
        cleanup_ids, retained_ids = state._branch_cleanup_sessions_locked(branch_id)
        plan = BranchFinalizePlan(
            identity=f"restart:{branch_id}:branch-finalize:v1",
            task_id="",
            parent_session_id=parent,
            branch_session_id=branch_id,
            base_count=base,
            merge=True,
            cleanup_session_ids=tuple(cleanup_ids),
            retained_session_ids=tuple(retained_ids),
        )
    try:
        merged = state._execute_branch_finalize_io(plan)
    except Exception:
        log.exception("startup reconcile: merging branch %s into %s failed", branch_id, parent[:12])
        return False
    with state._lock:
        snapshot = state._commit_branch_finalize_indexes_locked(plan)
    if snapshot is not None and not state._session_store.flush_snapshot(snapshot):
        log.warning("startup reconcile: sessions registry flush after merging %s failed", branch_id)
    _notify(state, parent, branch_id)
    log.info(
        "startup reconcile: merged %d message(s) of interrupted branch %s into %s", merged, branch_id, parent[:12],
    )
    return True
