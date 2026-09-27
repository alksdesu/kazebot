"""随仓库发布的平台描述。"""
from __future__ import annotations

from .descriptor import (
    CHANNEL_BRIDGE,
    CHANNEL_GROUP,
    CHANNEL_OTHER,
    CHANNEL_PRIVATE,
    HOOK_TRIGGER_POLICY,
    ID_NUMERIC,
    MENTION_NATIVE,
    STATE_ACTIVE_ACCOUNT,
    STATE_ALIASES,
    STATE_LIVE,
    STATE_ROUTES,
    ChannelDescriptor,
    ImageSupport,
    PlatformCapabilities,
    PlatformDescriptor,
)

# id 就是适配器上报的 platform_auth.platform；已有的定时任务、拒绝文案都按它写的。
QQ_NAPCAT = PlatformDescriptor(
    id="qq",
    display_name="QQ · NapCat",
    short_name="QQ",
    aliases=frozenset({"onebot", "onebot11"}),
    channels=(
        ChannelDescriptor("qq_group", CHANNEL_GROUP),
        ChannelDescriptor("qq_private", CHANNEL_PRIVATE),
        ChannelDescriptor("qq_forward", CHANNEL_BRIDGE, hashed=False),
        ChannelDescriptor("qq_unknown", CHANNEL_OTHER),
    ),
    id_shape=ID_NUMERIC,
    config_path="config/qq.yaml",
    config_env="CLONOTH_QQ_CONFIG_PATH",
    state_files={
        STATE_LIVE: "qq_live_state.json",
        STATE_ROUTES: "onebot_plugin_state.json",
        STATE_ALIASES: "onebot_anon_map.json",
        STATE_ACTIVE_ACCOUNT: "onebot_active_bot.json",
    },
    hooks={HOOK_TRIGGER_POLICY: "adapters._shared.trigger_policy"},
    # 按驱动现在做得到的填：NapCat 能撤回消息，但驱动没接，就不声明。
    # 字数上限不写死，由 output.message_limit 截断。
    capabilities=PlatformCapabilities(
        mention=MENTION_NATIVE,
        mention_everyone=True,
        quote=True,
        # 表情回应只有群消息上有。
        reactions=frozenset({CHANNEL_GROUP}),
        images=ImageSupport(formats=frozenset({"png", "jpeg", "gif", "webp", "bmp"})),
        native_emoji=True,
        merged_forward=True,
        typing=frozenset({CHANNEL_PRIVATE}),
    ),
)

BUILTIN_PLATFORMS: tuple[PlatformDescriptor, ...] = (QQ_NAPCAT,)
