"""命令输出的头尾保留：读的时候每一路留开头和结尾、记下中间丢了多少；按字数预算截时开头约四成、结尾约六成。

execute_command 不传 max_output_chars 就把留下的全交给 engine 落盘；改后检查命令的输出也保住结尾的结论。
"""
from __future__ import annotations

import asyncio
import importlib
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config_layout  # noqa: E402
from toolbox import syntax_check  # noqa: E402
from toolbox._common import (  # noqa: E402
    OUTPUT_HEAD_SHARE,
    JoinedOutput,
    StreamCapture,
    _drain,
    _gap_marker,
    fit_output,
    join_outputs,
    run_process,
)
from toolbox.context import ToolContext  # noqa: E402

# 包的 __init__ 把 execute_command 这个名字换成了函数，模块得从 sys.modules 里拿
exec_module = importlib.import_module("toolbox.builtins.execute_command")

_BUDGET_MARKER = re.compile(r"\[……按 \d+ 字符截取，中间省略了 (\d+) 字符（(\d+) 行）(?:；[^\]]*)?……\]\n")


class _Ctx(ToolContext):
    def __init__(self, workspace_root: Path) -> None:
        super().__init__(
            supervisor_url="http://unused",
            session_id="sess-exec",
            run_id="run-1",
            worker_id="worker-1",
            workspace_root=workspace_root,
            http=None,  # type: ignore[arg-type]
            registry=None,
            task_id="task-1",
        )
        self.requested: list[str] = []

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.requested.append(op)
        return {"safety_level": "auto"}

    async def check_cancelled(self) -> bool:
        return False


def _drained(data: bytes, head: int, tail: int) -> StreamCapture:
    async def run() -> StreamCapture:
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        return await _drain(reader, head, tail)

    return asyncio.run(run())


def _numbered_lines(count: int) -> bytes:
    return b"".join(f"line{index:06d}\n".encode() for index in range(count))


def _split(result: str) -> tuple[str, re.Match[str], str]:
    match = _BUDGET_MARKER.search(result)
    assert match is not None, result[:300]
    return result[:match.start()], match, result[match.end():]


class TestDrainBoundaries:
    def test_exactly_head_plus_tail_drops_nothing(self) -> None:
        data = bytes(range(65, 85))

        capture = _drained(data, 10, 10)

        assert capture.dropped == 0
        assert capture.data == data

    def test_one_byte_less_drops_nothing(self) -> None:
        data = bytes(range(65, 84))

        capture = _drained(data, 10, 10)

        assert (capture.dropped, capture.data) == (0, data)

    def test_one_byte_more_drops_exactly_that_byte(self) -> None:
        data = b"0123456789\n0123456789"

        capture = _drained(data, 10, 10)

        assert capture.head == data[:10]
        assert capture.tail == data[11:]
        assert (capture.dropped, capture.dropped_newlines) == (1, 1)
        assert capture.overflow is True

    def test_a_huge_stream_keeps_both_ends_and_counts_the_middle(self) -> None:
        data = _numbered_lines(300_000)

        capture = _drained(data, 1000, 1000)

        assert capture.head == data[:1000]
        assert capture.tail == data[-1000:]
        assert capture.dropped == len(data) - 2000
        assert capture.dropped_newlines == data[1000:-1000].count(b"\n")

    def test_without_a_tail_only_the_head_is_kept(self) -> None:
        data = b"abc\ndef\nghi\n"

        capture = _drained(data, 5, 0)

        assert (capture.head, capture.tail) == (b"abc\nd", b"")
        assert (capture.dropped, capture.dropped_newlines) == (len(data) - 5, 2)

    def test_cut_characters_go_to_the_dropped_part_so_both_ends_decode(self) -> None:
        data = "中文字符".encode() * 10

        capture = _drained(data, 4, 4)

        assert capture.head.decode("utf-8") == "中"
        assert capture.tail.decode("utf-8") == "符"
        assert capture.dropped == len(data) - len(capture.head) - len(capture.tail)

    def test_an_absent_stream_is_empty(self) -> None:
        assert asyncio.run(_drain(None, 10, 10)) == StreamCapture()

    @pytest.mark.asyncio
    async def test_a_real_process_keeps_both_ends(self) -> None:
        code = (
            "import sys; w = sys.stdout.buffer.write; "
            "[w(('line' + str(i).zfill(6) + chr(10)).encode()) for i in range(100000)]; "
            "sys.stderr.buffer.write(b'E' * 5000)"
        )
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        outcome = await run_process(
            proc, timeout_sec=60, keep_stdout=1000, keep_stderr=1000, tail_stdout=1000, tail_stderr=1000,
        )

        assert outcome.returncode == 0
        assert outcome.stdout_capture.head.startswith(b"line000000\n")
        assert outcome.stdout_capture.tail.endswith(b"line099999\n")
        assert outcome.stdout_capture.dropped == 100_000 * 11 - 2000
        assert outcome.stderr_capture.dropped == 3000
        assert outcome.stdout_overflow and outcome.stderr_overflow


class TestJoinOutputs:
    def test_stderr_follows_stdout_after_a_newline(self) -> None:
        joined = join_outputs(StreamCapture(head=b"out"), StreamCapture(head=b"err"))

        assert joined.text == "out\nerr"
        assert joined.gaps == ()

    def test_an_empty_stderr_adds_nothing(self) -> None:
        assert join_outputs(StreamCapture(head=b"out\n"), StreamCapture()).text == "out\n"

    def test_a_dropped_middle_becomes_a_marker_line(self) -> None:
        joined = join_outputs(StreamCapture(b"head", b"tail\n", 123, 4), StreamCapture())

        marker = _gap_marker(123, 4)
        assert joined.text == f"head\n{marker}\ntail\n"
        assert joined.dropped == 123
        (gap,) = joined.gaps
        assert joined.text[gap.start:gap.end] == f"\n{marker}\n"

    def test_both_streams_can_have_gaps(self) -> None:
        joined = join_outputs(StreamCapture(b"a\n", b"b\n", 10, 1), StreamCapture(b"c\n", b"d\n", 20, 2))

        assert [gap.dropped for gap in joined.gaps] == [10, 20]
        assert joined.dropped == 30
        assert joined.text.endswith("d\n")


def _lines(count: int, width: int = 39) -> str:
    return "".join(f"line-{index:04d} " + "x" * width + "\n" for index in range(count))


class TestFitOutput:
    def test_short_output_is_left_alone(self) -> None:
        assert fit_output(JoinedOutput("short"), 100) == ("short", 0)

    def test_a_zero_budget_means_no_cut(self) -> None:
        text = _lines(100)

        assert fit_output(JoinedOutput(text), 0) == (text, 0)

    def test_the_end_gets_more_room_than_the_start(self) -> None:
        text = _lines(200)

        result, omitted = fit_output(JoinedOutput(text), 2000)

        head, match, tail = _split(result)
        share = len(head) / (len(head) + len(tail))
        assert abs(share - OUTPUT_HEAD_SHARE) < 0.05
        assert len(tail) > len(head)
        assert omitted == int(match.group(1)) == len(text) - len(head) - len(tail)
        assert int(match.group(2)) == text.count("\n") - head.count("\n") - tail.count("\n")

    def test_cuts_land_on_line_boundaries(self) -> None:
        text = _lines(200)

        head, _match, tail = _split(fit_output(JoinedOutput(text), 2000)[0])

        assert head.startswith("line-0000 ")
        assert head.endswith("\n")
        assert tail.startswith("line-")
        assert tail.endswith("line-0199 " + "x" * 39 + "\n")

    def test_one_giant_line_is_cut_by_characters(self) -> None:
        text = "x" * 50_000

        result, omitted = fit_output(JoinedOutput(text), 2000)

        assert len(result) <= 2000
        assert omitted > 40_000

    @pytest.mark.parametrize("budget", [1000, 1001, 1500, 2000, 4000, 12_000, 99_999])
    # 文本本身当用例 id 会塞进 PYTEST_CURRENT_TEST 环境变量，Windows 上超过 32767 字直接报错
    @pytest.mark.parametrize(
        "text",
        [_lines(3000), "y" * 300_000, _lines(500, width=900), "\n" * 200_000],
        ids=["many-lines", "one-long-line", "wide-lines", "only-newlines"],
    )
    def test_never_longer_than_the_budget(self, budget: int, text: str) -> None:
        result, omitted = fit_output(JoinedOutput(text), budget)

        assert len(result) <= budget
        assert omitted > 0

    @pytest.mark.parametrize("budget", [1000, 1234, 4000, 50_000])
    def test_huge_read_time_counts_still_fit_the_budget(self, budget: int) -> None:
        # 读取时丢掉的行数可以比留下的字符多好几个数量级，说明里的数字变长也不能把结果撑过预算
        joined = join_outputs(StreamCapture(b"h" * 300_000, b"t" * 300_000, 10**13, 10**12), StreamCapture())

        result, _omitted = fit_output(joined, budget)

        assert len(result) <= budget
        assert "读取时另丢了 10000000000000 字节（1000000000000 行）" in result

    @pytest.mark.parametrize("budget", range(1000, 1400, 7))
    def test_a_read_time_marker_is_never_cut_in_half(self, budget: int) -> None:
        joined = join_outputs(StreamCapture(b"a\n" * 200, b"b\n" * 600, 999, 9), StreamCapture())
        whole = _gap_marker(999, 9)

        result, _omitted = fit_output(joined, budget)

        assert len(result) <= budget
        if "输出太长" in result:
            assert whole in result


def _shell(*argv: str) -> str:
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def _execute(workspace: Path, code: str, **args: Any) -> dict[str, Any]:
    command = _shell(sys.executable, "-c", code)
    return asyncio.run(exec_module.execute_command({"command": command, "timeout_sec": 60, **args}, _Ctx(workspace)))


_MANY_LINES = "import sys; w = sys.stdout.buffer.write; [w(('line' + str(i).zfill(6) + chr(10)).encode()) for i in range({n})]"


class TestExecuteCommandOutput:
    def test_without_a_budget_everything_kept_is_returned(self, tmp_path: Path) -> None:
        result = _execute(tmp_path, "import sys; sys.stdout.buffer.write(b'x' * 200000)")

        data = result["data"]
        assert result["ok"] is True
        assert data["output"] == "x" * 200_000
        assert data["result"] == "returncode=0\n" + "x" * 200_000
        assert "truncated" not in data

    def test_huge_output_keeps_head_and_tail_and_says_what_was_dropped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(exec_module, "CAPTURE_HEAD_BYTES", 2000)
        monkeypatch.setattr(exec_module, "CAPTURE_TAIL_BYTES", 2000)
        data = _numbered_lines(50_000)

        result = _execute(tmp_path, _MANY_LINES.format(n=50_000))

        out = result["data"]["output"]
        assert out.startswith("line000000\n")
        assert out.endswith("line049999\n")
        dropped = len(data) - 4000
        assert _gap_marker(dropped, data[2000:-2000].count(b"\n")) in out
        assert result["data"]["truncated"] is True
        assert result["data"]["droppedBytes"] == dropped
        assert "omittedChars" not in result["data"]

    def test_max_output_chars_keeps_more_of_the_end(self, tmp_path: Path) -> None:
        result = _execute(tmp_path, _MANY_LINES.format(n=5000), max_output_chars=2000)

        out = result["data"]["output"]
        head, _match, tail = _split(out)
        assert len(out) <= 2000
        assert head.startswith("line000000\n")
        assert tail.endswith("line004999\n")
        assert len(tail) > len(head)
        assert result["data"]["truncated"] is True
        assert result["data"]["omittedChars"] > 0

    def test_the_instance_default_budget_applies_until_the_model_passes_one(
        self, tmp_path: Path,
    ) -> None:
        runtime = config_layout.runtime_path(tmp_path)
        runtime.parent.mkdir(parents=True, exist_ok=True)
        runtime.write_text(yaml.safe_dump({"meta": {"execute_command": {"max_output_chars": 1500}}}), encoding="utf-8")
        code = _MANY_LINES.format(n=5000)

        default = _execute(tmp_path, code)["data"]["output"]
        asked = _execute(tmp_path, code, max_output_chars=3000)["data"]["output"]

        assert len(default) <= 1500
        assert 1500 < len(asked) <= 3000

    def test_stderr_follows_stdout(self, tmp_path: Path) -> None:
        result = _execute(tmp_path, "import sys; sys.stdout.buffer.write(b'out'); sys.stderr.buffer.write(b'err')")

        assert result["data"]["output"] == "out\nerr"
        assert result["data"]["result"] == "returncode=0\nout\nerr"

    def test_a_failing_command_is_still_a_successful_call(self, tmp_path: Path) -> None:
        result = _execute(tmp_path, "import sys; sys.stdout.buffer.write(b'bad'); sys.exit(3)")

        assert result["ok"] is True
        assert result["data"]["returncode"] == 3
        assert result["data"]["result"] == "returncode=3\nbad"

    @pytest.mark.parametrize("raw,expected", [
        (None, None),
        ("abc", None),
        (True, None),
        (0, None),
        (-5, None),
        (float("nan"), None),
        (float("inf"), None),
        (10, exec_module.MIN_OUTPUT_CHARS),
        (1500, 1500),
        ("1500", 1500),
        (10**12, exec_module.MAX_OUTPUT_CHARS),
    ])
    def test_the_requested_budget_is_normalised(self, raw: Any, expected: int | None) -> None:
        assert exec_module.requested_budget(raw) == expected


class TestCheckCommandOutput:
    def test_the_conclusion_at_the_end_survives(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        code = (
            "import sys; w = sys.stdout.buffer.write; "
            "[w(('lint line ' + str(i) + chr(10)).encode()) for i in range(3000)]; "
            "w(b'SUMMARY: 1 error'); sys.exit(1)"
        )
        monkeypatch.setattr(syntax_check, "load_runtime_config", lambda _root: {"edit_checks": {"commands": [
            {"glob": "*.py", "command": _shell(sys.executable, "-c", code), "timeout_sec": 60},
        ]}})
        target = tmp_path / "app.py"
        target.write_bytes(b"x = 1\n")
        ctx = _Ctx(tmp_path)

        (result,) = asyncio.run(syntax_check.run_external_checks(ctx, target.resolve(), tool_name="write_file"))

        assert ctx.requested == ["execute_command"]
        assert result.status == "failed"
        assert len(result.output) <= syntax_check._COMMAND_OUTPUT_CHARS
        assert result.output.startswith("lint line 0\n")
        assert result.output.endswith("SUMMARY: 1 error")
        assert result.truncated is True
        described = result.describe()
        assert "SUMMARY: 1 error" in described
        assert described.endswith("（输出过长，只留了开头和结尾）")
