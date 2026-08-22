from __future__ import annotations

import argparse
import logging
import socket
import atexit
import io
import signal
import sys
import threading
import time
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI

from workspace import resolve_workspace_root

from .api import create_app
from .instances import url_prefix
from .config_store import ConfigStore
from .eventlog import EventLog, SYSTEM_SESSION_ID
from .policy import PolicyEngine
from .process_manager import ProcessManager
from .scheduler import SchedulerThread
from .state import SupervisorState
from .types import TaskStatus


PORT_IN_USE_EXIT_CODE = 98


def main() -> None:
    # [2026-05-14] override=True: .env 文件值覆盖继承的环境变量。
    # 多实例部署时，每个实例读自己 cwd 下的 .env，互不干扰。
    load_dotenv(override=True)

    parser = argparse.ArgumentParser(description="Clonoth Supervisor")
    parser.add_argument("--host", default=os.getenv("CLONOTH_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("CLONOTH_PORT", "8765")))
    parser.add_argument("--no-shell", action="store_true", help="do not spawn shell runtime")
    parser.add_argument("--no-kernel", action="store_true", help="do not spawn kernel runtime")
    parser.add_argument("--no-workers", action="store_true", help="do not spawn any workers")
    parser.add_argument("--log-level", default=os.getenv("CLONOTH_LOG_LEVEL", "info"))
    parser.add_argument(
        "--access-log",
        action="store_true",
        help="enable uvicorn access log (VERY noisy because workers poll endpoints frequently)",
    )
    args = parser.parse_args()

    # 放在 load_dotenv 之后：多实例靠各自 cwd 的 .env 指定自己的工作区。
    workspace_root = resolve_workspace_root(Path(__file__).resolve().parents[1])
    data_dir = workspace_root / "data"
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # ---- 把 supervisor 自身的所有输出重定向到日志文件 ----
    # 这样 TUI 可以在同一终端前台运行，不会被 uvicorn 日志覆盖。
    _supervisor_log_path = log_dir / "supervisor.log"
    _supervisor_log_f = open(_supervisor_log_path, "a", encoding="utf-8", buffering=1)

    def _log(msg: str) -> None:
        """写 supervisor 日志（不写终端）。"""
        try:
            _supervisor_log_f.write(msg + "\n")
            _supervisor_log_f.flush()
        except Exception:
            pass

    # 在做任何状态变更之前，先检查端口是否可用
    # 如果端口被占用就立即退出，不会误删 restart_pending.json 或取消任务
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
            _s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            _s.bind((args.host, args.port))
    except OSError as e:
        _log(
            f"[supervisor] 端口 {args.host}:{args.port} 已被占用: {e}; "
            f"退出码={PORT_IN_USE_EXIT_CODE}，请先停止已有实例或改用 systemd 管理的唯一实例"
        )
        sys.exit(PORT_IN_USE_EXIT_CODE)

    events_path = data_dir / "events.jsonl"
    config_path = data_dir / "config.yaml"

    run_id = os.urandom(16).hex()

    eventlog = EventLog(events_path, run_id=run_id)
    policy = PolicyEngine(workspace_root=workspace_root)
    state = SupervisorState(workspace_root=workspace_root, eventlog=eventlog, policy=policy)

    config_store = ConfigStore(path=config_path)
    # 带图路由要问「当前渠道收不收图」，问的是同一个 store，不另开一份读盘。
    state.config_store = config_store
    state.write_boot_event()

    # [AutoC 2026-05-30] cancel_orphaned_tasks 已移除。
    # Why: 移除 EventLog 启动回放后，self.tasks 启动时为空，旧 task 不再被恢复。
    # How: 不再调用 cancel_orphaned_tasks，启动期清理改由 SupervisorState
    # 初始化阶段的 _reconcile_after_restart() 负责。
    # Purpose: 避免保留一个依赖旧回放路径且实际没有效果的启动步骤。

    # Check for pending restart completion → inject inbound_message (v3: self-awareness)
    _restart_pending_path = data_dir / "restart_pending.json"
    if _restart_pending_path.exists():
        try:
            import json as _rjson
            _pending = _rjson.loads(_restart_pending_path.read_text(encoding="utf-8"))
            _conv_key = _pending.get("conversation_key", "")
            _channel = _pending.get("channel", "")
            if _conv_key and _channel:
                _sid = state.get_or_create_session(channel=_channel, conversation_key=_conv_key)
                _evt = state.eventlog.append(
                    session_id=_sid,
                    component="supervisor",
                    type_="inbound_message",
                    payload={
                        "channel": _channel,
                        "conversation_key": _conv_key,
                        "text": "[系统通知] 全量重启已完成，新代码已生效。",
                    },
                )
                state.record_inbound_message_event(_evt)
                _log(f"[supervisor] Injected restart completion inbound for session {_sid}")
            else:
                _log(f"[supervisor] restart_pending.json missing conversation_key/channel, skipped")
        except Exception as e:
            _log(f"[supervisor] Failed to process restart_pending.json: {e}")
        finally:
            try:
                _restart_pending_path.unlink()
            except Exception:
                pass

    base_url = f"http://{args.host}:{args.port}"

    process_manager: ProcessManager | None = None
    if not args.no_workers:
        process_manager = ProcessManager(
            supervisor_url=base_url,
            workspace_root=workspace_root,
            log_dir=data_dir / "logs",
            log_func=_log,
        )
        if not args.no_kernel:
            process_manager.start_engine()

            # engine 崩了 supervisor 照样活着，systemd 看到的仍是 active running。
            # 没有这条看门狗，界面和进程表都不会有任何异常迹象。
            def _watchdog_event(kind: str, payload: dict) -> None:
                state.eventlog.append(
                    session_id=SYSTEM_SESSION_ID,
                    component="supervisor",
                    type_=kind,
                    payload=payload,
                )

            process_manager.start_watchdog(on_event=_watchdog_event)
        if not args.no_shell and process_manager.spawn_shell_cli:
            process_manager.start_shell_cli()

    scheduler = SchedulerThread(state=state, workspace_root=workspace_root)
    scheduler.start()

    # ---- 后台僵尸 task 回收线程 ----
    def _reap_zombie_tasks() -> None:
        """定期回收 lease 过期超过 grace period 的僵尸 task。

        跳过 session 中存在 pending approval 的 task，因为
        等审批期间 agent 是合法阻塞状态，不应被当作僵尸回收。
        """
        from datetime import datetime, timedelta, timezone
        from .types import ApprovalStatus
        _REAP_INTERVAL = 60.0
        _GRACE = timedelta(seconds=60)
        # [Zombie reap 2026-06-21] Why: a live heartbeat can keep a task in
        # running forever while it is logically stuck in a tool call (e.g.
        # create_schedule), causing QQ preempt/queue paths to target that old
        # task. How: cap absolute running wall-clock duration as a final safety
        # net. Purpose: over-old tasks are failed and routed normally.
        try:
            _MAX_RUNNING_SEC = max(300.0, float(os.getenv("CLONOTH_TASK_MAX_RUNNING_SECONDS", "21600")))
        except Exception:
            _MAX_RUNNING_SEC = 21600.0
        while True:
            time.sleep(_REAP_INTERVAL)
            try:
                now = datetime.now(timezone.utc)
                with state._lock:
                    # 预先收集有 pending approval 的 session 集合
                    _sessions_with_pending_approval: set[str] = set()
                    for a in state.approvals.values():
                        if a.status == ApprovalStatus.pending:
                            _sessions_with_pending_approval.add(a.session_id)

                    # [fix 2026-08-04] Why: 循环体内会调用 _route_completed_task_locked
                    # 与 _cleanup_stale_sessions_locked，它们可能增删 state.tasks，
                    # 直接遍历 state.tasks.values() 会抛
                    # "dictionary changed size during iteration"，导致本轮 reaping
                    # 提前中断、僵尸任务清不干净。How: 先对当前任务集合
                    # 做快照（list(...)）再遍历。Purpose: reaping 在锁内安全修改任务。
                    for task in list(state.tasks.values()):
                        if task.status != TaskStatus.running:
                            continue
                        # [Fork/Merge 2026-05-17] Why: approvals and user-visible
                        # events may now be attached to the parent route session
                        # while the running task itself is on a branch. How: check
                        # both runtime and route session before reaping. Purpose:
                        # approval waits are not killed because of branch routing.
                        route_session_id = state._route_session_id_for_task_locked(task)
                        if task.session_id in _sessions_with_pending_approval or route_session_id in _sessions_with_pending_approval:
                            continue

                        should_reap = False
                        reason = ""
                        if not task.lease_expires_at:
                            # [AutoC 2026-05-31] 兜底：running 且无 lease 超过 5 分钟视为僵尸。
                            # [Zombie reap 2026-06-21] Fix: do not fall through into
                            # `None + timedelta`; explicitly mark the reaping reason.
                            if task.updated_at + timedelta(minutes=5) < now:
                                should_reap = True
                                reason = "missing lease (zombie reaped by background)"
                        elif task.lease_expires_at + _GRACE < now:
                            should_reap = True
                            reason = "lease expired (zombie reaped by background)"

                        age_sec = (now - task.created_at).total_seconds()
                        if age_sec > _MAX_RUNNING_SEC:
                            should_reap = True
                            reason = f"max running age exceeded ({int(age_sec)}s > {int(_MAX_RUNNING_SEC)}s)"

                        if not should_reap:
                            continue

                        task.status = TaskStatus.failed
                        task.updated_at = now
                        task.lease_expires_at = None
                        task.result = {"action": "fail", "error": reason}
                        state._reset_task_route_state_locked(task)
                        state._event_task_snapshot("task_completed", task)
                        # [Fork/Merge 2026-05-17] Why: failing a branch task must
                        # still merge/cleanup the branch and emit a parent-routed
                        # error response. How: reuse the normal completion router
                        # after recording the task snapshot. Purpose: background
                        # zombie reaping follows the same terminal path as API reaping.
                        state._route_completed_task_locked(task)
                        _log(f"[zombie-reaper] reaped task {task.task_id[:12]} node={task.node_id} reason={reason}")
                    # [AutoC 2026-05-30] Why: branch 和 fresh/fork child session
                    # 历史上只标记 reset，不物理删除，sessions.json 会持续膨胀。
                    # How: 复用后台 zombie reaper 的定时锁内循环，每分钟执行一次
                    # stale registry 清理。Purpose: 旧遗留 reset 行和 24h 无活动的
                    # fresh/fork child 会话会被自动移除，accumulate 会话保留。
                    state._cleanup_stale_sessions_locked()
            except Exception as e:
                _log(f"[zombie-reaper] error: {e}")

    threading.Thread(target=_reap_zombie_tasks, daemon=True, name="zombie-reaper").start()

    app = create_app(
        state=state, process_manager=process_manager, config_store=config_store,
        host=args.host, port=args.port,
    )

    prefix = url_prefix()
    if prefix:
        # 多开时几个号共用一个域名，靠路径前缀分流：同源才能共用登录态，
        # WebSocket 也就不必经过任何代理。Mount 会自己剥前缀并设好 root_path。
        mounted = FastAPI()
        mounted.mount(prefix, app)
        app = mounted

    env_access_log = (os.getenv("CLONOTH_ACCESS_LOG") or "").strip().lower() in {"1", "true", "yes", "y"}
    access_log = bool(args.access_log or env_access_log)

    # TUI 子进程退出时自动关停 supervisor
    if process_manager and process_manager.shell_cli:
        _shell_proc = process_manager.shell_cli
        _pm = process_manager

        def _watch_shell() -> None:
            """后台线程：等待 TUI 进程退出，根据 restart 标记决定重启或退出。"""
            try:
                _shell_proc.popen.wait()
            except Exception:
                pass

            restart = _pm._restart_pending

            if restart:
                _log("[supervisor] TUI 已退出，正在重启...")
            else:
                _log("[supervisor] TUI 已退出，正在关闭...")

            try:
                _pm.stop_engine()
            except Exception:
                pass

            # 退出码 75 = 请求重启（由 main.py 外层循环检测）
            _code = 75 if restart else 0
            _log(f"[supervisor] exit code={_code}")
            import traceback as _tb
            _log(f'[DIAG] _watch_shell os._exit({_code}) stack:\n{"" .join(_tb.format_stack())}')
            import time as _t; _t.sleep(0.5)  # 确保日志写出
            os._exit(_code)

        threading.Thread(target=_watch_shell, daemon=True, name="shell-watcher").start()

    # uvicorn 日志全部写到文件，不输出到终端（终端留给 TUI）
    _uvi_log_cfg = {
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {
            "file": {
                "class": "logging.FileHandler",
                "filename": str(_supervisor_log_path),
                "mode": "a",
                "encoding": "utf-8",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["file"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": ["file"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["file"], "level": "INFO", "propagate": False},
        },
    }

    _log(f"[supervisor] starting uvicorn on {args.host}:{args.port}")
    # 用 Server API 替代 uvicorn.run()，禁用 uvicorn 的信号捕获。
    # uvicorn.run() 会安装自己的 SIGTERM/SIGINT handler 并在收到信号时
    # 调用 sys.exit(0)，导致 restart engine 时 supervisor 被意外杀死。
    import asyncio
    _uvi_config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        access_log=access_log,
        log_config=_uvi_log_cfg,
    )
    _uvi_server = uvicorn.Server(_uvi_config)
    _uvi_server.install_signal_handlers = lambda: None  # 禁用 uvicorn 信号捕获
    # 用 loop.run_until_complete 替代 asyncio.run()，后者会重装 SIGINT handler
    # 信号处理交给 process_manager._install_signal_handlers + _restarting_engine flag
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    try:
        _loop.run_until_complete(_uvi_server.serve())
    finally:
        _loop.close()
    _log("[DIAG] uvicorn server exited! supervisor about to exit")


if __name__ == "__main__":
    main()
