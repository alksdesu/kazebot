"""QQ 表情反应：阶段机字段、模型表态白名单与开关收口。

#23 received 阶段从未被贴过，删字段并让 README 与实现对齐。
#26 模型表态 add_reactions 必须复用 _set_message_react，关掉 enable_reactions 后完全静默。
"""
from __future__ import annotations

import asyncio
import re
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

HOME_GROUP = 700001
USER_QQ = 30003
README_PATH = _ROOT / "adapters" / "onebot" / "README.md"


class ReactBot:
    def __init__(self) -> None:
        self.self_id = "10000"
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_api(self, api: str, **kwargs: Any) -> Any:
        self.calls.append((api, kwargs))
        return None


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    return load_runtime(monkeypatch, tmp_path)


def _event() -> SimpleNamespace:
    return SimpleNamespace(
        user_id=USER_QQ, group_id=HOME_GROUP, message_id=42, sender=SimpleNamespace(role="member"),
    )


def _trigger(runtime: Any, bot: Any, event: Any) -> Any:
    return runtime.TriggerInfo(
        inbound_seq=1, conversation_key="k", session_id="s", is_dm=False,
        platform_data={"bot": bot, "event": event},
    )


def _run_add(runtime: Any, bot: Any, reactions: list[str]) -> None:
    trigger = _trigger(runtime, bot, _event())
    asyncio.run(runtime.TangQiuCallbacks().add_reactions(trigger, reactions))


# ---- #26: add_reactions 开关 + 白名单收口 ----

def test_reactions_off_stays_completely_silent(runtime: Any) -> None:
    set_live_config(runtime, enable_reactions=False)
    bot = ReactBot()

    _run_add(runtime, bot, ["66"])

    assert bot.calls == []


def test_reactions_on_sticks_the_whitelisted_face(runtime: Any) -> None:
    set_live_config(runtime, enable_reactions=True)
    bot = ReactBot()

    _run_add(runtime, bot, ["66"])

    assert len(bot.calls) == 1
    api, kwargs = bot.calls[0]
    assert api == "set_msg_emoji_like"
    assert kwargs["emoji_id"] == "66"
    assert kwargs["set"] is True


def test_reactions_drop_ids_outside_the_whitelist(runtime: Any) -> None:
    set_live_config(runtime, enable_reactions=True)
    bot = ReactBot()

    _run_add(runtime, bot, ["999999", " 66 "])

    assert [kwargs["emoji_id"] for _api, kwargs in bot.calls] == ["66"]



# ---- #25 提示词通路 ----

def test_reaction_prompt_lists_whole_whitelist_when_enabled(runtime: Any) -> None:
    set_live_config(runtime, enable_reactions=True)

    block = runtime._reaction_prompt_block()

    assert "[REACT:" in block
    for eid in runtime._REACT_MODEL_EMOJIS:
        assert eid in block


def test_reaction_prompt_is_empty_when_disabled(runtime: Any) -> None:
    set_live_config(runtime, enable_reactions=False)

    assert runtime._reaction_prompt_block() == ""


def _group_event() -> SimpleNamespace:
    return SimpleNamespace(
        user_id=USER_QQ, group_id=HOME_GROUP, message_id=42, time=1_700_000_000,
        sender=SimpleNamespace(nickname="某人", card="", user_id=USER_QQ, role="member"),
        get_message=lambda: [],
    )


def test_group_inbound_injects_reaction_prompt(runtime: Any) -> None:
    set_live_config(runtime, enable_reactions=True)
    bot = ReactBot()

    text, _ = asyncio.run(runtime._build_inbound_text(
        _group_event(), bot, "在吗", "conv_abc", [],
    ))

    assert "【QQ表情表态】" in text


def test_private_inbound_omits_reaction_prompt(runtime: Any) -> None:
    set_live_config(runtime, enable_reactions=True)
    bot = ReactBot()
    event = SimpleNamespace(
        user_id=USER_QQ, message_id=7,
        sender=SimpleNamespace(nickname="某人", card="", user_id=USER_QQ),
        get_message=lambda: [],
    )

    text = asyncio.run(runtime._build_private_inbound_text(event, bot, "在吗", "conv_dm", []))

    assert "【QQ表情表态】" not in text


def test_orchestrator_nodes_teach_react(runtime: Any) -> None:
    base = _ROOT / "config" / "nodes"
    for name in ("qq.orchestrator.yaml", "qq.orchestrator.example.yaml"):
        assert "[REACT:" in (base / name).read_text(encoding="utf-8")


# ---- 模型表态的数量硬约束 ----

def _react_probe(runtime: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, list[str]]:
    set_live_config(runtime, enable_reactions=True)
    stuck: list[str] = []

    async def record(_bot: Any, _event: Any, emoji_id: str, _enabled: bool) -> bool:
        stuck.append(emoji_id)
        return True

    monkeypatch.setattr(runtime, "_set_message_react", record)
    callbacks = runtime.TangQiuCallbacks()
    trigger = SimpleNamespace(platform_data={"bot": ReactBot(), "event": _group_event()})
    return callbacks, trigger, stuck


def test_model_reaction_is_capped_at_one(runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """提示词只是请求「最多一个」，守不守得住要看代码。"""
    callbacks, trigger, stuck = _react_probe(runtime, monkeypatch)

    asyncio.run(callbacks.add_reactions(trigger, ["4", "14", "182"]))

    assert stuck == ["4"]


def test_second_reaction_pass_on_same_message_is_ignored(
    runtime: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """中间回复与最终回复各解析一次，同一条触发消息不该被贴两回。"""
    callbacks, trigger, stuck = _react_probe(runtime, monkeypatch)

    asyncio.run(callbacks.add_reactions(trigger, ["4"]))
    asyncio.run(callbacks.add_reactions(trigger, ["66"]))

    assert stuck == ["4"]


def test_ids_outside_whitelist_never_reach_the_api(
    runtime: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    callbacks, trigger, stuck = _react_probe(runtime, monkeypatch)

    asyncio.run(callbacks.add_reactions(trigger, ["999", "4"]))

    assert stuck == ["4"]

def _resolved(value: Any):
    async def call(*args: Any, **kwargs: Any) -> Any:
        return value
    return call
