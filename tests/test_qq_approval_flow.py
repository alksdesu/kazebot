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
from adapters._shared import approvals  # noqa: E402
from adapters.onebot.segments import normalize_message_id  # noqa: E402

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
    text = runtime.approval._approval_summary(aid, operation, {})
    return {"message": [{"type": "text", "data": {"text": text}}], "sender": {"user_id": self_id}}


def _reply_event(runtime: Any, user_id: int, reply_to: Any) -> Any:
    message = runtime.state.Message([runtime.state.MessageSegment.reply(reply_to), runtime.state.MessageSegment.text("同意")])
    return SimpleNamespace(
        user_id=user_id, message_id=1, sender=SimpleNamespace(role=""),
        raw_message=None, get_message=lambda: message,
    )


def _plain_event(runtime: Any, user_id: int, text: str = "同意") -> Any:
    message = runtime.state.Message([runtime.state.MessageSegment.text(text)])
    return SimpleNamespace(
        user_id=user_id, message_id=1, sender=SimpleNamespace(role=""),
        raw_message=None, get_message=lambda: message,
    )


class TestApprovalVerb:
    """问题 41：一份词表、整词匹配。词表和整词判定本身在 test_shared_approvals，这里钉 QQ 的命令入口。"""

    def test_one_table_feeds_both_entries(self, runtime: Any) -> None:
        for verb in approvals.ALLOW_VERBS:
            assert runtime.approval._parse_approval_command(f"/审批 {verb} {AID}") == ("allow", AID)
        for verb in approvals.DENY_VERBS:
            assert runtime.approval._parse_approval_command(f"/审批 {verb} {AID}") == ("deny", AID)

    def test_bare_verb_needs_id_shaped_token(self, runtime: Any) -> None:
        assert runtime.approval._parse_approval_command("/通过 微信发给我") is None
        assert runtime.approval._parse_approval_command(f"/同意 {AID[:8]}") == ("allow", AID[:8])
        assert runtime.approval._parse_approval_command(f"/审批 同意 {AID}") == ("allow", AID)

    def test_a_command_without_the_slash_is_ordinary_chat(self, runtime: Any) -> None:
        # 引用卡片那条路径仍然免斜杠，走的是 parse_reply_verb。
        assert runtime.approval._parse_approval_command(f"同意 {AID}") is None
        assert runtime.approval._parse_approval_command(f"审批 同意 {AID}") is None
        assert approvals.parse_reply_verb("同意") == "allow"


class TestReplyTargetAnchoring:
    """问题 1/2：引用必须锚定到真实卡片，收件人绑定，已决策/超时登记。"""

    def test_bare_verb_without_reply_does_not_resolve(self, runtime: Any) -> None:
        runtime.state._adapter.approvals.pending[AID] = {"operation": "read_file", "created_at": time.time()}
        event = _plain_event(runtime, LISTED_QQ)
        target = asyncio.run(runtime.approval._resolve_approval_reply_target(_fake_bot(1), event))
        assert target.matched() is False
        assert AID in runtime.state._adapter.approvals.pending

    def test_quoted_non_card_message_is_ignored(self, runtime: Any) -> None:
        runtime.state._adapter.approvals.pending[AID] = {"operation": "read_file", "created_at": time.time()}
        bot = _fake_bot(1, {999: {"message": [{"type": "text", "data": {"text": "普通消息"}}],
                                  "sender": {"user_id": 1}}})
        target = asyncio.run(runtime.approval._resolve_approval_target_by_quoted_text(bot, 999))
        assert target.matched() is False

    def test_quoted_real_card_yields_approval_id(self, runtime: Any) -> None:
        runtime.state._adapter.approvals.pending[AID] = {"operation": "read_file", "created_at": time.time()}
        bot = _fake_bot(1, {999: _card_msg(runtime, AID, self_id=1)})
        target = asyncio.run(runtime.approval._resolve_approval_target_by_quoted_text(bot, 999))
        assert target.approval_id == AID and target.from_card is True

    def test_quoted_card_from_other_sender_is_rejected(self, runtime: Any) -> None:
        runtime.state._adapter.approvals.pending[AID] = {"operation": "read_file", "created_at": time.time()}
        # sender 不是 bot 自己 → 不认这张卡片。
        bot = _fake_bot(1, {999: _card_msg(runtime, AID, self_id=42)})
        target = asyncio.run(runtime.approval._resolve_approval_target_by_quoted_text(bot, 999))
        assert target.matched() is False

    def test_reply_map_binds_recipient(self, runtime: Any) -> None:
        runtime.state._adapter.approvals.pending[AID] = {"operation": "read_file", "created_at": time.time()}
        runtime.state._adapter.approvals.remember_card(555, AID, admin_id=LISTED_QQ)
        assert runtime.state._adapter.approvals.resolve_card(555, LISTED_QQ).approval_id == AID
        assert runtime.state._adapter.approvals.resolve_card(555, ADMIN2_QQ).matched() is False

    @pytest.mark.parametrize(("raw", "expected"), [
        (None, ""), ("", ""), ("  ", ""), (42, "42"), ("-123.0", "-123"), (" 7 ", "7"), ("1.5", "1"), ("abc", "abc"),
    ])
    def test_onebot_message_ids_normalize_to_one_key(self, raw: Any, expected: str) -> None:
        assert normalize_message_id(raw) == expected

    def test_card_ids_match_across_onebot_id_shapes(self, runtime: Any) -> None:
        # reply 段和 send_msg 回来的 id 写法不一：int、带小数点的字符串、带空白。
        runtime.state._adapter.approvals.pending[AID] = {"operation": "read_file", "created_at": time.time()}
        runtime.state._adapter.approvals.remember_card(-123, AID, admin_id=LISTED_QQ)
        for reply_id in ("-123.0", " -123 ", -123):
            assert runtime.state._adapter.approvals.resolve_card(reply_id, LISTED_QQ).approval_id == AID

    def test_settled_card_does_not_batch_another_pending(self, runtime: Any) -> None:
        now = time.time()
        runtime.state._adapter.approvals.pending[A1] = {"operation": "read_file", "created_at": now}
        runtime.state._adapter.approvals.pending[A2] = {"operation": "list_dir", "created_at": now}
        runtime.state._adapter.approvals.remember_card(101, A1, admin_id=LISTED_QQ)
        runtime.state._adapter.approvals.remember_card(102, A1, admin_id=ADMIN2_QQ)
        runtime.state._adapter.approvals.remember_card(201, A2, admin_id=LISTED_QQ)
        runtime.state._adapter.approvals.remember_card(202, A2, admin_id=ADMIN2_QQ)
        # admin1 已经批掉 A1。
        runtime.state._adapter.approvals.pending.pop(A1)
        runtime.state._adapter.approvals.settle(A1, admin_id=LISTED_QQ, decision="allow", operation="read_file")
        # admin2 引用他自己那张 A1 卡片。
        target = runtime.state._adapter.approvals.resolve_card(102, ADMIN2_QQ)
        assert target.settled_id == A1 and target.approval_id == ""
        assert A2 not in (target.approval_id, target.settled_id)

    def test_expired_pending_is_pruned_and_reported(self, runtime: Any) -> None:
        runtime.state._adapter.approvals.pending[AID] = {
            "operation": "read_file",
            "created_at": time.time() - runtime.state._adapter.approvals.ttl_seconds - 1,
        }
        resolved, message = runtime.approval._resolve_pending_approval_id(AID[:8])
        assert resolved is None and "超时" in message
        assert AID in runtime.state._adapter.approvals.settled
        assert AID not in runtime.state._adapter.approvals.pending

    def test_unique_fallback_is_opt_in_and_recipient_bound(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        set_live_config(runtime, approval_reply_unique_fallback=True)
        runtime.state._adapter.approvals.pending[AID] = {"operation": "read_file", "created_at": time.time()}
        # 卡片记在另一个 mid 上（NapCat 上报的 reply id 与之对不上），get_msg 也失败。
        runtime.state._adapter.approvals.remember_card(111, AID, admin_id=LISTED_QQ)
        bot = _fake_bot(1)  # get_msg 一律失败
        event = _reply_event(runtime, LISTED_QQ, reply_to=999)
        hit = asyncio.run(runtime.approval._resolve_approval_reply_target(bot, event))
        assert hit.approval_id == AID

    def test_unique_fallback_denies_non_recipient(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        set_live_config(runtime, approval_reply_unique_fallback=True)
        runtime.state._adapter.approvals.pending[AID] = {"operation": "read_file", "created_at": time.time()}
        runtime.state._adapter.approvals.remember_card(111, AID, admin_id=ADMIN2_QQ)  # 卡片发给别人
        bot = _fake_bot(1)
        event = _reply_event(runtime, LISTED_QQ, reply_to=999)
        miss = asyncio.run(runtime.approval._resolve_approval_reply_target(bot, event))
        assert miss.matched() is False


class TestFinishDecision:
    """问题 2：原子领取，多管理员只批一次，失败回滚。"""

    @pytest.fixture()
    def wired(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
        async def _finish(text: str = "") -> None:
            raise _Finished(text)

        monkeypatch.setattr(runtime.handlers, "_private_matcher", SimpleNamespace(finish=_finish))
        return runtime

    @staticmethod
    def _decide(runtime: Any, admin: int, aid: str, decision: str) -> str:
        try:
            asyncio.run(runtime.handlers._finish_approval_decision(admin, aid, decision))
        except _Finished as exc:
            return exc.text
        raise AssertionError("finish was not called")

    def test_concurrent_decisions_submit_once(self, wired: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        wired.state._adapter.approvals.pending[AID] = {"operation": "read_file", "created_at": time.time()}
        calls: list[tuple[str, str]] = []

        async def scenario() -> list[Any]:
            gate = asyncio.Event()

            async def approve(aid: str, decision: str, comment: str) -> bool:
                calls.append((aid, decision))
                await gate.wait()
                return True

            monkeypatch.setattr(wired.state._adapter, "client", SimpleNamespace(approve=approve))
            t1 = asyncio.create_task(wired.handlers._finish_approval_decision(LISTED_QQ, AID, "allow"))
            t2 = asyncio.create_task(wired.handlers._finish_approval_decision(ADMIN2_QQ, AID, "deny"))
            await asyncio.sleep(0.05)
            gate.set()
            return await asyncio.gather(t1, t2, return_exceptions=True)

        results = asyncio.run(scenario())
        texts = [exc.text for exc in results if isinstance(exc, _Finished)]
        assert len(calls) == 1
        assert any("已被处理" in t for t in texts)
        assert any("已同意审批" in t for t in texts)
        assert AID not in wired.state._adapter.approvals.pending

    def test_submit_exception_rolls_back(self, wired: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        wired.state._adapter.approvals.pending[AID] = {"operation": "read_file", "created_at": time.time()}

        async def approve(aid: str, decision: str, comment: str) -> bool:
            raise RuntimeError("boom")

        monkeypatch.setattr(wired.state._adapter, "client", SimpleNamespace(approve=approve))
        text = self._decide(wired, LISTED_QQ, AID, "allow")
        assert "提交审批失败" in text
        assert AID in wired.state._adapter.approvals.pending and AID not in wired.state._adapter.approvals.settled

    def test_supervisor_reject_rolls_back(self, wired: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        wired.state._adapter.approvals.pending[AID] = {"operation": "read_file", "created_at": time.time()}

        async def approve(aid: str, decision: str, comment: str) -> bool:
            return False

        monkeypatch.setattr(wired.state._adapter, "client", SimpleNamespace(approve=approve))
        text = self._decide(wired, LISTED_QQ, AID, "allow")
        assert text == "审批提交被 Supervisor 拒绝，请检查日志。"
        assert AID in wired.state._adapter.approvals.pending and AID not in wired.state._adapter.approvals.settled

    def test_success_receipt_carries_operation(self, wired: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        wired.state._adapter.approvals.pending[AID] = {"operation": "read_file", "created_at": time.time()}

        async def approve(aid: str, decision: str, comment: str) -> bool:
            return True

        monkeypatch.setattr(wired.state._adapter, "client", SimpleNamespace(approve=approve))
        text = self._decide(wired, LISTED_QQ, AID, "allow")
        assert f"已同意审批：{AID}" in text and "操作：read_file" in text
        assert AID not in wired.state._adapter.approvals.pending
        assert wired.state._adapter.approvals.settled[AID]["admin_id"] == str(LISTED_QQ)
        assert "已同意" in wired.approval._settled_approval_notice(AID)

    def test_second_decision_is_told_already_handled(self, wired: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        async def approve(aid: str, decision: str, comment: str) -> bool:
            return True

        monkeypatch.setattr(wired.state._adapter, "client", SimpleNamespace(approve=approve))
        wired.state._adapter.approvals.settle(AID, admin_id=LISTED_QQ, decision="allow", operation="read_file")
        text = self._decide(wired, ADMIN2_QQ, AID, "deny")
        assert "已被处理" in text
