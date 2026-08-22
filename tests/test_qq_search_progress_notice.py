"""联网搜索进度提示的回归测试。

判定曾经用 any() 扫整个 progress_records，而那是累积列表：搜索工具跑完之后记录还在，
条件就永远成立。实测一次 70 秒的任务里搜索只占 2.5 秒，剩下 50 多秒在跑 curl，
bot 却每 20 秒重复一遍「还在联网搜索中」，最后一条和答案同一秒发出。
"""
from __future__ import annotations

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

# 生产日志里的真实进度记录，含搜索的和不含的各取几条。
_SEARCH_RECORDS = [
    "[qq.orchestrator] 执行 3 个工具：exa_search、exa_search、execute_command",
    "[qq.orchestrator] exa_search: 已获得结果: query=site:alice.xfu.jp",
]
_COMMAND_RECORDS = [
    "[qq.orchestrator] execute_command: 命令完成 (rc=0): python3 -c ...",
    "[qq.orchestrator] 执行 1 个工具：execute_command",
]


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    return load_runtime(monkeypatch, tmp_path)


class _Bot:
    """记下每一条实际发出去的文本。"""

    def __init__(self) -> None:
        self.sent: list[str] = []


@pytest.fixture()
def capture(runtime: Any, monkeypatch: pytest.MonkeyPatch) -> _Bot:
    bot = _Bot()

    async def _send(_bot: Any, _target: Any, message: Any, **_kwargs: Any) -> int:
        bot.sent.append(str(message))
        return len(bot.sent)

    monkeypatch.setattr(runtime, "_send_qq_message", _send)
    return bot


async def _notice(runtime: Any, bot: _Bot, platform_data: dict[str, Any], *, at: float) -> None:
    await runtime._maybe_send_search_progress_notice(
        bot, {"type": "group", "group_id": 1, "conversation_key": "qq_group:x"}, platform_data,
    )


@pytest.fixture()
def clock(runtime: Any, monkeypatch: pytest.MonkeyPatch):
    """可控时钟。真等 20 秒会把测试拖成秒级。"""
    state = {"now": 1000.0}
    monkeypatch.setattr(runtime.time, "time", lambda: state["now"])
    return state


class TestMentionsSearch:
    @pytest.mark.parametrize("record", _SEARCH_RECORDS)
    def test_搜索记录认得出来(self, runtime: Any, record: str) -> None:
        assert runtime._progress_mentions_search(record) is True

    @pytest.mark.parametrize("record", _COMMAND_RECORDS)
    def test_跑命令的记录不算搜索(self, runtime: Any, record: str) -> None:
        assert runtime._progress_mentions_search(record) is False


@pytest.mark.asyncio
class TestNotice:
    async def test_首条立刻发(self, runtime: Any, capture: _Bot, clock: dict) -> None:
        data: dict[str, Any] = {}
        await _notice(runtime, capture, data, at=clock["now"])

        assert capture.sent == [runtime._SEARCH_PROGRESS_FIRST_NOTICE]

    async def test_二十秒内不追第二条(self, runtime: Any, capture: _Bot, clock: dict) -> None:
        data: dict[str, Any] = {}
        await _notice(runtime, capture, data, at=clock["now"])
        clock["now"] += 19.0
        await _notice(runtime, capture, data, at=clock["now"])

        assert len(capture.sent) == 1

    async def test_久等那条带上已等秒数(self, runtime: Any, capture: _Bot, clock: dict) -> None:
        data: dict[str, Any] = {}
        await _notice(runtime, capture, data, at=clock["now"])
        clock["now"] += 41.0
        await _notice(runtime, capture, data, at=clock["now"])

        assert "已等 41 秒" in capture.sent[1]

    async def test_久等那条一辈子只发一次(self, runtime: Any, capture: _Bot, clock: dict) -> None:
        data: dict[str, Any] = {}
        await _notice(runtime, capture, data, at=clock["now"])
        for _ in range(6):
            clock["now"] += 25.0
            await _notice(runtime, capture, data, at=clock["now"])

        # 首条 + 久等一条，再久也就这两条。
        assert len(capture.sent) == 2

    async def test_答案已经在投递就闭嘴(self, runtime: Any, capture: _Bot, clock: dict) -> None:
        data: dict[str, Any] = {}
        await _notice(runtime, capture, data, at=clock["now"])
        clock["now"] += 30.0
        data["_qq_final_reply_sent"] = True
        await _notice(runtime, capture, data, at=clock["now"])

        assert len(capture.sent) == 1


@pytest.mark.asyncio
class TestCurrentStageOnly:
    """判定要看当前在跑什么，不是这个任务历史上搜过网。走真实的 update_progress。"""

    @staticmethod
    async def _notified(runtime: Any, monkeypatch: pytest.MonkeyPatch, records: list[str]) -> bool:
        calls: list[int] = []

        async def _spy(*_args: Any, **_kwargs: Any) -> None:
            calls.append(1)

        monkeypatch.setattr(runtime, "_maybe_send_search_progress_notice", _spy)
        monkeypatch.setattr(runtime, "_target_from_platform_data", lambda _data: {"type": "group", "group_id": 1})

        event = SimpleNamespace(message_id=1)
        trigger = SimpleNamespace(
            platform_data={"event": event, "bot": object()},
            conversation_key="qq_group:x",
        )
        await runtime.TangQiuCallbacks().update_progress(
            trigger, SimpleNamespace(progress_records=list(records), stream_parts=[]),
        )
        return bool(calls)

    async def test_正在搜索时会提示(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        assert await self._notified(runtime, monkeypatch, _SEARCH_RECORDS) is True

    async def test_搜完转去跑命令就不再提示(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        # 这正是线上那次：搜索早在第 7 秒结束，之后 50 多秒都在 curl。
        assert await self._notified(runtime, monkeypatch, [*_SEARCH_RECORDS, *_COMMAND_RECORDS]) is False

    async def test_空记录不误报(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        assert await self._notified(runtime, monkeypatch, []) is False
