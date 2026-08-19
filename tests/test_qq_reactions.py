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


# ---- #25 白名单不变式（本步随 #26 一起落地）----

def test_model_and_stage_emojis_never_overlap(runtime: Any) -> None:
    assert set(runtime._REACT_MODEL_EMOJIS) & set(runtime._REACT_CLEANUP_EMOJIS) == set()


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


# ---- #23: received 阶段退役 ----

def test_received_stage_is_gone(runtime: Any) -> None:
    assert "received" not in runtime._REACT_STAGE_EMOJIS
    assert "76" not in runtime._REACT_CLEANUP_EMOJIS


def test_readme_stage_table_matches_implementation(runtime: Any) -> None:
    text = README_PATH.read_text(encoding="utf-8")
    rows = re.findall(r"^\|\s*(\w+)\s*\|\s*(\d+)\s*\|", text, re.MULTILINE)
    parsed = {stage: emoji for stage, emoji in rows}

    assert parsed == runtime._REACT_STAGE_EMOJIS


def test_process_group_message_uses_the_stage_table(
    runtime: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_live_config(runtime, enable_reactions=True)
    bot = ReactBot()
    reacts: list[str] = []

    async def record_react(_bot: Any, _event: Any, emoji_id: str, _enabled: bool) -> bool:
        reacts.append(emoji_id)
        return True

    async def noop(*args: Any, **kwargs: Any) -> Any:
        return None

    monkeypatch.setattr(runtime, "_client", SimpleNamespace(), raising=False)
    monkeypatch.setattr(runtime, "_session_state", SimpleNamespace(), raising=False)
    monkeypatch.setattr(runtime, "_set_message_react", record_react)
    monkeypatch.setattr(runtime, "_remember_message_for_reply_context", lambda *a, **k: None)
    monkeypatch.setattr(runtime, "_event_text_with_forward", _resolved("hi"))
    monkeypatch.setattr(runtime, "_is_direct_bot_interaction", lambda *a, **k: True)
    monkeypatch.setattr(runtime, "_strip_trigger_prefix", lambda text: text)
    monkeypatch.setattr(runtime, "_auto_like_user", noop)
    monkeypatch.setattr(runtime, "_collect_qq_attachments", _resolved(([], [])))
    monkeypatch.setattr(runtime, "_remember_recent_images", lambda *a, **k: None)
    for name in (
        "_maybe_handle_clear_group_memory_command", "_maybe_handle_model_command",
        "_maybe_handle_drawtools_command", "_maybe_handle_custom_face_command",
        "_maybe_handle_proactive_command", "_merge_recent_images_after_text",
    ):
        monkeypatch.setattr(runtime, name, noop)
    monkeypatch.setattr(runtime, "_record_group_message", lambda *a, **k: 1)
    monkeypatch.setattr(runtime, "_parse_direct_draw_command", lambda text: None)
    monkeypatch.setattr(runtime, "_build_inbound_text", _resolved(("text", -1)))
    monkeypatch.setattr(runtime, "_apply_attachment_hints", lambda text, *a, **k: text)
    monkeypatch.setattr(runtime, "_enqueue_or_submit_inbound", _resolved(True))
    # 用哨兵改写阶段表，证明提交处读的是表而不是写死的字面量。
    monkeypatch.setitem(runtime._REACT_STAGE_EMOJIS, "submitted", "999")

    event = SimpleNamespace(user_id=USER_QQ, group_id=HOME_GROUP, message_id=42, get_message=lambda: [])
    matcher = SimpleNamespace(finish=noop)
    asyncio.run(runtime._process_group_message(bot, event, matcher))

    assert reacts == ["999"]


def _resolved(value: Any):
    async def call(*args: Any, **kwargs: Any) -> Any:
        return value
    return call
