"""QQ 这一侧的表情包收集：哪些段算 QQ 标了表情包、哪些群收、交给收集器的来源是什么。"""
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

from _onebot_harness import load_runtime, set_live_config  # noqa: E402
from adapters.onebot.segments import any_marked_segment, marked_as_sticker  # noqa: E402
from platforms.descriptor import CHANNEL_GROUP  # noqa: E402
from platforms.inbound import InboundConversation, InboundFacts, InboundSender  # noqa: E402

_IMAGE = {"type": "image", "path": "data/attachments/a.png"}


class TestMarks:
    @pytest.mark.parametrize("seg_type", ["mface", "marketface", "MFace"])
    def test_sticker_segment_types_are_marked(self, seg_type: str) -> None:
        assert marked_as_sticker(seg_type, {})

    def test_sub_type_one_is_marked(self) -> None:
        # 群友发收藏表情时段类型仍是 image，只有 sub_type 能区分。
        assert marked_as_sticker("image", {"sub_type": 1})
        assert marked_as_sticker("image", {"subType": "1"})

    def test_plain_image_is_not_marked(self) -> None:
        assert not marked_as_sticker("image", {})
        assert not marked_as_sticker("image", {"sub_type": 0})
        assert not marked_as_sticker("image", {"sub_type": "not a number"})

    def test_summary_and_emoji_id_are_marked(self) -> None:
        assert marked_as_sticker("image", {"summary": "[表情]"})
        assert marked_as_sticker("image", {"summary": "[Sticker]"})
        assert marked_as_sticker("image", {"emoji_id": "abc123"})
        assert not marked_as_sticker("image", {"emoji_id": "   "})

    def test_one_marked_segment_marks_the_message(self) -> None:
        assert any_marked_segment([("text", {"text": "哈"}), ("image", {"sub_type": 1})])
        assert not any_marked_segment([("text", {"text": "哈"}), ("image", {})])


class _Collector:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def submit(self, attachments: Any, **kwargs: Any) -> int:
        self.calls.append({"attachments": list(attachments), **kwargs})
        return len(self.calls)


@pytest.fixture()
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    module = load_runtime(monkeypatch, tmp_path)
    set_live_config(module, sticker_collect=True)
    yield module
    module.state._adapter.outbound_idempotency.close()


@pytest.fixture()
def collector(runtime: Any, monkeypatch: pytest.MonkeyPatch) -> _Collector:
    recorder = _Collector()
    monkeypatch.setattr(runtime.emotes, "_sticker_collector_handle", lambda: recorder)
    return recorder


def _facts(group_id: Any, *, marked: bool = False) -> InboundFacts:
    return InboundFacts(
        sender=InboundSender(platform_id="30003", alias="UserA"),
        conversation=InboundConversation(
            platform="qq", channel="qq_group", kind=CHANNEL_GROUP, raw_id=group_id,
            conversation_key="qq_group:5f3c2a108d4e4b7a9c210e6f", real_conversation_key=f"qq_group:{group_id}",
        ),
        attachments=(_IMAGE,),
        marked_sticker=marked,
    )


class TestCollection:
    def test_the_collector_gets_the_stable_key_and_the_alias(self, runtime: Any, collector: _Collector) -> None:
        runtime.emotes._collect_stickers(_facts(42, marked=True))

        [call] = collector.calls
        # 图会长期留在共享库里：来源只存摘要过的会话键，发图人只存化名。
        assert call["source"] == "qq_group:5f3c2a108d4e4b7a9c210e6f" and call["contributor"] == "UserA"
        assert call["marked"] is True and call["attachments"] == [_IMAGE]

    def test_an_empty_whitelist_still_means_every_group(self, runtime: Any, collector: _Collector) -> None:
        runtime.emotes._collect_stickers(_facts(999))

        assert len(collector.calls) == 1

    @pytest.mark.parametrize("listed", [[42], ["42"]])
    def test_numeric_and_string_entries_cover_the_same_group(
        self, runtime: Any, collector: _Collector, listed: list[Any],
    ) -> None:
        set_live_config(runtime, sticker_groups=listed)

        runtime.emotes._collect_stickers(_facts("42"))
        runtime.emotes._collect_stickers(_facts(43))

        assert len(collector.calls) == 1

    def test_switched_off_collects_nothing(self, runtime: Any, collector: _Collector) -> None:
        set_live_config(runtime, sticker_collect=False)

        runtime.emotes._collect_stickers(_facts(42))

        assert collector.calls == []


def test_the_inbound_facts_carry_the_qq_mark(runtime: Any) -> None:
    message = [
        SimpleNamespace(type="image", data={"sub_type": 1, "file": "a.gif"}),
    ]
    event = SimpleNamespace(
        user_id=30003, group_id=42, message_id=7, time=1_700_000_000, self_id=90000,
        sender=SimpleNamespace(user_id=30003, nickname="某人", card="", role="member"),
        get_message=lambda: message, raw_message="", reply=None, anonymous=None, to_me=False,
    )

    facts = runtime.inbound._inbound_facts(SimpleNamespace(self_id="90000"), event, text="")

    assert facts.marked_sticker is True
