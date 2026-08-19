"""Discord 频道历史溢出不静默：被挤掉的未送达行要在 prompt 里报出来。

频道历史是定长队列，水位长期不推进时挤掉的行从来没进过 Clonoth 侧的 durable history，
prompt 里既看不到内容也看不到痕迹，模型会把断掉的一段聊天当连续的。push 逻辑在
context.py 与 messaging.py 各有一份，两份都必须记账，否则 Bot 自己的回复行仍会静默掉。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests._discord_harness import install_discord_stub  # noqa: E402

install_discord_stub()

_ROOT = _REPO_ROOT / "adapters" / "discord"
_PACKAGE_NAME = "_discord_history_adapter"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(
        f"{_PACKAGE_NAME}.{name}" if name else _PACKAGE_NAME,
        path,
        submodule_search_locations=[str(_ROOT)] if not name else None,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


if _PACKAGE_NAME not in sys.modules:
    _load("", _ROOT / "__init__.py")
    for _name in ("history", "context", "messaging", "send_contract", "callbacks"):
        _load(_name, _ROOT / f"{_name}.py")

history = sys.modules[f"{_PACKAGE_NAME}.history"]
context = sys.modules[f"{_PACKAGE_NAME}.context"]
messaging = sys.modules[f"{_PACKAGE_NAME}.messaging"]
callbacks = sys.modules[f"{_PACKAGE_NAME}.callbacks"]

_CHANNEL_ID = 4242


class _SessionState:
    def __init__(self) -> None:
        self.watermarks: dict[int, int] = {}

    def get_high_watermark(self, channel_id: int) -> int:
        return self.watermarks.get(int(channel_id), -1)


class _Member:
    id = 10001
    display_name = "阿绫"
    name = "aling"


@pytest.fixture()
def rt(tmp_path: Path) -> Any:
    return SimpleNamespace(
        workspace=tmp_path,
        channel_history={},
        history_seq_counter=0,
        history_max_len=3,
        history_gap={},
        session_state=_SessionState(),
        superusers=set(),
    )


def _fill(rt: Any, count: int, *, first: int = 0) -> None:
    for index in range(first, first + count):
        context._push_history(rt, _CHANNEL_ID, f"line{index}")


def _history_block(rt: Any) -> list[str]:
    text = context._build_context_text(rt, _CHANNEL_ID, _Member(), "在吗")
    lines = text.splitlines()
    start = lines.index("【群聊上下文记录】") + 1
    out: list[str] = []
    for line in lines[start:]:
        if not line or line.startswith("【") or line.startswith("当前时间"):
            break
        out.append(line)
    return out


class TestOverflowIsNotSilent:
    def test_evicting_an_undelivered_line_is_reported(self, rt) -> None:
        _fill(rt, 5)

        block = _history_block(rt)

        assert block[0] == "（此前 2 条消息超出缓存上限，未包含在内）"
        assert block[1:] == ["line2", "line3", "line4"]

    def test_a_delivered_line_falling_out_is_not_reported(self, rt) -> None:
        # seq 从 0 开始，三行送出去之后水位是 2。
        _fill(rt, 3)
        rt.session_state.watermarks[_CHANNEL_ID] = 2
        _fill(rt, 2, first=3)

        assert _history_block(rt) == ["line3", "line4"]

    def test_the_notice_disappears_once_the_watermark_passes_the_gap(self, rt) -> None:
        _fill(rt, 5)
        rt.session_state.watermarks[_CHANNEL_ID] = 5

        assert _history_block(rt) == ["（无新消息）"]

    def test_a_bot_reply_pushing_a_line_out_is_reported_too(self, rt) -> None:
        # messaging.py 自己复制了一份 push，漏掉它，Bot 一开口缺口就重新变静默。
        _fill(rt, 3)
        messaging._record_bot_reply(rt, _CHANNEL_ID, "好的")

        assert history.lost(rt, _CHANNEL_ID, -1) == 1
        assert _history_block(rt)[0].startswith("（此前 1 条")


class TestGapLifecycle:
    def test_a_context_reset_clears_the_gap(self, rt) -> None:
        # SDK 的 reset 两条分支都重置水位，按旧水位算的缺口不再成立。
        _fill(rt, 5)

        asyncio.run(callbacks.EreunaCallbacks(rt).on_context_reset(
            f"discord:{_CHANNEL_ID}", "compact", [],
        ))

        assert rt.history_gap == {}
        assert not _history_block(rt)[0].startswith("（此前")

    def test_an_empty_channel_still_reads_as_empty(self, rt) -> None:
        assert _history_block(rt) == ["（暂无历史）"]
