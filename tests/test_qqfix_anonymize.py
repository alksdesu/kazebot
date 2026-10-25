"""送给模型的文本里不出现真实 QQ 号：引用块、/生图 直达、第一次出现的发送者。

文本替换只换别名表里登记过的号，所以凡是这段文本牵涉的人都得先登记再替换。
"""
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

from _onebot_harness import load_runtime  # noqa: E402

NEWCOMER = "1234567890"
GROUP_ID = 700001


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    module = load_runtime(monkeypatch, tmp_path)

    async def no_get_msg(bot: Any, reply_message_id: Any) -> None:
        return None

    monkeypatch.setattr(module.message_text, "_get_reply_message", no_get_msg)
    return module


def _quote(runtime: Any, entry: dict[str, Any]) -> str:
    runtime.state._adapter.reply_message_cache["555"] = entry
    event = SimpleNamespace(reply=None, raw_message="[CQ:reply,id=555]")
    text, _ = asyncio.run(runtime.history._build_reply_context(event, SimpleNamespace(self_id="90000"), "qq_group:1"))
    return text


class TestQuotedMessages:
    @pytest.mark.parametrize("sender", [{"user_id": int(NEWCOMER)}, {"user_id": NEWCOMER, "card": "", "nickname": ""}, None])
    def test_a_nameless_unregistered_author_never_leaks(self, runtime: Any, sender: Any) -> None:
        text = _quote(runtime, {
            "message": None, "raw_message": f"我的号是 {NEWCOMER}", "sender": sender,
            "user_id": int(NEWCOMER), "time": 1_700_000_000,
        })

        alias = runtime.anonymize._anonymize_user_id(NEWCOMER)
        assert NEWCOMER not in text
        assert f"({alias})" in text
        assert f"我的号是 {alias}" in text

    def test_a_dict_author_with_a_card_shows_the_card(self, runtime: Any) -> None:
        text = _quote(runtime, {
            "message": None, "raw_message": "你好", "sender": {"user_id": NEWCOMER, "card": "老王", "nickname": "w"},
            "user_id": NEWCOMER, "time": 1_700_000_000,
        })

        assert "老王(" in text and NEWCOMER not in text


class TestInboundText:
    @staticmethod
    def _event(card: str) -> SimpleNamespace:
        return SimpleNamespace(
            user_id=int(NEWCOMER), group_id=GROUP_ID, message_id=7, time=1_700_000_000, anonymous=None,
            sender=SimpleNamespace(user_id=int(NEWCOMER), nickname="", card=card, role="member"),
            get_message=lambda: [],
        )

    def test_a_first_time_sender_whose_card_carries_the_number_is_masked(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def no_reply(event: Any, bot: Any, conversation_key: str) -> tuple[str, list]:
            return "", []

        monkeypatch.setattr(runtime.history, "_build_reply_context", no_reply)
        built = asyncio.run(runtime.inbound._build_inbound_text(
            self._event(f"小明{NEWCOMER}"), SimpleNamespace(self_id="90000"), f"我是 {NEWCOMER}", "qq_group:1", [],
        ))

        assert NEWCOMER not in built.text
        assert NEWCOMER not in built.merge_text

    @pytest.mark.parametrize("is_dm", [False, True])
    def test_the_draw_shortcut_masks_numbers_in_the_request(self, runtime: Any, is_dm: bool) -> None:
        runtime.anonymize._anonymize_user_id("2233445566")

        built = asyncio.run(runtime.inbound._build_draw_direct_inbound_text(
            self._event(""), "画 2233445566 的头像", is_dm,
        ))

        assert "2233445566" not in built.text
        assert built.merge_text in built.text
        assert runtime.anonymize._anonymize_user_id("2233445566") in built.merge_text


class TestRegisterThenSubstitute:
    def test_the_alias_book_registers_before_it_substitutes(self, runtime: Any) -> None:
        book = runtime.state._adapter.aliases

        text = book.substitute(f"联系 {NEWCOMER} 或群 {GROUP_ID}", users=[NEWCOMER], groups=[str(GROUP_ID)])

        assert NEWCOMER not in text and str(GROUP_ID) not in text
        assert book.user_alias_of(NEWCOMER) in text and book.group_alias_of(str(GROUP_ID)) in text

    def test_placeholder_and_bogus_ids_are_not_registered(self, runtime: Any) -> None:
        book = runtime.state._adapter.aliases

        text = runtime.anonymize._anonymize_text_for_ai("80000000 和 123", user_ids=("80000000", "123", ""))

        assert text == "80000000 和 123"
        assert book.user_alias_of("80000000") is None
        assert book.user_alias_of("123") is None

    def test_history_lines_register_the_sender_first(self, runtime: Any) -> None:
        line = runtime.history._compose_history_line(
            timestamp=1_700_000_000, sender=None, sender_id=NEWCOMER, text=f"我是 {NEWCOMER}",
        )

        assert NEWCOMER not in line

    @pytest.mark.parametrize("sender,expected", [
        ({"card": "老王", "nickname": "w"}, "老王"),
        ({"nickname": "小李"}, "小李"),
        (SimpleNamespace(card="", nickname="阿强"), "阿强"),
    ])
    def test_display_names_read_dict_and_object_senders(self, runtime: Any, sender: Any, expected: str) -> None:
        assert runtime.identity._sender_display_name(sender, NEWCOMER) == expected

    def test_a_nameless_sender_falls_back_to_the_alias(self, runtime: Any) -> None:
        name = runtime.identity._sender_display_name({}, NEWCOMER)

        assert name == runtime.anonymize._anonymize_user_id(NEWCOMER)
