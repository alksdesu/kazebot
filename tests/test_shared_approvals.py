"""共享层的审批流转：挂起、卡片锚点、定案、超时、原子领取、按 ID 查找，以及决策词。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adapters._shared import approvals  # noqa: E402
from adapters._shared.approvals import ApprovalBook, ApprovalReplyTarget  # noqa: E402
from platforms.descriptor import ID_NUMERIC, ID_STRING  # noqa: E402

AID = "12345678-1234-1234-1234-1234567890ab"
A1 = "aaaaaaaa-1111-1111-1111-111111111111"
A2 = "bbbbbbbb-2222-2222-2222-222222222222"
TTL = 100.0


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _ref(value: Any) -> str:
    return str(value).strip() if value is not None else ""


@pytest.fixture()
def clock() -> _Clock:
    return _Clock()


@pytest.fixture()
def book(clock: _Clock) -> ApprovalBook:
    return ApprovalBook(id_shape=ID_NUMERIC, normalize_ref=_ref, ttl_seconds=TTL, clock=clock)


def _open(book: ApprovalBook, approval_id: str = AID, operation: str = "read_file") -> None:
    book.open(approval_id, operation=operation, details={"path": "a.txt"}, conversation_key="conv", session_id="s1")


class TestVerbs:
    @pytest.mark.parametrize(("text", "expected"), [
        ("同意", "allow"), ("审批同意", "allow"), ("同意。", "allow"),
        ("ok", "allow"), ("OK", "allow"), ("审批：同意", "allow"),
        ("拒绝", "deny"), ("不同意", "deny"), ("审批 拒绝", "deny"),
        ("know", None), ("I don't know", None), ("note", None),
        ("now", None), ("nothing", None), ("no problem", None),
        ("okay 了", None), ("同意吧，快点", None), ("", None),
    ])
    def test_a_reply_verb_is_a_whole_word(self, text: str, expected: Any) -> None:
        assert approvals.parse_reply_verb(text) == expected

    def test_one_table_feeds_both_entries(self) -> None:
        for verb in approvals.ALLOW_VERBS:
            assert approvals.approval_verb(verb) == approvals.DECISION_ALLOW
            assert approvals.parse_command(f"审批 {verb} {AID}") == ("allow", AID)
        for verb in approvals.DENY_VERBS:
            assert approvals.approval_verb(verb) == approvals.DECISION_DENY
            assert approvals.parse_command(f"审批 {verb} {AID}") == ("deny", AID)
        assert approvals.ALLOW_VERBS & approvals.DENY_VERBS == frozenset()

    def test_a_bare_verb_command_needs_an_id_shaped_token(self) -> None:
        assert approvals.parse_command("通过 微信发给我") is None
        assert approvals.parse_command(f"同意 {AID[:8]}") == ("allow", AID[:8])
        assert approvals.parse_command(f"审批  同意\t{AID}") == ("allow", AID)

    @pytest.mark.parametrize("body", ["", "同意", "审批 同意", "随便 聊聊"])
    def test_incomplete_or_unrelated_bodies_are_not_commands(self, body: str) -> None:
        assert approvals.parse_command(body) is None


class TestPendingAndSettled:
    def test_open_records_the_request(self, book: ApprovalBook, clock: _Clock) -> None:
        _open(book)

        assert book.pending[AID] == {
            "operation": "read_file", "details": {"path": "a.txt"},
            "conversation_key": "conv", "session_id": "s1", "created_at": clock.now,
        }

    def test_close_moves_a_request_to_settled(self, book: ApprovalBook) -> None:
        _open(book)

        info = book.close(AID, decision=approvals.DECISION_DENY)

        assert info is not None and info["operation"] == "read_file"
        assert AID not in book.pending
        assert book.settled[AID]["decision"] == "deny"
        assert book.settled[AID]["operation"] == "read_file"
        # 不是人作的决定，没有管理员可记。
        assert book.settled[AID]["admin_id"] == ""

    def test_admin_ids_are_normalized_to_the_id_shape(self, book: ApprovalBook) -> None:
        book.settle(AID, decision="allow", admin_id=10001)

        assert book.settled[AID]["admin_id"] == "10001"

    def test_expired_requests_turn_into_expired_settlements(self, book: ApprovalBook, clock: _Clock) -> None:
        _open(book, A1)
        clock.now += TTL / 2
        _open(book, A2)
        clock.now += TTL / 2 + 1

        book.prune_expired()

        assert list(book.pending) == [A2]
        assert book.settled[A1]["decision"] == approvals.DECISION_EXPIRED

    def test_a_request_without_a_timestamp_never_expires(self, book: ApprovalBook, clock: _Clock) -> None:
        book.pending[AID] = {"operation": "read_file"}
        clock.now += TTL * 10

        book.prune_expired()

        assert AID in book.pending

    def test_the_settled_log_is_bounded(self, book: ApprovalBook) -> None:
        for index in range(210):
            book.settle(f"id-{index}", decision="allow")

        assert len(book.settled) == 200
        assert "id-0" not in book.settled and "id-209" in book.settled


class TestCards:
    def test_a_card_resolves_only_for_its_recipient(self, book: ApprovalBook) -> None:
        _open(book)
        book.remember_card(" 555 ", AID, 10001)

        assert book.resolve_card(555, "10001") == ApprovalReplyTarget(approval_id=AID, from_card=True)
        assert book.resolve_card(555, 10002).matched() is False
        assert book.resolve_card(556, 10001).matched() is False

    def test_a_card_for_a_settled_request_reports_it(self, book: ApprovalBook) -> None:
        _open(book, A1)
        _open(book, A2)
        book.remember_card(102, A1, 10002)
        book.remember_card(202, A2, 10002)
        book.close(A1, decision="allow", admin_id=10001)

        target = book.resolve_card(102, 10002)

        assert target == ApprovalReplyTarget(settled_id=A1, from_card=True)

    def test_unusable_cards_are_not_remembered(self, book: ApprovalBook) -> None:
        book.remember_card("", AID, 10001)
        book.remember_card(1, "", 10001)
        book.remember_card(2, AID, "not-a-number")

        assert not book.cards

    def test_the_card_map_is_bounded(self, book: ApprovalBook) -> None:
        for index in range(510):
            book.remember_card(index, AID, 10001)

        assert len(book.cards) == 500
        assert "0" not in book.cards and "509" in book.cards

    def test_recipients_and_unique_pending(self, book: ApprovalBook) -> None:
        _open(book)
        book.remember_card(1, AID, 10001)

        assert book.card_recipients(AID) == {"10001"}
        assert book.unique_pending_for(10001) == AID
        assert book.unique_pending_for(10002) == ""
        assert book.pending_count_for(10001) == 1

        _open(book, A2)
        book.remember_card(2, A2, 10001)

        assert book.unique_pending_for(10001) == ""
        assert book.pending_count_for(10001) == 2

    def test_string_ids_work_for_string_platforms(self, clock: _Clock) -> None:
        book = ApprovalBook(id_shape=ID_STRING, normalize_ref=_ref, ttl_seconds=TTL, clock=clock)
        _open(book)
        book.remember_card("msg-1", AID, "U024BE7LH")

        assert book.resolve_card("msg-1", "U024BE7LH").approval_id == AID


class TestClaim:
    def test_only_one_concurrent_claim_wins(self, book: ApprovalBook) -> None:
        _open(book)

        async def scenario() -> list[Any]:
            return list(await asyncio.gather(
                book.claim(AID, 10001, "allow"), book.claim(AID, 10002, "deny"),
            ))

        first, second = asyncio.run(scenario())

        assert first is not None and second is None
        assert book.settled[AID]["admin_id"] == "10001"
        assert book.settled[AID]["decision"] == "allow"

    def test_release_puts_the_request_back(self, book: ApprovalBook) -> None:
        _open(book)

        async def scenario() -> None:
            info = await book.claim(AID, 10001, "allow")
            assert info is not None
            await book.release(AID, info)

        asyncio.run(scenario())

        assert AID in book.pending and AID not in book.settled

    def test_an_expired_request_cannot_be_claimed(self, book: ApprovalBook, clock: _Clock) -> None:
        _open(book)
        clock.now += TTL + 1

        assert asyncio.run(book.claim(AID, 10001, "allow")) is None
        assert book.settled[AID]["decision"] == approvals.DECISION_EXPIRED


class TestLookup:
    def test_full_id_and_unique_prefix(self, book: ApprovalBook) -> None:
        _open(book)

        assert book.lookup(f" {AID} ") == approvals.ApprovalLookup(approvals.LOOKUP_FOUND, AID, AID)
        assert book.lookup(AID[:8]).approval_id == AID

    def test_empty_ambiguous_settled_and_unknown(self, book: ApprovalBook) -> None:
        _open(book, "abcd0000-1111")
        _open(book, "abcd0000-2222")
        book.settle(A1, decision="allow")

        assert book.lookup("  ").outcome == approvals.LOOKUP_NO_TOKEN
        assert book.lookup("abcd").outcome == approvals.LOOKUP_AMBIGUOUS
        assert book.lookup(A1[:8]) == approvals.ApprovalLookup(approvals.LOOKUP_SETTLED, A1[:8], A1)
        assert book.lookup("ffff").outcome == approvals.LOOKUP_UNKNOWN

    def test_lookup_reports_a_request_that_just_expired(self, book: ApprovalBook, clock: _Clock) -> None:
        _open(book)
        clock.now += TTL + 1

        lookup = book.lookup(AID[:8])

        assert lookup.outcome == approvals.LOOKUP_SETTLED
        assert book.settled[lookup.approval_id]["decision"] == approvals.DECISION_EXPIRED

    def test_known_ids_lists_pending_before_settled(self, book: ApprovalBook) -> None:
        book.settle(A1, decision="allow")
        _open(book, A2)

        assert book.known_ids() == [A2, A1]
