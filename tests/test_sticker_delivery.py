"""表情包选图、发送确认与重试。"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._onebot_harness import load_runtime, set_live_config
from stickers.store import CAPTION_DONE, STATE_LIBRARY


class Bot:
    self_id = "10000"

    def __init__(self, runtime):
        self.runtime = runtime
        self.sent = []
        self.attempts = 0
        self.fail_at = set()
        self.ambiguous = False

    async def call_api(self, name, **kwargs):
        if name == "get_group_member_list":
            return []
        assert name == "fetch_custom_face_detail"
        raise RuntimeError("synthetic collection API unavailable")

    async def send_group_msg(self, *, group_id, message):
        self.attempts += 1
        if self.attempts in self.fail_at:
            raise ConnectionRefusedError("synthetic send not started")
        if self.ambiguous:
            raise self.runtime.OneBotAmbiguousAckError("synthetic unknown acknowledgement")
        self.sent.append(list(message))
        return {"message_id": str(self.attempts)}

    async def send_private_msg(self, *, user_id, message):
        return await self.send_group_msg(group_id=user_id, message=message)


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    module = load_runtime(monkeypatch, tmp_path)
    set_live_config(module, sticker_send_probability=1.0, sticker_repeat_window_sec=600,
                    reply_to_trigger=False)
    module.set_sticker_resolver(module._resolve_sticker)
    module._adopt_bot_scope(SimpleNamespace(self_id=Bot.self_id))
    yield module
    if module._sticker_store is not None:
        module._sticker_store.close()
    module._outbound_idempotency.close()


def add_image(runtime, name="甲", tags=("开心",), *, size=None):
    raw = (f"synthetic-image-{name}".encode() if size is None else b"x" * size)
    digest = hashlib.sha256(raw).hexdigest()
    rel = f"data/stickers/library/{digest}.png"
    path = Path(runtime.CLONOTH_WORKSPACE) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    store = runtime._sticker_store_handle()
    store.add(sha256=digest, name=name, rel_path=rel, source="test", state=STATE_LIBRARY)
    store.set_auto_tags(digest, list(tags), version=1)
    store.mark_caption(digest, CAPTION_DONE)
    return store, digest, path


def target(kind="group", value=42):
    return {"type": kind, "group_id" if kind == "group" else "user_id": value}


def context(runtime, event="event-1", generation=0):
    return runtime.OutboundSendContext(event_id=event, idempotency_key=f"{event}:{generation}",
                                       replay_generation=generation)


def images(bot):
    return [segment["data"]["file"] for message in bot.sent for segment in message
            if segment["type"] == "image"]


@pytest.mark.parametrize("kind", ["group", "private"])
def test_collection_failure_still_sends_local_image_and_text(runtime, kind):
    store, digest, _ = add_image(runtime)
    bot = Bot(runtime)
    assert asyncio.run(runtime._send_split_text(
        bot, target(kind), "正文[表情:甲]", send_context=context(runtime),
    ))
    assert len(images(bot)) == 1
    assert any(item["data"].get("text") == "正文" for item in bot.sent[0])
    assert all("sticker_sha256" not in item["data"] for item in bot.sent[0])
    assert store.get(digest).sent_count == 1


def test_resolution_does_not_count_and_name_and_tag_share_cooldown(runtime):
    store, digest, _ = add_image(runtime)
    key = runtime._sticker_conversation_key(target())

    async def run():
        token = runtime._sticker_send_conversation.set(key)
        try:
            assert await runtime._resolve_sticker("甲")
            assert await runtime._resolve_sticker("开心")
            assert store.get(digest).sent_count == 0
            store.record_sent(key, digest)
            assert not await runtime._resolve_sticker("甲")
            assert not await runtime._resolve_sticker("开心")
        finally:
            runtime._sticker_send_conversation.reset(token)
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["missing", "empty", "oversize"])
def test_bad_tag_candidate_falls_back_to_next(runtime, failure):
    store, bad, path = add_image(runtime, "甲")
    if failure == "missing":
        path.unlink()
    else:
        path.write_bytes(b"" if failure == "empty" else b"x" * (3 * 1024 * 1024 + 1))
    _, good, _ = add_image(runtime, "乙")
    result = asyncio.run(runtime._resolve_sticker("开心"))
    assert result.sha256 == good
    assert store.get(bad).sent_count == store.get(good).sent_count == 0


@pytest.mark.parametrize("size,allowed", [(0, False), (3 * 1024 * 1024, True),
                                          (3 * 1024 * 1024 + 1, False)])
def test_resolver_checks_actual_file_size(runtime, size, allowed):
    add_image(runtime, size=size)
    assert bool(asyncio.run(runtime._resolve_sticker("甲"))) is allowed


def test_definitely_failed_send_does_not_count_and_retry_reuses_image(runtime):
    store, digest, _ = add_image(runtime)
    bot = Bot(runtime)
    bot.fail_at.add(1)

    async def run():
        with pytest.raises(runtime.classify_send_exception(ConnectionRefusedError()).__class__):
            await runtime._send_split_text(bot, target(), "[表情:开心]", send_context=context(runtime))
        assert store.get(digest).sent_count == 0
        other = runtime._sticker_conversation_key(target(value=43))
        store.record_sent(other, digest)
        assert await runtime._send_split_text(bot, target(), "[表情:开心]", send_context=context(runtime))
    asyncio.run(run())
    assert len(images(bot)) == 1
    assert store.get(digest).sent_count == 2


def test_successful_retry_after_restart_does_not_reread_deleted_image(runtime):
    store, digest, path = add_image(runtime)
    bot = Bot(runtime)

    async def run():
        await runtime._send_split_text(bot, target(), "[表情:甲]", send_context=context(runtime))
        state = runtime._outbound_idempotency
        state.close()
        runtime._outbound_idempotency = type(state)(state.path)
        path.unlink()
        await runtime._send_split_text(bot, target(), "[表情:甲]", send_context=context(runtime))
    asyncio.run(run())
    assert len(images(bot)) == 1
    assert store.get(digest).sent_count == 1


def test_partially_sent_reply_only_retries_failed_part(runtime):
    store, first, _ = add_image(runtime, "甲")
    _, second, _ = add_image(runtime, "乙")
    bot = Bot(runtime)
    bot.fail_at.add(2)
    set_live_config(runtime, reply_to_trigger=True)
    destination = dict(target(), reply_message_id="77")
    text = "第一句[表情:甲][SPLIT]第二句[表情:乙]"

    async def run():
        with pytest.raises(Exception):
            await runtime._send_split_text(bot, destination, text, send_context=context(runtime))
        assert store.get(first).sent_count == 1
        assert store.get(second).sent_count == 0
        await runtime._send_split_text(bot, destination, text, send_context=context(runtime))
    asyncio.run(run())
    assert len(bot.sent) == len(images(bot)) == 2
    assert sum(item["type"] == "reply" for message in bot.sent for item in message) == 1
    assert store.get(first).sent_count == store.get(second).sent_count == 1


def test_multiple_markers_and_concurrent_retries_do_not_repeat_one_image(runtime):
    store, digest, _ = add_image(runtime)
    bot = Bot(runtime)

    async def run():
        await asyncio.gather(*(runtime._send_split_text(
            bot, target(), "[表情:甲][表情:开心]", send_context=context(runtime),
        ) for _ in range(2)))
    asyncio.run(run())
    assert len(images(bot)) == 1
    assert store.get(digest).sent_count == 1
    assert not runtime._sticker_delivery_locks


def test_ambiguous_ack_is_not_counted_or_retried(runtime):
    store, digest, _ = add_image(runtime)
    bot = Bot(runtime)
    bot.ambiguous = True

    async def run():
        for _ in range(2):
            with pytest.raises(runtime.OneBotAmbiguousAckError):
                await runtime._send_split_text(bot, target(), "[表情:甲]", send_context=context(runtime))
        assert not await runtime._outbound_idempotency.pending_message_receipts()
    asyncio.run(run())
    assert bot.attempts == 1
    assert store.get(digest).sent_count == 0


def test_confirmed_delivery_is_reconciled_after_count_failure(runtime, monkeypatch):
    store, digest, _ = add_image(runtime)
    original = store.record_sent
    bot = Bot(runtime)

    def fail(*args, **kwargs):
        raise OSError("synthetic disk busy")

    async def run():
        monkeypatch.setattr(store, "record_sent", fail)
        await runtime._send_split_text(bot, target(), "[表情:甲]", send_context=context(runtime))
        assert store.get(digest).sent_count == 0
        assert len(await runtime._outbound_idempotency.pending_message_receipts()) == 1
        monkeypatch.setattr(store, "record_sent", original)
        assert await runtime._reconcile_sticker_receipts()
        await runtime._send_split_text(bot, target(), "[表情:甲]", send_context=context(runtime))
    asyncio.run(run())
    assert len(images(bot)) == store.get(digest).sent_count == 1


def test_confirmed_delivery_is_not_counted_twice_when_receipt_ack_fails(runtime, monkeypatch):
    store, digest, _ = add_image(runtime)
    outbound = runtime._outbound_idempotency
    original = outbound.ack_message_receipt
    bot = Bot(runtime)

    async def fail(*args, **kwargs):
        raise OSError("synthetic receipt ack failure")

    async def run():
        monkeypatch.setattr(outbound, "ack_message_receipt", fail)
        await runtime._send_split_text(bot, target(), "[表情:甲]", send_context=context(runtime))
        assert store.get(digest).sent_count == 1
        monkeypatch.setattr(outbound, "ack_message_receipt", original)
        assert await runtime._reconcile_sticker_receipts()
    asyncio.run(run())
    assert store.get(digest).sent_count == 1


def test_different_conversations_and_new_events_are_independent(runtime):
    store, digest, _ = add_image(runtime)
    bot = Bot(runtime)

    async def run():
        await runtime._send_split_text(bot, target(), "[表情:甲]", send_context=context(runtime))
        await runtime._send_split_text(bot, target(value=43), "[表情:甲]", send_context=context(runtime))
        set_live_config(runtime, sticker_repeat_window_sec=0)
        await runtime._send_split_text(bot, target(), "[表情:甲]", send_context=context(runtime, generation=1))
    asyncio.run(run())
    assert store.get(digest).sent_count == len(images(bot)) == 3


def test_frozen_selection_refuses_changed_file_before_send(runtime):
    store, digest, path = add_image(runtime)
    bot = Bot(runtime)
    bot.fail_at.add(1)

    async def run():
        with pytest.raises(Exception):
            await runtime._send_split_text(bot, target(), "[表情:甲]", send_context=context(runtime))
        path.write_bytes(b"changed image")
        with pytest.raises(runtime.OneBotSendContractError):
            await runtime._send_split_text(bot, target(), "[表情:甲]", send_context=context(runtime))
    asyncio.run(run())
    assert bot.attempts == 1
    assert store.get(digest).sent_count == 0


def test_combat_and_burst_count_only_confirmed_images(runtime):
    store, first, _ = add_image(runtime, "甲", ("开心", "大笑"))
    _, second, _ = add_image(runtime, "乙", ("开心", "大笑"))
    bot = Bot(runtime)
    destination = dict(target(), _response_purpose="ambient")
    ctx = runtime.OutboundSendContext(event_id="combat:1", purpose="ambient",
                                      conversation_key=runtime._sticker_conversation_key(target()))

    async def run():
        assert await runtime._send_combat_sticker(bot, destination, "开心大笑", ctx, 0)
        bot.fail_at.add(2)
        assert not await runtime._send_combat_sticker(bot, destination, "开心大笑", ctx, 1)
        assert sum(store.get(digest).sent_count for digest in (first, second)) == 1
        assert await runtime._send_combat_sticker(bot, destination, "开心大笑", ctx, 1)
    asyncio.run(run())
    assert len(images(bot)) == 2
    assert store.get(first).sent_count == store.get(second).sent_count == 1


def test_cooldown_and_zero_cooldown_for_following_event(runtime):
    store, digest, _ = add_image(runtime)
    bot = Bot(runtime)

    async def run():
        await runtime._send_split_text(bot, target(), "[表情:甲]", send_context=context(runtime))
        assert await runtime._send_split_text(bot, target(), "正文[表情:甲]", send_context=context(runtime, "event-2"))
        set_live_config(runtime, sticker_repeat_window_sec=0)
        await runtime._send_split_text(bot, target(), "[表情:甲]", send_context=context(runtime, "event-3"))
    asyncio.run(run())
    assert len(bot.sent) == 3
    assert len(images(bot)) == store.get(digest).sent_count == 2


def test_favorite_metadata_still_sends_without_local_count(runtime, monkeypatch):
    store, digest, _ = add_image(runtime)
    bot = Bot(runtime)
    monkeypatch.setattr(runtime, "_current_custom_face_metadata", lambda: [
        {"name": "甲", "url": "https://example.invalid/favorite.png"},
    ])
    asyncio.run(runtime._send_split_text(bot, target(), "[表情:甲]", send_context=context(runtime)))
    assert images(bot) == ["https://example.invalid/favorite.png"]
    assert store.get(digest).sent_count == 0


def test_mixed_favorites_and_local_images_preserve_their_accounting(runtime, monkeypatch):
    store, digest, _ = add_image(runtime)
    bot = Bot(runtime)
    monkeypatch.setattr(runtime, "_current_custom_face_metadata", lambda: [
        {"name": "收藏", "url": "https://example.invalid/favorite.png"},
    ])
    asyncio.run(runtime._send_split_text(
        bot, target(), "[表情:收藏][表情:甲]", send_context=context(runtime),
    ))
    assert len(images(bot)) == 2
    assert store.get(digest).sent_count == 1


def test_missing_platform_message_id_is_ambiguous_not_success(runtime, monkeypatch):
    store, digest, _ = add_image(runtime)
    bot = Bot(runtime)

    async def send(**kwargs):
        return {}

    monkeypatch.setattr(bot, "send_group_msg", send)
    with pytest.raises(runtime.OneBotAmbiguousAckError):
        asyncio.run(runtime._send_split_text(bot, target(), "[表情:甲]", send_context=context(runtime)))
    assert store.get(digest).sent_count == 0


def test_plain_first_part_does_not_trigger_legacy_quarantine(runtime):
    store, digest, _ = add_image(runtime)
    bot = Bot(runtime)
    asyncio.run(runtime._send_split_text(
        bot, target(), "正文[SPLIT][表情:甲]", send_context=context(runtime),
    ))
    assert len(bot.sent) == 2
    assert store.get(digest).sent_count == 1


def test_old_delivery_claim_is_quarantined_before_new_selection(runtime):
    store, digest, _ = add_image(runtime)
    bot = Bot(runtime)
    outbound = runtime._outbound_idempotency
    ctx = context(runtime)

    async def run():
        key = f"event:{ctx.idempotency_key}:old-rendered-hash:group:42"
        claim = await outbound.begin(key)
        await outbound.commit(claim, platform_message_id="old-1")
        outbound._db.execute("UPDATE claims SET updated=? WHERE key=?", (outbound.protocol_started_at - 1, key))
        with pytest.raises(runtime.OneBotAmbiguousAckError):
            await runtime._send_split_text(bot, target(), "[表情:甲]", send_context=ctx)
    asyncio.run(run())
    assert bot.attempts == store.get(digest).sent_count == 0


def test_context_free_delivery_can_send_again_after_short_retention(runtime):
    store, digest, _ = add_image(runtime)
    bot = Bot(runtime)
    set_live_config(runtime, sticker_repeat_window_sec=0)
    outbound = runtime._outbound_idempotency
    now = [outbound._clock()]
    outbound._clock = lambda: now[0]

    async def run():
        await runtime._send_split_text(bot, target(), "[表情:甲]")
        await runtime._send_split_text(bot, target(), "[表情:甲]")
        assert len(images(bot)) == 1
        now[0] += runtime.ONEBOT_IDEMPOTENCY_FALLBACK_SENT_TTL_SECONDS + 1
        await runtime._send_split_text(bot, target(), "[表情:甲]")
    asyncio.run(run())
    assert len(images(bot)) == store.get(digest).sent_count == 2


def test_combat_retry_uses_event_identity_not_changed_history(runtime):
    store, digest, _ = add_image(runtime, tags=("开心", "大笑"))
    bot = Bot(runtime)
    ctx = runtime.OutboundSendContext(event_id="combat:stable", purpose="ambient",
                                      conversation_key=runtime._sticker_conversation_key(target()))

    async def run():
        assert await runtime._send_combat_sticker(bot, target(), "开心", ctx, 0)
        assert await runtime._send_combat_sticker(bot, target(), "后来大家又大笑了", ctx, 0)
    asyncio.run(run())
    assert len(images(bot)) == store.get(digest).sent_count == 1


def test_receipt_backlog_never_allows_selection_with_stale_cooldown(runtime):
    store, digest, _ = add_image(runtime)
    outbound = runtime._outbound_idempotency
    key = runtime._sticker_conversation_key(target())

    async def run():
        for index in range(101):
            async def prepare():
                return {"processed_segments": [{"type": "text", "content": "synthetic"}],
                        "send_identity": f"pending:{index}", "conversation_key": key,
                        "selected_images": [digest] if index == 100 else []}
            plan = await outbound.get_or_create_message_plan(f"pending:{index}", target(), prepare)
            claim = await outbound.begin(plan.claim_key)
            await outbound.commit(claim, platform_message_id=f"p{index}")
        token = runtime._sticker_send_conversation.set(key)
        try:
            assert not await runtime._resolve_sticker("甲")
            assert await runtime._reconcile_sticker_receipts()
            assert not await runtime._resolve_sticker("甲")
        finally:
            runtime._sticker_send_conversation.reset(token)
    asyncio.run(run())
    assert store.get(digest).sent_count == 1


def test_inflight_old_account_uses_its_own_cooldown_after_account_switch(runtime):
    store, digest, _ = add_image(runtime)
    old_bot = Bot(runtime)
    old_key = runtime._sticker_conversation_key(target(), old_bot)
    runtime._adopt_bot_scope(SimpleNamespace(self_id="20000"))
    new_key = runtime._sticker_conversation_key(target())
    assert old_key != new_key
    asyncio.run(runtime._send_split_text(
        old_bot, target(), "[表情:甲]", send_context=context(runtime),
    ))
    assert digest in store.recently_sent(old_key, within_sec=600)
    assert digest not in store.recently_sent(new_key, within_sec=600)
