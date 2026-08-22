"""表情包库的存储层。

两条要紧的：内容哈希去重必须连"已弃"也算见过（否则黑名单形同虚设），
以及模型靠名字点图，名字重了就会点歪。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


from stickers import store as ss


@pytest.fixture()
def store(tmp_path: Path):
    handle = ss.StickerStore(tmp_path / "stickers.sqlite3")
    yield handle
    handle.close()


def _add(store, digest: str, **kwargs):
    params = {
        "sha256": digest,
        "rel_path": f"data/stickers/pending/{digest}.png",
        "source": ss.SOURCE_GROUP,
    }
    params.update(kwargs)
    return store.add(**params)


def _promote(store, digest: str, tags=("开心", "猫")):
    _add(store, digest, name=tags[0])
    store.accept(digest)
    store.set_auto_tags(digest, list(tags), version=ss.CAPTION_PROMPT_VERSION)


# ── 去重 ──

def test_new_digest_is_accepted(store) -> None:
    assert _add(store, "a" * 64) is not None
    assert store.known("a" * 64)


def test_same_digest_twice_is_refused(store) -> None:
    _add(store, "a" * 64)
    assert _add(store, "a" * 64) is None


def test_discarded_digest_still_counts_as_known(store) -> None:
    # 黑名单的全部意义就在这里：丢过的图不能因为再被发一次就重新入库。
    _add(store, "a" * 64)
    store.discard("a" * 64)
    assert store.known("a" * 64)
    assert _add(store, "a" * 64) is None
    assert store.state_of("a" * 64) == ss.STATE_DISCARDED


def test_forget_lets_the_image_come_back(store) -> None:
    _add(store, "a" * 64)
    store.discard("a" * 64)
    assert store.forget("a" * 64)
    assert not store.known("a" * 64)
    assert _add(store, "a" * 64) is not None


def test_unknown_digest_has_empty_state(store) -> None:
    assert store.state_of("f" * 64) == ""
    assert not store.known("f" * 64)


def test_discard_drops_the_path_but_keeps_the_hash(store) -> None:
    _add(store, "a" * 64)
    store.discard("a" * 64)
    row = store.get("a" * 64)
    assert row is not None and row.rel_path == "" and row.tags == []


# ── 名字唯一性 ──

def test_duplicate_names_get_a_suffix(store) -> None:
    _add(store, "a" * 64, name="开心")
    _add(store, "b" * 64, name="开心")
    names = {store.get("a" * 64).name, store.get("b" * 64).name}
    assert names == {"开心", "开心2"}


def test_marker_syntax_characters_are_stripped_from_names(store) -> None:
    # 名字要能被模型照抄进 [表情:名称]，带方括号或冒号就把标记本身破坏了。
    row = _add(store, "a" * 64, name="[开心: 猫，狗]")
    assert row is not None
    assert not set("[]:：,，") & set(row.name)


def test_blank_name_falls_back_to_the_digest(store) -> None:
    row = _add(store, "a" * 64, name="   ")
    assert row is not None and row.name


def test_by_name_only_finds_library_items(store) -> None:
    _add(store, "a" * 64, name="开心")
    assert store.by_name("开心") is None
    store.accept("a" * 64)
    assert store.by_name("开心") is not None


def test_rename_keeps_uniqueness(store) -> None:
    _promote(store, "a" * 64, ("开心",))
    _promote(store, "b" * 64, ("难过",))
    assert store.rename("b" * 64, "开心") == "开心2"


# ── 状态流转 ──

def test_accept_moves_pending_to_library(store) -> None:
    _add(store, "a" * 64)
    assert store.accept("a" * 64)
    assert store.state_of("a" * 64) == ss.STATE_LIBRARY


def test_accept_can_relocate_the_file(store) -> None:
    _add(store, "a" * 64)
    store.accept("a" * 64, rel_path="data/stickers/library/x.png")
    assert store.get("a" * 64).rel_path == "data/stickers/library/x.png"


def test_accepting_twice_is_refused(store) -> None:
    _add(store, "a" * 64)
    store.accept("a" * 64)
    assert not store.accept("a" * 64)


def test_pending_items_are_not_usable(store) -> None:
    _add(store, "a" * 64)
    assert store.all_usable() == []


def test_library_without_tags_is_not_usable(store) -> None:
    # 没打标的图对模型不可描述，注入了也选不出来。
    _add(store, "a" * 64)
    store.accept("a" * 64)
    assert store.all_usable() == []


def test_tagged_library_item_is_usable(store) -> None:
    _promote(store, "a" * 64)
    usable = store.all_usable()
    assert len(usable) == 1 and usable[0].usable


# ── 标签合并 ──

def test_manual_tags_extend_auto_tags(store) -> None:
    _promote(store, "a" * 64, ("开心", "猫"))
    store.set_manual_tags("a" * 64, ["招财"])
    assert set(store.get("a" * 64).tags) == {"开心", "猫", "招财"}


def test_override_discards_auto_tags(store) -> None:
    _promote(store, "a" * 64, ("开心", "猫"))
    store.set_manual_tags("a" * 64, ["招财"], override=True)
    assert store.get("a" * 64).tags == ["招财"]


def test_recaptioning_respects_a_standing_override(store) -> None:
    # 人工覆盖过的图，后台重打标不能把人工结果冲掉。
    _promote(store, "a" * 64, ("开心",))
    store.set_manual_tags("a" * 64, ["招财"], override=True)
    store.set_auto_tags("a" * 64, ["难过", "狗"], version=ss.CAPTION_PROMPT_VERSION)
    assert store.get("a" * 64).tags == ["招财"]
    assert store.get("a" * 64).auto_tags == ["难过", "狗"]


def test_merge_tags_dedupes_and_keeps_order() -> None:
    assert ss.merge_tags(["猫", "开心"], ["开心", "招财"], override=False) == [
        "开心", "招财", "猫",
    ]


def test_tag_writes_on_a_missing_row_are_silent(store) -> None:
    store.set_auto_tags("f" * 64, ["x"], version=1)
    store.set_manual_tags("f" * 64, ["y"])


# ── 打标队列 ──

def test_new_items_are_due_for_caption(store) -> None:
    _add(store, "a" * 64)
    assert [row.sha256 for row in store.due_for_caption()] == ["a" * 64]


def test_captioned_items_leave_the_queue(store) -> None:
    _add(store, "a" * 64)
    store.set_auto_tags("a" * 64, ["开心"], version=ss.CAPTION_PROMPT_VERSION)
    assert store.due_for_caption() == []


def test_a_newer_prompt_version_requeues_everything(store) -> None:
    # prompt 语义变了，旧标签和新标签不可比，必须重跑。
    _add(store, "a" * 64)
    store.set_auto_tags("a" * 64, ["开心"], version=1)
    assert store.due_for_caption(version=1) == []
    assert len(store.due_for_caption(version=2)) == 1


def test_discarded_items_are_never_captioned(store) -> None:
    # 打标要花钱，丢掉的图不值这个钱。
    _add(store, "a" * 64)
    store.discard("a" * 64)
    assert store.due_for_caption() == []


def test_failed_captions_can_be_retried_alone(store) -> None:
    _add(store, "a" * 64)
    _add(store, "b" * 64)
    store.set_auto_tags("a" * 64, ["开心"], version=ss.CAPTION_PROMPT_VERSION)
    store.mark_caption("b" * 64, ss.CAPTION_FAILED, error="模型超时")
    assert store.reset_captions(only_failed=True) == 1
    assert [row.sha256 for row in store.due_for_caption()] == ["b" * 64]


def test_caption_error_is_recorded_and_truncated(store) -> None:
    _add(store, "a" * 64)
    store.mark_caption("a" * 64, ss.CAPTION_FAILED, error="x" * 900)
    row = store.get("a" * 64)
    assert row.caption_state == ss.CAPTION_FAILED and len(row.caption_error) == 500


def test_unknown_caption_state_is_rejected(store) -> None:
    _add(store, "a" * 64)
    with pytest.raises(ValueError):
        store.mark_caption("a" * 64, "bogus")


# ── 已发历史 ──

def test_recording_a_send_bumps_the_counter(store) -> None:
    _promote(store, "a" * 64)
    store.record_sent("conv1", "a" * 64)
    row = store.get("a" * 64)
    assert row.sent_count == 1 and row.last_sent_at > 0


def test_recording_the_same_image_twice_still_counts(store) -> None:
    _promote(store, "a" * 64)
    store.record_sent("conv1", "a" * 64)
    store.record_sent("conv1", "a" * 64)
    assert store.get("a" * 64).sent_count == 2


def test_recent_sends_are_scoped_per_conversation(store) -> None:
    # 别的群刚发过，不该妨碍这个群发。
    _promote(store, "a" * 64)
    store.record_sent("conv1", "a" * 64)
    assert store.recently_sent("conv1", within_sec=3600) == {"a" * 64}
    assert store.recently_sent("conv2", within_sec=3600) == set()


def test_old_sends_fall_out_of_the_window(store) -> None:
    _promote(store, "a" * 64)
    store.record_sent("conv1", "a" * 64)
    assert store.recently_sent("conv1", within_sec=0) == set()


def test_pruning_the_send_log(store) -> None:
    _promote(store, "a" * 64)
    store.record_sent("conv1", "a" * 64)
    assert store.prune_sent_log(older_than_sec=0) == 1
    assert store.recently_sent("conv1", within_sec=3600) == set()


def test_forgetting_an_image_clears_its_send_log(store) -> None:
    _promote(store, "a" * 64)
    store.record_sent("conv1", "a" * 64)
    store.forget("a" * 64)
    assert store.recently_sent("conv1", within_sec=3600) == set()


# ── 配额与浏览 ──

def test_overflow_returns_the_oldest_beyond_the_cap(store) -> None:
    for index in range(5):
        _add(store, str(index) * 64)
    extra = store.overflow(ss.STATE_PENDING, keep=3)
    assert len(extra) == 2
    assert {row.sha256 for row in extra} == {"0" * 64, "1" * 64}


def test_overflow_is_empty_below_the_cap(store) -> None:
    _add(store, "a" * 64)
    assert store.overflow(ss.STATE_PENDING, keep=3) == []


def test_expired_pending_ignores_library_items(store) -> None:
    _add(store, "a" * 64)
    _promote(store, "b" * 64)
    expired = store.expired_pending(ttl_sec=0)
    assert [row.sha256 for row in expired] == ["a" * 64]


def test_counts_report_each_bucket(store) -> None:
    _add(store, "a" * 64)
    _promote(store, "b" * 64)
    _add(store, "c" * 64)
    store.discard("c" * 64)
    counts = store.counts()
    assert counts[ss.STATE_PENDING] == 1
    assert counts[ss.STATE_LIBRARY] == 1
    assert counts[ss.STATE_DISCARDED] == 1
    assert counts["usable"] == 1
    assert counts["awaiting_caption"] == 1


def test_browse_filters_by_state(store) -> None:
    _add(store, "a" * 64)
    _promote(store, "b" * 64)
    rows = store.browse(state=ss.STATE_LIBRARY)
    assert [row.sha256 for row in rows] == ["b" * 64]


def test_browse_searches_names_and_tags(store) -> None:
    _promote(store, "a" * 64, ("开心", "猫"))
    _promote(store, "b" * 64, ("难过", "狗"))
    assert len(store.browse(search="猫")) == 1
    assert len(store.browse(search="难过")) == 1


def test_browse_paginates(store) -> None:
    for index in range(5):
        _add(store, str(index) * 64)
    assert len(store.browse(limit=2)) == 2
    assert len(store.browse(limit=2, offset=4)) == 1


# ── 并发 ──

def test_two_handles_share_one_library(tmp_path: Path) -> None:
    # 多开时几个实例各开一个连接读写同一个文件，这是共享图库的基本前提。
    path = tmp_path / "stickers.sqlite3"
    first = ss.StickerStore(path)
    second = ss.StickerStore(path)
    try:
        _add(first, "a" * 64)
        assert second.known("a" * 64)
        second.accept("a" * 64)
        assert first.state_of("a" * 64) == ss.STATE_LIBRARY
    finally:
        first.close()
        second.close()


def test_reopening_keeps_everything(tmp_path: Path) -> None:
    path = tmp_path / "stickers.sqlite3"
    first = ss.StickerStore(path)
    _promote(first, "a" * 64, ("开心",))
    first.close()
    second = ss.StickerStore(path)
    try:
        row = second.by_name("开心")
        assert row is not None and row.usable
    finally:
        second.close()
