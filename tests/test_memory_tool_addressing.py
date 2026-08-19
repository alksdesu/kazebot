"""三个记忆工具的寻址与字段继承。

save / list / delete 此前各写一份 namespace 推导，只有 save 认 subject —— 于是
data/memory/user_<别名>/ 下的档案写得进去、列不出来、删不掉。整条替换还会把调用方
没重复给的 keywords 洗成空，条目下一轮起永久不可召回。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

from engine import memory_subjects
from engine.builtin.knowledge_inject import (
    _conversation_memory_namespace,
    build_knowledge_context,
    delete_memory,
    list_memories,
    save_memory,
)
from engine.node import Node
from toolbox.context import ToolContext

_CONV_KEY = "qq_group:bc3f12b0621298e191a49fa7"
_SUBJECT = "UserA"


def _ctx(
    tmp_path: Path,
    node_extra: dict[str, Any] | None = None,
    subjects: list[str] | None = None,
) -> ToolContext:
    ctx = ToolContext(
        supervisor_url="http://localhost:0",
        session_id="sess-1",
        run_id="run-1",
        worker_id="worker-1",
        workspace_root=tmp_path,
        http=None,  # type: ignore[arg-type]
        registry=None,
        conversation_key=_CONV_KEY,
        node_id="system.memory_extractor",
    )
    if node_extra is not None:
        object.__setattr__(ctx, "_node_extra", node_extra)
    if subjects is not None:
        # engine 在构造 ToolContext 时把本轮注入过档案的人挂上来。
        object.__setattr__(ctx, "_memory_subjects", subjects)
    return ctx


def _save(tmp_path: Path, **args: Any) -> dict[str, Any]:
    node_extra = args.pop("_node_extra", None)
    subjects = args.pop("_subjects", None)
    payload = {"id": "m1", "book": "people", "content": "内容", **args}
    return asyncio.run(save_memory(payload, _ctx(tmp_path, node_extra, subjects)))


def _read(tmp_path: Path, namespace: str, book: str = "people") -> list[dict[str, Any]]:
    path = tmp_path / "data" / "memory" / namespace / f"{book}.yaml"
    if not path.exists():
        return []
    return yaml.safe_load(path.read_text(encoding="utf-8"))["entries"]


def _conv(tmp_path: Path, book: str = "people") -> list[dict[str, Any]]:
    return _read(tmp_path, _conversation_memory_namespace(_CONV_KEY), book)


def _archive(tmp_path: Path, book: str = "people") -> list[dict[str, Any]]:
    return _read(tmp_path, f"user_{_SUBJECT}", book)


def _enroll(tmp_path: Path, subject: str = _SUBJECT) -> None:
    memory_subjects.record_interaction(tmp_path, subject)


class TestRecallabilityGuard:
    def test_a_keywordless_non_constant_entry_is_refused(self, tmp_path: Path) -> None:
        result = _save(tmp_path)

        assert not result["ok"]
        assert "never be recalled" in result["error"]
        assert _conv(tmp_path) == []

    def test_keywords_make_it_saveable(self, tmp_path: Path) -> None:
        assert _save(tmp_path, keywords=["张三"])["ok"]

    def test_constant_makes_it_saveable(self, tmp_path: Path) -> None:
        assert _save(tmp_path, constant=True)["ok"]

    def test_an_enrolled_subject_makes_it_saveable(self, tmp_path: Path) -> None:
        _enroll(tmp_path)

        assert _save(tmp_path, subject=_SUBJECT)["ok"]

    def test_an_unenrolled_subject_does_not_count_as_recallable(self, tmp_path: Path) -> None:
        # subject 退回会话 namespace 后这条就只剩关键词一条活路，而它没有关键词。
        result = _save(tmp_path, subject="UserZZ")

        assert not result["ok"]

    def test_clearing_keywords_explicitly_is_refused(self, tmp_path: Path) -> None:
        _save(tmp_path, keywords=["张三"])

        assert not _save(tmp_path, keywords=[])["ok"]


class TestFieldInheritance:
    def test_updating_only_content_keeps_keywords(self, tmp_path: Path) -> None:
        """提取器的提示词主动要求「同 id 更新」，而它不会重复给全部字段。"""
        _save(tmp_path, keywords=["张三", "电话"], priority=7, scan_depth=5)

        assert _save(tmp_path, content="新内容")["ok"]

        entry = _conv(tmp_path)[0]
        assert entry["content"] == "新内容"
        assert entry["keywords"] == ["张三", "电话"]
        assert entry["priority"] == 7
        assert entry["scan_depth"] == 5

    def test_updating_keeps_constant_and_enabled(self, tmp_path: Path) -> None:
        # 断言 ok 是必须的：继承失效时守卫会把这次更新拒掉，磁盘上留着旧值，
        # 不断言就会把「根本没写进去」读成「字段保住了」。
        _save(tmp_path, constant=True, enabled=False)

        assert _save(tmp_path, content="新内容")["ok"]

        entry = _conv(tmp_path)[0]
        assert entry["constant"] is True
        assert entry["enabled"] is False
        assert entry["content"] == "新内容"

    def test_updating_keeps_node_ids_and_subject(self, tmp_path: Path) -> None:
        _enroll(tmp_path)
        _save(tmp_path, subject=_SUBJECT, node_ids=["qq.orchestrator"])

        assert _save(tmp_path, content="新内容", _subjects=[_SUBJECT])["ok"]

        entry = _archive(tmp_path)[0]
        assert entry["node_ids"] == ["qq.orchestrator"]
        assert entry["subject"] == _SUBJECT
        assert entry["content"] == "新内容"

    def test_an_explicit_value_still_overrides(self, tmp_path: Path) -> None:
        _save(tmp_path, keywords=["旧"], priority=1)

        assert _save(tmp_path, keywords=["新"], priority=9)["ok"]

        entry = _conv(tmp_path)[0]
        assert entry["keywords"] == ["新"]
        assert entry["priority"] == 9

    def test_created_at_survives_but_updated_at_moves(self, tmp_path: Path) -> None:
        _save(tmp_path, keywords=["k"])
        created = _conv(tmp_path)[0]["created_at"]

        assert _save(tmp_path, content="新内容")["ok"]

        entry = _conv(tmp_path)[0]
        assert entry["created_at"] == created
        assert entry["updated_at"] >= created
        assert entry["content"] == "新内容"


class TestNamespaceAddressing:
    def test_a_new_subject_entry_lands_in_the_archive(self, tmp_path: Path) -> None:
        _enroll(tmp_path)

        _save(tmp_path, subject=_SUBJECT, keywords=["k"])

        assert len(_archive(tmp_path)) == 1
        assert _conv(tmp_path) == []

    def test_an_unenrolled_subject_falls_back_to_the_conversation(self, tmp_path: Path) -> None:
        _save(tmp_path, subject="UserZZ", keywords=["k"])

        assert len(_conv(tmp_path)) == 1
        assert _read(tmp_path, "user_UserZZ") == []

    def test_an_existing_conversation_entry_is_updated_in_place(self, tmp_path: Path) -> None:
        """建档前写在会话里的条目，建档后不该在档案里长出第二份。"""
        _save(tmp_path, keywords=["k"])
        _enroll(tmp_path)

        _save(tmp_path, subject=_SUBJECT, content="新内容")

        assert len(_conv(tmp_path)) == 1
        assert _conv(tmp_path)[0]["content"] == "新内容"
        assert _archive(tmp_path) == []

    def test_a_node_memory_book_is_honoured(self, tmp_path: Path) -> None:
        _save(tmp_path, keywords=["k"], _node_extra={"memory_book": "shared"})

        assert len(_read(tmp_path, "shared")) == 1
        assert _conv(tmp_path) == []

    def test_an_enrolled_subject_outranks_the_node_memory_book(self, tmp_path: Path) -> None:
        _enroll(tmp_path)

        _save(tmp_path, subject=_SUBJECT, keywords=["k"], _node_extra={"memory_book": "shared"})

        assert len(_archive(tmp_path)) == 1
        assert _read(tmp_path, "shared") == []

    def test_this_turns_archives_are_addressable_without_repeating_the_subject(
        self, tmp_path: Path,
    ) -> None:
        """模型看得见的档案条目就必须更新得到，否则那次更新静默进会话再被去重吃掉。"""
        _enroll(tmp_path)
        _save(tmp_path, subject=_SUBJECT, keywords=["k"])

        assert _save(tmp_path, content="新内容", _subjects=[_SUBJECT])["ok"]

        assert len(_archive(tmp_path)) == 1
        assert _archive(tmp_path)[0]["content"] == "新内容"
        assert _conv(tmp_path) == []

    def test_an_unrelated_archive_is_not_addressable(self, tmp_path: Path) -> None:
        # 没被注入过的人的档案不该因为同 id 就被改写。
        _enroll(tmp_path)
        _save(tmp_path, subject=_SUBJECT, keywords=["k"])

        assert _save(tmp_path, content="新内容", keywords=["k"], _subjects=[])["ok"]

        assert _archive(tmp_path)[0]["content"] == "内容"
        assert _conv(tmp_path)[0]["content"] == "新内容"

    def test_the_book_is_part_of_the_identity(self, tmp_path: Path) -> None:
        # id 唯一性是 per-book 的，同 id 不同 book 是两条不同记忆。
        _save(tmp_path, keywords=["k"])
        _save(tmp_path, book="rules", keywords=["k"])

        assert len(_conv(tmp_path, "people")) == 1
        assert len(_conv(tmp_path, "rules")) == 1


class TestListAndDelete:
    def _list(self, tmp_path: Path, **args: Any) -> list[dict[str, Any]]:
        return asyncio.run(list_memories(args, _ctx(tmp_path)))["data"]["entries"]

    def _delete(self, tmp_path: Path, **args: Any) -> dict[str, Any]:
        payload = {"id": "m1", "book": "people", **args}
        return asyncio.run(delete_memory(payload, _ctx(tmp_path)))

    def test_an_archive_entry_can_be_listed(self, tmp_path: Path) -> None:
        _enroll(tmp_path)
        _save(tmp_path, subject=_SUBJECT, keywords=["k"])

        listed = self._list(tmp_path, subject=_SUBJECT)

        assert [e["id"] for e in listed] == ["m1"]
        assert listed[0]["subject"] == _SUBJECT

    def test_listing_without_subject_shows_the_conversation_only(self, tmp_path: Path) -> None:
        _enroll(tmp_path)
        _save(tmp_path, subject=_SUBJECT, keywords=["k"])
        _save(tmp_path, id="m2", keywords=["k"])

        assert [e["id"] for e in self._list(tmp_path)] == ["m2"]

    def test_an_archive_entry_can_be_deleted(self, tmp_path: Path) -> None:
        _enroll(tmp_path)
        _save(tmp_path, subject=_SUBJECT, keywords=["k"])

        assert self._delete(tmp_path, subject=_SUBJECT)["ok"]
        assert _archive(tmp_path) == []

    def test_deleting_without_the_subject_cannot_reach_the_archive(self, tmp_path: Path) -> None:
        _enroll(tmp_path)
        _save(tmp_path, subject=_SUBJECT, keywords=["k"])

        result = self._delete(tmp_path)

        assert not result["ok"]
        assert len(_archive(tmp_path)) == 1

    def test_a_conversation_entry_still_deletes_without_subject(self, tmp_path: Path) -> None:
        _save(tmp_path, keywords=["k"])

        assert self._delete(tmp_path)["ok"]
        assert _conv(tmp_path) == []

    def test_constant_entries_stay_protected(self, tmp_path: Path) -> None:
        _enroll(tmp_path)
        _save(tmp_path, subject=_SUBJECT, constant=True)

        result = self._delete(tmp_path, subject=_SUBJECT)

        assert not result["ok"]
        assert "constant" in result["error"]
        assert len(_archive(tmp_path)) == 1

    def test_a_missing_book_and_a_missing_id_report_differently(self, tmp_path: Path) -> None:
        assert "book not found" in self._delete(tmp_path)["error"]

        _save(tmp_path, id="other", keywords=["k"])
        assert "memory not found" in self._delete(tmp_path)["error"]


class TestReadSideDedupe:
    def _memory_text(self, tmp_path: Path, subjects: list[str]) -> str:
        node = Node(id="qq.orchestrator", type="ai", prompt="p")
        _static, _sd, memory_static, memory_dynamic = build_knowledge_context(
            tmp_path,
            node,
            "随便说点什么",
            [],
            {},
            {"conversation_key": _CONV_KEY, "memory_hints": {"subjects": subjects}},
        )
        return "\n".join(
            str(m.get("content") or "") for m in list(memory_static) + list(memory_dynamic)
        )

    def _write_raw(self, tmp_path: Path, namespace: str, content: str) -> None:
        path = tmp_path / "data" / "memory" / namespace / "people.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(
                {"book": "people", "entries": [{
                    "id": "m1", "content": content, "constant": True, "enabled": True,
                }]},
                allow_unicode=True,
            ),
            encoding="utf-8",
        )

    def test_a_duplicated_id_renders_once_and_the_archive_wins(self, tmp_path: Path) -> None:
        _enroll(tmp_path)
        self._write_raw(tmp_path, _conversation_memory_namespace(_CONV_KEY), "会话里的旧版本")
        self._write_raw(tmp_path, f"user_{_SUBJECT}", "档案里的新版本")

        text = self._memory_text(tmp_path, [_SUBJECT])

        assert text.count("## m1") == 1
        assert "档案里的新版本" in text
        assert "会话里的旧版本" not in text

    def test_distinct_ids_from_both_places_all_show_up(self, tmp_path: Path) -> None:
        _enroll(tmp_path)
        self._write_raw(tmp_path, _conversation_memory_namespace(_CONV_KEY), "会话内容")
        archive = tmp_path / "data" / "memory" / f"user_{_SUBJECT}" / "people.yaml"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text(
            yaml.safe_dump(
                {"book": "people", "entries": [{
                    "id": "m2", "content": "档案内容", "constant": True, "enabled": True,
                }]},
                allow_unicode=True,
            ),
            encoding="utf-8",
        )

        text = self._memory_text(tmp_path, [_SUBJECT])

        assert "会话内容" in text
        assert "档案内容" in text

    def test_the_same_id_in_a_different_book_is_not_deduped(self, tmp_path: Path) -> None:
        _enroll(tmp_path)
        self._write_raw(tmp_path, _conversation_memory_namespace(_CONV_KEY), "会话内容")
        archive = tmp_path / "data" / "memory" / f"user_{_SUBJECT}" / "rules.yaml"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text(
            yaml.safe_dump(
                {"book": "rules", "entries": [{
                    "id": "m1", "content": "另一本里的同 id", "constant": True, "enabled": True,
                }]},
                allow_unicode=True,
            ),
            encoding="utf-8",
        )

        text = self._memory_text(tmp_path, [_SUBJECT])

        assert "会话内容" in text
        assert "另一本里的同 id" in text
