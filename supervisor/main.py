from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
import time
import os

import uvicorn
from fastapi import FastAPI

from clonoth_runtime import TASK_REAP_INTERVAL_SECONDS, process_workspace

from .api import create_app
from .instances import url_prefix
from .logging_setup import configure_supervisor_logging
from .config_store import ConfigStore
from .eventlog import EventLog, SYSTEM_SESSION_ID
from .policy import PolicyEngine
from .process_manager import ProcessManager
from .scheduler import SchedulerThread
from .state import SupervisorState
from .task_reaper import reap_stuck_tasks_locked


PORT_IN_USE_EXIT_CODE = 98

# -m 跑时 __name__ 是 __main__，看不出来源。
_logger = logging.getLogger("supervisor.main")


def main() -> None:
    # 要在读端口等参数之前：多实例靠各自工作区的 .env 给端口和密钥。
    workspace_root = process_workspace()

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

    data_dir = workspace_root / "data"
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # 整个进程的日志都进这份文件，终端留给前台的 TUI。
    log_level = configure_supervisor_logging(log_dir / "supervisor.log", args.log_level)

    # 在做任何状态变更之前，先检查端口是否可用
    # 如果端口被占用就立即退出，不会误删 restart_pending.json 或取消任务
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
            _s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            _s.bind((args.host, args.port))
    except OSError as e:
        _logger.error(
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
                _logger.info(f"[supervisor] Injected restart completion inbound for session {_sid}")
            else:
                _logger.warning(f"[supervisor] restart_pending.json missing conversation_key/channel, skipped")
        except Exception as e:
            _logger.exception(f"[supervisor] Failed to process restart_pending.json: {e}")
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

    def _housekeeping_loop() -> None:
        while True:
            time.sleep(TASK_REAP_INTERVAL_SECONDS)
            try:
                with state._lock:
                    reap_stuck_tasks_locked(state)
                    # 分支和 fresh/fork 子会话只标 reset 不删，不清的话 sessions.json 会一直涨
                    state._cleanup_stale_sessions_locked()
            except Exception:
                _logger.exception("[housekeeping] tick failed")

    threading.Thread(target=_housekeeping_loop, daemon=True, name="supervisor-housekeeping").start()

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
                _logger.info("[supervisor] TUI 已退出，正在重启...")
            else:
                _logger.info("[supervisor] TUI 已退出，正在关闭...")

            try:
                _pm.stop_engine()
            except Exception:
                pass

            # 退出码 75 = 请求重启（由 main.py 外层循环检测）
            _code = 75 if restart else 0
            _logger.info(f"[supervisor] exit code={_code}")
            import traceback as _tb
            _logger.info(f'[DIAG] _watch_shell os._exit({_code}) stack:\n{"" .join(_tb.format_stack())}')
            import time as _t; _t.sleep(0.5)  # 确保日志写出
            os._exit(_code)

        threading.Thread(target=_watch_shell, daemon=True, name="shell-watcher").start()

    _logger.info(f"[supervisor] starting uvicorn on {args.host}:{args.port}")
    # 用 Server API 替代 uvicorn.run()，禁用 uvicorn 的信号捕获。
    # uvicorn.run() 会安装自己的 SIGTERM/SIGINT handler 并在收到信号时
    # 调用 sys.exit(0)，导致 restart engine 时 supervisor 被意外杀死。
    import asyncio
    _uvi_config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        log_level=log_level,
        access_log=access_log,
        # 不让 uvicorn 自己配日志：它的 logger 往上传到进程那个 handler，格式和轮转都一样。
        log_config=None,
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
    _logger.info("[DIAG] uvicorn server exited! supervisor about to exit")


if __name__ == "__main__":
    main()
