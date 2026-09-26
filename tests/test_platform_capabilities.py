"""平台能力声明：没声明的能力当不支持，声明本身在构造时就校验。

后面的出站渲染、提示词生成都按它做决定，写错一个字段比少写一个更糟：少写只是少个功能，
写错会让模型用上驱动发不出去的写法。
"""
from __future__ import annotations

import json
from types import MappingProxyType

import pytest

from platforms.builtin import QQ_NAPCAT
from platforms.descriptor import (
    CHANNEL_GROUP,
    CHANNEL_OTHER,
    CHANNEL_PRIVATE,
    MENTION_NATIVE,
    MENTION_NONE,
    MENTION_TEXT,
    ChannelDescriptor,
    ImageSupport,
    PlatformCapabilities,
    PlatformDescriptor,
    ReplyQuota,
)

_ROOM = ChannelDescriptor("x_room", CHANNEL_GROUP)
_DM = ChannelDescriptor("x_dm", CHANNEL_PRIVATE)
_FALLBACK = ChannelDescriptor("x_unknown", CHANNEL_OTHER)


def _platform(capabilities: PlatformCapabilities, channels: tuple[ChannelDescriptor, ...] = (_ROOM, _DM, _FALLBACK)):
    return PlatformDescriptor(id="x", display_name="X", short_name="X", channels=channels, capabilities=capabilities)


class TestDefaults:
    def test_an_undeclared_platform_is_plain_text(self) -> None:
        capabilities = PlatformDescriptor(id="x", display_name="X", short_name="X").capabilities

        assert capabilities.mention == MENTION_NONE
        assert capabilities.images is None
        assert not any((
            capabilities.markdown, capabilities.quote, capabilities.reactions, capabilities.native_emoji,
            capabilities.merged_forward, capabilities.progress_edit, capabilities.recall, capabilities.edit,
            capabilities.mention_everyone,
        ))
        assert capabilities.typing == frozenset() and dict(capabilities.reply_quotas) == {}

    def test_plain_text_platforms_can_still_push_and_send_links(self) -> None:
        capabilities = PlatformCapabilities()

        assert capabilities.proactive and capabilities.links
        assert capabilities.max_text_length == 0 and capabilities.max_split_parts == 0


class TestValidation:
    @pytest.mark.parametrize("overrides", [
        pytest.param({"mention": "shout"}, id="unknown_mention_form"),
        pytest.param({"mention_everyone": True}, id="everyone_without_mentions"),
        pytest.param({"max_text_length": -1}, id="negative_length"),
        pytest.param({"max_split_parts": -1}, id="negative_parts"),
        pytest.param({"proactive_daily_limit": -1}, id="negative_daily_limit"),
        pytest.param({"typing": frozenset({"bridge"})}, id="typing_outside_conversations"),
        pytest.param({"reply_quotas": {"other": ReplyQuota(5, 300)}}, id="quota_outside_conversations"),
    ])
    def test_a_malformed_declaration_is_rejected(self, overrides: dict) -> None:
        with pytest.raises(ValueError):
            PlatformCapabilities(**overrides)

    @pytest.mark.parametrize("limit, window", [(0, 300), (5, 0), (-1, 300)])
    def test_a_reply_quota_needs_a_positive_limit_and_window(self, limit: int, window: float) -> None:
        with pytest.raises(ValueError):
            ReplyQuota(limit, window)

    @pytest.mark.parametrize("overrides", [
        pytest.param({"formats": frozenset()}, id="no_format"),
        pytest.param({"formats": frozenset({" "})}, id="blank_format"),
        pytest.param({"formats": frozenset({"png"}), "max_bytes": -1}, id="negative_size"),
    ])
    def test_image_support_needs_a_format_and_a_sane_size(self, overrides: dict) -> None:
        with pytest.raises(ValueError):
            ImageSupport(**overrides)

    def test_capabilities_must_name_channel_kinds_the_platform_has(self) -> None:
        with pytest.raises(ValueError):
            _platform(PlatformCapabilities(typing=frozenset({CHANNEL_PRIVATE})), channels=(_ROOM, _FALLBACK))
        with pytest.raises(ValueError):
            _platform(PlatformCapabilities(reply_quotas={CHANNEL_PRIVATE: ReplyQuota(4, 3600)}), channels=(_ROOM, _FALLBACK))

    def test_a_consistent_declaration_is_accepted(self) -> None:
        platform = _platform(PlatformCapabilities(
            mention=MENTION_TEXT,
            reply_quotas={CHANNEL_GROUP: ReplyQuota(5, 300), CHANNEL_PRIVATE: ReplyQuota(4, 3600)},
            typing=frozenset({CHANNEL_PRIVATE}),
            links=False,
        ))

        assert platform.capabilities.reply_quotas[CHANNEL_GROUP] == ReplyQuota(5, 300)


class TestNormalization:
    def test_image_formats_are_compared_case_insensitively(self) -> None:
        assert ImageSupport(formats=frozenset({" PNG", "Gif"})).formats == frozenset({"png", "gif"})

    def test_the_declaration_cannot_be_changed_after_the_fact(self) -> None:
        capabilities = PlatformCapabilities(reply_quotas={CHANNEL_GROUP: ReplyQuota(5, 300)})

        assert isinstance(capabilities.reply_quotas, MappingProxyType)
        with pytest.raises(TypeError):
            capabilities.reply_quotas[CHANNEL_PRIVATE] = ReplyQuota(4, 3600)  # type: ignore[index]

    def test_a_typing_list_becomes_a_set(self) -> None:
        assert PlatformCapabilities(typing=[CHANNEL_PRIVATE, CHANNEL_PRIVATE]).typing == frozenset({CHANNEL_PRIVATE})  # type: ignore[arg-type]


class TestPublicView:
    def test_the_console_sees_every_field_as_json(self) -> None:
        capabilities = PlatformCapabilities(
            mention=MENTION_NATIVE,
            reply_quotas={CHANNEL_GROUP: ReplyQuota(5, 300)},
            images=ImageSupport(formats=frozenset({"png", "gif"}), max_bytes=1024, upload_first=True),
            typing=frozenset({CHANNEL_PRIVATE}),
        )

        public = json.loads(json.dumps(capabilities.public_dict()))

        assert set(public) == {
            "max_text_length", "markdown", "max_split_parts", "reply_quotas", "mention", "mention_everyone",
            "quote", "reactions", "images", "native_emoji", "merged_forward", "proactive",
            "proactive_daily_limit", "typing", "progress_edit", "recall", "edit", "links",
        }
        assert public["reply_quotas"] == {"group": {"limit": 5, "window_sec": 300}}
        assert public["images"] == {
            "formats": ["gif", "png"], "max_bytes": 1024, "animated": True,
            "inline_with_text": True, "upload_first": True,
        }
        assert public["typing"] == ["private"]

    def test_a_platform_without_images_says_null(self) -> None:
        assert PlatformCapabilities().public_dict()["images"] is None

    def test_the_platform_view_carries_its_capabilities(self) -> None:
        assert QQ_NAPCAT.public_dict()["capabilities"] == QQ_NAPCAT.capabilities.public_dict()


class TestQQDeclaration:
    """QQ 的声明要对得上驱动现在的行为：多写了就会有人按它去用驱动没有的功能。"""

    def test_what_the_napcat_driver_renders(self) -> None:
        capabilities = QQ_NAPCAT.capabilities

        assert capabilities.mention == MENTION_NATIVE and capabilities.mention_everyone
        assert capabilities.quote and capabilities.reactions and capabilities.native_emoji
        assert capabilities.merged_forward and capabilities.proactive and capabilities.links

    def test_what_the_napcat_driver_does_not_do(self) -> None:
        capabilities = QQ_NAPCAT.capabilities

        # QQ 不渲染 Markdown，发送前按配置剥掉；状态消息既不编辑也不撤回。
        assert not capabilities.markdown
        assert not capabilities.progress_edit and not capabilities.edit and not capabilities.recall
        assert dict(capabilities.reply_quotas) == {} and capabilities.proactive_daily_limit == 0

    def test_only_private_chats_show_typing(self) -> None:
        assert QQ_NAPCAT.capabilities.typing == frozenset({CHANNEL_PRIVATE})

    def test_images_go_out_directly_including_animations(self) -> None:
        images = QQ_NAPCAT.capabilities.images

        assert images is not None
        assert {"png", "jpeg", "gif", "webp", "bmp"} == images.formats
        assert images.animated and images.inline_with_text and not images.upload_first

    def test_the_length_cap_is_left_to_configuration(self) -> None:
        assert QQ_NAPCAT.capabilities.max_text_length == 0
        assert QQ_NAPCAT.capabilities.max_split_parts == 0
