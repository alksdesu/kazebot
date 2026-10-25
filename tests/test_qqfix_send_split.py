"""QQ 出站：分段幂等身份、分条上限、@全体成员的门槛。"""
from __future__ import annotations

import asyncio
import sys
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
from adapters._shared import outbound  # noqa: E402

GROUP_ID = 700001
ADMIN_QQ = 10001
MEMBER_QQ = 30003
MESSAGE_ID = 99


class _Bot:
    self_id = "10000"

    def __init__(self, members: list[dict[str, Any]] | None = None, *, members_fail: bool = False) -> None:
        self.members = members if members is not None else [
            {"user_id": ADMIN_QQ, "nickname": "管理员", "card": ""},
            {"user_id": MEMBER_QQ, "nickname": "群员", "card": ""},
        ]
        self.members_fail = members_fail
        self.sent: list[list[dict[str, Any]]] = []

    async def call_api(self, api: str, **kwargs: Any) -> Any:
        if api == "get_group_member_list":
            if self.members_fail:
                raise RuntimeError("NapCat timeout")
            return self.members
        raise AssertionError(f"unexpected api {api}")

    async def send_group_msg(self, *, group_id: int, message: Any) -> dict[str, str]:
        self.sent.append([dict(segment) for segment in message])
        return {"message_id": str(len(self.sent))}

    async def send_private_msg(self, *, user_id: int, message: Any) -> dict[str, str]:
        self.sent.append([dict(segment) for segment in message])
        return {"message_id": str(len(self.sent))}


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    module = load_runtime(monkeypatch, tmp_path)
    set_live_config(module, reply_to_trigger=True, admin_users=frozenset({ADMIN_QQ}))
    yield module
    module.state._adapter.outbound_idempotency.close()


def _target(**extra: Any) -> dict[str, Any]:
    return {"type": "group", "group_id": GROUP_ID, "conversation_key": f"qq_group:{GROUP_ID}", **extra}


def _context(runtime: Any) -> Any:
    return runtime.state.OutboundSendContext(
        event_id="evt-1", idempotency_key="delivery:evt-1", conversation_key=f"qq_group:{GROUP_ID}",
    )


def _texts(sent: list[list[dict[str, Any]]]) -> list[str]:
    return ["".join(seg["data"].get("text", "") for seg in row if seg["type"] == "text").strip() for row in sent]


def _ats(row: list[dict[str, Any]]) -> list[str]:
    return [str(seg["data"].get("qq")) for seg in row if seg["type"] == "at"]


def _event(user_id: int, *, anonymous: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user_id, group_id=GROUP_ID, message_id=MESSAGE_ID, anonymous=anonymous,
        sender=SimpleNamespace(user_id=user_id, nickname="n", card="", role="member"),
        get_message=lambda: [],
    )


class TestSegmentIdentity:
    def test_a_replay_without_the_trigger_does_not_resend_plain_text(self, runtime: Any) -> None:
        # 第一次带引用 + @ 发出；重放时 trigger 已经没了，渲染结果不同，但仍是同一段话。
        bot = _Bot()
        quoted = _target(reply_message_id=MESSAGE_ID, reply_sender_id=MEMBER_QQ)
        asyncio.run(runtime.sending._send_split_text(bot, quoted, "第一段[SPLIT]第二段", send_context=_context(runtime)))
        assert len(bot.sent) == 2

        asyncio.run(runtime.sending._send_split_text(bot, _target(), "第一段[SPLIT]第二段", send_context=_context(runtime)))

        assert len(bot.sent) == 2

    def test_the_same_words_at_another_position_are_a_different_part(self, runtime: Any) -> None:
        bot = _Bot()

        asyncio.run(runtime.sending._send_split_text(bot, _target(), "收到[SPLIT]收到", send_context=_context(runtime)))

        assert _texts(bot.sent) == ["收到", "收到"]


class TestPartLimit:
    @staticmethod
    def _parts(count: int) -> str:
        return outbound.SPLIT_MARKER.join(f"第{i}条" for i in range(1, count + 1))

    def test_the_default_caps_a_reply_at_eight_messages(self, runtime: Any) -> None:
        bot = _Bot()

        asyncio.run(runtime.sending._send_split_text(bot, _target(), self._parts(11), send_context=_context(runtime)))

        texts = _texts(bot.sent)
        assert runtime.state.live.max_reply_parts == 8
        assert len(texts) == 8
        assert texts[-1] == "第8条\n第9条\n第10条\n第11条"

    @pytest.mark.parametrize("limit,expected", [(3, 3), (0, 11), (1, 1)])
    def test_the_operator_sets_the_cap(self, runtime: Any, limit: int, expected: int) -> None:
        set_live_config(runtime, max_reply_parts=limit)
        bot = _Bot()

        asyncio.run(runtime.sending._send_split_text(bot, _target(), self._parts(11), send_context=_context(runtime)))

        assert len(bot.sent) == expected
        assert " ".join(_texts(bot.sent)).split() == [f"第{i}条" for i in range(1, 12)]

    def test_the_guide_promises_the_same_cap_the_sender_applies(self, runtime: Any) -> None:
        set_live_config(runtime, max_reply_parts=3)
        assert "最多 3 条，多出来的会并进最后一条。" in runtime.output_guide._split_section().text

        set_live_config(runtime, max_reply_parts=0)
        assert "最多" not in runtime.output_guide._split_section().text

        set_live_config(runtime, max_reply_parts=1)
        assert runtime.output_guide._split_section() is None

    def test_the_strictest_of_platform_and_operator_wins(self) -> None:
        assert outbound.strictest_limit(0, 8) == 8
        assert outbound.strictest_limit(3, 8) == 3
        assert outbound.strictest_limit(0, 0) == 0
        assert outbound.strictest_limit(5, 0) == 5


class TestEveryoneMention:
    @staticmethod
    def _message(runtime: Any) -> Any:
        segment = runtime.state.MessageSegment
        return runtime.state.Message([segment.at("all"), segment.text(" 开会")])

    def test_without_permission_at_all_becomes_plain_text(self, runtime: Any) -> None:
        cleaned = asyncio.run(runtime.sending._sanitize_group_at_segments(_Bot(), GROUP_ID, self._message(runtime)))

        assert _ats(list(cleaned)) == []
        assert "@全体成员" in "".join(seg.data.get("text", "") for seg in cleaned)

    def test_with_permission_at_all_is_kept(self, runtime: Any) -> None:
        cleaned = asyncio.run(runtime.sending._sanitize_group_at_segments(
            _Bot(), GROUP_ID, self._message(runtime), allow_everyone=True,
        ))

        assert _ats(list(cleaned)) == ["all"]

    def test_the_rule_holds_even_when_the_member_list_is_unavailable(self, runtime: Any) -> None:
        cleaned = asyncio.run(runtime.sending._sanitize_group_at_segments(
            _Bot(members_fail=True), GROUP_ID, self._message(runtime),
        ))

        assert _ats(list(cleaned)) == []

    def test_a_member_literally_named_all_is_mentioned_instead(self, runtime: Any) -> None:
        bot = _Bot([{"user_id": 55555, "nickname": "all", "card": ""}])

        cleaned = asyncio.run(runtime.sending._sanitize_group_at_segments(bot, GROUP_ID, self._message(runtime)))

        assert _ats(list(cleaned)) == ["55555"]

    @pytest.mark.parametrize("user_id,anonymous,allowed", [
        (ADMIN_QQ, None, True),
        (MEMBER_QQ, None, False),
        (ADMIN_QQ, {"id": 1, "name": "匿名", "flag": "f"}, False),
    ])
    def test_only_a_listed_admin_trigger_unlocks_it(self, runtime: Any, user_id: int, anonymous: Any, allowed: bool) -> None:
        target = runtime.queueing._target_from_platform_data({
            "type": "group", "group_id": GROUP_ID, "event": _event(user_id, anonymous=anonymous),
            "conversation_key": f"qq_group:{GROUP_ID}",
        })

        assert runtime.sending._may_mention_everyone(target) is allowed

    @pytest.mark.parametrize("user_id,expected", [(ADMIN_QQ, ["all"]), (MEMBER_QQ, [])])
    def test_the_reply_path_applies_the_trigger_rule(self, runtime: Any, user_id: int, expected: list[str]) -> None:
        bot = _Bot()
        target = runtime.queueing._target_from_platform_data({
            "type": "group", "group_id": GROUP_ID, "event": _event(user_id),
            "conversation_key": f"qq_group:{GROUP_ID}",
        })

        asyncio.run(runtime.sending._send_qq_message(bot, target, self._message(runtime), send_context=_context(runtime)))

        assert _ats(bot.sent[-1]) == expected

    def test_proactive_targets_carry_the_flag_only_for_groups(self, runtime: Any) -> None:
        group = runtime.models.ProactiveTarget("group", GROUP_ID, "群")
        private = runtime.models.ProactiveTarget("private", MEMBER_QQ, "人")

        assert runtime.sending._may_mention_everyone(runtime.proactive._target_to_send_dict(group, mention_everyone=True))
        assert not runtime.sending._may_mention_everyone(runtime.proactive._target_to_send_dict(group))
        assert runtime.sending.MENTION_EVERYONE_FLAG not in runtime.proactive._target_to_send_dict(private, mention_everyone=True)
