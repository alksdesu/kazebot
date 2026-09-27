"""平台输出说明：按能力声明生成，平台做不到的写法模型看不到。"""
from __future__ import annotations

import pytest

from adapters._shared import outbound
from adapters._shared.output_guide import (
    GUIDE_VERSION,
    SECTION_EMOJI,
    SECTION_REACTIONS,
    SECTION_SPLIT,
    GuideSection,
    OutputGuide,
    emoji_section,
    reaction_section,
    split_section,
)
from platforms.descriptor import (
    CHANNEL_GROUP,
    CHANNEL_OTHER,
    CHANNEL_PRIVATE,
    ChannelDescriptor,
    ImageSupport,
    PlatformCapabilities,
    PlatformDescriptor,
)

_CHANNELS = (
    ChannelDescriptor("chat_room", CHANNEL_GROUP),
    ChannelDescriptor("chat_dm", CHANNEL_PRIVATE),
    ChannelDescriptor("chat_other", CHANNEL_OTHER),
)
_PNG = ImageSupport(formats=frozenset({"png"}))


def _platform(**capabilities: object) -> PlatformDescriptor:
    return PlatformDescriptor(
        id="chat", display_name="Chat", short_name="Chat", channels=_CHANNELS,
        capabilities=PlatformCapabilities(**capabilities),  # type: ignore[arg-type]
    )


class TestSplit:
    def test_the_title_names_the_platform_and_the_marker_is_taught(self) -> None:
        section = split_section(_platform())

        assert section is not None and section.key == SECTION_SPLIT and not section.volatile
        assert section.text.splitlines()[0] == "【Chat回复分条】"
        assert "收到[SPLIT]我看看" in section.text and "不使用 [SPLIT]" in section.text

    def test_an_unlimited_platform_states_no_cap(self) -> None:
        assert "最多" not in split_section(_platform()).text

    def test_a_capped_platform_says_where_the_rest_goes(self) -> None:
        assert "最多 3 条，多出来的会并进最后一条。" in split_section(_platform(max_split_parts=3)).text

    def test_a_platform_that_sends_one_message_gets_no_split_advice(self) -> None:
        assert split_section(_platform(max_split_parts=1)) is None

    def test_the_marker_is_read_when_the_guide_is_built(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(outbound, "SPLIT_MARKER", "<br/>")

        text = split_section(_platform()).text

        assert "收到<br/>我看看" in text and "[SPLIT]" not in text


class TestReactions:
    _CATALOG = {"76": "赞", "4": "得意"}

    def test_only_kinds_that_take_reactions_get_the_catalog(self) -> None:
        platform = _platform(reactions=frozenset({CHANNEL_GROUP}))

        group = reaction_section(platform, CHANNEL_GROUP, self._CATALOG)

        assert group is not None and group.key == SECTION_REACTIONS and not group.volatile
        assert "只能使用下列 ID：76=赞、4=得意。" in group.text
        assert reaction_section(platform, CHANNEL_PRIVATE, self._CATALOG) is None

    def test_a_platform_without_reactions_never_mentions_the_marker(self) -> None:
        assert reaction_section(_platform(), CHANNEL_GROUP, self._CATALOG) is None

    def test_an_empty_catalog_means_reactions_are_off(self) -> None:
        assert reaction_section(_platform(reactions=frozenset({CHANNEL_GROUP})), CHANNEL_GROUP, {}) is None


class TestEmoji:
    def test_native_emoji_are_listed_with_the_platform_label(self) -> None:
        section = emoji_section(
            _platform(native_emoji=True), native_label="服务器表情", native_names=["甲", "乙"], native_hidden=3,
        )

        assert section is not None and section.key == SECTION_EMOJI
        assert section.text.splitlines() == [
            "【可用表情】", "在回复里写 [表情:名称] 就能发。", "服务器表情（只能用这些名字）：甲、乙（另有 3 个未展示）",
        ]

    def test_the_hidden_count_is_left_out_when_everything_is_shown(self) -> None:
        section = emoji_section(_platform(native_emoji=True), native_label="服务器表情", native_names=["甲"])

        assert section.text.splitlines()[-1] == "服务器表情（只能用这些名字）：甲"

    def test_stickers_go_one_per_line_with_the_fallback_advice(self) -> None:
        section = emoji_section(_platform(images=_PNG), stickers=["甲（大笑、猫）", "乙（无语）"])

        lines = section.text.splitlines()
        assert [line for line in lines if line.startswith("  ")] == ["  甲（大笑、猫）", "  乙（无语）"]
        assert "会去标签里找" in section.text

    def test_the_section_is_always_volatile(self) -> None:
        native_only = emoji_section(_platform(native_emoji=True, images=_PNG), native_label="收藏", native_names=["甲"])
        with_stickers = emoji_section(_platform(native_emoji=True, images=_PNG), stickers=["乙（无语）"])

        # 有没有抽中表情包每轮都在变；跟着切换位置的话，缓存前缀和动态区两头都会失效。
        assert native_only.volatile and with_stickers.volatile

    @pytest.mark.parametrize("capabilities, names, stickers", [
        pytest.param({}, ["甲"], ["乙（无语）"], id="no_native_emoji_no_images"),
        pytest.param({"native_emoji": True}, [], [], id="nothing_to_offer"),
        pytest.param({"images": _PNG}, ["甲"], [], id="native_names_without_native_support"),
    ])
    def test_nothing_the_platform_can_send_means_no_section(
        self, capabilities: dict, names: list[str], stickers: list[str],
    ) -> None:
        assert emoji_section(_platform(**capabilities), native_label="收藏", native_names=names, stickers=stickers) is None


class TestGuide:
    def test_missing_and_blank_sections_are_dropped(self) -> None:
        guide = OutputGuide.of(None, GuideSection("blank", "  \n"), GuideSection(SECTION_SPLIT, "分条"))

        assert [section.key for section in guide.sections] == [SECTION_SPLIT]
        assert guide.section(SECTION_SPLIT).text == "分条" and guide.section(SECTION_EMOJI) is None

    def test_the_payload_keeps_order_and_placement(self) -> None:
        guide = OutputGuide.of(GuideSection(SECTION_SPLIT, "分条"), GuideSection(SECTION_EMOJI, "表情", volatile=True))

        assert guide.payload() == {
            "version": GUIDE_VERSION,
            "sections": [
                {"key": SECTION_SPLIT, "text": "分条", "volatile": False},
                {"key": SECTION_EMOJI, "text": "表情", "volatile": True},
            ],
        }

    def test_an_empty_guide_is_still_sent_so_a_preempt_can_clear_the_old_one(self) -> None:
        assert OutputGuide.of(None).payload() == {"version": GUIDE_VERSION, "sections": []}
