"""关键词编译与 allowlist 的两组语义。

`/api/config` 原来会变成 re.compile("api", IGNORECASE)——判据是「以 / 开头且后面还有 /」，
flags 从任意尾串里瞎抠，config 里恰好有个 i。而纯 ASCII 短词按子串匹配等同通配符：
`ai` 命中 said / mail / again。

`memories: {mode: allowlist}` 不写 allow 反而全部放行，和 `skills:` 完全相同的配置形状
语义相反：空 list 是 falsy，被当成「没声明限制」。
"""
from __future__ import annotations

import re
import sys
from collections import OrderedDict
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.builtin import knowledge_inject  # noqa: E402
from engine.builtin.knowledge_inject import (  # noqa: E402
    _filter_entries,
    compile_keyword,
    match_keywords,
)


@pytest.fixture(autouse=True)
def _fresh_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    # 记过日志的关键词存在进程里，用例之间不能串
    monkeypatch.setattr(knowledge_inject, "_WARNED_KEYWORDS", OrderedDict())


def _matches(keyword: str, text: str) -> bool:
    return match_keywords([compile_keyword(keyword)], text)


class TestRegexKeywordsNeedTheWholeForm:
    @pytest.mark.parametrize("keyword", ["/api/config", "/home/user", "/etc/hosts"])
    def test_a_path_like_keyword_is_literal(self, keyword: str) -> None:
        compiled = compile_keyword(keyword)

        assert not isinstance(compiled, re.Pattern), f"{keyword} 被当成正则了"
        assert _matches(keyword, f"请看 {keyword} 这个文件")

    def test_a_path_like_keyword_no_longer_matches_its_first_segment(self) -> None:
        # 旧行为：/api/config → re.compile("api", IGNORECASE)，任何含 api 的文本都命中。
        assert not _matches("/api/config", "这是 api 文档")

    @pytest.mark.parametrize("keyword,flags", [
        ("/abc/", 0),
        ("/abc/i", re.IGNORECASE),
        ("/abc/is", re.IGNORECASE | re.DOTALL),
        ("/abc/m", re.MULTILINE),
    ])
    def test_a_proper_regex_keyword_still_compiles(self, keyword: str, flags: int) -> None:
        compiled = compile_keyword(keyword)

        assert isinstance(compiled, re.Pattern)
        assert compiled.flags & flags == flags

    def test_a_slash_command_stays_literal(self) -> None:
        # 单斜杠开头一直是安全的，别在收紧时把它带走。
        compiled = compile_keyword("/生图")

        assert not isinstance(compiled, re.Pattern)
        assert _matches("/生图", "帮我 /生图 一只猫")

    def test_a_broken_regex_falls_back_to_literal_with_a_warning(self, caplog) -> None:
        with caplog.at_level("WARNING"):
            compiled = compile_keyword("/[unclosed/i")

        assert not isinstance(compiled, re.Pattern)
        assert any("not a valid regex" in r.getMessage() for r in caplog.records)

    def test_a_reinterpreted_keyword_is_logged(self, caplog) -> None:
        # 语义翻转必须留痕，否则召回集合变了而没人知道。
        with caplog.at_level("INFO"):
            compile_keyword("/api/config")

        assert any("matched literally" in str(r.getMessage()) for r in caplog.records)

    @pytest.mark.parametrize("keyword,needle", [
        ("/[unclosed/i", "not a valid regex"),
        ("/api/config", "matched literally"),
    ])
    def test_the_same_keyword_is_logged_once(self, caplog, keyword: str, needle: str) -> None:
        # 技能目录和记忆本两秒就重扫一次，写错的关键词不改就每次都记一遍。
        with caplog.at_level("INFO"):
            for _ in range(5):
                compile_keyword(keyword)
            compile_keyword(keyword.replace("unclosed", "still-unclosed").replace("config", "settings"))

        assert sum(needle in r.getMessage() for r in caplog.records) == 2

    @pytest.mark.parametrize("keyword", ["/a{4294967296}/", "/" + "(" * 1000 + "x" + ")" * 1000 + "/"])
    def test_a_regex_python_gives_up_on_falls_back_to_literal(self, caplog, keyword: str) -> None:
        # 重复次数太大抛 OverflowError、嵌套太深抛 RecursionError，都不是 re.error：接不住就打断整轮注入。
        with caplog.at_level("WARNING"):
            compiled = compile_keyword(keyword)

        assert compiled == keyword.lower()
        assert any("not a valid regex" in r.getMessage() for r in caplog.records)


class TestShortAsciiKeywordsNeedBoundaries:
    @pytest.mark.parametrize("text", ["said", "mail", "again", "detail"])
    def test_a_two_letter_keyword_no_longer_matches_inside_words(self, text: str) -> None:
        assert not _matches("ai", text)

    @pytest.mark.parametrize("text", ["ai 很有意思", "关于 AI 的讨论", "(ai)", "ai,", "什么是ai"])
    def test_it_still_matches_a_standalone_occurrence(self, text: str) -> None:
        assert _matches("ai", text)

    def test_matching_stays_case_insensitive(self) -> None:
        assert _matches("ai", "AI 是什么")
        assert _matches("AI", "ai 是什么")

    def test_a_longer_ascii_keyword_keeps_substring_matching(self) -> None:
        assert _matches("python", "pythonista")

    def test_a_short_cjk_keyword_keeps_substring_matching(self) -> None:
        # 中文没有词边界可用，`猫` 只能保子串 —— 这是语言的固有限制，不是漏改。
        assert _matches("猫", "猫娘")
        assert _matches("猫", "熊猫")

    def test_a_short_keyword_next_to_cjk_still_matches(self) -> None:
        # 不能用 \b：CJK 也算 \w，`\bai\b` 在「他说ai很好」里不成立，会反过来漏掉。
        assert _matches("ai", "他说ai很好")


class TestAllowlistIsFailClosed:
    def _entry(self, kind: str, **extra) -> dict:
        base = {"kind": kind, "id": "e1", "book": "b1", "node_ids": []}
        base.update(extra)
        return base

    def test_an_empty_memory_allowlist_denies_everything(self) -> None:
        """作者写 allowlist 是为了收紧，原来的空表反而全部放行。"""
        kept = _filter_entries(
            [self._entry("memory")], node_id="n1", memory_mode="allowlist", memory_allow=[],
        )

        assert kept == []

    def test_an_empty_skill_allowlist_denies_everything(self) -> None:
        kept = _filter_entries(
            [self._entry("skill")], node_id="n1", skill_mode="allowlist", skill_allow=[],
        )

        assert kept == []

    def test_memory_and_skill_agree_on_the_same_config_shape(self) -> None:
        entries = [self._entry("memory"), self._entry("skill")]

        kept = _filter_entries(
            entries, node_id="n1",
            skill_mode="allowlist", skill_allow=[],
            memory_mode="allowlist", memory_allow=[],
        )

        assert kept == []

    def test_an_unset_allowlist_still_means_no_restriction(self) -> None:
        # legacy 入口默认传 None，不能跟着空表一起被禁掉。
        kept = _filter_entries(
            [self._entry("memory")], node_id="n1", memory_mode="allowlist", memory_allow=None,
        )

        assert len(kept) == 1

    def test_a_populated_allowlist_filters_by_book(self) -> None:
        entries = [self._entry("memory", book="allowed"), self._entry("memory", id="e2", book="denied")]

        kept = _filter_entries(
            entries, node_id="n1", memory_mode="allowlist", memory_allow=["allowed"],
        )

        assert [e["book"] for e in kept] == ["allowed"]

    def test_mode_all_ignores_the_allowlist(self) -> None:
        kept = _filter_entries(
            [self._entry("memory")], node_id="n1", memory_mode="all", memory_allow=[],
        )

        assert len(kept) == 1
