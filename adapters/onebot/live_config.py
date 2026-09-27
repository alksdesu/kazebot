"""QQ 适配层热载配置：config/qq.yaml 改完即生效，没写的通用键落到 config/features.yaml。

每个键的 yaml 路径、env 兜底、解析器和生效方式集中声明在 LIVE_KEYS，通用键取共享的那份声明；
live 读取 / /qq/state 快照 / 接线测试共用这一份声明。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from adapters._shared import capability
from adapters._shared.console_schema import choice, id_list, number, text_input, toggle
from adapters._shared.features import adopt, new_features_config
from adapters._shared.live_config import (
    BOOL,
    PLAIN_STR,
    RELOAD_RECONCILED,
    Coercer,
    DerivedDefault,
    LiveConfig,
    LiveKey,
    coerce_bool,
    float_in,
    grant_for,
    int_in,
)
from .config import (
    CLONOTH_WORKSPACE,
    GROUP_TRIGGER_MODES,
    QQ_ID_MAX,
    QQ_ID_MIN,
    QQ_ID_PATTERN,
    parse_qq_id_item,
    parse_qq_id_list,
)

logger = logging.getLogger("nonebot.plugin.clonoth_agent.live_config")

# 单项规则由 bot 公布给控制台，值域上下界与 pattern 都取 config 里那一份，
# 免得公布的规则和 coercer 实际执行的规则各说一套。
ID_INPUT_RULE: Mapping[str, Any] = MappingProxyType({
    "kind": "id",
    "pattern": QQ_ID_PATTERN,
    "min": QQ_ID_MIN,
    "max": QQ_ID_MAX,
})


def _warn_bad_id(raw: Any) -> None:
    logger.warning("live config: %r is not a usable QQ id", raw)


def _coerce_id_list(raw: Any, default: Any) -> tuple[int, ...]:
    if isinstance(raw, (list, tuple)):
        # yaml 原生列表：`- 12345` 是 int，`- "12345"` 是 str，两种都收。
        # 保持配置顺序而不是转成 set —— 审批通知按名单顺序逐个私发，顺序稳定日志才可读。
        out: list[int] = []
        for item in raw:
            value = parse_qq_id_item(item, _warn_bad_id)
            if value is None:
                continue
            if value not in out:
                out.append(value)
        return tuple(out)
    return tuple(parse_qq_id_list(str(raw or ""), _warn_bad_id))


ID_LIST = Coercer(_coerce_id_list, input_rule=ID_INPUT_RULE)

_STICKER_STRATEGIES = (("strict", "严格"), ("loose", "宽松"), ("none", "全收"))
_STICKER_LOOKUP_ORDERS = (("native_first", "收藏表情"), ("library_first", "图库"))


def _legacy_strip_markdown_default(fallback: bool) -> Callable[[], DerivedDefault]:
    """ONEBOT_STRIP_MARKDOWN_STYLES 是星号/下划线拆分前的旧总开关。

    设了它就同时决定两个新开关的默认值，没设则各自用自己的默认。
    """

    def derive() -> DerivedDefault:
        raw = os.environ.get("ONEBOT_STRIP_MARKDOWN_STYLES")
        if raw is None:
            return DerivedDefault(fallback, from_env=False)
        return DerivedDefault(coerce_bool(raw, False), from_env=True)

    return derive


def _derive_allow_private_friends() -> DerivedDefault:
    """私聊放行策略以前是从白名单文本里找「好友」二字推导出来的。

    yaml 里已经是显式布尔，这里只保留 env 兜底路径的原语义：名单为空 = 只放行好友，
    名单里写了「好友」/「friend」说明也想放行好友。
    """
    raw = ""
    for env_name in ("CLONOTH_ALLOWED_PRIVATE_USERS", "ONEBOT_ALLOWED_PRIVATE_USERS"):
        value = os.environ.get(env_name)
        if value is not None and value.strip():
            raw = value.strip()
            break
    from_env = bool(raw)
    if not raw:
        raw = "[私聊只允许已经通过好友请求的人]"
    return DerivedDefault(
        not parse_qq_id_list(raw) or "好友" in raw or "friend" in raw.lower(),
        from_env=from_env,
    )


LIVE_KEYS: tuple[LiveKey, ...] = (
    # 信道
    LiveKey(
        "allowed_groups", "channels.allowed_groups", ID_LIST, (),
        env=("CLONOTH_ALLOWED_GROUPS", "ONEBOT_ALLOWED_GROUPS"),
        note="空列表 = 不在任何群说话",
        ui=id_list("允许说话的群", empty="名单为空", placeholder="输入群号，回车添加"),
    ),
    LiveKey(
        "allowed_private_users", "channels.allowed_private_users", ID_LIST, (),
        env=("CLONOTH_ALLOWED_PRIVATE_USERS", "ONEBOT_ALLOWED_PRIVATE_USERS"),
        ui=id_list("允许私聊的 QQ", empty="名单为空，只按下面的好友规则放行", placeholder="输入 QQ 号，回车添加"),
    ),
    LiveKey(
        "allow_private_friends", "channels.allow_private_friends", BOOL, True,
        env=("ONEBOT_ALLOW_PRIVATE_FRIENDS",),
        derive_default=_derive_allow_private_friends,
        ui=toggle("好友自动放行", "已经通过好友请求的人不必再进名单。关掉之后只有上面名单里的人能私聊。"),
    ),
    LiveKey(
        "private_denied_reply", "channels.private_denied_reply", PLAIN_STR, "",
        note="空 = 一个字都不回。回一句就等于向任何陌生人确认这个号是 bot",
        ui=text_input("拒绝时回复"),
    ),
    # 权限
    LiveKey(
        "admin_users", "permissions.admin_users", ID_LIST, (),
        env=("CLONOTH_ADMIN_QQ_USERS", "ONEBOT_ADMIN_USERS"),
        note="空列表 = 管理命令全部不可用，审批一律自动拒绝",
        ui=id_list("管理员 QQ", empty="名单为空", placeholder="输入 QQ 号，回车添加"),
    ),
    # 逐项能力档位。默认全是 admin，也就是升级前的行为：只有上面那份名单说了算。
    # 控制台按 bot 公布的能力清单画这一块，档位键自己不带控件。
    *(
        LiveKey(
            f"grant_{cap.key}", f"permissions.capabilities.{cap.key}",
            grant_for(cap.key), cap.default,
            note=(
                "群主/群管拿到后只能作用于自己那个群"
                if cap.group_scoped
                else "影响面超出单个群，只能给管理员名单"
            ),
        )
        for cap in capability.CAPABILITIES
    ),
    # 触发
    adopt("group_trigger", env=("ONEBOT_GROUP_TRIGGER",)),
    adopt("trigger_prefixes", env=("ONEBOT_TRIGGER_PREFIXES",)),
    adopt("signal_all"),
    adopt("signal_at"),
    adopt("signal_reply"),
    adopt("signal_prefix"),
    adopt("signal_name"),
    adopt("signal_keyword"),
    adopt("signal_random"),
    adopt("name_words"),
    adopt("name_anywhere"),
    adopt("keyword_words"),
    adopt("random_probability"),
    adopt("llm_intent_enabled"),
    adopt("llm_intent_node_id"),
    adopt("llm_intent_timeout_sec"),
    adopt("llm_intent_max_inflight"),
    adopt("llm_intent_context_messages"),
    adopt("cooldown_group_sec"),
    adopt("cooldown_user_sec"),
    adopt("cooldown_exempt_at"),
    # 输出格式
    LiveKey(
        "strip_asterisk_styles", "output.strip_asterisk_styles", BOOL, True,
        env=("ONEBOT_STRIP_MARKDOWN_ASTERISK_STYLES",),
        derive_default=_legacy_strip_markdown_default(True),
        ui=toggle("去掉星号强调", "QQ 不渲染 Markdown，*强调* 会原样显示成星号。"),
    ),
    LiveKey(
        "strip_underscore_styles", "output.strip_underscore_styles", BOOL, False,
        env=("ONEBOT_STRIP_MARKDOWN_UNDERSCORE_STYLES",),
        derive_default=_legacy_strip_markdown_default(False),
        ui=toggle("去掉下划线强调", "下划线同理。但代码和文件名里常有下划线，默认不动。"),
    ),
    adopt("reply_to_trigger", env=("ONEBOT_REPLY_TO_TRIGGER",)),
    LiveKey(
        "message_limit", "output.message_limit", int_in(min_value=500), 4300,
        env=("ONEBOT_QQ_MESSAGE_LIMIT",),
        ui=number("单条消息", step=100, unit="字"),
    ),
    adopt("history_text_limit", env=("ONEBOT_HISTORY_TEXT_LIMIT",)),
    LiveKey(
        "face_prompt_limit", "output.face_prompt_limit", int_in(min_value=0, max_value=200), 50,
        env=("ONEBOT_CUSTOM_FACE_PROMPT_LIMIT",),
        ui=number("最多列出", unit="个"),
    ),
    # 默认关有实测原因：所有 QQ 事件走同一条反向 WS 并在 SDK 里串行 await，而
    # send_group_forward_msg 把几张大图一次交给 NapCat 要几十秒，期间这条 WS 上的
    # 其它 call_api 全部排队，整个 bot 卡死。逐张直发已用超时容错解决了重复发/丢图。
    LiveKey(
        "enable_image_forward_merge", "output.image_forward_merge", BOOL, False,
        env=("ONEBOT_ENABLE_IMAGE_FORWARD_MERGE",),
        note="合并转发会长时间占住那条唯一的反向 WS，出问题要能立刻关掉",
        ui=toggle("合并发图", "多张图合并成一条转发发出，而不是一张张发。"),
    ),
    LiveKey(
        "image_forward_merge_threshold", "output.image_forward_merge_threshold", int_in(min_value=2, max_value=16), 2,
        env=("ONEBOT_IMAGE_FORWARD_MERGE_THRESHOLD",),
        ui=number("至少", unit="张才合并"),
    ),
    # 扩展能力
    LiveKey(
        "enable_reactions", "extensions.reactions", BOOL, True,
        env=("ONEBOT_ENABLE_REACTIONS",),
        ui=toggle("表情回应", "给群消息贴表情回应。"),
    ),
    LiveKey(
        "enable_auto_like", "extensions.auto_like", BOOL, False,
        env=("ONEBOT_ENABLE_AUTO_LIKE",),
        ui=toggle("自动点赞", "给群友的资料卡点赞。"),
    ),
    LiveKey(
        "auto_like_times", "extensions.auto_like_times", int_in(min_value=1, max_value=20), 10,
        env=("ONEBOT_AUTO_LIKE_TIMES",),
        ui=number("每人每天", unit="次"),
    ),
    adopt("enable_preempt", env=("ONEBOT_ENABLE_PREEMPT",)),
    adopt("enable_echo", env=("ONEBOT_ECHO_ENABLED",)),
    adopt("echo_threshold", env=("ONEBOT_ECHO_THRESHOLD",)),
    adopt("echo_max_length", env=("ONEBOT_ECHO_MAX_TEXT_LENGTH",)),
    adopt("echo_window_sec", env=("ONEBOT_ECHO_WINDOW_SEC",)),
    adopt("echo_cooldown_sec", env=("ONEBOT_ECHO_COOLDOWN_SEC",)),
    LiveKey(
        "sticker_collect", "extensions.sticker.collect", BOOL, False,
        env=("ONEBOT_STICKER_COLLECT",),
        note="把群里的表情包攒进图库",
        ui=toggle("收集表情包", "把群里刷过的表情包攒进图库。"),
    ),
    adopt("sticker_auto_tag", env=("ONEBOT_STICKER_AUTO_TAG",)),
    LiveKey(
        "sticker_strategy", "extensions.sticker.strategy", PLAIN_STR, "loose",
        env=("ONEBOT_STICKER_STRATEGY",),
        note="收多严：strict 只收 QQ 标了表情包的，loose 再放行看着不像截图的，none 全收",
        ui=choice("收多严", _STICKER_STRATEGIES),
    ),
    LiveKey(
        "sticker_groups", "extensions.sticker.groups", ID_LIST, (),
        env=("ONEBOT_STICKER_GROUPS",),
        note="只从这些群收，留空表示所有已授权的群",
        ui=id_list("只从这些群收", empty="所有已授权的群都收", placeholder="输入群号，回车添加"),
    ),
    adopt("sticker_auto_accept", env=("ONEBOT_STICKER_AUTO_ACCEPT",)),
    adopt("sticker_pending_limit", env=("ONEBOT_STICKER_PENDING_LIMIT",)),
    adopt("sticker_library_limit", env=("ONEBOT_STICKER_LIBRARY_LIMIT",)),
    adopt("sticker_pending_ttl_days", env=("ONEBOT_STICKER_PENDING_TTL_DAYS",)),
    adopt("sticker_send_probability", env=("ONEBOT_STICKER_SEND_PROBABILITY",)),
    adopt("sticker_prompt_limit", env=("ONEBOT_STICKER_PROMPT_LIMIT",)),
    adopt("sticker_repeat_window_sec", env=("ONEBOT_STICKER_REPEAT_WINDOW_SEC",)),
    LiveKey(
        "sticker_lookup_order", "extensions.sticker.lookup_order", PLAIN_STR, "native_first",
        note="表情名同时对得上收藏表情和图库时先用哪个：native_first 收藏表情，library_first 图库",
        ui=choice("重名时先用", _STICKER_LOOKUP_ORDERS),
    ),
    adopt("sticker_combat", env=("ONEBOT_STICKER_COMBAT",)),
    adopt("sticker_burst_probability", env=("ONEBOT_STICKER_BURST_PROBABILITY",)),
    # 输入能力
    adopt("enable_image_input", env=("ONEBOT_ENABLE_IMAGE_INPUT",)),
    adopt("max_images_per_turn", env=("ONEBOT_MAX_IMAGES_PER_TURN",)),
    adopt("image_max_bytes", env=("ONEBOT_IMAGE_MAX_BYTES",)),
    adopt("image_wait_after_text_sec", env=("ONEBOT_IMAGE_WAIT_AFTER_TEXT_SECONDS",)),
    adopt("enable_file_input", env=("ONEBOT_ENABLE_FILE_INPUT",)),
    adopt("max_files_per_turn", env=("ONEBOT_MAX_FILES_PER_TURN",)),
    adopt("file_max_bytes", env=("ONEBOT_FILE_MAX_BYTES",)),
    adopt("file_extra_allowed_extensions", env=("ONEBOT_FILE_EXTRA_ALLOWED_EXTENSIONS",)),
    LiveKey(
        "enable_forward_msg_input", "input.forward_msg", BOOL, True,
        env=("ONEBOT_ENABLE_FORWARD_MSG_INPUT",),
        ui=toggle("读合并转发", "展开合并转发的聊天记录。"),
    ),
    LiveKey(
        "forward_msg_max_depth", "input.forward_msg_max_depth", int_in(min_value=0, max_value=8), 3,
        env=("ONEBOT_FORWARD_MSG_MAX_DEPTH",),
        ui=number("嵌套深度", unit="层"),
    ),
    LiveKey(
        "forward_msg_max_messages", "input.forward_msg_max_messages", int_in(min_value=1, max_value=500), 80,
        env=("ONEBOT_FORWARD_MSG_MAX_MESSAGES",),
        ui=number("最多", step=10, unit="条"),
    ),
    LiveKey(
        "forward_msg_text_limit", "input.forward_msg_text_limit", int_in(min_value=500, max_value=100000), 12000,
        env=("ONEBOT_FORWARD_MSG_TEXT_LIMIT",),
        ui=number("总字数", step=1000, unit="字"),
    ),
    LiveKey(
        "forward_msg_timeout_sec", "input.forward_msg_timeout_sec", float_in(min_value=0.5, max_value=60.0), 8.0,
        env=("ONEBOT_FORWARD_MSG_TIMEOUT_SECONDS",),
        note="展开一条消息里全部转发卡片的总时限；超时按未展开处理",
        ui=number("展开超时", step=0.5, unit="秒"),
    ),
    LiveKey(
        "forward_msg_expand_for_trigger", "input.forward_msg_expand_for_trigger", BOOL, True,
        env=("ONEBOT_FORWARD_MSG_EXPAND_FOR_TRIGGER",),
        note="关掉后群触发判定只看到占位符，转发消息要 @ 或回复才理",
        ui=toggle("群里靠关键词也能触发"),
    ),
    LiveKey(
        "enable_forward_msg_media", "input.forward_msg_media", BOOL, True,
        env=("ONEBOT_ENABLE_FORWARD_MSG_MEDIA",),
        note="转发卡片里的图片和文件是否一并采集；条数仍受 max_images_per_turn / max_files_per_turn 限制",
        ui=toggle("里面的图片和文件也收"),
    ),
    # 队列
    adopt("enable_queue", env=("ONEBOT_ENABLE_QQ_QUEUE",)),
    adopt("queue_workers", env=("ONEBOT_QQ_QUEUE_WORKERS",)),
    adopt("queue_interval", env=("ONEBOT_QQ_QUEUE_INTERVAL",)),
    adopt("queue_wait_for_reply", env=("ONEBOT_QQ_QUEUE_WAIT_FOR_REPLY",)),
    adopt("queue_reply_timeout", env=("ONEBOT_QQ_QUEUE_REPLY_TIMEOUT",)),
    # 审批
    adopt("approval_reply_unique_fallback", env=("ONEBOT_APPROVAL_REPLY_UNIQUE_FALLBACK",)),
    adopt("approval_bare_verb_unique", env=("ONEBOT_APPROVAL_BARE_VERB_UNIQUE",)),
    # 群历史
    adopt("group_history_max", env=("ONEBOT_GROUP_HISTORY_MAX",)),
    adopt("group_history_undelivered_ratio", env=("ONEBOT_GROUP_HISTORY_UNDELIVERED_RATIO",)),
    # 转发 Bridge
    LiveKey(
        "enable_forward_bridge", "forward_bridge.enabled", BOOL, True,
        env=("ONEBOT_ENABLE_FORWARD_BRIDGE",), reload=RELOAD_RECONCILED,
        note="开关翻转要起停本地 HTTP server",
        ui=toggle("启用转发桥", "关掉会立刻停掉这个本地端口。"),
    ),
    LiveKey(
        "forward_bridge_max_messages", "forward_bridge.max_messages", int_in(min_value=1, max_value=200), 30,
        env=("ONEBOT_FORWARD_BRIDGE_MAX_MESSAGES",),
        ui=number("单次最多", unit="条"),
    ),
)

# / 与 ／ 只参与触发判定不参与剥离：/draw、/nai 等命令正则要求字面斜杠，剥掉会失配，
# 且群里贴 /www/wwwroot/... 这类绝对路径时正文会被静默改写。
_UNSTRIPPABLE_PREFIXES = frozenset({"/", "／"})


def _derived(values: dict[str, Any]) -> None:
    """从已解析的键推出来的值。放进同一份快照，call site 不必自己算。"""
    values["trigger_prefixes_strippable"] = tuple(
        p for p in values["trigger_prefixes"] if p not in _UNSTRIPPABLE_PREFIXES
    )
    values["group_trigger_is_known"] = values["group_trigger"] in GROUP_TRIGGER_MODES


DERIVED_NAMES: tuple[str, ...] = ("trigger_prefixes_strippable", "group_trigger_is_known")


def config_path() -> Path:
    """平台配置文件的位置，由平台描述声明。env 覆盖只为测试与非常规部署留口子。"""
    from platforms.builtin import QQ_NAPCAT
    from platforms.registry import required_config_path

    return required_config_path(QQ_NAPCAT, Path(CLONOTH_WORKSPACE))


# 实例跟着本模块走：共享层整个进程只有一份，缓存和上下文快照放那边会被几次加载共用。
features = new_features_config(Path(CLONOTH_WORKSPACE))
_config = LiveConfig(
    LIVE_KEYS, path=config_path, derive=_derived, derived_names=DERIVED_NAMES, defaults=features,
)

LIVE_KEYS_BY_NAME: Mapping[str, LiveKey] = _config.keys_by_name
RECONCILED_KEYS: tuple[str, ...] = _config.reconciled_keys
live = _config.live
refresh = _config.refresh
current = _config.current
pinned = _config.pinned
invalidate = _config.invalidate
reload = _config.reload
save = _config.save
raw_document = _config.raw_document
document_from = _config.document_from


def state_payload() -> dict[str, Any]:
    """bot 进程实际生效的配置快照 + 文件指纹。GET /qq/state 的主体。"""
    payload = _config.state_payload()
    # 能力清单由 bot 公布而不是前端写死：加一项能力只该改这一处，
    # 漏掉的那项在界面上无声消失，运营者以为没有这回事。
    payload["capabilities"] = capability.catalog()
    return payload
