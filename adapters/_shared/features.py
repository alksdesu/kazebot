"""全平台共用的功能设置：触发、复读、队列、表情包这类每个平台都要的项，默认值写在全局文件里。

平台文件里写同一路径就是这个平台单独覆盖；表情包图库那几项只认全局文件，平台文件里的老写法只兜底。
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .console_schema import build_schema, number, percent, text_input, toggle, tristate, word_list
from .features_layout import FEATURE_SECTIONS
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
from .trigger_policy import LEGACY_ALL_MODES, LEGACY_PREFIX_MODES

FEATURES_CONFIG_PATH = "config/features.yaml"
# 测试和非常规部署改位置用，和各平台配置文件的路径覆盖一个意思。
FEATURES_CONFIG_ENV = "CLONOTH_FEATURES_CONFIG_PATH"

_MB = 1024 * 1024


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
        ui=toggle("唯一待审批兜底", "引用了审批卡片却取不到编号时，按「当前唯一待审批」处理。"),
    ),
    _overridable(
        "approval_bare_verb_unique", "approval.bare_verb_unique", BOOL, True,
        note="不引用卡片、整句只发「同意」时按当前唯一待审批处理；要求那张卡片确实发给本人",
        ui=toggle("直接回同意", "不引用卡片、整句只发「同意」时，按当前唯一待审批处理。"),
    ),
    # 触发。group_mode 是被七个信号取代的旧枚举，控制台只读它推导三态的当前值，不给控件。
    _overridable("group_trigger", "trigger.group_mode", TRIGGER_MODE, "mention_only"),
    # ／ 必须在列：命令正则认全角斜杠，但它不在触发前缀里的话，群里发 ／帮助 连
    # 触发都触发不了，用户只会看到 bot 对半角斜杠有反应、对全角没有。
    _overridable(
        "trigger_prefixes", "trigger.prefixes", PREFIX_LIST, "!,！,/,／",
        ui=word_list("触发前缀", empty="没有前缀，前缀触发不会命中", placeholder="输入一个前缀，回车添加"),
    ),
    # 七个信号开关。前四个是三态：没配就跟随 group_mode 的旧语义（见 trigger_policy）。
    _overridable(
        "signal_all", "trigger.signals.all", TRISTATE_BOOL, None,
        ui=tristate(
            "全量回应", "群里任何人说任何话都回。开着的时候下面几条都不再影响判定。",
            follows="group_trigger", when=LEGACY_ALL_MODES,
        ),
    ),
    _overridable(
        "signal_at", "trigger.signals.at", TRISTATE_BOOL, None,
        ui=tristate("被 @", "消息里任何位置 @ 到 bot，不限句首。", unset=True),
    ),
    _overridable(
        "signal_reply", "trigger.signals.reply", TRISTATE_BOOL, None,
        ui=tristate("被回复", "有人引用 bot 说过的话。", unset=True),
    ),
    _overridable(
        "signal_prefix", "trigger.signals.prefix", TRISTATE_BOOL, None,
        ui=tristate("前缀触发", "以配置的前缀开头。", follows="group_trigger", when=LEGACY_PREFIX_MODES),
    ),
    _overridable(
        "signal_name", "trigger.signals.name", BOOL, False,
        ui=toggle("出现名字", "消息里提到 bot 的名字。原文会完整交给模型，不会被剥掉。"),
    ),
    _overridable(
        "signal_keyword", "trigger.signals.keyword", BOOL, False,
        ui=toggle("关键词插话", "聊到特定话题时主动接话。"),
    ),
    _overridable(
        "signal_random", "trigger.signals.random", BOOL, False,
        ui=toggle("随机插话", "按概率随便说两句。骰子在运行时才掷，试听里只标注为「看运行时」。"),
    ),
    _overridable(
        "name_words", "trigger.name.words", WORD_LIST, "",
        ui=word_list("认这些名字", empty="还没有名字，这一条不会命中", placeholder="输入一个名字，回车添加"),
    ),
    _overridable(
        "name_anywhere", "trigger.name.anywhere", BOOL, True, note="关掉就只认句首",
        ui=toggle("不限句首，句中提到也算"),
    ),
    _overridable(
        "keyword_words", "trigger.keyword.words", WORD_LIST, "",
        ui=word_list("听这些词", empty="还没有关键词，这一条不会命中", placeholder="输入一个关键词，回车添加"),
    ),
    _overridable(
        "random_probability", "trigger.random.probability", float_in(min_value=0.0, max_value=1.0), 0.02,
        ui=percent("每条消息"),
    ),
    _overridable(
        "llm_intent_enabled", "trigger.llm_intent.enabled", BOOL, False,
        ui=toggle(
            "让模型判断",
            "问一次轻量模型「这句话需要你接话吗」。每条不命中的群消息都会产生一次模型往返，务必配独立的便宜渠道。",
        ),
    ),
    _overridable(
        "llm_intent_node_id", "trigger.llm_intent.node_id", PLAIN_STR, "chat.intent",
        ui=text_input("判定节点"),
    ),
    _overridable(
        "llm_intent_timeout_sec", "trigger.llm_intent.timeout_sec", float_in(min_value=1.0, max_value=60.0), 8.0,
        ui=number("超时", step=0.5, unit="秒"),
    ),
    _overridable(
        "llm_intent_max_inflight", "trigger.llm_intent.max_inflight", int_in(min_value=1, max_value=16), 1,
        note="同时在跑的意愿判断上限。engine worker 只有两三个，占满了真实对话就得排队",
        ui=number("同时最多", unit="个"),
    ),
    _overridable(
        "llm_intent_context_messages", "trigger.llm_intent.context_messages",
        int_in(min_value=0, max_value=50), 8,
        note="带给判定模型的最近几条群聊。0 = 只看当前这一条",
        ui=number("带上最近", unit="条"),
    ),
    _overridable(
        "cooldown_group_sec", "trigger.cooldown.per_group_sec", float_in(min_value=0.0), 0.0,
        note="0 = 不限。冷却管的是「命中也不说」",
        ui=number("每群", unit="秒"),
    ),
    _overridable(
        "cooldown_user_sec", "trigger.cooldown.per_user_sec", float_in(min_value=0.0), 0.0,
        ui=number("每人", unit="秒"),
    ),
    _overridable(
        "cooldown_exempt_at", "trigger.cooldown.exempt_at", BOOL, True,
        note="被 @ 时无视冷却；关掉会让用户直接叫 bot 却被静默忽略",
        ui=toggle("被 @ 时无视冷却", "有人直接叫 bot，就不该被静默忽略。"),
    ),
    # 输出
    _overridable(
        "reply_to_trigger", "output.reply_to_trigger", BOOL, True,
        ui=toggle("引用原消息", "回复时引用触发它的那条消息，群里热闹时能看清在回谁。"),
    ),
    _overridable(
        "history_text_limit", "output.history_text_limit", int_in(min_value=50), 400,
        ui=number("历史里每条", step=50, unit="字"),
    ),
    # 群互动
    _overridable(
        "enable_preempt", "extensions.preempt", BOOL, False,
        ui=toggle("抢答", "上一条还没答完就来了新消息时，放弃旧的去答新的。"),
    ),
    _overridable(
        "enable_echo", "extensions.echo.enabled", BOOL, False,
        note="连着几个人刷同一句时 Bot 也跟一条",
        ui=toggle("跟复读", "连着几个人刷同一句时，也跟一条。"),
    ),
    _overridable(
        "echo_threshold", "extensions.echo.threshold", int_in(min_value=2, max_value=10), 3,
        note="几条相同消息才跟，且必须来自不同的人",
        ui=number("连续", unit="条才跟"),
    ),
    _overridable(
        "echo_max_length", "extensions.echo.max_text_length", int_in(min_value=1, max_value=500), 30,
        note="超过这个长度不跟：长文本复读是刷屏，不是接龙",
        ui=number("最长", unit="字"),
    ),
    _overridable(
        "echo_window_sec", "extensions.echo.window_sec", float_in(min_value=1.0, max_value=3600.0), 120.0,
        note="复读要在这个时间窗内连续发生才算数",
        ui=number("时间窗", unit="秒"),
    ),
    _overridable(
        "echo_cooldown_sec", "extensions.echo.cooldown_sec", float_in(min_value=0.0, max_value=86400.0), 90.0,
        note="同一个群两次跟读之间至少隔这么久",
        ui=number("跟完歇", unit="秒"),
    ),
    # 表情包图库
    _global_only(
        "sticker_auto_tag", "extensions.sticker.auto_tag", BOOL, True,
        note="收进来的图自动打标签。关掉就不再花看图渠道的钱，但没标签的图检索不到",
        ui=toggle(
            "自动打标签", "收进来的图自动送去看图渠道打标签。关掉就不再产生这笔开销，但没标签的图检索不到，也就发不出去。",
        ),
    ),
    _global_only(
        "sticker_auto_accept", "extensions.sticker.auto_accept", BOOL, False,
        note="收到就直接入库，跳过人工筛选",
        ui=toggle("跳过人工筛选", "收到就直接入库，不进待审池。"),
    ),
    _global_only(
        "sticker_pending_limit", "extensions.sticker.pending_limit", int_in(min_value=0, max_value=5000), 200,
        note="待审池上限，满了不再收新的",
        ui=number("待审池", step=10, unit="张"),
    ),
    _global_only(
        "sticker_library_limit", "extensions.sticker.library_limit", int_in(min_value=0, max_value=20000), 1000,
        note="图库上限，超出时最旧的先出局",
        ui=number("图库", step=50, unit="张"),
    ),
    _global_only(
        "sticker_pending_ttl_days", "extensions.sticker.pending_ttl_days", int_in(min_value=0, max_value=365), 7,
        note="待审池里放这么多天还没人管就丢掉",
        ui=number("待审保留", unit="天"),
    ),
    # 表情包挑选
    _overridable(
        "sticker_send_probability", "extensions.sticker.send_probability",
        float_in(min_value=0.0, max_value=1.0), 0.2,
        note="每轮多大概率把表情包清单给模型看。0 表示从不发；给了它也可以不发",
        ui=number("每轮出现概率", max_value=1, step=0.05),
    ),
    _overridable(
        "sticker_prompt_limit", "extensions.sticker.prompt_limit", int_in(min_value=0, max_value=200), 60,
        note="清单最多列几张。库比这个大就只列发得最少的那些，轮着上",
        ui=number("最多列出", unit="张"),
    ),
    _overridable(
        "sticker_repeat_window_sec", "extensions.sticker.repeat_window_sec",
        int_in(min_value=0, max_value=86400), 1800,
        note="同一个会话里这段时间内发过的图不再发",
        ui=number("同图间隔", step=60, unit="秒"),
    ),
    _overridable(
        "sticker_combat", "extensions.sticker.combat", BOOL, False,
        note="群里连着刷图时跟着接一张；阈值和冷却用内置默认值",
        ui=toggle("接梗", "群里连着刷图时跟着接一张。要求连图来自至少两个人，中间夹了纯文本就不算。"),
    ),
    _overridable(
        "sticker_burst_probability", "extensions.sticker.burst_probability",
        float_in(min_value=0.0, max_value=1.0), 0.0,
        note="自己发完图后再补一张的概率，0 表示不补",
        ui=number("发完再补一张的概率", max_value=1, step=0.05),
    ),
    # 输入
    _overridable(
        "enable_image_input", "input.image", BOOL, True,
        ui=toggle("看图片", "把图片交给视觉节点识别。"),
    ),
    _overridable(
        "max_images_per_turn", "input.max_images_per_turn", int_in(min_value=1, max_value=16), 4,
        ui=number("每轮最多", unit="张"),
    ),
    _overridable(
        "image_max_bytes", "input.image_max_bytes", int_in(min_value=1024), 10 * _MB,
        ui=number("单图上限", scale=_MB, step=1, unit="MB"),
    ),
    _overridable(
        "image_wait_after_text_sec", "input.image_wait_after_text_sec", float_in(min_value=0.0, max_value=10.0), 2.5,
        ui=number("等图窗口", step=0.5, unit="秒"),
    ),
    _overridable(
        "enable_file_input", "input.file", BOOL, True,
        ui=toggle("读文件", "读取对方发来的文件内容。"),
    ),
    _overridable(
        "max_files_per_turn", "input.max_files_per_turn", int_in(min_value=1, max_value=16), 3,
        ui=number("每轮最多", unit="个"),
    ),
    _overridable(
        "file_max_bytes", "input.file_max_bytes", int_in(min_value=1024), 50 * _MB,
        ui=number("单文件上限", scale=_MB, step=1, unit="MB"),
    ),
    _overridable(
        "file_extra_allowed_extensions", "input.file_extra_allowed_extensions", WORD_LIST, "",
        note="在内置白名单之外额外放行的入站文件扩展名；可执行/脚本宿主类型不受此项影响",
        ui=word_list("额外放行", empty="只收内置白名单里的类型", placeholder="输入一个扩展名，回车添加"),
    ),
    # 队列
    _overridable(
        "enable_queue", "queue.enabled", BOOL, False, reload=RELOAD_RECONCILED,
        note="开关翻转要增删 worker task",
        ui=toggle("启用队列", "按顺序一条条处理，避免同时开好几个对话。"),
    ),
    _overridable(
        "queue_workers", "queue.workers", int_in(min_value=1, max_value=32), 4, reload=RELOAD_RECONCILED,
        note="改数量要增删 worker task",
        ui=number("worker", unit="个"),
    ),
    _overridable(
        "queue_interval", "queue.interval_sec", float_in(min_value=0.0), 2.0,
        ui=number("间隔", step=0.5, unit="秒"),
    ),
    _overridable(
        "queue_wait_for_reply", "queue.wait_for_reply", BOOL, False,
        ui=toggle("等回复", "等上一条真的回完再取下一条，而不是发出去就算完。"),
    ),
    _overridable(
        "queue_reply_timeout", "queue.reply_timeout_sec", float_in(min_value=1.0), 120.0,
        ui=number("超时", step=10, unit="秒"),
    ),
    # 群历史
    _overridable(
        "group_history_max", "history.group_max", int_in(min_value=0), 20, reload=RELOAD_RECONCILED,
        note="改条数要重建每个群的 deque",
        ui=number("每群", unit="条"),
    ),
    _overridable(
        "group_history_undelivered_ratio", "history.undelivered_ratio",
        float_in(min_value=1.0, max_value=10.0), 1.0, reload=RELOAD_RECONCILED,
        note="改倍数会改变 deque 硬上限，同样要重建",
        ui=number("上限倍数", step=0.5, unit="倍"),
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
    """平台键表里的全平台设置：路径、解析、默认值、层和控件都跟着全局声明走，平台只补自己早就有的 env 兜底。"""
    return dataclasses.replace(FEATURE_KEYS_BY_NAME[name], env=env)


def schema() -> dict[str, Any]:
    """全局页的控制台 schema。"""
    return build_schema(FEATURE_SECTIONS, FEATURE_KEYS)
