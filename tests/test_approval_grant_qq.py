"""QQ 私聊里的审批授权：三条审批入口带上时长和审批人，回执写清授权到几点、怎么收回，/审批 收回 只给有审批权的人。"""
from __future__ import annotations

import asyncio
import itertools
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _onebot_harness import load_runtime, set_live_config  # noqa: E402
from clonoth_sdk import types as sdk_types  # noqa: E402
from clonoth_sdk.types import (  # noqa: E402
    APPROVAL_ALLOWED,
    APPROVAL_DENIED,
    GRANT_NOTE_DISABLED,
    GRANT_NOTE_NO_APPROVER,
    GRANT_NOTE_NO_REQUESTER,
    GRANT_NOTE_NOT_REQUESTER,
    GRANT_NOTE_ONCE,
    ApprovalDecision,
    ApprovalGrant,
)
from supervisor.approval_grants import GrantIdentity  # noqa: E402

ADMIN_QQ = 10001
FRIEND_QQ = 30003
BOT_SCOPE = "90001"
CARD_REF = 555
AID = "12345678-1234-1234-1234-1234567890ab"
A1 = "aaaaaaaa-1111-1111-1111-111111111111"
A2 = "bbbbbbbb-2222-2222-2222-222222222222"
# 08:00 UTC 是北京时间 16:00，回执按北京时间写。
NOW = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)
PASSED_THROUGH = "<passed on to the ordinary chat chain>"
APPROVER = {"platform": "qq", "user_id": str(ADMIN_QQ), "bot_scope": BOT_SCOPE}

_MESSAGE_IDS = itertools.count(1000)


class _Finished(BaseException):
    """NoneBot 的 FinishedException 是 BaseException：不会被 except Exception 吞掉。"""

    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self.text = text


class _Matcher:
    async def finish(self, message: Any = None, **_kwargs: Any) -> None:
        raise _Finished("" if message is None else str(message))


def _grant(approval_id: str, minutes: int, *, until: datetime | None = None, grant_id: str = "g-1") -> ApprovalGrant:
    return ApprovalGrant(
        grant_id=grant_id, platform="qq", user_id=str(ADMIN_QQ), bot_scope=BOT_SCOPE,
        until=until or NOW + timedelta(minutes=minutes), minutes=minutes, approval_id=approval_id, issued_at=NOW,
    )


class _Supervisor:
    """假的 supervisor 客户端：记下审批决定和收回请求。同意时按要的时长回一条授权，上限 max_minutes。"""

    def __init__(self) -> None:
        self.decisions: list[dict[str, Any]] = []
        self.revocations: list[dict[str, Any]] = []
        self.grant_note = ""
        self.max_minutes = 1440
        self.revoked = 1
        self.revoke_error: Exception | None = None

    async def decide_approval(
        self,
        approval_id: str,
        *,
        decision: str,
        comment: str | None = None,
        approver: dict[str, Any] | None = None,
        grant_minutes: int | None = None,
    ) -> ApprovalDecision:
        self.decisions.append({
            "approval_id": approval_id, "decision": decision, "approver": approver, "grant_minutes": grant_minutes,
        })
        if decision != "allow":
            return ApprovalDecision(approval_id, decision, APPROVAL_DENIED)
        if self.grant_note:
            return ApprovalDecision(approval_id, decision, APPROVAL_ALLOWED, grant_note=self.grant_note)
        minutes = min(30 if grant_minutes is None else grant_minutes, self.max_minutes)
        if minutes <= 0:
            return ApprovalDecision(approval_id, decision, APPROVAL_ALLOWED, grant_note=GRANT_NOTE_ONCE)
        return ApprovalDecision(approval_id, decision, APPROVAL_ALLOWED, grant=_grant(approval_id, minutes))

    async def revoke_approval_grants(
        self, *, grant_id: str = "", identity: dict[str, Any] | None = None,
    ) -> list[ApprovalGrant]:
        self.revocations.append({"grant_id": grant_id, "identity": identity})
        if self.revoke_error is not None:
            raise self.revoke_error
        return [_grant(AID, 30, grant_id=f"g-{index}") for index in range(self.revoked)]


@dataclass
class _Chat:
    runtime: Any
    supervisor: _Supervisor

    @property
    def book(self) -> Any:
        return self.runtime.state._adapter.approvals

    def pending(self, approval_id: str = AID, *, card_ref: int = CARD_REF, admin: int = ADMIN_QQ) -> None:
        self.book.open(approval_id, operation="write_file", details={"path": "engine/runner.py"})
        self.book.remember_card(card_ref, approval_id, admin_id=admin)

    def send(self, text: str, *, user_id: int = ADMIN_QQ, reply_to: int | None = None, sub_type: str = "") -> str:
        """私聊发一条，返回 bot 的回复；走到普通聊天那一段时是 PASSED_THROUGH。"""
        onebot = self.runtime.state
        parts = [onebot.MessageSegment.reply(reply_to)] if reply_to is not None else []
        message = onebot.Message([*parts, onebot.MessageSegment.text(text)])
        event = SimpleNamespace(
            user_id=user_id, sub_type=sub_type, message_id=next(_MESSAGE_IDS), sender=SimpleNamespace(role=""),
            raw_message=None, get_message=lambda: message,
        )
        try:
            asyncio.run(self.runtime.handlers._handle_private_agent(SimpleNamespace(self_id=int(BOT_SCOPE)), event))
        except _Finished as exc:
            return exc.text
        raise AssertionError("the private handler finished without answering")


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    module = load_runtime(monkeypatch, tmp_path)
    set_live_config(module, admin_users=frozenset({ADMIN_QQ}), allow_private_friends=True)
    monkeypatch.setattr(module.state._adapter, "active_bot_scope", BOT_SCOPE)
    return module


@pytest.fixture()
def chat(runtime: Any, monkeypatch: pytest.MonkeyPatch) -> _Chat:
    supervisor = _Supervisor()
    monkeypatch.setattr(runtime.handlers, "_private_matcher", _Matcher())
    monkeypatch.setattr(runtime.state._adapter, "client", supervisor, raising=False)
    monkeypatch.setattr(runtime.state._adapter, "session_state", SimpleNamespace(), raising=False)
    monkeypatch.setattr(runtime.message_text, "_remember_message_for_reply_context", lambda event: None)

    async def event_text(_bot: Any, event: Any) -> str:
        return "".join(str(segment.data.get("text", "")) for segment in event.get_message() if segment.type == "text")

    async def help_command(*, event: Any, user_text: str) -> str:
        # 审批那几段之后的第一站：走到这里就是被当成了普通聊天。
        return PASSED_THROUGH

    monkeypatch.setattr(runtime.message_text, "_event_text_with_forward", event_text)
    monkeypatch.setattr(runtime.commands, "_maybe_handle_help_command", help_command)
    return _Chat(runtime, supervisor)


def _decided(chat: _Chat) -> list[tuple[str, str, int | None]]:
    return [(item["approval_id"], item["decision"], item["grant_minutes"]) for item in chat.supervisor.decisions]


class TestTheQuotedCard:
    def test_a_duration_after_the_verb_goes_with_the_decision(self, chat: _Chat) -> None:
        chat.pending()

        reply = chat.send("同意 2h", reply_to=CARD_REF)

        assert chat.supervisor.decisions == [
            {"approval_id": AID, "decision": "allow", "approver": APPROVER, "grant_minutes": 120},
        ]
        assert reply == "\n".join([
            f"已同意审批：{AID}",
            "操作：write_file",
            "已授权到 2026-10-26 18:00：这段时间里你发起的操作免审批，敏感操作也一样。",
            "提前收回：私聊发送 /审批 收回",
        ])
        assert AID not in chat.book.pending

    def test_a_plain_verb_leaves_the_duration_to_the_policy(self, chat: _Chat) -> None:
        chat.pending()

        reply = chat.send("同意", reply_to=CARD_REF)

        assert _decided(chat) == [(AID, "allow", None)]
        assert "已授权到 2026-10-26 16:30" in reply

    def test_once_approves_without_a_grant(self, chat: _Chat) -> None:
        chat.pending()

        reply = chat.send("同意 单次", reply_to=CARD_REF)

        assert _decided(chat) == [(AID, "allow", 0)]
        assert "只同意了这一次，没有生成授权。" in reply and "已授权到" not in reply

    def test_a_deny_carries_no_duration(self, chat: _Chat) -> None:
        chat.pending()

        reply = chat.send("拒绝", reply_to=CARD_REF)

        assert _decided(chat) == [(AID, "deny", None)]
        assert reply == f"已拒绝审批：{AID}\n操作：write_file"

    @pytest.mark.parametrize("text", ["拒绝 2h", "同意 1.5h", "同意吧，快点", "同意 一小时", "同意 2h 吧", "同意 1 0m"])
    def test_a_reply_that_is_not_exactly_a_decision_stays_chat(self, chat: _Chat, text: str) -> None:
        chat.pending()

        assert chat.send(text, reply_to=CARD_REF) == PASSED_THROUGH
        assert chat.supervisor.decisions == []
        assert AID in chat.book.pending


class TestTheCommand:
    @pytest.mark.parametrize(("text", "minutes", "until"), [
        (f"/审批 同意 {AID} 2h", 120, "2026-10-26 18:00"),
        (f"/审批 同意 {AID} 45 分钟", 45, "2026-10-26 16:45"),
        (f"/同意 {AID[:8]} 30m", 30, "2026-10-26 16:30"),
        (f"/approval approve {AID} 1d", 1440, "2026-10-27 16:00"),
    ])
    def test_a_duration_after_the_id(self, chat: _Chat, text: str, minutes: int, until: str) -> None:
        chat.pending()

        reply = chat.send(text)

        assert chat.supervisor.decisions == [
            {"approval_id": AID, "decision": "allow", "approver": APPROVER, "grant_minutes": minutes},
        ]
        assert f"已授权到 {until}" in reply and "/审批 收回" in reply

    def test_without_a_duration_the_policy_default_applies(self, chat: _Chat) -> None:
        chat.pending()

        chat.send(f"/审批 同意 {AID}")

        assert _decided(chat) == [(AID, "allow", None)]

    def test_once_after_the_id(self, chat: _Chat) -> None:
        chat.pending()

        reply = chat.send(f"/审批 同意 {AID} 单次")

        assert _decided(chat) == [(AID, "allow", 0)]
        assert "只同意了这一次" in reply

    def test_a_duration_past_the_ceiling_is_reported_as_capped(self, chat: _Chat) -> None:
        chat.pending()

        reply = chat.send(f"/审批 同意 {AID} 3d")

        assert _decided(chat) == [(AID, "allow", 4320)]
        assert "已授权到 2026-10-27 16:00" in reply
        assert "要的时长超过了上限，按上限 1 天授权。" in reply

    @pytest.mark.parametrize("tail", ["好的", "1.5h", "2h 3m", "一小时", "1h30m"])
    def test_a_bad_duration_decides_nothing(self, chat: _Chat, tail: str) -> None:
        chat.pending()

        reply = chat.send(f"/审批 同意 {AID} {tail}")

        assert reply == chat.runtime.approval._GRANT_DURATION_HINT
        assert chat.supervisor.decisions == []
        assert AID in chat.book.pending

    def test_a_deny_ignores_what_follows_the_id(self, chat: _Chat) -> None:
        chat.pending()

        reply = chat.send(f"/审批 拒绝 {AID} 2h")

        assert _decided(chat) == [(AID, "deny", None)]
        assert reply.startswith(f"已拒绝审批：{AID}")

    def test_a_duration_without_an_id_decides_the_only_pending_approval(self, chat: _Chat) -> None:
        chat.pending()

        chat.send("/审批 同意 2h")

        assert _decided(chat) == [(AID, "allow", 120)]

    def test_a_duration_without_an_id_and_two_pending_asks_which(self, chat: _Chat) -> None:
        chat.pending(A1, card_ref=101)
        chat.pending(A2, card_ref=102)

        reply = chat.send("/审批 同意 2h")

        assert "无法安全确定目标" in reply and "/审批 同意 <ID>（后面可以加时长" in reply
        assert chat.supervisor.decisions == []

    def test_a_day_count_is_looked_up_as_an_id_first(self, chat: _Chat) -> None:
        chat.pending()

        reply = chat.send("/审批 同意 1d")

        assert reply.startswith("没有找到待审批 ID：1d。")
        assert "/审批 同意 <ID> 1d" in reply and "「同意 1天」" in reply
        assert chat.supervisor.decisions == []

    def test_a_deny_with_a_day_count_gets_the_plain_answer(self, chat: _Chat) -> None:
        chat.pending()

        assert chat.send("/审批 拒绝 1d") == "没有找到待审批 ID：1d"

    def test_without_the_slash_it_is_chat(self, chat: _Chat) -> None:
        chat.pending()

        assert chat.send(f"审批 同意 {AID} 2h") == PASSED_THROUGH
        assert chat.supervisor.decisions == []


class TestTheBareVerb:
    @pytest.mark.parametrize("text", ["同意 45分钟", "/同意 45分钟", "同意45分钟"])
    def test_a_duration_on_the_only_pending_approval(self, chat: _Chat, text: str) -> None:
        chat.pending()

        reply = chat.send(text)

        assert _decided(chat) == [(AID, "allow", 45)]
        assert "已授权到 2026-10-26 16:45" in reply

    def test_with_unique_matching_off_it_still_asks(self, chat: _Chat) -> None:
        set_live_config(chat.runtime, approval_bare_verb_unique=False)
        chat.pending()

        reply = chat.send("同意 45分钟")

        assert "无法安全确定目标" in reply
        assert chat.supervisor.decisions == []

    def test_without_a_pending_approval_it_is_chat(self, chat: _Chat) -> None:
        assert chat.send("同意 2h") == PASSED_THROUGH
        assert chat.supervisor.decisions == []

    def test_someone_without_the_capability_is_not_intercepted(self, chat: _Chat) -> None:
        chat.pending()

        assert chat.send("同意 2h", user_id=FRIEND_QQ, sub_type="friend") == PASSED_THROUGH
        assert chat.supervisor.decisions == []


class TestRevoking:
    @pytest.mark.parametrize("text", ["/审批 收回", "/审批 收回授权", "/approval revoke", "/审批 收回。", "／审批 收回"])
    def test_the_approver_revokes_his_own_grants(self, chat: _Chat, text: str) -> None:
        reply = chat.send(text)

        assert reply == "已收回你名下 1 条生效中的授权，之后你发起的操作照常审批。"
        assert chat.supervisor.revocations == [{"grant_id": "", "identity": APPROVER}]

    def test_the_count_is_reported(self, chat: _Chat) -> None:
        chat.supervisor.revoked = 2

        assert chat.send("/审批 收回") == "已收回你名下 2 条生效中的授权，之后你发起的操作照常审批。"

    def test_nothing_to_revoke(self, chat: _Chat) -> None:
        chat.supervisor.revoked = 0

        assert chat.send("/审批 收回") == "你名下没有生效中的授权。"

    def test_a_failure_is_reported(self, chat: _Chat) -> None:
        chat.supervisor.revoke_error = RuntimeError("supervisor down")

        assert chat.send("/审批 收回") == "收回授权失败：supervisor down"

    def test_someone_without_the_capability_is_refused(self, chat: _Chat) -> None:
        reply = chat.send("/审批 收回", user_id=FRIEND_QQ, sub_type="friend")

        assert reply == "你不是 Clonoth 审批管理员，没有可收回的授权。"
        assert chat.supervisor.revocations == []

    def test_a_switched_off_capability_is_named_to_the_listed_admin(self, chat: _Chat) -> None:
        set_live_config(chat.runtime, grant_approval="off")

        assert chat.send("/审批 收回") == "「审批」已在配置里关闭。"
        assert chat.supervisor.revocations == []

    def test_a_switched_off_capability_stays_quiet_to_others(self, chat: _Chat) -> None:
        set_live_config(chat.runtime, grant_approval="off")

        assert chat.send("/审批 收回", user_id=FRIEND_QQ, sub_type="friend") == PASSED_THROUGH
        assert chat.supervisor.revocations == []

    @pytest.mark.parametrize("text", ["审批 收回", "收回", "/审批 收回 全部", "/审批 撤回"])
    def test_anything_else_is_not_a_revocation(self, chat: _Chat, text: str) -> None:
        assert chat.send(text) == PASSED_THROUGH
        assert chat.supervisor.revocations == []


class TestTheApprover:
    def test_it_is_reported_the_way_the_inbound_identity_is(self, runtime: Any) -> None:
        # 两边认法不一致，本人同意自己的请求也会被当成「不是本人发起」。
        inbound = {
            "platform": "qq",
            "user_id": runtime.identity._verified_sender_id(SimpleNamespace(user_id=ADMIN_QQ)),
            "bot_scope": runtime.routing._active_bot_scope(),
        }

        approver = runtime.approval._approver(ADMIN_QQ)

        assert approver == APPROVER
        assert GrantIdentity.from_auth(approver).same_person(GrantIdentity.from_auth(inbound))

    def test_the_anonymous_placeholder_is_nobody(self, runtime: Any) -> None:
        assert runtime.approval._approver(80000000)["user_id"] == ""
        assert GrantIdentity.from_auth(runtime.approval._approver(80000000)) is None

    def test_without_an_account_the_scope_is_empty(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(runtime.state._adapter, "active_bot_scope", "")

        assert runtime.approval._approver(ADMIN_QQ)["bot_scope"] == ""


class TestTheReceipt:
    def test_a_grant_names_its_expiry_and_how_to_revoke(self, runtime: Any) -> None:
        result = ApprovalDecision(AID, "allow", APPROVAL_ALLOWED, grant=_grant(AID, 30))

        assert runtime.approval._decision_receipt(AID, "allow", "write_file", result, None) == "\n".join([
            f"已同意审批：{AID}",
            "操作：write_file",
            "已授权到 2026-10-26 16:30：这段时间里你发起的操作免审批，敏感操作也一样。",
            "提前收回：私聊发送 /审批 收回",
        ])

    def test_a_capped_duration_says_so(self, runtime: Any) -> None:
        result = ApprovalDecision(AID, "allow", APPROVAL_ALLOWED, grant=_grant(AID, 1440))

        receipt = runtime.approval._decision_receipt(AID, "allow", "write_file", result, 4320)

        assert "要的时长超过了上限，按上限 1 天授权。" in receipt

    def test_no_cap_is_reported_when_nothing_was_asked(self, runtime: Any) -> None:
        result = ApprovalDecision(AID, "allow", APPROVAL_ALLOWED, grant=_grant(AID, 30))

        assert "上限" not in runtime.approval._decision_receipt(AID, "allow", "write_file", result, None)

    def test_a_later_existing_grant_is_kept(self, runtime: Any) -> None:
        later = NOW + timedelta(hours=4)
        result = ApprovalDecision(AID, "allow", APPROVAL_ALLOWED, grant=_grant(AID, 30, until=later))

        receipt = runtime.approval._decision_receipt(AID, "allow", "write_file", result, 30)

        assert "已授权到 2026-10-26 20:00" in receipt
        assert "你原有的授权到期更晚，到期时间沿用原来的。" in receipt

    @pytest.mark.parametrize(("note", "text"), [
        (GRANT_NOTE_ONCE, "只同意了这一次，没有生成授权。"),
        (GRANT_NOTE_NOT_REQUESTER, "这条请求不是你发起的，只同意了这一次，没有生成授权。"),
        (GRANT_NOTE_NO_REQUESTER, "查不到这条请求是谁发起的，只同意了这一次，没有生成授权。"),
        (GRANT_NOTE_NO_APPROVER, "认不出审批人的身份，只同意了这一次，没有生成授权。"),
        (GRANT_NOTE_DISABLED, "策略的默认时长是 0"),
    ])
    def test_no_grant_says_why(self, runtime: Any, note: str, text: str) -> None:
        result = ApprovalDecision(AID, "allow", APPROVAL_ALLOWED, grant_note=note)

        receipt = runtime.approval._decision_receipt(AID, "allow", "write_file", result, None)

        assert text in receipt and "已授权到" not in receipt

    def test_every_note_the_supervisor_can_send_has_a_text(self, runtime: Any) -> None:
        notes = {value for name, value in vars(sdk_types).items() if name.startswith("GRANT_NOTE_")}

        assert notes and notes <= set(runtime.approval._GRANT_NOTE_TEXT)

    @pytest.mark.parametrize("result", [
        pytest.param(ApprovalDecision(AID, "allow", APPROVAL_ALLOWED), id="old-supervisor"),
        pytest.param(ApprovalDecision(AID, "allow", APPROVAL_ALLOWED, grant_note="something-new"), id="unknown-note"),
        pytest.param(
            ApprovalDecision(AID, "allow", APPROVAL_ALLOWED, grant=ApprovalGrant(grant_id="g", platform="qq", user_id="1")),
            id="grant-without-expiry",
        ),
    ])
    def test_without_usable_grant_details_only_the_decision_is_told(self, runtime: Any, result: ApprovalDecision) -> None:
        receipt = runtime.approval._decision_receipt(AID, "allow", "write_file", result, None)

        assert receipt == f"已同意审批：{AID}\n操作：write_file"

    def test_a_deny_never_talks_about_grants(self, runtime: Any) -> None:
        result = ApprovalDecision(AID, "deny", APPROVAL_DENIED, grant_note=GRANT_NOTE_ONCE)

        assert runtime.approval._decision_receipt(AID, "deny", None, result, None) == f"已拒绝审批：{AID}\n操作：unknown"

    @pytest.mark.parametrize(("minutes", "text"), [
        (45, "45 分钟"), (90, "90 分钟"), (60, "1 小时"), (120, "2 小时"), (1440, "1 天"), (4320, "3 天"), (0, "0 分钟"),
    ])
    def test_minutes_are_written_in_the_largest_whole_unit(self, runtime: Any, minutes: int, text: str) -> None:
        assert runtime.approval._minutes_text(minutes) == text

    def test_the_expiry_is_written_in_beijing_time(self, runtime: Any) -> None:
        assert runtime.approval._local_time(datetime(2026, 10, 26, 16, 5, tzinfo=timezone.utc)) == "2026-10-27 00:05"


class TestTheCardAndTheHelp:
    def test_the_card_shows_the_duration_and_slash_commands(self, runtime: Any) -> None:
        card = runtime.approval._approval_summary(AID, "write_file", {"path": "engine/runner.py"})

        assert f"/审批 同意 {AID}" in card and f"/审批 拒绝 {AID}" in card
        assert "同意 2h" in card and "单次" in card
        # 卡片上多了说明，引用时照样取得出是哪条审批。
        assert runtime.approval._extract_approval_id_from_text(card) == AID

    def test_every_hint_writes_the_command_with_its_slash(self, runtime: Any) -> None:
        assert "/审批 同意 <ID>" in runtime.approval._APPROVAL_REPLY_UNRESOLVED_HINT
        assert "/审批 同意 <ID> 2h" in runtime.approval._GRANT_DURATION_HINT
        assert runtime.approval._REVOKE_HINT == "提前收回：私聊发送 /审批 收回"

    def test_the_help_lists_durations_and_revoking_for_an_approver(self, runtime: Any) -> None:
        text = runtime.commands._help_text(SimpleNamespace(user_id=ADMIN_QQ))

        assert "/审批 同意 <ID> [时长]" in text and "/审批 收回" in text

    def test_others_do_not_see_them(self, runtime: Any) -> None:
        assert "/审批" not in runtime.commands._help_text(SimpleNamespace(user_id=FRIEND_QQ))
