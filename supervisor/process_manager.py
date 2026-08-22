from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from clonoth_runtime import get_bool, get_float, get_int, load_runtime_config


def _timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _make_child_preexec():
    """返回 preexec_fn：Linux 上设置 PR_SET_PDEATHSIG，父进程退出时子进程自动收到 SIGTERM。"""
    if sys.platform == "linux":
        def _set_pdeathsig():
            try:
                import ctypes
                import ctypes.util
                libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
                PR_SET_PDEATHSIG = 1
                libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
            except Exception:
                pass
        return _set_pdeathsig
    return None


@dataclass
class ManagedProcess:
    name: str
    popen: subprocess.Popen
    log_path: Path | None
    started_at: float = 0.0


@dataclass
class WorkerHealth:
    """一个 worker 反复起不来时的记账。给看门狗和运行页共用。"""

    failures: int = 0
    next_retry_at: float = 0.0
    last_exit_code: int | None = None
    last_log: str = ""
    given_up: bool = False
    respawns: int = 0


class ProcessManager:
    """管理引擎 worker 和 CLI 适配器进程。"""

    def __init__(self, *, supervisor_url: str, workspace_root: Path, log_dir: Path, log_func=None) -> None:
        self.supervisor_url = supervisor_url
        self.workspace_root = workspace_root
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.engines: list[ManagedProcess] = []
        self.shell_cli: ManagedProcess | None = None
        self._stopped = False
        self._restart_pending = False
        self._restarting_engine = False  # restart engine 期间抑制信号退出
        self._health: dict[str, WorkerHealth] = {}
        self._health_lock = threading.RLock()
        self._watchdog: threading.Thread | None = None
        self._on_event = None

        # 日志函数：写日志文件而不是终端（终端留给 TUI）
        self._log = log_func or (lambda msg: print(msg))

        runtime_cfg = load_runtime_config(workspace_root)

        self.stop_wait_timeout_sec = get_float(
            runtime_cfg,
            "supervisor.process_manager.stop_wait_timeout_sec",
            5.0,
            min_value=0.1,
            max_value=30.0,
        )
        self.engine_workers = get_int(
            runtime_cfg,
            "supervisor.process_manager.engine_workers",
            2,
            min_value=1,
            max_value=8,
        )

        # 默认 False：TUI 在原终端前台运行（supervisor 输出已重定向到日志文件）
        platform_default_new_console = False
        cfg_new_console = runtime_cfg.get("supervisor", {}).get("process_manager", {}).get("shell_new_console", None)
        shell_new_console = get_bool(runtime_cfg, "supervisor.process_manager.shell_new_console", platform_default_new_console)
        if cfg_new_console is None:
            shell_new_console = platform_default_new_console

        env_raw = os.getenv("CLONOTH_SHELL_NEW_CONSOLE", "").strip().lower()
        if env_raw in {"1", "true", "yes"}:
            shell_new_console = True
        elif env_raw in {"0", "false", "no"}:
            shell_new_console = False

        self.shell_new_console = shell_new_console
        # shell TUI 不是核心组件，默认不自动拉起；config 可开启，
        # env CLONOTH_SPAWN_SHELL_CLI 覆盖 config（1/true/yes to
        # enable, 0/false/no to disable).
        # Purpose: production no longer crashes on the optional shell TUI.
        self.spawn_shell_cli = get_bool(runtime_cfg, "supervisor.process_manager.spawn_shell_cli", False)
        _spawn_shell_env = os.getenv("CLONOTH_SPAWN_SHELL_CLI", "").strip().lower()
        if _spawn_shell_env in {"1", "true", "yes"}:
            self.spawn_shell_cli = True
        elif _spawn_shell_env in {"0", "false", "no"}:
            self.spawn_shell_cli = False

        # 注册清理：supervisor 退出时杀掉所有子进程
        atexit.register(self._cleanup)
        self._install_signal_handlers()

    def _install_signal_handlers(self) -> None:
        """拦截 SIGTERM/SIGINT，先清理子进程再退出。"""
        for sig in (signal.SIGTERM, signal.SIGINT):
            prev_handler = signal.getsignal(sig)

            def _handler(signum, frame, _prev=prev_handler):
                import traceback as _tb
                try:
                    _sname = signal.Signals(signum).name
                except (ValueError, AttributeError):
                    _sname = str(signum)
                _tname = threading.current_thread().name
                # restart engine 期间信号泄漏到 supervisor，忽略不退出
                if self._restarting_engine:
                    return
                self._cleanup()
                # 调用原有的 handler
                if callable(_prev) and _prev not in (signal.SIG_IGN, signal.SIG_DFL):
                    _prev(signum, frame)
                else:
                    sys.exit(0)

            try:
                signal.signal(sig, _handler)
            except (OSError, ValueError):
                # 非主线程中无法设置信号处理
                pass

    def _cleanup(self) -> None:
        """清理所有子进程。可重入安全。"""
        if self._stopped:
            return
        self._stopped = True
        self._log("[process_manager] cleaning up child processes...")
        self.stop_all()

    def _spawn(
        self,
        *,
        name: str,
        module: str,
        extra_args: list[str] | None = None,
        capture_output: bool = True,
        new_console: bool = False,
    ) -> ManagedProcess:
        log_path = self.log_dir / f"{name}-{_timestamp()}.log" if capture_output else None
        stdout = None
        stderr = None
        if capture_output and log_path:
            log_f = log_path.open("a", encoding="utf-8")
            stdout = log_f
            stderr = subprocess.STDOUT

        # cwd 是工作区而非代码目录，而 -m 只把 cwd 加进 sys.path：不补这一条，
        # 工作区与代码分离时子进程连自己的包都 import 不到。
        repo_root = str(Path(__file__).resolve().parents[1])
        env = {
            **os.environ,
            "CLONOTH_SUPERVISOR_URL": self.supervisor_url,
            "PYTHONPATH": os.pathsep.join(
                p for p in (repo_root, os.environ.get("PYTHONPATH", "")) if p
            ),
        }

        cmd = [sys.executable, "-m", module, "--supervisor", self.supervisor_url]
        if extra_args:
            cmd.extend(extra_args)

        creationflags = 0
        preexec_fn = None
        if sys.platform == "win32" and new_console:
            creationflags = subprocess.CREATE_NEW_CONSOLE
        elif sys.platform != "win32":
            # Linux/macOS: 父进程退出时子进程自动收到 SIGTERM
            preexec_fn = _make_child_preexec()

        p = subprocess.Popen(
            cmd,
            cwd=str(self.workspace_root),
            env=env,
            stdout=stdout,
            stderr=stderr,
            creationflags=creationflags,
            preexec_fn=preexec_fn,
            start_new_session=True,  # engine 独立进程组，杀 engine 不波及 supervisor
        )
        self._log(f"[process_manager] spawned {name} pid={p.pid}")
        return ManagedProcess(name=name, popen=p, log_path=log_path, started_at=time.time())

    def engine_names(self) -> list[str]:
        return [f"engine-{i}" for i in range(1, self.engine_workers + 1)]

    # 下面四个都跟看门狗抢 self.engines：主动停的那一刻它正好巡检，会把停掉的
    # 当成崩溃补回来。共用一把锁，让整个停/起过程对它不可分割。
    def start_engine(self) -> None:
        with self._health_lock:
            self._start_engine_locked()

    def _start_engine_locked(self) -> None:
        self._stopped = False
        self.engines = [proc for proc in self.engines if proc.popen.poll() is None]
        # 按缺哪个补哪个，不按数量补：engine-1 死了而 engine-2 还活着时，
        # 数着补会再造一个也叫 engine-2 的出来，两个 worker 抢同一个 id 注册。
        alive = {proc.name for proc in self.engines}
        for name in self.engine_names():
            if name in alive:
                continue
            self.engines.append(self._spawn(
                name=name,
                module="engine",
                capture_output=True,
                new_console=False,
                extra_args=["--worker-id", name],
            ))

    def start_shell_cli(self) -> None:
        if self.shell_cli and self.shell_cli.popen.poll() is None:
            return
        runtime_cfg = load_runtime_config(self.workspace_root)
        shell_mode = (runtime_cfg.get("shell", {}).get("mode", "") or "tui").strip().lower()
        module = "shell.tui" if shell_mode == "tui" else "shell.cli"
        name = "shell-tui" if shell_mode == "tui" else "shell-cli"

        self.shell_cli = self._spawn(
            name=name,
            module=module,
            capture_output=False,
            new_console=self.shell_new_console,
        )

    def stop_engine(self) -> None:
        with self._health_lock:
            self._stop_engine_locked()

    def _stop_engine_locked(self) -> None:
        for proc in list(self.engines):
            self._stop(proc)
        self.engines = []

    def stop_shell_cli(self) -> None:
        if self.shell_cli:
            self._stop(self.shell_cli)
            self.shell_cli = None

    def restart_engine(self) -> None:
        with self._health_lock:
            self._stop_engine_locked()
            # 人工重启：把上一轮的退避和停手状态一并清掉，否则刚重启就被退避挡住。
            for health in self._health.values():
                health.failures = 0
                health.next_retry_at = 0.0
                health.given_up = False
            self._start_engine_locked()

    def stop_all(self) -> None:
        with self._health_lock:
            self.stop_shell_cli()
            self._stop_engine_locked()

    # ================================================================
    #  看门狗
    # ================================================================
    # spawn 成功不等于 engine 可用 —— 它可能连上 supervisor 之前就退了。所以
    # 「活过这么久」才算一次成功，否则起来就崩会被记成健康。
    _STABLE_SEC = 20.0
    _POLL_SEC = 2.0
    _BACKOFF_BASE_SEC = 1.0
    _BACKOFF_CAP_SEC = 30.0
    _MAX_FAILURES = 5
    _TAIL_CHARS = 800

    def start_watchdog(self, on_event=None) -> None:
        """engine 掉了自动重拉。退避重试，连续失败就停手等人来看。

        on_event(kind, payload) 用来记事件；不给就只写日志。
        """
        if self._watchdog is not None:
            return
        self._on_event = on_event
        self._watchdog = threading.Thread(
            target=self._watchdog_loop, name="engine-watchdog", daemon=True,
        )
        self._watchdog.start()

    def worker_health(self) -> dict[str, dict]:
        """每个 worker 现在什么样。运行页读这个。"""
        with self._health_lock:
            alive = {proc.name: proc for proc in self.engines if proc.popen.poll() is None}
            return {
                name: {
                    "alive": name in alive,
                    "pid": alive[name].popen.pid if name in alive else None,
                    "uptime_sec": round(time.time() - alive[name].started_at, 1) if name in alive else 0.0,
                    "failures": health.failures,
                    "respawns": health.respawns,
                    "given_up": health.given_up,
                    "last_exit_code": health.last_exit_code,
                    "last_log": health.last_log,
                    "retry_in_sec": max(0.0, round(health.next_retry_at - time.time(), 1)),
                }
                for name, health in (
                    (n, self._health.setdefault(n, WorkerHealth())) for n in self.engine_names()
                )
            }

    def clear_given_up(self) -> None:
        """人工重试：把停手状态和退避一起清掉，下一轮巡检立刻重拉。"""
        with self._health_lock:
            for health in self._health.values():
                health.given_up = False
                health.failures = 0
                health.next_retry_at = 0.0

    def _tail_log(self, proc: ManagedProcess) -> str:
        if proc.log_path is None or not proc.log_path.exists():
            return ""
        try:
            text = proc.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return text[-self._TAIL_CHARS:].strip()

    def _watchdog_loop(self) -> None:
        while not self._stopped:
            time.sleep(self._POLL_SEC)
            # 关停和重启 engine 都会让进程短暂消失，这时候补一个上去是在跟它们打架。
            if self._stopped or self._restarting_engine:
                continue
            try:
                self._watchdog_tick()
            except Exception as exc:
                self._log(f"[watchdog] 巡检出错，下一轮继续：{exc}")

    def _watchdog_tick(self) -> None:
        now = time.time()
        with self._health_lock:
            for proc in list(self.engines):
                if proc.popen.poll() is None:
                    # 活过观察期才算这一次真的起来了。
                    health = self._health.setdefault(proc.name, WorkerHealth())
                    if health.failures and now - proc.started_at >= self._STABLE_SEC:
                        self._log(f"[watchdog] {proc.name} 稳住了，重置退避")
                        health.failures = 0
                        health.next_retry_at = 0.0
                    continue
                self._reap(proc, now)

            for name in self.engine_names():
                if any(p.name == name and p.popen.poll() is None for p in self.engines):
                    continue
                health = self._health.setdefault(name, WorkerHealth())
                if health.given_up or now < health.next_retry_at:
                    continue
                self._respawn(name, health)

    def _reap(self, proc: ManagedProcess, now: float) -> None:
        """已经退出的进程：记账、算退避、从列表里摘掉。"""
        self.engines.remove(proc)
        health = self._health.setdefault(proc.name, WorkerHealth())
        health.last_exit_code = proc.popen.returncode
        health.last_log = self._tail_log(proc)
        lasted = now - proc.started_at
        if lasted >= self._STABLE_SEC:
            # 跑了很久才退出，不算「起不来」，退避从头开始。
            health.failures = 1
        else:
            health.failures += 1
        # 头一次崩立刻拉起来：偶发的 OOM、瞬时异常不该白等。退避是留给「反复起不来」的。
        delay = 0.0 if health.failures <= 1 else min(
            self._BACKOFF_CAP_SEC,
            self._BACKOFF_BASE_SEC * (2 ** (health.failures - 2)),
        )
        health.next_retry_at = now + delay
        self._log(
            f"[watchdog] {proc.name} 退出（code={health.last_exit_code}，"
            f"活了 {lasted:.0f}s），第 {health.failures} 次，{delay:.0f}s 后重拉"
        )
        self._emit("engine_died", {
            "worker_id": proc.name,
            "exit_code": health.last_exit_code,
            "uptime_sec": round(lasted, 1),
            "failures": health.failures,
            "retry_in_sec": delay,
            "tail": health.last_log,
        })
        if health.failures >= self._MAX_FAILURES:
            health.given_up = True
            self._log(f"[watchdog] {proc.name} 连续 {health.failures} 次起不来，停手")
            self._emit("engine_gave_up", {
                "worker_id": proc.name,
                "failures": health.failures,
                "exit_code": health.last_exit_code,
                "tail": health.last_log,
            })

    def _respawn(self, name: str, health: WorkerHealth) -> None:
        try:
            proc = self._spawn(
                name=name, module="engine", capture_output=True,
                new_console=False, extra_args=["--worker-id", name],
            )
        except Exception as exc:
            health.failures += 1
            health.next_retry_at = time.time() + self._BACKOFF_CAP_SEC
            self._log(f"[watchdog] {name} 拉不起来：{exc}")
            return
        self.engines.append(proc)
        health.respawns += 1
        self._emit("engine_respawned", {
            "worker_id": name, "pid": proc.popen.pid,
            "failures": health.failures, "respawns": health.respawns,
        })

    def _emit(self, kind: str, payload: dict) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(kind, payload)
        except Exception as exc:
            self._log(f"[watchdog] 事件没记上：{exc}")

    def _stop(self, proc: ManagedProcess) -> None:
        p = proc.popen
        if p.poll() is not None:
            return
        try:
            p.terminate()
            p.wait(timeout=float(self.stop_wait_timeout_sec))
        except Exception:
            try:
                p.kill()
                p.wait(timeout=5)  # reap after SIGKILL to release ports
            except Exception:
                pass
        self._log(f"[process_manager] stopped {proc.name} pid={p.pid}")
