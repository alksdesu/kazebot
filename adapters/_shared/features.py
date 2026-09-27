"""全平台共用的功能设置：触发、复读、队列、表情包这类每个平台都要的项，默认值写在全局文件里。

平台文件里写同一路径就是这个平台单独覆盖；表情包图库那几项只认全局文件，平台文件里的老写法只兜底。
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .live_config import (
    BOOL,
    GLOBAL_AND_PLATFORM,
    GLOBAL_ONLY,
    LAYER_GLOBAL,
    PLAIN_STR,
    PREFIX_LIST,
    RELOAD_RECONCILED,
    TRIGGER_MODE,
    TRISTATE_BOOL,
    WORD_LIST,
    Coercer,
    LiveConfig,
    LiveKey,
    float_in,
    int_in,
)

FEATURES_CONFIG_PATH = "config/features.yaml"
# 测试和非常规部署改位置用，和各平台配置文件的路径覆盖一个意思。
FEATURES_CONFIG_ENV = "CLONOTH_FEATURES_CONFIG_PATH"


def _overridable(name: str, path: str, coerce: Coercer, default: Any, **extra: Any) -> LiveKey:
    return LiveKey(name, path, coerce, default, layers=GLOBAL_AND_PLATFORM, **extra)


def _global_only(name: str, path: str, coerce: Coercer, default: Any, **extra: Any) -> LiveKey:
    # 图库全平台只有一份，按平台各设一个上限说不通。
    return LiveKey(name, path, coerce, default, layers=GLOBAL_ONLY, **extra)


FEATURE_KEYS: tuple[LiveKey, ...] = (
    # 审批
    _overridable(
        "approval_reply_unique_fallback", "approval.reply_unique_fallback", BOOL, False,
        note="引用审批卡片但取不到 approval_id 时，回退到「当前唯一待审批」；开着更好用，也更容易误批",
    ),
    _overridable(
        "approval_bare_verb_unique", "approval.bare_verb_unique", BOOL, True,
        note="不引用卡片、整句只发「同意」时按当前唯一待审批处理；要求那张卡片确实发给本人",
    ),
    # 触发
    _overridable("group_trigger", "trigger.group_mode", TRIGGER_MODE, "mention_only"),
    # ／ 必须在列：命令正则认全角斜杠，但它不在触发前缀里的话，群里发 ／帮助 连
    # 触发都触发不了，用户只会看到 bot 对半角斜杠有反应、对全角没有。
    _overridable("trigger_prefixes", "trigger.prefixes", PREFIX_LIST, "!,！,/,／"),
    # 七个信号开关。前四个是三态：没配就跟随 group_mode 的旧语义（见 trigger_policy）。
    _overridable("signal_all", "trigger.signals.all", TRISTATE_BOOL, None),
    _overridable("signal_at", "trigger.signals.at", TRISTATE_BOOL, None),
    _overridable("signal_reply", "trigger.signals.reply", TRISTATE_BOOL, None),
    _overridable("signal_prefix", "trigger.signals.prefix", TRISTATE_BOOL, None),
    _overridable("signal_name", "trigger.signals.name", BOOL, False),
    _overridable("signal_keyword", "trigger.signals.keyword", BOOL, False),
    _overridable("signal_random", "trigger.signals.random", BOOL, False),
    _overridable("name_words", "trigger.name.words", WORD_LIST, ""),
    _overridable("name_anywhere", "trigger.name.anywhere", BOOL, True, note="关掉就只认句首"),
    _overridable("keyword_words", "trigger.keyword.words", WORD_LIST, ""),
    _overridable("random_probability", "trigger.random.probability", float_in(min_value=0.0, max_value=1.0), 0.02),
    _overridable("llm_intent_enabled", "trigger.llm_intent.enabled", BOOL, False),
    _overridable("llm_intent_node_id", "trigger.llm_intent.node_id", PLAIN_STR, "chat.intent"),
    _overridable(
        "llm_intent_timeout_sec", "trigger.llm_intent.timeout_sec", float_in(min_value=1.0, max_value=60.0), 8.0,
    ),
    _overridable(
        "llm_intent_max_inflight", "trigger.llm_intent.max_inflight", int_in(min_value=1, max_value=16), 1,
        note="同时在跑的意愿判断上限。engine worker 只有两三个，占满了真实对话就得排队",
    ),
    _overridable(
        "llm_intent_context_messages", "trigger.llm_intent.context_messages",
        int_in(min_value=0, max_value=50), 8,
        note="带给判定模型的最近几条群聊。0 = 只看当前这一条",
    ),
    _overridable(
        "cooldown_group_sec", "trigger.cooldown.per_group_sec", float_in(min_value=0.0), 0.0,
        note="0 = 不限。冷却管的是「命中也不说」",
    ),
    _overridable("cooldown_user_sec", "trigger.cooldown.per_user_sec", float_in(min_value=0.0), 0.0),
    _overridable(
        "cooldown_exempt_at", "trigger.cooldown.exempt_at", BOOL, True,
        note="被 @ 时无视冷却；关掉会让用户直接叫 bot 却被静默忽略",
    ),
    # 输出
    _overridable("reply_to_trigger", "output.reply_to_trigger", BOOL, True),
    _overridable("history_text_limit", "output.history_text_limit", int_in(min_value=50), 400),
    # 群互动
    _overridable("enable_preempt", "extensions.preempt", BOOL, False),
    _overridable(
        "enable_echo", "extensions.echo.enabled", BOOL, False,
        note="连着几个人刷同一句时 Bot 也跟一条",
    ),
    _overridable(
        "echo_threshold", "extensions.echo.threshold", int_in(min_value=2, max_value=10), 3,
        note="几条相同消息才跟，且必须来自不同的人",
    ),
    _overridable(
        "echo_max_length", "extensions.echo.max_text_length", int_in(min_value=1, max_value=500), 30,
        note="超过这个长度不跟：长文本复读是刷屏，不是接龙",
    ),
    _overridable(
        "echo_window_sec", "extensions.echo.window_sec", float_in(min_value=1.0, max_value=3600.0), 120.0,
        note="复读要在这个时间窗内连续发生才算数",
    ),
    _overridable(
        "echo_cooldown_sec", "extensions.echo.cooldown_sec", float_in(min_value=0.0, max_value=86400.0), 90.0,
        note="同一个群两次跟读之间至少隔这么久",
    ),
    # 表情包图库
    _global_only(
        "sticker_auto_tag", "extensions.sticker.auto_tag", BOOL, True,
        note="收进来的图自动打标签。关掉就不再花看图渠道的钱，但没标签的图检索不到",
    ),
    _global_only(
        "sticker_auto_accept", "extensions.sticker.auto_accept", BOOL, False,
        note="收到就直接入库，跳过人工筛选",
    ),
    _global_only(
        "sticker_pending_limit", "extensions.sticker.pending_limit", int_in(min_value=0, max_value=5000), 200,
        note="待审池上限，满了不再收新的",
    ),
    _global_only(
        "sticker_library_limit", "extensions.sticker.library_limit", int_in(min_value=0, max_value=20000), 1000,
        note="图库上限，超出时最旧的先出局",
    ),
    _global_only(
        "sticker_pending_ttl_days", "extensions.sticker.pending_ttl_days", int_in(min_value=0, max_value=365), 7,
        note="待审池里放这么多天还没人管就丢掉",
    ),
    # 表情包挑选
    _overridable(
        "sticker_send_probability", "extensions.sticker.send_probability",
        float_in(min_value=0.0, max_value=1.0), 0.2,
        note="每轮多大概率把表情包清单给模型看。0 表示从不发；给了它也可以不发",
    ),
    _overridable(
        "sticker_prompt_limit", "extensions.sticker.prompt_limit", int_in(min_value=0, max_value=200), 60,
        note="清单最多列几张。库比这个大就只列发得最少的那些，轮着上",
    ),
    _overridable(
        "sticker_repeat_window_sec", "extensions.sticker.repeat_window_sec",
        int_in(min_value=0, max_value=86400), 1800,
        note="同一个会话里这段时间内发过的图不再发",
    ),
    _overridable(
        "sticker_combat", "extensions.sticker.combat", BOOL, False,
        note="群里连着刷图时跟着接一张；阈值和冷却用内置默认值",
    ),
    _overridable(
        "sticker_burst_probability", "extensions.sticker.burst_probability",
        float_in(min_value=0.0, max_value=1.0), 0.0,
        note="自己发完图后再补一张的概率，0 表示不补",
    ),
    # 输入
    _overridable("enable_image_input", "input.image", BOOL, True),
    _overridable("max_images_per_turn", "input.max_images_per_turn", int_in(min_value=1, max_value=16), 4),
    _overridable("image_max_bytes", "input.image_max_bytes", int_in(min_value=1024), 10 * 1024 * 1024),
    _overridable(
        "image_wait_after_text_sec", "input.image_wait_after_text_sec", float_in(min_value=0.0, max_value=10.0), 2.5,
    ),
    _overridable("enable_file_input", "input.file", BOOL, True),
    _overridable("max_files_per_turn", "input.max_files_per_turn", int_in(min_value=1, max_value=16), 3),
    _overridable("file_max_bytes", "input.file_max_bytes", int_in(min_value=1024), 50 * 1024 * 1024),
    _overridable(
        "file_extra_allowed_extensions", "input.file_extra_allowed_extensions", WORD_LIST, "",
        note="在内置白名单之外额外放行的入站文件扩展名；可执行/脚本宿主类型不受此项影响",
    ),
    # 队列
    _overridable(
        "enable_queue", "queue.enabled", BOOL, False, reload=RELOAD_RECONCILED,
        note="开关翻转要增删 worker task",
    ),
    _overridable(
        "queue_workers", "queue.workers", int_in(min_value=1, max_value=32), 4, reload=RELOAD_RECONCILED,
        note="改数量要增删 worker task",
    ),
    _overridable("queue_interval", "queue.interval_sec", float_in(min_value=0.0), 2.0),
    _overridable("queue_wait_for_reply", "queue.wait_for_reply", BOOL, False),
    _overridable("queue_reply_timeout", "queue.reply_timeout_sec", float_in(min_value=1.0), 120.0),
    # 群历史
    _overridable(
        "group_history_max", "history.group_max", int_in(min_value=0), 20, reload=RELOAD_RECONCILED,
        note="改条数要重建每个群的 deque",
    ),
    _overridable(
        "group_history_undelivered_ratio", "history.undelivered_ratio",
        float_in(min_value=1.0, max_value=10.0), 1.0, reload=RELOAD_RECONCILED,
        note="改倍数会改变 deque 硬上限，同样要重建",
    ),
)

FEATURE_KEYS_BY_NAME: Mapping[str, LiveKey] = MappingProxyType({key.name: key for key in FEATURE_KEYS})


def features_config_path(workspace_root: Path) -> Path:
    override = os.environ.get(FEATURES_CONFIG_ENV, "").strip()
    return Path(override) if override else Path(workspace_root) / FEATURES_CONFIG_PATH


def new_features_config(workspace_root: Path) -> LiveConfig:
    """全局默认那一层。实例由使用方持有：共享层整个进程只有一份，缓存放这里会被几次加载共用。"""
    root = Path(workspace_root)
    return LiveConfig(FEATURE_KEYS, path=lambda: features_config_path(root), layer=LAYER_GLOBAL)


def adopt(name: str, *, env: tuple[str, ...] = ()) -> LiveKey:
    """平台键表里的全平台设置：路径、解析、默认值和层都跟着全局声明走，平台只补自己早就有的 env 兜底。"""
    return dataclasses.replace(FEATURE_KEYS_BY_NAME[name], env=env)
