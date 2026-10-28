"""SKILL.md 的 frontmatter 写坏了要说出来，不按空元数据生效。

按空元数据算的话，enabled: false 的技能被打开，strategy: output 的输出规则成了普通技能、trigger 丢了，
关键词、node_ids、order 全丢；create_or_update_skill 还会把写坏的那段丢掉、按参数重拼一份写回去。
没有 frontmatter（第一行不是 ---）照旧按没有元数据算，不算写坏。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import config_layout  # noqa: E402
from _file_edits_fake import FakeFileEdits  # noqa: E402
from engine.builtin import knowledge_inject  # noqa: E402
from engine.builtin.dream import DreamHandler  # noqa: E402
from engine.builtin.knowledge_inject import (  # noqa: E402
    SkillFrontmatterError,
    build_knowledge_context,
    create_or_update_skill,
    list_skills,
    load_memory_catalog,
    load_skill_catalog,
    output_rule_skills,
    parse_skill_frontmatter,
    skill_rows,
)
from engine.memory_namespace import conversation_namespace  # noqa: E402
from engine.node import Node, SkillAccess, ToolAccess  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402

_LOGGER = "engine.builtin.knowledge_inject"
# 坏的那一行上写着 sk-SECRET：日志、列表、工具结果里只能有位置和问题。
_BROKEN = "---\nname: guard\nstrategy: output\ntrigger: BAD\nnote: sk-SECRET-skill: x\nenabled: false\n---\n别说 BAD。\n"
_CONV_KEY = "qq_group:bc3f12b0621298e191a49fa7"


@pytest.fixture(autouse=True)
def _fresh_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    # 目录缓存按工作区存两秒，用例之间不能串
    monkeypatch.setattr(knowledge_inject._SkillCache, "_entries", {})


def _skill(root: Path, directory: str, text: str) -> Path:
    path = config_layout.skills_dir(root) / directory / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    config_layout.write_config_text(path, text)
    return path


def _warnings(caplog: pytest.LogCaptureFixture, needle: str) -> list[str]:
    return [
        record.getMessage() for record in caplog.records
        if record.name == _LOGGER and record.levelno == logging.WARNING and needle in record.getMessage()
    ]


class _Ctx(FakeFileEdits, ToolContext):
    """离线 ToolContext：写 skill 走 write_file 的认领和送审，认领交给 FakeSupervisor，送审一律放行。"""

    def __init__(self, workspace_root: Path) -> None:
        super().__init__(
            supervisor_url="http://unused", session_id="sess-skill", run_id="run-1", worker_id="worker-1",
            workspace_root=workspace_root, http=None, registry=None,  # type: ignore[arg-type]
        )

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return {"safety_level": "auto", "reason": "test"}

    async def check_cancelled(self) -> bool:
        return False


def _node() -> Node:
    return Node(
        id="node-1", type="ai", prompt="Work carefully.", tool_mode="native",
        tool_access=ToolAccess(mode="all"), skill_access=SkillAccess(),
    )


def _written_meta(root: Path, directory: str) -> dict[str, Any]:
    text = (config_layout.skills_dir(root) / directory / "SKILL.md").read_text(encoding="utf-8")
    meta, _body = parse_skill_frontmatter(text)
    return meta


# ---------------------------------------------------------------------------
#  拆 frontmatter
# ---------------------------------------------------------------------------

class TestTheParser:
    def test_no_frontmatter_means_no_metadata(self) -> None:
        assert parse_skill_frontmatter("只有正文。\n---\n后面也算正文。\n") == ({}, "只有正文。\n---\n后面也算正文。\n")

    def test_the_body_starts_right_after_the_closing_line(self) -> None:
        assert parse_skill_frontmatter("---\nname: a\n---\n\n正文\n") == ({"name": "a"}, "\n正文\n")

    @pytest.mark.parametrize("text", [
        pytest.param("\ufeff---\nname: a\n---\n正文", id="bom"),
        pytest.param("---\r\nname: a\r\n---\r\n正文", id="crlf"),
        pytest.param("---  \nname: a\n---\t\n正文", id="trailing-spaces"),
    ])
    def test_a_bom_crlf_or_trailing_spaces_still_open_and_close_it(self, text: str) -> None:
        meta, body = parse_skill_frontmatter(text)

        assert meta == {"name": "a"}
        assert body.rstrip("\r") == "正文"

    @pytest.mark.parametrize("text", ["---\n---\n正文", "---\n# 先空着\n---\n正文"])
    def test_an_empty_frontmatter_is_no_metadata(self, text: str) -> None:
        assert parse_skill_frontmatter(text) == ({}, "正文")

    def test_an_unclosed_frontmatter_is_an_error(self) -> None:
        with pytest.raises(SkillFrontmatterError) as caught:
            parse_skill_frontmatter("---\nname: a\n正文\n")

        assert (caught.value.line, caught.value.column) == (1, None)
        assert str(caught.value) == "第 1 行：frontmatter 没有收尾，后面没有单独一行的 ---"

    def test_a_yaml_error_is_placed_on_the_line_of_the_file(self) -> None:
        with pytest.raises(SkillFrontmatterError) as caught:
            parse_skill_frontmatter(_BROKEN)

        assert (caught.value.line, caught.value.column) == (5, 22)
        assert caught.value.problem == "frontmatter 的 YAML 读不出来：mapping values are not allowed here"
        assert str(caught.value) == f"第 5 行第 22 列：{caught.value.problem}"
        assert "sk-SECRET" not in str(caught.value)

    @pytest.mark.parametrize("text", ["---\n- a\n- b\n---\n正文", "---\n就一句话\n---\n正文"])
    def test_a_frontmatter_that_is_not_a_mapping_is_an_error(self, text: str) -> None:
        with pytest.raises(SkillFrontmatterError, match="frontmatter 顶层不是 key: value 的映射"):
            parse_skill_frontmatter(text)

    def test_a_value_yaml_cannot_build_is_an_error(self) -> None:
        with pytest.raises(SkillFrontmatterError) as caught:
            parse_skill_frontmatter("---\nname: a\norder: !!int sk-SECRET-order\n---\n正文")

        assert caught.value.line == 3
        assert "sk-SECRET" not in str(caught.value)


# ---------------------------------------------------------------------------
#  注入和输出规则
# ---------------------------------------------------------------------------

class TestLoading:
    def test_a_broken_skill_is_neither_injected_nor_an_output_rule(self, tmp_path: Path) -> None:
        _skill(tmp_path, "guard", _BROKEN)
        _skill(tmp_path, "demo", "---\nname: demo\nstrategy: constant\n---\nDemo body.\n")
        node = SimpleNamespace(
            id="node-a", aliases=(), extra={},
            skill_access=SkillAccess(mode="all"), memory_access=SimpleNamespace(mode="none", allow=None),
        )

        static, dynamic, _memory_static, _memory_dynamic = build_knowledge_context(tmp_path, node, "BAD", [], {})

        assert [item["dir"] for item in load_skill_catalog(tmp_path, _use_cache=False)] == ["demo"]
        rendered = json.dumps(static + dynamic, ensure_ascii=False)
        assert "Demo body." in rendered and "别说 BAD" not in rendered
        assert output_rule_skills(tmp_path, _node()) == []

    def test_a_broken_version_is_logged_once_and_a_new_one_again(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        path = _skill(tmp_path, "guard", _BROKEN)

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            for _ in range(3):
                load_skill_catalog(tmp_path, _use_cache=False)
            assert len(_warnings(caplog, "skill guard skipped")) == 1

            config_layout.write_config_text(path, "---\nname: guard\nkeywords: [a\n---\n正文\n")
            load_skill_catalog(tmp_path, _use_cache=False)

        warnings = _warnings(caplog, "skill guard skipped")
        assert len(warnings) == 2
        assert "第 5 行第 22 列" in warnings[0] and "第 3 行" in warnings[1]
        assert "sk-SECRET" not in caplog.text

    def test_fixing_the_file_brings_the_skill_back(self, tmp_path: Path) -> None:
        path = _skill(tmp_path, "guard", _BROKEN)
        assert load_skill_catalog(tmp_path, _use_cache=False) == []

        config_layout.write_config_text(path, "---\nname: guard\nstrategy: output\ntrigger: BAD\n---\n别说 BAD。\n")

        [item] = load_skill_catalog(tmp_path, _use_cache=False)
        assert item["strategy"] == "output" and item["output_rule"]["pattern"].search("xBADx")

    def test_a_file_that_is_not_utf8_is_skipped_with_the_reason(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        path = config_layout.skills_dir(tmp_path) / "bytes" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"---\nname: bytes\n---\n\xff\xfe\n")

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert load_skill_catalog(tmp_path, _use_cache=False) == []

        assert "读不了：不是 UTF-8 编码" in caplog.text

    def test_a_skill_without_frontmatter_is_loaded_as_before(self, tmp_path: Path) -> None:
        _skill(tmp_path, "plain", "只有正文。\n")

        [item] = load_skill_catalog(tmp_path, _use_cache=False)

        assert (item["name"], item["dir"], item["strategy"], item["body"]) == ("plain", "plain", "normal", "只有正文。")

    def test_a_number_yaml_reads_as_infinity_falls_back_instead_of_breaking_the_scan(self, tmp_path: Path) -> None:
        _skill(tmp_path, "huge", "---\nname: huge\norder: .inf\npriority: .nan\n---\n正文\n")

        [item] = load_skill_catalog(tmp_path, _use_cache=False)

        assert (item["order"], item["priority"]) == (0, 0)

    @pytest.mark.parametrize("pattern", ["/a{4294967296}/", "/" + "(" * 1000 + "x" + ")" * 1000 + "/"])
    def test_a_regex_keyword_python_cannot_compile_is_matched_literally(self, tmp_path: Path, pattern: str) -> None:
        # 重复次数太大抛 OverflowError、嵌套太深抛 RecursionError，都不是 re.error
        _skill(tmp_path, "odd", "---\n" + yaml.safe_dump({"name": "odd", "keywords": [pattern]}) + "---\n正文\n")

        [item] = load_skill_catalog(tmp_path, _use_cache=False)

        assert item["compiled_keywords"] == [pattern.lower()]


# ---------------------------------------------------------------------------
#  list_skills 和控制台的列表
# ---------------------------------------------------------------------------

class TestListing:
    def test_a_broken_skill_is_listed_with_its_problem(self, tmp_path: Path) -> None:
        _skill(tmp_path, "guard", _BROKEN)
        _skill(tmp_path, "fine", "---\nname: Fine Skill\nenabled: false\nkeywords: 单个关键词\n---\n正文\n")

        result = asyncio.run(list_skills({}, _Ctx(tmp_path)))

        rows = {row["dir"]: row for row in result["data"]["skills"]}
        assert rows["guard"] == {
            "name": "guard", "dir": "guard", "path": "config/skills/guard/SKILL.md",
            "problem": "第 5 行第 22 列：frontmatter 的 YAML 读不出来：mapping values are not allowed here",
        }
        assert (rows["fine"]["name"], rows["fine"]["enabled"], rows["fine"]["keywords"]) == (
            "Fine Skill", False, ["单个关键词"],
        )
        assert result["data"]["result"] == "2 skills, 1 unusable (see problem)"
        assert "sk-SECRET" not in json.dumps(result, ensure_ascii=False)

    def test_the_tool_and_the_console_list_the_same_values(self, tmp_path: Path) -> None:
        _skill(tmp_path, "guard", "---\nname: guard\nstrategy: weird\norder: 3\nnode_ids: [a]\n---\n正文\n")

        listed = asyncio.run(list_skills({}, _Ctx(tmp_path)))["data"]["skills"]
        [console] = skill_rows(tmp_path, preview_chars=200)

        assert listed == [{key: value for key, value in console.items() if key != "body_preview"}]
        assert (console["strategy"], console["order"], console["node_ids"]) == ("normal", 3, ["a"])
        assert console["body_preview"] == "正文"


# ---------------------------------------------------------------------------
#  create_or_update_skill
# ---------------------------------------------------------------------------

class TestWriting:
    def test_a_content_with_a_broken_frontmatter_is_refused_and_nothing_is_written(self, tmp_path: Path) -> None:
        path = _skill(tmp_path, "guard", "---\nname: guard\nnode_ids: [a]\norder: 4\n---\n旧的。\n")

        result = asyncio.run(create_or_update_skill({"name": "guard", "content": _BROKEN}, _Ctx(tmp_path)))

        assert result["ok"] is False
        assert "nothing was written" in result["error"] and "第 5 行第 22 列" in result["error"]
        assert "sk-SECRET" not in json.dumps(result, ensure_ascii=False)
        assert path.read_text(encoding="utf-8") == "---\nname: guard\nnode_ids: [a]\norder: 4\n---\n旧的。\n"

    def test_the_fields_of_a_good_content_are_kept(self, tmp_path: Path) -> None:
        content = "---\nnode_ids: [writer]\norder: 4\nkeywords: [a]\n---\n正文。\n"

        result = asyncio.run(create_or_update_skill({"name": "guard", "content": content}, _Ctx(tmp_path)))

        assert result["ok"] is True
        meta = _written_meta(tmp_path, "guard")
        assert (meta["node_ids"], meta["order"], meta["keywords"]) == (["writer"], 4, ["a"])

    @pytest.mark.parametrize(("arguments", "fragment"), [
        pytest.param({"order": "abc"}, "order must be a whole number, got 'abc'", id="order-text"),
        pytest.param({"priority": 1.5}, "priority must be a whole number, got 1.5", id="priority-fraction"),
        pytest.param({"scan_depth": True}, "scan_depth must be a whole number, got True", id="scan-depth-bool"),
        pytest.param({"strategy": "weird"}, "strategy must be one of constant, normal, output", id="strategy"),
        pytest.param({"keywords": "a, b"}, "keywords must be a list of strings", id="keywords-text"),
        pytest.param({"content": ["---"]}, "content must be the SKILL.md text as a string", id="content-list"),
        pytest.param({"content": "---\norder: '5'\n---\n正文\n"}, "order must be a whole number, got '5'", id="content-order"),
        pytest.param({"content": "---\nstrategy: Sometimes\n---\n正文\n"}, "strategy must be one of", id="content-strategy"),
    ])
    def test_a_value_loading_would_silently_replace_is_refused(
        self, tmp_path: Path, arguments: dict[str, Any], fragment: str,
    ) -> None:
        result = asyncio.run(create_or_update_skill({"name": "guard", **arguments}, _Ctx(tmp_path)))

        assert result["ok"] is False and fragment in result["error"]
        assert not (config_layout.skills_dir(tmp_path) / "guard").exists()

    def test_whole_numbers_written_as_text_or_floats_are_accepted(self, tmp_path: Path) -> None:
        arguments = {"name": "guard", "order": "5", "priority": 2.0, "scan_depth": -3}

        result = asyncio.run(create_or_update_skill(arguments, _Ctx(tmp_path)))

        assert result["ok"] is True
        meta = _written_meta(tmp_path, "guard")
        assert (meta["order"], meta["priority"], meta["scan_depth"]) == (5, 2, 0)


# ---------------------------------------------------------------------------
#  记忆本目录
# ---------------------------------------------------------------------------

def _book(root: Path, name: str, text: str) -> Path:
    path = root / "data" / "memory" / conversation_namespace(_CONV_KEY) / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    config_layout.write_config_text(path, text)
    return path


class TestMemoryBooks:
    @pytest.mark.parametrize("text,reason", [
        pytest.param("- 一条\n- 两条\n", "top level is a list, not a mapping", id="list"),
        pytest.param("book: 杂项\nentries: 一条\n", "entries is a str, not a list", id="entries-text"),
    ])
    def test_a_book_of_the_wrong_shape_is_skipped_and_said_once(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, text: str, reason: str,
    ) -> None:
        _book(tmp_path, "杂项", text)
        _book(tmp_path, "习惯", yaml.safe_dump({"book": "习惯", "entries": [{"id": "m1", "content": "内容", "keywords": ["a"]}]}))
        namespace = conversation_namespace(_CONV_KEY)

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            for _ in range(3):
                catalog = load_memory_catalog(tmp_path, memory_book=namespace, _use_cache=False)

        assert [entry["id"] for entry in catalog] == ["m1"]
        assert _warnings(caplog, "memory book 杂项.yaml") == [f"memory book 杂项.yaml skipped, unreadable: {reason}"]

    def test_a_number_yaml_reads_as_infinity_does_not_drop_the_book(self, tmp_path: Path) -> None:
        _book(tmp_path, "习惯", "book: 习惯\nentries:\n- id: m1\n  content: 内容\n  keywords: [a]\n  priority: .inf\n")

        [entry] = load_memory_catalog(tmp_path, memory_book=conversation_namespace(_CONV_KEY), _use_cache=False)

        assert entry["priority"] == 0


# ---------------------------------------------------------------------------
#  dream 的技能清单
# ---------------------------------------------------------------------------

class TestTheDreamSkillList:
    def test_a_broken_skill_is_still_listed_by_its_directory(self, tmp_path: Path) -> None:
        # 漏掉它，Promote 阶段会以为没有同主题的技能，再建一个
        _skill(tmp_path, "guard", _BROKEN)
        _skill(tmp_path, "writing", "---\nname: writing\ndescription: |\n  写作规范，\n  分两行写。\n---\n正文\n")

        listing = DreamHandler()._load_skill_list(tmp_path)

        assert listing.splitlines() == [
            "- guard: (读不了：第 5 行第 22 列：frontmatter 的 YAML 读不出来：mapping values are not allowed here)",
            "- writing: 写作规范， 分两行写。",
        ]
        assert "sk-SECRET" not in listing
