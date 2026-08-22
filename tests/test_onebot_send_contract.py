from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
import importlib.util
import inspect
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _onebot_harness import load_live_config, load_runtime as _load_runtime, set_live_config, write_live_config  # noqa: E402
from clonoth_sdk.types import DeliveryContext  # noqa: E402

_MODULE_PATH = Path(__file__).resolve().parents[1] / "adapters" / "onebot" / "send_contract.py"
_SPEC = importlib.util.spec_from_file_location("_onebot_send_contract", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

_EMOJI_MODULE_PATH = Path(__file__).resolve().parents[1] / "adapters" / "onebot" / "emoji_handler.py"
_EMOJI_SPEC = importlib.util.spec_from_file_location("_onebot_emoji_handler", _EMOJI_MODULE_PATH)
assert _EMOJI_SPEC is not None and _EMOJI_SPEC.loader is not None
_EMOJI_MODULE = importlib.util.module_from_spec(_EMOJI_SPEC)
sys.modules[_EMOJI_SPEC.name] = _EMOJI_MODULE
_EMOJI_SPEC.loader.exec_module(_EMOJI_MODULE)

from _onebot_emoji_handler import (  # type: ignore[import-not-found]  # noqa: E402
    process_emojis,
    strip_output_markers,
)
from _onebot_send_contract import (  # type: ignore[import-not-found]  # noqa: E402
    IdempotencyOwnershipError,
    OneBotAmbiguousAckError,
    OneBotSendContractError,
    OneBotSendNotStartedError,
    OutboundSendContext,
    TwoPhaseIdempotencyStore,
    classify_send_exception,
    context_from_sources,
    image_content_identity,
    make_idempotency_key,
    protected_claim_send,
    target_from_idempotency_key,
    validate_send_request,
)


class _Bot:
    pass


def test_default_markdown_policy_strips_stars_but_preserves_underscores() -> None:
    source = (
        "抓到真凶了！你写成了 PUB_CACHE，正确叫法是 PUB_CACHE；"
        "样式 *斜体*、**粗体**、_下划线斜体_、__下划线粗体__；"
        "行内代码 `PUB_CACHE`。"
    )
    expected = (
        "抓到真凶了！你写成了 PUB_CACHE，正确叫法是 PUB_CACHE；"
        "样式 斜体、粗体、_下划线斜体_、__下划线粗体__；"
        "行内代码 PUB_CACHE。"
    )

    assert strip_output_markers(source) == expected

    segments = asyncio.run(process_emojis(source, _Bot(), []))
    assert "".join(str(item.get("content") or "") for item in segments) == expected


def test_markdown_style_cleanup_can_be_explicitly_enabled() -> None:
    source = "*斜体*、**粗体**、_下划线斜体_、__下划线粗体__"

    assert strip_output_markers(source, strip_markdown_styles=True) == (
        "斜体、粗体、下划线斜体、下划线粗体"
    )


def test_markdown_style_cleanup_config_uses_safe_split_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    for key in (
        "ONEBOT_STRIP_MARKDOWN_STYLES",
        "ONEBOT_STRIP_MARKDOWN_ASTERISK_STYLES",
        "ONEBOT_STRIP_MARKDOWN_UNDERSCORE_STYLES",
    ):
        monkeypatch.delenv(key, raising=False)
    default = load_live_config(monkeypatch, tmp_path / "default")
    assert default.live.strip_asterisk_styles is True
    assert default.live.strip_underscore_styles is False

    underscore_enabled = load_live_config(
        monkeypatch, tmp_path / "underscore", ONEBOT_STRIP_MARKDOWN_UNDERSCORE_STYLES="1",
    )
    assert underscore_enabled.live.strip_asterisk_styles is True
    assert underscore_enabled.live.strip_underscore_styles is True

    monkeypatch.delenv("ONEBOT_STRIP_MARKDOWN_UNDERSCORE_STYLES", raising=False)
    # 拆分前的旧总开关仍要同时决定两个新开关的默认值。
    legacy_enabled = load_live_config(
        monkeypatch, tmp_path / "legacy", ONEBOT_STRIP_MARKDOWN_STYLES="1",
    )
    assert legacy_enabled.live.strip_asterisk_styles is True
    assert legacy_enabled.live.strip_underscore_styles is True


def test_markdown_style_yaml_overrides_the_legacy_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    module = load_live_config(monkeypatch, tmp_path, ONEBOT_STRIP_MARKDOWN_STYLES="1")

    write_live_config(module, strip_underscore_styles=False)

    assert module.live.strip_asterisk_styles is True
    assert module.live.strip_underscore_styles is False


def test_missing_bot_and_target_are_explicit_contract_errors() -> None:
    with pytest.raises(OneBotSendContractError, match="missing bot"):
        validate_send_request(None, {"type": "group", "group_id": 1})
    with pytest.raises(OneBotSendContractError, match="missing target"):
        validate_send_request(_Bot(), None)
    with pytest.raises(OneBotSendContractError, match="missing group_id"):
        validate_send_request(_Bot(), {"type": "group"})


def test_two_phase_failure_releases_pending_and_success_commits_sent(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = TwoPhaseIdempotencyStore(tmp_path / "claims.sqlite3")

        first = await store.begin("event-1")
        assert first.acquired is True
        assert await store.state("event-1") == "pending"

        concurrent = await store.begin("event-1")
        assert concurrent.acquired is False
        assert concurrent.state == "pending"

        await store.release(first)
        assert await store.state("event-1") is None

        retry = await store.begin("event-1")
        assert retry.acquired is True
        await store.commit(retry)
        assert await store.state("event-1") == "sent"

        replay = await store.begin("event-1")
        assert replay.acquired is False
        assert replay.state == "sent"

    asyncio.run(exercise())


def test_sent_state_survives_restart(tmp_path: Path) -> None:
    async def exercise() -> None:
        path = tmp_path / "claims.sqlite3"
        first = TwoPhaseIdempotencyStore(path)
        claim = await first.begin("restart-key")
        await first.commit(claim)
        first.close()
        restarted = TwoPhaseIdempotencyStore(path)
        assert await restarted.state("restart-key") == "sent"
        duplicate = await restarted.begin("restart-key")
        assert duplicate.acquired is False and duplicate.state == "sent"

    asyncio.run(exercise())


def test_event_id_is_preferred_and_fallback_uses_target_and_content() -> None:
    target = {"type": "private", "user_id": 42}
    event_key_a = make_idempotency_key(target, "first body", event_id="outbound-7")
    event_key_b = make_idempotency_key(target, "changed body", event_id="outbound-7")
    assert event_key_a == event_key_b

    fallback_a = make_idempotency_key(target, "first body")
    fallback_b = make_idempotency_key(target, "changed body")
    assert fallback_a != fallback_b
    assert fallback_a != make_idempotency_key({"type": "private", "user_id": 43}, "first body")


def test_image_identity_uses_file_content_not_path_or_transport() -> None:
    payload = b"same image bytes"
    local_path_identity = image_content_identity(payload)
    base64_resend_identity = image_content_identity(payload)
    assert local_path_identity == base64_resend_identity
    assert local_path_identity.startswith("image:sha256:")


def test_send_exception_classification_distinguishes_retry_safety() -> None:
    retryable = classify_send_exception(RuntimeError("ENOENT: no such file or directory"))
    assert retryable.retryable is True
    assert isinstance(retryable, OneBotSendNotStartedError)
    assert retryable.definitely_not_sent is True
    assert retryable.ambiguous_ack is False

    ambiguous = classify_send_exception(RuntimeError("Timeout: NTEvent sendMsg"))
    assert ambiguous.retryable is False

    bad_request = classify_send_exception(RuntimeError("invalid group parameter"))
    assert bad_request.retryable is False

    forward_timeout = classify_send_exception(
        RuntimeError("NetWorkError: WebSocket call api send_group_forward_msg timeout")
    )
    assert forward_timeout.ambiguous_ack is True

    at_uid = classify_send_exception(RuntimeError("Get Uid Error"))
    assert isinstance(at_uid, OneBotSendNotStartedError)


class _AtGroupBot:
    """群发假件：按序返回 send_group_msg 的成败，并暴露每次实际发出的 message。"""

    self_id = "1"

    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = list(outcomes)
        self.sends: list[Any] = []

    async def call_api(self, name: str, **kwargs: Any) -> Any:
        if name == "get_group_member_list":
            return [{"user_id": 111, "card": "", "nickname": "群友"}]
        return None

    async def send_group_msg(self, *, group_id: int, message: Any) -> Any:
        self.sends.append(message)
        outcome = self._outcomes[min(len(self.sends) - 1, len(self._outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _at_message(runtime: Any) -> Any:
    return runtime.MessageSegment.at(111) + runtime.MessageSegment.text("hi")


def test_group_at_message_is_not_resent_after_ambiguous_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    bot = _AtGroupBot([runtime.ActionFailed("Timeout: NTEvent ... sendMsg")])

    with pytest.raises(runtime.OneBotAmbiguousAckError):
        asyncio.run(runtime._send_qq_message(
            bot, {"type": "group", "group_id": 5}, _at_message(runtime),
            idempotency_key="k-timeout",
        ))

    assert len(bot.sends) == 1
    assert asyncio.run(runtime._outbound_idempotency.state("k-timeout")) == "ambiguous"


def test_group_unknown_failure_is_not_resent_as_at_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    bot = _AtGroupBot([runtime.ActionFailed("rich media transfer failed")])

    with pytest.raises(runtime.OneBotAmbiguousAckError):
        asyncio.run(runtime._send_qq_message(
            bot, {"type": "group", "group_id": 5}, _at_message(runtime),
            idempotency_key="k-unknown",
        ))

    assert len(bot.sends) == 1
    assert asyncio.run(runtime._outbound_idempotency.state("k-unknown")) == "ambiguous"


def test_group_at_uid_failure_degrades_to_text_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    bot = _AtGroupBot([runtime.ActionFailed("Get Uid Error"), {"message_id": 77}])

    result = asyncio.run(runtime._send_qq_message(
        bot, {"type": "group", "group_id": 5}, _at_message(runtime),
        idempotency_key="k-uid",
    ))

    assert result == "77"
    assert len(bot.sends) == 2
    assert asyncio.run(runtime._outbound_idempotency.state("k-uid")) == "sent"
    second = bot.sends[1]
    assert all(seg.get("type") != "at" for seg in second)
    assert any("@群友" in str(seg.get("data", {}).get("text", "")) for seg in second)


def test_temp_file_missing_resend_keeps_at_segment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    bot = _AtGroupBot([
        runtime.ActionFailed("ENOENT: no such file or directory"),
        {"message_id": 88},
    ])

    result = asyncio.run(runtime._send_qq_message(
        bot, {"type": "group", "group_id": 5}, _at_message(runtime),
        idempotency_key="k-enoent",
    ))

    assert result == "88"
    assert len(bot.sends) == 2
    assert any(seg.get("type") == "at" for seg in bot.sends[1])


def test_send_once_has_no_dead_dedupe_parameter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    once_params = inspect.signature(runtime._send_qq_message_once).parameters
    send_params = inspect.signature(runtime._send_qq_message).parameters

    assert "dedupe" not in once_params
    assert "dedupe" not in send_params
    assert "idempotency_key" in once_params
    assert hasattr(runtime, "_is_api_call_timeout") is False
    assert hasattr(runtime, "_is_napcat_sendmsg_timeout") is False
    assert hasattr(runtime, "_is_napcat_enoent") is False


@dataclass
class _Trigger:
    inbound_seq: int = 12
    conversation_key: str = "qq_group:abc"
    task_id: str = "task-from-trigger"
    platform_data: dict = field(default_factory=lambda: {"legacy": True})


@dataclass
class _MainState:
    platform_data: dict = field(
        default_factory=lambda: {"task_id": "task-from-main-state"}
    )


def test_context_accepts_current_and_future_sdk_callback_sources() -> None:
    context = context_from_sources(
        trigger=_Trigger(),
        main_state=_MainState(),
        platform_data={"conversation_key": "qq_group:future"},
        event_data={
            "event_seq": 99,
            "event_id": "evt-99",
            "payload": {
                "task_id": "task-from-event",
                "source_inbound_seq": 12,
            },
        },
    )
    assert context == OutboundSendContext(
        event_seq=99,
        event_id="evt-99",
        task_id="task-from-event",
        source_inbound_seq=12,
        conversation_key="qq_group:future",
    )


def test_child_context_gives_each_message_under_one_event_a_stable_key() -> None:
    root = OutboundSendContext(event_id="evt-1")
    first = root.child("text:0:hello")
    same = root.child("text:0:hello")
    second = root.child("text:1:world")
    assert first.event_id == same.event_id
    assert first.event_id != second.event_id
def test_real_attachment_stack_awaits_all_and_aggregates_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    release = asyncio.Event()
    calls: list[str] = []

    async def send_path(bot, target, path, filename="", **kwargs):
        calls.append(path.name)
        if path.name == "first.png":
            await release.wait()
            raise ConnectionError("connection reset")
        return "message-2"

    monkeypatch.setattr(runtime, "_send_attachment_path", send_path)

    async def exercise() -> None:
        task = asyncio.create_task(runtime._send_text_and_attachments(
            object(), {"type": "group", "group_id": 1}, "",
            [{"path": str(tmp_path / "first.png")}, {"path": str(tmp_path / "second.png")}],
            send_context=DeliveryContext(event_id="evt-batch", idempotency_key="id:evt-batch"),
        ))
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(runtime.OneBotAttachmentBatchError):
            await task

    asyncio.run(exercise())
    assert calls == ["first.png", "second.png"]


def test_real_image_timeout_is_ambiguous_and_does_not_auto_resend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    image = tmp_path / "image.png"
    image.write_bytes(b"png payload")

    class Bot:
        self_id = "7"
        def __init__(self):
            self.files: list[str] = []
        async def send_group_msg(self, *, group_id: int, message: Any):
            file = message["data"]["file"] if isinstance(message, dict) else message[0]["data"]["file"]
            self.files.append(file)
            if len(self.files) == 1:
                raise RuntimeError("Timeout: NTEvent sendMsg")
            return {"message_id": 88}

    bot = Bot()
    context = DeliveryContext(event_id="evt-image", attempt=3, idempotency_key="id:evt-image")
    with pytest.raises(runtime.OneBotAmbiguousAckError):
        asyncio.run(runtime._send_attachment_path(
            bot, {"type": "group", "group_id": 1}, image, send_context=context,
        ))
    assert len(bot.files) == 1
    assert not bot.files[0].startswith("base64://")
    identity = runtime.image_content_identity(image.read_bytes())
    child = context.child(identity)
    key = runtime.make_idempotency_key(
        {"type": "group", "group_id": 1}, identity,
        event_id=child.idempotency_key or child.event_id,
    )
    assert asyncio.run(runtime._outbound_idempotency.state(key)) == "ambiguous"


def test_real_pending_owner_waits_and_forward_batch_is_claimed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    gate = asyncio.Event()

    class Bot:
        self_id = "9"
        def __init__(self):
            self.send_count = 0
            self.forward_calls: list[tuple[str, dict[str, Any]]] = []
        async def send_group_msg(self, **kwargs):
            self.send_count += 1
            await gate.wait()
            return {"message_id": 9}
        async def call_api(self, name: str, **kwargs):
            self.forward_calls.append((name, kwargs))
            return {"message_id": 10}

    bot = Bot()
    target = {"type": "group", "group_id": 2}
    context = DeliveryContext(event_id="evt-owner")

    async def owner_conflict() -> None:
        first = asyncio.create_task(runtime._send_qq_message(
            bot, target, "same", send_context=context, content_identity="same",
        ))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(runtime._send_qq_message(
            bot, target, "same", send_context=context, content_identity="same",
        ))
        await asyncio.sleep(0.05)
        assert not second.done()
        gate.set()
        assert await first == "9"
        assert (await second).startswith("idempotent:")

    asyncio.run(owner_conflict())
    assert bot.send_count == 1

    first_image = tmp_path / "a.png"
    second_image = tmp_path / "b.png"
    first_image.write_bytes(b"a")
    second_image.write_bytes(b"b")
    set_live_config(runtime, enable_image_forward_merge=True, image_forward_merge_threshold=2)
    asyncio.run(runtime._send_attachments(
        bot, target, [{"path": str(first_image)}, {"path": str(second_image)}],
        send_context=DeliveryContext(event_id="evt-forward", idempotency_key="id:evt-forward"),
    ))
    assert [name for name, _ in bot.forward_calls] == ["send_group_forward_msg"]


def test_stale_idempotency_owner_cannot_commit_after_takeover(tmp_path: Path) -> None:
    async def exercise() -> None:
        path = tmp_path / "owners.sqlite3"
        first = TwoPhaseIdempotencyStore(path)
        second = TwoPhaseIdempotencyStore(path)
        stale = await first.begin("logical")
        first._db.execute("UPDATE claims SET lease_until=0 WHERE key='logical'")
        winner = await second.begin("logical")
        assert winner.acquired is True and winner.owner != stale.owner
        await second.commit(winner)
        with pytest.raises(IdempotencyOwnershipError):
            await first.commit(stale)
        assert await first.state("logical") == "sent"

    asyncio.run(exercise())


def test_image_all_fallback_failures_are_aggregated_under_same_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    image = tmp_path / "failure.png"
    image.write_bytes(b"failure image")
    begin_owners: list[str] = []
    original_begin = runtime._outbound_idempotency.begin

    async def recording_begin(key: str):
        claim = await original_begin(key)
        if claim.acquired:
            begin_owners.append(claim.owner)
        return claim

    monkeypatch.setattr(runtime._outbound_idempotency, "begin", recording_begin)

    class Bot:
        async def send_group_msg(self, **kwargs: Any):
            raise ConnectionError("connection reset")

    with pytest.raises(runtime.OneBotAttachmentBatchError) as raised:
        asyncio.run(runtime._send_attachment_path(
            Bot(), {"type": "group", "group_id": 3}, image,
            send_context=DeliveryContext(event_id="all-fail", idempotency_key="id:all-fail"),
        ))
    assert len(raised.value.errors) == 3
    assert len(begin_owners) == 1


def test_long_forward_heartbeat_prevents_cross_instance_takeover(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    runtime._outbound_idempotency.lease_seconds = 0.15
    first_image, second_image = tmp_path / "one.png", tmp_path / "two.png"
    first_image.write_bytes(b"one")
    second_image.write_bytes(b"two")

    class Bot:
        self_id = "1"
        async def call_api(self, name: str, **kwargs: Any):
            await asyncio.sleep(0.4)
            return {"message_id": 99}

    async def exercise() -> None:
        task = asyncio.create_task(runtime._try_send_images_as_forward(
            Bot(), {"type": "group", "group_id": 5},
            [{"path": str(first_image)}, {"path": str(second_image)}],
            send_context=DeliveryContext(event_id="long-forward", idempotency_key="id:long-forward"),
        ))
        await asyncio.sleep(0.25)
        row = runtime._outbound_idempotency._db.execute(
            "SELECT key,lease_until FROM claims WHERE state='pending'"
        ).fetchone()
        assert row is not None and row[1] > time.time()
        contender = TwoPhaseIdempotencyStore(runtime._outbound_idempotency.path)
        contender.lease_seconds = 0.15
        conflict = await contender.begin(row[0])
        assert conflict.acquired is False and conflict.state == "pending"
        assert await task is True

    asyncio.run(exercise())


def _merge_bot_images(tmp_path: Path) -> tuple[Path, Path]:
    first_image, second_image = tmp_path / "a.png", tmp_path / "b.png"
    first_image.write_bytes(b"a")
    second_image.write_bytes(b"b")
    return first_image, second_image


def test_forward_merge_falls_back_to_one_by_one_on_permanent_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    first_image, second_image = _merge_bot_images(tmp_path)
    set_live_config(runtime, enable_image_forward_merge=True, image_forward_merge_threshold=2)

    class Bot:
        self_id = "1"
        def __init__(self) -> None:
            self.group_sends: list[Any] = []
        async def call_api(self, name: str, **kwargs: Any):
            raise RuntimeError("retcode=1404 api not found")
        async def send_group_msg(self, *, group_id: int, message: Any):
            self.group_sends.append(message)
            return {"message_id": len(self.group_sends)}

    bot = Bot()
    asyncio.run(runtime._send_attachments(
        bot, {"type": "group", "group_id": 7},
        [{"path": str(first_image)}, {"path": str(second_image)}],
        send_context=DeliveryContext(event_id="evt-fallback", idempotency_key="id:evt-fallback"),
    ))
    assert len(bot.group_sends) == 2


def test_forward_merge_ambiguous_ack_does_not_resend_one_by_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    first_image, second_image = _merge_bot_images(tmp_path)
    set_live_config(runtime, enable_image_forward_merge=True, image_forward_merge_threshold=2)

    class Bot:
        self_id = "1"
        def __init__(self) -> None:
            self.group_sends = 0
        async def call_api(self, name: str, **kwargs: Any):
            raise RuntimeError("Timeout: NTEvent sendMsg")
        async def send_group_msg(self, **kwargs: Any):
            self.group_sends += 1
            return {"message_id": 1}

    bot = Bot()
    with pytest.raises(runtime.OneBotAmbiguousAckError):
        asyncio.run(runtime._send_attachments(
            bot, {"type": "group", "group_id": 7},
            [{"path": str(first_image)}, {"path": str(second_image)}],
            send_context=DeliveryContext(event_id="evt-amb", idempotency_key="id:evt-amb"),
        ))
    assert bot.group_sends == 0


def test_forward_card_message_id_maps_to_every_merged_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    first_image, second_image = _merge_bot_images(tmp_path)
    set_live_config(runtime, enable_image_forward_merge=True, image_forward_merge_threshold=2)

    class Bot:
        self_id = "1"
        async def call_api(self, name: str, **kwargs: Any):
            return {"message_id": 4242}

    asyncio.run(runtime._send_attachments(
        Bot(), {"type": "group", "group_id": 7},
        [{"path": str(first_image)}, {"path": str(second_image)}],
        send_context=DeliveryContext(event_id="evt-map", idempotency_key="id:evt-map"),
    ))
    records = runtime._message_attachment_records("4242", only_images=True)
    assert len(records) == 2
    assert {Path(r["path"]).name for r in records} == {"a.png", "b.png"}


def test_non_image_attachments_survive_a_failed_merge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    first_image, second_image = _merge_bot_images(tmp_path)
    blob = tmp_path / "data.bin"
    blob.write_bytes(b"blob")
    set_live_config(runtime, enable_image_forward_merge=True, image_forward_merge_threshold=2)

    class Bot:
        self_id = "1"
        def __init__(self) -> None:
            self.image_sends = 0
            self.uploads: list[str] = []
        async def call_api(self, api: str, **kwargs: Any):
            if api == "upload_group_file":
                self.uploads.append(str(kwargs.get("name") or ""))
                return {"message_id": 1}
            raise RuntimeError("retcode=1404 api not found")
        async def send_group_msg(self, **kwargs: Any):
            self.image_sends += 1
            return {"message_id": 1}

    bot = Bot()
    asyncio.run(runtime._send_attachments(
        bot, {"type": "group", "group_id": 7},
        [
            {"path": str(first_image)},
            {"path": str(second_image)},
            {"path": str(blob), "name": "data.bin"},
        ],
        send_context=DeliveryContext(event_id="evt-bin", idempotency_key="id:evt-bin"),
    ))
    assert bot.image_sends == 2
    assert bot.uploads == ["data.bin"]


def test_file_upload_crash_lease_replays_and_commits_content_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    attachment = tmp_path / "artifact.bin"
    attachment.write_bytes(b"artifact bytes")
    target = {"type": "group", "group_id": 8}
    context = DeliveryContext(event_id="file-event", idempotency_key="id:file-event")
    identity = f"file:sha256:{runtime.hashlib.sha256(attachment.read_bytes()).hexdigest()}"
    child = context.child(identity)
    key = runtime.make_idempotency_key(
        target, identity, event_id=child.idempotency_key or child.event_id,
    )

    async def abandoned_claim() -> None:
        claim = await runtime._outbound_idempotency.begin(key)
        assert claim.acquired
        runtime._outbound_idempotency._db.execute(
            "UPDATE claims SET lease_until=0 WHERE key=?", (key,),
        )

    asyncio.run(abandoned_claim())

    class Bot:
        def __init__(self):
            self.calls = 0
        async def call_api(self, api: str, **kwargs: Any):
            self.calls += 1
            assert api == "upload_group_file"
            assert kwargs["file"].startswith("base64://")
            return {"message_id": 123}

    bot = Bot()
    assert asyncio.run(runtime._send_attachment_path(
        bot, target, attachment, send_context=context,
    )) == "123"
    assert bot.calls == 1
    assert asyncio.run(runtime._outbound_idempotency.state(key)) == "sent"



def test_force_replay_context_bypasses_previous_onebot_sent_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)

    class Bot:
        def __init__(self):
            self.calls = 0
        async def send_group_msg(self, **kwargs: Any):
            self.calls += 1
            return {"message_id": self.calls}

    bot = Bot()
    target = {"type": "group", "group_id": 10}
    first = DeliveryContext(
        event_id="force-platform", idempotency_key="id:force-platform",
    )
    replay = DeliveryContext(
        event_id="force-platform", idempotency_key="id:force-platform:replay:1",
        replay_generation=1, force_replay=True,
    )
    assert asyncio.run(runtime._send_qq_message(
        bot, target, "body", send_context=first, content_identity="body",
    )) == "1"
    assert asyncio.run(runtime._send_qq_message(
        bot, target, "body", send_context=replay, content_identity="body",
    )) == "2"
    assert bot.calls == 2
    assert runtime._outbound_idempotency.pending_ttl > 240


def test_bridge_independent_identical_text_and_file_requests_are_not_permanently_deduped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    artifact = tmp_path / "same.bin"
    artifact.write_bytes(b"same file")

    class Bot:
        self_id = "42"
        def __init__(self):
            self.text_calls = 0
            self.file_calls = 0
        async def send_group_msg(self, **kwargs: Any):
            self.text_calls += 1
            return {"message_id": self.text_calls}
        async def call_api(self, api: str, **kwargs: Any):
            assert api == "upload_group_file"
            self.file_calls += 1
            return {"message_id": 100 + self.file_calls}

    bot = Bot()
    monkeypatch.setattr(runtime, "get_bot", lambda: bot)

    # Delivering to another group and sending workspace files both require an admin
    # origin, so register the session the bridge resolves the requester from.
    set_live_config(runtime, admin_users=frozenset({10001}))
    monkeypatch.setitem(
        runtime._session_targets, "sess-authz",
        {"type": "group", "group_id": 99, "user_id": 10001},
    )

    async def resolve(*args: Any, **kwargs: Any):
        return runtime.ProactiveTarget("group", 99, "Group99"), ""

    async def emojis(text: str, *args: Any, **kwargs: Any):
        return [runtime.MessageSegment.text(text)]

    monkeypatch.setattr(runtime, "_forward_bridge_resolve_target", resolve)
    monkeypatch.setattr(runtime, "process_emojis", emojis)
    monkeypatch.setattr(
        runtime, "_forward_bridge_resolve_files",
        lambda *a, **k: ([{"path": str(artifact), "name": artifact.name}], []),
    )

    async def exercise() -> None:
        first_text = {"action": "remind", "target_type": "group", "target_ref": "x", "text": "same",
                        "session_id": "sess-authz"}
        await runtime._forward_bridge_execute(first_text)
        await runtime._forward_bridge_execute(first_text)  # same request retry
        second_text = {"action": "remind", "target_type": "group", "target_ref": "x", "text": "same",
                        "session_id": "sess-authz"}
        await runtime._forward_bridge_execute(second_text)
        assert first_text["_request_identity"] != second_text["_request_identity"]

        first_file = {
            "action": "file", "target_type": "group", "target_ref": "x",
            "file_paths": [str(artifact)], "session_id": "sess-authz",
        }
        await runtime._forward_bridge_execute(first_file)
        await runtime._forward_bridge_execute(first_file)  # same request retry
        second_file = {
            "action": "file", "target_type": "group", "target_ref": "x",
            "file_paths": [str(artifact)], "session_id": "sess-authz",
        }
        await runtime._forward_bridge_execute(second_file)
        assert first_file["_request_identity"] != second_file["_request_identity"]

    asyncio.run(exercise())
    assert bot.text_calls == 2
    assert bot.file_calls == 2


def test_sent_retention_and_bound_never_delete_valid_pending(tmp_path: Path) -> None:
    now = [1_000.0]
    store = TwoPhaseIdempotencyStore(
        tmp_path / "retention.sqlite3", sent_ttl=10.0, max_items=3,
        clock=lambda: now[0],
    )

    async def exercise() -> None:
        pending = await store.begin("pending-owner")
        assert pending.acquired
        for index in range(8):
            claim = await store.begin(f"sent-{index}")
            await store.commit(claim, sent_ttl=5.0)
        assert await store.state("pending-owner") == "pending"
        sent_count = store._db.execute(
            "SELECT COUNT(*) FROM claims WHERE state='sent'"
        ).fetchone()[0]
        assert sent_count <= 2  # one valid pending row occupies the configured bound
        now[0] += 6.0
        assert await store.state("pending-owner") == "pending"
        assert store._db.execute(
            "SELECT COUNT(*) FROM claims WHERE state='sent'"
        ).fetchone()[0] == 0

    asyncio.run(exercise())


def test_context_free_fallback_uses_short_sent_retention(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    assert runtime._sent_ttl_for_context(DeliveryContext()) == runtime.ONEBOT_IDEMPOTENCY_FALLBACK_SENT_TTL_SECONDS
    assert runtime._sent_ttl_for_context(
        DeliveryContext(idempotency_key="id:durable")
    ) is None


def test_bridge_http_handler_uses_public_request_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    captured: list[dict[str, Any]] = []

    async def execute(payload: dict[str, Any]) -> dict[str, Any]:
        captured.append(dict(payload))
        return {"ok": True, "result": "sent"}

    monkeypatch.setattr(runtime, "_forward_bridge_execute", execute)

    class Request:
        def __init__(self, payload: dict[str, Any], headers: dict[str, str]):
            self._payload = payload
            self.headers = headers

        async def json(self) -> dict[str, Any]:
            return dict(self._payload)

    async def exercise() -> None:
        token = runtime._forward_bridge_token()
        response = await runtime._forward_bridge_http_handler(Request(
            {
                "op": "remind",
                "request_id": "json-logical-request",
                "_request_identity": "spoofed-private-value",
            },
            {"Idempotency-Key": "header-logical-request", "X-Forward-Token": token},
        ))
        assert response.status == 200

        response = await runtime._forward_bridge_http_handler(Request(
            {"op": "remind"},
            {"Idempotency-Key": "header-only-request", "X-Forward-Token": token},
        ))
        assert response.status == 200

    asyncio.run(exercise())
    assert captured[0]["request_id"] == "json-logical-request"
    assert captured[0]["_request_identity"] == "json-logical-request"
    assert captured[1]["request_id"] == "header-only-request"
    assert captured[1]["_request_identity"] == "header-only-request"


def test_qq_forward_http_retry_reuses_identity_and_new_call_is_distinct() -> None:
    received: list[tuple[str, str, str, dict[str, Any]]] = []
    processed: set[str] = set()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP handler contract
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            request_id = str(payload["request_id"])
            received.append((
                request_id,
                self.headers.get("Idempotency-Key", ""),
                self.headers.get("X-Request-ID", ""),
                payload,
            ))
            if request_id not in processed:
                # Bridge completed the send, but its first response was lost.
                processed.add(request_id)
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return

            response = json.dumps({"ok": True, "result": "sent"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = os.environ.copy()
        env.update({
            "ONEBOT_FORWARD_BRIDGE_HOST": "127.0.0.1",
            "ONEBOT_FORWARD_BRIDGE_PORT": str(server.server_port),
            "ONEBOT_FORWARD_HTTP_ATTEMPTS": "2",
            "ONEBOT_FORWARD_HTTP_RETRY_DELAY": "0",
            "CLONOTH_SESSION_ID": "session-test",
            "CLONOTH_TASK_ID": "task-shared-by-both-logical-calls",
            # 两端都钉死 UTF-8：子进程默认按系统 locale 输出，父进程按 locale 解码，
            # 在中文 Windows 上撞见编不出的字符就整条流报 UnicodeDecodeError。
            "PYTHONIOENCODING": "utf-8",
        })
        args = json.dumps({
            "op": "remind",
            "target_type": "self",
            "text": "identical content",
        })
        tool_path = _ROOT / "tools" / "qq_forward.py"

        for _ in range(2):
            completed = subprocess.run(
                [sys.executable, str(tool_path)],
                input=args,
                text=True,
                encoding="utf-8",
                capture_output=True,
                cwd=_ROOT,
                env=env,
                timeout=10,
                check=False,
            )
            assert completed.returncode == 0, completed.stderr or completed.stdout
            assert json.loads(completed.stdout)["ok"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert len(received) == 4
    first_id, second_id = received[0][0], received[2][0]
    assert first_id == received[1][0]
    assert second_id == received[3][0]
    assert first_id != second_id
    assert len(processed) == 2
    for request_id, idempotency_key, x_request_id, payload in received:
        assert idempotency_key == request_id
        assert x_request_id == request_id
        assert payload["request_id"] == request_id


def test_protected_send_waits_through_outer_cancellation_and_commits(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = TwoPhaseIdempotencyStore(tmp_path / "shield.sqlite3")
        claim = await store.begin("event:shield")
        started, finish = asyncio.Event(), asyncio.Event()

        async def platform_send():
            started.set()
            await finish.wait()
            return {"message_id": 91}

        task = asyncio.create_task(protected_claim_send(
            store, claim, platform_send,
            message_id_getter=lambda result: str(result["message_id"]),
        ))
        await started.wait()
        task.cancel()
        finish.set()
        assert await task == {"message_id": 91}
        assert await store.state("event:shield") == "sent"

    asyncio.run(exercise())


def test_protected_send_cancel_during_commit_still_commits(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = TwoPhaseIdempotencyStore(tmp_path / "commit-shield.sqlite3")
        claim = await store.begin("event:commit-shield")
        started, finish = asyncio.Event(), asyncio.Event()
        original_commit = store.commit

        async def delayed_commit(*args, **kwargs):
            started.set()
            await finish.wait()
            await original_commit(*args, **kwargs)

        store.commit = delayed_commit  # type: ignore[method-assign]
        task = asyncio.create_task(protected_claim_send(
            store, claim, lambda: asyncio.sleep(0, result={"message_id": 92}),
            message_id_getter=lambda result: str(result["message_id"]),
        ))
        await started.wait()
        task.cancel()
        finish.set()
        assert await task == {"message_id": 92}
        assert await store.state("event:commit-shield") == "sent"

    asyncio.run(exercise())


def test_onebot_explicit_not_started_failure_releases_claim(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = TwoPhaseIdempotencyStore(tmp_path / "not-started.sqlite3")
        claim = await store.begin("event:not-started")

        async def platform_send():
            raise OneBotSendNotStartedError("cancelled before API call")

        with pytest.raises(OneBotSendNotStartedError):
            await protected_claim_send(store, claim, platform_send)
        assert await store.state("event:not-started") is None

    asyncio.run(exercise())


def test_onebot_internal_cancel_is_ambiguous_restart_and_force_generation(tmp_path: Path) -> None:
    async def exercise() -> None:
        path = tmp_path / "ambiguous.sqlite3"
        store = TwoPhaseIdempotencyStore(path)
        claim = await store.begin("event:cancel")

        async def platform_send():
            raise asyncio.CancelledError()

        with pytest.raises(OneBotAmbiguousAckError):
            await protected_claim_send(store, claim, platform_send)
        assert await store.state("event:cancel") == "ambiguous"
        store.close()

        restarted = TwoPhaseIdempotencyStore(path)
        with pytest.raises(OneBotAmbiguousAckError):
            await restarted.begin("event:cancel")
        replay = await restarted.begin("event:cancel:replay:1")
        assert await protected_claim_send(
            restarted, replay, lambda: asyncio.sleep(0, result="message-2"),
        ) == "message-2"
        assert await restarted.state("event:cancel:replay:1") == "sent"

    asyncio.run(exercise())


def test_onebot_commit_cancel_fault_marks_message_id_ambiguous(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = TwoPhaseIdempotencyStore(tmp_path / "commit-cancel.sqlite3")
        claim = await store.begin("event:commit-cancel")

        async def cancelled_commit(*args, **kwargs):
            raise asyncio.CancelledError()

        store.commit = cancelled_commit  # type: ignore[method-assign]
        with pytest.raises(OneBotAmbiguousAckError):
            await protected_claim_send(
                store, claim, lambda: asyncio.sleep(0, result={"message_id": 93}),
                message_id_getter=lambda result: str(result["message_id"]),
            )
        row = store._db.execute(
            "SELECT state,platform_message_id FROM claims WHERE key='event:commit-cancel'"
        ).fetchone()
        assert row == ("ambiguous", "93")

    asyncio.run(exercise())


class _Clock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def _dead_letter(store: Any, key: str, error: str = "timeout waiting for ack") -> None:
    claim = await store.begin(key)
    await store.mark_ambiguous(claim, error=error)


def test_a_dead_letter_survives_inside_its_ttl(tmp_path: Path) -> None:
    async def exercise() -> None:
        clock = _Clock()
        store = TwoPhaseIdempotencyStore(tmp_path / "c.sqlite3", ambiguous_ttl=60, clock=clock)
        await _dead_letter(store, "content:group:1:d")
        clock.advance(59)
        with pytest.raises(OneBotAmbiguousAckError):
            await store.begin("content:group:1:d")

    asyncio.run(exercise())


def test_a_dead_letter_is_released_after_its_ttl(tmp_path: Path) -> None:
    async def exercise() -> None:
        clock = _Clock()
        store = TwoPhaseIdempotencyStore(tmp_path / "c.sqlite3", ambiguous_ttl=60, clock=clock)
        await _dead_letter(store, "content:group:1:d")
        clock.advance(61)
        released = await store.begin("content:group:1:d")
        assert released.acquired is True
        assert released.state == "pending"

    asyncio.run(exercise())


def test_a_zero_ttl_keeps_a_dead_letter_forever(tmp_path: Path) -> None:
    async def exercise() -> None:
        clock = _Clock()
        store = TwoPhaseIdempotencyStore(tmp_path / "c.sqlite3", ambiguous_ttl=0, clock=clock)
        await _dead_letter(store, "content:group:1:d")
        clock.advance(10 * 365 * 24 * 3600)
        with pytest.raises(OneBotAmbiguousAckError):
            await store.begin("content:group:1:d")

    asyncio.run(exercise())


def test_clearing_a_dead_letter_unblocks_the_key(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = TwoPhaseIdempotencyStore(tmp_path / "c.sqlite3", ambiguous_ttl=0)
        await _dead_letter(store, "content:group:1:d")
        assert await store.clear_ambiguous("content:group:1:d") is True
        reclaimed = await store.begin("content:group:1:d")
        assert reclaimed.acquired is True

    asyncio.run(exercise())


def test_clearing_refuses_a_sent_claim(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = TwoPhaseIdempotencyStore(tmp_path / "c.sqlite3")
        claim = await store.begin("event:sent-key")
        await store.commit(claim)
        assert await store.clear_ambiguous("event:sent-key") is False
        assert await store.state("event:sent-key") == "sent"

    asyncio.run(exercise())


def test_clearing_all_reports_how_many_were_released(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = TwoPhaseIdempotencyStore(tmp_path / "c.sqlite3", ambiguous_ttl=0)
        for i in range(3):
            await _dead_letter(store, f"content:group:1:{i}")
        sent = await store.begin("event:ok")
        await store.commit(sent)
        assert await store.clear_all_ambiguous() == 3
        assert await store.state("event:ok") == "sent"

    asyncio.run(exercise())


def test_the_dead_letter_list_carries_handle_target_and_error(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = TwoPhaseIdempotencyStore(tmp_path / "c.sqlite3", ambiguous_ttl=0)
        await _dead_letter(store, "content:group:12:deadbeef", error="boom detail")
        claims = await store.ambiguous_claims()
        assert len(claims) == 1
        entry = claims[0]
        assert len(entry.handle) == 8
        assert all(ch in "0123456789abcdef" for ch in entry.handle)
        assert "boom detail" in entry.last_error

    asyncio.run(exercise())


def test_the_target_is_recovered_from_content_and_event_keys() -> None:
    assert target_from_idempotency_key("content:group:12:deadbeef") == "group:12"
    assert target_from_idempotency_key("event:outbound_message:id:e1:group:12") == "group:12"
    assert target_from_idempotency_key("content:private:42:d") == "private:42"
    assert target_from_idempotency_key("event:xxx") == ""
    assert target_from_idempotency_key("request:scope:digest") == ""


def test_an_expired_dead_letter_is_not_listed(tmp_path: Path) -> None:
    async def exercise() -> None:
        clock = _Clock()
        store = TwoPhaseIdempotencyStore(tmp_path / "c.sqlite3", ambiguous_ttl=60, clock=clock)
        await _dead_letter(store, "content:group:1:d")
        clock.advance(61)
        assert await store.ambiguous_claims() == []
        assert await store.ambiguous_count() == 0

    asyncio.run(exercise())


def _ops_admin() -> Any:
    return types.SimpleNamespace(user_id=777, message_id=1, sender=types.SimpleNamespace(role=""))


def test_the_ops_command_lists_and_releases(monkeypatch: Any, tmp_path: Path) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    set_live_config(runtime, admin_users=frozenset({777}))
    admin = _ops_admin()

    async def exercise() -> None:
        store = runtime._outbound_idempotency
        assert await runtime._maybe_handle_dead_letter_command(
            event=admin, user_text="/发送死信",
        ) == "当前没有卡住的投递记录。"

        for key in ("content:group:10:aaa", "content:group:20:bbb"):
            await _dead_letter(store, key)

        by_key = {c.key: c.handle for c in await store.ambiguous_claims(limit=0)}
        listing = await runtime._maybe_handle_dead_letter_command(event=admin, user_text="/发送死信")
        assert by_key["content:group:10:aaa"] in listing
        assert by_key["content:group:20:bbb"] in listing
        assert "可能已经收到过" in listing

        released = await runtime._maybe_handle_dead_letter_command(
            event=admin, user_text=f"/发送死信 清理 {by_key['content:group:10:aaa']}",
        )
        assert "已放行" in released
        assert await store.state("content:group:10:aaa") is None
        assert await store.state("content:group:20:bbb") == "ambiguous"

        cleared_all = await runtime._maybe_handle_dead_letter_command(
            event=admin, user_text="/发送死信 清理 全部",
        )
        assert "已放行" in cleared_all
        assert await store.ambiguous_count() == 0

    asyncio.run(exercise())


def test_the_ops_command_rejects_a_nonunique_handle_prefix(monkeypatch: Any, tmp_path: Path) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    set_live_config(runtime, admin_users=frozenset({777}))
    admin = _ops_admin()

    async def exercise() -> None:
        store = runtime._outbound_idempotency
        # 确定性搜索两个首字符相同的 handle，用 1 位前缀触发歧义。
        seen: dict[str, str] = {}
        collision: str | None = None
        for i in range(1000):
            key = f"content:group:1:{i}"
            head = hashlib.sha256(key.encode("utf-8")).hexdigest()[0]
            if head in seen:
                await _dead_letter(store, seen[head])
                await _dead_letter(store, key)
                collision = head
                break
            seen[head] = key
        assert collision is not None
        reply = await runtime._maybe_handle_dead_letter_command(
            event=admin, user_text=f"/发送死信 清理 {collision}",
        )
        assert reply == "编号不唯一，请多输几位。"

    asyncio.run(exercise())


def test_the_ops_command_shows_usage_for_a_trailing_word(monkeypatch: Any, tmp_path: Path) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    set_live_config(runtime, admin_users=frozenset({777}))

    reply = asyncio.run(
        runtime._maybe_handle_dead_letter_command(event=_ops_admin(), user_text="/发送死信吧")
    )
    assert reply is not None
    assert "用法" in reply
    assert "没有这个编号" not in reply


def test_attachment_send_locks_are_bounded_and_keep_a_busy_bucket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    order: list[str] = []
    holder_entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_busy() -> None:
        async with runtime._attachment_send_order({"type": "group", "group_id": 1}):
            order.append("holder")
            holder_entered.set()
            await release.wait()

    async def waiter() -> None:
        async with runtime._attachment_send_order({"type": "group", "group_id": 1}):
            order.append("waiter")

    async def exercise() -> None:
        holder = asyncio.create_task(hold_busy())
        await holder_entered.wait()
        follower = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        for i in range(runtime._ATTACHMENT_SEND_LOCK_MAX_KEYS + 20):
            async with runtime._attachment_send_order({"type": "group", "group_id": 1000 + i}):
                pass
        assert not follower.done()
        assert "group:1" in runtime._attachment_send_locks
        assert len(runtime._attachment_send_locks) <= runtime._ATTACHMENT_SEND_LOCK_MAX_KEYS + 1
        release.set()
        await holder
        await follower

    asyncio.run(exercise())
    assert order == ["holder", "waiter"]


def test_custom_face_cache_is_keyed_by_account_and_drops_expired_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _EMOJI_MODULE._custom_face_cache.clear()

    class Bot:
        def __init__(self, self_id: str) -> None:
            self.self_id = self_id
            self.calls = 0

        async def call_api(self, name: str, **kwargs: Any) -> Any:
            self.calls += 1
            return [{"emojiId": "e1"}]

    bot_a = Bot("10001")
    bot_b = Bot("10001")

    async def exercise() -> None:
        await _EMOJI_MODULE.fetch_custom_face_details(bot_a)
        await _EMOJI_MODULE.fetch_custom_face_details(bot_b)
        assert bot_a.calls == 1
        assert bot_b.calls == 0

        _EMOJI_MODULE.invalidate_custom_face_cache(bot_b)
        await _EMOJI_MODULE.fetch_custom_face_details(bot_a)
        assert bot_a.calls == 2

        _EMOJI_MODULE._custom_face_cache["10001"] = (0.0, [])
        monkeypatch.setattr(_EMOJI_MODULE, "_CUSTOM_FACE_CACHE_TTL", 0)
        bot_c = Bot("10002")
        await _EMOJI_MODULE.fetch_custom_face_details(bot_c)
        assert "10001" not in _EMOJI_MODULE._custom_face_cache
        assert len(_EMOJI_MODULE._custom_face_cache) == 1

    asyncio.run(exercise())


def test_group_member_cache_drops_expired_groups(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime, "_GROUP_MEMBER_CACHE_TTL", 0)

    class Bot:
        async def call_api(self, name: str, **kwargs: Any) -> Any:
            return [{"user_id": 1, "card": "", "nickname": "n"}]

    bot = Bot()

    async def exercise() -> None:
        await runtime._load_group_members(bot, 1)
        await runtime._load_group_members(bot, 2)

    asyncio.run(exercise())
    assert len(runtime._group_member_cache) == 1


def test_conversation_buckets_are_bounded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = _load_runtime(monkeypatch, tmp_path)
    max_keys = runtime._CONVERSATION_BUCKET_MAX_KEYS
    event = types.SimpleNamespace(user_id=1)

    for i in range(max_keys + 5):
        runtime._remember_recent_images(
            f"conv:{i}", event, [{"type": "image", "path": f"/tmp/x{i}.png"}],
        )

    assert len(runtime._recent_images) == max_keys
    assert f"conv:{max_keys + 4}" in runtime._recent_images
    assert "conv:0" not in runtime._recent_images
