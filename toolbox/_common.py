"""Shared utilities and constants for built-in tools."""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import config_layout
from clonoth_runtime import classify_path, load_policy_config, parse_extra_roots
from engine.project_root import foreign_worktrees_root
from platforms.protection import secret_env_names

from . import process_tree
from .context import ToolContext


_READ_CHUNK_BYTES = 64 * 1024
# 杀掉进程组后管道应当很快关上；漏网的孙进程还攥着管道时最多再等这么久，别为它卡死。
_KILL_GRACE_SEC = 5.0
# 按预算截取输出时开头占的比例：报错和总结通常在结尾，结尾多留
OUTPUT_HEAD_SHARE = 0.4


def kill_process_group(proc: "asyncio.subprocess.Process") -> None:
    """Kill the process and everything it started, without waiting.

    proc.kill() only kills the direct child (shell); grandchildren (e.g. a PM2 daemon)
    inherit the pipe fds and keep the reader waiting for EOF. Processes are started with
    start_new_session=True, so on POSIX the child's pid is its process group id. On Windows
    taskkill /T walks the tree from the still-running shell in a background thread, with a
    handle held so the pid cannot be reused meanwhile.
    """
    if os.name == "nt":
        pinned = _pin_running(proc)
        if pinned is None:
            _kill_direct(proc)
            return
        threading.Thread(target=pinned.kill_tree_and_close, name="kill-process-tree", daemon=True).start()
        return
    _kill_posix_group(proc)


async def terminate_process_tree(proc: "asyncio.subprocess.Process") -> None:
    """Like kill_process_group, but on Windows waits until taskkill has finished."""
    if os.name == "nt":
        pinned = _pin_running(proc)
        if pinned is not None:
            # 句柄由线程停完之后自己放：这里被取消了也不会提前放掉 PID
            await asyncio.to_thread(pinned.kill_tree_and_close)
        _kill_direct(proc)
        return
    _kill_posix_group(proc)


def _kill_posix_group(proc: "asyncio.subprocess.Process") -> None:
    try:
        # 组号就是子进程的 pid；getpgid 在子进程已被收走、pid 换了人时会查到别人的组
        os.killpg(proc.pid, signal.SIGKILL)
        return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    _kill_direct(proc)


def _kill_direct(proc: "asyncio.subprocess.Process") -> None:
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


def _pin_running(proc: "asyncio.subprocess.Process") -> "process_tree.PinnedProcess | None":
    if proc.returncode is not None:
        return None
    try:
        return process_tree.pin(proc.pid)
    except OSError:
        return None


@dataclass(frozen=True)
class StreamCapture:
    """一路输出留下来的开头和结尾。dropped 是夹在中间没留的字节数，dropped_newlines 是其中的换行数。

    只留开头时 tail 为空，dropped 就是开头之后的全部；head 和 tail 之间 dropped 为 0 时两段是连着的。"""

    head: bytes = b""
    tail: bytes = b""
    dropped: int = 0
    dropped_newlines: int = 0

    @property
    def overflow(self) -> bool:
        return self.dropped > 0

    @property
    def data(self) -> bytes:
        """开头和结尾直接接上；中间丢过东西时要用 join_outputs 才看得出缺口在哪。"""
        return self.head + self.tail


@dataclass
class ProcessOutcome:
    """一次子进程运行的结果。超出保留上限的输出边读边扔，只记下丢了多少。"""

    returncode: int | None
    stdout_capture: StreamCapture = field(default_factory=StreamCapture)
    stderr_capture: StreamCapture = field(default_factory=StreamCapture)
    timed_out: bool = False
    cancelled: bool = False

    @property
    def stdout(self) -> bytes:
        return self.stdout_capture.data

    @property
    def stderr(self) -> bytes:
        return self.stderr_capture.data

    @property
    def stdout_overflow(self) -> bool:
        return self.stdout_capture.overflow

    @property
    def stderr_overflow(self) -> bool:
        return self.stderr_capture.overflow


def _incomplete_utf8_suffix(data: bytes) -> int:
    """data 末尾被截成半个的 UTF-8 字符占几个字节；末尾完整时是 0。"""
    for back in range(1, min(4, len(data)) + 1):
        byte = data[-back]
        if byte & 0xC0 == 0x80:
            continue
        if byte < 0x80:
            return 0
        if byte & 0xE0 == 0xC0:
            need = 2
        elif byte & 0xF0 == 0xE0:
            need = 3
        elif byte & 0xF8 == 0xF0:
            need = 4
        else:
            return 0
        return back if need > back else 0
    return 0


def _leading_continuations(data: bytes) -> int:
    count = 0
    while count < min(3, len(data)) and data[count] & 0xC0 == 0x80:
        count += 1
    return count


async def _drain(stream: asyncio.StreamReader | None, head_limit: int, tail_limit: int = 0) -> StreamCapture:
    """读到 EOF，留前 head_limit 和后 tail_limit 字节。中间的照读照扔：不读走的话子进程写满管道就会卡住。

    中间丢过东西时，开头末尾和结尾开头被截成半个的 UTF-8 字符也算进丢掉的部分，两段各自都能干净地解码。"""
    if stream is None:
        return StreamCapture()
    head = bytearray()
    tail = bytearray()
    dropped = 0
    dropped_newlines = 0
    while True:
        chunk = await stream.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        room = head_limit - len(head)
        if room > 0:
            head.extend(chunk[:room])
            chunk = chunk[room:]
        if not chunk:
            continue
        if tail_limit <= 0:
            dropped += len(chunk)
            dropped_newlines += chunk.count(b"\n")
            continue
        tail.extend(chunk)
        excess = len(tail) - tail_limit
        if excess > 0:
            dropped += excess
            dropped_newlines += tail.count(b"\n", 0, excess)
            del tail[:excess]
    if dropped and tail_limit > 0:
        cut = _incomplete_utf8_suffix(head)
        if cut:
            del head[-cut:]
            dropped += cut
        skip = _leading_continuations(tail)
        if skip:
            del tail[:skip]
            dropped += skip
    return StreamCapture(bytes(head), bytes(tail), dropped, dropped_newlines)


@dataclass(frozen=True)
class _Gap:
    """拼好的输出里一处「读取时丢了中间一段」的标记：在文本里的起止位置，以及丢了多少字节、几个换行。"""

    start: int
    end: int
    dropped: int
    newlines: int


@dataclass(frozen=True)
class JoinedOutput:
    """stdout 后接 stderr 拼成的文本；读取时丢过的地方换成了一行标记，gaps 记着这些标记的位置。"""

    text: str
    gaps: tuple[_Gap, ...] = ()

    @property
    def dropped(self) -> int:
        return sum(gap.dropped for gap in self.gaps)


def _gap_marker(dropped: int, newlines: int) -> str:
    return f"[……输出太长，中间 {dropped} 字节（{newlines} 行）没有保留；要看全部就把输出重定向到文件再分段读……]"


def join_outputs(stdout: StreamCapture, stderr: StreamCapture) -> JoinedOutput:
    """stdout 在前、stderr 在后，中间隔一个换行；stderr 为空时只有 stdout。按 UTF-8 解码，坏字节换成替换符。"""
    parts: list[str] = []
    gaps: list[_Gap] = []
    length = 0

    def append(capture: StreamCapture) -> None:
        nonlocal length
        if not capture.dropped:
            piece = capture.data.decode("utf-8", errors="replace")
            parts.append(piece)
            length += len(piece)
            return
        head = capture.head.decode("utf-8", errors="replace")
        parts.append(head)
        length += len(head)
        prefix = "" if not head or head.endswith("\n") else "\n"
        marker = prefix + _gap_marker(capture.dropped, capture.dropped_newlines) + "\n"
        gaps.append(_Gap(length, length + len(marker), capture.dropped, capture.dropped_newlines))
        parts.append(marker)
        length += len(marker)
        tail = capture.tail.decode("utf-8", errors="replace")
        parts.append(tail)
        length += len(tail)

    append(stdout)
    if stderr.head or stderr.tail or stderr.dropped:
        parts.append("\n")
        length += 1
        append(stderr)
    return JoinedOutput("".join(parts), tuple(gaps))


def _budget_marker(budget: int, chars: int, lines: int, dropped: int, dropped_lines: int) -> str:
    extra = f"；读取时另丢了 {dropped} 字节（{dropped_lines} 行）" if dropped else ""
    return f"[……按 {budget} 字符截取，中间省略了 {chars} 字符（{lines} 行）{extra}……]"


def fit_output(output: JoinedOutput, budget: int, *, head_share: float = OUTPUT_HEAD_SHARE) -> tuple[str, int]:
    """超过 budget 字符时只留开头约 head_share、结尾其余的部分，尽量在行边界切开，中间换成一行说明。

    返回 (文本, 省略的字符数)；没超时原样返回、省略数为 0。读取时丢过的标记落进省略部分的，丢的量并进说明里。"""
    text = output.text
    if budget <= 0 or len(text) <= budget:
        return text, 0
    # 说明里每个数都按可能的最大值占位；读取时丢掉的行数可以比留下的字符还多
    dropped_lines = max(len(text), sum(gap.newlines for gap in output.gaps))
    reserve = len(_budget_marker(budget, len(text), len(text), output.dropped, dropped_lines)) + 2
    body = max(2, budget - reserve)
    head_budget = max(1, int(body * head_share))
    tail_budget = max(1, body - head_budget)
    newline = text.rfind("\n", 0, head_budget)
    head_end = newline + 1 if newline + 1 >= head_budget // 2 else head_budget
    window = len(text) - tail_budget
    newline = text.find("\n", window)
    tail_start = newline + 1 if newline != -1 and len(text) - newline - 1 >= tail_budget // 2 else window
    tail_start = max(tail_start, head_end)
    for gap in output.gaps:
        if gap.start < head_end < gap.end:
            head_end = gap.start
        if gap.start < tail_start < gap.end:
            tail_start = gap.end
    tail_start = max(tail_start, head_end)
    inside = [gap for gap in output.gaps if head_end <= gap.start and gap.end <= tail_start]
    marker_chars = sum(gap.end - gap.start for gap in inside)
    marker_newlines = sum(text.count("\n", gap.start, gap.end) for gap in inside)
    omitted = tail_start - head_end - marker_chars
    lines = text.count("\n", head_end, tail_start) - marker_newlines
    marker = _budget_marker(
        budget, omitted, lines, sum(gap.dropped for gap in inside), sum(gap.newlines for gap in inside),
    )
    head = text[:head_end]
    if head and not head.endswith("\n"):
        head += "\n"
    return f"{head}{marker}\n{text[tail_start:]}", omitted


async def _feed_and_collect(
    proc: "asyncio.subprocess.Process", stdin: bytes | None, readers: "asyncio.Future[Any]",
) -> Any:
    if proc.stdin is not None:
        try:
            if stdin:
                proc.stdin.write(stdin)
                await proc.stdin.drain()
        except OSError:
            # 子进程没读完输入就退出了（断管道、连接重置，Windows 上还有管道正在关闭）：照常收它的输出和退出码。
            pass
        finally:
            proc.stdin.close()
    outputs = await readers
    await proc.wait()
    return outputs


async def _cancel_requested(probe: Callable[[], Awaitable[bool]] | None) -> bool:
    if probe is None:
        return False
    try:
        return bool(await probe())
    except Exception:
        return False


async def run_process(
    proc: "asyncio.subprocess.Process",
    *,
    timeout_sec: float,
    keep_stdout: int,
    keep_stderr: int,
    tail_stdout: int = 0,
    tail_stderr: int = 0,
    stdin: bytes | None = None,
    is_cancelled: Callable[[], Awaitable[bool]] | None = None,
    poll_interval: float = 0.2,
) -> ProcessOutcome:
    """等进程跑完、收齐输出：每一路留开头 keep_* 字节，tail_* 大于 0 时再留结尾这么多字节。

    超时、任务被取消，以及等待本身被取消（CancelledError）时都会杀掉整个进程组，
    所以调用方须以 start_new_session=True 启动进程。
    """
    readers = asyncio.gather(
        _drain(proc.stdout, keep_stdout, tail_stdout), _drain(proc.stderr, keep_stderr, tail_stderr),
    )
    finished = asyncio.ensure_future(_feed_and_collect(proc, stdin, readers))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(float(timeout_sec), 0.0)

    async def stop(**flags: bool) -> ProcessOutcome:
        await terminate_process_tree(proc)
        try:
            stdout, stderr = await asyncio.wait_for(asyncio.shield(finished), timeout=_KILL_GRACE_SEC)
        except Exception:
            stdout, stderr = StreamCapture(), StreamCapture()
        return ProcessOutcome(proc.returncode, stdout, stderr, **flags)

    try:
        while True:
            done, _pending = await asyncio.wait({finished}, timeout=poll_interval)
            if finished in done:
                stdout, stderr = finished.result()
                return ProcessOutcome(proc.returncode, stdout, stderr)
            if await _cancel_requested(is_cancelled):
                return await stop(cancelled=True)
            if loop.time() >= deadline:
                return await stop(timed_out=True)
    finally:
        if proc.returncode is None:
            kill_process_group(proc)
        for pending in (finished, readers):
            if not pending.done():
                pending.cancel()


_SENSITIVE_ENV_KEYS_UPPER = {
    # Bearer for the whole admin API, policy included: a tool holding it could approve itself.
    "CLONOTH_ADMIN_TOKEN",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
}


# 隔离任务里起的命令看这两个变量找原仓库和它的虚拟环境：worktree 里没有 .venv、node_modules 和被忽略的文件
ORIGIN_ROOT_ENV = "CLONOTH_ORIGIN_ROOT"
ORIGIN_VENV_ENV = "CLONOTH_ORIGIN_VENV"
_ISOLATION_ENV_KEYS_UPPER = frozenset({ORIGIN_ROOT_ENV, ORIGIN_VENV_ENV})
_VENV_DIR_NAMES = (".venv", "venv")
# 往上找虚拟环境最多走几层；到了有 .git 的那一层（仓库根）就停
_VENV_SEARCH_DEPTH = 8


def safe_subprocess_env(*, project_origin: Path | str | None = None) -> dict[str, str]:
    """Return a subprocess env with common secret variables stripped.

    This is not a complete security boundary, but it meaningfully reduces
    accidental leakage from command execution. Every child process a tool, an edit
    check or a resident process starts gets its env here; project_origin is given for
    a task isolated in a git worktree (see isolation_env).
    """
    env = os.environ.copy()
    platform_secrets = secret_env_names()
    for k in list(env.keys()):
        ku = k.upper()

        if ku in _ISOLATION_ENV_KEYS_UPPER:
            env.pop(k, None)
            continue
        if ku in _SENSITIVE_ENV_KEYS_UPPER:
            env.pop(k, None)
            continue
        if ku.endswith("_API_KEY"):
            env.pop(k, None)
            continue
        # Declared by platforms/; a tool that needs one reads data/platform_secrets.yaml or .env itself.
        if ku in platform_secrets:
            env.pop(k, None)
            continue
    env.update(isolation_env(project_origin))
    return env


def _venv_in(directory: Path) -> Path | None:
    for name in _VENV_DIR_NAMES:
        candidate = directory / name
        if (candidate / "pyvenv.cfg").is_file():
            return candidate
    return None


def origin_venv(origin: Path | str) -> Path | None:
    """原仓库的虚拟环境：从项目根对应的目录往上找到仓库根为止，找不到时，本进程跑在这个仓库里的虚拟环境上就用它。"""
    try:
        start = Path(origin).resolve()
    except (OSError, RuntimeError):
        return None
    top: Path | None = None
    current = start
    for _ in range(_VENV_SEARCH_DEPTH):
        found = _venv_in(current)
        if found is not None:
            return found
        if (current / ".git").exists():
            top = current
            break
        if current.parent == current:
            break
        current = current.parent
    if sys.prefix == sys.base_prefix:
        return None
    try:
        own = Path(sys.prefix).resolve()
    except (OSError, RuntimeError):
        return None
    boundary = top or start
    return own if own == boundary or boundary in own.parents else None


def isolation_env(project_origin: Path | str | None) -> dict[str, str]:
    """隔离任务子进程多带的环境变量：原仓库里对应项目根的目录，以及原仓库的虚拟环境（找得到时）。不是隔离任务给空表。"""
    if not project_origin:
        return {}
    origin = Path(project_origin)
    env = {ORIGIN_ROOT_ENV: str(origin)}
    venv = origin_venv(origin)
    if venv is not None:
        env[ORIGIN_VENV_ENV] = str(venv)
    return env


def display_namer(root: Path, project_root: Path) -> Callable[[str], str]:
    """root 下的相对路径（/ 分隔，空串是 root 本身）在结果里怎么写：项目根里的写项目根相对路径，之外的写绝对路径。

    模型拿结果里的相对路径再调文件工具时，那边也按项目根解析。"""
    # root 已经解析到底；共享组配置、多开实例的代码目录经受信链接通到外面，只算一次它在项目根里叫什么。
    base = config_layout.workspace_logical_path(project_root, root)

    def name(walk_rel: str) -> str:
        if base is None:
            return (root / walk_rel).as_posix() if walk_rel else root.as_posix()
        if not walk_rel:
            return base
        return walk_rel if base == "." else f"{base}/{walk_rel}"

    return name


def _load_allowed_extra_roots(workspace_root: Path) -> list[Path]:
    """Load extra_roots from policy.yaml for defense in depth path checks."""
    data = load_policy_config(workspace_root)
    return parse_extra_roots(
        workspace_root,
        data.get("extra_roots") if isinstance(data, dict) else None,
    )


def _foreign_worktrees_root(workspace_root: Path) -> Path | None:
    try:
        return foreign_worktrees_root(workspace_root)
    except (OSError, RuntimeError, ValueError):
        return None


def resolve_and_classify(
    workspace_root: Path,
    path_str: str,
    *,
    project_root: Path | None = None,
    project_origin: Path | None = None,
) -> tuple[Path, bool]:
    """Resolve path and classify as internal (False) or external (True).

    Relative paths resolve under project_root when given, else under workspace_root;
    extra_roots always come from the instance policy under workspace_root. A task isolated
    in a git worktree passes project_origin: its files are judged as the same place in the
    original repository, the way the supervisor judges them. Other tasks' worktrees are external.

    Returns (resolved_path, is_external).
    Raises ValueError for invalid or escaping relative paths.
    """
    extra_roots = _load_allowed_extra_roots(workspace_root)
    resolved, display, is_external = classify_path(
        workspace_root, extra_roots, path_str,
        project_root=project_root,
        project_origin=project_origin,
        foreign_root=_foreign_worktrees_root(workspace_root),
    )
    if resolved is None:
        raise ValueError(display)
    return resolved, is_external


def resolve_under_allowed_roots(
    workspace_root: Path,
    path_str: str,
    *,
    project_root: Path | None = None,
    project_origin: Path | None = None,
) -> Path:
    """Resolve path under workspace_root, project_root or policy-configured extra_roots.

    Allows untrusted external absolute paths (policy layer enforces approval).
    """
    resolved, _is_external = resolve_and_classify(
        workspace_root, path_str, project_root=project_root, project_origin=project_origin,
    )
    return resolved


async def request_guard(
    ctx: ToolContext,
    op: str,
    parameters: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Request supervisor policy decision and wait for approval when needed.

    Returns (op_response, error_result).
    error_result is None when the operation may proceed.
    """
    op_res = await ctx.request_op(op, parameters)
    safety_level = str(op_res.get("safety_level") or "")

    if await ctx.check_cancelled():
        return op_res, {"ok": False, "error": "task cancelled", "cancelled": True}

    if safety_level == "deny":
        return op_res, {"ok": False, "error": op_res.get("reason", "denied")}

    if safety_level == "approval_required":
        approval_id = op_res.get("approval_id")
        approval = await ctx.wait_for_approval(approval_id)
        if approval.get("status") == "cancelled":
            return op_res, {"ok": False, "error": "task cancelled", "approval_id": approval_id, "cancelled": True}
        if approval.get("status") != "allowed":
            return op_res, {"ok": False, "error": "user denied approval", "approval_id": approval_id}

    if await ctx.check_cancelled():
        return op_res, {"ok": False, "error": "task cancelled", "cancelled": True}

    return op_res, None


async def guard_external_read(
    ctx: ToolContext,
    is_external: bool,
    rel_path: str,
    tool_name: str,
    reason: str = "",
) -> dict[str, Any] | None:
    """Request approval for external paths. Returns error dict or None (proceed)."""
    if not is_external:
        return None
    _op, err = await request_guard(
        ctx, "read_file",
        {"path": rel_path, "tool_name": tool_name,
         "reason": reason or f"{tool_name} on external path: {rel_path}"},
    )
    if err is not None:
        return {
            "success": False,
            "cancelled": err.get("cancelled", False),
            "error": err.get("error", "denied"),
        }
    return None
