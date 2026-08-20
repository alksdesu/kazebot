"""群复读跟读：难的不是判「一样」，是那几条不该跟的边界。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_spec = importlib.util.spec_from_file_location(
    "_test_echo_policy", _ROOT / "adapters" / "onebot" / "echo_policy.py",
)
assert _spec and _spec.loader
echo = importlib.util.module_from_spec(_spec)
sys.modules["_test_echo_policy"] = echo
_spec.loader.exec_module(echo)

E = echo.EchoEntry


def _run(entries, *, threshold=3, now=100.0, window=120.0, already=""):
    return echo.detect_echo(
        entries, threshold=threshold, now=now,
        max_age_seconds=window, already_echoed=already,
    )


class TestEchoableText:
    @pytest.mark.parametrize("text", ["+1", "哈哈哈", "666", "ok"])
    def test_short_chatter_is_echoable(self, text: str) -> None:
        assert echo.is_echoable_text(text, max_length=30)

    def test_a_command_is_never_echoed(self) -> None:
        # 跟着复读等于替别人把命令又发一遍。
        assert not echo.is_echoable_text("/清除群记忆", max_length=30)
        assert not echo.is_echoable_text("／帮助", max_length=30)

    def test_mentions_and_replies_are_never_echoed(self) -> None:
        # 指向别人的东西复读出去就指错人了。
        assert not echo.is_echoable_text("[CQ:at,qq=123] 来", max_length=30)
        assert not echo.is_echoable_text("[CQ:reply,id=9] 对", max_length=30)

    def test_a_bare_placeholder_is_not_content(self) -> None:
        # 三个人各发一张不同的图，正文都会渲染成 [图片]。
        assert not echo.is_echoable_text("[图片]", max_length=30)
        assert not echo.is_echoable_text("[表情包]", max_length=30)

    def test_long_text_is_refused(self) -> None:
        assert not echo.is_echoable_text("x" * 31, max_length=30)

    def test_empty_is_refused(self) -> None:
        assert not echo.is_echoable_text("   ", max_length=30)


class TestDetect:
    def test_three_different_people_trigger_it(self) -> None:
        entries = [E("txt:+1", "a", 98.0), E("txt:+1", "b", 99.0), E("txt:+1", "c", 100.0)]
        assert _run(entries) == "txt:+1"

    def test_one_person_spamming_is_not_an_echo(self) -> None:
        # 同一个人连刷三条是刷屏，跟上去只会火上浇油。
        entries = [E("txt:+1", "a", 98.0), E("txt:+1", "a", 99.0), E("txt:+1", "a", 100.0)]
        assert _run(entries) == ""

    def test_two_people_are_not_enough_at_threshold_three(self) -> None:
        entries = [E("txt:+1", "a", 98.0), E("txt:+1", "b", 99.0), E("txt:+1", "a", 100.0)]
        assert _run(entries) == ""

    def test_a_different_last_message_breaks_the_chain(self) -> None:
        entries = [E("txt:+1", "a", 98.0), E("txt:+1", "b", 99.0), E("txt:别的", "c", 100.0)]
        assert _run(entries) == ""

    def test_it_only_looks_at_the_tail(self) -> None:
        entries = [
            E("txt:旧", "z", 90.0),
            E("txt:+1", "a", 98.0), E("txt:+1", "b", 99.0), E("txt:+1", "c", 100.0),
        ]
        assert _run(entries) == "txt:+1"

    def test_stale_messages_do_not_count(self) -> None:
        entries = [E("txt:+1", "a", 1.0), E("txt:+1", "b", 2.0), E("txt:+1", "c", 100.0)]
        assert _run(entries, window=10.0) == ""

    def test_it_does_not_echo_the_same_thing_twice(self) -> None:
        # 跟过一次就打住，否则人再补一条又凑够数，Bot 会一路接力。
        entries = [E("txt:+1", "a", 98.0), E("txt:+1", "b", 99.0), E("txt:+1", "c", 100.0)]
        assert _run(entries, already="txt:+1") == ""

    def test_a_new_phrase_after_an_echo_still_works(self) -> None:
        entries = [E("txt:666", "a", 98.0), E("txt:666", "b", 99.0), E("txt:666", "c", 100.0)]
        assert _run(entries, already="txt:+1") == "txt:666"

    def test_stickers_compare_by_id_not_by_placeholder(self) -> None:
        same = [E("img:aaa", "a", 98.0), E("img:aaa", "b", 99.0), E("img:aaa", "c", 100.0)]
        assert _run(same) == "img:aaa"
        different = [E("img:aaa", "a", 98.0), E("img:bbb", "b", 99.0), E("img:ccc", "c", 100.0)]
        assert _run(different) == ""

    def test_threshold_two_is_honoured(self) -> None:
        entries = [E("txt:+1", "a", 99.0), E("txt:+1", "b", 100.0)]
        assert _run(entries, threshold=2) == "txt:+1"

    def test_an_empty_key_never_matches(self) -> None:
        entries = [E("", "a", 98.0), E("", "b", 99.0), E("", "c", 100.0)]
        assert _run(entries) == ""

    def test_not_enough_history(self) -> None:
        assert _run([E("txt:+1", "a", 100.0)]) == ""
        assert _run([]) == ""
