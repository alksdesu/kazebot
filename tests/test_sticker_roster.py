"""挑表情包的通用规则：清单给不给、给哪些，按名字和标签找图时谁先谁后。平台只提供「这张发不发得出去」。"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from stickers.roster import RosterRules, candidates, offered, prompt_roster, roster_entry  # noqa: E402
from stickers.store import CAPTION_DONE, STATE_LIBRARY, StickerStore  # noqa: E402

_CONV = "chat_room:5f3c2a108d4e4b7a9c210e6f"


@pytest.fixture()
def store(tmp_path: Path):
    handle = StickerStore(tmp_path / "stickers.sqlite3")
    yield handle
    handle.close()


def _add(store: StickerStore, name: str, tags: list[str], *, sent: int = 0) -> str:
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    store.add(sha256=digest, rel_path=f"data/stickers/library/{digest}.gif", size=1, source="test",
              state=STATE_LIBRARY, name=name)
    store.set_auto_tags(digest, tags, version=1)
    store.mark_caption(digest, CAPTION_DONE)
    for _ in range(sent):
        # 记在别的会话上：sent_count 是全局的，落在 _CONV 会顺带算成「刚发过」。
        store.record_sent("chat_room:seed", digest)
    return digest


def _always(_row: object) -> bool:
    return True


class TestOffered:
    @pytest.mark.parametrize("roll,expected", [(0.0, True), (0.399999, True), (0.4, False), (0.999999, False)])
    def test_the_roll_must_land_under_the_probability(self, roll: float, expected: bool) -> None:
        assert offered(RosterRules(probability=0.4), roll) is expected

    def test_zero_probability_never_offers(self) -> None:
        assert offered(RosterRules(probability=0.0), 0.0) is False

    def test_a_zero_limit_never_offers(self) -> None:
        assert offered(RosterRules(limit=0, probability=1.0), 0.0) is False


class TestPromptRoster:
    def test_the_least_sent_come_first_when_it_does_not_fit(self, store: StickerStore) -> None:
        _add(store, "老", ["a"], sent=9)
        _add(store, "中", ["b"], sent=3)
        _add(store, "新", ["c"])

        rows = prompt_roster(store, _CONV, RosterRules(limit=2), sendable=_always)

        # 按名字排的话「中」会挤掉「新」，冷门图永远没机会露面。
        assert [row.name for row in rows] == ["新", "中"]

    def test_recently_sent_in_this_conversation_is_left_out(self, store: StickerStore) -> None:
        hot = _add(store, "刚发过", ["a"])
        _add(store, "没发过", ["b"])
        store.record_sent(_CONV, hot)

        rows = prompt_roster(store, _CONV, RosterRules(repeat_window_sec=3600), sendable=_always)

        assert [row.name for row in rows] == ["没发过"]

    def test_the_platform_decides_what_it_can_send(self, store: StickerStore) -> None:
        _add(store, "发得出", ["a"])
        _add(store, "发不出", ["b"])

        rows = prompt_roster(store, _CONV, RosterRules(), sendable=lambda row: row.name == "发得出")

        assert [row.name for row in rows] == ["发得出"]

    def test_an_entry_names_the_picture_and_its_tags(self, store: StickerStore) -> None:
        tagged, bare = _add(store, "甲", ["开心、快乐", "猫"]), _add(store, "乙", [])

        assert roster_entry(store.get(tagged)) == "甲（开心、快乐、猫）"
        assert roster_entry(store.get(bare)) == "乙（无标签）"


class TestCandidates:
    def test_the_exact_name_comes_before_tag_matches(self, store: StickerStore) -> None:
        _add(store, "无语", ["猫"])
        _add(store, "猫猫", ["无语"])

        assert [row.name for row in candidates(store, "无语", skip=set())] == ["无语", "猫猫"]

    def test_skipped_pictures_are_passed_over(self, store: StickerStore) -> None:
        named = _add(store, "无语", ["猫"])
        _add(store, "猫猫", ["无语"])

        assert [row.name for row in candidates(store, "无语", skip={named})] == ["猫猫"]

    def test_every_offered_picture_joins_the_skip_set(self, store: StickerStore) -> None:
        named, tagged = _add(store, "无语", ["猫"]), _add(store, "猫猫", ["无语"])
        skip: set[str] = set()

        first = next(candidates(store, "无语", skip=skip))

        assert first.sha256 == named and skip == {named}
        assert [row.sha256 for row in candidates(store, "无语", skip=skip)] == [tagged]

    def test_a_word_matching_nothing_offers_nothing(self, store: StickerStore) -> None:
        _add(store, "甲", ["开心"])

        assert list(candidates(store, "无语", skip=set())) == []
