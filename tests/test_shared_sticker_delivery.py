"""表情包发送的通用内核：挑图跳过刚发过和这条回复挑过的，按计划读图时核对内容，确认送达才记账，同一会话串行。"""
from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adapters._shared.outbound import planned_stickers, sticker_segment  # noqa: E402
from adapters._shared.send_guard import SendContractError, TwoPhaseIdempotencyStore  # noqa: E402
from adapters._shared.sticker_delivery import ConversationLocks, StickerDelivery  # noqa: E402
from stickers.store import CAPTION_DONE, STATE_LIBRARY, Sticker, StickerStore  # noqa: E402

_CONV = "chat_room:5f3c2a108d4e4b7a9c210e6f"
_WINDOW = 600


def _room(target: Mapping[str, Any]) -> str:
    return f"room:{target['room']}"


def _fail(*args: Any, **kwargs: Any) -> Any:
    raise OSError("synthetic disk busy")


class _Platform:
    """图库加一个按内存读图的平台：files 里没有的图就算发不了。"""

    def __init__(self, tmp_path: Path) -> None:
        self.store = StickerStore(tmp_path / "stickers.sqlite3")
        self.plans = TwoPhaseIdempotencyStore(tmp_path / "plans.sqlite3", target_identity=_room)
        self.files: dict[str, bytes] = {}
        self.delivery = StickerDelivery(plans=self.plans, open_store=lambda: self.store, read=self.read)

    def read(self, row: Sticker) -> bytes:
        return self.files.get(row.sha256, b"")

    def add(self, name: str, tags: list[str]) -> str:
        raw = f"synthetic-{name}".encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        self.store.add(
            sha256=digest, rel_path=f"data/stickers/library/{digest}.png", size=len(raw), source="test",
            state=STATE_LIBRARY, name=name,
        )
        self.store.set_auto_tags(digest, tags, version=1)
        self.store.mark_caption(digest, CAPTION_DONE)
        self.files[digest] = raw
        return digest

    def segment(self, digest: str) -> dict[str, str]:
        return sticker_segment(digest, hashlib.sha256(self.files[digest]).hexdigest())

    async def plan(self, key: str, digests: list[str]) -> Any:
        segments = [self.segment(digest) for digest in digests]

        async def build() -> dict[str, Any]:
            return {
                "processed_segments": segments, "send_identity": key,
                "selected_images": planned_stickers(segments), "conversation_key": _CONV,
            }

        return await self.plans.get_or_create_message_plan(key, {"room": "abc"}, build)

    async def confirm(self, key: str, digests: list[str]) -> str:
        """造一次确认送达：冻结选图的计划，领取后提交，留下一张待记账的回执。"""
        plan = await self.plan(key, digests)
        claim = await self.plans.begin(plan.claim_key)
        await self.plans.commit(claim, platform_message_id=f"p-{key}")
        return plan.delivery_id

    def pick(self, name: str, **options: Any) -> Any:
        return asyncio.run(self.delivery.pick(name, conversation_key=_CONV, repeat_window_sec=_WINDOW, **options))

    def close(self) -> None:
        self.plans.close()
        self.store.close()


@pytest.fixture()
def platform(tmp_path: Path):
    handle = _Platform(tmp_path)
    yield handle
    handle.close()


class TestPick:
    def test_the_pick_carries_the_digest_of_what_was_read(self, platform: _Platform) -> None:
        digest = platform.add("wave", ["hello"])

        picked = platform.pick("wave")

        assert (picked.sha256, picked.content_sha256) == (digest, hashlib.sha256(platform.files[digest]).hexdigest())

    def test_sent_here_recently_or_picked_in_this_reply_is_skipped(self, platform: _Platform) -> None:
        recent, chosen, fresh = (platform.add(name, ["hello"]) for name in ("a", "b", "c"))
        platform.store.record_sent(_CONV, recent)
        selected = {chosen}

        picked = platform.pick("hello", selected=selected)

        assert picked.sha256 == fresh and selected == {chosen, fresh}

    def test_the_repeat_window_belongs_to_one_conversation(self, platform: _Platform) -> None:
        digest = platform.add("wave", ["hello"])
        platform.store.record_sent("chat_room:elsewhere", digest)

        assert platform.pick("wave").sha256 == digest

    def test_a_picture_the_platform_cannot_send_gives_way_to_the_next(self, platform: _Platform) -> None:
        unsendable, sendable = platform.add("a", ["hello"]), platform.add("b", ["hello"])
        del platform.files[unsendable]

        assert platform.pick("hello").sha256 == sendable

    def test_nothing_is_picked_without_a_library(self, platform: _Platform) -> None:
        blind = StickerDelivery(plans=platform.plans, open_store=lambda: None, read=platform.read)

        assert asyncio.run(blind.pick("wave", conversation_key=_CONV, repeat_window_sec=_WINDOW)) is None

    def test_nothing_is_picked_while_a_confirmed_send_is_left_uncounted(
        self, platform: _Platform, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        digest = platform.add("wave", ["hello"])
        asyncio.run(platform.confirm("earlier", [digest]))
        monkeypatch.setattr(platform.store, "record_sent", _fail)

        # 刚送达的图还没进去重窗口，这时挑图会把它再挑一次。
        assert platform.pick("wave") is None

    def test_a_library_failure_picks_nothing_and_is_logged(
        self, platform: _Platform, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        platform.add("wave", ["hello"])
        monkeypatch.setattr(platform.store, "by_name", _fail)

        assert platform.pick("wave") is None
        assert "表情包取图失败" in caplog.text


class TestLoad:
    def test_the_planned_picture_is_read_back(self, platform: _Platform) -> None:
        digest = platform.add("wave", ["hello"])

        assert asyncio.run(platform.delivery.load(platform.segment(digest))) == platform.files[digest]

    @pytest.mark.parametrize("change", ["replaced", "unsendable"])
    def test_a_changed_or_unsendable_picture_is_refused_not_swapped(self, platform: _Platform, change: str) -> None:
        digest = platform.add("wave", ["hello"])
        platform.add("nod", ["hello"])
        segment = platform.segment(digest)
        if change == "replaced":
            platform.files[digest] = b"another picture"
        else:
            del platform.files[digest]

        with pytest.raises(SendContractError):
            asyncio.run(platform.delivery.load(segment))

    def test_a_picture_gone_from_the_library_is_refused(self, platform: _Platform) -> None:
        with pytest.raises(SendContractError):
            asyncio.run(platform.delivery.load(sticker_segment("a" * 64, "b" * 64)))


class TestSettle:
    def test_a_confirmed_send_is_counted_once(self, platform: _Platform) -> None:
        digest = platform.add("wave", ["hello"])

        async def scenario() -> tuple[bool, bool]:
            delivery_id = await platform.confirm("reply-1", [digest])
            return await platform.delivery.settle(delivery_id), await platform.delivery.settle(delivery_id)

        assert asyncio.run(scenario()) == (True, True)
        assert platform.store.get(digest).sent_count == 1
        assert digest in platform.store.recently_sent(_CONV, within_sec=_WINDOW)

    def test_an_unconfirmed_plan_counts_nothing(self, platform: _Platform) -> None:
        digest = platform.add("wave", ["hello"])

        async def scenario() -> bool:
            plan = await platform.plan("reply-2", [digest])
            return await platform.delivery.settle(plan.delivery_id)

        assert asyncio.run(scenario()) is True
        assert platform.store.get(digest).sent_count == 0

    def test_without_a_library_the_receipt_waits_for_later(self, platform: _Platform) -> None:
        digest = platform.add("wave", ["hello"])
        blind = StickerDelivery(plans=platform.plans, open_store=lambda: None, read=platform.read)

        async def scenario() -> tuple[bool, bool]:
            delivery_id = await platform.confirm("reply-3", [digest])
            return await blind.settle(delivery_id), await platform.delivery.settle(delivery_id)

        assert asyncio.run(scenario()) == (False, True)
        assert platform.store.get(digest).sent_count == 1

    def test_an_accounting_failure_is_retried_later(
        self, platform: _Platform, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        digest = platform.add("wave", ["hello"])
        original = platform.store.record_sent

        async def scenario() -> tuple[bool, bool]:
            delivery_id = await platform.confirm("reply-4", [digest])
            monkeypatch.setattr(platform.store, "record_sent", _fail)
            failed = await platform.delivery.settle(delivery_id)
            monkeypatch.setattr(platform.store, "record_sent", original)
            return failed, await platform.delivery.settle(delivery_id)

        assert asyncio.run(scenario()) == (False, True)
        assert platform.store.get(digest).sent_count == 1
        assert "表情包发送回执记账失败" in caplog.text


class TestConversationLocks:
    def test_one_conversation_is_served_in_turn_and_others_are_not_held(self) -> None:
        locks = ConversationLocks()
        order: list[str] = []

        async def reply(key: str, label: str, pause: float) -> None:
            async with locks.hold(key):
                order.append(f"{label}+")
                await asyncio.sleep(pause)
                order.append(f"{label}-")

        async def scenario() -> None:
            await asyncio.gather(reply("a", "first", 0.02), reply("a", "second", 0), reply("b", "other", 0))

        asyncio.run(scenario())

        assert order.index("first-") < order.index("second+")
        assert order.index("other-") < order.index("first-")
        assert len(locks) == 0

    def test_a_failing_holder_still_lets_the_next_one_in(self) -> None:
        locks = ConversationLocks()

        async def scenario() -> None:
            with pytest.raises(RuntimeError):
                async with locks.hold("a"):
                    raise RuntimeError("synthetic failure")
            async with locks.hold("a"):
                pass

        asyncio.run(scenario())

        assert len(locks) == 0
