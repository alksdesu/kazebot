"""共享的发送防重：目标身份和失败分类由平台注入，键格式和死信事件名跟着平台走。"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adapters._shared.send_guard import (  # noqa: E402
    SendError,
    SendNotStartedError,
    TwoPhaseIdempotencyStore,
    idempotency_key,
    protected_claim_send,
    target_key_of,
)


def _room(target: Mapping[str, Any]) -> str:
    return f"room:{target['room']}"


def _store(path: Path, **options: Any) -> TwoPhaseIdempotencyStore:
    return TwoPhaseIdempotencyStore(path, target_identity=_room, **options)


async def _plan_body() -> dict[str, Any]:
    return {
        "processed_segments": [{"type": "text", "content": "synthetic body"}],
        "send_identity": "synthetic-send", "selected_images": [], "conversation_key": "synthetic-conversation",
    }


class TestKeys:
    def test_an_event_key_round_trips_its_target(self) -> None:
        key = idempotency_key("room:abc", "ignored", event_id="evt-1")

        assert key == "event:evt-1:room:abc"
        assert target_key_of(key) == "room:abc"

    def test_a_content_key_round_trips_its_target(self) -> None:
        key = idempotency_key("room:abc", "hello")

        assert key.startswith("content:room:abc:")
        assert target_key_of(key) == "room:abc"

    def test_the_content_digest_separates_targets_but_not_repeats(self) -> None:
        assert idempotency_key("room:a", "x") == idempotency_key("room:a", "x")
        assert idempotency_key("room:a", "x") != idempotency_key("room:b", "x")


class TestPlatformBinding:
    def test_the_store_cannot_be_opened_without_a_target_identity(self, tmp_path: Path) -> None:
        with pytest.raises(TypeError):
            TwoPhaseIdempotencyStore(tmp_path / "unbound.sqlite3")  # type: ignore[call-arg]

    def test_message_plans_use_the_injected_identity(self, tmp_path: Path) -> None:
        store = _store(tmp_path / "plans.sqlite3")
        try:
            plan = asyncio.run(store.get_or_create_message_plan("plan-1", {"room": "abc"}, _plan_body))
        finally:
            store.close()

        assert plan.target == "room:abc"
        assert plan.claim_key == f"event:message-plan:{plan.delivery_id}:room:abc"

    def test_dead_letter_events_carry_the_platform_namespace(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        now = [1000.0]
        store = _store(tmp_path / "dead.sqlite3", ambiguous_ttl=10, clock=lambda: now[0], log_namespace="chat")

        async def scenario() -> None:
            claim = await store.begin("event:e1:room:abc")
            await store.mark_ambiguous(claim, error="synthetic unknown acknowledgement")
            now[0] += 11
            with caplog.at_level(logging.INFO, logger="adapters._shared.send_guard"):
                await store.ambiguous_count()

        try:
            asyncio.run(scenario())
        finally:
            store.close()

        assert "chat_dead_letter_expired count=1" in caplog.text


class TestProtectedSend:
    def test_the_platform_classifier_decides_to_release_the_claim(self, tmp_path: Path) -> None:
        store = _store(tmp_path / "send.sqlite3")
        seen: list[BaseException] = []

        def classify(exc: BaseException) -> SendError:
            seen.append(exc)
            return SendNotStartedError(f"synthetic not started: {exc}", cause=exc)

        async def failing_send() -> None:
            raise RuntimeError("synthetic platform failure")

        async def scenario() -> str | None:
            claim = await store.begin("event:e2:room:abc")
            with pytest.raises(SendNotStartedError):
                await protected_claim_send(store, claim, failing_send, classify=classify)
            return await store.state(claim.key)

        try:
            state = asyncio.run(scenario())
        finally:
            store.close()

        assert state is None
        assert [str(exc) for exc in seen] == ["synthetic platform failure"]

    def test_a_classifier_must_be_given(self, tmp_path: Path) -> None:
        # 漏传分类器不能悄悄退回某个默认：哪些失败能重试只有平台自己知道。
        store = _store(tmp_path / "unclassified.sqlite3")

        async def send() -> None:
            return None

        try:
            claim = asyncio.run(store.begin("event:e3:room:abc"))
            with pytest.raises(TypeError):
                protected_claim_send(store, claim, send)  # type: ignore[call-arg]
        finally:
            store.close()
