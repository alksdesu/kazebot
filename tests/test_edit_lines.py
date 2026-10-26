"""hashline 模式：read_file 每行带「行号:哈希」，edit_lines 按这些锚点改行，有一个对不上就整体不写。

写盘和 apply_diff 同一条流程；没开 hashline 的节点 read_file 照旧，edit_lines 的 spec 带 requires_edit_mode。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from toolbox.builtins.apply_diff import apply_diff  # noqa: E402
from toolbox.builtins.edit_lines import edit_lines  # noqa: E402
from toolbox.builtins.read_file import read_file  # noqa: E402
from toolbox.builtins.undo_edits import undo_edits  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402
from toolbox.edit_journal import EditJournal  # noqa: E402
from toolbox.hashline import line_hash  # noqa: E402
from toolbox.registry import ToolRegistry  # noqa: E402
from toolbox.text_files import decode_text, normalize_newlines, split_lines  # noqa: E402

_BOM = b"\xef\xbb\xbf"


class _Ctx(ToolContext):
    """离线 ToolContext：节点 extra 里的 edit_mode 默认是 hashline；按 op 名给出预设的 safety_level。"""

    def __init__(self, workspace_root: Path, *, hashline: bool = True, task: str = "task-1", **decisions: str) -> None:
        super().__init__(
            supervisor_url="http://unused",
            session_id="sess-lines",
            run_id="run-1",
            worker_id="worker-1",
            workspace_root=workspace_root,
            http=None,  # type: ignore[arg-type]
            registry=None,
            task_id=task,
        )
        self._node_extra = {"edit_mode": "hashline"} if hashline else {}
        self.decisions = dict(decisions)
        self.requested: list[tuple[str, dict[str, Any]]] = []
        self.cancelled = False

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.requested.append((op, dict(parameters)))
        return {"safety_level": self.decisions.get(op, "auto"), "reason": f"{op} decided by the test"}

    async def check_cancelled(self) -> bool:
        return self.cancelled

    @property
    def ops(self) -> list[str]:
        return [op for op, _parameters in self.requested]


def h(line: str) -> str:
    return line_hash(line)


def other_hash(line: str) -> str:
    return f"{(int(line_hash(line), 16) + 1) % 4096:03x}"


def seed(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def ten(path: Path) -> Path:
    return seed(path, "".join(f"line {index}\n" for index in range(1, 11)).encode("utf-8"))


def anchor_of(path: Path, line_no: int) -> str:
    lines = split_lines(normalize_newlines(decode_text(path.read_bytes())))
    return f"{line_no}:{line_hash(lines[line_no - 1])}"


def read(ctx: _Ctx, *entries: str | dict[str, Any]) -> dict[str, Any]:
    files = [entry if isinstance(entry, dict) else {"path": entry} for entry in entries]
    return asyncio.run(read_file({"files": files}, ctx))


def edit(ctx: _Ctx, path: str, *edits: Any) -> dict[str, Any]:
    return asyncio.run(edit_lines({"path": path, "edits": list(edits)}, ctx))


class TestReadFileModes:
    def test_hashline_mode_prints_an_anchor_on_every_line(self, tmp_path: Path) -> None:
        seed(tmp_path / "f.txt", b"alpha\n  beta\n")

        result = read(_Ctx(tmp_path), "f.txt")

        assert result["data"]["result"] == f"── f.txt ── 共 2 行\n1:{h('alpha')}|alpha\n2:{h('  beta')}|  beta"
        assert result["content"] == f"1:{h('alpha')}|alpha\n2:{h('  beta')}|  beta"

    def test_the_default_mode_is_unchanged(self, tmp_path: Path) -> None:
        seed(tmp_path / "f.txt", b"alpha\n  beta\n")

        result = read(_Ctx(tmp_path, hashline=False), "f.txt")

        assert result["data"]["result"] == "── f.txt ── 共 2 行\n   1 | alpha\n   2 |   beta"

    def test_a_range_keeps_the_real_line_numbers(self, tmp_path: Path) -> None:
        ten(tmp_path / "ten.txt")

        text = read(_Ctx(tmp_path), {"path": "ten.txt", "startLine": 4, "endLine": 5})["data"]["result"]

        assert text == f"── ten.txt ── 文件共 10 行，显示 4-5 行\n4:{h('line 4')}|line 4\n5:{h('line 5')}|line 5"

    def test_truncation_hints_are_the_same_as_before(self, tmp_path: Path) -> None:
        seed(tmp_path / "big.txt", "".join(f"line-{index}\n" for index in range(1, 701)).encode("utf-8"))

        result = read(_Ctx(tmp_path), "big.txt")

        text = result["data"]["result"]
        assert text.splitlines()[0] == "── big.txt ── 文件共 700 行，只显示了 1-500 行；继续读用 startLine=501"
        assert f"500:{h('line-500')}|line-500" in text
        assert "line-501" not in text
        assert result["data"]["results"][0]["nextStartLine"] == 501

    def test_the_hash_of_a_clipped_line_covers_the_whole_line(self, tmp_path: Path) -> None:
        long_line = "x" * 5000
        seed(tmp_path / "wide.txt", f"{long_line}\nshort\n".encode("utf-8"))

        result = read(_Ctx(tmp_path), "wide.txt")

        text = result["data"]["result"]
        assert f"1:{h(long_line)}|" in text
        assert "（本行共 5000 字符，只显示了前 2000 个）" in text
        assert result["data"]["results"][0]["clippedLines"] == [1]


class TestEditLines:
    def test_a_replace_is_written_and_echoed_with_fresh_anchors(self, tmp_path: Path) -> None:
        path = ten(tmp_path / "ten.txt")
        ctx = _Ctx(tmp_path)
        read(ctx, "ten.txt")

        result = edit(ctx, "ten.txt", {"op": "replace", "start": anchor_of(path, 5), "content": "line five"})

        assert result["ok"] is True
        assert path.read_bytes().decode("utf-8").splitlines()[4] == "line five"
        text = result["data"]["result"]
        assert text.startswith("已修改 ten.txt：1 个 edit 全部应用并保存")
        assert "edits[0] 把第 5 行换成 1 行，改动后在第 5 行" in text
        assert "改动后的内容（按改后的行号:哈希，前后各带 2 行）：" in text
        assert "── 第 3-7 行（edits[0]）──" in text
        assert f"5:{h('line five')}|line five" in text
        assert f"3:{h('line 3')}|line 3" in text and f"7:{h('line 7')}|line 7" in text
        assert "line 8" not in text
        data = result["data"]
        assert data["edits"][0]["startLine"] == 5 and data["echoed"] is True
        assert data["undoable"] is True and data["changed"] is True

    def test_every_anchor_refers_to_the_file_as_it_was_read(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"a\nb\nc\nd\ne\n")
        ctx = _Ctx(tmp_path)
        read(ctx, "f.txt")

        result = edit(
            ctx, "f.txt",
            {"op": "replace", "start": anchor_of(path, 1), "content": "A"},
            {"op": "delete", "start": anchor_of(path, 3)},
            {"op": "insert_after", "start": anchor_of(path, 5), "content": "f\ng\n"},
        )

        assert result["ok"] is True
        assert path.read_bytes() == b"A\nb\nd\ne\nf\ng\n"
        text = result["data"]["result"]
        assert "edits[1] 删除第 3 行，原处在改动后的第 3 行" in text
        assert "edits[2] 在第 5 行之后插入 2 行，改动后在第 5-6 行" in text

    def test_deleting_the_last_lines_points_at_the_end_of_the_file(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"a\nb\nc\n")
        ctx = _Ctx(tmp_path)
        read(ctx, "f.txt")

        result = edit(ctx, "f.txt", {"op": "delete", "start": anchor_of(path, 2), "end": anchor_of(path, 3)})

        assert path.read_bytes() == b"a\n"
        assert "edits[0] 删除第 2-3 行，原处在改动后的文件末尾" in result["data"]["result"]

    def test_crlf_and_bom_are_preserved(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "win.txt", _BOM + b"a\r\nb\r\nc\r\n")
        ctx = _Ctx(tmp_path)
        read(ctx, "win.txt")

        result = edit(ctx, "win.txt", {"op": "replace", "start": anchor_of(path, 2), "content": "B\n"})

        assert result["ok"] is True
        assert path.read_bytes() == _BOM + b"a\r\nB\r\nc\r\n"

    def test_an_identical_replacement_writes_nothing(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"same\n")
        ctx = _Ctx(tmp_path)
        read(ctx, "f.txt")
        before = path.stat().st_mtime_ns

        result = edit(ctx, "f.txt", {"op": "replace", "start": anchor_of(path, 1), "content": "same"})

        assert result["ok"] is True and result["data"]["changed"] is False
        assert "没有写盘" in result["data"]["result"]
        assert path.stat().st_mtime_ns == before

    def test_the_syntax_check_runs_after_writing(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "m.py", b"def f():\n    return 1\n")
        ctx = _Ctx(tmp_path)
        read(ctx, "m.py")

        result = edit(ctx, "m.py", {"op": "replace", "start": anchor_of(path, 2), "content": "    return (1"})

        assert result["ok"] is True
        assert "语法检查未通过（Python）" in result["data"]["result"]
        assert path.read_bytes() == b"def f():\n    return (1\n"

    def test_the_edit_goes_through_the_write_approval(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"a\n")
        ctx = _Ctx(tmp_path)

        edit(ctx, "f.txt", {"op": "replace", "start": anchor_of(path, 1), "content": "A"})

        assert ctx.requested == [(
            "write_file",
            {"path": "f.txt", "content_preview": "edit_lines: 1 edit(s)", "content_len": 0, "tool_name": "edit_lines"},
        )]

    def test_a_denied_edit_writes_nothing(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"a\n")
        ctx = _Ctx(tmp_path, write_file="deny")

        result = edit(ctx, "f.txt", {"op": "replace", "start": anchor_of(path, 1), "content": "A"})

        assert result["ok"] is False
        assert path.read_bytes() == b"a\n"

    def test_a_cancelled_edit_is_marked_cancelled(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"a\n")
        ctx = _Ctx(tmp_path)
        ctx.cancelled = True

        result = edit(ctx, "f.txt", {"op": "replace", "start": anchor_of(path, 1), "content": "A"})

        assert result["ok"] is False and result["cancelled"] is True and result["data"]["cancelled"] is True
        assert result["error"] == "Edit was cancelled by user"
        assert path.read_bytes() == b"a\n"

    def test_the_change_is_journaled_and_can_be_undone(self, tmp_path: Path) -> None:
        path = ten(tmp_path / "ten.txt")
        original = path.read_bytes()
        ctx = _Ctx(tmp_path)
        read(ctx, "ten.txt")
        edit(ctx, "ten.txt", {"op": "delete", "start": anchor_of(path, 2), "end": anchor_of(path, 9)})

        [task] = EditJournal(tmp_path).tasks("sess-lines")
        assert task.entries[0].tool == "edit_lines"
        result = asyncio.run(undo_edits({}, ctx))

        assert result["ok"] is True
        assert path.read_bytes() == original


class TestAnchorsThatDoNotMatch:
    def test_a_stale_anchor_rejects_every_edit_and_shows_the_current_lines(self, tmp_path: Path) -> None:
        path = ten(tmp_path / "ten.txt")
        original = path.read_bytes()
        ctx = _Ctx(tmp_path)
        read(ctx, "ten.txt")
        wrong = f"5:{other_hash('line 5')}"

        result = edit(
            ctx, "ten.txt",
            {"op": "replace", "start": anchor_of(path, 2), "content": "two"},
            {"op": "replace", "start": wrong, "content": "five"},
        )

        assert result["ok"] is False
        assert path.read_bytes() == original
        data = result["data"]
        assert data["failedEdits"] == [{
            "index": 1, "anchor": "start", "expected": wrong, "line": 5,
            "reason": "hash_mismatch", "actual": f"5:{h('line 5')}",
        }]
        assert data["appliedCount"] == 0 and data["failedCount"] == 1 and data["editCount"] == 2
        text = data["result"]
        assert text.startswith("ERROR: edit_lines 没有修改 ten.txt")
        assert "不会自动挪到别的行" in text
        assert f"edits[1].start {wrong} 对不上：第 5 行现在是 5:{h('line 5')}" in text
        assert "文件现在的内容（行号:哈希）：" in text
        assert "── 第 2-8 行 ──" in text
        assert f"5:{h('line 5')}|line 5" in text

    def test_an_anchor_past_the_end_is_reported(self, tmp_path: Path) -> None:
        path = ten(tmp_path / "ten.txt")
        ctx = _Ctx(tmp_path)
        read(ctx, "ten.txt")

        result = edit(ctx, "ten.txt", {"op": "delete", "start": anchor_of(path, 9), "end": "40:abc"})

        assert result["ok"] is False
        [failed] = result["data"]["failedEdits"]
        assert failed["reason"] == "out_of_range" and failed["anchor"] == "end"
        assert "超出了文件的行数（共 10 行）" in result["data"]["result"]
        assert f"10:{h('line 10')}|line 10" in result["data"]["result"]

    def test_an_empty_file_has_nothing_to_anchor_on(self, tmp_path: Path) -> None:
        seed(tmp_path / "empty.txt", b"")
        ctx = _Ctx(tmp_path)
        read(ctx, "empty.txt")

        result = edit(ctx, "empty.txt", {"op": "insert_after", "start": f"1:{h('')}", "content": "x"})

        assert result["ok"] is False
        assert "用 write_file" in result["data"]["result"]

    def test_no_current_lines_for_a_file_not_read_in_this_session(self, tmp_path: Path) -> None:
        seed(tmp_path / "f.txt", b"secret = 1\nother = 2\n")
        ctx = _Ctx(tmp_path)

        result = edit(ctx, "f.txt", {"op": "replace", "start": f"1:{other_hash('secret = 1')}", "content": "x"})

        assert result["ok"] is False
        text = result["data"]["result"]
        assert "本会话没有读过这个文件的当前版本" in text
        assert "secret" not in text and "other = 2" not in text
        assert "actual" not in result["data"]["failedEdits"][0]
        assert "read_file" not in ctx.ops

    def test_a_file_changed_after_reading_is_refused(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"one\ntwo\n")
        ctx = _Ctx(tmp_path)
        read(ctx, "f.txt")
        path.write_bytes(b"changed\nlines\n")

        result = edit(ctx, "f.txt", {"op": "replace", "start": anchor_of(path, 1), "content": "x"})

        assert result["ok"] is False and result["data"]["stale"] is True
        assert path.read_bytes() == b"changed\nlines\n"


class TestMalformedCalls:
    @pytest.mark.parametrize(
        "bad,message",
        [
            ({"op": "move", "start": "1:abc"}, "op 只能是"),
            ({"op": "replace", "content": "x"}, "缺少 start"),
            ({"op": "replace", "start": "12", "content": "x"}, "start"),
            ({"op": "insert_after", "start": "1:abc", "end": "2:abc", "content": "x"}, "不要给 end"),
            ({"op": "delete", "start": "1:abc", "content": "x"}, "delete 不用 content"),
            ({"op": "insert_before", "start": "1:abc", "content": ""}, "没有要插入的内容"),
            ({"op": "replace", "start": "1:abc"}, "缺少 content"),
            ({"op": "replace", "start": "3:abc", "end": "1:abc", "content": "x"}, "之前"),
            ({"op": "replace", "start": "1:abc", "content": "1:abc|x\n2:def|y\n"}, "原样抄了进来"),
            ({"op": "replace", "start": "1:abc", "content": {"text": "x"}}, "content 要是字符串"),
            ("not an object", "必须是对象"),
        ],
    )
    def test_malformed_edits_are_rejected_before_any_approval(self, tmp_path: Path, bad: Any, message: str) -> None:
        path = seed(tmp_path / "f.txt", b"a\nb\nc\n")
        ctx = _Ctx(tmp_path)

        result = edit(ctx, "f.txt", bad)

        assert result["ok"] is False and ctx.requested == []
        assert message in result["data"]["failedEdits"][0]["error"]
        assert path.read_bytes() == b"a\nb\nc\n"

    def test_overlapping_edits_are_rejected_before_any_approval(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"a\nb\nc\nd\ne\n")
        ctx = _Ctx(tmp_path)

        result = edit(
            ctx, "f.txt",
            {"op": "replace", "start": anchor_of(path, 2), "end": anchor_of(path, 4), "content": "x"},
            {"op": "insert_after", "start": anchor_of(path, 3), "content": "y"},
        )

        assert result["ok"] is False and ctx.requested == []
        [problem] = result["data"]["failedEdits"]
        assert problem["index"] == 1 and "冲突" in problem["error"]
        assert path.read_bytes() == b"a\nb\nc\nd\ne\n"

    @pytest.mark.parametrize("args", [{"edits": []}, {"path": "f.txt"}, {"path": "f.txt", "edits": "replace"}, {"edits": [{}]}])
    def test_a_call_without_path_or_edits_is_refused(self, tmp_path: Path, args: dict[str, Any]) -> None:
        seed(tmp_path / "f.txt", b"a\n")
        ctx = _Ctx(tmp_path)

        result = asyncio.run(edit_lines(args, ctx))

        assert result["ok"] is False and ctx.requested == []

    def test_a_list_of_lines_is_accepted_as_content(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"a\nb\n")
        ctx = _Ctx(tmp_path)
        read(ctx, "f.txt")

        result = edit(ctx, "f.txt", {"op": "insert_before", "start": anchor_of(path, 2), "content": ["x", "", "y"]})

        assert result["ok"] is True
        assert path.read_bytes() == b"a\nx\n\ny\nb\n"


class TestApplyDiffInHashlineMode:
    def test_the_echo_carries_anchors(self, tmp_path: Path) -> None:
        ten(tmp_path / "ten.txt")
        ctx = _Ctx(tmp_path)
        read(ctx, "ten.txt")

        result = asyncio.run(apply_diff({"path": "ten.txt", "diffs": [{"search": "line 5\n", "replace": "line five\n"}]}, ctx))

        text = result["data"]["result"]
        assert result["ok"] is True
        assert "改动后的内容（按改后的行号:哈希，前后各带 2 行）：" in text
        assert f"5:{h('line five')}|line five" in text
        assert "   5 | line five" not in text

    def test_the_closest_snippet_carries_anchors(self, tmp_path: Path) -> None:
        seed(tmp_path / "calc.py", b"def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n")
        ctx = _Ctx(tmp_path)
        read(ctx, "calc.py")

        result = asyncio.run(apply_diff({"path": "calc.py", "diffs": [{
            "search": "def sub(a, b):\n    return a - c\n", "replace": "def sub(a, b):\n    return b - a\n",
        }]}, ctx))

        assert result["ok"] is False
        assert f"4:{h('def sub(a, b):')}|def sub(a, b):" in result["data"]["result"]

    def test_the_default_mode_echo_is_unchanged(self, tmp_path: Path) -> None:
        ten(tmp_path / "ten.txt")
        ctx = _Ctx(tmp_path, hashline=False)
        read(ctx, "ten.txt")

        result = asyncio.run(apply_diff({"path": "ten.txt", "diffs": [{"search": "line 5\n", "replace": "line five\n"}]}, ctx))

        text = result["data"]["result"]
        assert "改动后的内容（按改后的行号，前后各带 2 行）：" in text
        assert "   5 | line five" in text


async def _noop(_args: dict[str, Any], _ctx: Any) -> dict[str, Any]:
    return {"ok": True, "data": {"result": "ok"}}


class TestRegistry:
    def test_edit_lines_is_declared_for_hashline_nodes_only(self) -> None:
        registry = ToolRegistry(workspace_root=_ROOT, tools_dir=_ROOT / "tools", load_external=False)

        specs = {spec["name"]: spec for spec in registry.builtin_specs()}

        assert specs["edit_lines"]["requires_edit_mode"] == "hashline"
        assert specs["edit_lines"]["input_schema"]["required"] == ["path", "edits"]
        assert set(specs["edit_lines"]["input_schema"]["properties"]["edits"]["items"]["properties"]) == {
            "op", "start", "end", "content",
        }
        assert "requires_edit_mode" not in specs["undo_edits"]
        assert specs["undo_edits"]["input_schema"]["properties"]["action"]["enum"] == ["undo", "list"]
        assert "requires_edit_mode" not in specs["read_file"]
        assert "hashline" in specs["read_file"]["description"]

    def test_extra_spec_fields_survive_a_reload(self, tmp_path: Path) -> None:
        registry = ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools", load_external=False)

        registry.register_builtin_tool(
            "custom_tool", "custom", {"type": "object", "properties": {}}, _noop, {"parallel_safe": True},
        )
        registry.reload()

        assert registry.get_spec("custom_tool")["parallel_safe"] is True
        assert next(spec for spec in registry.builtin_specs() if spec["name"] == "custom_tool")["parallel_safe"] is True

    def test_extra_spec_fields_cannot_replace_the_core_ones(self, tmp_path: Path) -> None:
        registry = ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools", load_external=False)

        with pytest.raises(ValueError, match="description"):
            registry.register_builtin_tool("custom_tool", "custom", {"type": "object"}, _noop, {"description": "other"})

    def test_the_four_argument_form_still_works(self, tmp_path: Path) -> None:
        registry = ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools", load_external=False)

        registry.register_builtin_tool("plain_tool", "plain", {"type": "object"}, _noop)

        spec = next(spec for spec in registry.builtin_specs() if spec["name"] == "plain_tool")
        assert spec == {"name": "plain_tool", "description": "plain", "input_schema": {"type": "object"}}
