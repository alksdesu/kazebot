"""QQ 适配层热载配置。

config/qq.yaml 改完即生效，不重启进程。每个键的 yaml 路径、env 兜底、解析器和生效方式
集中声明在 LIVE_KEYS，live 读取 / /qq/state 快照 / 接线测试共用这一份声明。
"""
from __future__ import annotations

import copy
import logging
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping

import yaml

from . import capability
from .config import (
    CLONOTH_WORKSPACE,
    GROUP_TRIGGER_MODES,
    QQ_ID_MAX,
    QQ_ID_MIN,
    QQ_ID_PATTERN,
    parse_prefix_list,
    parse_qq_id_item,
    parse_qq_id_list,
)
from .yaml_loader import load_yaml

logger = logging.getLogger("nonebot.plugin.clonoth_agent.live_config")

# 生效方式。live 读到即生效；reconciled 的键改了以后还要有人去重建运行期对象
# （deque、worker task、HTTP server），由 reconcile_live_config 负责。
RELOAD_LIVE = "live"
RELOAD_RECONCILED = "reconciled"


# ── 类型转换 ───────────────────────────────────

@dataclass(frozen=True)
class Coercer:
    """把 yaml 值或 env 字符串转成运行期类型。"""

    convert: Callable[[Any, Any], Any]
    # 布尔开关沿用 _env_bool 的读法：env 设成空串就是「关」，不继续往后找下一个 env 名。
    # 列表/字符串相反 —— 空串等于没配，继续 fallback。
    empty_env_is_value: bool = False
    # 列表类键的单项规则。控制台按这份规则校验新增项，跨语言只能靠公布同一份数据对齐。
    # compare=False：只读映射不可哈希，算进字段就会让这个 frozen dataclass 失去 __hash__。
    input_rule: Mapping[str, Any] | None = field(default=None, compare=False)

    def __call__(self, raw: Any, default: Any) -> Any:
        return self.convert(raw, default)


_TRUTHY = {"1", "true", "yes", "y", "on"}
_FALSY = {"0", "false", "no", "off", ""}


def _coerce_bool(raw: Any, default: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    token = str(raw if raw is not None else "").strip().lower()
    if token in _FALSY:
        return False
    if token in _TRUTHY:
        return True
    # 既不是真值也不是假值的字面量（"maybe"、"是"）不该悄悄变成 True。
    logger.warning("live config: unrecognized boolean %r, using %r", raw, default)
    return bool(default)


BOOL = Coercer(_coerce_bool, empty_env_is_value=True)


def int_in(min_value: int | None = None, max_value: int | None = None) -> Coercer:
    def convert(raw: Any, default: Any) -> int:
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            value = int(default)
        if min_value is not None:
            value = max(min_value, value)
        if max_value is not None:
            value = min(max_value, value)
        return value

    return Coercer(convert)


def float_in(min_value: float | None = None, max_value: float | None = None) -> Coercer:
    def convert(raw: Any, default: Any) -> float:
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            value = float(default)
        if min_value is not None:
            value = max(min_value, value)
        if max_value is not None:
            value = min(max_value, value)
        return value

    return Coercer(convert)


# 单项规则由 bot 公布给控制台，值域上下界与 pattern 都取 config 里那一份，
# 免得公布的规则和 coercer 实际执行的规则各说一套。
ID_INPUT_RULE: Mapping[str, Any] = MappingProxyType({
    "kind": "id",
    "pattern": QQ_ID_PATTERN,
    "min": QQ_ID_MIN,
    "max": QQ_ID_MAX,
})
# 词表单项：去首尾空白、非空、按原样去重。
WORD_INPUT_RULE: Mapping[str, Any] = MappingProxyType({"kind": "word", "trim": True})


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


def _warn_bool_literal(raw: Any) -> None:
    logger.warning("live config: %r is a YAML boolean; quote it to use it as text", raw)


def _coerce_prefix_list(raw: Any, default: Any) -> tuple[str, ...]:
    if isinstance(raw, bool):
        _warn_bool_literal(raw)
        raw = default
    if isinstance(raw, (list, tuple)):
        out: list[str] = []
        for item in raw:
            # 想拿 true 当词就得加引号：YAML 1.2 里裸 true 就是布尔，别的工具也这么读。
            if isinstance(item, bool):
                _warn_bool_literal(item)
                continue
            token = str(item if item is not None else "").strip()
            if token and token not in out:
                out.append(token)
        return tuple(out)
    return parse_prefix_list(str(raw or ""))


PREFIX_LIST = Coercer(_coerce_prefix_list, input_rule=WORD_INPUT_RULE)


def _coerce_trigger_mode(raw: Any, default: Any) -> str:
    if isinstance(raw, bool):
        _warn_bool_literal(raw)
        return str(default)
    return str(raw or "").strip().lower() or str(default)


TRIGGER_MODE = Coercer(_coerce_trigger_mode)


def _coerce_tristate_bool(raw: Any, default: Any) -> bool | None:
    """三态开关：None = 没配，由消费方回落旧语义。

    七个触发信号里有四个（all/at/reply/prefix）在旧的 group_mode 枚举下已经有确定行为，
    直接给 True/False 会在升级时静默改变判定，所以要能表达「这一项我没管」。
    """
    if raw is None:
        return None
    return _coerce_bool(raw, default if default is not None else False)


# 空 env 只能算「没配」：三态的 False 是运营者的显式选择，空串表达不了它。
TRISTATE_BOOL = Coercer(_coerce_tristate_bool)


def _coerce_word_list(raw: Any, default: Any) -> tuple[str, ...]:
    """名字/关键词列表。和触发前缀同样的解析，只是不做前缀那套剥离约定。"""
    return _coerce_prefix_list(raw, default)


WORD_LIST = Coercer(_coerce_word_list, input_rule=WORD_INPUT_RULE)


def _coerce_plain_str(raw: Any, default: Any) -> str:
    if isinstance(raw, bool):
        _warn_bool_literal(raw)
        return str(default)
    return str(raw if raw is not None else "").strip() or str(default)


PLAIN_STR = Coercer(_coerce_plain_str)


def grant_for(capability_key: str) -> Coercer:
    """能力授权档。夹紧就在这里做一次，七个判定点都不必再操心上限。"""
    return Coercer(lambda raw, default: capability.clamp(capability_key, raw))


# ── 键声明 ───────────────────────────────────

@dataclass(frozen=True)
class LiveKey:
    """一个热载键。name 是 call site 用的属性名，path 是 qq.yaml 里的点分路径。"""

    name: str
    path: str
    coerce: Coercer
    default: Any
    env: tuple[str, ...] = ()
    reload: str = RELOAD_LIVE
    # env 也没配时的兜底。只给那几个「默认值本身要从别的 env 推导」的键用。
    derive_default: Callable[[], Any] | None = None
    note: str = ""

    def resolve(self, document: Mapping[str, Any]) -> Any:
        raw = _dig(document, self.path)
        if raw is not None:
            return self.coerce(raw, self.default)
        for env_name in self.env:
            value = os.environ.get(env_name)
            if value is None:
                continue
            if not value.strip() and not self.coerce.empty_env_is_value:
                continue
            return self.coerce(value, self.default)
        if self.derive_default is not None:
            return self.coerce(self.derive_default(), self.default)
        return self.coerce(self.default, self.default)


def _dig(document: Mapping[str, Any], dotted: str) -> Any:
    cur: Any = document
    for part in dotted.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _legacy_strip_markdown_default(fallback: bool) -> Callable[[], Any]:
    """ONEBOT_STRIP_MARKDOWN_STYLES 是星号/下划线拆分前的旧总开关。

    设了它就同时决定两个新开关的默认值，没设则各自用自己的默认。
    """

    def derive() -> Any:
        raw = os.environ.get("ONEBOT_STRIP_MARKDOWN_STYLES")
        return fallback if raw is None else _coerce_bool(raw, False)

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

LIVE_KEYS_BY_NAME: Mapping[str, LiveKey] = MappingProxyType({key.name: key for key in LIVE_KEYS})

RECONCILED_KEYS: tuple[str, ...] = tuple(k.name for k in LIVE_KEYS if k.reload == RELOAD_RECONCILED)

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


# ── 加载 ───────────────────────────────────

def config_path() -> Path:
    """config/qq.yaml 的位置。env 覆盖只为测试与非常规部署留口子。"""
    override = os.environ.get("CLONOTH_QQ_CONFIG_PATH", "").strip()
    if override:
        return Path(override)
    return Path(CLONOTH_WORKSPACE) / "config" / "qq.yaml"


@dataclass
class _Cache:
    path: str = ""
    # None = 强制重解析。和 valid 分开两个字段：invalidate() 只能让缓存过期，
    # 不能把「上一份有效配置」一起丢掉 —— 否则写坏 yaml 时白名单会塌成空。
    stamp: tuple[int, int] | None = None
    # values 是从哪个文件版本来的。和 stamp 不同：解析失败时 stamp 记的是坏文件，
    # values 还是旧的那份 —— 外部要靠这个才能区分「已生效」和「还在用旧配置」。
    values_stamp: tuple[int, int] = (0, 0)
    document: Mapping[str, Any] = field(default_factory=dict)
    values: Mapping[str, Any] = field(default_factory=dict)
    error: str = ""
    valid: bool = False


_cache = _Cache()
_cache_lock = threading.Lock()

_SNAPSHOT: ContextVar[Mapping[str, Any] | None] = ContextVar("qq_live_snapshot", default=None)


def _file_stamp(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
    except OSError:
        return (0, 0)
    return (st.st_mtime_ns, st.st_size)


def _build(document: Mapping[str, Any]) -> Mapping[str, Any]:
    values: dict[str, Any] = {key.name: key.resolve(document) for key in LIVE_KEYS}
    _derived(values)
    return MappingProxyType(values)


def _reload_if_changed() -> Mapping[str, Any]:
    """按 mtime+size 判断要不要重解析；解析失败保留上一份有效配置。"""
    path = config_path()
    stamp = _file_stamp(path)
    key = str(path)
    with _cache_lock:
        if _cache.valid and _cache.path == key and _cache.stamp == stamp:
            return _cache.values

        document: Mapping[str, Any] | None = {}
        error = ""
        if stamp != (0, 0):
            try:
                loaded = load_yaml(path.read_text(encoding="utf-8"))
            except Exception as exc:
                document = None
                error = f"{type(exc).__name__}: {exc}"
            else:
                if loaded is None:
                    document = {}
                elif isinstance(loaded, Mapping):
                    document = loaded
                else:
                    document = None
                    error = f"expected a mapping at the top level, got {type(loaded).__name__}"

        if document is None:
            # 保留上一份有效配置而不是回落到默认值：群白名单空 = 全群静默，
            # 管理员名单空 = 审批全拒 —— 写坏一个字符不该等于把 bot 关掉。
            # stamp 照旧记下，否则每次读都重试解析同一个坏文件、刷满日志。
            logger.error("qq.yaml is unusable (%s); keeping the previous configuration", error)
            had_valid = _cache.valid and _cache.path == key
            _cache.path = key
            _cache.stamp = stamp
            _cache.error = error
            if not had_valid:
                # 从没读到过有效内容（进程起来时文件就是坏的），只能用 env + 默认值兜。
                _cache.document = {}
                _cache.values = _build({})
                _cache.values_stamp = (0, 0)
                _cache.valid = True
            return _cache.values

        _cache.path = key
        _cache.stamp = stamp
        _cache.values_stamp = stamp
        _cache.document = document
        _cache.values = _build(document)
        _cache.error = ""
        _cache.valid = True
        return _cache.values


def refresh() -> Mapping[str, Any]:
    """重读配置并钉进当前上下文。

    在每个入口（消息 rule、Bridge 请求、队列取件）调一次，之后整条处理链读到的都是
    同一份快照 —— 运营者中途改白名单不会让处理到一半的消息换标准。
    """
    snapshot = _reload_if_changed()
    _SNAPSHOT.set(snapshot)
    return snapshot


def current() -> Mapping[str, Any]:
    """当前生效快照。上下文里钉过就用那份，没钉过就实时读文件。

    没钉过时选「实时」而不是「进程启动那份」：漏调 refresh 的后果变成多一次 stat，
    而不是配置永久冻结在启动时刻。
    """
    pinned = _SNAPSHOT.get()
    return pinned if pinned is not None else _reload_if_changed()


@contextmanager
def pinned(**overrides: Any) -> Iterator[Mapping[str, Any]]:
    """在当前上下文里临时替换若干键。测试与 /qq/trigger/dry-run 共用。"""
    unknown = sorted(set(overrides) - set(LIVE_KEYS_BY_NAME) - set(DERIVED_NAMES))
    if unknown:
        raise KeyError(f"unknown live config keys: {unknown}")
    values = dict(current())
    values.update(overrides)
    if "trigger_prefixes" in overrides and "trigger_prefixes_strippable" not in overrides:
        _derived(values)
    snapshot = MappingProxyType(values)
    token = _SNAPSHOT.set(snapshot)
    try:
        yield snapshot
    finally:
        _SNAPSHOT.reset(token)


class _Live:
    """`live.allowed_groups` 形式的读取入口。拼错键名立刻 AttributeError。"""

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        try:
            return current()[name]
        except KeyError:
            raise AttributeError(f"no live config key named {name!r}") from None

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("live config is read-only; edit config/qq.yaml or use pinned()")

    def __dir__(self) -> list[str]:
        return sorted(set(LIVE_KEYS_BY_NAME) | set(DERIVED_NAMES))


live = _Live()


def invalidate() -> None:
    """让文件缓存过期。别处改过 qq.yaml、而 mtime 粒度可能看不出来时调。

    只动缓存不动上下文里钉住的快照 —— 钉住的语义就是「这条消息处理期间配置不变」，
    别人改文件不该把它掀掉。
    """
    with _cache_lock:
        _cache.stamp = None


def save(document: Mapping[str, Any]) -> Mapping[str, Any]:
    """在本进程内写回 qq.yaml 并立刻重载，返回新的生效快照。

    生产写入在 supervisor（bot 没起来时也要能改配置），这里是测试与本地工具的入口。
    dump 阶段就抛掉表示不出来的值、同目录 tmp + replace，避免 bot 读到半截 yaml。
    """
    if not isinstance(document, Mapping):
        raise TypeError(f"qq.yaml must be a mapping, got {type(document).__name__}")
    text = yaml.safe_dump(dict(document), allow_unicode=True, sort_keys=False)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    invalidate()
    # 解开调用方自己钉住的快照：保存完立刻读到旧值会被当成「没生效」。
    _SNAPSHOT.set(None)
    return _reload_if_changed()


def raw_document() -> dict[str, Any]:
    """qq.yaml 的原始内容（已解析成 dict）。给 save() 的合并基底用。"""
    _reload_if_changed()
    with _cache_lock:
        return copy.deepcopy(dict(_cache.document))


def document_from(values: Mapping[str, Any], base: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """把 {键名: 值} 摊成 qq.yaml 的嵌套结构，可选择并进已有文档。

    调用方只认键名，点分路径只此一处知道 —— 换 yaml 布局不必改调用方。
    """
    unknown = sorted(set(values) - set(LIVE_KEYS_BY_NAME))
    if unknown:
        raise KeyError(f"unknown live config keys: {unknown}")
    document: dict[str, Any] = copy.deepcopy(dict(base or {}))
    for name, value in values.items():
        cursor = document
        parts = LIVE_KEYS_BY_NAME[name].path.split(".")
        for part in parts[:-1]:
            nested = cursor.get(part)
            if not isinstance(nested, dict):
                nested = {}
                cursor[part] = nested
            cursor = nested
        cursor[parts[-1]] = _jsonable(value)
    return document


def _jsonable(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        # 集合没有顺序，落盘前定序，否则每次写出的 yaml 都不一样。
        return sorted(value)
    if isinstance(value, (tuple, list)):
        return list(value)
    return value


def state_payload() -> dict[str, Any]:
    """bot 进程实际生效的配置快照 + 文件指纹。GET /qq/state 的主体。"""
    values = current()
    path = config_path()
    stamp = _file_stamp(path)
    with _cache_lock:
        error = _cache.error
        values_stamp = _cache.values_stamp
    return {
        "config_path": str(path),
        "file": {
            "exists": stamp != (0, 0),
            "mtime_ns": stamp[0],
            "size": stamp[1],
        },
        # values 实际来自哪个文件版本。和 file 不一致就说明这份文件没被采纳。
        "loaded": {
            "exists": values_stamp != (0, 0),
            "mtime_ns": values_stamp[0],
            "size": values_stamp[1],
        },
        "parse_error": error,
        "values": {name: _jsonable(values[name]) for name in sorted(values)},
        "reload": {key.name: key.reload for key in LIVE_KEYS},
        "notes": {key.name: key.note for key in LIVE_KEYS if key.note},
        # 点分路径一起公布：supervisor 校验 PUT 上来的 yaml 时要认得哪些路径有人消费，
        # 而它跑在另一个进程里，import 不到这张表（这个包的 __init__ 会拉起 nonebot）。
        "paths": {key.name: key.path for key in LIVE_KEYS},
        # 能力清单由 bot 公布而不是前端写死：加一项能力只该改这一处，
        # 漏掉的那项在界面上无声消失，运营者以为没有这回事。
        "capabilities": capability.catalog(),
        # 列表项规则同样由 bot 公布：前端重写一遍就会和 coercer 漂移，
        # 而漂移的表现是「界面收下了、保存后消失」。
        "input_rules": {
            key.name: dict(key.coerce.input_rule)
            for key in LIVE_KEYS
            if key.coerce.input_rule is not None
        },
    }
