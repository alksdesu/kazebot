"""控制台侧记忆增删改查。

写路径与 save_memory 共用一把锁和同一套默认值：页面上改的和后台提取器写的
落在同一份 yaml 里，任何一边偷懒都会覆盖掉另一边。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine import memory_admin  # noqa: E402
from engine.builtin.knowledge_inject import _DEFAULT_SCAN_DEPTH  # noqa: E402


def _book_path(root: Path, namespace: str, book: str = "default") -> Path:
    return root / "data" / "memory" / namespace / f"{book}.yaml"


class TestUpsert:
    def test_a_new_entry_lands_with_the_same_defaults_save_memory_uses(self, tmp_path: Path) -> None:
        entry = memory_admin.upsert_entry(
            tmp_path, "user_UserA", {"id": "likes_tea", "content": "喜欢喝奶茶", "keywords": "奶茶, 喝的"},
        )

        assert entry["scan_depth"] == _DEFAULT_SCAN_DEPTH
        assert entry["enabled"] is True
        assert entry["priority"] == 0
        assert entry["keywords"] == ["奶茶", "喝的"]

    def test_a_subject_namespace_stamps_the_subject(self, tmp_path: Path) -> None:
        # 少了 subject，这条就不会随「提到这个人」被加载，等于白存。
        entry = memory_admin.upsert_entry(
            tmp_path, "user_UserA", {"id": "a", "content": "x", "keywords": ["k"]},
        )

        assert entry["subject"] == "UserA"

    def test_manual_entries_are_marked_so_cleanup_skips_them(self, tmp_path: Path) -> None:
        # 14 天淘汰和 dream 整理都只动 source=auto，人写的得留住。
        entry = memory_admin.upsert_entry(
            tmp_path, "user_UserA", {"id": "a", "content": "x", "keywords": ["k"]},
        )

        assert entry["source"] == "manual"

    def test_editing_an_auto_entry_hands_it_over_to_the_human(self, tmp_path: Path) -> None:
        memory_admin.upsert_entry(tmp_path, "user_UserA", {"id": "a", "content": "x", "keywords": ["k"]})
        path = _book_path(tmp_path, "user_UserA")
        path.write_text(
            path.read_text(encoding="utf-8").replace("source: manual", "source: auto"), encoding="utf-8",
        )

        entry = memory_admin.upsert_entry(
            tmp_path, "user_UserA", {"id": "a", "content": "改过了", "keywords": ["k"]},
        )

        assert entry["source"] == "manual"

    def test_an_update_keeps_the_original_created_at(self, tmp_path: Path) -> None:
        first = memory_admin.upsert_entry(
            tmp_path, "user_UserA", {"id": "a", "content": "x", "keywords": ["k"]},
        )

        second = memory_admin.upsert_entry(
            tmp_path, "user_UserA", {"id": "a", "content": "y", "keywords": ["k"]},
        )

        assert second["created_at"] == first["created_at"]

    def test_an_entry_nothing_could_recall_is_refused(self, tmp_path: Path) -> None:
        # 注入侧对这种条目直接 continue，存下去就是一条永远读不到的死记忆。
        with pytest.raises(memory_admin.MemoryAdminError):
            memory_admin.upsert_entry(tmp_path, "conv_" + "0" * 24, {"id": "ghost", "content": "x"})

    def test_constant_alone_is_enough_to_be_recalled(self, tmp_path: Path) -> None:
        entry = memory_admin.upsert_entry(
            tmp_path, "conv_" + "0" * 24, {"id": "always", "content": "x", "constant": True},
        )

        assert entry["constant"] is True

    def test_empty_content_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(memory_admin.MemoryAdminError):
            memory_admin.upsert_entry(tmp_path, "user_UserA", {"id": "a", "content": "   ", "keywords": ["k"]})

    def test_duplicate_keywords_collapse(self, tmp_path: Path) -> None:
        entry = memory_admin.upsert_entry(
            tmp_path, "user_UserA", {"id": "a", "content": "x", "keywords": ["k", " k ", "j"]},
        )

        assert entry["keywords"] == ["k", "j"]


class TestNamespaceValidation:
    @pytest.mark.parametrize("bad", ["../etc", "user_../x", "conv_zzz", "user_1bad", "conv_短", "x" * 90])
    def test_a_namespace_that_could_escape_the_memory_dir_is_refused(self, tmp_path: Path, bad: str) -> None:
        # namespace 会被拼进目录名，穿越必须在入口就挡掉。
        with pytest.raises(memory_admin.MemoryAdminError):
            memory_admin.list_entries(tmp_path, bad)

    @pytest.mark.parametrize("bad", ["../book", "a/b", "x" * 90])
    def test_a_bad_book_name_is_refused(self, tmp_path: Path, bad: str) -> None:
        with pytest.raises(memory_admin.MemoryAdminError):
            memory_admin.upsert_entry(
                tmp_path, "user_UserA", {"id": "a", "content": "x", "keywords": ["k"], "book": bad},
            )

    def test_a_bad_entry_id_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(memory_admin.MemoryAdminError):
            memory_admin.upsert_entry(tmp_path, "user_UserA", {"id": "../x", "content": "x", "keywords": ["k"]})


class TestListing:
    def test_nothing_is_listed_before_anything_is_written(self, tmp_path: Path) -> None:
        assert memory_admin.list_namespaces(tmp_path) == []
        assert memory_admin.list_entries(tmp_path, "user_UserA") == []

    def test_a_namespace_reports_its_kind_and_count(self, tmp_path: Path) -> None:
        memory_admin.upsert_entry(tmp_path, "user_UserA", {"id": "a", "content": "x", "keywords": ["k"]})
        memory_admin.upsert_entry(
            tmp_path, "conv_" + "1" * 24, {"id": "b", "content": "y", "keywords": ["k"]},
        )

        rows = {row["namespace"]: row for row in memory_admin.list_namespaces(tmp_path)}

        assert rows["user_UserA"]["kind"] == "subject"
        assert rows["user_UserA"]["subject"] == "UserA"
        assert rows["conv_" + "1" * 24]["kind"] == "conversation"
        assert rows["user_UserA"]["entry_count"] == 1

    def test_entries_carry_the_book_they_live_in(self, tmp_path: Path) -> None:
        memory_admin.upsert_entry(
            tmp_path, "user_UserA", {"id": "a", "content": "x", "keywords": ["k"], "book": "people"},
        )

        assert memory_admin.list_entries(tmp_path, "user_UserA")[0]["book"] == "people"

    def test_a_legacy_entry_without_scan_depth_reads_as_the_default(self, tmp_path: Path) -> None:
        # 手写的 yaml 常常没有这个键，缺它会退回「只看当前这一句」。
        path = _book_path(tmp_path, "user_UserA")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("book: default\nentries:\n- id: a\n  content: x\n  keywords: [k]\n", encoding="utf-8")

        assert memory_admin.list_entries(tmp_path, "user_UserA")[0]["scan_depth"] == _DEFAULT_SCAN_DEPTH


class TestDeletion:
    def test_deleting_the_last_entry_removes_the_empty_book(self, tmp_path: Path) -> None:
        memory_admin.upsert_entry(tmp_path, "user_UserA", {"id": "a", "content": "x", "keywords": ["k"]})

        assert memory_admin.delete_entry(tmp_path, "user_UserA", "default", "a")
        assert not _book_path(tmp_path, "user_UserA").exists()

    def test_deleting_one_entry_leaves_the_others(self, tmp_path: Path) -> None:
        memory_admin.upsert_entry(tmp_path, "user_UserA", {"id": "a", "content": "x", "keywords": ["k"]})
        memory_admin.upsert_entry(tmp_path, "user_UserA", {"id": "b", "content": "y", "keywords": ["k"]})

        memory_admin.delete_entry(tmp_path, "user_UserA", "default", "a")

        assert [row["id"] for row in memory_admin.list_entries(tmp_path, "user_UserA")] == ["b"]

    def test_deleting_something_that_is_not_there_reports_false(self, tmp_path: Path) -> None:
        assert not memory_admin.delete_entry(tmp_path, "user_UserA", "default", "nope")

    def test_clearing_a_namespace_reports_how_much_it_dropped(self, tmp_path: Path) -> None:
        memory_admin.upsert_entry(tmp_path, "user_UserB", {"id": "a", "content": "x", "keywords": ["k"]})
        memory_admin.upsert_entry(tmp_path, "user_UserB", {"id": "b", "content": "y", "constant": True})

        assert memory_admin.clear_namespace(tmp_path, "user_UserB") == 2
        assert not (tmp_path / "data" / "memory" / "user_UserB").exists()

    def test_clearing_an_absent_namespace_is_not_an_error(self, tmp_path: Path) -> None:
        assert memory_admin.clear_namespace(tmp_path, "user_UserZ") == 0
