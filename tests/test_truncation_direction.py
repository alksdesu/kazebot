"""两处「文本超长截断」的方向必须各自朝对的一边。

送压缩器的文本要保头：被丢掉的正是马上要被删、最需要被总结的最老内容。
送提取器的 transcript 要保尾：它的任务是「总结刚刚发生了什么」，而原来是正序累加、
超了就 break，丢掉当条及其后全部，于是一个长回合里它只看得到最早的部分。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.builtin.memory_extract import _format_transcript_for_extract  # noqa: E402


def _msg(role: str, content: str, **extra) -> dict:
    return {"role": role, "content": content, **extra}


class TestExtractorKeepsTheRecentTail:
    def test_the_newest_turn_survives_a_budget_overflow(self) -> None:
        messages = [
            _msg("user", "最早的问题 " + "填充" * 400),
            _msg("assistant", "最早的回答 " + "填充" * 400),
            _msg("user", "最新的问题"),
            _msg("assistant", "最新的回答"),
        ]

        text = _format_transcript_for_extract(messages, max_chars=200)

        assert "最新的回答" in text
        assert "最早的问题" not in text

    def test_nothing_after_the_overflow_is_silently_dropped(self) -> None:
        """正序 break 的后果是「超长那条之后的全部消息」一起消失，而不只是它自己。"""
        messages = [
            _msg("user", "长消息 " + "填充" * 400),
            _msg("user", "其后第一条"),
            _msg("user", "其后第二条"),
            _msg("user", "其后第三条"),
        ]

        text = _format_transcript_for_extract(messages, max_chars=300)

        assert "其后第一条" in text
        assert "其后第二条" in text
        assert "其后第三条" in text

    def test_chronological_order_is_preserved(self) -> None:
        messages = [_msg("user", f"第{i}条") for i in range(1, 5)]

        text = _format_transcript_for_extract(messages, max_chars=10000)

        assert text.index("第1条") < text.index("第2条") < text.index("第3条") < text.index("第4条")

    def test_a_single_oversized_message_is_still_returned(self) -> None:
        # 一条就超预算时不能返回空串：那会让整轮提取无输入可看。
        text = _format_transcript_for_extract([_msg("user", "填充" * 4000)], max_chars=10)

        assert text.strip()

    def test_everything_fits_when_under_budget(self) -> None:
        messages = [_msg("user", "问题"), _msg("assistant", "回答")]

        text = _format_transcript_for_extract(messages, max_chars=10000)

        assert "问题" in text
        assert "回答" in text

    def test_system_messages_are_still_excluded(self) -> None:
        messages = [_msg("system", "你是助手"), _msg("user", "问题")]

        text = _format_transcript_for_extract(messages, max_chars=10000)

        assert "你是助手" not in text
        assert "问题" in text

    def test_a_tool_result_keeps_its_head(self) -> None:
        """工具结果保头是对的：开头才是返回状态和报错。"""
        messages = [_msg("tool", "ERROR: not found" + "x" * 2000, name="read_file")]

        text = _format_transcript_for_extract(messages, max_chars=10000)

        assert "ERROR: not found" in text
        assert "<truncated>" in text

    def test_an_empty_message_list_yields_an_empty_transcript(self) -> None:
        assert _format_transcript_for_extract([], max_chars=10000) == ""


class TestCompactorKeepsTheOldHead:
    def test_the_compactor_input_keeps_the_beginning(self) -> None:
        """方向与提取器相反，而且这两处以前都是同一个错误方向。"""
        import inspect

        from engine.builtin import compact as compact_mod

        source = inspect.getsource(compact_mod._build_compactor_input)

        # 保头是 [:limit]；保尾会写成 [-limit:]
        assert "_PTL_MAX_CHARS]" in source
        assert "-_PTL_MAX_CHARS:" not in source
