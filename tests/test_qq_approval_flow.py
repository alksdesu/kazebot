"""QQ 审批链：引用锚定、收件人绑定、已决策/超时登记、动词整词匹配、原子领取。

钉住问题 1/2/41：裸发动词不批、多管理员不误批别条、now/note/know 不再命中拒绝。
"""
from __future__ import annotations

import asyncio
import sys
import time
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

LISTED_QQ = 10001
ADMIN2_QQ = 10002
OUTSIDER_QQ = 30003

AID = "12345678-1234-1234-1234-1234567890ab"
A1 = "aaaaaaaa-1111-1111-1111-111111111111"
A2 = "bbbbbbbb-2222-2222-2222-222222222222"


class _Finished(BaseException):
    """模拟 NoneBot 的 FinishedException：继承 BaseException，不被 except Exception 吞掉。"""

    def __init__(self, text: str = "") -> None:
        self.text = text
        super().__init__(text)


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    module = load_runtime(monkeypatch, tmp_path)
    set_live_config(module, admin_users=frozenset({LISTED_QQ, ADMIN2_QQ}))
    return module


def _fake_bot(self_id: int, msg_by_id: dict[Any, Any] | None = None) -> Any:
    store = dict(msg_by_id or {})

    async def call_api(action: str, **kwargs: Any) -> Any:
        if action != "get_msg":
            raise RuntimeError(f"unexpected api {action}")
        mid = kwargs.get("message_id")
        data = store.get(mid)
        if data is None:
            data = store.get(str(mid))
        if data is None:
            raise RuntimeError("no such msg")
        return data

    return SimpleNamespace(self_id=self_id, call_api=call_api)


def _card_msg(runtime: Any, aid: str, self_id: int, *, operation: str = "read_file") -> dict[str, Any]:
    text = runtime._approval_summary(aid, operation, {})
    return {"message": [{"type": "text", "data": {"text": text}}], "sender": {"user_id": self_id}}


def _reply_event(runtime: Any, user_id: int, reply_to: Any) -> Any:
    message = runtime.Message([runtime.MessageSegment.reply(reply_to), runtime.MessageSegment.text("同意")])
    return SimpleNamespace(
        user_id=user_id, message_id=1, sender=SimpleNamespace(role=""),
        raw_message=None, get_message=lambda: message,
    )


def _plain_event(runtime: Any, user_id: int, text: str = "同意") -> Any:
    message = runtime.Message([runtime.MessageSegment.text(text)])
    return SimpleNamespace(
        user_id=user_id, message_id=1, sender=SimpleNamespace(role=""),
        raw_message=None, get_message=lambda: message,
    )


class TestApprovalVerb:
    """问题 41：一份词表、整词匹配。"""

    @pytest.mark.parametrize("text,expected", [
        ("同意", "allow"), ("审批同意", "allow"), ("同意。", "allow"),
        ("ok", "allow"), ("OK", "allow"), ("审批：同意", "allow"),
        ("拒绝", "deny"), ("不同意", "deny"), ("审批 拒绝", "deny"),
        ("know", None), ("I don't know", None), ("note", None),
        ("now", None), ("nothing", None), ("no problem", None),
        ("okay 了", None), ("同意吧，快点", None),
    ])
    def test_reply_verb_is_whole_word_only(self, runtime: Any, text: str, expected: Any) -> None:
        assert runtime._parse_approval_reply_verb(text) == expected

    def test_one_table_feeds_both_entries(self, runtime: Any) -> None:
        for verb in runtime._APPROVAL_ALLOW_VERBS:
            assert runtime._approval_verb(verb) == "allow"
            assert runtime._parse_approval_command(f"/审批 {verb} {AID}") == ("allow", AID)
        for verb in runtime._APPROVAL_DENY_VERBS:
            assert runtime._approval_verb(verb) == "deny"
            assert runtime._parse_approval_command(f"/审批 {verb} {AID}") == ("deny", AID)
        assert runtime._APPROVAL_ALLOW_VERBS & runtime._APPROVAL_DENY_VERBS == frozenset()

    def test_bare_verb_needs_id_shaped_token(self, runtime: Any) -> None:
        assert runtime._parse_approval_command("/通过 微信发给我") is None
        assert runtime._parse_approval_command(f"/同意 {AID[:8]}") == ("allow", AID[:8])
        assert runtime._parse_approval_command(f"/审批 同意 {AID}") == ("allow", AID)

    def test_a_command_without_the_slash_is_ordinary_chat(self, runtime: Any) -> None:
        # 引用卡片那条路径仍然免斜杠，走的是 _parse_approval_reply_verb。
        assert runtime._parse_approval_command(f"同意 {AID}") is None
        assert runtime._parse_approval_command(f"审批 同意 {AID}") is None
        assert runtime._parse_approval_reply_verb("同意") == "allow"


class TestReplyTargetAnchoring:
    """问题 1/2：引用必须锚定到真实卡片，收件人绑定，已决策/超时登记。"""

    def test_bare_verb_without_reply_does_not_resolve(self, runtime: Any) -> None:
        runtime._pending_approvals[AID] = {"operation": "read_file", "created_at": time.time()}
        event = _plain_event(runtime, LISTED_QQ)
        target = asyncio.run(runtime._resolve_approval_reply_target(_fake_bot(1), event))
        assert target.matched() is False
        assert AID in runtime._pending_approvals

    def test_quoted_non_card_message_is_ignored(self, runtime: Any) -> None:
        runtime._pending_approvals[AID] = {"operation": "read_file", "created_at": time.time()}
        bot = _fake_bot(1, {999: {"message": [{"type": "text", "data": {"text": "普通消息"}}],
                                  "sender": {"user_id": 1}}})
        target = asyncio.run(runtime._resolve_approval_target_by_quoted_text(bot, 999))
        assert target.matched() is False

    def test_quoted_real_card_yields_approval_id(self, runtime: Any) -> None:
        runtime._pending_approvals[AID] = {"operation": "read_file", "created_at": time.time()}
        bot = _fake_bot(1, {999: _card_msg(runtime, AID, self_id=1)})
        target = asyncio.run(runtime._resolve_approval_target_by_quoted_text(bot, 999))
        assert target.approval_id == AID and target.from_card is True

    def test_quoted_card_from_other_sender_is_rejected(self, runtime: Any) -> None:
        runtime._pending_approvals[AID] = {"operation": "read_file", "created_at": time.time()}
        # sender 不是 bot 自己 → 不认这张卡片。
        bot = _fake_bot(1, {999: _card_msg(runtime, AID, self_id=42)})
        target = asyncio.run(runtime._resolve_approval_target_by_quoted_text(bot, 999))
        assert target.matched() is False

    def test_reply_map_binds_recipient(self, runtime: Any) -> None:
        runtime._pending_approvals[AID] = {"operation": "read_file", "created_at": time.time()}
        runtime._remember_approval_message(555, AID, admin_id=LISTED_QQ)
        assert runtime._resolve_approval_target_by_reply(555, LISTED_QQ).approval_id == AID
        assert runtime._resolve_approval_target_by_reply(555, ADMIN2_QQ).matched() is False

    def test_settled_card_does_not_batch_another_pending(self, runtime: Any) -> None:
        now = time.time()
        runtime._pending_approvals[A1] = {"operation": "read_file", "created_at": now}
        runtime._pending_approvals[A2] = {"operation": "list_dir", "created_at": now}
        runtime._remember_approval_message(101, A1, admin_id=LISTED_QQ)
        runtime._remember_approval_message(102, A1, admin_id=ADMIN2_QQ)
        runtime._remember_approval_message(201, A2, admin_id=LISTED_QQ)
        runtime._remember_approval_message(202, A2, admin_id=ADMIN2_QQ)
        # admin1 已经批掉 A1。
        runtime._pending_approvals.pop(A1)
        runtime._record_settled_approval(A1, admin_id=LISTED_QQ, decision="allow", operation="read_file")
        # admin2 引用他自己那张 A1 卡片。
        target = runtime._resolve_approval_target_by_reply(102, ADMIN2_QQ)
        assert target.settled_id == A1 and target.approval_id == ""
        assert A2 not in (target.approval_id, target.settled_id)

    def test_expired_pending_is_pruned_and_reported(self, runtime: Any) -> None:
        runtime._pending_approvals[AID] = {
            "operation": "read_file",
            "created_at": time.time() - runtime.PENDING_APPROVAL_TTL_SECONDS - 1,
        }
        resolved, message = runtime._resolve_pending_approval_id(AID[:8])
        assert resolved is None and "超时" in message
        assert AID in runtime._settled_approvals
        assert AID not in runtime._pending_approvals

    def test_unique_fallback_is_opt_in_and_recipient_bound(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        set_live_config(runtime, approval_reply_unique_fallback=True)
        runtime._pending_approvals[AID] = {"operation": "read_file", "created_at": time.time()}
        # 卡片记在另一个 mid 上（NapCat 上报的 reply id 与之对不上），get_msg 也失败。
        runtime._remember_approval_message(111, AID, admin_id=LISTED_QQ)
        bot = _fake_bot(1)  # get_msg 一律失败
        event = _reply_event(runtime, LISTED_QQ, reply_to=999)
        hit = asyncio.run(runtime._resolve_approval_reply_target(bot, event))
        assert hit.approval_id == AID

    def test_unique_fallback_denies_non_recipient(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        set_live_config(runtime, approval_reply_unique_fallback=True)
        runtime._pending_approvals[AID] = {"operation": "read_file", "created_at": time.time()}
        runtime._remember_approval_message(111, AID, admin_id=ADMIN2_QQ)  # 卡片发给别人
        bot = _fake_bot(1)
        event = _reply_event(runtime, LISTED_QQ, reply_to=999)
        miss = asyncio.run(runtime._resolve_approval_reply_target(bot, event))
        assert miss.matched() is False


class TestFinishDecision:
    """问题 2：原子领取，多管理员只批一次，失败回滚。"""

    @pytest.fixture()
    def wired(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
        async def _finish(text: str = "") -> None:
            raise _Finished(text)

        monkeypatch.setattr(runtime, "_private_matcher", SimpleNamespace(finish=_finish))
        return runtime

    @staticmethod
    def _decide(runtime: Any, admin: int, aid: str, decision: str) -> str:
        try:
            asyncio.run(runtime._finish_approval_decision(admin, aid, decision))
        except _Finished as exc:
            return exc.text
        raise AssertionError("finish was not called")

    def test_concurrent_decisions_submit_once(self, wired: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        wired._pending_approvals[AID] = {"operation": "read_file", "created_at": time.time()}
        calls: list[tuple[str, str]] = []

        async def scenario() -> list[Any]:
            gate = asyncio.Event()

            async def approve(aid: str, decision: str, comment: str) -> bool:
                calls.append((aid, decision))
                await gate.wait()
                return True

            monkeypatch.setattr(wired, "_client", SimpleNamespace(approve=approve))
            t1 = asyncio.create_task(wired._finish_approval_decision(LISTED_QQ, AID, "allow"))
            t2 = asyncio.create_task(wired._finish_approval_decision(ADMIN2_QQ, AID, "deny"))
            await asyncio.sleep(0.05)
            gate.set()
            return await asyncio.gather(t1, t2, return_exceptions=True)

        results = asyncio.run(scenario())
        texts = [exc.text for exc in results if isinstance(exc, _Finished)]
        assert len(calls) == 1
        assert any("已被处理" in t for t in texts)
        assert any("已同意审批" in t for t in texts)
        assert AID not in wired._pending_approvals

    def test_submit_exception_rolls_back(self, wired: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        wired._pending_approvals[AID] = {"operation": "read_file", "created_at": time.time()}

        async def approve(aid: str, decision: str, comment: str) -> bool:
            raise RuntimeError("boom")

        monkeypatch.setattr(wired, "_client", SimpleNamespace(approve=approve))
        text = self._decide(wired, LISTED_QQ, AID, "allow")
        assert "提交审批失败" in text
        assert AID in wired._pending_approvals and AID not in wired._settled_approvals

    def test_supervisor_reject_rolls_back(self, wired: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        wired._pending_approvals[AID] = {"operation": "read_file", "created_at": time.time()}

        async def approve(aid: str, decision: str, comment: str) -> bool:
            return False

        monkeypatch.setattr(wired, "_client", SimpleNamespace(approve=approve))
        text = self._decide(wired, LISTED_QQ, AID, "allow")
        assert text == "审批提交被 Supervisor 拒绝，请检查日志。"
        assert AID in wired._pending_approvals and AID not in wired._settled_approvals

    def test_success_receipt_carries_operation(self, wired: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        wired._pending_approvals[AID] = {"operation": "read_file", "created_at": time.time()}

        async def approve(aid: str, decision: str, comment: str) -> bool:
            return True

        monkeypatch.setattr(wired, "_client", SimpleNamespace(approve=approve))
        text = self._decide(wired, LISTED_QQ, AID, "allow")
        assert f"已同意审批：{AID}" in text and "操作：read_file" in text
        assert AID not in wired._pending_approvals
        assert wired._settled_approvals[AID]["admin_id"] == LISTED_QQ
        assert "已同意" in wired._settled_approval_notice(AID)

    def test_second_decision_is_told_already_handled(self, wired: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        async def approve(aid: str, decision: str, comment: str) -> bool:
            return True

        monkeypatch.setattr(wired, "_client", SimpleNamespace(approve=approve))
        wired._record_settled_approval(AID, admin_id=LISTED_QQ, decision="allow", operation="read_file")
        text = self._decide(wired, ADMIN2_QQ, AID, "deny")
        assert "已被处理" in text
