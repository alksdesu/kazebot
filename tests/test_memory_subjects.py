"""按人记忆：建档门槛、识别涉及的人、跨会话加载。

记忆原先只按会话 namespace 存、靠关键词召回，没有「这条记忆关于谁」的维度：
@ 张三之后 bot 对张三一无所知，因为「张三」三个字不在当轮句子里。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# NoneBot 不是核心测试环境的依赖，因此单文件加载纯逻辑模块而不 import 整个插件。
_MODULE_PATH = _ROOT / "adapters" / "onebot" / "memory_hints.py"
_SPEC = importlib.util.spec_from_file_location("_onebot_memory_hints", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

aliases_in_text = _MODULE.aliases_in_text
at_segment_user_ids = _MODULE.at_segment_user_ids
collect_subjects = _MODULE.collect_subjects
display_name_mentions = _MODULE.display_name_mentions

from engine import memory_subjects  # noqa: E402
from engine.builtin.knowledge_inject import (  # noqa: E402
    _DEFAULT_SCAN_DEPTH,
    _apply_memory_budget,
    _load_subject_memory_entries,
    normalize_memory_entries,
)


def _enroll(tmp_path: Path, *aliases: str) -> None:
    for alias in aliases:
        memory_subjects.record_interaction(tmp_path, alias)


def _write_subject_memory(tmp_path: Path, alias: str, entries: list[dict[str, Any]]) -> Path:
    book_dir = tmp_path / "data" / "memory" / f"user_{alias}"
    book_dir.mkdir(parents=True, exist_ok=True)
    path = book_dir / "default.yaml"
    path.write_text(
        yaml.safe_dump({"book": "default", "entries": entries}, allow_unicode=True), encoding="utf-8",
    )
    return path


def _entry(eid: str, content: str = "内容", **extra: Any) -> dict[str, Any]:
    return {"id": eid, "content": content, **extra}


class TestRoster:
    def test_nobody_is_enrolled_before_a_direct_interaction(self, tmp_path: Path) -> None:
        assert memory_subjects.enrolled_subjects(tmp_path) == set()
        assert not memory_subjects.is_enrolled(tmp_path, "UserA")

    def test_a_direct_interaction_enrolls_the_sender(self, tmp_path: Path) -> None:
        memory_subjects.record_interaction(tmp_path, "UserA")

        assert memory_subjects.is_enrolled(tmp_path, "UserA")

    def test_repeat_interactions_accumulate_a_count(self, tmp_path: Path) -> None:
        memory_subjects.record_interaction(tmp_path, "UserA")
        memory_subjects.record_interaction(tmp_path, "UserA")

        roster = memory_subjects.load_roster(tmp_path)
        assert roster["UserA"]["interactions"] == 2
        assert roster["UserA"]["first_seen"] <= roster["UserA"]["last_seen"]

    def test_an_invalid_alias_is_refused(self, tmp_path: Path) -> None:
        # 别名会被拼进目录名，路径穿越必须在入口就挡掉。
        for bad in ["", "  ", "../etc", "user/../x", "1User", "a" * 200]:
            assert not memory_subjects.record_interaction(tmp_path, bad)
        assert memory_subjects.enrolled_subjects(tmp_path) == set()

    def test_a_corrupt_roster_reads_as_nobody_enrolled(self, tmp_path: Path) -> None:
        # 宁可不加载记忆，也不要凭一份坏数据给人乱开档案。
        path = memory_subjects.roster_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")

        assert memory_subjects.enrolled_subjects(tmp_path) == set()

    def test_a_roster_with_the_wrong_shape_is_ignored(self, tmp_path: Path) -> None:
        path = memory_subjects.roster_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"subjects": ["UserA"]}), encoding="utf-8")

        assert memory_subjects.enrolled_subjects(tmp_path) == set()

    def test_the_namespace_is_derived_from_the_alias(self) -> None:
        assert memory_subjects.subject_namespace("UserA") == "user_UserA"
        assert memory_subjects.subject_namespace("../etc") == ""

    def test_only_enrolled_subjects_survive_resolution(self, tmp_path: Path) -> None:
        _enroll(tmp_path, "UserA", "UserC")

        resolved = memory_subjects.resolve_load_subjects(tmp_path, ["UserA", "UserB", "UserC", "UserA"])

        assert resolved == ["UserA", "UserC"]

    def test_resolution_tolerates_a_non_list(self, tmp_path: Path) -> None:
        assert memory_subjects.resolve_load_subjects(tmp_path, "UserA") == []
        assert memory_subjects.resolve_load_subjects(tmp_path, None) == []


class TestAliasDetection:
    def test_finds_aliases_next_to_cjk_without_word_boundaries(self) -> None:
        # \b 在中文旁边不成立（CJK 也是 \w），用它会把这种最常见的情况漏掉。
        assert aliases_in_text("他说UserAF很靠谱") == ["UserAF"]

    def test_does_not_slice_a_short_alias_out_of_a_longer_one(self) -> None:
        assert aliases_in_text("UserAF 来了") == ["UserAF"]
        assert "UserA" not in aliases_in_text("UserAF 来了")

    def test_keeps_first_seen_order_and_dedupes(self) -> None:
        assert aliases_in_text("UserB 和 UserA，还有 UserB") == ["UserB", "UserA"]

    def test_ignores_lowercase_and_digit_suffixed_lookalikes(self) -> None:
        assert aliases_in_text("usera user1 Userab") == []

    def test_empty_text_is_empty(self) -> None:
        assert aliases_in_text("") == []


class TestAtSegments:
    def test_reads_the_qq_of_every_at_segment(self) -> None:
        message = [
            {"type": "text", "data": {"text": "看看"}},
            {"type": "at", "data": {"qq": "10001"}},
            {"type": "at", "data": {"qq": "10002"}},
        ]

        assert at_segment_user_ids(message) == ["10001", "10002"]

    def test_skips_at_all(self) -> None:
        # @全体成员 不对应任何一个人。
        assert at_segment_user_ids([{"type": "at", "data": {"qq": "all"}}]) == []

    def test_dedupes_and_tolerates_none(self) -> None:
        message = [{"type": "at", "data": {"qq": "10001"}}, {"type": "at", "data": {"qq": "10001"}}]

        assert at_segment_user_ids(message) == ["10001"]
        assert at_segment_user_ids(None) == []


class TestDisplayNameMentions:
    def test_matches_an_enrolled_display_name(self) -> None:
        names = {"UserA": "张三"}

        assert display_name_mentions("张三昨天说要请客", display_names=names) == ["UserA"]

    def test_prefers_the_longer_name_when_one_contains_the_other(self) -> None:
        names = {"UserA": "张三", "UserB": "张三丰"}

        assert display_name_mentions("张三丰在吗", display_names=names)[0] == "UserB"

    def test_ignores_names_too_short_to_be_safe(self) -> None:
        # 一个人叫「明」，「明天下雨」就会中。
        assert display_name_mentions("明天下雨", display_names={"UserA": "明"}) == []

    def test_respects_the_exclude_list(self) -> None:
        names = {"UserA": "张三"}

        assert display_name_mentions("张三在吗", display_names=names, exclude=["UserA"]) == []


class TestCollectSubjects:
    def test_the_sender_comes_first(self) -> None:
        # 预算裁剪时发起人的档案最该留下。
        subjects = collect_subjects(sender_alias="UserB", anonymized_text="UserA 你好")

        assert subjects[0] == "UserB"
        assert set(subjects) == {"UserA", "UserB"}

    def test_at_segments_are_resolved_through_the_alias_map(self) -> None:
        subjects = collect_subjects(
            sender_alias="UserA",
            anonymized_text="看一下",
            at_user_ids=["10002"],
            alias_of_user_id=lambda uid: {"10002": "UserC"}.get(uid, ""),
        )

        assert subjects == ["UserA", "UserC"]

    def test_unknown_senders_are_dropped(self) -> None:
        assert collect_subjects(sender_alias="UserUnknown", anonymized_text="") == []

    def test_anonymous_placeholder_sender_is_dropped(self) -> None:
        assert collect_subjects(sender_alias="AnonUnknown", anonymized_text="") == []

    def test_display_names_fill_in_after_the_aliases(self) -> None:
        subjects = collect_subjects(
            sender_alias="UserA",
            anonymized_text="张三 和 UserB 都在",
            display_names={"UserC": "张三"},
        )

        assert subjects == ["UserA", "UserB", "UserC"]

    def test_a_sender_mentioned_again_is_not_duplicated(self) -> None:
        subjects = collect_subjects(sender_alias="UserA", anonymized_text="UserA 自言自语")

        assert subjects == ["UserA"]


class TestSubjectMemoryLoading:
    def test_loads_the_whole_archive_of_a_mentioned_person(self, tmp_path: Path) -> None:
        _enroll(tmp_path, "UserA")
        _write_subject_memory(tmp_path, "UserA", [_entry("m1"), _entry("m2")])

        entries = _load_subject_memory_entries(tmp_path, {"memory_hints": {"subjects": ["UserA"]}})

        assert {e["id"] for e in entries} == {"m1", "m2"}
        assert {e["subject"] for e in entries} == {"UserA"}

    def test_entries_without_keywords_still_load(self, tmp_path: Path) -> None:
        # 按人加载的召回理由是「这轮提到了这个人」，不该再要求关键词命中。
        _enroll(tmp_path, "UserA")
        _write_subject_memory(tmp_path, "UserA", [_entry("m1")])

        entries = _load_subject_memory_entries(tmp_path, {"memory_hints": {"subjects": ["UserA"]}})

        assert [e["strategy"] for e in entries] == ["subject"]

    def test_a_never_enrolled_person_has_no_archive_to_load(self, tmp_path: Path) -> None:
        # 被别人提到但从未主动找过 bot 的人不建档，所以也没东西可加载。
        _write_subject_memory(tmp_path, "UserZ", [_entry("m1")])

        assert _load_subject_memory_entries(tmp_path, {"memory_hints": {"subjects": ["UserZ"]}}) == []

    def test_no_hints_loads_nothing(self, tmp_path: Path) -> None:
        _enroll(tmp_path, "UserA")
        _write_subject_memory(tmp_path, "UserA", [_entry("m1")])

        assert _load_subject_memory_entries(tmp_path, {}) == []
        assert _load_subject_memory_entries(tmp_path, None) == []

    def test_several_people_are_loaded_in_the_order_given(self, tmp_path: Path) -> None:
        _enroll(tmp_path, "UserA", "UserB")
        _write_subject_memory(tmp_path, "UserA", [_entry("a1")])
        _write_subject_memory(tmp_path, "UserB", [_entry("b1")])

        entries = _load_subject_memory_entries(tmp_path, {"memory_hints": {"subjects": ["UserB", "UserA"]}})

        assert [e["subject"] for e in entries] == ["UserB", "UserA"]

    def test_constant_entries_keep_their_strategy(self, tmp_path: Path) -> None:
        _enroll(tmp_path, "UserA")
        _write_subject_memory(tmp_path, "UserA", [_entry("m1", constant=True)])

        entries = _load_subject_memory_entries(tmp_path, {"memory_hints": {"subjects": ["UserA"]}})

        assert [e["strategy"] for e in entries] == ["constant"]


class TestScanDepthDefault:
    def test_a_yaml_entry_without_scan_depth_gets_the_default(self) -> None:
        # 默认 0 等于「只看当前这一句」，以「张三」为关键词的记忆在「他电话多少」上不激活。
        entries = normalize_memory_entries([{"id": "m1", "content": "c", "keywords": ["张三"]}])

        assert entries[0]["scan_depth"] == _DEFAULT_SCAN_DEPTH
        assert _DEFAULT_SCAN_DEPTH > 0

    def test_an_explicit_zero_is_respected(self) -> None:
        entries = normalize_memory_entries(
            [{"id": "m1", "content": "c", "keywords": ["x"], "scan_depth": 0}]
        )

        assert entries[0]["scan_depth"] == 0


class TestMemoryBudget:
    def _buckets(self, active: list[dict[str, Any]], constant: list[dict[str, Any]] | None = None):
        return {"memory_constant": list(constant or []), "memory_active": list(active)}

    def test_the_highest_priority_entry_is_not_the_one_dropped(self) -> None:
        # 旧实现是 first-fit：一条高优先级的长记忆被跳过，换三条无关紧要的短记忆进来。
        buckets = self._buckets([
            {"id": "big", "content": "x" * 300, "priority": 9},
            {"id": "mid", "content": "x" * 150, "priority": 5},
            {"id": "small", "content": "x" * 40, "priority": 1},
        ])

        _apply_memory_budget(buckets, 200)

        assert [e["id"] for e in buckets["memory_active"]] == []

    def test_it_keeps_filling_in_priority_order_while_the_budget_allows(self) -> None:
        buckets = self._buckets([
            {"id": "a", "content": "x" * 100, "priority": 9},
            {"id": "b", "content": "x" * 80, "priority": 5},
            {"id": "c", "content": "x" * 500, "priority": 1},
        ])

        _apply_memory_budget(buckets, 200)

        assert [e["id"] for e in buckets["memory_active"]] == ["a", "b"]

    def test_constant_entries_are_never_evicted(self) -> None:
        # 工具描述写的是「总是注入」，让它们参与淘汰等于说话不算数。
        buckets = self._buckets(
            [{"id": "act", "content": "x" * 100, "priority": 9}],
            [{"id": "const", "content": "x" * 500, "priority": 0}],
        )

        _apply_memory_budget(buckets, 200)

        assert [e["id"] for e in buckets["memory_constant"]] == ["const"]

    def test_freshness_breaks_priority_ties(self) -> None:
        buckets = self._buckets([
            {"id": "old", "content": "x" * 150, "priority": 5, "updated_at": "2026-01-01T00:00:00+00:00"},
            {"id": "new", "content": "x" * 150, "priority": 5, "updated_at": "2026-08-01T00:00:00+00:00"},
        ])

        _apply_memory_budget(buckets, 200)

        assert [e["id"] for e in buckets["memory_active"]] == ["new"]

    def test_a_zero_budget_still_trims_nothing(self) -> None:
        buckets = self._buckets([{"id": "a", "content": "x" * 9999, "priority": 0}])

        _apply_memory_budget(buckets, 0)

        assert [e["id"] for e in buckets["memory_active"]] == ["a"]

    def test_dropped_entries_are_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        # 记忆没有 INDEX 兜底，被丢掉就是彻底隐形，至少要能对上账。
        buckets = self._buckets([{"id": "a", "content": "x" * 500, "priority": 0}])

        with caplog.at_level("INFO"):
            _apply_memory_budget(buckets, 100)

        assert "memory budget dropped" in caplog.text
