"""read_file / write_file / apply_diff 端到端：模型只看得到 data.result，截断、失败原因、改动位置都得写在里面。

apply_diff 有一个 diff 失败就整体不写盘；读过之后被别人改过的文件不许覆盖；改完的检查附在结果末尾，不回滚。
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import config_layout  # noqa: E402
from _file_edits_fake import FakeFileEdits  # noqa: E402
from engine.tool_step import summarize_result  # noqa: E402
from toolbox import file_state  # noqa: E402
from toolbox.builtins.apply_diff import apply_diff  # noqa: E402
from toolbox.builtins.read_file import MAX_RESULT_BYTES, read_file  # noqa: E402
from toolbox.builtins.write_file import write_file  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402
from toolbox.syntax_check import quote_path  # noqa: E402

# toolbox.builtins 把同名函数导出成了包属性，按属性取到的是函数而不是模块
apply_diff_module = importlib.import_module("toolbox.builtins.apply_diff")

_BOM = b"\xef\xbb\xbf"
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (1).to_bytes(4, "big") * 2 + b"\x08\x02\x00\x00\x00"


class _Ctx(FakeFileEdits, ToolContext):
    """离线 ToolContext：记下每次送审和只问结论的 op；按 op 名给出预设的 safety_level，没写的一律 auto。

    没有任务号，读戳直接记在会话层；认领和读戳交给本工作区的 FakeSupervisor。
    """

    def __init__(self, workspace_root: Path, *, session: str = "sess-edit", **decisions: str) -> None:
        super().__init__(
            supervisor_url="http://unused",
            session_id=session,
            run_id="run-1",
            worker_id="worker-1",
            workspace_root=workspace_root,
            http=None,  # type: ignore[arg-type]
            registry=None,
        )
        self.decisions = decisions
        self.requested: list[tuple[str, dict[str, Any]]] = []
        self.evaluated: list[tuple[str, dict[str, Any]]] = []

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.requested.append((op, dict(parameters)))
        return {"safety_level": self.decisions.get(op, "auto"), "reason": f"{op} decided by the test"}

    async def evaluate_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.evaluated.append((op, dict(parameters)))
        return {"safety_level": self.decisions.get(op, "auto"), "reason": f"{op} decided by the test"}

    async def check_cancelled(self) -> bool:
        return False

    @property
    def ops(self) -> list[str]:
        return [op for op, _parameters in self.requested]


@pytest.fixture
def ctx(tmp_path: Path) -> _Ctx:
    return _Ctx(tmp_path)


def seed(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def read(ctx: _Ctx, *entries: str | dict[str, Any]) -> dict[str, Any]:
    files = [entry if isinstance(entry, dict) else {"path": entry} for entry in entries]
    return asyncio.run(read_file({"files": files}, ctx))


def write(ctx: _Ctx, path: str, content: Any) -> dict[str, Any]:
    return asyncio.run(write_file({"path": path, "content": content}, ctx))


def diff(ctx: _Ctx, path: str, *diffs: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(apply_diff({"path": path, "diffs": list(diffs)}, ctx))


def edit(search: str, replace: str, start_line: int | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"search": search, "replace": replace}
    if start_line is not None:
        entry["start_line"] = start_line
    return entry


def numbered(count: int) -> bytes:
    return "".join(f"line-{i}\n" for i in range(1, count + 1)).encode("utf-8")


def wide_file(lines: int = 400) -> bytes:
    return ("y" * 64 + "\n").encode("utf-8") * lines


class TestReadFileTells:
    def test_a_long_file_says_how_many_lines_and_where_to_continue(self, ctx: _Ctx, tmp_path: Path) -> None:
        seed(tmp_path / "big.txt", numbered(700))

        result = read(ctx, "big.txt")

        text = result["data"]["result"]
        assert text.splitlines()[0] == "── big.txt ── 文件共 700 行，只显示了 1-500 行；继续读用 startLine=501"
        assert " 500 | line-500" in text
        assert "line-501" not in text
        entry = result["data"]["results"][0]
        assert entry["truncated"] is True and entry["truncatedBy"] == "default_limit"
        assert entry["nextStartLine"] == 501 and entry["totalLines"] == 700 and entry["shownLines"] == 500

    def test_continuing_from_the_hint_reads_the_rest(self, ctx: _Ctx, tmp_path: Path) -> None:
        seed(tmp_path / "big.txt", numbered(700))

        text = read(ctx, {"path": "big.txt", "startLine": 501})["data"]["result"]

        assert text.splitlines()[0] == "── big.txt ── 文件共 700 行，显示 501-700 行"
        assert " 700 | line-700" in text

    def test_a_complete_file_states_its_line_count(self, ctx: _Ctx, tmp_path: Path) -> None:
        seed(tmp_path / "small.txt", b"a\nb\nc\n")

        assert read(ctx, "small.txt")["data"]["result"] == "── small.txt ── 共 3 行\n   1 | a\n   2 | b\n   3 | c"

    def test_an_explicit_range_names_the_range_shown(self, ctx: _Ctx, tmp_path: Path) -> None:
        seed(tmp_path / "f.txt", "".join(f"{i}\n" for i in range(1, 51)).encode("utf-8"))

        text = read(ctx, {"path": "f.txt", "startLine": 10, "endLine": 12})["data"]["result"]

        assert text == "── f.txt ── 文件共 50 行，显示 10-12 行\n  10 | 10\n  11 | 11\n  12 | 12"

    def test_a_numeric_string_range_is_accepted(self, ctx: _Ctx, tmp_path: Path) -> None:
        seed(tmp_path / "f.txt", "".join(f"{i}\n" for i in range(1, 51)).encode("utf-8"))

        entry = read(ctx, {"path": "f.txt", "startLine": "10", "endLine": "11"})["data"]["results"][0]

        assert entry["lineCount"] == 2 and entry["startLine"] == 10 and entry["endLine"] == 11

    def test_start_line_past_the_end_is_an_error(self, ctx: _Ctx, tmp_path: Path) -> None:
        seed(tmp_path / "f.txt", b"a\nb\n")

        result = read(ctx, {"path": "f.txt", "startLine": 9})

        assert result["ok"] is False
        assert "startLine 9 超出了文件的行数（共 2 行）" in result["data"]["result"]

    def test_an_empty_file_is_reported_not_dropped(self, ctx: _Ctx, tmp_path: Path) -> None:
        seed(tmp_path / "empty.txt", b"")

        result = read(ctx, "empty.txt")

        assert result["ok"] is True
        assert result["data"]["result"] == "── empty.txt ── 空文件（0 行）"

    def test_a_very_long_line_is_clipped_and_marked(self, ctx: _Ctx, tmp_path: Path) -> None:
        seed(tmp_path / "wide.txt", ("x" * 5000 + "\nshort\n").encode("utf-8"))

        result = read(ctx, "wide.txt")

        text = result["data"]["result"]
        assert "（本行共 5000 字符，只显示了前 2000 个）" in text
        assert "x" * 2001 not in text
        assert "   2 | short" in text
        assert result["data"]["results"][0]["clippedLines"] == [1]

    def test_crlf_and_bom_do_not_leak_into_the_content(self, ctx: _Ctx, tmp_path: Path) -> None:
        seed(tmp_path / "win.txt", _BOM + b"a\r\nb\r\n")

        assert read(ctx, "win.txt")["data"]["result"] == "── win.txt ── 共 2 行\n   1 | a\n   2 | b"

    def test_lines_are_counted_by_newline_only(self, ctx: _Ctx, tmp_path: Path) -> None:
        # 换页符不是行尾：行号要和 apply_diff 数 \n 的口径一致
        seed(tmp_path / "ff.txt", b"a\x0cb\nc\n")

        text = read(ctx, "ff.txt")["data"]["result"]

        assert text.split("\n")[0] == "── ff.txt ── 共 2 行"
        assert "   2 | c" in text


class TestTheResultBudget:
    def test_the_total_is_capped_and_files_past_it_are_listed(self, ctx: _Ctx, tmp_path: Path) -> None:
        for name in ("a.txt", "b.txt", "c.txt"):
            seed(tmp_path / name, wide_file())

        result = read(ctx, "a.txt", "b.txt", "c.txt")

        data = result["data"]
        text = data["result"]
        a, b, c = data["results"]
        assert result["ok"] is True
        assert a["lineCount"] == 400 and "truncated" not in a
        assert b["truncated"] is True and b["truncatedBy"] == "budget"
        assert 0 < b["lineCount"] < 400
        assert b["nextStartLine"] == b["lineCount"] + 1
        assert c["type"] == "omitted" and c["omitted"] is True
        assert data["omittedCount"] == 1 and data["successCount"] == 2 and data["failCount"] == 0
        # 被截掉、没读的文件写在最前面：再长的结果被引擎截尾，这一句也还在
        first_line = text.splitlines()[0]
        assert first_line.startswith("本次 read_file 没有全部显示：")
        assert "c.txt 未读取" in first_line
        assert f"继续读用 startLine={b['nextStartLine']}" in text
        assert "── c.txt ── 未读取" in text
        assert len(text.encode("utf-8")) <= MAX_RESULT_BYTES + 1024

    def test_images_still_come_through_after_the_text_budget_is_spent(self, ctx: _Ctx, tmp_path: Path) -> None:
        for name in ("a.txt", "b.txt"):
            seed(tmp_path / name, wide_file())
        seed(tmp_path / "pic.png", _PNG)

        result = read(ctx, "a.txt", "b.txt", "pic.png")

        picture = result["data"]["results"][2]
        assert picture["type"] == "multimodal" and picture["mimeType"] == "image/png"
        assert len(result["data"]["attachments"]) == 1
        assert "── pic.png ── [image: image/png" in result["data"]["result"]


class TestReadBeforeEdit:
    def test_write_file_refuses_a_file_changed_after_reading(self, ctx: _Ctx, tmp_path: Path) -> None:
        path = seed(tmp_path / "notes.md", b"mine\n")
        read(ctx, "notes.md")
        path.write_bytes(b"someone else's edit\n")

        result = write(ctx, "notes.md", "new\n")

        assert result["ok"] is False
        assert result["error"] == file_state.STALE_MESSAGE
        assert result["data"]["stale"] is True
        assert path.read_bytes() == b"someone else's edit\n"

    def test_apply_diff_refuses_a_file_changed_after_reading(self, ctx: _Ctx, tmp_path: Path) -> None:
        path = seed(tmp_path / "notes.md", b"keep: me\n")
        read(ctx, "notes.md")
        path.write_bytes(b"keep: me\nadded: elsewhere\n")

        result = diff(ctx, "notes.md", edit("keep: me", "keep: us"))

        assert result["ok"] is False and result["data"]["stale"] is True
        assert file_state.STALE_MESSAGE in result["data"]["result"]
        assert path.read_bytes() == b"keep: me\nadded: elsewhere\n"

    def test_reading_again_clears_the_refusal(self, ctx: _Ctx, tmp_path: Path) -> None:
        path = seed(tmp_path / "notes.md", b"mine\n")
        read(ctx, "notes.md")
        path.write_bytes(b"theirs\n")
        assert write(ctx, "notes.md", "new\n")["ok"] is False

        read(ctx, "notes.md")

        assert write(ctx, "notes.md", "new\n")["ok"] is True
        assert path.read_bytes() == b"new\n"

    def test_a_partial_read_still_guards_the_whole_file(self, ctx: _Ctx, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"a\nb\nc\n")
        read(ctx, {"path": "f.txt", "startLine": 1, "endLine": 1})
        path.write_bytes(b"a\nb\nC\n")

        assert diff(ctx, "f.txt", edit("a\n", "A\n"))["data"]["stale"] is True

    def test_the_tools_own_writes_keep_the_stamp_current(self, ctx: _Ctx, tmp_path: Path) -> None:
        seed(tmp_path / "f.txt", b"one\n")
        read(ctx, "f.txt")

        assert diff(ctx, "f.txt", edit("one", "two"))["ok"] is True
        assert diff(ctx, "f.txt", edit("two", "three"))["ok"] is True
        assert write(ctx, "f.txt", "four\n")["ok"] is True
        assert diff(ctx, "f.txt", edit("four", "five"))["ok"] is True
        assert (tmp_path / "f.txt").read_bytes() == b"five\n"

    def test_touching_without_changing_is_not_a_change(self, ctx: _Ctx, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"one\n")
        read(ctx, "f.txt")
        info = path.stat()
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))

        assert write(ctx, "f.txt", "two\n")["ok"] is True

    def test_another_conversation_keeps_its_own_stamps(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"one\n")
        reader = _Ctx(tmp_path, session="reader")
        other = _Ctx(tmp_path, session="other")
        read(reader, "f.txt")

        result = write(other, "f.txt", "mine\n")

        assert result["ok"] is True and result["data"]["unreadOverwrite"] is True
        # 对读过的那个会话来说，文件已经不是它看到的那一版了
        assert write(reader, "f.txt", "x\n")["ok"] is False
        assert path.read_bytes() == b"mine\n"

    def test_branch_tasks_of_one_conversation_share_the_stamps(self, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"one\n")
        first = _Ctx(tmp_path, session="branch_1")
        second = _Ctx(tmp_path, session="branch_2")
        first._context_archive_session_id = "main"  # type: ignore[attr-defined]
        second._context_archive_session_id = "main"  # type: ignore[attr-defined]
        read(first, "f.txt")
        path.write_bytes(b"changed\n")

        assert write(second, "f.txt", "x\n")["data"]["stale"] is True


class TestWriteFile:
    def test_overwriting_an_unread_file_is_allowed_and_noted(self, ctx: _Ctx, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"old\n")

        result = write(ctx, "f.txt", "new\n")

        assert result["ok"] is True
        assert result["data"]["unreadOverwrite"] is True
        assert "本会话之前没有读过这个文件" in result["data"]["result"]
        assert path.read_bytes() == b"new\n"

    def test_overwriting_a_file_that_was_read_carries_no_warning(self, ctx: _Ctx, tmp_path: Path) -> None:
        seed(tmp_path / "f.txt", b"old\n")
        read(ctx, "f.txt")

        result = write(ctx, "f.txt", "new\n")

        assert "unreadOverwrite" not in result["data"]
        assert "没有读过" not in result["data"]["result"]

    def test_a_new_file_is_created_with_its_directories(self, ctx: _Ctx, tmp_path: Path) -> None:
        result = write(ctx, "sub/dir/new.txt", "hello\n")

        assert result["ok"] is True and result["data"]["created"] is True
        assert result["data"]["result"].startswith("已写入 sub/dir/new.txt（新建文件，6 字节）")
        assert (tmp_path / "sub" / "dir" / "new.txt").read_bytes() == b"hello\n"

    def test_bom_and_crlf_survive_an_overwrite(self, ctx: _Ctx, tmp_path: Path) -> None:
        path = seed(tmp_path / "win.txt", _BOM + b"a\r\nb\r\n")

        result = write(ctx, "win.txt", "x\ny\n")

        assert path.read_bytes() == _BOM + b"x\r\ny\r\n"
        assert result["data"]["bytes"] == path.stat().st_size

    def test_missing_content_is_an_error_not_an_empty_file(self, ctx: _Ctx, tmp_path: Path) -> None:
        result = asyncio.run(write_file({"path": "f.txt"}, ctx))

        assert result["ok"] is False
        assert not (tmp_path / "f.txt").exists()
        assert ctx.requested == []

    def test_an_empty_string_writes_an_empty_file(self, ctx: _Ctx, tmp_path: Path) -> None:
        assert write(ctx, "empty.txt", "")["ok"] is True
        assert (tmp_path / "empty.txt").read_bytes() == b""

    def test_a_json_object_as_content_is_written_as_json(self, ctx: _Ctx, tmp_path: Path) -> None:
        result = write(ctx, "data.json", {"a": [1, 2], "名": "值"})

        assert result["ok"] is True
        assert json.loads((tmp_path / "data.json").read_text(encoding="utf-8")) == {"a": [1, 2], "名": "值"}
        assert result["data"]["syntaxCheck"]["ok"] is True

    def test_a_file_deleted_after_reading_is_recreated_with_a_note(self, ctx: _Ctx, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"old\n")
        read(ctx, "f.txt")
        path.unlink()

        result = write(ctx, "f.txt", "back\n")

        assert result["ok"] is True and result["data"]["created"] is True
        assert "被删掉了" in result["data"]["result"]

    def test_a_denied_write_changes_nothing(self, tmp_path: Path) -> None:
        ctx = _Ctx(tmp_path, write_file="deny")
        path = seed(tmp_path / "f.txt", b"old\n")

        result = write(ctx, "f.txt", "new\n")

        assert result["ok"] is False
        assert path.read_bytes() == b"old\n"

    def test_a_directory_is_not_written(self, ctx: _Ctx, tmp_path: Path) -> None:
        (tmp_path / "dir").mkdir()

        assert write(ctx, "dir", "x")["ok"] is False

    def test_a_failed_write_names_the_file_without_its_absolute_path(
        self, ctx: _Ctx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 原因原样交给模型：OSError 的原文带着工作区的绝对路径和临时文件名
        write_module = importlib.import_module("toolbox.builtins.write_file")

        def refuse(*args: Any, **kwargs: Any) -> Any:
            raise PermissionError(13, "Permission denied", str(tmp_path / "notes" / ".f.txt.1234.tmp"))

        monkeypatch.setattr(write_module, "_commit", refuse)

        result = write(ctx, "notes/f.txt", "new\n")

        assert result["ok"] is False
        assert result["error"] == "写入 notes/f.txt 失败：Permission denied"
        assert str(tmp_path) not in result["data"]["result"]
        assert not (tmp_path / "notes" / "f.txt").exists()

    @pytest.mark.parametrize(
        "name,content,label",
        [
            ("bad.py", "def f(:\n    pass\n", "Python"),
            ("bad.json", '{"a": }\n', "JSON"),
            ("bad.yaml", "a: [1, 2\n", "YAML"),
            ("bad.toml", "a = \n", "TOML"),
        ],
    )
    def test_a_syntax_error_is_reported_and_the_file_is_kept(
        self, ctx: _Ctx, tmp_path: Path, name: str, content: str, label: str,
    ) -> None:
        result = write(ctx, name, content)

        assert result["ok"] is True
        assert f"语法检查未通过（{label}）" in result["data"]["result"]
        assert result["data"]["syntaxCheck"]["ok"] is False
        assert (tmp_path / name).read_bytes() == content.encode("utf-8")

    def test_a_broken_skill_frontmatter_is_reported_and_the_file_is_kept(self, ctx: _Ctx, tmp_path: Path) -> None:
        # 加载时写坏的技能整个被跳过：改完就得告诉模型，不然它以为技能已经生效了
        content = "---\nname: demo\nnote: sk-SECRET-skill: x\n---\n\n正文\n"

        result = write(ctx, "config/skills/demo/SKILL.md", content)

        assert result["ok"] is True
        assert "语法检查未通过（SKILL.md）：第 3 行第 " in result["data"]["result"]
        assert result["data"]["syntaxCheck"]["language"] == "skill"
        assert result["data"]["syntaxCheck"]["message"].startswith("frontmatter 的 YAML 读不出来：")
        assert "sk-SECRET" not in json.dumps(result, ensure_ascii=False)
        assert (tmp_path / "config" / "skills" / "demo" / "SKILL.md").read_bytes() == content.encode("utf-8")

    def test_a_yaml_value_that_cannot_be_built_is_reported_not_raised(self, ctx: _Ctx, tmp_path: Path) -> None:
        # 检查在写盘之后跑：值构造失败要报成未通过，抛出去的话工具报错，人会以为这次没写上
        content = "port: !!int sk-SECRET-port\nwhen: 2024-13-45\n"

        result = write(ctx, "bad.yaml", content)

        assert result["ok"] is True
        assert "语法检查未通过（YAML）：第 1 行第 7 列：值不是合法的 int" in result["data"]["result"]
        assert result["data"]["syntaxCheck"] == {
            "language": "yaml", "ok": False, "line": 1, "column": 7, "message": "值不是合法的 int",
        }
        assert "sk-SECRET" not in json.dumps(result, ensure_ascii=False)
        assert (tmp_path / "bad.yaml").read_bytes() == content.encode("utf-8")

    def test_a_valid_python_file_passes_the_check(self, ctx: _Ctx, tmp_path: Path) -> None:
        result = write(ctx, "ok.py", "x = 1\n")

        assert "语法检查通过（Python）" in result["data"]["result"]
        assert result["data"]["syntaxCheck"] == {"language": "python", "ok": True}


class TestApplyDiffIsAllOrNothing:
    def test_if_any_diff_fails_nothing_is_written(self, ctx: _Ctx, tmp_path: Path) -> None:
        original = b"alpha = 1\nbeta = 2\ngamma = 3\n"
        path = seed(tmp_path / "conf.py", original)

        result = diff(ctx, "conf.py", edit("alpha = 1", "alpha = 10"), edit("delta = 4", "delta = 40"))

        assert result["ok"] is False
        assert path.read_bytes() == original
        data = result["data"]
        assert data["appliedCount"] == 0 and data["failedCount"] == 1 and data["matchedCount"] == 1
        assert [item["index"] for item in data["failedDiffs"]] == [1]
        assert data["failedDiffs"][0]["reason"] == "not_found"
        assert [item["index"] for item in data["matchedDiffs"]] == [0]
        text = data["result"]
        assert text.startswith("ERROR: apply_diff 没有修改 conf.py")
        assert "diffs[1] 没能应用：没有找到匹配的内容" in text
        assert "diffs[0] 可以应用（精确匹配）" in text

    def test_ambiguous_diffs_list_the_candidate_lines(self, ctx: _Ctx, tmp_path: Path) -> None:
        seed(tmp_path / "f.txt", b"x = 1\ny = 2\nx = 1\n")

        result = diff(ctx, "f.txt", edit("x = 1", "x = 9"))

        failed = result["data"]["failedDiffs"][0]
        assert failed["reason"] == "ambiguous"
        assert failed["candidateLines"] == [1, 3]
        assert "start_line" in result["data"]["result"]

    def test_the_failure_shows_the_closest_snippet_with_line_numbers(self, ctx: _Ctx, tmp_path: Path) -> None:
        seed(tmp_path / "calc.py", b"def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n")
        read(ctx, "calc.py")

        result = diff(ctx, "calc.py", edit("def sub(a, b):\n    return a - c\n", "def sub(a, b):\n    return b - a\n"))

        text = result["data"]["result"]
        assert result["ok"] is False
        assert "最接近的是第 4-5 行" in text
        assert "   4 | def sub(a, b):" in text
        assert result["data"]["failedDiffs"][0]["closest"]["startLine"] == 4

    @pytest.mark.parametrize("level", ["deny", "approval_required"])
    def test_no_snippet_for_an_unread_file_that_needs_more_than_auto(self, tmp_path: Path, level: str) -> None:
        # 写得了不等于读得了：apply_diff 的失败信息不能变成读文件的后门，也不为片段另弹读审批
        ctx = _Ctx(tmp_path, read_file=level)
        seed(tmp_path / "calc.py", b"def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n")

        result = diff(ctx, "calc.py", edit("def sub(a, b):\n    return a - c\n", "x"))

        text = result["data"]["result"]
        assert "不提供相近的片段" in text
        assert "def sub" not in text and "return a - b" not in text
        assert "closest" not in result["data"]["failedDiffs"][0]
        assert "read_file" not in ctx.ops
        assert ctx.evaluated == [("read_file", {"path": "calc.py"})]

    def test_an_unread_file_readable_without_approval_gets_the_snippet(self, ctx: _Ctx, tmp_path: Path) -> None:
        seed(tmp_path / "calc.py", b"def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n")

        result = diff(ctx, "calc.py", edit("def sub(a, b):\n    return a - c\n", "x"))

        text = result["data"]["result"]
        assert "最接近的是第 4-5 行" in text
        assert result["data"]["failedDiffs"][0]["closest"]["startLine"] == 4
        assert ctx.ops == ["write_file"]
        assert ctx.evaluated == [("read_file", {"path": "calc.py"})]

    def test_a_failure_without_a_close_match_does_not_ask_about_reading(self, tmp_path: Path) -> None:
        ctx = _Ctx(tmp_path, read_file="deny")
        seed(tmp_path / "f.txt", b"alpha\n")

        result = diff(ctx, "f.txt", edit("completely different text that is nowhere", "x"))

        assert result["ok"] is False
        assert ctx.evaluated == []

    def test_malformed_diffs_are_rejected_before_asking_for_approval(self, ctx: _Ctx, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"x\n")

        result = asyncio.run(apply_diff({"path": "f.txt", "diffs": [
            {"replace": "y"},
            {"search": "x", "replace": "y", "start_line": "abc"},
            {"search": "", "replace": "y"},
        ]}, ctx))

        assert result["ok"] is False
        assert ctx.requested == []
        assert [item["index"] for item in result["data"]["failedDiffs"]] == [0, 1, 2]
        assert path.read_bytes() == b"x\n"

    def test_a_missing_file_is_an_error(self, ctx: _Ctx, tmp_path: Path) -> None:
        result = diff(ctx, "missing.txt", edit("a", "b"))

        assert result["ok"] is False
        assert "文件不存在" in result["error"]

    def test_a_failed_read_or_write_names_the_file_without_its_absolute_path(
        self, ctx: _Ctx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        edit_flow = importlib.import_module("toolbox.builtins._edit_flow")
        path = seed(tmp_path / "f.txt", b"alpha\n")

        def refuse(*args: Any, **kwargs: Any) -> Any:
            raise PermissionError(13, "Permission denied", str(path))

        monkeypatch.setattr(edit_flow, "transact_text", refuse)

        result = diff(ctx, "f.txt", edit("alpha", "beta"))

        assert result["ok"] is False
        assert result["error"] == "读写 f.txt 失败：Permission denied"
        assert str(tmp_path) not in result["data"]["result"]
        assert path.read_bytes() == b"alpha\n"

    def test_a_binary_file_is_refused(self, ctx: _Ctx, tmp_path: Path) -> None:
        path = seed(tmp_path / "blob.bin", b"\xff\xfe\x00\x01")

        result = diff(ctx, "blob.bin", edit("a", "b"))

        assert result["ok"] is False and "不是 UTF-8 文本" in result["error"]
        assert path.read_bytes() == b"\xff\xfe\x00\x01"

    def test_a_file_over_the_size_limit_is_refused(
        self, ctx: _Ctx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(apply_diff_module, "_MAX_FILE_SIZE", 8)
        seed(tmp_path / "big.txt", b"0123456789\n")

        result = diff(ctx, "big.txt", edit("0", "1"))

        assert result["ok"] is False
        assert result["data"]["fileSize"] == 11 and result["data"]["maxFileSize"] == 8


class TestApplyDiffWrites:
    def test_crlf_and_bom_are_preserved(self, ctx: _Ctx, tmp_path: Path) -> None:
        path = seed(tmp_path / "win.txt", _BOM + b"a: 1\r\nb: 2\r\nc: 3\r\n")

        result = diff(ctx, "win.txt", edit("b: 2\n", "b: 20\nb2: 21\n"))

        assert result["ok"] is True
        assert path.read_bytes() == _BOM + b"a: 1\r\nb: 20\r\nb2: 21\r\nc: 3\r\n"

    def test_a_lone_carriage_return_elsewhere_survives(self, ctx: _Ctx, tmp_path: Path) -> None:
        path = seed(tmp_path / "cr.txt", b"keep\rthis\nchange me\n")

        diff(ctx, "cr.txt", edit("change me", "changed"))

        assert path.read_bytes() == b"keep\rthis\nchanged\n"

    def test_the_edit_is_echoed_with_line_numbers_and_context(self, ctx: _Ctx, tmp_path: Path) -> None:
        seed(tmp_path / "ten.txt", "".join(f"line {i}\n" for i in range(1, 11)).encode("utf-8"))
        read(ctx, "ten.txt")

        result = diff(ctx, "ten.txt", edit("line 5\n", "line five\n"))

        text = result["data"]["result"]
        assert result["ok"] is True
        assert "diffs[0] 精确匹配，改动后在第 5 行" in text
        assert "── 第 3-7 行（diffs[0]）──" in text
        for expected in ("   3 | line 3", "   5 | line five", "   7 | line 7"):
            assert expected in text
        assert "   2 | line 2" not in text and "   8 | line 8" not in text
        assert result["data"]["diffs"][0]["startLine"] == 5
        assert result["data"]["echoed"] is True

    def test_the_level_used_is_reported(self, ctx: _Ctx, tmp_path: Path) -> None:
        seed(tmp_path / "f.py", b"def f():\n    if x:\n        return 1\n")

        result = diff(ctx, "f.py", edit("if x:\n    return 1\n", "if x:\n    return 2\n"))

        assert result["data"]["diffs"][0]["level"] == "indentation"
        assert "缩进弹性" in result["data"]["result"]
        assert (tmp_path / "f.py").read_bytes() == b"def f():\n    if x:\n        return 2\n"

    def test_several_diffs_apply_in_order_with_a_compensated_start_line(self, ctx: _Ctx, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"head\nx\nmid\nx\n")

        result = diff(ctx, "f.txt", edit("head\n", "head\nnew1\nnew2\n"), edit("x\n", "y\n", start_line=4))

        assert result["ok"] is True
        assert path.read_bytes() == b"head\nnew1\nnew2\nx\nmid\ny\n"

    def test_an_identical_replacement_writes_nothing(self, ctx: _Ctx, tmp_path: Path) -> None:
        path = seed(tmp_path / "f.txt", b"same\n")
        before = path.stat().st_mtime_ns

        result = diff(ctx, "f.txt", edit("same", "same"))

        assert result["ok"] is True and result["data"]["changed"] is False
        assert "没有写盘" in result["data"]["result"]
        assert path.stat().st_mtime_ns == before

    def test_a_python_edit_that_breaks_the_syntax_is_written_and_reported(self, ctx: _Ctx, tmp_path: Path) -> None:
        path = seed(tmp_path / "m.py", b"def f():\n    return 1\n")

        result = diff(ctx, "m.py", edit("    return 1\n", "    return (1\n"))

        assert result["ok"] is True
        assert "语法检查未通过（Python）" in result["data"]["result"]
        assert result["data"]["syntaxCheck"]["ok"] is False
        assert path.read_bytes() == b"def f():\n    return (1\n"

    def test_the_echo_needs_read_permission(self, tmp_path: Path) -> None:
        ctx = _Ctx(tmp_path, read_file="deny")
        path = seed(tmp_path / "secret.txt", b"token=abc\nother=1\n")

        result = diff(ctx, "secret.txt", edit("other=1", "other=2"))

        text = result["data"]["result"]
        assert result["ok"] is True
        assert "不回显改动处的原文" in text
        assert "token=abc" not in text
        assert result["data"]["echoed"] is False
        assert "read_file" not in ctx.ops
        assert ctx.evaluated == [("read_file", {"path": "secret.txt"})]
        assert path.read_bytes() == b"token=abc\nother=2\n"

    def test_a_file_read_first_needs_no_second_read_check(self, ctx: _Ctx, tmp_path: Path) -> None:
        seed(tmp_path / "f.txt", b"a\nb\n")
        read(ctx, "f.txt")

        diff(ctx, "f.txt", edit("a", "A"))

        assert ctx.ops == ["read_file", "write_file"]
        assert ctx.evaluated == []

    def test_an_unread_file_that_needs_read_approval_gets_line_numbers_only(self, tmp_path: Path) -> None:
        # 读要审批的文件（data/** 之类）写入批准之后，不能为几行回显再弹一张读审批卡
        ctx = _Ctx(tmp_path, read_file="approval_required")
        seed(tmp_path / "f.txt", b"a\nb\n")

        result = diff(ctx, "f.txt", edit("a", "A"))

        text = result["data"]["result"]
        assert ctx.ops == ["write_file"]
        assert ctx.evaluated == [("read_file", {"path": "f.txt"})]
        assert result["data"]["echoed"] is False
        assert "改动后在第 1 行" in text
        assert "不回显改动处的原文" in text

    def test_an_unread_file_readable_without_approval_is_echoed(self, ctx: _Ctx, tmp_path: Path) -> None:
        seed(tmp_path / "f.txt", b"a\nb\n")

        result = diff(ctx, "f.txt", edit("a", "A"))

        text = result["data"]["result"]
        assert ctx.ops == ["write_file"]
        assert ctx.evaluated == [("read_file", {"path": "f.txt"})]
        assert result["data"]["echoed"] is True
        assert "   1 | A" in text

    def test_a_failed_read_policy_check_only_costs_the_echo(self, tmp_path: Path) -> None:
        class Unreachable(_Ctx):
            async def evaluate_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
                raise RuntimeError("supervisor unreachable")

        ctx = Unreachable(tmp_path)
        path = seed(tmp_path / "f.txt", b"a\nb\n")

        result = diff(ctx, "f.txt", edit("a", "A"))

        assert result["ok"] is True and result["data"]["echoed"] is False
        assert path.read_bytes() == b"A\nb\n"

    def test_external_checks_go_through_the_execute_command_approval(self, tmp_path: Path) -> None:
        ctx = _Ctx(tmp_path, execute_command="deny")
        runtime = config_layout.runtime_path(tmp_path)
        runtime.parent.mkdir(parents=True, exist_ok=True)
        runtime.write_text(
            yaml.safe_dump({"edit_checks": {"commands": [{"glob": "*.py", "command": "ruff check {path}"}]}}),
            encoding="utf-8",
        )
        seed(tmp_path / "pkg" / "m.py", b"x = 1\n")

        result = diff(ctx, "pkg/m.py", edit("x = 1", "x = 2"))

        assert result["ok"] is True
        commands = [params["command"] for op, params in ctx.requested if op == "execute_command"]
        assert commands == [f"ruff check {quote_path('pkg/m.py')}"]
        assert "外部检查没有执行" in result["data"]["result"]
        assert result["data"]["externalChecks"][0]["status"] == "denied"
        assert (tmp_path / "pkg" / "m.py").read_bytes() == b"x = 2\n"


class TestProgressSummaries:
    def test_a_write_whose_syntax_check_failed_says_so(self, ctx: _Ctx) -> None:
        result = write(ctx, "bad.json", "{")

        assert summarize_result("write_file", result, args={"path": "bad.json"}) == "已写入 bad.json，语法检查未通过"

    def test_an_apply_diff_that_changed_nothing_says_so(self, ctx: _Ctx, tmp_path: Path) -> None:
        seed(tmp_path / "f.txt", b"same\n")

        result = diff(ctx, "f.txt", edit("same", "same"))

        summary = summarize_result("apply_diff", result, args={"path": "f.txt", "diffs": [{}]})
        assert summary == "差异应用 f.txt (1 处修改，内容无变化)"

    def test_files_skipped_by_the_budget_show_up_in_the_summary(self, ctx: _Ctx, tmp_path: Path) -> None:
        for name in ("a.txt", "b.txt", "c.txt"):
            seed(tmp_path / name, wide_file())

        result = read(ctx, "a.txt", "b.txt", "c.txt")

        assert summarize_result("read_file", result, args={}) == "读取 3 个文件: 2 成功, 0 失败, 1 个超出单次上限未读"

    def test_a_failed_apply_diff_summary_carries_the_reason(self, ctx: _Ctx, tmp_path: Path) -> None:
        seed(tmp_path / "f.txt", b"a\n")

        result = diff(ctx, "f.txt", edit("zzz-not-there", "b"))

        summary = summarize_result("apply_diff", result, args={"path": "f.txt", "diffs": [{}]})
        assert summary.startswith("失败: 1 个 diff 没能应用，文件没有改动")
