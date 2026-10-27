"""委派两端共用的约束：engine 组同步批次、等异步委派，supervisor 建子任务、挂起和恢复调用方都按这里来。"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from clonoth_runtime import get_int

from .attachments import is_allowed_attachment_path
from .dispatch_handle import parse_handle

MAX_DISPATCH_BATCH_ITEMS = 8
MAX_ITEM_ATTACHMENTS = 32
# 批次项里要接着哪个子 agent 干：值是委派结果给的句柄
CONTINUE_FROM_KEY = "continue_from"
# 批次项最多等几秒：到点没做完的那一项按失败交回，其余照常收齐
ITEM_TIMEOUT_KEY = "timeout_sec"
# 委派项要不要隔离：值是 ISOLATION_WORKTREE 时子任务在自己的 git worktree 里做；批次项、dispatch_input、dispatch_origin 都用这两个键
ISOLATION_KEY = "isolation"
ISOLATION_WORKTREE = "worktree"
# 要隔离的仓库：绝对路径，不写就是工作区所在的仓库
REPO_KEY = "repo"

# dispatch 动作的 dispatch_input 带这个键：不派新的子任务，挂起调用方等这些异步委派
AWAIT_KEY = "await_dispatches"
# supervisor 挂起调用方等异步委派期间写在它 input 上，恢复时去掉
AWAITING_INPUT_KEY = "_awaiting_dispatches"
AWAIT_ALL = "all"
AWAIT_ANY = "any"
AWAIT_MODES = (AWAIT_ALL, AWAIT_ANY)
MAX_AWAIT_HANDLES = 16

AWAIT_TIMEOUT_CONFIG_KEY = "engine.dispatch.await_timeout_sec"
MAX_TIMEOUT_CONFIG_KEY = "engine.dispatch.max_timeout_sec"
MIN_TIMEOUT_SEC = 5
DEFAULT_AWAIT_TIMEOUT_SEC = 600
DEFAULT_MAX_TIMEOUT_SEC = 3600
MAX_TIMEOUT_CEILING_SEC = 21600

_ATTACHMENT_TYPES = frozenset({"image", "file"})


@dataclass(frozen=True)
class DispatchTimeouts:
    """await_dispatches 不写 timeout_sec 时等多久，以及它和 wait 委派单项 timeout_sec 的上限（秒）。"""

    await_default: int = DEFAULT_AWAIT_TIMEOUT_SEC
    maximum: int = DEFAULT_MAX_TIMEOUT_SEC

    @classmethod
    def from_config(cls, runtime_cfg: Any) -> "DispatchTimeouts":
        cfg = runtime_cfg if isinstance(runtime_cfg, dict) else {}
        maximum = get_int(
            cfg, MAX_TIMEOUT_CONFIG_KEY, DEFAULT_MAX_TIMEOUT_SEC,
            min_value=60, max_value=MAX_TIMEOUT_CEILING_SEC,
        )
        default = get_int(
            cfg, AWAIT_TIMEOUT_CONFIG_KEY, DEFAULT_AWAIT_TIMEOUT_SEC,
            min_value=MIN_TIMEOUT_SEC, max_value=MAX_TIMEOUT_CEILING_SEC,
        )
        return cls(await_default=min(default, maximum), maximum=maximum)

    def clamp(self, seconds: int) -> int:
        return max(MIN_TIMEOUT_SEC, min(int(seconds), self.maximum))


@dataclass(frozen=True)
class AwaitRequest:
    """await_dispatches 交给 supervisor 的请求。handles 是模型写的原文，格式由 supervisor 逐个再认一遍。"""

    handles: tuple[str, ...]
    mode: str = AWAIT_ALL
    timeout_sec: int | None = None
    call_id: str = ""

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"handles": list(self.handles), "mode": self.mode}
        if self.timeout_sec is not None:
            body["timeout_sec"] = self.timeout_sec
        if self.call_id:
            body["call_id"] = self.call_id
        return body


def parse_timeout_sec(raw: Any) -> int | None:
    """模型填的秒数：正数才算，小数向上取整；布尔、空串、非数字、非正数给 None。"""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return int(math.ceil(value))


def batch_item_timeout(item: Any, timeouts: DispatchTimeouts) -> int:
    """批次项带的单项超时，夹到上下限；没带或写得不对给 0。"""
    if not isinstance(item, dict):
        return 0
    seconds = parse_timeout_sec(item.get(ITEM_TIMEOUT_KEY))
    return timeouts.clamp(seconds) if seconds is not None else 0


def await_request(action: Any) -> AwaitRequest | None:
    """dispatch 动作带的 await_dispatches 请求；不是这种动作给 None。

    supervisor 这一侧不信 engine 校验过：句柄去重、截到上限，mode 认不出按 all，秒数写得不对按没写。
    """
    if not isinstance(action, dict):
        return None
    dispatch_input = action.get("dispatch_input")
    raw = dispatch_input.get(AWAIT_KEY) if isinstance(dispatch_input, dict) else None
    if not isinstance(raw, dict):
        return None
    handles: list[str] = []
    raw_handles = raw.get("handles")
    for item in raw_handles if isinstance(raw_handles, list) else []:
        text = str(item or "").strip() if isinstance(item, (str, int, float)) and not isinstance(item, bool) else ""
        if text and text not in handles:
            handles.append(text)
        if len(handles) >= MAX_AWAIT_HANDLES:
            break
    mode = str(raw.get("mode") or "").strip().lower()
    return AwaitRequest(
        handles=tuple(handles),
        mode=mode if mode in AWAIT_MODES else AWAIT_ALL,
        timeout_sec=parse_timeout_sec(raw.get("timeout_sec")),
        call_id=str(raw.get("call_id") or "").strip(),
    )


def dispatch_isolation(item: Any) -> str:
    """委派项要的隔离方式：要 worktree 给 ISOLATION_WORKTREE，没写或写了别的给空串。"""
    if not isinstance(item, dict):
        return ""
    raw = item.get(ISOLATION_KEY)
    return ISOLATION_WORKTREE if isinstance(raw, str) and raw.strip().lower() == ISOLATION_WORKTREE else ""


def dispatch_repo(item: Any) -> str:
    """委派项写的 repo，去掉首尾空白；没写或不是字符串给空串。"""
    if not isinstance(item, dict):
        return ""
    raw = item.get(REPO_KEY)
    return raw.strip() if isinstance(raw, str) else ""


def batch_item_continue_from(item: Any) -> str:
    """批次项带的 continue_from 句柄；没带或格式不对给空串。"""
    if not isinstance(item, dict):
        return ""
    handle = parse_handle(item.get(CONTINUE_FROM_KEY))
    return str(handle) if handle is not None else ""


def _relative_attachment_path(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    path = raw.strip()
    normalized = path.replace("\\", "/")
    # 绝对路径和盘符拼到工作区下会跳出工作区，is_allowed_attachment_path 会先剥掉开头的 / 再判
    if not path or normalized.startswith("/") or ":" in normalized.split("/", 1)[0]:
        return ""
    return path if is_allowed_attachment_path(path) else ""


def batch_item_attachments(raw: Any) -> list[dict[str, str]]:
    """批次项带的附件只留格式对、路径在 data/ 下的；这里不读盘，子节点读附件时还会按真实位置再判一次。"""
    if not isinstance(raw, list):
        return []
    kept: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if len(kept) >= MAX_ITEM_ATTACHMENTS:
            break
        if not isinstance(item, dict):
            continue
        path = _relative_attachment_path(item.get("path"))
        if not path or path in seen:
            continue
        mime = item.get("mime_type")
        mime = mime.strip() if isinstance(mime, str) else ""
        kind = str(item.get("type") or "").strip()
        if kind not in _ATTACHMENT_TYPES:
            kind = "image" if mime.startswith("image/") else "file"
        name = item.get("name")
        name = name.strip() if isinstance(name, str) and name.strip() else PurePosixPath(path.replace("\\", "/")).name
        attachment = {"type": kind, "path": path, "name": name}
        if mime:
            attachment["mime_type"] = mime
        kept.append(attachment)
        seen.add(path)
    return kept
