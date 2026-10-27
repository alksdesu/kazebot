"""undo_edits：按任务撤销编辑工具的改动，或者列出本会话（含派出去的子任务）的编辑记录；不给 task_id 只撤当前任务。

一个文件现在的内容还是那个任务最后写下的版本，才放回改之前的样子（原来没有的文件就删掉）；之后又被改过的跳过并说明原因，force 才盖掉。
每个文件写回之前先认领，再走 write_file 的策略和审批。"""
from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from clonoth_runtime import parse_bool

from .. import file_state
from .._common import request_guard, resolve_under_allowed_roots
from ..context import ToolContext, project_origin_of, project_root_of
from ..edit_journal import EditJournal, JournalCorrupt, JournalEntry, JournalScope, TaskJournal, inside_journal
from ..file_state import _path_key
from ..syntax_check import command_path

_LIST_TASKS = 20
_LIST_FILES = 30
_CANDIDATE_FILES = 5
_TASK_PREFIX_MIN = 8
_FORCE_HINT = "确认要盖掉之后的改动就加 force=true"

RESTORED = "restored"
DELETED = "deleted"
ALREADY = "already"
SKIPPED = "skipped"
DENIED = "denied"
CANCELLED = "cancelled"
BLOCKED = "blocked"
HALTED = "halted"
_DONE = frozenset({RESTORED, DELETED, ALREADY})


@dataclass(frozen=True)
class _Plan:
    """action：restore / delete / already / skip。

    forced：文件已不是那个任务写下的版本，靠 force 才动它；overridable：这次跳过是因为文件之后又变了，加 force 就能盖掉。
    """

    action: str
    reason: str = ""
    forced: bool = False
    overridable: bool = False

    def skip_reason(self) -> str:
        return f"{self.reason}；{_FORCE_HINT}" if self.overridable else self.reason


@dataclass(frozen=True)
class _FileOutcome:
    """stamp：写回之后的新戳，锁外登记到当前任务名下；holder：挡住这次撤销的那个任务。"""

    entry: JournalEntry
    action: str
    reason: str = ""
    forced: bool = False
    stamp: file_state.Stamp | None = None
    holder: dict[str, Any] | None = None
    unstamped: bool = False

    @property
    def done(self) -> bool:
        return self.action in _DONE

    def line(self) -> str:
        display = self.entry.display
        forced = "（加了 force，盖掉了之后的改动）" if self.forced else ""
        unstamped = "（恢复后的版本没能登记到 supervisor，再改它之前先 read_file）" if self.unstamped else ""
        if self.action == RESTORED:
            return f"- 已恢复 {display}{forced}{unstamped}"
        if self.action == DELETED:
            return f"- 已删除 {display}（这个任务新建的文件）{forced}"
        if self.action == ALREADY:
            if self.entry.created:
                return f"- {display} 已经不在了（这个任务新建的文件），不用删"
            return f"- {display} 已经是改之前的内容，不用恢复"
        return f"- 跳过 {display}：{self.reason}"

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"path": self.entry.display, "action": self.action}
        if self.reason:
            data["reason"] = self.reason
        if self.forced:
            data["forced"] = True
        if self.holder:
            data["claimedBy"] = dict(self.holder)
        if self.unstamped:
            data["stampRecorded"] = False
        return data


def _error(message: str, **data_fields: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, "data": {"result": f"ERROR: {message}", **data_fields}}


def _when(timestamp: float) -> str:
    if timestamp <= 0:
        return "时间不详"
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


def _task_label(task: TaskJournal, current_task: str) -> str:
    current = "，当前任务" if task.task_id == current_task else ""
    return f"任务 {task.task_id}（{_when(task.changed_at)}{current}）"


def _latest_per_path(task: TaskJournal) -> list[JournalEntry]:
    """每个文件只留最新的那一段改动，按第一次改它的顺序排。"""
    latest: dict[str, JournalEntry] = {}
    for entry in sorted(task.entries, key=lambda item: item.id):
        latest[_path_key(Path(entry.path))] = entry
    return sorted(latest.values(), key=lambda item: item.id)


def _entry_status(entry: JournalEntry) -> str:
    if entry.undone is not None:
        return "已撤销"
    if not entry.undoable:
        return f"不可撤销：{entry.pre.reason or '没有记下原文件'}"
    if entry.post is None:
        return "未撤销，写入后的版本没有记下"
    return "未撤销"


def _task_status(task: TaskJournal) -> str:
    entries = _latest_per_path(task)
    undone = sum(1 for entry in entries if entry.undone is not None)
    stuck = any(entry.undone is None and not entry.undoable for entry in entries)
    if undone == len(entries):
        status = "已撤销"
    elif undone:
        status = "部分撤销"
    else:
        status = "未撤销"
    return f"{status}，有文件不可撤销" if stuck else status


def _entry_line(entry: JournalEntry) -> str:
    if entry.created:
        how = f"{entry.tool} 新建"
    elif entry.post is not None and not entry.post.exists:
        how = f"{entry.tool} 删除"
    else:
        how = entry.tool
    if entry.writes > 1:
        how += f"，写了 {entry.writes} 次"
    return f"{entry.display}（{how}）{_entry_status(entry)}"


def _list_response(tasks: list[TaskJournal], current_task: str, retention_days: int) -> dict[str, Any]:
    if not tasks:
        text = f"本会话没有编辑记录（write_file / apply_diff / edit_lines 改文件之前记下的原文件保留 {retention_days} 天）。"
        return {
            "ok": True,
            "data": {"result": text, "action": "list", "tasks": [], "totalTasks": 0, "retentionDays": retention_days},
        }
    shown = tasks[:_LIST_TASKS]
    lines = [
        f"本会话（含派出去的子任务）的编辑记录（共 {len(tasks)} 个任务，最近改过文件的在前；记录保留 {retention_days} 天）：",
    ]
    rows: list[dict[str, Any]] = []
    for number, task in enumerate(shown, 1):
        node = f" · {task.node_id}" if task.node_id else ""
        lines.append(f"{number}. {_task_label(task, current_task)}{node}：{_task_status(task)}")
        entries = _latest_per_path(task)
        lines.extend(f"   - {_entry_line(entry)}" for entry in entries[:_LIST_FILES])
        if len(entries) > _LIST_FILES:
            lines.append(f"   - …还有 {len(entries) - _LIST_FILES} 个文件没有列出")
        rows.append({
            "taskId": task.task_id,
            "changedAt": _when(task.changed_at),
            "nodeId": task.node_id,
            "current": task.task_id == current_task,
            "status": _task_status(task),
            "files": [
                {
                    "path": entry.display, "tool": entry.tool, "created": entry.created, "writes": entry.writes,
                    "undone": entry.undone is not None, "undoable": entry.undoable, "status": _entry_status(entry),
                }
                for entry in entries
            ],
        })
    if len(tasks) > len(shown):
        lines.append(f"更早的 {len(tasks) - len(shown)} 个任务没有列出。")
    lines.append(
        "不带 task_id 的 undo 只撤当前任务，而且要本会话里只剩当前任务有改动没撤；"
        "否则用 task_id 指明撤哪个任务，paths 只撤其中几个文件。"
    )
    return {
        "ok": True,
        "data": {
            "result": "\n".join(lines), "action": "list", "tasks": rows,
            "totalTasks": len(tasks), "retentionDays": retention_days,
        },
    }


def _requested_paths(raw: Any) -> list[str] | None:
    if raw is None or raw == "" or raw == []:
        return None
    items = [raw] if isinstance(raw, str) else raw
    if not isinstance(items, list) or not all(isinstance(item, str) and item.strip() for item in items):
        raise ValueError("paths 要是文件路径字符串的数组")
    return [item.strip() for item in items]


def _find_task(tasks: list[TaskJournal], ref: str) -> tuple[TaskJournal | None, str]:
    for task in tasks:
        if task.task_id == ref:
            return task, ""
    if len(ref) >= _TASK_PREFIX_MIN:
        matches = [task for task in tasks if task.task_id.startswith(ref)]
        if len(matches) == 1:
            return matches[0], ""
        if matches:
            names = "、".join(task.task_id for task in matches)
            return None, f"task_id {ref} 能对上 {len(matches)} 个任务（{names}），写完整的 task_id"
    return None, f"本会话没有任务 {ref} 的编辑记录（不是这个会话的任务，或者已经过了保留期）；用 action=list 看有哪些"


def _leftover_reasons(task: TaskJournal) -> str:
    reasons = sorted({entry.pre.reason or "没有记下原文件" for entry in task.entries if entry.undone is None})
    return "；".join(reasons)


def _nothing_left(tasks: list[TaskJournal]) -> str:
    if not tasks:
        return "本会话没有编辑记录，没有可撤销的改动"
    leftovers = [task for task in tasks if any(entry.undone is None for entry in task.entries)]
    if leftovers:
        reasons = "；".join(f"{task.task_id}（{_leftover_reasons(task)}）" for task in leftovers)
        return f"本会话已经没有能撤销的改动；剩下的都不可撤销：{reasons}"
    return "本会话的改动都已经撤销过了"


def _candidates_error(head: str, pending: list[TaskJournal], current_task: str) -> dict[str, Any]:
    lines = [f"ERROR: {head}。还有改动没撤的任务（最近改过文件的在前）："]
    rows: list[dict[str, Any]] = []
    for task in pending[:_LIST_TASKS]:
        files = [entry.display for entry in _latest_per_path(task) if entry.pending]
        shown = "、".join(files[:_CANDIDATE_FILES])
        if len(files) > _CANDIDATE_FILES:
            shown += f" 等 {len(files)} 个文件"
        node = f" · {task.node_id}" if task.node_id else ""
        lines.append(f"- {_task_label(task, current_task)}{node}：{shown}")
        rows.append({
            "taskId": task.task_id,
            "nodeId": task.node_id,
            "changedAt": _when(task.changed_at),
            "current": task.task_id == current_task,
            "files": files,
        })
    if len(pending) > _LIST_TASKS:
        lines.append(f"更早的 {len(pending) - _LIST_TASKS} 个任务没有列出，用 action=list 查看。")
    return {
        "ok": False,
        "error": head,
        "data": {"result": "\n".join(lines), "action": "undo", "needTaskId": True, "candidates": rows},
    }


def _default_task(tasks: list[TaskJournal], current_task: str) -> tuple[TaskJournal | None, dict[str, Any] | None]:
    """不给 task_id 时只撤当前任务；本会话里还有别的任务的改动没撤，就列出候选、要求指明，不替模型挑。

    同一会话里并行的另一个任务也记在这里，按「最近一个」去撤会撤到别人正在做的改动。
    """
    pending = [task for task in tasks if any(entry.pending for entry in task.entries)]
    mine = next((task for task in pending if task.task_id == current_task), None)
    if mine is not None and len(pending) == 1:
        return mine, None
    if not pending:
        return None, _error(_nothing_left(tasks))
    if mine is None:
        own = next((task for task in tasks if task.task_id == current_task), None)
        reasons = _leftover_reasons(own) if own is not None else ""
        head = "当前任务没有可撤销的改动"
        if reasons:
            head += f"（剩下的不可撤销：{reasons}）"
        head += f"；本会话里还有 {len(pending)} 个任务的改动没撤，要撤哪一个请用 task_id 指明"
    else:
        head = f"本会话里有 {len(pending)} 个任务的改动没撤，不给 task_id 时不替你挑；要撤哪一个请用 task_id 指明，撤当前任务也要写明"
    return None, _candidates_error(head, pending, current_task)


def _targets(
    task: TaskJournal,
    requested: list[str] | None,
    workspace_root: Path,
    project_root: Path,
    project_origin: Path | None = None,
) -> tuple[list[JournalEntry], list[str], list[str]]:
    """要撤的条目（后改的在前），以及 paths 里这个任务没改过的、已经撤过的。解析路径要读策略文件，放线程里调。"""
    open_entries = [entry for entry in task.entries if entry.undone is None]
    if requested is None:
        return sorted(open_entries, key=lambda item: item.id, reverse=True), [], []
    selected: list[JournalEntry] = []
    missing: list[str] = []
    settled: list[str] = []
    for raw in requested:
        keys: set[str] = set()
        with contextlib.suppress(ValueError, OSError):
            keys.add(_path_key(resolve_under_allowed_roots(
                workspace_root, raw, project_root=project_root, project_origin=project_origin,
            )))
        matched = [entry for entry in task.entries if entry.display == raw or _path_key(Path(entry.path)) in keys]
        if not matched:
            missing.append(raw)
            continue
        still_open = [entry for entry in matched if entry.undone is None]
        if not still_open:
            settled.append(raw)
        selected.extend(entry for entry in still_open if entry not in selected)
    return sorted(selected, key=lambda item: item.id, reverse=True), missing, settled


def _post_mismatch(entry: JournalEntry, snapshot: file_state.FileSnapshot) -> str | None:
    """文件现在是不是那个任务最后写下的版本；不是时给出原因。"""
    post = entry.post
    if post is None:
        return "这次改动没有记下写入后的版本，确认不了之后没被别处改过"
    if post.exists != snapshot.exists:
        return "文件在这次改动之后被删掉了" if post.exists else "文件在这次改动之后又被建了出来"
    if snapshot.stamp is not None and snapshot.stamp.sha256 != post.sha256:
        return "文件在这次改动之后又被改过"
    return None


def _plan(entry: JournalEntry, snapshot: file_state.FileSnapshot, *, force: bool) -> _Plan:
    # 已经是改之前的样子就不用比改后的版本了：被别处改回去、或者新建的文件已经没了，都没有可以盖掉的东西
    if entry.pre.exists:
        if snapshot.stamp is not None and snapshot.stamp.sha256 == entry.pre.sha256:
            return _Plan("already")
    elif not snapshot.exists:
        return _Plan("already")
    mismatch = _post_mismatch(entry, snapshot)
    if mismatch is not None and not force:
        return _Plan("skip", mismatch, overridable=True)
    return _Plan("restore" if entry.pre.exists else "delete", forced=mismatch is not None)


def _probe(
    journal: EditJournal, session: str, task_id: str, entry: JournalEntry, force: bool,
) -> tuple[_Plan, bytes | None]:
    """送审之前先看一眼：会被跳过、或者已经是原样的，就不必为它弹审批卡；要写回的先把原文件内容读出来核对好。"""
    try:
        snapshot = file_state.take_snapshot(Path(entry.path))
    except OSError as exc:
        return _Plan("skip", f"读不了这个文件（{exc}）"), None
    plan = _plan(entry, snapshot, force=force)
    if plan.action != "restore":
        return plan, None
    try:
        return plan, journal.load_pre_image(session, task_id, entry)
    except (ValueError, OSError) as exc:
        return _Plan("skip", str(exc)), None


def _restore_locked(path: Path, data: bytes, snapshot: file_state.FileSnapshot, mode: int | None) -> file_state.Stamp:
    """整份原子写回原文件的字节，换行符和 BOM 就在字节里。调用方持有 file_state.edit_lock(path, 工作区)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if snapshot.exists or mode is None or os.name == "nt":
        return file_state.commit_locked(path, data, expected=snapshot)
    # 文件已经不在了：先按原来的权限建个空文件，整份替换会沿用目标的权限，不会有一瞬间按 umask 建出来、谁都能读
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError as exc:
        raise file_state.ConcurrentEdit(file_state.CONCURRENT_MESSAGE) from exc
    os.close(fd)
    placeholder = file_state.take_snapshot(path)
    try:
        return file_state.commit_locked(path, data, expected=placeholder)
    except BaseException:
        with contextlib.suppress(OSError):
            if file_state.take_snapshot(path).stamp == placeholder.stamp:
                path.unlink()
        raise


def _delete_locked(path: Path, expected: file_state.FileSnapshot) -> None:
    """确认还是比对过的那一版再删。engine worker 之间靠 edit_lock 排队，这里再比一次，挡的是不走这把锁的写入方。"""
    current = file_state.take_snapshot(path)
    if not current.exists:
        return
    if current.stamp is None or expected.stamp is None or current.stamp.sha256 != expected.stamp.sha256:
        raise file_state.ConcurrentEdit(file_state.CONCURRENT_MESSAGE)
    path.unlink()


def _remove_created_dirs(path: Path, created_dirs: tuple[str, ...]) -> None:
    """那个任务为新文件建出来、现在又空了的目录，从最深的删起；不空的、不是这个文件上级的都不动。"""
    for directory in created_dirs:
        candidate = Path(directory)
        if candidate == path or not path.is_relative_to(candidate):
            continue
        try:
            candidate.rmdir()
        except OSError:
            break


def _undo_locked(
    journal: EditJournal,
    scope: JournalScope,
    task: TaskJournal,
    entry: JournalEntry,
    data: bytes | None,
    force: bool,
) -> _FileOutcome:
    path = Path(entry.path)
    with file_state.edit_lock(path, journal.workspace_root):
        current = journal.entry(task.session, task.task_id, entry.id)
        if current is None:
            return _FileOutcome(entry, SKIPPED, "编辑日志里已经没有这条记录")
        if current.undone is not None:
            return _FileOutcome(entry, SKIPPED, "已经撤销过了")
        snapshot = file_state.take_snapshot(path)
        plan = _plan(current, snapshot, force=force)
        if plan.action == "skip":
            return _FileOutcome(entry, SKIPPED, f"比对之后文件又变了：{plan.skip_reason()}")
        stamp: file_state.Stamp | None = None
        if plan.action == "restore":
            original = data if data is not None else journal.load_pre_image(task.session, task.task_id, current)
            stamp = _restore_locked(path, original, snapshot, current.pre.mode)
            action = RESTORED
        elif plan.action == "delete":
            _delete_locked(path, snapshot)
            _remove_created_dirs(path, current.pre.created_dirs)
            action = DELETED
        else:
            action = ALREADY
        journal.mark_undone(task.session, task.task_id, entry.id, by_task=scope.task_id, action=action, forced=plan.forced)
        return _FileOutcome(current, action, forced=plan.forced, stamp=stamp)


def _guard_parameters(ctx: ToolContext, task: TaskJournal, entry: JournalEntry, plan: _Plan) -> dict[str, Any]:
    if plan.action == "delete":
        preview = f"undo_edits: 删除 {entry.display}（任务 {task.task_id} 新建的文件）"
        size = 0
    else:
        preview = f"undo_edits: 把 {entry.display} 恢复成任务 {task.task_id} 改之前的内容（{entry.pre.size} 字节）"
        size = entry.pre.size
    if plan.forced:
        preview += "，会盖掉之后的改动"
    return {
        "path": command_path(project_root_of(ctx), Path(entry.path)),
        "content_preview": preview,
        "content_len": size,
        "tool_name": "undo_edits",
    }


async def _apply_undo(
    journal: EditJournal, scope: JournalScope, task: TaskJournal, entry: JournalEntry, data: bytes | None, force: bool,
) -> _FileOutcome:
    try:
        return await asyncio.to_thread(_undo_locked, journal, scope, task, entry, data, force)
    except file_state.ConcurrentEdit:
        return _FileOutcome(entry, SKIPPED, "文件在比对和写入之间被别处改过，没有动它")
    except JournalCorrupt as exc:
        return _FileOutcome(entry, SKIPPED, str(exc))
    except ValueError as exc:
        return _FileOutcome(entry, SKIPPED, f"编辑日志用不了（{exc}）")
    except OSError as exc:
        return _FileOutcome(entry, SKIPPED, f"读写文件失败（{exc}）")


async def _settle(ctx: ToolContext, grant: file_state.EditGrant, outcome: _FileOutcome) -> _FileOutcome:
    """写回了就把新版本记到当前任务名下，认领留到任务结束；没动文件就还回这次才拿的认领。"""
    if outcome.action == RESTORED:
        recorded = await file_state.finish_edit(ctx, grant, outcome.stamp, revealable=outcome.entry.pre.revealable)
        return outcome if recorded else replace(outcome, unstamped=True)
    if outcome.action == DELETED:
        await file_state.finish_edit(ctx, grant, None, revealable=False)
        return outcome
    await file_state.abandon_edit(ctx, grant)
    return outcome


async def _undo_entry(
    ctx: ToolContext, journal: EditJournal, scope: JournalScope, task: TaskJournal, entry: JournalEntry, *, force: bool,
) -> _FileOutcome:
    if not entry.undoable:
        return _FileOutcome(entry, SKIPPED, f"{entry.pre.reason or '改之前没有记下原文件'}，撤不了")
    # 编辑工具从不往日志目录里写，记着这种路径只能是 manifest 被改过
    if inside_journal(ctx.workspace_root, Path(entry.path)):
        return _FileOutcome(entry, SKIPPED, "日志里记的路径落在编辑日志目录里，不能当撤销的目标")
    plan, data = await asyncio.to_thread(_probe, journal, task.session, task.task_id, entry, force)
    if plan.action == "skip":
        return _FileOutcome(entry, SKIPPED, plan.skip_reason())
    if plan.action not in {"restore", "delete"}:
        return await _apply_undo(journal, scope, task, entry, data, force)
    try:
        grant = await file_state.begin_edit(ctx, Path(entry.path), entry.display)
    except file_state.EditBlocked as blocked:
        holder = blocked.data.get("claimedBy")
        return _FileOutcome(
            entry, HALTED if blocked.unavailable else BLOCKED, blocked.message,
            holder=holder if isinstance(holder, dict) else None,
        )
    _op, err = await request_guard(ctx, "write_file", _guard_parameters(ctx, task, entry, plan))
    if err is not None:
        await file_state.abandon_edit(ctx, grant)
        if err.get("cancelled"):
            return _FileOutcome(entry, CANCELLED, "任务已取消")
        return _FileOutcome(entry, DENIED, f"审批没有通过（{err.get('error') or 'denied'}）")
    outcome = await _apply_undo(journal, scope, task, entry, data, force)
    return await _settle(ctx, grant, outcome)


def _counts(outcomes: list[_FileOutcome]) -> str:
    labels = (
        (RESTORED, "恢复"), (DELETED, "删除"), (ALREADY, "本来就是原样"), (SKIPPED, "跳过"),
        (BLOCKED, "别的任务正在改"), (DENIED, "审批没过"),
    )
    parts = [
        f"{label} {count} 个"
        for action, label in labels
        if (count := sum(1 for outcome in outcomes if outcome.action == action))
    ]
    return "、".join(parts) or "没有处理任何文件"


def _undo_response(
    task: TaskJournal,
    outcomes: list[_FileOutcome],
    missing: list[str],
    settled: list[str],
    *,
    cancelled: bool,
    halted: str,
    current_task: str,
) -> dict[str, Any]:
    finished = [outcome for outcome in outcomes if outcome.action not in {CANCELLED, HALTED}]
    lines = [f"撤销{_task_label(task, current_task)}的改动：{_counts(finished)}。"]
    lines.extend(outcome.line() for outcome in finished)
    lines.extend(f"- 这个任务没有改过 {raw}" for raw in missing)
    lines.extend(f"- {raw} 在这个任务里的改动已经撤销过了" for raw in settled)
    if halted:
        lines.append(f"{halted}，后面的文件没有处理。")
    if cancelled:
        lines.append("任务被取消，后面的文件没有处理。")
    done = [outcome for outcome in finished if outcome.done]
    data: dict[str, Any] = {
        "result": "\n".join(lines),
        "action": "undo",
        "taskId": task.task_id,
        "files": [outcome.to_data() for outcome in finished],
        "changedPaths": [outcome.entry.display for outcome in finished if outcome.action in {RESTORED, DELETED}],
        "restoredCount": sum(1 for outcome in finished if outcome.action == RESTORED),
        "deletedCount": sum(1 for outcome in finished if outcome.action == DELETED),
        "skippedCount": sum(1 for outcome in finished if not outcome.done),
    }
    if missing:
        data["notInTask"] = list(missing)
    if settled:
        data["alreadyUndone"] = list(settled)
    if halted:
        data["halted"] = halted
    if cancelled:
        data["cancelled"] = True
        return {"ok": False, "error": "task cancelled", "cancelled": True, "data": data}
    if not done:
        first = next((outcome for outcome in finished if outcome.reason), None)
        if first is not None:
            error = f"没有撤销任何文件：{first.entry.display} {first.reason}"
        else:
            error = f"没有撤销任何文件：{halted}" if halted else "没有撤销任何文件"
        return {"ok": False, "error": error, "data": data}
    return {"ok": True, "data": data}


async def undo_edits(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    action = str(args.get("action") or "undo").strip().lower()
    if action not in {"list", "undo"}:
        return _error(f"action 只能是 list 或 undo，收到 {args.get('action')!r}")
    scope = JournalScope.of(ctx)
    if not scope.session.strip():
        return _error("没有会话号，找不到编辑记录")
    try:
        root = await file_state.journal_session(ctx)
    except file_state.EditBlocked as blocked:
        return _error(blocked.message, **blocked.data)
    journal = EditJournal(ctx.workspace_root)
    try:
        # accumulate 子节点自己的会话下也可能有保留期内的记录，一并列出
        tasks = await asyncio.to_thread(journal.tasks_in, (root, scope.session))
    except OSError as exc:
        return _error(f"编辑日志读不出来（{exc}）")

    if action == "list":
        retention_days = await asyncio.to_thread(lambda: journal.retention_days)
        return _list_response(tasks, scope.task_id, retention_days)

    try:
        requested = _requested_paths(args.get("paths"))
    except ValueError as exc:
        return _error(str(exc))
    force = bool(parse_bool(args.get("force"), False))
    ref = str(args.get("task_id") or "").strip()
    if ref:
        task, problem = _find_task(tasks, ref)
        if task is None:
            return _error(problem)
    else:
        task, refusal = _default_task(tasks, scope.task_id)
        if task is None:
            return refusal or _error(_nothing_left(tasks))

    entries, missing, settled = await asyncio.to_thread(
        _targets, task, requested, ctx.workspace_root, project_root_of(ctx), project_origin_of(ctx),
    )
    if not entries:
        if requested is None:
            return _error(f"{_task_label(task, scope.task_id)}的改动都已经撤销过了", taskId=task.task_id)
        details = [f"这个任务没有改过 {raw}" for raw in missing] + [f"{raw} 已经撤销过了" for raw in settled]
        return _error(f"{_task_label(task, scope.task_id)}里没有要撤的文件：{'；'.join(details)}", taskId=task.task_id)

    outcomes: list[_FileOutcome] = []
    cancelled = False
    halted = ""
    for entry in entries:
        outcome = await _undo_entry(ctx, journal, scope, task, entry, force=force)
        outcomes.append(outcome)
        if outcome.action == CANCELLED:
            cancelled = True
            break
        if outcome.action == HALTED:
            halted = outcome.reason
            break
    return _undo_response(
        task, outcomes, missing, settled, cancelled=cancelled, halted=halted, current_task=scope.task_id,
    )
