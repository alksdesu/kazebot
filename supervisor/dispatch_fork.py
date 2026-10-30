"""异步 fork 委派的历史复制：在全局锁外把来源会话暂存成临时文件，进锁建任务时再整份装进新会话。

复制以派发那一刻为准：engine 先把这一轮要写的落盘再发委派，等应答期间调用方不会再写来源会话。
复制不了就明确拒收或判失败，不让子任务悄悄从空历史开始。
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from engine.conversation_store import ConversationStore, StagedFork

if TYPE_CHECKING:
    from .state import SupervisorState

log = logging.getLogger(__name__)

FORK_SOURCE_KEY = "dispatch_fork_from_session"
SOURCE_UNKNOWN_STATUS = 422
COPY_FAILED_STATUS = 500
# 来源会话要拼进对话文件名：只认 uuid、branch_N 这类字符，../ 和 Windows 上的 : 都带不出对话目录
_SESSION_FILE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class ForkCopyError(Exception):
    """fork 复制做不了：status 是回给 engine 的 HTTP 状态码，reason 是做不了的原因。"""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason

    @property
    def refusal(self) -> str:
        """拒收入站时给模型看的那句。"""
        return f"没有派出：{self.reason}，这次委派没有建出来。"


def fork_source(payload: Mapping[str, Any]) -> str:
    """入站要从哪个会话 fork 历史；不 fork 给空串。"""
    return str(payload.get(FORK_SOURCE_KEY) or "").strip()


def _store(state: "SupervisorState") -> ConversationStore:
    return ConversationStore(state.workspace_root / "data" / "conversations")


def _copy_failed(source: str, exc: BaseException) -> ForkCopyError:
    return ForkCopyError(
        COPY_FAILED_STATUS, f"复制 fork 来源会话 {source[:24]} 的历史时读写出错（{type(exc).__name__}），可以稍后重试",
    )


def check_source_locked(state: "SupervisorState", source: str) -> None:
    """来源会话得是 supervisor 登记过的会话，或者是名字像会话 id、已经有对话文件的会话；都不是就抛 ForkCopyError。"""
    if source in state.sessions:
        return
    if (
        _SESSION_FILE_ID_RE.fullmatch(source)
        and (state.workspace_root / "data" / "conversations" / f"{source}.jsonl").is_file()
    ):
        return
    raise ForkCopyError(SOURCE_UNKNOWN_STATUS, f"fork 的来源会话 {source[:24]} 不存在，复制不了调用方的历史")


def stage(state: "SupervisorState", source: str) -> StagedFork:
    """在锁外把来源会话暂存成临时文件。读写出错抛 ForkCopyError：细节写进日志，给模型的只有错误类型。"""
    try:
        return _store(state).stage_fork(source)
    except Exception as exc:
        log.warning("staging the fork of session %s failed", source[:24], exc_info=True)
        raise _copy_failed(source, exc) from exc


def install_locked(state: "SupervisorState", staged: StagedFork, session_id: str) -> int:
    """把暂存的副本装进新建的会话，返回装进去的消息条数；目标已有内容时不装。出错抛 ForkCopyError。"""
    try:
        return _store(state).install_fork(staged, session_id)
    except Exception as exc:
        log.warning(
            "installing the fork of session %s into %s failed", staged.source_session_id[:24], session_id[:24],
            exc_info=True,
        )
        raise _copy_failed(staged.source_session_id, exc) from exc


def discard(staged: StagedFork | None) -> None:
    """丢掉没装进去的暂存副本。"""
    if staged is None:
        return
    try:
        ConversationStore.discard_fork(staged)
    except OSError:
        log.warning("removing the staged fork of session %s failed", staged.source_session_id[:24], exc_info=True)


def copy_if_empty_locked(state: "SupervisorState", source: str, session_id: str) -> str:
    """没经过 /v1/inbound 暂存的 fork（进程里直接建的任务）在锁里补做复制，目标已有内容就不动。

    复制不了时返回写进任务失败结果的原因，复制成功或不用复制时给空串。
    """
    staged: StagedFork | None = None
    try:
        if _store(state).has_messages(session_id):
            return ""
        check_source_locked(state, source)
        staged = stage(state, source)
        install_locked(state, staged, session_id)
    except ForkCopyError as failure:
        return f"没能开始：{failure.reason}"
    except Exception as exc:
        log.warning("checking the fork target %s failed", session_id[:24], exc_info=True)
        return f"没能开始：{_copy_failed(source, exc).reason}"
    finally:
        discard(staged)
    return ""
