"""稳定会话键的组成：渠道前缀取平台声明，摘要拌进密钥和账号作用域。QQ 的键必须和从前逐字节相同。"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adapters._shared.conversation_hash import ConversationKeys, digest, rekey  # noqa: E402
from platforms.builtin import QQ_NAPCAT  # noqa: E402
from platforms.descriptor import (  # noqa: E402
    CHANNEL_GROUP,
    CHANNEL_OTHER,
    ID_STRING,
    ChannelDescriptor,
    PlatformDescriptor,
)

_SECRET = "c" * 64
_NEW = PlatformDescriptor(
    id="newchat", display_name="New Chat", short_name="New", id_shape=ID_STRING,
    channels=(ChannelDescriptor("new_room", CHANNEL_GROUP), ChannelDescriptor("new_unknown", CHANNEL_OTHER)),
)


class TestQqKeysStayByteIdentical:
    def test_an_unsalted_group_key_matches_the_pinned_value(self) -> None:
        assert ConversationKeys(QQ_NAPCAT, "").stable_key(
            "qq_group:123456789", bot_scope="",
        ) == "qq_group:bc3f12b0621298e191a49fa7"

    @pytest.mark.parametrize(
        ("real", "channel"),
        [
            ("qq_group:123456789", "qq_group"),
            ("qq_private:10001", "qq_private"),
            ("qq_unknown:x", "qq_unknown"),
            # 转发桥不做摘要，从来没有自己的稳定键前缀；落到兜底渠道和从前一样。
            ("qq_forward:group:1", "qq_unknown"),
            ("something-else", "qq_unknown"),
        ],
    )
    def test_the_prefix_follows_the_declared_channels(self, real: str, channel: str) -> None:
        keys = ConversationKeys(QQ_NAPCAT, _SECRET)

        assert keys.stable_key(real, bot_scope="10000") == f"{channel}:{digest(real, _SECRET, bot_scope='10000')}"

    def test_the_account_scope_separates_namespaces(self) -> None:
        keys = ConversationKeys(QQ_NAPCAT, _SECRET)

        assert keys.stable_key("qq_group:1", bot_scope="10000") != keys.stable_key("qq_group:1", bot_scope="20000")
        assert keys.stable_key("qq_group:1", bot_scope="") == f"qq_group:{digest('qq_group:1', _SECRET)}"

    def test_the_secret_stays_out_of_the_repr(self) -> None:
        assert _SECRET not in repr(ConversationKeys(QQ_NAPCAT, _SECRET))


class TestAccountScope:
    def test_a_new_platform_refuses_a_key_without_the_bot_account(self) -> None:
        keys = ConversationKeys(_NEW, _SECRET)

        for scope in ("", "  "):
            with pytest.raises(ValueError, match="bot account"):
                keys.stable_key("new_room:r1", bot_scope=scope)

    def test_a_new_platforms_accounts_get_their_own_keys(self) -> None:
        keys = ConversationKeys(_NEW, _SECRET)

        assert keys.stable_key("new_room:r1", bot_scope="app-1") != keys.stable_key("new_room:r1", bot_scope="app-2")

    def test_qq_may_still_derive_unscoped_keys(self) -> None:
        # 加账号隔离之前攒下的会话键没有作用域，还没登录上时也照这样算。
        assert QQ_NAPCAT.legacy_conversation_keys is True
        assert ConversationKeys(QQ_NAPCAT, _SECRET).stable_key("qq_group:1", bot_scope="").startswith("qq_group:")

    def test_the_account_has_to_be_passed_explicitly(self) -> None:
        with pytest.raises(TypeError):
            ConversationKeys(QQ_NAPCAT, _SECRET).stable_key("qq_group:1")  # type: ignore[call-arg]


class TestDeclarations:
    def test_a_platform_without_a_fallback_channel_is_refused(self) -> None:
        channels = tuple(channel for channel in QQ_NAPCAT.channels if channel.kind != CHANNEL_OTHER)

        with pytest.raises(ValueError, match="no hashed channel for unrecognized conversations"):
            ConversationKeys(replace(QQ_NAPCAT, channels=channels), _SECRET)

    def test_an_unhashed_fallback_channel_is_refused(self) -> None:
        # 兜底渠道出的也是「前缀:摘要」，声明成不做摘要就和附件目录、迁移对不上。
        channels = tuple(
            ChannelDescriptor(channel.name, channel.kind, hashed=False) if channel.kind == CHANNEL_OTHER else channel
            for channel in QQ_NAPCAT.channels
        )

        with pytest.raises(ValueError, match="no hashed channel for unrecognized conversations"):
            ConversationKeys(replace(QQ_NAPCAT, channels=channels), _SECRET)


class TestRekey:
    def test_the_old_prefix_is_kept_and_only_the_digest_moves(self) -> None:
        old = f"qq_private:{digest('qq_group:1', '')}"

        assert rekey(old, "qq_group:1", _SECRET, bot_scope="10000") == (
            f"qq_private:{digest('qq_group:1', _SECRET, bot_scope='10000')}"
        )
