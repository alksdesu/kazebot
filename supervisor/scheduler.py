"""轻量定时调度器。

每分钟醒一次，扫描 data/schedules.yaml，到时间的往 inbound 队列注入消息。
调度线程挂了不影响 Supervisor 主进程。
"""
# 系统级定时任务通过 supervisor hook handler 接入，调度器只负责发 tick。

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

import yaml

from engine.context_store import cleanup_old_contexts
from engine.cron import cron_match


if TYPE_CHECKING:
    from .state import SupervisorState

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  schedules.yaml CRUD
# ---------------------------------------------------------------------------

def _schedules_path(workspace_root: Path) -> Path:
    return workspace_root / "data" / "schedules.yaml"


def load_schedules(workspace_root: Path) -> list[dict[str, Any]]:
    p = _schedules_path(workspace_root)
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    items = data.get("schedules")
    if not isinstance(items, list):
        return []
    return [s for s in items if isinstance(s, dict)]


def save_schedules(workspace_root: Path, schedules: list[dict[str, Any]]) -> None:
    p = _schedules_path(workspace_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump({"schedules": schedules}, sort_keys=False, allow_unicode=True)
    p.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
#  调度线程
# ---------------------------------------------------------------------------

class SchedulerThread:
    """后台线程。每 60 秒检查一次 schedules.yaml，匹配的注入 inbound。"""

    def __init__(self, *, state: "SupervisorState", workspace_root: Path) -> None:
        self._state = state
        self._workspace_root = workspace_root
        self._last_fired: dict[str, str] = {}  # schedule_id -> "YYYY-MM-DD HH:MM"
        self._thread: threading.Thread | None = None
        self._running_scripts: set[str] = set()  # 防重入

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")
        self._thread.start()
        log.info("[scheduler] started")

    def _loop(self) -> None:
        tick_count = 0
        while True:
            try:
                self._tick()
            except Exception as e:
                log.warning(f"[scheduler] tick error: {e}")
            tick_count += 1
            # 每 30 分钟清理一次旧上下文快照（30 ticks × 60s = 30min）
            if tick_count % 30 == 0:
                self._cleanup()
            time.sleep(60)

    def _tick(self) -> None:
        now = datetime.now(timezone.utc)
        now_key = now.strftime("%Y-%m-%d %H:%M")

        schedules = load_schedules(self._workspace_root)

        for s in schedules:
            sid = str(s.get("id") or "").strip()
            if not sid:
                continue
            if not s.get("enabled", True):
                continue

            cron_expr = str(s.get("cron") or "").strip()
            if not cron_expr:
                continue

            # 防止同一分钟重复触发
            if self._last_fired.get(sid) == now_key:
                continue

            if not cron_match(cron_expr, now):
                continue

            # 匹配成功
            self._last_fired[sid] = now_key
            once = bool(s.get("once", False))
            stype = str(s.get("type") or "message").strip()

            if stype == "script":
                fired = self._fire_script(s, sid)
            else:
                fired = self._fire_message(s, sid)

            # 注入失败还照删，这条一次性提醒就再也不会响；留着等下次 cron 匹配重试。
            if once and fired:
                self._remove_schedule(sid)

        # Why: system-level scheduled features now live in engine.builtin
        # supervisor hook handlers. How: emit a dream schedule tick with a
        # callback-only context under the state's re-entrant lock. Purpose: keep
        # SchedulerThread generic while avoiding direct handler access to state.
        # handler 按 schedule_type 自己过滤，所以每种系统级定时特性 fire 一次。
        # 这些 handler 必须立即返回：整个循环都在 state 锁里，一个慢 handler 会把
        # 所有用户定时任务一起卡住。
        for schedule_type in ("dream", "data_cleanup"):
            with self._state._lock:
                self._state.hook_registry.fire(
                    "on_schedule_tick",
                    self._state._build_supervisor_hook_ctx(
                        schedule_type=schedule_type,
                        now=now,
                        now_key=now_key,
                    ),
                )

    def _inject_inbound(
        self, sid: str, text: str, conv_key: str, entry_node_id: str = "",
        attachments: list | None = None, created_by: str = "",
    ) -> bool:
        """共用的 inbound 注入逻辑。返回是否真的注入成功。"""
        msg_id = f"scheduler:{sid}:{uuid.uuid4()}"
        if ":" in conv_key:
            channel = conv_key.split(":", 1)[0]
        else:
            channel = "scheduler"
        try:
            session_id = self._state.get_or_create_session(
                channel=channel, conversation_key=conv_key,
            )
            payload: dict[str, Any] = {
                "channel": channel,
                "conversation_key": conv_key,
                "message_id": msg_id,
                "text": text,
                "schedule_id": sid,
                "entry_node_id": entry_node_id,
            }
            if created_by:
                # 定时任务一律按非管理员跑：无人值守的高权限操作没人能当场喊停。
                payload["platform_auth"] = {
                    "platform": "qq", "user_id": created_by, "is_admin": False,
                }
            if attachments:
                payload["attachments"] = attachments
            evt = self._state.eventlog.append(
                session_id=session_id,
                component="scheduler",
                type_="inbound_message",
                payload=payload,
            )
            self._state.record_inbound_message_event(evt)
            log.info(f"[scheduler] fired: {sid} -> session={session_id}")
            return True
        except Exception as e:
            log.warning(f"[scheduler] inject failed for {sid}: {e}")
            return False

    def _fire_message(self, s: dict, sid: str) -> bool:
        """type=message：直接注入文本（原有逻辑）。"""
        text = str(s.get("text") or f"[scheduled:{sid}]").strip()
        conv_key = str(s.get("conversation_key") or f"scheduler:{sid}").strip()
        entry_node_id = str(s.get("entry_node_id") or "").strip()
        return self._inject_inbound(
            sid, text, conv_key, entry_node_id, created_by=str(s.get("created_by") or ""),
        )

    def _fire_script(self, s: dict, sid: str) -> bool:
        """type=script：执行脚本，stdout JSON 解析后注入 inbound。返回是否真的注入成功。"""
        command = str(s.get("command") or "").strip()
        if not command:
            log.error(f"[scheduler] script {sid}: missing 'command' field")
            return False
        if sid in self._running_scripts:
            log.warning(f"[scheduler] script {sid}: still running, skipping")
            return False
        self._running_scripts.add(sid)
        try:
            timeout = int(s.get("timeout") or 30)
            silent = bool(s.get("silent", True))
            text_prefix = str(s.get("text") or "").strip()
            conv_key = str(s.get("conversation_key") or f"scheduler:{sid}").strip()
            entry_node_id = str(s.get("entry_node_id") or "").strip()
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                timeout=timeout, cwd=str(self._workspace_root),
            )
            if result.stderr and result.stderr.strip():
                log.warning(f"[scheduler] script {sid} stderr: {result.stderr.strip()[:500]}")
            if result.returncode != 0:
                log.error(f"[scheduler] script {sid} exited with rc={result.returncode}")
                return False
            stdout = (result.stdout or "").strip()
            if not stdout:
                if silent:
                    log.debug(f"[scheduler] script {sid}: empty stdout, silent skip")
                    return False
                log.info(f"[scheduler] script {sid}: empty stdout, injecting prefix")
                if not text_prefix:
                    return False
                return self._inject_inbound(
                    sid, text_prefix, conv_key, entry_node_id,
                    created_by=str(s.get("created_by") or ""),
                )
            # 解析 JSON
            try:
                data = json.loads(stdout)
            except json.JSONDecodeError as e:
                log.error(f"[scheduler] script {sid}: invalid JSON output: {e}")
                return False
            body_text = str(data.get("text") or "").strip()
            if not body_text:
                log.error(f"[scheduler] script {sid}: JSON missing 'text' field")
                return False
            if text_prefix:
                body_text = text_prefix + "\n" + body_text
            attachments = data.get("attachments") or None
            return self._inject_inbound(
                sid, body_text, conv_key, entry_node_id, attachments,
                created_by=str(s.get("created_by") or ""),
            )
        except subprocess.TimeoutExpired:
            log.error(f"[scheduler] script {sid}: timed out after {s.get('timeout', 30)}s")
            return False
        except Exception as e:
            log.error(f"[scheduler] script {sid}: unexpected error: {e}")
            return False
        finally:
            self._running_scripts.discard(sid)

    def _remove_schedule(self, sid: str) -> None:
        try:
            items = load_schedules(self._workspace_root)
            items = [s for s in items if str(s.get("id") or "").strip() != sid]
            save_schedules(self._workspace_root, items)
            log.info(f"[scheduler] removed once-schedule: {sid}")
        except Exception as e:
            log.warning(f"[scheduler] failed to remove once-schedule {sid}: {e}")

    def _cleanup(self) -> None:
        """定期清理旧的上下文快照和已完成 task 的内存。"""
        try:
            from .types import TaskStatus

            # 在锁内收集 keep_refs 快照
            with self._state._lock:
                keep_refs: set[str] = set()
                for task in self._state.tasks.values():
                    if task.status not in {TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled}:
                        ref = str(task.input.get("context_ref") or "").strip()
                        if ref:
                            keep_refs.add(ref)
                        cref = str(task.continuation.get("resume_context_ref") or "").strip()
                        if cref:
                            keep_refs.add(cref)
                        for frame in (task.continuation.get("resume_stack") or []):
                            if isinstance(frame, dict):
                                fref = str(frame.get("context_ref") or "").strip()
                                if fref:
                                    keep_refs.add(fref)

            # 文件清理在锁外执行（IO 操作）
            count = cleanup_old_contexts(
                self._workspace_root,
                max_age_seconds=3600.0,  # 1 小时前的快照
                keep_refs=keep_refs,
            )
            if count > 0:
                log.info(f"[scheduler] cleaned up {count} old context snapshots")

            # 在锁内清理内存中已终结的旧 task（保留最近 200 个）
            with self._state._lock:
                terminal = {TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled}
                finished_ids = [
                    tid for tid in self._state._task_order
                    if tid in self._state.tasks and self._state.tasks[tid].status in terminal
                ]
                if len(finished_ids) > 200:
                    to_remove = finished_ids[:-200]
                    for tid in to_remove:
                        self._state.tasks.pop(tid, None)
                    self._state._task_order = [
                        tid for tid in self._state._task_order if tid in self._state.tasks
                    ]
                    log.info(f"[scheduler] pruned {len(to_remove)} old tasks from memory")

                # 回收超时的 running 状态 task（安全网，防止 worker 崩溃后 task 永久孤立）
                _now_ts = datetime.now(timezone.utc)
                _stale_cutoff = _now_ts - timedelta(minutes=10)
                try:
                    # [AutoC] 默认任务硬上限统一为 1 小时（3600s），与审批超时对齐；
                    # 可由 CLONOTH_TASK_MAX_RUNNING_SECONDS 覆盖。
                    _max_running_sec = max(300.0, float(os.getenv("CLONOTH_TASK_MAX_RUNNING_SECONDS", "3600")))
                except Exception:
                    _max_running_sec = 3600.0
                _stale_ids: list[tuple[str, str]] = []
                for tid, t in self._state.tasks.items():
                    if t.status != TaskStatus.running:
                        continue
                    if t.updated_at < _stale_cutoff:
                        _stale_min = int((_now_ts - t.updated_at).total_seconds() / 60)
                        _stale_ids.append((tid, f"任务运行超时（{_stale_min} 分钟无响应，疑似 worker 崩溃）"))
                        continue
                    _age_sec = (_now_ts - t.created_at).total_seconds()
                    if _age_sec > _max_running_sec:
                        _stale_ids.append((tid, f"任务运行过久（{int(_age_sec)} 秒），已自动终止以防止僵死阻塞聊天"))
                for tid, _reason in _stale_ids:
                    task = self._state.tasks.get(tid)
                    if task is None or task.status != TaskStatus.running:
                        continue
                    task.status = TaskStatus.failed
                    task.result = {
                        "action": "fail",
                        "node_id": task.node_id or "",
                        "error": _reason,
                    }
                    task.updated_at = _now_ts
                    task.lease_expires_at = None
                    self._state._reset_task_route_state_locked(task)
                    self._state._event_task_snapshot("task_completed", task, component="supervisor")
                    self._state._route_completed_task_locked(task)
                    log.warning(f"[scheduler] reaped stale running task {tid} (node={task.node_id}, reason={_reason})")
        except Exception as e:
            log.warning(f"[scheduler] cleanup error: {e}")

