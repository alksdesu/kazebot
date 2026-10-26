"""工具运行时：子进程输出边读边扔，超时和取消连进程组一起杀，脚本输出先解析再截断，
工具抛异常只算这一次调用失败，git 和整树遍历不占事件循环，web_search 的子进程不会写满管道卡死。"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from toolbox._common import run_process, safe_subprocess_env  # noqa: E402
from toolbox.builtins import system as system_tool  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402
from toolbox.registry import ToolRegistry  # noqa: E402


async def _spawn(code: str, *, with_stdin: bool = False) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable, "-c", code,
        stdin=asyncio.subprocess.PIPE if with_stdin else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )


def _supervisor(*, cancelled: bool = False) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/ops/request":
            return httpx.Response(200, json={"safety_level": "allow"})
        if request.url.path.endswith("/cancelled"):
            return httpx.Response(200, json={"cancelled": cancelled})
        if request.url.path == "/v1/admin/restart":
            return httpx.Response(200, json={"scheduled": True, "target": "engine"})
        return httpx.Response(200, json={})

    return httpx.MockTransport(handle)


def _context(root: Path, http: httpx.AsyncClient, registry: ToolRegistry | None) -> ToolContext:
    return ToolContext(
        supervisor_url="http://sv",
        session_id="sess-1",
        run_id="run-1",
        worker_id="worker-1",
        workspace_root=root,
        http=http,
        registry=registry,
        task_id="task-1",
    )


def _registry(root: Path) -> ToolRegistry:
    return ToolRegistry(workspace_root=root, tools_dir=root / "tools")


async def _execute(
    registry: ToolRegistry, name: str, arguments: dict[str, Any] | None = None, *, cancelled: bool = False,
) -> dict[str, Any]:
    async with httpx.AsyncClient(transport=_supervisor(cancelled=cancelled)) as http:
        ctx = _context(registry.workspace_root, http, registry)
        return await registry.execute(name=name, arguments=arguments or {}, ctx=ctx)


def _write_script(root: Path, name: str, main: str, *, timeout_sec: float | None = None) -> None:
    lines = [f"SPEC = {{'name': {name!r}, 'description': 'probe', 'input_schema': {{'type': 'object', 'properties': {{}}}}}}"]
    if timeout_sec is not None:
        lines.append(f"TIMEOUT_SEC = {timeout_sec!r}")
    lines.append("if __name__ == '__main__':")
    lines.extend(f"    {line}" for line in textwrap.dedent(main).strip().splitlines())
    tools_dir = root / "tools"
    tools_dir.mkdir(exist_ok=True)
    (tools_dir / f"{name}.py").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _shell(*argv: str) -> str:
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


class TestRunProcess:
    @pytest.mark.asyncio
    async def test_output_past_the_cap_is_drained_rather_than_left_in_the_pipe(self) -> None:
        # 两路各 1MB，远超管道缓冲：只留前 1000 字节，但子进程得能写完正常退出。
        proc = await _spawn("import sys; sys.stdout.write('o' * 1_000_000); sys.stderr.write('e' * 1_000_000)")

        outcome = await run_process(proc, timeout_sec=60, keep_stdout=1000, keep_stderr=1000)

        assert outcome.returncode == 0
        assert outcome.timed_out is False
        assert outcome.stdout == b"o" * 1000
        assert outcome.stderr == b"e" * 1000
        assert outcome.stdout_overflow is True
        assert outcome.stderr_overflow is True

    @pytest.mark.asyncio
    async def test_input_reaches_the_process(self) -> None:
        proc = await _spawn("import sys; sys.stdout.write(sys.stdin.read().upper())", with_stdin=True)

        outcome = await run_process(proc, stdin=b"hello", timeout_sec=60, keep_stdout=100, keep_stderr=100)

        assert outcome.stdout == b"HELLO"
        assert outcome.stdout_overflow is False

    @pytest.mark.asyncio
    async def test_a_process_that_never_reads_its_input_is_still_collected(self) -> None:
        # 输入比管道缓冲大、子进程又不读：写 stdin 撞上断管道不能把整次调用变成异常。
        proc = await _spawn("print('done')", with_stdin=True)

        outcome = await run_process(proc, stdin=b"x" * 1_000_000, timeout_sec=60, keep_stdout=100, keep_stderr=100)

        assert outcome.returncode == 0
        assert outcome.stdout.strip() == b"done"

    @pytest.mark.asyncio
    async def test_a_timeout_kills_the_process(self) -> None:
        proc = await _spawn("import time; time.sleep(30)")
        started = time.monotonic()

        outcome = await run_process(proc, timeout_sec=0.5, keep_stdout=100, keep_stderr=100)

        assert outcome.timed_out is True
        assert time.monotonic() - started < 10
        assert proc.returncode is not None

    @pytest.mark.asyncio
    async def test_a_cancel_request_stops_the_process(self) -> None:
        asked: list[bool] = []

        async def cancelled() -> bool:
            asked.append(True)
            return True

        proc = await _spawn("import time; time.sleep(30)")

        outcome = await run_process(
            proc, timeout_sec=60, keep_stdout=100, keep_stderr=100, is_cancelled=cancelled, poll_interval=0.05,
        )

        assert outcome.cancelled is True
        assert outcome.timed_out is False
        assert asked
        assert proc.returncode is not None

    @pytest.mark.asyncio
    async def test_an_unreachable_cancel_probe_does_not_stop_the_process(self) -> None:
        async def unreachable() -> bool:
            raise httpx.ConnectError("supervisor is down")

        proc = await _spawn("import time; time.sleep(0.3); print('ok')")

        outcome = await run_process(
            proc, timeout_sec=60, keep_stdout=100, keep_stderr=100, is_cancelled=unreachable, poll_interval=0.01,
        )

        assert outcome.cancelled is False
        assert outcome.stdout.strip() == b"ok"

    @pytest.mark.asyncio
    async def test_cancelling_the_wait_kills_the_process(self) -> None:
        # 引擎取消工具调用时 CancelledError 打断的是等待，子进程不能留在后台接着跑。
        proc = await _spawn("import time; time.sleep(30)")
        waiter = asyncio.ensure_future(run_process(proc, timeout_sec=60, keep_stdout=100, keep_stderr=100))
        await asyncio.sleep(0.3)

        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        await asyncio.wait_for(proc.wait(), timeout=10)
        assert proc.returncode is not None

    @pytest.mark.asyncio
    @pytest.mark.skipif(not hasattr(os, "killpg"), reason="进程组只在 POSIX 上有")
    async def test_a_grandchild_holding_the_pipes_dies_with_the_group(self) -> None:
        # 只杀直接子进程的话，孙进程攥着管道，读端要等满 5 秒宽限期才放手。
        proc = await _spawn(
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            "time.sleep(30)\n"
        )
        started = time.monotonic()

        outcome = await run_process(proc, timeout_sec=0.5, keep_stdout=100, keep_stderr=100)

        assert outcome.timed_out is True
        assert time.monotonic() - started < 4


class TestScriptToolOutput:
    @pytest.mark.asyncio
    async def test_a_long_result_is_clipped_after_parsing_and_keeps_its_attachments(self, tmp_path: Path) -> None:
        # 先截 stdout 再解析的话，长结果整份解析失败、退化成纯文本，附件跟着丢。
        _write_script(tmp_path, "long_result", """
            import json, sys
            json.load(sys.stdin)
            print(json.dumps({"ok": True, "data": {"result": "x" * 20000, "attachments": [{"path": "out.png"}]}}))
        """)

        result = await _execute(_registry(tmp_path), "long_result")

        assert result["ok"] is True
        assert result["data"]["attachments"] == [{"path": "out.png"}]
        assert result["data"]["result"] == "x" * 16000 + "\n...<truncated 4000 chars>"

    @pytest.mark.asyncio
    async def test_a_long_failure_stays_a_failure(self, tmp_path: Path) -> None:
        _write_script(tmp_path, "long_failure", """
            import json
            print(json.dumps({"ok": False, "error": "quota exceeded", "data": {"result": "ERROR: " + "y" * 20000}}))
        """)

        result = await _execute(_registry(tmp_path), "long_failure")

        assert result["ok"] is False
        assert result["error"] == "quota exceeded"
        assert result["data"]["result"].endswith("<truncated 4007 chars>")

    @pytest.mark.asyncio
    async def test_half_written_json_is_an_error_rather_than_a_plain_text_success(self, tmp_path: Path) -> None:
        _write_script(tmp_path, "half_json", """
            import sys
            sys.stdout.write('{"ok": true, "data": {"result": "abc')
        """)

        result = await _execute(_registry(tmp_path), "half_json")

        assert result["ok"] is False
        assert result["error"] == "script printed malformed JSON"

    @pytest.mark.asyncio
    async def test_output_past_the_byte_cap_is_rejected_without_hanging(self, tmp_path: Path) -> None:
        _write_script(tmp_path, "flood", """
            import sys
            chunk = "z" * 65536
            for _ in range(80):
                sys.stdout.write(chunk)
        """)
        started = time.monotonic()

        result = await _execute(_registry(tmp_path), "flood")

        assert result["ok"] is False
        assert "exceeded" in result["error"]
        assert time.monotonic() - started < 30

    @pytest.mark.asyncio
    async def test_log_lines_ahead_of_the_json_do_not_hide_it(self, tmp_path: Path) -> None:
        _write_script(tmp_path, "chatty", """
            import json
            print("loading model...")
            print(json.dumps({"ok": True, "data": {"result": "answer"}}))
        """)

        result = await _execute(_registry(tmp_path), "chatty")

        assert result["ok"] is True
        assert result["data"]["result"] == "answer"

    @pytest.mark.asyncio
    async def test_plain_text_output_still_counts_as_a_result(self, tmp_path: Path) -> None:
        _write_script(tmp_path, "plain", """
            print("hello world")
        """)

        result = await _execute(_registry(tmp_path), "plain")

        assert result["ok"] is True
        assert result["data"]["result"] == "hello world"

    @pytest.mark.asyncio
    async def test_a_structured_failure_with_a_nonzero_exit_keeps_its_details(self, tmp_path: Path) -> None:
        _write_script(tmp_path, "picky", """
            import json, sys
            print(json.dumps({"ok": False, "error": "bad input", "hint": "pass a path"}))
            sys.exit(2)
        """)

        result = await _execute(_registry(tmp_path), "picky")

        assert result["ok"] is False
        assert result["error"] == "bad input"
        assert result["hint"] == "pass a path"
        assert result["data"]["result"] == "ERROR: bad input"

    @pytest.mark.asyncio
    async def test_an_unstructured_crash_reports_the_exit_code(self, tmp_path: Path) -> None:
        _write_script(tmp_path, "crash", """
            import sys
            sys.stderr.write("Traceback: boom")
            sys.exit(3)
        """)

        result = await _execute(_registry(tmp_path), "crash")

        assert result["ok"] is False
        assert result["error"] == "script exited with code 3"
        assert "boom" in result["data"]["stderr"]

    @pytest.mark.asyncio
    async def test_a_script_past_its_time_limit_is_stopped(self, tmp_path: Path) -> None:
        _write_script(tmp_path, "sleepy", """
            import time
            time.sleep(30)
        """, timeout_sec=1)
        started = time.monotonic()

        result = await _execute(_registry(tmp_path), "sleepy")

        assert result["ok"] is False
        assert result["error"] == "script timeout after 1.0s"
        assert time.monotonic() - started < 15

    @pytest.mark.asyncio
    async def test_a_cancelled_task_stops_the_script(self, tmp_path: Path) -> None:
        _write_script(tmp_path, "sleepy", """
            import time
            time.sleep(30)
        """)
        started = time.monotonic()

        result = await _execute(_registry(tmp_path), "sleepy", cancelled=True)

        assert result["ok"] is False
        assert result["cancelled"] is True
        assert time.monotonic() - started < 15


class TestToolFailuresStayInsideTheCall:
    @pytest.mark.asyncio
    async def test_a_builtin_that_raises_becomes_an_error_result(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)

        async def boom(_args: dict[str, Any], _ctx: Any) -> dict[str, Any]:
            raise IsADirectoryError("data")

        registry.register_builtin_tool("boom", "raises", {"type": "object", "properties": {}}, boom)

        result = await _execute(registry, "boom")

        assert result == {
            "ok": False,
            "error": "tool boom failed: IsADirectoryError: data",
            "data": {"result": "ERROR: tool boom failed: IsADirectoryError: data"},
        }

    @pytest.mark.asyncio
    async def test_writing_over_a_directory_fails_the_call_not_the_task(self, tmp_path: Path) -> None:
        # write_file 写之前先认出目标是目录，两个平台给同一句报错，只是一次失败的工具调用。
        (tmp_path / "data").mkdir()

        result = await _execute(_registry(tmp_path), "write_file", {"path": "data", "content": "x"})

        assert result["ok"] is False
        assert "是目录" in result["error"]
        assert result["data"]["result"] == f"ERROR: {result['error']}"
        assert (tmp_path / "data").is_dir()

    @pytest.mark.asyncio
    async def test_cancellation_still_propagates(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)

        async def halted(_args: dict[str, Any], _ctx: Any) -> dict[str, Any]:
            raise asyncio.CancelledError()

        registry.register_builtin_tool("halted", "cancelled", {"type": "object", "properties": {}}, halted)

        with pytest.raises(asyncio.CancelledError):
            await _execute(registry, "halted")

    @pytest.mark.asyncio
    async def test_a_legacy_cancelled_failure_keeps_the_flag_on_top(self, tmp_path: Path) -> None:
        # 独立工具任务判没判取消只看顶层 cancelled，归一化时不能只挪进 data。
        registry = _registry(tmp_path)

        async def legacy(_args: dict[str, Any], _ctx: Any) -> dict[str, Any]:
            return {"ok": False, "error": "task cancelled", "cancelled": True}

        registry.register_builtin_tool("legacy", "old shape", {"type": "object", "properties": {}}, legacy)

        result = await _execute(registry, "legacy")

        assert result["ok"] is False
        assert result["cancelled"] is True
        assert result["data"]["cancelled"] is True


class TestExecuteCommand:
    @pytest.mark.asyncio
    async def test_large_output_is_truncated(self, tmp_path: Path) -> None:
        command = _shell(sys.executable, "-c", "print('x' * 200000)")

        result = await _execute(_registry(tmp_path), "execute_command", {"command": command, "timeout_sec": 60})

        assert result["ok"] is True
        assert result["data"]["returncode"] == 0
        assert result["data"]["output"] == "x" * 12000 + "\n...<truncated>"

    @pytest.mark.asyncio
    async def test_a_command_past_its_timeout_is_stopped(self, tmp_path: Path) -> None:
        command = _shell(sys.executable, "-c", "import time; time.sleep(30)")
        started = time.monotonic()

        result = await _execute(_registry(tmp_path), "execute_command", {"command": command, "timeout_sec": 1})

        assert result["ok"] is False
        assert result["error"] == "timeout after 1.0s"
        assert time.monotonic() - started < 15

    @pytest.mark.asyncio
    async def test_a_cancelled_task_reports_cancelled(self, tmp_path: Path) -> None:
        command = _shell(sys.executable, "-c", "import time; time.sleep(30)")

        result = await _execute(_registry(tmp_path), "execute_command", {"command": command}, cancelled=True)

        assert result["ok"] is False
        assert result["cancelled"] is True


class TestListDirRecursive:
    @pytest.mark.asyncio
    async def test_ignored_directories_are_pruned_and_the_rest_listed(self, tmp_path: Path) -> None:
        (tmp_path / "pkg" / "sub").mkdir(parents=True)
        (tmp_path / "pkg" / "a.py").write_text("", encoding="utf-8")
        (tmp_path / "pkg" / "sub" / "b.py").write_text("", encoding="utf-8")
        (tmp_path / "pkg" / ".git" / "objects").mkdir(parents=True)
        (tmp_path / "pkg" / ".git" / "objects" / "ab").write_text("", encoding="utf-8")

        result = await _execute(_registry(tmp_path), "list_dir", {"paths": ["pkg"], "recursive": True})

        assert result["ok"] is True
        section = result["data"]["results"][0]
        assert section["entries"] == [
            {"name": "sub/", "type": "directory"},
            {"name": "a.py", "type": "file"},
            {"name": "sub/b.py", "type": "file"},
        ]
        assert (section["fileCount"], section["dirCount"]) == (2, 1)

    @pytest.mark.asyncio
    async def test_the_walk_runs_off_the_event_loop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # 包的 __init__ 把 list_dir 这个名字换成了函数，模块得从 sys.modules 里拿。
        module = importlib.import_module("toolbox.builtins.list_dir")
        real_walk = module._list_entries_recursive
        loop_thread = threading.get_ident()
        walked_in: list[int] = []

        def recording(base: Path, ignore_dirs: frozenset[str]) -> Any:
            walked_in.append(threading.get_ident())
            return real_walk(base, ignore_dirs)

        monkeypatch.setattr(module, "_list_entries_recursive", recording)
        (tmp_path / "pkg").mkdir()

        result = await _execute(_registry(tmp_path), "list_dir", {"paths": ["pkg"], "recursive": True})

        assert result["ok"] is True
        assert walked_in
        assert loop_thread not in walked_in


async def _restart(root: Path) -> dict[str, Any]:
    async with httpx.AsyncClient(transport=_supervisor()) as http:
        return await system_tool.request_restart({"target": "engine", "reason": "test"}, _context(root, http, None))


class TestRestartGitStaysOffTheLoop:
    @pytest.mark.asyncio
    async def test_git_runs_in_a_worker_thread(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        loop_thread = threading.get_ident()
        threads: list[int] = []

        def snapshot(_ctx: Any) -> dict[str, Any]:
            threads.append(threading.get_ident())
            return {"git_ok": False}

        def commit(**_kwargs: Any) -> dict[str, Any]:
            threads.append(threading.get_ident())
            return {"ok": True, "committed": False}

        monkeypatch.setattr(system_tool, "_git_snapshot", snapshot)
        monkeypatch.setattr(system_tool, "_git_commit_all", commit)

        result = await _restart(tmp_path)

        assert result["ok"] is True
        assert result["data"]["git"] == {"git_ok": False}
        assert result["data"]["git_commit"] == {"ok": True, "committed": False}
        assert len(threads) == 2
        assert loop_thread not in threads

    @pytest.mark.asyncio
    async def test_the_loop_keeps_ticking_while_git_works(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # 大仓库一次 git diff 能跑几十秒，这期间同进程的心跳和别的工具不能跟着停。
        def slow_snapshot(_ctx: Any) -> dict[str, Any]:
            time.sleep(0.5)
            return {"git_ok": False}

        monkeypatch.setattr(system_tool, "_git_snapshot", slow_snapshot)
        monkeypatch.setattr(system_tool, "_git_commit_all", lambda **_kwargs: {"ok": True, "committed": False})
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        ticking = asyncio.ensure_future(ticker())
        try:
            await _restart(tmp_path)
        finally:
            ticking.cancel()

        assert ticks >= 10

    @pytest.mark.asyncio
    async def test_two_restarts_never_run_git_at_once(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # 两个 git add/commit 同时跑会抢 index.lock，后到的那次直接失败。
        active = 0
        peak = 0
        counter = threading.Lock()

        def commit(**_kwargs: Any) -> dict[str, Any]:
            nonlocal active, peak
            with counter:
                active += 1
                peak = max(peak, active)
            time.sleep(0.2)
            with counter:
                active -= 1
            return {"ok": True, "committed": False}

        monkeypatch.setattr(system_tool, "_git_snapshot", lambda _ctx: {"git_ok": False})
        monkeypatch.setattr(system_tool, "_git_commit_all", commit)

        first, second = await asyncio.gather(_restart(tmp_path), _restart(tmp_path))

        assert first["ok"] is True and second["ok"] is True
        assert peak == 1


def _web_search_in(root: Path, child_main: str) -> Path:
    tools_dir = root / "tools"
    tools_dir.mkdir()
    for name in ("web_search.py", "_channel.py", "_wire.py"):
        shutil.copy(_ROOT / "tools" / name, tools_dir / name)
    (tools_dir / "exa_search.py").write_text(textwrap.dedent(child_main).strip() + "\n", encoding="utf-8")
    return tools_dir / "web_search.py"


def _run_web_search(root: Path, script: Path, **arguments: Any) -> tuple[dict[str, Any], float]:
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps({"query": "q", "source": "exa", **arguments}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        cwd=root,
        env={**safe_subprocess_env(), "PYTHONIOENCODING": "utf-8"},
    )
    return json.loads(done.stdout), time.monotonic() - started


class TestWebSearchChildren:
    def test_a_child_printing_more_than_a_pipe_buffer_finishes_promptly(self, tmp_path: Path) -> None:
        # 先等退出再读管道的话，子进程写满 64KiB 就卡住，只能熬到子超时被杀。
        script = _web_search_in(tmp_path, """
            import json, sys
            json.load(sys.stdin)
            print(json.dumps({"ok": True, "data": {"result": "R" * 200000}}))
        """)

        result, elapsed = _run_web_search(tmp_path, script, total_timeout_sec=60, child_timeout_sec=40)

        assert result["ok"] is True
        assert "R" * 1000 in result["data"]["result"]
        assert elapsed < 30

    def test_a_stuck_child_is_killed_at_its_timeout(self, tmp_path: Path) -> None:
        script = _web_search_in(tmp_path, """
            import time
            time.sleep(60)
        """)

        result, elapsed = _run_web_search(tmp_path, script, total_timeout_sec=30, child_timeout_sec=5)

        assert result["ok"] is False
        assert "exa_search timeout after 5s" in result["data"]["result"]
        assert elapsed < 25
