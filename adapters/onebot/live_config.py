"""QQ 适配层热载配置。

config/qq.yaml 改完即生效，不重启进程。每个键的 yaml 路径、env 兜底、解析器和生效方式
集中声明在 LIVE_KEYS，live 读取 / /qq/state 快照 / 接线测试共用这一份声明。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from adapters._shared import capability
from adapters._shared.live_config import (
    BOOL,
    PLAIN_STR,
    PREFIX_LIST,
    RELOAD_RECONCILED,
    TRIGGER_MODE,
    TRISTATE_BOOL,
    WORD_LIST,
    Coercer,
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


def _legacy_strip_markdown_default(fallback: bool) -> Callable[[], Any]:
    """ONEBOT_STRIP_MARKDOWN_STYLES 是星号/下划线拆分前的旧总开关。

    设了它就同时决定两个新开关的默认值，没设则各自用自己的默认。
    """

    def derive() -> Any:
        raw = os.environ.get("ONEBOT_STRIP_MARKDOWN_STYLES")
        return fallback if raw is None else coerce_bool(raw, False)

    return derive


def _derive_allow_private_friends() -> bool:
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
    if not raw:
        raw = "[私聊只允许已经通过好友请求的人]"
    return (
        not parse_qq_id_list(raw)
        or "好友" in raw
        or "friend" in raw.lower()
    )


LIVE_KEYS: tuple[LiveKey, ...] = (
    # 信道
    LiveKey(
        "allowed_groups", "channels.allowed_groups", ID_LIST, (),
        env=("CLONOTH_ALLOWED_GROUPS", "ONEBOT_ALLOWED_GROUPS"),
        note="空列表 = 不在任何群说话",
    ),
    LiveKey(
        "allowed_private_users", "channels.allowed_private_users", ID_LIST, (),
        env=("CLONOTH_ALLOWED_PRIVATE_USERS", "ONEBOT_ALLOWED_PRIVATE_USERS"),
    ),
    LiveKey(
        "allow_private_friends", "channels.allow_private_friends", BOOL, True,
        env=("ONEBOT_ALLOW_PRIVATE_FRIENDS",),
        derive_default=_derive_allow_private_friends,
    ),
    LiveKey(
        "private_denied_reply", "channels.private_denied_reply", PLAIN_STR, "",
        note="空 = 一个字都不回。回一句就等于向任何陌生人确认这个号是 bot",
    ),
    # 权限
    LiveKey(
        "admin_users", "permissions.admin_users", ID_LIST, (),
        env=("CLONOTH_ADMIN_QQ_USERS", "ONEBOT_ADMIN_USERS"),
        note="空列表 = 管理命令全部不可用，审批一律自动拒绝",
    ),
    # 逐项能力档位。默认全是 admin，也就是升级前的行为：只有上面那份名单说了算。
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
    LiveKey(
        "group_trigger", "trigger.group_mode", TRIGGER_MODE, "mention_only",
        env=("ONEBOT_GROUP_TRIGGER",),
    ),
    # ／ 必须在列：命令正则认全角斜杠，但它不在触发前缀里的话，群里发 ／帮助 连
    # 触发都触发不了，用户只会看到 bot 对半角斜杠有反应、对全角没有。
    LiveKey(
        "trigger_prefixes", "trigger.prefixes", PREFIX_LIST, "!,！,/,／",
        env=("ONEBOT_TRIGGER_PREFIXES",),
    ),
    # 七个信号开关。前四个是三态：没配就跟随 group_mode 的旧语义（见 trigger_policy）。
    LiveKey("signal_all", "trigger.signals.all", TRISTATE_BOOL, None),
    LiveKey("signal_at", "trigger.signals.at", TRISTATE_BOOL, None),
    LiveKey("signal_reply", "trigger.signals.reply", TRISTATE_BOOL, None),
    LiveKey("signal_prefix", "trigger.signals.prefix", TRISTATE_BOOL, None),
    LiveKey("signal_name", "trigger.signals.name", BOOL, False),
    LiveKey("signal_keyword", "trigger.signals.keyword", BOOL, False),
    LiveKey("signal_random", "trigger.signals.random", BOOL, False),
    LiveKey("name_words", "trigger.name.words", WORD_LIST, ""),
    LiveKey(
        "name_anywhere", "trigger.name.anywhere", BOOL, True,
        note="关掉就只认句首",
    ),
    LiveKey("keyword_words", "trigger.keyword.words", WORD_LIST, ""),
    LiveKey(
        "random_probability", "trigger.random.probability", float_in(min_value=0.0, max_value=1.0), 0.02,
    ),
    LiveKey("llm_intent_enabled", "trigger.llm_intent.enabled", BOOL, False),
    LiveKey("llm_intent_node_id", "trigger.llm_intent.node_id", PLAIN_STR, "qq.intent"),
    LiveKey(
        "llm_intent_timeout_sec", "trigger.llm_intent.timeout_sec", float_in(min_value=1.0, max_value=60.0), 8.0,
    ),
    LiveKey(
        "llm_intent_max_inflight", "trigger.llm_intent.max_inflight", int_in(min_value=1, max_value=16), 1,
        note="同时在跑的意愿判断上限。engine worker 只有两三个，占满了真实对话就得排队",
    ),
    LiveKey(
        "llm_intent_context_messages", "trigger.llm_intent.context_messages",
        int_in(min_value=0, max_value=50), 8,
        note="带给判定模型的最近几条群聊。0 = 只看当前这一条",
    ),
    LiveKey(
        "cooldown_group_sec", "trigger.cooldown.per_group_sec", float_in(min_value=0.0), 0.0,
        note="0 = 不限。冷却管的是「命中也不说」",
    ),
    LiveKey("cooldown_user_sec", "trigger.cooldown.per_user_sec", float_in(min_value=0.0), 0.0),
    LiveKey(
        "cooldown_exempt_at", "trigger.cooldown.exempt_at", BOOL, True,
        note="被 @ 时无视冷却；关掉会让用户直接叫 bot 却被静默忽略",
    ),
    # 输出格式
    LiveKey(
        "strip_asterisk_styles", "output.strip_asterisk_styles", BOOL, True,
        env=("ONEBOT_STRIP_MARKDOWN_ASTERISK_STYLES",),
        derive_default=_legacy_strip_markdown_default(True),
    ),
    LiveKey(
        "strip_underscore_styles", "output.strip_underscore_styles", BOOL, False,
        env=("ONEBOT_STRIP_MARKDOWN_UNDERSCORE_STYLES",),
        derive_default=_legacy_strip_markdown_default(False),
    ),
    LiveKey(
        "reply_to_trigger", "output.reply_to_trigger", BOOL, True,
        env=("ONEBOT_REPLY_TO_TRIGGER",),
    ),
    LiveKey(
        "message_limit", "output.message_limit", int_in(min_value=500), 4300,
        env=("ONEBOT_QQ_MESSAGE_LIMIT",),
    ),
    LiveKey(
        "history_text_limit", "output.history_text_limit", int_in(min_value=50), 400,
        env=("ONEBOT_HISTORY_TEXT_LIMIT",),
    ),
    LiveKey(
        "face_prompt_limit", "output.face_prompt_limit", int_in(min_value=0, max_value=200), 50,
        env=("ONEBOT_CUSTOM_FACE_PROMPT_LIMIT",),
    ),
    # 默认关有实测原因：所有 QQ 事件走同一条反向 WS 并在 SDK 里串行 await，而
    # send_group_forward_msg 把几张大图一次交给 NapCat 要几十秒，期间这条 WS 上的
    # 其它 call_api 全部排队，整个 bot 卡死。逐张直发已用超时容错解决了重复发/丢图。
    LiveKey(
        "enable_image_forward_merge", "output.image_forward_merge", BOOL, False,
        env=("ONEBOT_ENABLE_IMAGE_FORWARD_MERGE",),
        note="合并转发会长时间占住那条唯一的反向 WS，出问题要能立刻关掉",
    ),
    LiveKey(
        "image_forward_merge_threshold", "output.image_forward_merge_threshold", int_in(min_value=2, max_value=16), 2,
        env=("ONEBOT_IMAGE_FORWARD_MERGE_THRESHOLD",),
    ),
    # 扩展能力
    LiveKey(
        "enable_reactions", "extensions.reactions", BOOL, True,
        env=("ONEBOT_ENABLE_REACTIONS",),
    ),
    LiveKey(
        "enable_auto_like", "extensions.auto_like", BOOL, False,
        env=("ONEBOT_ENABLE_AUTO_LIKE",),
    ),
    LiveKey(
        "auto_like_times", "extensions.auto_like_times", int_in(min_value=1, max_value=20), 10,
        env=("ONEBOT_AUTO_LIKE_TIMES",),
    ),
    LiveKey(
        "enable_preempt", "extensions.preempt", BOOL, False,
        env=("ONEBOT_ENABLE_PREEMPT",),
    ),
    LiveKey(
        "enable_echo", "extensions.echo.enabled", BOOL, False,
        env=("ONEBOT_ECHO_ENABLED",),
        note="连着几个人刷同一句时 Bot 也跟一条",
    ),
    LiveKey(
        "echo_threshold", "extensions.echo.threshold", int_in(min_value=2, max_value=10), 3,
        env=("ONEBOT_ECHO_THRESHOLD",),
        note="几条相同消息才跟，且必须来自不同的人",
    ),
    LiveKey(
        "echo_max_length", "extensions.echo.max_text_length", int_in(min_value=1, max_value=500), 30,
        env=("ONEBOT_ECHO_MAX_TEXT_LENGTH",),
        note="超过这个长度不跟：长文本复读是刷屏，不是接龙",
    ),
    LiveKey(
        "echo_window_sec", "extensions.echo.window_sec", float_in(min_value=1.0, max_value=3600.0), 120.0,
        env=("ONEBOT_ECHO_WINDOW_SEC",),
        note="复读要在这个时间窗内连续发生才算数",
    ),
    LiveKey(
        "echo_cooldown_sec", "extensions.echo.cooldown_sec", float_in(min_value=0.0, max_value=86400.0), 90.0,
        env=("ONEBOT_ECHO_COOLDOWN_SEC",),
        note="同一个群两次跟读之间至少隔这么久",
    ),
    LiveKey(
        "sticker_collect", "extensions.sticker.collect", BOOL, False,
        env=("ONEBOT_STICKER_COLLECT",),
        note="把群里的表情包攒进图库",
    ),
    LiveKey(
        "sticker_auto_tag", "extensions.sticker.auto_tag", BOOL, True,
        env=("ONEBOT_STICKER_AUTO_TAG",),
        note="收进来的图自动打标签。关掉就不再花看图渠道的钱，但没标签的图检索不到",
    ),
    LiveKey(
        "sticker_strategy", "extensions.sticker.strategy", PLAIN_STR, "loose",
        env=("ONEBOT_STICKER_STRATEGY",),
        note="收多严：strict 只收 QQ 标了表情包的，loose 再放行看着不像截图的，none 全收",
    ),
    LiveKey(
        "sticker_groups", "extensions.sticker.groups", ID_LIST, (),
        env=("ONEBOT_STICKER_GROUPS",),
        note="只从这些群收，留空表示所有已授权的群",
    ),
    LiveKey(
        "sticker_auto_accept", "extensions.sticker.auto_accept", BOOL, False,
        env=("ONEBOT_STICKER_AUTO_ACCEPT",),
        note="收到就直接入库，跳过人工筛选",
    ),
    LiveKey(
        "sticker_pending_limit", "extensions.sticker.pending_limit", int_in(min_value=0, max_value=5000), 200,
        env=("ONEBOT_STICKER_PENDING_LIMIT",),
        note="待审池上限，满了不再收新的",
    ),
    LiveKey(
        "sticker_library_limit", "extensions.sticker.library_limit", int_in(min_value=0, max_value=20000), 1000,
        env=("ONEBOT_STICKER_LIBRARY_LIMIT",),
        note="图库上限，超出时最旧的先出局",
    ),
    LiveKey(
        "sticker_pending_ttl_days", "extensions.sticker.pending_ttl_days", int_in(min_value=0, max_value=365), 7,
        env=("ONEBOT_STICKER_PENDING_TTL_DAYS",),
        note="待审池里放这么多天还没人管就丢掉",
    ),
    LiveKey(
        "sticker_send_probability", "extensions.sticker.send_probability",
        float_in(min_value=0.0, max_value=1.0), 0.2,
        env=("ONEBOT_STICKER_SEND_PROBABILITY",),
        note="每轮多大概率把表情包清单给模型看。0 表示从不发；给了它也可以不发",
    ),
    LiveKey(
        "sticker_prompt_limit", "extensions.sticker.prompt_limit", int_in(min_value=0, max_value=200), 60,
        env=("ONEBOT_STICKER_PROMPT_LIMIT",),
        note="清单最多列几张。库比这个大就只列发得最少的那些，轮着上",
    ),
    LiveKey(
        "sticker_repeat_window_sec", "extensions.sticker.repeat_window_sec",
        int_in(min_value=0, max_value=86400), 1800,
        env=("ONEBOT_STICKER_REPEAT_WINDOW_SEC",),
        note="同一个会话里这段时间内发过的图不再发",
    ),
    LiveKey(
        "sticker_combat", "extensions.sticker.combat", BOOL, False,
        env=("ONEBOT_STICKER_COMBAT",),
        note="群里连着刷图时跟着接一张；阈值和冷却用内置默认值",
    ),
    LiveKey(
        "sticker_burst_probability", "extensions.sticker.burst_probability",
        float_in(min_value=0.0, max_value=1.0), 0.0,
        env=("ONEBOT_STICKER_BURST_PROBABILITY",),
        note="自己发完图后再补一张的概率，0 表示不补",
    ),
    # 输入能力
    LiveKey(
        "enable_image_input", "input.image", BOOL, True,
        env=("ONEBOT_ENABLE_IMAGE_INPUT",),
    ),
    LiveKey(
        "max_images_per_turn", "input.max_images_per_turn", int_in(min_value=1, max_value=16), 4,
        env=("ONEBOT_MAX_IMAGES_PER_TURN",),
    ),
    LiveKey(
        "image_max_bytes", "input.image_max_bytes", int_in(min_value=1024), 10 * 1024 * 1024,
        env=("ONEBOT_IMAGE_MAX_BYTES",),
    ),
    LiveKey(
        "image_wait_after_text_sec", "input.image_wait_after_text_sec", float_in(min_value=0.0, max_value=10.0), 2.5,
        env=("ONEBOT_IMAGE_WAIT_AFTER_TEXT_SECONDS",),
    ),
    LiveKey(
        "enable_file_input", "input.file", BOOL, True,
        env=("ONEBOT_ENABLE_FILE_INPUT",),
    ),
    LiveKey(
        "max_files_per_turn", "input.max_files_per_turn", int_in(min_value=1, max_value=16), 3,
        env=("ONEBOT_MAX_FILES_PER_TURN",),
    ),
    LiveKey(
        "file_max_bytes", "input.file_max_bytes", int_in(min_value=1024), 50 * 1024 * 1024,
        env=("ONEBOT_FILE_MAX_BYTES",),
    ),
    LiveKey(
        "file_extra_allowed_extensions", "input.file_extra_allowed_extensions", WORD_LIST, "",
        env=("ONEBOT_FILE_EXTRA_ALLOWED_EXTENSIONS",),
        note="在内置白名单之外额外放行的入站文件扩展名；可执行/脚本宿主类型不受此项影响",
    ),
    LiveKey(
        "enable_forward_msg_input", "input.forward_msg", BOOL, True,
        env=("ONEBOT_ENABLE_FORWARD_MSG_INPUT",),
    ),
    LiveKey(
        "forward_msg_max_depth", "input.forward_msg_max_depth", int_in(min_value=0, max_value=8), 3,
        env=("ONEBOT_FORWARD_MSG_MAX_DEPTH",),
    ),
    LiveKey(
        "forward_msg_max_messages", "input.forward_msg_max_messages", int_in(min_value=1, max_value=500), 80,
        env=("ONEBOT_FORWARD_MSG_MAX_MESSAGES",),
    ),
    LiveKey(
        "forward_msg_text_limit", "input.forward_msg_text_limit", int_in(min_value=500, max_value=100000), 12000,
        env=("ONEBOT_FORWARD_MSG_TEXT_LIMIT",),
    ),
    LiveKey(
        "forward_msg_timeout_sec", "input.forward_msg_timeout_sec", float_in(min_value=0.5, max_value=60.0), 8.0,
        env=("ONEBOT_FORWARD_MSG_TIMEOUT_SECONDS",),
        note="展开一条消息里全部转发卡片的总时限；超时按未展开处理",
    ),
    LiveKey(
        "forward_msg_expand_for_trigger", "input.forward_msg_expand_for_trigger", BOOL, True,
        env=("ONEBOT_FORWARD_MSG_EXPAND_FOR_TRIGGER",),
        note="关掉后群触发判定只看到占位符，转发消息要 @ 或回复才理",
    ),
    LiveKey(
        "enable_forward_msg_media", "input.forward_msg_media", BOOL, True,
        env=("ONEBOT_ENABLE_FORWARD_MSG_MEDIA",),
        note="转发卡片里的图片和文件是否一并采集；条数仍受 max_images_per_turn / max_files_per_turn 限制",
    ),
    # 队列
    LiveKey(
        "enable_queue", "queue.enabled", BOOL, False,
        env=("ONEBOT_ENABLE_QQ_QUEUE",), reload=RELOAD_RECONCILED,
        note="开关翻转要增删 worker task",
    ),
    LiveKey(
        "queue_workers", "queue.workers", int_in(min_value=1, max_value=32), 4,
        env=("ONEBOT_QQ_QUEUE_WORKERS",), reload=RELOAD_RECONCILED,
        note="改数量要增删 worker task",
    ),
    LiveKey(
        "queue_interval", "queue.interval_sec", float_in(min_value=0.0), 2.0,
        env=("ONEBOT_QQ_QUEUE_INTERVAL",),
    ),
    LiveKey(
        "queue_wait_for_reply", "queue.wait_for_reply", BOOL, False,
        env=("ONEBOT_QQ_QUEUE_WAIT_FOR_REPLY",),
    ),
    LiveKey(
        "queue_reply_timeout", "queue.reply_timeout_sec", float_in(min_value=1.0), 120.0,
        env=("ONEBOT_QQ_QUEUE_REPLY_TIMEOUT",),
    ),
    # 审批
    LiveKey(
        "approval_reply_unique_fallback", "approval.reply_unique_fallback", BOOL, False,
        env=("ONEBOT_APPROVAL_REPLY_UNIQUE_FALLBACK",),
        note="引用审批卡片但取不到 approval_id 时，回退到「当前唯一待审批」；开着更好用，也更容易误批",
    ),
    LiveKey(
        "approval_bare_verb_unique", "approval.bare_verb_unique", BOOL, True,
        env=("ONEBOT_APPROVAL_BARE_VERB_UNIQUE",),
        note="不引用卡片、整句只发「同意」时按当前唯一待审批处理；要求那张卡片确实发给本人",
    ),
    # 群历史
    LiveKey(
        "group_history_max", "history.group_max", int_in(min_value=0), 20,
        env=("ONEBOT_GROUP_HISTORY_MAX",), reload=RELOAD_RECONCILED,
        note="改条数要重建每个群的 deque",
    ),
    LiveKey(
        "group_history_undelivered_ratio", "history.undelivered_ratio", float_in(min_value=1.0, max_value=10.0), 1.0,
        env=("ONEBOT_GROUP_HISTORY_UNDELIVERED_RATIO",), reload=RELOAD_RECONCILED,
        note="改倍数会改变 deque 硬上限，同样要重建",
    ),
    # 转发 Bridge
    LiveKey(
        "enable_forward_bridge", "forward_bridge.enabled", BOOL, True,
        env=("ONEBOT_ENABLE_FORWARD_BRIDGE",), reload=RELOAD_RECONCILED,
        note="开关翻转要起停本地 HTTP server",
    ),
    LiveKey(
        "forward_bridge_max_messages", "forward_bridge.max_messages", int_in(min_value=1, max_value=200), 30,
        env=("ONEBOT_FORWARD_BRIDGE_MAX_MESSAGES",),
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
_config = LiveConfig(LIVE_KEYS, path=config_path, derive=_derived, derived_names=DERIVED_NAMES)

LIVE_KEYS_BY_NAME: Mapping[str, LiveKey] = _config.keys_by_name
RECONCILED_KEYS: tuple[str, ...] = _config.reconciled_keys
live = _config.live
refresh = _config.refresh
current = _config.current
pinned = _config.pinned
invalidate = _config.invalidate
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
