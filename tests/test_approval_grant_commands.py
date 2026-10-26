"""审批回复里的授权时长和收回命令：写法只认整数加单位或「永久」，拒绝不看时长，说不清的一律不当审批。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adapters._shared import approvals  # noqa: E402
from adapters._shared.approvals import ApprovalIntent  # noqa: E402

AID = "12345678-1234-1234-1234-1234567890ab"
ALLOW = approvals.DECISION_ALLOW
DENY = approvals.DECISION_DENY


class TestGrantMinutes:
    @pytest.mark.parametrize(("text", "minutes"), [
        ("2h", 120), ("2H", 120), ("2 h", 120), ("30m", 30), ("1d", 1440), ("3天", 4320),
        ("45分钟", 45), ("45 分钟", 45), ("10分", 10), ("2小时", 120),
        # 秒向上取整：「30s」不能变成不授权。
        ("90s", 2), ("30秒", 1), ("1s", 1), ("60s", 1),
        (" 2h ", 120), ("2h。", 120), ("2h!", 120),
        ("单次", 0), ("once", 0), ("ONCE", 0), ("0m", 0),
    ])
    def test_whole_numbers_with_one_unit(self, text: str, minutes: int) -> None:
        assert approvals.grant_minutes_of(text) == minutes

    @pytest.mark.parametrize("text", [
        "", "2", "h", "1.5h", ".5h", "-2h", "+2h", "一小时", "两小时", "1h30m", "2h 3m",
        "1 0m", "2hours", "2hh", "2周", "２h", "1234567890m", "2h，好的", "好的", "单次吧",
    ])
    def test_anything_else_is_not_a_duration(self, text: str) -> None:
        assert approvals.grant_minutes_of(text) is None

    @pytest.mark.parametrize("text", ["永久", "permanent", "forever"])
    def test_permanent_is_not_a_number_of_minutes(self, text: str) -> None:
        assert approvals.grant_minutes_of(text) is None


class TestGrantAsk:
    @pytest.mark.parametrize("text", ["永久", " 永久 ", "永久。", "permanent", "PERMANENT", "forever", "Forever!"])
    def test_the_permanent_spellings(self, text: str) -> None:
        assert approvals.grant_ask_of(text) == approvals.GrantAsk(permanent=True)

    @pytest.mark.parametrize(("text", "minutes"), [("2h", 120), ("45 分钟", 45), ("单次", 0), ("once", 0)])
    def test_durations_read_as_before(self, text: str, minutes: int) -> None:
        assert approvals.grant_ask_of(text) == approvals.GrantAsk(minutes=minutes)

    @pytest.mark.parametrize("text", ["", "永远", "永久吧", "永久 2h", "2h 永久", "一直", "always", "永 久"])
    def test_anything_else_asks_for_nothing(self, text: str) -> None:
        assert approvals.grant_ask_of(text) is None


class TestTheDecisionCommand:
    @pytest.mark.parametrize(("body", "intent"), [
        (f"审批 同意 {AID}", ApprovalIntent(ALLOW, AID)),
        (f"审批 同意 {AID} 2h", ApprovalIntent(ALLOW, AID, 120)),
        (f"审批 同意 {AID} 45 分钟", ApprovalIntent(ALLOW, AID, 45)),
        (f"审批 同意 {AID} 单次", ApprovalIntent(ALLOW, AID, 0)),
        (f"审批  同意\t{AID}\t30m", ApprovalIntent(ALLOW, AID, 30)),
        (f"同意 {AID[:8]} 30m", ApprovalIntent(ALLOW, AID[:8], 30)),
        (f"approval approve {AID} 1d", ApprovalIntent(ALLOW, AID, 1440)),
        (f"批准 {AID} once", ApprovalIntent(ALLOW, AID, 0)),
        (f"审批 同意 {AID} 永久", ApprovalIntent(ALLOW, AID, grant_permanent=True)),
        (f"同意 {AID[:8]} forever", ApprovalIntent(ALLOW, AID[:8], grant_permanent=True)),
        (f"approval approve {AID} permanent", ApprovalIntent(ALLOW, AID, grant_permanent=True)),
    ])
    def test_a_duration_follows_the_id(self, body: str, intent: ApprovalIntent) -> None:
        assert approvals.parse_decision_command(body) == intent

    @pytest.mark.parametrize("tail", ["2h", "随便写", "1.5h", "单次", "永久"])
    def test_a_deny_never_reads_a_duration(self, tail: str) -> None:
        assert approvals.parse_decision_command(f"审批 拒绝 {AID} {tail}") == ApprovalIntent(DENY, AID)

    @pytest.mark.parametrize("tail", ["好的", "1.5h", "2h 3m", "一小时", "1h30m", "2h 吧", "永久 2h", "永久吧"])
    def test_a_tail_that_is_not_a_duration_is_an_error(self, tail: str) -> None:
        # 写错的时长不能悄悄按默认时长批掉：拼错一个字就多给了半小时免审。
        intent = approvals.parse_decision_command(f"审批 同意 {AID} {tail}")

        assert intent == ApprovalIntent(ALLOW, AID, duration_error=True)

    @pytest.mark.parametrize("body", [
        "审批 同意 2h", "审批 同意 45 分钟", "审批 同意 单次", "approval approve 30m", "审批 同意 永久", "approval approve forever",
    ])
    def test_a_duration_without_an_id_names_no_approval(self, body: str) -> None:
        # 交给裸动词的唯一匹配：只有本人恰好一条待审批时才批。
        assert approvals.parse_decision_command(body) is None

    @pytest.mark.parametrize("token", ["300d", "1d", "45"])
    def test_a_token_that_could_be_an_id_is_taken_as_one(self, token: str) -> None:
        assert approvals.parse_decision_command(f"审批 同意 {token}") == ApprovalIntent(ALLOW, token)

    @pytest.mark.parametrize("body", ["同意 2h", "同意 45 分钟", "通过 微信发给我", "同意", "审批 同意", "", "随便 聊聊", "同意 永久"])
    def test_a_bare_verb_still_needs_an_id_shaped_token(self, body: str) -> None:
        assert approvals.parse_decision_command(body) is None

    def test_the_old_parser_keeps_ignoring_the_tail(self) -> None:
        # 不认时长的平台照旧拿 parse_command，ID 后面写什么都不影响它。
        assert approvals.parse_command(f"审批 同意 {AID} 2h") == ("allow", AID)
        assert approvals.parse_command(f"审批 同意 {AID} 好的") == ("allow", AID)
        assert approvals.parse_command("审批 同意 2h") == ("allow", "2h")


class TestTheReplyDecision:
    @pytest.mark.parametrize(("text", "intent"), [
        ("同意", ApprovalIntent(ALLOW)),
        ("同意。", ApprovalIntent(ALLOW)),
        ("拒绝", ApprovalIntent(DENY)),
        ("不同意", ApprovalIntent(DENY)),
        ("审批 拒绝", ApprovalIntent(DENY)),
        ("同意 1h", ApprovalIntent(ALLOW, grant_minutes=60)),
        ("同意1h", ApprovalIntent(ALLOW, grant_minutes=60)),
        ("同意 2 h", ApprovalIntent(ALLOW, grant_minutes=120)),
        ("审批 同意 45 分钟", ApprovalIntent(ALLOW, grant_minutes=45)),
        ("审批：同意 2h", ApprovalIntent(ALLOW, grant_minutes=120)),
        ("审批同意2h", ApprovalIntent(ALLOW, grant_minutes=120)),
        ("同意 单次", ApprovalIntent(ALLOW, grant_minutes=0)),
        ("同意单次", ApprovalIntent(ALLOW, grant_minutes=0)),
        ("approve 30m", ApprovalIntent(ALLOW, grant_minutes=30)),
        ("approved 30m", ApprovalIntent(ALLOW, grant_minutes=30)),
        ("approval approve 1d", ApprovalIntent(ALLOW, grant_minutes=1440)),
        ("Approve 2H", ApprovalIntent(ALLOW, grant_minutes=120)),
        ("同意 2h。", ApprovalIntent(ALLOW, grant_minutes=120)),
        ("同意 永久", ApprovalIntent(ALLOW, grant_permanent=True)),
        ("同意永久", ApprovalIntent(ALLOW, grant_permanent=True)),
        ("同意 永久。", ApprovalIntent(ALLOW, grant_permanent=True)),
        ("审批 同意 永久", ApprovalIntent(ALLOW, grant_permanent=True)),
        ("审批：同意 永久", ApprovalIntent(ALLOW, grant_permanent=True)),
        ("批准 forever", ApprovalIntent(ALLOW, grant_permanent=True)),
        ("approve PERMANENT", ApprovalIntent(ALLOW, grant_permanent=True)),
    ])
    def test_a_verb_with_an_optional_duration(self, text: str, intent: ApprovalIntent) -> None:
        assert approvals.parse_reply_decision(text) == intent

    @pytest.mark.parametrize("text", [
        "同意吧，快点", "同意 1.5h", "同意 一小时", "同意 2h 吧", "同意 1 0m", "同意 -2h", "同意 ２h",
        "拒绝 2h", "不同意 2h", "驳回 单次", "ok 2h", "2h", "单次", "同意 2h 3m", "我同意 2h",
        "ok", "yes", "know", "",
        "永久", "拒绝 永久", "驳回 forever", "同意 永久吧", "同意 永久 2h", "同意 永远", "我同意 永久", "同意 永 久",
    ])
    def test_anything_else_stays_ordinary_chat(self, text: str) -> None:
        assert approvals.parse_reply_decision(text) is None

    @pytest.mark.parametrize("text", ["同意", "拒绝", "审批 同意", "approve", "同意吧，快点", "ok"])
    def test_a_bare_verb_reads_the_same_as_before(self, text: str) -> None:
        intent = approvals.parse_reply_decision(text)

        assert (intent.decision if intent is not None else None) == approvals.parse_reply_verb(text)


class TestTheRevokeCommand:
    @pytest.mark.parametrize("body", [
        "审批 收回", "审批 收回授权", "approval revoke", "approval REVOKE", "审批  收回", "审批 收回。", "审批\t收回授权",
    ])
    def test_the_accepted_spellings(self, body: str) -> None:
        assert approvals.parse_revoke_command(body) is True

    @pytest.mark.parametrize("body", [
        "", "收回", "revoke", "审批收回", "审批 收回 全部", "审批 撤回", "审批 收回 g-1", "审批 同意 收回", "approval revoke all",
    ])
    def test_anything_else_is_not_a_revocation(self, body: str) -> None:
        assert approvals.parse_revoke_command(body) is False


def test_the_once_words_are_not_approval_verbs() -> None:
    # 「单次」「永久」单独发出来不能被当成同意。
    for word in approvals.GRANT_ONCE_WORDS | approvals.GRANT_PERMANENT_WORDS | approvals.REVOKE_WORDS:
        assert approvals.approval_verb(word) is None


def test_the_intent_defaults_to_the_policy_duration() -> None:
    intent: Any = ApprovalIntent(ALLOW, AID)

    assert (intent.grant_minutes, intent.duration_error, intent.grant_permanent) == (None, False, False)
