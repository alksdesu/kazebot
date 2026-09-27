"""随仓库发布的平台描述。"""
from __future__ import annotations

from .descriptor import (
    CHANNEL_BRIDGE,
    CHANNEL_GROUP,
    CHANNEL_OTHER,
    CHANNEL_PRIVATE,
    HOOK_CONSOLE_SCHEMA,
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
    SecretField,
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
    legacy_conversation_keys=True,
    config_path="config/qq.yaml",
    config_env="CLONOTH_QQ_CONFIG_PATH",
    state_files={
        STATE_LIVE: "qq_live_state.json",
        STATE_ROUTES: "onebot_plugin_state.json",
        STATE_ALIASES: "onebot_anon_map.json",
        STATE_ACTIVE_ACCOUNT: "onebot_active_bot.json",
    },
    state_env={
        STATE_ROUTES: "ONEBOT_STATE_FILE",
        STATE_ALIASES: "ONEBOT_ANON_MAP_FILE",
    },
    hooks={
        HOOK_TRIGGER_POLICY: "adapters._shared.trigger_policy",
        HOOK_CONSOLE_SCHEMA: "adapters.onebot.console_layout",
    },
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
    secrets=(
        # NoneBot 自己从 env 读这一项；写在 data/config.yaml 里时由 bot.py 交给它。
        SecretField("access_token", "NapCat 连接令牌", env="ONEBOT_ACCESS_TOKEN"),
        SecretField(
            "forward_bridge_token",
            "转发桥令牌",
            env="ONEBOT_FORWARD_BRIDGE_TOKEN",
            file="data/onebot_forward_bridge_token",
            file_env="ONEBOT_FORWARD_BRIDGE_TOKEN_FILE",
        ),
        # 改了它已有的会话键全部失配，记忆目录分叉到新命名空间。
        SecretField(
            "conversation_hash_secret",
            "会话键摘要密钥",
            env="ONEBOT_CONVERSATION_HASH_SECRET",
            file="data/onebot_conversation_hash_secret",
            file_env="ONEBOT_CONVERSATION_HASH_SECRET_FILE",
        ),
        # supervisor 转发控制台的扫码、切号时拿它登 NapCat WebUI。部署脚本在容器首启后把它回填进 .env。
        SecretField("napcat_webui_token", "NapCat WebUI 令牌", env="NAPCAT_WEBUI_TOKEN"),
    ),
    # 排队中的账号操作请求里带着 admin token，文件还会被 root 侧 runner 执行。
    secret_files=("data/napcat-account.*",),
    # 别名双向表、会话哈希到真实群号的路由、真实 sender_id 的附件索引都在 onebot_* 里，
    # 写成前缀通配，以后再加一份状态文件也默认护住。快捷登录状态由真实的切号结果推出来，只有 supervisor 该写。
    owned_files=("data/onebot_*", "data/cache/onebot_*", "data/qq_dead_logins.json"),
    # 按人的资料里全是明文 QQ 号，外传等于把去匿名化字典发出去；改它照常走审批。
    private_files=("config/qq_user_profiles.yaml",),
)

BUILTIN_PLATFORMS: tuple[PlatformDescriptor, ...] = (QQ_NAPCAT,)
