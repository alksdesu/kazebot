"""轻量定时调度器。

每分钟醒一次，扫描 data/schedules.yaml，到时间的往 inbound 队列注入消息。
调度线程挂了不影响 Supervisor 主进程。
"""
# 系统级定时任务通过 supervisor hook handler 接入，调度器只负责发 tick。

from __future__ import annotations

import contextlib
import copy
import json
import logging
import math
import os
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

import yaml

import config_layout
from clonoth_runtime import parse_bool
from engine.context_store import cleanup_old_contexts
from engine.conversation_migration import ConversationRekey, RekeyCount
from engine.cron import cron_faults, cron_match
from toolbox._common import safe_subprocess_env


if TYPE_CHECKING:
    from .state import SupervisorState

log = logging.getLogger(__name__)

# 记录创建平台之前，只有 QQ 适配器会写 created_by。
_LEGACY_CREATOR_PLATFORM = "qq"

_SCRIPT_TIMEOUT_DEFAULT_SEC = 30.0
_SCRIPT_TIMEOUT_MIN_SEC = 1.0
_SCRIPT_TIMEOUT_MAX_SEC = 3600.0
# 杀掉之后最多再等这么久收管道：自己另开会话逃出进程组的孙进程会一直攥着它。
_SCRIPT_REAP_GRACE_SEC = 5.0
_TICK_OFFSET_SEC = 1.0
# 内存里保留的已了结任务数；还在被活任务引用的不算在内
_KEEP_FINISHED_TASKS = 200


def _creator_fields(schedule: dict) -> dict[str, str]:
    return {
        "created_by": str(schedule.get("created_by") or ""),
        "created_platform": str(schedule.get("created_platform") or ""),
    }


# ---------------------------------------------------------------------------
#  schedules.yaml CRUD
# ---------------------------------------------------------------------------

# 相对工作区的位置。读不懂的原因用这个名字说：原因会交给模型、进日志，绝对路径不往外带。
SCHEDULES_FILE = "data/schedules.yaml"


def schedules_path(workspace_root: Path) -> Path:
    return Path(workspace_root) / SCHEDULES_FILE


def load_schedules(workspace_root: Path) -> list[dict[str, Any]]:
    """只读的调用方用：读不懂当成没有定时任务，调度线程不至于崩掉。同一份坏文件只记一次日志。"""
    path = schedules_path(workspace_root)
    try:
        return _read_schedule_file(path)
    except Exception as exc:
        _report_unreadable(path, exc)
        return []


def read_schedules(workspace_root: Path) -> list[dict[str, Any]]:
    """读完要接着改这份文件的调用方用：读不懂抛 ScheduleFileUnreadable，不当成没有定时任务。"""
    return _read_schedule_file(schedules_path(workspace_root))


def save_schedules(workspace_root: Path, schedules: list[dict[str, Any]]) -> None:
    path = schedules_path(workspace_root)
    with config_layout.locked(path):
        _write_schedule_file(path, schedules)


def update_schedules(
    workspace_root: Path, change: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """在锁里读、改、写回，返回改完的清单。scheduler、定时任务工具和迁移都改这份文件，各读各写会互相冲掉。"""
    return _update_schedule_file(schedules_path(workspace_root), change)


def schedules_lock(workspace_root: Path) -> contextlib.AbstractContextManager[None]:
    """整份改写定时任务文件的人拿这把锁，和 update_schedules 排队；控制台原文编辑器写入时拿的也是它。"""
    return config_layout.locked(schedules_path(workspace_root))


def check_schedules_text(text: str) -> list[str]:
    """控制台原文编辑器保存前的校验：结构不对抛 ValueError，调度时会被跳过的条目和永远不会触发的 cron 只提醒。"""
    try:
        data = config_layout.load_yaml_text(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML 解析失败：{config_layout.yaml_error_text(exc)}") from exc
    warnings: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(_schedule_entries(data, SCHEDULES_FILE), 1):
        if not isinstance(item, dict):
            warnings.append(f"第 {index} 条不是映射，调度时会跳过")
            continue
        sid = str(item.get("id") or "").strip()
        if not sid:
            warnings.append(f"第 {index} 条没有 id，调度时会跳过")
            continue
        if sid in seen:
            warnings.append(f"id {sid} 重复，同一分钟只有第一条会触发")
        seen.add(sid)
        warnings.extend(f"{sid} 的 cron {fault}" for fault in cron_faults(str(item.get("cron") or "")))
        if str(item.get("type") or "message").strip() == "script" and not str(item.get("command") or "").strip():
            warnings.append(f"{sid} 是 script 类型却没写 command，不会执行")
    return warnings


def rekey_schedules(path: Path, rekey: ConversationRekey, *, apply: bool) -> RekeyCount:
    """定时任务跟着会话走：到点按新会话键注入，旧键在平台的路由表里已经查不到真实会话。"""
    if not rekey or not Path(path).exists():
        return RekeyCount()
    moved = 0

    def change(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        nonlocal moved
        for item in items:
            new = rekey.key(item.get("conversation_key"))
            if new is not None:
                item["conversation_key"] = new
                moved += 1
        return items

    if apply:
        _update_schedule_file(path, change)
    else:
        change(_read_schedule_file(path))
    return RekeyCount(moved)


# 已经为哪个文件版本记过「读不懂」：调度线程每分钟都读，不去重的话坏文件每分钟刷一条。
_unreadable_stamps: dict[Path, tuple[int, int, int] | None] = {}
_unreadable_lock = threading.Lock()


def _report_unreadable(path: Path, exc: Exception) -> None:
    stamp = config_layout.file_stamp(path)
    with _unreadable_lock:
        if path in _unreadable_stamps and _unreadable_stamps[path] == stamp:
            return
        _unreadable_stamps[path] = stamp
    reason = str(exc) if isinstance(exc, ScheduleFileUnreadable) else f"{SCHEDULES_FILE} 读不懂：{type(exc).__name__}"
    log.warning("[scheduler] %s，修好之前一条定时任务都不会触发", reason)


def _schedule_entries(data: Any, where: str) -> list[Any]:
    """文件内容里的 schedules 列表，原样返回。空文件算没有定时任务。"""
    if data is None:
        return []
    if not isinstance(data, dict):
        raise ValueError(f"{where} 顶层要写成映射（schedules: [...]），现在是 {type(data).__name__}")
    items = data.get("schedules") or []
    if not isinstance(items, list):
        raise ValueError(f"{where} 里的 schedules 要写成列表，现在是 {type(items).__name__}")
    return items


class ScheduleFileUnreadable(ValueError):
    """定时任务文件在盘上却读不懂。消息只有位置和问题：条目里写着要发的话和要跑的命令，原文不能进日志、交给模型。"""


def _read_schedule_file(path: Path) -> list[dict[str, Any]]:
    """文件在但读不懂就抛 ScheduleFileUnreadable：读坏当成空表再写回去，全部定时任务就没了。"""
    if not path.exists():
        return []
    try:
        document = config_layout.load_yaml_text(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ScheduleFileUnreadable(f"{SCHEDULES_FILE} 读不懂：{config_layout.config_error_text(exc)}") from exc
    try:
        items = _schedule_entries(document, SCHEDULES_FILE)
    except ValueError as exc:
        raise ScheduleFileUnreadable(str(exc)) from exc
    return [item for item in items if isinstance(item, dict)]


def _write_schedule_file(path: Path, schedules: list[dict[str, Any]]) -> None:
    """调用方已经拿着 config_layout.locked(path)。"""
    config_layout.write_locked(path, yaml.safe_dump({"schedules": schedules}, sort_keys=False, allow_unicode=True))


def _update_schedule_file(
    path: Path, change: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    with config_layout.locked(path):
        current = _read_schedule_file(path)
        updated = change(copy.deepcopy(current))
        if updated != current:
            _write_schedule_file(path, updated)
        return updated


# ---------------------------------------------------------------------------
#  调度线程
# ---------------------------------------------------------------------------

def _until_next_minute(now: float) -> float:
    """睡到下一分钟开头稍后一点。固定睡 60 秒的话每轮自己的耗时会累积，迟早整整跳过一分钟，那一分钟的 cron 就不响了。"""
    return 60.0 - now % 60.0 + _TICK_OFFSET_SEC


def _script_timeout(raw: Any) -> float:
    """脚本的超时秒数。写坏的按默认，压在上下限之间：写错的一条不该一直占着一个线程。"""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _SCRIPT_TIMEOUT_DEFAULT_SEC
    if not math.isfinite(value) or value <= 0:
        return _SCRIPT_TIMEOUT_DEFAULT_SEC
    return min(max(value, _SCRIPT_TIMEOUT_MIN_SEC), _SCRIPT_TIMEOUT_MAX_SEC)


def _kill_tree(proc: subprocess.Popen) -> None:
    """连同脚本拉起的子孙一起杀：只杀 shell 的话，孙进程攥着管道，communicate 永远等不到头。"""
    if os.name == "nt":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=_SCRIPT_REAP_GRACE_SEC,
            )
    else:
        # start_new_session 让脚本自成一个进程组，组号就是它的 pid；shell 先退了，组里的孙进程照样杀得到。
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        proc.kill()


class SchedulerThread:
    """后台线程。每分钟开头检查一次 schedules.yaml，匹配的注入 inbound。"""

    def __init__(self, *, state: "SupervisorState", workspace_root: Path) -> None:
        self._state = state
        self._workspace_root = workspace_root
        self._last_fired: dict[str, str] = {}  # schedule_id -> "YYYY-MM-DD HH:MM"
        self._checked_crons: dict[str, str] = {}  # schedule_id -> 已经查过有没有永远不触发的项的 cron
        self._thread: threading.Thread | None = None
        # 还在跑的脚本任务，防重入；脚本在各自的线程里结束，所以要加锁。
        self._running_scripts: set[str] = set()
        self._scripts_lock = threading.Lock()

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
            time.sleep(_until_next_minute(time.time()))

    def _tick(self) -> None:
        now = datetime.now(timezone.utc)
        now_key = now.strftime("%Y-%m-%d %H:%M")

        for s in load_schedules(self._workspace_root):
            sid = str(s.get("id") or "").strip()
            if not sid or not parse_bool(s.get("enabled"), True):
                continue

            cron_expr = str(s.get("cron") or "").strip()
            self._report_cron_faults(sid, cron_expr)
            if not cron_expr:
                continue

            # 防止同一分钟重复触发
            if self._last_fired.get(sid) == now_key:
                continue

            if not cron_match(cron_expr, now):
                continue

            self._last_fired[sid] = now_key
            once = bool(parse_bool(s.get("once"), False))
            if str(s.get("type") or "message").strip() == "script":
                self._start_script(s, sid, once)
            elif self._fire_message(s, sid) and once:
                # 注入失败还照删，这条一次性提醒就再也不会响；留着等下次 cron 匹配重试。
                self._remove_schedule(sid)

        # handler 按 schedule_type 自己过滤，每种系统级定时特性 fire 一次。
        # 它们在 state 锁里同步跑，必须立即返回：慢了会拖住下一分钟的定时任务。
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

    def _report_cron_faults(self, sid: str, cron_expr: str) -> None:
        """每分钟都读一遍 schedules.yaml：同一条只在 cron 改过之后再查，写错的不至于每分钟刷一条日志。"""
        if self._checked_crons.get(sid) == cron_expr:
            return
        self._checked_crons[sid] = cron_expr
        faults = cron_faults(cron_expr)
        if faults:
            log.warning("[scheduler] 定时任务 %s 的 cron「%s」%s", sid, cron_expr, "；".join(faults))

    def _inject_inbound(
        self, sid: str, text: str, conv_key: str, entry_node_id: str = "",
        attachments: list | None = None, created_by: str = "", created_platform: str = "",
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
                    "platform": created_platform or _LEGACY_CREATOR_PLATFORM,
                    "user_id": created_by,
                    "is_admin": False,
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
        return self._inject_inbound(sid, text, conv_key, entry_node_id, **_creator_fields(s))

    def _start_script(self, s: dict, sid: str, once: bool) -> None:
        """脚本各跑各的线程：在调度线程里等它跑完，这期间到点的定时任务全得跟着等，等过了那一分钟就再也不响。"""
        with self._scripts_lock:
            if sid in self._running_scripts:
                log.warning("[scheduler] script %s: still running, skipping", sid)
                return
            self._running_scripts.add(sid)
        worker = threading.Thread(
            target=self._run_script, args=(copy.deepcopy(s), sid, once),
            daemon=True, name=f"scheduler-script:{sid}",
        )
        try:
            worker.start()
        except BaseException:
            self._finish_script(sid)
            raise

    def _run_script(self, s: dict, sid: str, once: bool) -> None:
        try:
            if self._fire_script(s, sid) and once:
                self._remove_schedule(sid)
        except Exception:
            log.exception("[scheduler] script %s: unexpected error", sid)
        finally:
            self._finish_script(sid)

    def _finish_script(self, sid: str) -> None:
        with self._scripts_lock:
            self._running_scripts.discard(sid)

    def _fire_script(self, s: dict, sid: str) -> bool:
        """type=script：执行脚本，stdout JSON 解析后注入 inbound。返回是否真的注入成功。"""
        command = str(s.get("command") or "").strip()
        if not command:
            log.error("[scheduler] script %s: missing 'command' field", sid)
            return False
        stdout = self._run_command(sid, command, _script_timeout(s.get("timeout")))
        if stdout is None:
            return False
        text_prefix = str(s.get("text") or "").strip()
        conv_key = str(s.get("conversation_key") or f"scheduler:{sid}").strip()
        entry_node_id = str(s.get("entry_node_id") or "").strip()
        stdout = stdout.strip()
        if not stdout:
            if parse_bool(s.get("silent"), True):
                log.debug("[scheduler] script %s: empty stdout, silent skip", sid)
                return False
            if not text_prefix:
                return False
            log.info("[scheduler] script %s: empty stdout, injecting prefix", sid)
            return self._inject_inbound(sid, text_prefix, conv_key, entry_node_id, **_creator_fields(s))
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as e:
            log.error("[scheduler] script %s: invalid JSON output: %s", sid, e)
            return False
        if not isinstance(data, dict):
            log.error("[scheduler] script %s: JSON output must be an object, got %s", sid, type(data).__name__)
            return False
        body_text = str(data.get("text") or "").strip()
        if not body_text:
            log.error("[scheduler] script %s: JSON missing 'text' field", sid)
            return False
        if text_prefix:
            body_text = text_prefix + "\n" + body_text
        attachments = data.get("attachments") or None
        return self._inject_inbound(
            sid, body_text, conv_key, entry_node_id, attachments, **_creator_fields(s),
        )

    def _run_command(self, sid: str, command: str, timeout: float) -> str | None:
        """跑一条脚本命令，成功返回 stdout；起不来、超时、非零退出都记日志返回 None。"""
        try:
            proc = subprocess.Popen(
                command, shell=True, cwd=str(self._workspace_root),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                encoding="utf-8", errors="replace",
                # 和 execute_command 一样剥掉模型密钥、管理令牌和平台令牌：命令是人写进文件的，跑在 supervisor 的身份下。
                env=safe_subprocess_env(),
                start_new_session=True,
            )
        except (OSError, ValueError) as e:
            log.error("[scheduler] script %s: failed to start: %s", sid, e)
            return None
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try:
                proc.communicate(timeout=_SCRIPT_REAP_GRACE_SEC)
            except subprocess.TimeoutExpired:
                log.warning("[scheduler] script %s: a child left the process group and still holds the pipes", sid)
            log.error("[scheduler] script %s: timed out after %gs, killed", sid, timeout)
            return None
        if stderr and stderr.strip():
            log.warning("[scheduler] script %s stderr: %s", sid, stderr.strip()[:500])
        if proc.returncode != 0:
            log.error("[scheduler] script %s exited with rc=%s", sid, proc.returncode)
            return None
        return stdout or ""

    def _remove_schedule(self, sid: str) -> None:
        try:
            update_schedules(
                self._workspace_root,
                lambda items: [s for s in items if str(s.get("id") or "").strip() != sid],
            )
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

            with self._state._lock:
                pruned = self._state._prune_finished_tasks_locked(_KEEP_FINISHED_TASKS)
            if pruned:
                log.info("[scheduler] pruned %d old tasks from memory", pruned)
        except Exception as e:
            log.warning(f"[scheduler] cleanup error: {e}")

