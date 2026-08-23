"""Clonoth QQ 接入插件（NoneBot2 + OneBot 11）。

纯对话模式：群成员 @Bot 或用户私聊后，插件把请求提交到 Supervisor，
拿到最终结果再发回对应 QQ 会话。中间回复会展示，工具调用、进度日志、
审批请求和子任务状态不展示。
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import contextvars
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import random
import re
import secrets
import sys
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, DefaultDict, Deque, Dict, Iterable, List, Mapping, Optional

import httpx
from nonebot import get_bot, get_driver, on_message, on_notice
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent, GroupUploadNoticeEvent, Message, MessageSegment, PrivateMessageEvent
from nonebot.adapters.onebot.v11.exception import ActionFailed
from nonebot.rule import Rule

# 按人记忆的建档名册由适配层写、engine worker 读，两侧共用这一份判定。
from engine import memory_subjects
from engine.attachments import sniff_image_mime

from .faces import face_display_name
from engine.memory_hit_cache import prune_namespace_hits

from .config import (
    BQBS_PATH,
    CLONOTH_BASE_URL,
    CLONOTH_WORKSPACE,
    CONVERSATION_HASH_SECRET,
    CONVERSATION_HASH_SECRET_FILE,
    CUSTOM_FACE_METADATA_PATH,
    CUSTOM_FACE_NAMES_PATH,
    FORWARD_BRIDGE_HOST,
    FORWARD_BRIDGE_PORT,
    FORWARD_BRIDGE_TOKEN,
    FORWARD_BRIDGE_TOKEN_FILE,
    ENTRY_NODE_ID,
    GROUP_TRIGGER_MODES,
    IMAGE_CACHE_TTL_SECONDS,
    IMAGE_DOWNLOAD_TIMEOUT,
    LOCAL_SOURCE_ROOTS,
    IMAGE_FORWARD_MERGE_NICKNAME,
    RECENT_FILE_MAX_AGE_SECONDS,
    RECENT_FILE_MAX_ITEMS,
    RECENT_IMAGE_MAX_AGE_SECONDS,
    RECENT_IMAGE_MAX_ITEMS,
    USER_PROFILES_PATH,
    ONEBOT_STATE_FILE,
    ONEBOT_IDEMPOTENCY_STORE_FILE,
    ONEBOT_IDEMPOTENCY_SENT_TTL_SECONDS,
    ONEBOT_IDEMPOTENCY_FALLBACK_SENT_TTL_SECONDS,
    ONEBOT_IDEMPOTENCY_MAX_ITEMS,
    ONEBOT_IDEMPOTENCY_AMBIGUOUS_TTL_SECONDS,
    REPLY_ATTACHMENT_CACHE_FILE,
    ANON_MAP_FILE,
    ANON_MAP_RETENTION_DAYS,
    PENDING_APPROVAL_TTL_SECONDS,
)
from . import capability
from .echo_policy import EchoEntry, detect_echo, is_echoable_text
from . import bot_scope as _bot_scope
from .conversation_hash import digest as _digest_conversation_key, resolve_secret
from .yaml_loader import load_yaml
from .trigger_policy import (
    CooldownState,
    TriggerConfig,
    TriggerDecision,
    TriggerInput,
    evaluate as trigger_evaluate,
    matched_prefix,
    resolve_llm_intent,
)
from .live_config import (
    RECONCILED_KEYS,
    live,
    pinned as pinned_live_config,
    refresh as refresh_live_config,
    state_payload as live_config_state_payload,
)
from .attachment_policy import (
    inbound_file_reject_reason,
    looks_like_file_query,
    looks_like_image_query,
    select_recent_attachment_entries,
    should_fallback_to_recent_attachments,
    source_attachments_from_merged,
)
from .forward_authz import (
    delivery_deny_reason as forward_delivery_deny_reason,
    file_deny_reason as forward_file_deny_reason,
    file_paths_deny_reason as forward_file_paths_deny_reason,
)
from .memory_hints import (
    at_segment_user_ids,
    collect_subjects as collect_memory_subjects,
)
from .send_contract import (
    AmbiguousClaim,
    IdempotencyClaim,
    IdempotencyOwnershipError,
    OneBotAmbiguousAckError,
    OneBotAttachmentBatchError,
    OneBotSendContractError,
    OutboundSendContext,
    TwoPhaseIdempotencyStore,
    classify_send_exception,
    context_from_sources,
    image_content_identity,
    is_at_uid_failure,
    is_temp_file_missing,
    make_idempotency_key,
    protected_claim_send,
    target_from_idempotency_key,
    target_identity,
    validate_send_request,
)
from .emoji_handler import (
    count_duplicate_face_names,
    duplicated_detail_names,
    extract_named_custom_face_metadata,
    extract_named_custom_face_names,
    fetch_custom_face_details,
    find_custom_faces_by_base_name,
    format_custom_face_detail_line,
    invalidate_custom_face_cache,
    list_custom_face_details,
    load_bqbs,
    load_custom_face_metadata,
    load_custom_face_names,
    process_emojis,
    resolve_custom_face,
    set_at_alias_resolver,
    set_sticker_resolver,
    strip_output_markers,
    write_custom_face_metadata,
    write_custom_face_names,
)
from stickers.collect import CollectConfig, StickerCollector, any_marked_segment
from stickers.combat import CombatConfig, CombatTracker
from stickers.collect import store_path as sticker_store_path
from stickers.rank import rank
from stickers.store import STATE_LIBRARY, STATE_PENDING, StickerStore
from stickers.tagger import StickerTagger, TaggerConfig

# clonoth_sdk 随工作区分发而非 pip 安装，插件加载时先把工作区加进 sys.path。
if CLONOTH_WORKSPACE not in sys.path:
    sys.path.insert(0, CLONOTH_WORKSPACE)

from clonoth_sdk import (  # noqa: E402  # sys.path 必须先插入工作区。
    BotConfig,
    ChildTaskState,
    ClonothClient,
    EventRouter,
    IntentVerdict,
    MainTaskState,
    SessionState,
    TriggerInfo,
)

logger = logging.getLogger("nonebot.plugin.clonoth_agent")
driver = get_driver()

_CST = dt.timezone(dt.timedelta(hours=8))
_SPLIT_SIGNAL = "[SPLIT]"
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_QQ_EMOJI_MARK_RE = re.compile(r"\[QQ_EMOJI:(.+?)\]")
_CQ_RE = re.compile(r"\[CQ:([^,\]]+)(?:,([^\]]*))?\]")
_AT_QQ_RE = re.compile(r"\[(?:CQ:)?at[,:][^\]]*?qq=([^,\]\s]+)", re.IGNORECASE)
# 命令一律 / 开头。裸词会把「画图软件推荐哪个」「可用表情」这类日常句子当命令截胡，
# 而 / 不在可剥离前缀里，群聊私聊拿到的都是同一个字面量。全角 ／ 一并收：
# 中文输入法下打出来的是另一个码位，用户看不出区别。
_CMD_PREFIX = r"[/／]"
# 尾参形态。命令只在这里分叉，别名表各自维护。
_CMD_NO_ARG = r"\s*$"
_CMD_OPT_COUNT = r"(?:\s+(\d+))?\s*$"
_CMD_ONE_ARG = r"\s+(.+?)\s*$"
_CMD_TWO_ARGS = r"\s+(\S+)\s+(.+?)\s*$"
# 吞掉剩余全部内容。中文别名后面常常不带空格（/生图画只猫），所以不能要求词边界，
# 代价是 /drawhelp 会被 draw 分支吃掉 —— 靠分派链里 help 先判来保证，顺序不能动。
_CMD_REST = r"\s*(.*)$"


def _cmd_re(aliases: tuple[str, ...], tail: str = _CMD_NO_ARG) -> "re.Pattern[str]":
    """把别名表编成「/别名 + 尾参」的命令正则。"""
    body = "|".join(re.escape(alias) for alias in aliases)
    return re.compile(rf"^{_CMD_PREFIX}(?:{body}){tail}", re.IGNORECASE)


def _strip_command_prefix(text: str) -> str:
    """剥掉引导斜杠交给集合式命令解析；不是命令返回空串。

    首段里再出现斜杠的一律不认：群里贴 /var/log/x 这类路径很常见，
    而开了前缀触发信号之后它们照样会走到命令链上来。
    """
    raw = str(text or "").strip()
    if not raw or raw[0] not in "/／":
        return ""
    body = raw[1:].lstrip()
    head = body.split(None, 1)[0] if body else ""
    if not head or "/" in head or "／" in head:
        return ""
    return body


_CUSTOM_FACE_LIST_RE = _cmd_re(("表情列表", "收藏表情列表", "emoji列表", "可用表情"), _CMD_OPT_COUNT)
_CUSTOM_FACE_DETAIL_LIST_RE = _cmd_re(("表情详情列表", "收藏表情详情", "表情管理列表"), _CMD_OPT_COUNT)
_CUSTOM_FACE_SYNC_RE = _cmd_re(("同步表情列表", "刷新表情列表", "更新表情列表"))
_STICKER_RE = _cmd_re(("表情包", "表情包库", "图库"), _CMD_REST)
_CUSTOM_FACE_HELP_RE = _cmd_re(("表情包帮助", "表情帮助", "表情包命令", "表情命令帮助"))
_CUSTOM_FACE_ADD_RE = _cmd_re(("收藏表情", "添加表情", "保存表情", "表情收藏"), _CMD_ONE_ARG)
_CUSTOM_FACE_RENAME_RE = _cmd_re(("命名表情", "重命名表情", "改名表情"), _CMD_TWO_ARGS)
_CUSTOM_FACE_DELETE_RE = _cmd_re(("删除表情", "移除表情", "取消收藏表情"), _CMD_ONE_ARG)
_DRAW_DIRECT_RE = _cmd_re(
    ("生图", "画图", "绘图", "nai生图", "novelai生图", "draw", "nai", "novelai"), _CMD_REST,
)
_DRAW_HELP_RE = _cmd_re(("生图帮助", "画图帮助", "绘图帮助", "画师串帮助", "drawhelp", "naihelp"))
_DRAW_PRESET_LIST_RE = _cmd_re(
    ("画师串列表", "绘图预设列表", "生图预设列表", "画风列表", "drawpresets", "presets"),
)
_DRAW_PRESET_SWITCH_RE = _cmd_re(
    ("切换画师串", "切换绘图预设", "切换生图预设", "切换画风", "setdrawpreset", "preset"), _CMD_ONE_ARG,
)
_MODEL_SWITCH_RE = _cmd_re(
    ("切换模型", "切换主模型", "切模型", "设置模型", "setmodel", "switchmodel"), _CMD_ONE_ARG,
)
# 去掉了「模型帮助」：它和「当前模型」是同一个分支，只回一行模型名，叫帮助属于误导。
_MODEL_SHOW_RE = _cmd_re(("当前模型", "查看模型", "model", "showmodel"))
DRAW_NODE_ID = "draw.novelai_planner"
# 2026-07-09: 旧的"5~12 位数字一律当作 QQ 号"兜底匿名正则已废弃（会误伤金额/验证码/
# 日期等普通数字）。现改为只对采集入口登记的已知真实 ID 做精确匿名，不再保留该正则。
# 2026-05-03 修改原因：QQ 端需要把 Clonoth 任务生命周期映射为离散 React 阶段。
# 做法是集中维护阶段到 emoji_id 的映射和阶段顺序，目的在于后续回调只声明
# 目标阶段，避免 stream_delta 高频到达时重复调用 OneBot React API。
# 模型可用的表态 emoji。
_REACT_MODEL_EMOJI_IDS = (
    "4", "5", "9", "14", "63", "66", "76", "79", "99", "182", "201", "264", "271", "319",
)
# 名字只在 faces 表里写一份：这里和收到表情时显示的必须是同一个词。
_REACT_MODEL_EMOJIS: Dict[str, str] = {
    eid: face_display_name(eid) for eid in _REACT_MODEL_EMOJI_IDS
}
assert all(_REACT_MODEL_EMOJIS.values())
_SEARCH_PROGRESS_FIRST_NOTICE = "已收到联网搜索请求，正在检索网页资料，可能需要几秒钟……"
_SEARCH_PROGRESS_STILL_RUNNING_NOTICE = "还在联网搜索中（已等 {waited} 秒），我会拿到结果后马上整理回复。"
_SEARCH_PROGRESS_STILL_RUNNING_DELAY_SEC = 20.0
_SEARCH_PROGRESS_KEYWORDS = ("web_search", "exa_search", "x_search")


def _group_history_capacity() -> int:
    """群历史 deque 的硬上限：软上限之上的位置只租给还没送到 engine 的行。"""
    return int(max(live.group_history_max, 0) * live.group_history_undelivered_ratio)


# 群聊上下文只保留最近 N 条。这样可以给入口节点提供社交语境，
# 同时避免每次 inbound 发送过长历史。
_group_history: DefaultDict[int, Deque["GroupHistoryLine"]] = defaultdict(lambda: deque(maxlen=_group_history_capacity()))
# 每行的序号来源。engine 侧确认收下某轮 inbound 后，水位推进到那轮带过的最大序号，
# 之后只补新行 —— 否则每条群消息都会被 20 轮 inbound 各带一次，在 durable history 里存 20 份。
_group_history_seq: DefaultDict[int, int] = defaultdict(int)
# 被缓存上限吃掉、且当时还没送到 engine 的最高序号。seq 逐 1 递增，缺了几行由它和水位算出来。
_group_history_gap: Dict[int, int] = {}
# 群号 → 清空上下文那一刻的 inbound 序号。清空时还在飞的任务照样会回来投递，
# 它带的是上一段上下文的产物，落回刚清空的缓存就等于没清。
_context_clear_barrier: Dict[int, int] = {}
# 至今见过的最大 inbound 序号，用来给上面那道门槛定位。
_last_inbound_seq: int = 0

# EventRouter 回调只拿到 session/trigger，因此这里保存发送最终回复所需的平台对象。
_session_targets: Dict[str, Dict[str, Any]] = {}
_conversation_bots: Dict[str, Bot] = {}
_real_conversation_keys: Dict[str, str] = {}
_stable_conversation_keys: Dict[str, str] = {}
_persisted_session_targets: Dict[str, Dict[str, Any]] = {}
_last_bot: Optional[Bot] = None


@dataclass
class RecentAttachmentEntry:
    attachment: Dict[str, Any]
    created_at: float
    sender_id: str
    message_id: str


@dataclass
class ProactiveTarget:
    target_type: str
    target_id: int
    label: str


@dataclass(frozen=True)
class GroupHistoryLine:
    """群历史缓存里的一行，seq 用于和 engine 侧高水位比对。"""

    seq: int
    text: str


@dataclass
class GroupContentRecord:
    """保留最近群消息的结构化副本，供管理员自然语言转发筛选。"""

    formatted_line: str
    text: str
    sender_name: str
    sender_id: str
    timestamp: float
    message_id: str = ""
    seq: int = 0  # 与群历史行同号：deque 淘汰后位置下标会整体左移，挑选只能按这个号
    attachments: List[Dict[str, Any]] | None = None


@dataclass(frozen=True)
class ForwardSelection:
    """qq_forward 按 ref 挑选群消息的结果：命中记录、未命中 ref、错误文案。"""

    records: List[GroupContentRecord]
    missing_refs: tuple[str, ...] = ()
    error: str = ""


_CONVERSATION_BUCKET_MAX_KEYS = 512


class _BucketMap(OrderedDict):
    """按会话分桶的进程内缓存：写入即刷新为最新，桶数超上限时淘汰最久未写入的会话。"""

    def __init__(self, factory: Callable[[], Any], max_keys: int) -> None:
        super().__init__()
        self._factory = factory
        self._max_keys = max_keys

    def __missing__(self, key: Any) -> Any:
        value = self._factory()
        self[key] = value
        return value

    def touch(self, key: Any) -> None:
        # 桶可能因写入前提前 return 而没被创建，move_to_end 对不存在的键会抛 KeyError。
        if key not in self:
            return
        self.move_to_end(key)
        while len(self) > self._max_keys:
            self.popitem(last=False)


_recent_images: "_BucketMap" = _BucketMap(lambda: deque(maxlen=RECENT_IMAGE_MAX_ITEMS), _CONVERSATION_BUCKET_MAX_KEYS)
# QQ 群文件是独立一条消息、带不了 @，那条永远不会触发 bot。不在这里留一手，
# 下一条「读一下上面那个文件」就再也拿不到它了。
_recent_files: "_BucketMap" = _BucketMap(lambda: deque(maxlen=RECENT_FILE_MAX_ITEMS), _CONVERSATION_BUCKET_MAX_KEYS)
# 复读判定只看内容指纹和发言人，不用整条历史：判完就丢，不参与任何送达账本。
_echo_recent: DefaultDict[int, Deque[EchoEntry]] = defaultdict(lambda: deque(maxlen=12))
# 每个群最近跟过的那句，防止人再补一条又凑够数、Bot 一路接力。
_echo_last: Dict[int, tuple[str, float]] = {}
# 借 group_history_max 定容量，但语义不同：这份副本只供 qq_forward 挑选转发候选，没有送达账本，
# 因此不参与缺口记账、也不吃未送达倍数。调小群历史条数会顺带缩小可转发范围，至少 20 条。
_group_content_records: DefaultDict[int, Deque[GroupContentRecord]] = defaultdict(lambda: deque(maxlen=max(live.group_history_max, 20)))
# [2026-07-07] 按会话记录 Bot 最近发出/生成的附件（如生图插件产出的图片），
# 供 qq_forward “把刚才生成的那张图发给 xx”等自然语言请求检索。
# 只保存工作区内本地路径与显示名，不涉及真实 QQ 号。
_recent_sent_attachments: "_BucketMap" = _BucketMap(lambda: deque(maxlen=max(RECENT_IMAGE_MAX_ITEMS, 20)), _CONVERSATION_BUCKET_MAX_KEYS)
# 每个桶单调递增的附件序号：下标会随新附件入队和缺文件过滤而漂移，挑选只能按这个号。
_sent_attachment_seq: DefaultDict[str, int] = defaultdict(int)
_last_attachment_cleanup_at = 0.0
# 清理频率不是运营期要调的旋钮（保留期才是），所以是代码常量。
_ATTACHMENT_CLEANUP_INTERVAL_SEC = 3600.0
# data/attachments 同时住着 engine 产物和其他适配器的附件，只清 QQ 自己的会话目录。
# 前缀 = _stable_conversation_key 的三种 prefix 把 ":" 换成 "_"。
_QQ_ATTACHMENT_DIR_PREFIXES = ("qq_group_", "qq_private_", "qq_unknown_")

# [2026-07-17] Bot 已发出附件消息的 message_id -> 本地附件路径列表 映射。
# Why: Bot 发出的图片（如绘图产物）在上下文里被替换成占位符/表情包，用户引用
# 这条消息要求“基于这张图继续改”时，AI 看不到原图。How: 发图时接住 send_*_msg
# 返回的 message_id，登记它对应的本地附件路径；用户引用命中时把该图当附件送给 AI。
# Purpose: 让“引用 Bot 生成的图 + 追加要求”能真正带上原图，而普通表情包（无此
# 映射）仍保持屏蔽，不会重新触发识图。
_MESSAGE_ATTACHMENT_MAX_ITEMS = int(os.environ.get("ONEBOT_MSG_ATTACHMENT_CACHE_ITEMS", "512") or "512")
_sent_message_attachments: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()


@dataclass
class QueuedInbound:
    matcher: Any
    bot: Bot
    event: Event
    channel: str
    real_conversation_key: str
    stable_conversation_key: str
    text: str
    attachments: List[Dict[str, Any]]
    is_dm: bool
    platform_updates: Dict[str, Any]
    user_text: str
    entry_node_id: str = ""
    # 本条 inbound 文本带出的最大群历史序号；-1 = 没带历史（私聊 / 直达绘图）。
    history_watermark: int = -1
    # 发言人是不是主动找 bot（@ / 前缀 / 私聊）。只有主动的才给他建记忆档案。
    direct_interaction: bool = True


_qq_queue: Deque[QueuedInbound] = deque()
_qq_queue_by_key: Dict[str, QueuedInbound] = {}
_qq_queue_condition = asyncio.Condition()
_qq_waiting_replies: Dict[str, asyncio.Event] = {}
_auto_like_today: Dict[int, str] = {}
_reply_message_cache: Dict[str, Dict[str, Any]] = {}
_reply_message_cache_order: Deque[str] = deque()
# 持久化的“message_id -> 本地附件”索引。用于 NapCat get_msg 取不到引用消息时，
# 仍能转发此前已经下载到 Clonoth 的图片/表情包。只保存路径和少量路由元数据。
_reply_attachment_cache: Dict[str, Dict[str, Any]] = {}
# Outbound replay protection uses pending/sent phases: failed sends release pending,
# while only acknowledged sends become sent.
_outbound_idempotency = TwoPhaseIdempotencyStore(
    ONEBOT_IDEMPOTENCY_STORE_FILE,
    sent_ttl=ONEBOT_IDEMPOTENCY_SENT_TTL_SECONDS,
    max_items=ONEBOT_IDEMPOTENCY_MAX_ITEMS,
    ambiguous_ttl=ONEBOT_IDEMPOTENCY_AMBIGUOUS_TTL_SECONDS,
)
_route_state_lock = asyncio.Lock()
# 映射表的 key 必须是纯数字：_anonymize_text_for_ai 的 (?<!\d)/(?!\d) 边界只对数字成立。
_anon_users: Dict[str, str] = {}
_anon_groups: Dict[str, str] = {}
_anon_user_reverse: Dict[str, str] = {}
_anon_group_reverse: Dict[str, str] = {}
# 每条别名的真实最后出现时间，供 TTL 回收判定；只在进程内维护，落盘时写进 last_seen 字段。
_anon_user_last_seen: Dict[str, float] = {}
_anon_group_last_seen: Dict[str, float] = {}
# 每次命中都写盘会把节流窗口打满，同一小时内只记一次就够 TTL 判定用。
_ANON_MAP_TOUCH_MIN_INTERVAL = 3600.0
# 条目数超过此值时提示可以开保留期回收。
_ANON_MAP_LARGE_WARN = 20000
# 显式维护“下一个编号”计数器。不再用 len(_anon_users) 推断，避免持久化恢复/
# TTL 回收后长度与实际已用编号不一致导致别名冲突。加载时以文件为准恢复。
_anon_user_next: int = 0
_anon_group_next: int = 0
_anon_map_dirty: bool = False
# 匿名映射写盘节流：新登记不再每次都立即写盘，而是合并到至少间隔 5s 后写一次。
# _anon_map_save_task 保存待触发的延迟 flush task；_anon_map_last_saved_at 记录上次落盘时间。
_ANON_MAP_SAVE_MIN_INTERVAL = max(0.0, float(os.environ.get("ONEBOT_ANON_MAP_SAVE_MIN_INTERVAL", "5.0")))
_anon_map_save_task: Optional["asyncio.Task[Any]"] = None
_anon_map_last_saved_at: float = 0.0

# 待管理员审批的 Clonoth 操作。key 为 approval_id，value 保存操作、详情和来源会话。
_pending_approvals: Dict[str, Dict[str, Any]] = {}

# 卡片标题同时是“这条消息确实是审批卡片”的唯一证据，改文案要连反查一起改。
_APPROVAL_SUMMARY_HEADER = "【Clonoth 审批请求】"
_APPROVAL_CARD_ID_RE = re.compile(r"ID:\s*([0-9a-fA-F][0-9a-fA-F-]{7,})")
_APPROVAL_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# 引用了审批卡片但反查不出可决策目标时的回执：区别于“这就是一句普通聊天”。
_APPROVAL_REPLY_UNRESOLVED_HINT = (
    "这条审批已不在待处理列表（可能已被处理或已超时）。"
    "要处理其它审批请手动发送：审批 同意 <ID>。"
)


@dataclass(frozen=True)
class _ApprovalCard:
    """一张审批卡片的锚点：卡片对应的 approval_id 与收件管理员。"""

    approval_id: str
    admin_id: int


@dataclass(frozen=True)
class _ApprovalReplyTarget:
    """引用快捷审批的反查结果。from_card 为真表示确认引用的是 bot 发出的审批卡片。"""

    approval_id: str = ""
    settled_id: str = ""
    from_card: bool = False

    def matched(self) -> bool:
        return bool(self.approval_id or self.settled_id or self.from_card)


# 发给管理员的审批消息 message_id -> 卡片锚点映射，支持“引用卡片回同意/拒绝”快捷审批。
# 记录收件人是为了反查时校验“引用者就是这张卡片的收件人”。有界，超限清理最旧条目。
_approval_message_ids: "OrderedDict[str, _ApprovalCard]" = OrderedDict()
_APPROVAL_MSG_MAP_MAX = 500

# 已决策/已失效的审批登记：重复决策或引用已处理卡片时回一句“已被处理”，而非误批另一条。
_settled_approvals: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_SETTLED_APPROVAL_MAX = 200

# 决策领取锁：多个管理员同时决策同一条时，只有抢到的人能提交。
_approval_decision_lock = asyncio.Lock()


def _normalize_msg_id(message_id: Any) -> str:
    """将消息 id 归一化为稳定字符串 key。

    Why: NapCat/OneBot 上报的 reply id 与 send_private_msg 返回的
    message_id 可能分别以 int / 带小数点浮点字符串 / 带空白的形式出现，直接
    str() 对比会因“-123 vs -123.0”等差异而反查失败。How: 先 strip，再尝试按
    整数归一化（容忍 '-123.0' 这类）。Purpose: 写入与反查使用同一归一化规则，
    避免引用快捷审批因 id 格式差异而失败。
    """
    if message_id is None:
        return ""
    key = str(message_id).strip()
    if not key:
        return ""
    try:
        return str(int(float(key))) if any(c in key for c in ".") else str(int(key))
    except (ValueError, TypeError):
        return key


def _remember_approval_message(message_id: Any, approval_id: str, admin_id: int) -> None:
    """记录审批消息 id -> 卡片锚点（approval_id + 收件管理员），供引用回复审批时反查校验。"""
    if not approval_id:
        return
    key = _normalize_msg_id(message_id)
    if not key:
        return
    try:
        admin = int(admin_id)
    except (TypeError, ValueError):
        return
    _approval_message_ids[key] = _ApprovalCard(approval_id=approval_id, admin_id=admin)
    _approval_message_ids.move_to_end(key)
    while len(_approval_message_ids) > _APPROVAL_MSG_MAP_MAX:
        _approval_message_ids.popitem(last=False)


def _approval_card_recipients(approval_id: str) -> set[int]:
    """这条审批的卡片实际发给了哪些管理员，用于唯一回退时校验收件人。"""
    if not approval_id:
        return set()
    return {
        card.admin_id
        for card in _approval_message_ids.values()
        if card.approval_id == approval_id
    }


def _record_settled_approval(
    approval_id: str, *, admin_id: int, decision: str, operation: str = "",
) -> None:
    """登记一条已决策/已失效的审批，供重复决策与引用已处理卡片时告知而非误批。"""
    if not approval_id:
        return
    _settled_approvals[approval_id] = {
        "decision": decision,
        "admin_id": admin_id,
        "at": time.time(),
        "operation": operation,
    }
    _settled_approvals.move_to_end(approval_id)
    while len(_settled_approvals) > _SETTLED_APPROVAL_MAX:
        _settled_approvals.popitem(last=False)


def _settled_approval_notice(approval_id: str) -> str:
    """引用/决策一条已处理审批时的回执文案。"""
    info = _settled_approvals.get(approval_id) or {}
    decision = str(info.get("decision") or "")
    label = {"allow": "已同意", "deny": "已拒绝", "expired": "已超时失效"}.get(decision, "已处理")
    admin_id = info.get("admin_id")
    by = f"，由管理员 {admin_id}" if admin_id else ""
    return f"该审批已被处理：{approval_id}（{label}{by}）。"


def _prune_expired_approvals() -> None:
    """把超过 TTL 的待审批移出 pending 并登记为已失效；engine 侧已 auto-deny，这里同步收尾。"""
    if not _pending_approvals:
        return
    now = time.time()
    expired: List[str] = []
    for aid, info in _pending_approvals.items():
        created = float(info.get("created_at") or 0) or now
        if now - created > PENDING_APPROVAL_TTL_SECONDS:
            expired.append(aid)
    for aid in expired:
        info = _pending_approvals.pop(aid, {})
        _record_settled_approval(
            aid, admin_id=0, decision="expired", operation=str(info.get("operation") or ""),
        )


def _resolve_approval_target_by_reply(reply_message_id: Any, user_id: int) -> _ApprovalReplyTarget:
    """按 message_id 反查审批卡片：命中且收件人是本人时确认 from_card，再落 pending / settled。"""
    key = _normalize_msg_id(reply_message_id)
    if not key:
        return _ApprovalReplyTarget()
    card = _approval_message_ids.get(key)
    if card is None:
        return _ApprovalReplyTarget()
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return _ApprovalReplyTarget()
    if card.admin_id != uid:
        return _ApprovalReplyTarget()
    if card.approval_id in _pending_approvals:
        return _ApprovalReplyTarget(approval_id=card.approval_id, from_card=True)
    if card.approval_id in _settled_approvals:
        return _ApprovalReplyTarget(settled_id=card.approval_id, from_card=True)
    return _ApprovalReplyTarget(from_card=True)


def _extract_approval_id_from_text(text: str) -> str:
    """从被引用的审批卡片文本中提取 approval_id 候选；脱钩 pending，取不到返回空串。"""
    if not text:
        return ""
    m = _APPROVAL_CARD_ID_RE.search(text)
    if m:
        return m.group(1)
    m = _APPROVAL_UUID_RE.search(text)
    if m:
        return m.group(0)
    for aid in list(_pending_approvals) + list(_settled_approvals):
        if aid and aid in text:
            return aid
    return ""


async def _resolve_approval_target_by_quoted_text(bot: Bot, reply_message_id: Any) -> _ApprovalReplyTarget:
    """get_msg 拉被引用消息，确认是 bot 自己发出的审批卡片后再取 approval_id。"""
    if reply_message_id is None:
        return _ApprovalReplyTarget()
    try:
        reply_obj = await _get_reply_message(bot, reply_message_id)
    except Exception:
        reply_obj = None
    if not isinstance(reply_obj, dict):
        return _ApprovalReplyTarget()
    parts: List[str] = []
    for key in ("raw_message", "message"):
        val = reply_obj.get(key)
        if isinstance(val, str):
            parts.append(val)
        elif isinstance(val, list):
            for seg in val:
                data = seg.get("data") if isinstance(seg, dict) else None
                if isinstance(data, dict):
                    txt = data.get("text")
                    if isinstance(txt, str):
                        parts.append(txt)
    text = "\n".join(parts)
    if _APPROVAL_SUMMARY_HEADER not in text:
        return _ApprovalReplyTarget()
    sender = reply_obj.get("sender")
    sender_id = sender.get("user_id") if isinstance(sender, dict) else None
    if sender_id is None or _normalize_msg_id(sender_id) != _normalize_msg_id(getattr(bot, "self_id", None)):
        return _ApprovalReplyTarget()
    aid = _extract_approval_id_from_text(text)
    if aid and aid in _pending_approvals:
        return _ApprovalReplyTarget(approval_id=aid, from_card=True)
    if aid and aid in _settled_approvals:
        return _ApprovalReplyTarget(settled_id=aid, from_card=True)
    return _ApprovalReplyTarget(from_card=True)


async def _resolve_approval_reply_target(bot: Bot, event: Event) -> _ApprovalReplyTarget:
    """引用快捷审批的唯一入口：先 map 反查，再 get_msg 卡片校验，最后按 opt-in 唯一回退。"""
    _prune_expired_approvals()
    has_reply, reply_message_id = _scan_reply_reference(
        event.get_message(), getattr(event, "raw_message", None),
    )
    if not has_reply:
        return _ApprovalReplyTarget()
    user_id = getattr(event, "user_id", None)
    target = _resolve_approval_target_by_reply(reply_message_id, user_id)
    if target.matched():
        return target
    if _can("approval", event):
        target = await _resolve_approval_target_by_quoted_text(bot, reply_message_id)
        if target.matched():
            return target
        if live.approval_reply_unique_fallback and len(_pending_approvals) == 1:
            only_id = next(iter(_pending_approvals))
            uid = _as_qq_id(user_id)
            if uid is not None and uid in _approval_card_recipients(only_id):
                return _ApprovalReplyTarget(approval_id=only_id, from_card=True)
    return _ApprovalReplyTarget()


# QQ 用户身份/称呼 Profile。只影响模型可见的称呼和身份说明，不授予任何权限。
# 权限仍只由管理员名单与能力档位判定，见 _can。
_QQ_USER_PROFILES: Dict[str, Dict[str, Any]] = {}

_client: Optional[ClonothClient] = None
_session_state: Optional[SessionState] = None
_event_router: Optional[EventRouter] = None
_router_task: Optional[asyncio.Task] = None
# 按 worker 下标存而不是列表：下标决定缩容时谁该退，某个 worker 意外退出后
# 补位也要补回它原来那个空位，否则会出现两个同下标的 worker。
_qq_queue_tasks: Dict[int, asyncio.Task] = {}
_live_reconcile_task: Optional[asyncio.Task] = None
_attachment_cleanup_task: Optional[asyncio.Task] = None
# 热载键落到运行期对象上的对齐周期。停等一次 stat 的代价换「改完两秒内生效」。
_LIVE_RECONCILE_INTERVAL_SEC = 2.0
# 快照没变也定期重写一次，supervisor 靠这个时间戳判断 bot 进程是不是还活着。
_LIVE_STATE_HEARTBEAT_SEC = 30.0
# 触发冷却。进程内内存：重启之后继续静默毫无道理，窗口本来就是秒级的。
_trigger_cooldown = CooldownState()
# 随机插话骰子的盐。每次启动换一份：常量盐会让外人按 message_id 算出哪条消息能触发插话。
_TRIGGER_ROLL_SALT = os.urandom(16)
# 按消息身份缓存的触发判定。只为「一条消息在同一次派发里被判两遍」而存在，所以很小。
_TRIGGER_DECISION_CACHE_MAX = 256
_trigger_decisions: "OrderedDict[tuple[str, ...], TriggerDecision]" = OrderedDict()
_live_state_signature: str = ""
_live_state_published_at: float = 0.0
_callbacks: Optional["TangQiuCallbacks"] = None
_bqbs: List[str] = []
_custom_face_names: List[str] = []
_custom_face_metadata: List[Dict[str, Any]] = []


def _is_group_allowed(group_id: int) -> bool:
    """判断 Bot 能否在该群活动；空列表表示不允许任何群。

    入站响应与出站主动发送共用这一个判定：曾经出站侧各自写成
    `if live.allowed_groups and gid not in live.allowed_groups`，空名单时反而全放行，
    与入站的全拒结论相反，而空名单正是未配置时的默认状态。
    """
    try:
        return int(group_id) in live.allowed_groups
    except (TypeError, ValueError):
        return False


def _is_admin_user(user_id: Any) -> bool:
    """是否在 admin_users 名单里。

    这是身份，不是能力 —— 能力要过 `_can`。除了鉴权，这份名单还兼着私聊白名单、
    审批推送收件人、提交给 engine 的 platform_auth.is_admin，那几处要的就是名单本身，
    不受能力档位影响：把工作区文件读权限跟着群主身份放出去是另一回事。
    """
    try:
        return int(user_id) in live.admin_users
    except Exception:
        return False


def _requester(event: Any) -> capability.Requester:
    """把事件里的身份事实原样搬过来，判定规则一律留给 capability。

    群消息按有没有 group_id 认，不按事件类型 —— 私聊事件万一带了 sender.role，
    也进不了判定：没有 group_id 的请求者一律算普通用户（Requester.rank）。
    """
    user_id = getattr(event, "user_id", None)
    # 匿名消息的身份不可核验，一律按普通群员判定。
    anonymous = bool(_anonymous_identity(event))
    return capability.Requester(
        user_id=_as_qq_id(user_id),
        listed_admin=(not anonymous) and _is_admin_user(user_id),
        group_id=_as_qq_id(getattr(event, "group_id", None)),
        group_role="" if anonymous else str(getattr(getattr(event, "sender", None), "role", "") or ""),
    )


def _can(capability_key: str, event: Any) -> bool:
    """这个人有没有这项能力。目标范围另判，见 _target_scope_denied。"""
    return capability.allows(capability_key, _grant_of(capability_key), _requester(event))


def _grant_of(capability_key: str) -> str:
    return getattr(live, f"grant_{capability_key}")


def _capability_denial(capability_key: str, event: Any, fallback: str) -> str | None:
    """没有这项能力时回什么。None = 当作没有这个命令，一个字都不回。

    档位关掉这件事只有名单里的人听得懂，对别人说等于公布这项能力存在。
    """
    if _grant_of(capability_key) != capability.OFF:
        return fallback
    if not _is_admin_user(getattr(event, "user_id", None)):
        logger.info(
            "QQ capability %s is off; staying silent for user=%s",
            capability_key, getattr(event, "user_id", None),
        )
        return None
    cap = capability.BY_KEY.get(capability_key)
    return f"「{cap.name if cap else capability_key}」已在配置里关闭。"


def _as_qq_id(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_private_allowed(event: PrivateMessageEvent) -> bool:
    """判断 QQ 私聊是否允许接入 Clonoth。id 解析不出来一律不放行。"""
    user_id = _as_qq_id(getattr(event, "user_id", None))
    if user_id is None:
        return False
    if user_id in live.admin_users or user_id in live.allowed_private_users:
        return True
    if not live.allow_private_friends:
        return False
    return str(getattr(event, "sub_type", "") or "").lower() == "friend"


def _is_private_origin_allowed(user_id: int) -> bool:
    """来源私聊是否仍在白名单内。供 qq_forward 读接口与「私发给我」写侧共用。

    只认名单，比入站闸门严：这里没有事件对象可查 sub_type=friend，拿总开关顶替
    等于对任何人放行，而这两处是借 Bot 身份主动发出去，宽进不该等于宽出。
    """
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    return uid in live.admin_users or uid in live.allowed_private_users


async def _allowed_group_rule(event: Event) -> bool:
    """把群类型和白名单放在规则层过滤，避免无关消息进入上下文缓存。"""
    if not isinstance(event, GroupMessageEvent):
        return False
    # rule 是这条消息的最早入口，在这里钉住配置快照：整条处理链读到的都是同一份，
    # 运营者中途改白名单不会让处理到一半的消息换标准。
    refresh_live_config()
    return _is_group_allowed(int(event.group_id))


async def _group_trigger_text(bot: Bot, event: GroupMessageEvent) -> str:
    """触发判定看到的正文。不展开的话转发卡片只是一个占位符，关键词和意愿判断都判不中。"""
    if not live.forward_msg_expand_for_trigger:
        return _message_to_text(event.get_message(), getattr(bot, "self_id", None)).strip()
    return (await _event_text_with_forward(bot, event)).strip()


async def _agent_group_rule(bot: Bot, event: Event) -> bool:
    """匹配允许群里的 Agent 触发消息。七个信号的判定见 trigger_policy。"""
    if not isinstance(event, GroupMessageEvent):
        return False
    refresh_live_config()
    if not _is_group_allowed(int(event.group_id)):
        return False
    text = await _group_trigger_text(bot, event)
    decision = _group_trigger_decision_once(event, bot, text)
    if decision.blocked_by:
        logger.debug(
            "QQ trigger suppressed group=%s signal=%s by=%s",
            getattr(event, "group_id", None), decision.signal, decision.blocked_by,
        )
    if decision.triggered:
        # 在这里记冷却而不是等真的发出去：rule 返回 True 就意味着这条消息会被当成一轮
        # bot 发言处理（本地命令拦截也会产出回复）。handler 中途异常时会多冷却一轮，
        # 而「多等一会儿」是这两个方向里安全的那一个。
        _trigger_cooldown.record(_trigger_input(event, bot, text))
    return decision.triggered


async def _intent_group_rule(bot: Bot, event: Event) -> bool:
    """匹配「显式信号都不命中、但开了意愿判断」的群消息。

    只判要不要问模型，不在这里问：rule 里做一次几秒的往返，会把同一条消息后面的
    matcher 连同群历史记录一起卡住整整那么久。
    """
    if not isinstance(event, GroupMessageEvent):
        return False
    refresh_live_config()
    if not live.llm_intent_enabled:
        return False
    if not _is_group_allowed(int(event.group_id)):
        return False
    text = await _group_trigger_text(bot, event)
    return _group_trigger_decision_once(event, bot, text).awaits_llm_intent()


async def _private_message_rule(event: Event) -> bool:
    """只匹配 QQ 私聊消息，避免私聊请求被群聊白名单逻辑误拦截。"""
    # 2026-05-01 修改原因：私聊没有群号，也不需要 @Bot；这里单独识别
    # PrivateMessageEvent，使私聊入口和现有群聊入口互不影响。
    if not isinstance(event, PrivateMessageEvent):
        return False
    refresh_live_config()
    return True


def _approval_summary(approval_id: str, operation: str, details: Dict[str, Any]) -> str:
    """生成发给 QQ 管理员的审批摘要，限制长度并避免刷屏。"""
    lines = [
        _APPROVAL_SUMMARY_HEADER,
        f"ID: {approval_id}",
        f"操作: {operation or 'unknown'}",
    ]
    for key in ("tool_name", "path", "command", "reason", "safety_level"):
        value = details.get(key)
        if value is not None and value != "":
            lines.append(f"{key}: {str(value)[:1000]}")
    args = details.get("args") or details.get("parameters")
    if args:
        lines.append(f"参数: {str(args)[:1500]}")
    lines.extend([
        "",
        "【快捷审批】直接引用(回复)本消息，发送“同意”或“拒绝”即可。",
        f"或手动发送：审批 同意 {approval_id}",
        f"　　　　　审批 拒绝 {approval_id}",
    ])
    return _truncate_qq_text("\n".join(lines))


_APPROVAL_ALLOW_VERBS = frozenset({
    "同意", "批准", "通过", "允许", "allow", "approve", "approved", "ok", "yes", "y",
})
_APPROVAL_DENY_VERBS = frozenset({
    "拒绝", "驳回", "不同意", "不批准", "deny", "reject", "rejected", "no", "n",
})
_APPROVAL_COMMAND_PREFIXES = ("审批", "approval")
_APPROVAL_VERB_STRIP = " 　。，、！!.~"
# 形状与 approval_id（uuid4）绑定：裸动词命令只在 token 像 id 时才当审批，避免劫持普通私聊。
_APPROVAL_TOKEN_RE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{3,}$")


def _approval_verb(token: str) -> Optional[str]:
    """两个入口唯一的动词判定：整词匹配，返回 'allow' / 'deny' / None。"""
    word = (token or "").strip().strip(_APPROVAL_VERB_STRIP).lower()
    if not word:
        return None
    if word in _APPROVAL_DENY_VERBS:
        return "deny"
    if word in _APPROVAL_ALLOW_VERBS:
        return "allow"
    return None


def _parse_approval_command(text: str) -> Optional[tuple[str, str]]:
    """解析管理员私聊审批命令，返回 (decision, approval_id_or_prefix)。

    引用审批卡片回一个词那条路径不经过这里，仍然免斜杠 —— 卡片在，指向就是唯一的。
    """
    normalized = re.sub(r"\s+", " ", _strip_command_prefix(text))
    if not normalized:
        return None
    parts = normalized.split(" ")
    if len(parts) < 2:
        return None
    if parts[0] in _APPROVAL_COMMAND_PREFIXES and len(parts) >= 3:
        decision = _approval_verb(parts[1])
        token = parts[2]
        explicit_prefix = True
    else:
        decision = _approval_verb(parts[0])
        token = parts[1]
        explicit_prefix = False
    if decision is None:
        return None
    # 裸动词命令 token 必须像 approval_id，否则“通过 微信发给我”会劫持普通私聊。
    if not explicit_prefix and not _APPROVAL_TOKEN_RE.match(token):
        return None
    return decision, token


def _parse_approval_reply_verb(text: str) -> Optional[str]:
    """引用回复里的审批意图；整句必须就是一个动词。返回 'allow' / 'deny' / None。"""
    # 子串匹配会让 know/note/now 命中 no，改整词。
    normalized = re.sub(r"\s+", "", (text or "").strip()).lower()
    if not normalized:
        return None
    for prefix in _APPROVAL_COMMAND_PREFIXES:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):].lstrip(":：")
    return _approval_verb(normalized)


def _unique_pending_approval_for(user_id: Any) -> str:
    """恰好一条待审批、且那张卡片确实发给本人时返回它的 id，否则空串。"""
    _prune_expired_approvals()
    if len(_pending_approvals) != 1:
        return ""
    only_id = next(iter(_pending_approvals))
    uid = _as_qq_id(user_id)
    if uid is None or uid not in _approval_card_recipients(only_id):
        return ""
    return only_id


def _pending_approvals_for(user_id: Any) -> int:
    """本人收到过卡片的待审批条数，用于裸动词歧义时给出可操作的提示。"""
    uid = _as_qq_id(user_id)
    if uid is None:
        return 0
    return sum(1 for aid in _pending_approvals if uid in _approval_card_recipients(aid))


def _resolve_pending_approval_id(token: str) -> tuple[Optional[str], str]:
    """允许管理员用完整 approval_id 或唯一前缀审批；命中已处理条目时告知而非误批。"""
    _prune_expired_approvals()
    token = (token or "").strip()
    if not token:
        return None, "缺少审批 ID。"
    if token in _pending_approvals:
        return token, ""
    matches = [aid for aid in _pending_approvals if aid.startswith(token)]
    if len(matches) == 1:
        return matches[0], ""
    if len(matches) > 1:
        return None, f"审批 ID 前缀不唯一：{token}"
    settled = [aid for aid in _settled_approvals if aid == token or aid.startswith(token)]
    if len(settled) == 1:
        return None, _settled_approval_notice(settled[0])
    return None, f"没有找到待审批 ID：{token}"


def _sanitize_name(name: str, max_len: int = 32) -> str:
    """清洗成员名称，避免换行和结构符号破坏输入格式。"""
    name = (name or "").replace("\n", " ").replace("\r", " ")
    name = name.replace("[", "(").replace("]", ")").strip()
    return (name[:max_len] + "…") if len(name) > max_len else (name or "未知成员")


def _load_qq_user_profiles(path_text: str) -> Dict[str, Dict[str, Any]]:
    """从 JSON/YAML 加载 QQ 用户称呼 Profile；仅用于展示和提示，不授予权限。"""
    path_text = str(path_text or "").strip()
    if not path_text:
        return {}
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = (Path(CLONOTH_WORKSPACE) / path).resolve()
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if path.suffix.lower() == ".json" else load_yaml(raw)
    except FileNotFoundError:
        return {}
    except Exception:
        logger.warning("failed to load QQ user profiles: %s", path, exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    users = data.get("users") if isinstance(data.get("users"), dict) else data
    profiles: Dict[str, Dict[str, Any]] = {}
    for raw_uid, raw_profile in users.items():
        uid = str(raw_uid or "").strip()
        if not uid or not isinstance(raw_profile, dict):
            continue
        profile = {
            "display_name": str(raw_profile.get("display_name") or raw_profile.get("name") or "").strip(),
            "address_as": str(raw_profile.get("address_as") or raw_profile.get("call_as") or "").strip(),
            "title": str(raw_profile.get("title") or raw_profile.get("role") or "").strip(),
            "note": str(raw_profile.get("note") or raw_profile.get("style_note") or "").strip(),
        }
        profiles[uid] = {k: v for k, v in profile.items() if v}
    return profiles


def _qq_user_profile(user_id: Any) -> Dict[str, Any]:
    raw = str(user_id or "").strip()
    return dict(_QQ_USER_PROFILES.get(raw) or {})


def _qq_profile_display_name(user_id: Any) -> str:
    name = str(_qq_user_profile(user_id).get("display_name") or "").strip()
    return _sanitize_name(name) if name else ""


def _sender_display_name(sender: Any, fallback_user_id: Any = "") -> str:
    """优先使用 QQ Profile 显示名，其次使用群名片/昵称，最后回退到匿名别名。

    2026-07-09 修改原因：取消了 `_anonymize_text_for_ai` 的泛数字兜底后，本函数不能再
    直接回退到裸 QQ 号，否则无名片/无昵称的发送者真实 QQ 号会泄露给模型。
    无可用名称时回退到稳定匿名别名（并登记映射）。
    """
    profile_name = _qq_profile_display_name(fallback_user_id)
    if profile_name:
        return profile_name
    card = getattr(sender, "card", "") or ""
    nickname = getattr(sender, "nickname", "") or ""
    if card or nickname:
        return _sanitize_name(card or nickname)
    return _anonymize_user_id(fallback_user_id) if str(fallback_user_id or "").strip() else "未知成员"


# QQ 群匿名消息的发送者占位号，全群匿名成员共用；不能当成一个真实用户登记。
_ANONYMOUS_PLACEHOLDER_QQ_IDS = frozenset({"80000000"})
_ANONYMOUS_UNKNOWN_ALIAS = "AnonUnknown"
_ANONYMOUS_DISPLAY_NAME = "匿名成员"
# 匿名 id 由平台按天轮换，落盘只会撑大映射表且反解无意义，因此这份表只存在于进程内。
_ANON_SENDER_MAX_ENTRIES = 4096
_anon_senders: "OrderedDict[str, str]" = OrderedDict()
_anon_sender_next: int = 0


def _anonymous_field(anonymous: Any, key: str) -> str:
    if isinstance(anonymous, dict):
        return str(anonymous.get(key) or "").strip()
    return str(getattr(anonymous, key, "") or "").strip()


def _anonymous_identity(event: Any) -> str:
    """群匿名消息的身份键；非匿名消息返回空串。"""
    anonymous = getattr(event, "anonymous", None)
    if anonymous is None:
        return ""
    ident = (
        _anonymous_field(anonymous, "id")
        or _anonymous_field(anonymous, "flag")
        or _anonymous_field(anonymous, "name")
    )
    group = str(getattr(event, "group_id", "") or "")
    return f"anon:{group}:{ident}" if ident else f"anon:{group}:unknown"


def _anonymize_anonymous_sender(event: Any) -> str:
    """给匿名发送者分配进程内别名；不同匿名成员必须拿到不同别名。"""
    global _anon_sender_next
    key = _anonymous_identity(event)
    if not key:
        return ""
    alias = _anon_senders.get(key)
    if alias is None:
        alias = _alias_from_index("Anon", _anon_sender_next)
        _anon_sender_next += 1
        _anon_senders[key] = alias
        while len(_anon_senders) > _ANON_SENDER_MAX_ENTRIES:
            _anon_senders.popitem(last=False)
    else:
        _anon_senders.move_to_end(key)
    return alias


def _event_user_alias(event: Any) -> str:
    """模型可见的发送者稳定标识。"""
    return _anonymize_anonymous_sender(event) or _anonymize_user_id(getattr(event, "user_id", ""))


def _event_display_name(event: Any) -> str:
    """模型可见的发送者显示名。匿名昵称是发送者自选的，用它等于让人随手冒充别人。"""
    if _anonymous_identity(event):
        return _ANONYMOUS_DISPLAY_NAME
    return _sender_display_name(getattr(event, "sender", None), getattr(event, "user_id", ""))


def _event_sender_key(event: Any) -> str:
    """“同一发送者”判定用的键（最近图片绑定），匿名成员之间不能互相命中。"""
    return _anonymous_identity(event) or str(getattr(event, "user_id", "") or "")


def _user_identity_lines(event: Any, display_name: str) -> List[str]:
    """生成模型可见的用户身份/称呼提示；权限仍由代码侧 platform_auth 控制。"""
    anonymous = bool(_anonymous_identity(event))
    user_id = "" if anonymous else getattr(event, "user_id", "")
    profile = _qq_user_profile(user_id)
    stable_user = _event_user_alias(event)
    lines = [
        f"稳定用户标识: {stable_user}",
        f"显示名: {_sanitize_name(display_name)}",
        # 标识排在显示名前面、历史里又每行都跟着，模型很容易拿它当称呼；而 @ 标记确实要用它，不能一概禁。
        f"称呼规则: 说到这个人时用显示名，{stable_user} 只在 [at:{stable_user}] 里用，不要写进正文",
    ]
    address_as = str(profile.get("address_as") or "").strip()
    if address_as:
        lines.append(f"称呼要求: 请称呼该用户为「{_sanitize_name(address_as)}」")
    title = str(profile.get("title") or "").strip()
    if title:
        lines.append(f"身份标签: {_sanitize_name(title)}")
    note = str(profile.get("note") or "").strip()
    if note:
        lines.append(f"称呼/语气备注: {_sanitize_name(note, max_len=80)}")
    lines.append(f"Clonoth 管理员: {'是' if (not anonymous and _is_admin_user(user_id)) else '否'}")
    lines.append("权限说明: 管理员权限仅由系统配置判定，以上称呼配置不授予任何权限。")
    if anonymous:
        lines.append("身份说明: 群匿名消息，发送者身份无法核验")
    return lines


def _format_hhmm(timestamp: Optional[int]) -> str:
    """把 OneBot 秒级时间戳格式化为入口节点要求的 HH:MM。"""
    try:
        return dt.datetime.fromtimestamp(int(timestamp or time.time()), _CST).strftime("%H:%M")
    except Exception:
        return dt.datetime.now(_CST).strftime("%H:%M")


def _compact_text(text: str, limit: int | None = None) -> str:
    """把历史消息压缩到单行，避免每轮请求携带过长上下文。"""
    limit = live.history_text_limit if limit is None else limit
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit] + "…" if len(text) > limit else text


# 商城表情走图片下载通道，占位符必须和 image 一致，否则附件路径回填不到正文。
STICKER_SEGMENT_TYPES = frozenset({"mface", "marketface"})
IMAGE_SEGMENT_TYPES = frozenset({"image"}) | STICKER_SEGMENT_TYPES
# 下游用 text.replace(IMAGE_PLACEHOLDER, ...) 把附件路径回填进正文，改字面量会同时打断回填和合并转发的占位清理。
IMAGE_PLACEHOLDER = "[图片]"
FACE_PLACEHOLDER = "[QQ表情]"
VOICE_PLACEHOLDER = "[语音]"
VIDEO_PLACEHOLDER = "[视频]"
FORWARD_PLACEHOLDER = "[合并转发]"
STICKER_PLACEHOLDER = "[表情包]"


def _segment_type_and_data(segment: Any) -> tuple[str, dict[str, Any]]:
    """兼容 NoneBot MessageSegment 与 OneBot dict segment。"""
    if isinstance(segment, dict):
        seg_type = str(segment.get("type") or "")
        raw_data = segment.get("data")
        return seg_type, raw_data if isinstance(raw_data, dict) else {}
    seg_type = str(getattr(segment, "type", "") or "")
    raw_data = getattr(segment, "data", {}) or {}
    return seg_type, raw_data if isinstance(raw_data, dict) else {}


def _bot_id_set(bot_self_id: Any) -> set[str]:
    value = str(bot_self_id or "").strip()
    return {value} if value else set()


def _segment_to_text(seg_type: str, data: Mapping[str, Any], bot_ids: set[str]) -> str | None:
    """单个消息段的模型可读文本。返回 None 表示这一段不进正文。"""
    if seg_type == "text":
        return str(data.get("text") or "")
    if seg_type == "at":
        qq = str(data.get("qq") or "").strip()
        if _qq_matches_bot(qq, bot_ids):
            return None
        if qq.lower() == "all":
            return "@全体成员"
        # 没有 profile 显示名时回退到匿名别名（并登记映射），避免裸 QQ 号进入上下文。
        return f"@{_qq_profile_display_name(qq) or _anonymize_user_id(qq)}"
    if seg_type in IMAGE_SEGMENT_TYPES:
        return IMAGE_PLACEHOLDER
    if seg_type == "face":
        face_id = str(data.get("id") or "").strip()
        if not face_id:
            return FACE_PLACEHOLDER
        return f"[QQ表情:{face_display_name(face_id) or face_id}]"
    if seg_type == "record":
        return VOICE_PLACEHOLDER
    if seg_type == "video":
        return VIDEO_PLACEHOLDER
    if seg_type == "file":
        name = str(data.get("name") or data.get("file_name") or data.get("file") or "").strip()
        return f"[文件:{_sanitize_name(Path(name).name if name else '附件', max_len=80)}]"
    if seg_type == "forward":
        return FORWARD_PLACEHOLDER
    if seg_type == "reply":
        # 引用内容由 _build_reply_context 单独还原成引用块，正文里再放一次等于把裸 message_id 交给模型。
        return None
    return f"[{seg_type}]" if seg_type else None


def _message_to_text(
    message: Any, bot_self_id: Any = None, forward_blocks: List[str] | None = None,
) -> str:
    """把 OneBot 消息段转换为模型可读文本，同时用占位符保留非文本内容。

    forward_blocks 按出现顺序顶掉转发占位符，展开内容就落在卡片原来的位置。
    """
    bot_ids = _bot_id_set(bot_self_id)
    pending = list(forward_blocks or [])
    parts: List[str] = []
    for segment in message:
        seg_type, data = _segment_type_and_data(segment)
        if seg_type == "forward" and pending:
            parts.append("\n" + pending.pop(0) + "\n")
            continue
        rendered = _segment_to_text(seg_type, data, bot_ids)
        if rendered is not None:
            parts.append(rendered)
    return "".join(parts).strip()


def _message_to_text_generic(
    message: Any, bot_self_id: Any = None, forward_blocks: List[str] | None = None,
) -> str:
    """把 NoneBot Message、OneBot segment list 或 CQ 字符串转换为模型可读文本。"""
    if message is None:
        return ""
    if isinstance(message, str):
        return _format_cq_message(message, bot_self_id, forward_blocks).strip()
    # Message 是 list 子类，和裸 segment list 走同一条遍历。
    if isinstance(message, (list, tuple)):
        return _message_to_text(message, bot_self_id, forward_blocks)
    try:
        return _message_to_text(Message(message), bot_self_id, forward_blocks)
    except Exception:
        return str(message).strip()


def _parse_cq_params(raw: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in (raw or "").split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        out[key] = value
    return out


def _iter_segments(message: Any) -> List[tuple[str, Dict[str, Any]]]:
    """把 Message、裸 segment list 或 CQ 字符串摊成同一串 (类型, data)。"""
    if message is None:
        return []
    if isinstance(message, str):
        return [(m.group(1), _parse_cq_params(m.group(2) or "")) for m in _CQ_RE.finditer(message)]
    try:
        return [_segment_type_and_data(segment) for segment in message]
    except Exception:
        return []


@dataclass(frozen=True)
class ForwardSource:
    """一张转发卡片的来源。forward_id 要调接口拉，inline 是上报里直接内嵌的子消息。"""

    forward_id: str = ""
    inline: tuple[Any, ...] = ()


def _forward_source_from_data(data: Mapping[str, Any]) -> ForwardSource | None:
    """把一个 forward 段的 data 解析成来源。两种形态都取不到时返回 None。"""
    forward_id = ""
    for key in ("id", "res_id", "resId", "forward_id", "forwardId"):
        value = data.get(key)
        if value is not None and str(value).strip():
            forward_id = str(value).strip()
            break
    # 有些实现把嵌套层直接内嵌成 node 列表而不给 res_id，不认这一形态那层就只剩占位符。
    inline: tuple[Any, ...] = ()
    for key in ("content", "messages", "nodes"):
        value = data.get(key)
        if isinstance(value, (list, tuple)) and value:
            inline = tuple(value)
            break
    if not forward_id and not inline:
        return None
    return ForwardSource(forward_id=forward_id, inline=inline)


def _extract_forward_sources(message: Any) -> List[ForwardSource]:
    """按出现顺序提取消息里的合并转发卡片。顺序要和正文里的占位符一一对上。"""
    sources: List[ForwardSource] = []
    for seg_type, data in _iter_segments(message):
        if seg_type != "forward":
            continue
        source = _forward_source_from_data(data)
        if source is not None:
            sources.append(source)
    return sources


_FORWARD_CACHE_TTL_SEC = 120.0
_FORWARD_CACHE_MAX = 64
_forward_msg_cache: "OrderedDict[str, tuple[float, List[Dict[str, Any]]]]" = OrderedDict()


def _normalize_forward_messages(data: Any) -> List[Dict[str, Any]] | None:
    """get_forward_msg 的返回在各实现间形状不一：裸列表、{messages}、{data:{messages}}。"""
    if isinstance(data, list):
        return [m for m in data if isinstance(m, dict)]
    if not isinstance(data, dict):
        return None
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if isinstance(messages, list):
        return [m for m in messages if isinstance(m, dict)]
    return None


async def _get_forward_messages(bot: Bot, forward_id: str) -> List[Dict[str, Any]] | None:
    """通过 NapCat get_forward_msg 读取合并转发消息列表。

    结果按 res_id 短期缓存：一条消息的正文展开和图片采集各遍历一遍，同一张卡片没有
    拉两次的道理。
    """
    if not forward_id:
        return None
    now = time.time()
    cached = _forward_msg_cache.get(forward_id)
    if cached is not None:
        if now - cached[0] <= _FORWARD_CACHE_TTL_SEC:
            _forward_msg_cache.move_to_end(forward_id)
            return cached[1]
        _forward_msg_cache.pop(forward_id, None)
    try:
        data = await bot.call_api("get_forward_msg", id=forward_id)
    except Exception:
        logger.warning("get_forward_msg failed for forward_id=%s", forward_id, exc_info=True)
        return None
    messages = _normalize_forward_messages(data)
    if messages is None:
        return None
    _forward_msg_cache[forward_id] = (now, messages)
    while len(_forward_msg_cache) > _FORWARD_CACHE_MAX:
        _forward_msg_cache.popitem(last=False)
    return messages


async def _forward_source_messages(bot: Bot, source: ForwardSource) -> List[Dict[str, Any]] | None:
    """取一张卡片的子消息。接口拉不到时退回上报里内嵌的那份。"""
    messages = await _get_forward_messages(bot, source.forward_id) if source.forward_id else None
    if messages is None:
        messages = [m for m in source.inline if isinstance(m, dict)] or None
    return messages


# 合并转发子消息发送者无名片/昵称时的固定占位。
FORWARD_SENDER_PLACEHOLDER = "转发用户"


def _format_forward_sender(sender: Any) -> str:
    """返回合并转发子消息发送者的显示名。

    2026-07-09 修改原因：合并转发卡片里的发送者往往是与 Bot 无直接交互的路人
    （一张卡片可能带几十个陆陌号）。不应把这些无关 QQ 号写入匿名映射表（更不应持久化）。
    因此这里不再调 _anonymize_user_id（既不登记内存匿名表、也不落盘），直接用
    profile 显示名 / 群名片 / 昵称；都没有时回退到固定占位，绝不暴露裸 QQ 号。
    """
    if isinstance(sender, dict):
        user_id = str(sender.get("user_id") or "").strip()
        raw_name = str(sender.get("card") or sender.get("nickname") or "").strip()
    else:
        user_id = str(getattr(sender, "user_id", "") or "").strip()
        raw_name = str(getattr(sender, "card", "") or getattr(sender, "nickname", "") or "").strip()
    display = _qq_profile_display_name(user_id) or raw_name
    return _sanitize_name(display) if display else FORWARD_SENDER_PLACEHOLDER


def _truncate_forward_text(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= live.forward_msg_text_limit:
        return text
    return text[:live.forward_msg_text_limit] + "\n…（合并转发内容过长，已截断）"


async def _format_forward_msg(
    bot: Bot,
    source: ForwardSource,
    bot_self_id: Any = None,
    *,
    depth: int,
    visited: set[str],
    remaining: List[int],
) -> str:
    """递归读取并格式化单张合并转发卡片。"""
    forward_id = source.forward_id
    label = f"合并转发 id={forward_id}" if forward_id else "合并转发"
    if not live.enable_forward_msg_input:
        return f"[{label}]"
    if not forward_id and not source.inline:
        return "[合并转发:缺少id]"
    if depth <= 0:
        return f"[{label}, 已达到读取深度上限]"
    if forward_id and forward_id in visited:
        return f"[{label}, 已跳过循环引用]"
    if remaining[0] <= 0:
        return "[合并转发:已达到读取条数上限]"

    if forward_id:
        visited.add(forward_id)
    messages = await _forward_source_messages(bot, source)
    if messages is None:
        visited.discard(forward_id)
        return f"[{label}, 读取失败]"

    lines: List[str] = [f"【{label}】"]
    for index, item in enumerate(messages, start=1):
        if remaining[0] <= 0:
            lines.append("…（合并转发条数过多，已截断）")
            break
        remaining[0] -= 1
        sender_name = _format_forward_sender(item.get("sender"))
        content = item.get("content") if item.get("content") is not None else item.get("message")
        text = await _message_to_text_with_forward(
            bot,
            content,
            bot_self_id,
            depth=depth - 1,
            visited=visited,
            remaining=remaining,
        )
        text = _compact_text(text or "[暂不支持的消息类型]", limit=max(live.history_text_limit, 1200))
        # 合并转发子消息只展示发送者显示名，不附带匿名 ID（避免缓存无关号）。
        lines.append(f"{index}. [{_format_hhmm(item.get('time'))}] {sender_name}: {text}")
    lines.append("【合并转发结束】")
    visited.discard(forward_id)
    return _truncate_forward_text("\n".join(lines))


async def _message_to_text_with_forward(
    bot: Bot,
    message: Any,
    bot_self_id: Any = None,
    *,
    depth: int | None = None,
    visited: set[str] | None = None,
    remaining: List[int] | None = None,
) -> str:
    """把消息转为文本，合并转发卡片就地展开成内容。"""
    if not live.enable_forward_msg_input:
        return _message_to_text_generic(message, bot_self_id)
    sources = _extract_forward_sources(message)
    if not sources:
        return _message_to_text_generic(message, bot_self_id)

    depth_value = live.forward_msg_max_depth if depth is None else depth
    visited_set = visited if visited is not None else set()
    remaining_box = remaining if remaining is not None else [live.forward_msg_max_messages]

    async def expand() -> List[str]:
        blocks: List[str] = []
        for source in sources:
            blocks.append(await _format_forward_msg(
                bot,
                source,
                bot_self_id,
                depth=depth_value,
                visited=visited_set,
                remaining=remaining_box,
            ))
        return blocks

    if depth is not None:
        return _message_to_text_generic(message, bot_self_id, await expand())

    timeout = float(live.forward_msg_timeout_sec)
    try:
        blocks = await asyncio.wait_for(expand(), timeout=timeout)
    except asyncio.TimeoutError:
        # 群触发判定也走这条路，干等下去会把后面的 matcher 一起压住。
        logger.warning("forward expansion timed out after %.1fs", timeout)
        blocks = ["[合并转发:读取超时]" for _ in sources]
    return _message_to_text_generic(message, bot_self_id, blocks)


_EVENT_TEXT_CACHE_MAX = 128
_event_text_cache: "OrderedDict[tuple[str, ...], str]" = OrderedDict()


async def _event_text_with_forward(bot: Bot, event: Event) -> str:
    """事件正文，转发卡片已展开。同一条事件只展开一次。

    两个 rule 加 handler 会各要一份同样的文本；判定缓存和冷却记账又都拿正文当 key，
    两边给的不一样这条消息就会被判两遍。
    """
    message = event.get_message() if hasattr(event, "get_message") else None
    if not _extract_forward_sources(message):
        return _message_to_text_generic(message, getattr(bot, "self_id", None))
    key = _event_identity(event)
    cached = _event_text_cache.get(key)
    if cached is not None:
        _event_text_cache.move_to_end(key)
        return cached
    text = await _message_to_text_with_forward(bot, message, getattr(bot, "self_id", None))
    _event_text_cache[key] = text
    while len(_event_text_cache) > _EVENT_TEXT_CACHE_MAX:
        _event_text_cache.popitem(last=False)
    return text


def _format_cq_message(
    text: str, bot_self_id: Any = None, forward_blocks: List[str] | None = None,
) -> str:
    bot_ids = _bot_id_set(bot_self_id)
    pending = list(forward_blocks or [])

    def repl(match: re.Match[str]) -> str:
        seg_type = match.group(1)
        if seg_type == "forward" and pending:
            return "\n" + pending.pop(0) + "\n"
        rendered = _segment_to_text(seg_type, _parse_cq_params(match.group(2) or ""), bot_ids)
        return "" if rendered is None else rendered

    return _CQ_RE.sub(repl, text)


def _scan_reply_reference(message: Any, raw_message: Any = None) -> tuple[bool, Any | None]:
    """扫描 reply 段：返回（有没有 reply 段, 被引用消息 id）。id 可能为 None（段在但无可用 id）。"""
    def pick_id(data: Dict[str, Any]) -> Any | None:
        for key in ("id", "message_id", "messageId", "messageid", "message_seq", "seq"):
            value = data.get(key)
            if value is not None and str(value).strip():
                return value
        return None

    has_reply = False
    try:
        segments = list(message) if message is not None and not isinstance(message, str) else []
    except Exception:
        segments = []
    for seg in segments:
        seg_type, data = _segment_type_and_data(seg)
        if seg_type != "reply":
            continue
        has_reply = True
        rid = pick_id(data)
        if rid is not None:
            return True, rid

    for candidate in (message, raw_message):
        if not isinstance(candidate, str):
            continue
        for match in _CQ_RE.finditer(candidate):
            if match.group(1) != "reply":
                continue
            has_reply = True
            rid = _parse_cq_params(match.group(2) or "").get("id")
            if rid is not None and str(rid).strip():
                return True, rid
    return has_reply, None


def _extract_reply_message_id(message: Any, raw_message: Any = None) -> Any | None:
    """从 reply segment / CQ reply 中提取被引用消息 ID。"""
    return _scan_reply_reference(message, raw_message)[1]


def _remember_message_for_reply_context(event: Event) -> None:
    """缓存最近消息，弥补 NapCat/NoneBot reply 字段不完整的情况。"""
    message_id = getattr(event, "message_id", None)
    if message_id is None or not str(message_id).strip():
        return
    sender = getattr(event, "sender", None)
    _reply_message_cache[str(message_id)] = {
        "message": event.get_message() if hasattr(event, "get_message") else None,
        "raw_message": getattr(event, "raw_message", None),
        "sender": sender,
        "user_id": getattr(event, "user_id", None),
        "time": getattr(event, "time", None),
    }
    if str(message_id) not in _reply_message_cache_order:
        _reply_message_cache_order.append(str(message_id))
    while len(_reply_message_cache_order) > 1000:
        old_id = _reply_message_cache_order.popleft()
        _reply_message_cache.pop(old_id, None)


async def _get_reply_message(bot: Bot, reply_message_id: Any) -> Dict[str, Any] | None:
    """通过 NapCat get_msg 兜底获取被引用消息。"""
    if reply_message_id is None:
        return None
    raw_id = str(reply_message_id).strip()
    message_id_param: Any = int(raw_id) if raw_id.isdigit() else reply_message_id
    try:
        data = await bot.call_api("get_msg", message_id=message_id_param)
    except Exception:
        logger.warning("get_msg failed for reply_message_id=%s", reply_message_id, exc_info=True)
        return None
    if isinstance(data, dict):
        return data
    return None


_REPLY_PAYLOAD_FIELDS = ("message", "raw_message", "sender", "user_id", "time")


def _reply_payload_from_source(source: Any) -> Dict[str, Any]:
    """把 event.reply 对象 / 缓存条目 / get_msg 返回值统一成同一份字段集。"""
    if source is None:
        return {}
    if isinstance(source, dict):
        return {key: source.get(key) for key in _REPLY_PAYLOAD_FIELDS}
    return {key: getattr(source, key, None) for key in _REPLY_PAYLOAD_FIELDS}


def _reply_payload_has_content(payload: Dict[str, Any]) -> bool:
    return bool(payload.get("message") is not None or payload.get("raw_message"))


def _fill_reply_payload(payload: Dict[str, Any], extra: Dict[str, Any]) -> None:
    for key, value in extra.items():
        if value is not None and payload.get(key) is None:
            payload[key] = value


async def _resolve_reply_payload(bot: Bot, event: Event, reply_message_id: Any) -> Dict[str, Any] | None:
    """event.reply → 本地缓存 → get_msg 逐级补全被引用消息，只填空位不覆盖。

    NapCat 私聊引用常常 get_msg 取不回，覆盖式回退会把 event.reply 已经给出的 raw_message/sender 清空。
    """
    payload = _reply_payload_from_source(getattr(event, "reply", None))
    if not _reply_payload_has_content(payload) and reply_message_id is not None:
        _fill_reply_payload(payload, _reply_payload_from_source(_reply_message_cache.get(str(reply_message_id))))
    if not _reply_payload_has_content(payload) and reply_message_id is not None:
        _fill_reply_payload(payload, _reply_payload_from_source(await _get_reply_message(bot, reply_message_id)))
    return payload if any(value is not None for value in payload.values()) else None


_IMAGE_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}


def _attachment_error_text(error: str) -> str:
    if error == "too_large":
        return f"图片太大，已超过当前限制 {live.image_max_bytes // 1024 // 1024}MB。"
    if error == "download_failed":
        return "我收到了图片，但下载失败了，可能是 QQ 临时链接已过期。"
    if error == "unsupported_mime":
        return "我收到了图片，但它不是我能识别的图片格式（只支持 PNG/JPEG/GIF/WEBP/BMP）。"
    if error == "local_denied":
        return "我收到了图片，但它指向 Clonoth 工作区之外的本机路径，已拒绝读取（如为同机部署，请把该目录加入 ONEBOT_LOCAL_SOURCE_ROOTS）。"
    return "我收到了图片，但处理图片时失败了。"


def _touch_attachment_files(attachments: Iterable[Any]) -> None:
    """把被引用的附件 mtime 推到现在：适配层和 engine data_cleanup 都只按 mtime 判龄，续期是唯一能同时对两边生效的手段。"""
    now = time.time()
    for attachment in attachments:
        path = _resolve_attachment_path(attachment)
        if path is None:
            continue
        try:
            os.utime(path, (now, now))
        except OSError:
            continue


def _cleanup_old_qq_attachments(now: float | None = None) -> None:
    """清理 QQ 会话附件目录中的过期文件；默认保留 IMAGE_CACHE_TTL_SECONDS。"""
    global _last_attachment_cleanup_at
    now = time.time() if now is None else now
    if now - _last_attachment_cleanup_at < _ATTACHMENT_CLEANUP_INTERVAL_SEC:
        return
    _last_attachment_cleanup_at = now
    root = Path(CLONOTH_WORKSPACE) / "data" / "attachments"
    if not root.exists():
        return
    cutoff = now - IMAGE_CACHE_TTL_SECONDS
    try:
        for conv_dir in root.iterdir():
            # data/attachments 同时住着 engine 产物和其他适配器的附件，只清 QQ 自己的会话目录。
            if not conv_dir.is_dir() or not conv_dir.name.startswith(_QQ_ATTACHMENT_DIR_PREFIXES):
                continue
            for p in conv_dir.rglob("*"):
                try:
                    if p.is_file() and p.stat().st_mtime < cutoff:
                        p.unlink(missing_ok=True)
                except Exception:
                    continue
            for sub in sorted(
                (x for x in conv_dir.rglob("*") if x.is_dir()),
                key=lambda x: len(x.parts), reverse=True,
            ):
                try:
                    sub.rmdir()
                except Exception:
                    pass
            try:
                conv_dir.rmdir()
            except Exception:
                pass
    except Exception as exc:
        logger.debug("QQ attachment cleanup skipped: %s", exc)


def _remember_recent_images(conversation_key: str, event: Event, attachments: List[Dict[str, Any]]) -> None:
    if not attachments:
        return
    # sender_key 用于"同一发送者"判定（匿名成员之间不互相命中）；sender_qq 会被当真实 QQ 号
    # 塞进转发 node，占位号发不出去，匿名时留空。
    sender_key = _event_sender_key(event)
    sender_qq = "" if _anonymous_identity(event) else str(getattr(event, "user_id", "") or "")
    message_id = str(getattr(event, "message_id", "") or "")
    now = time.time()
    q = _recent_images[conversation_key]
    fq = _recent_files[conversation_key]
    image_atts: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for att in attachments:
        if not att.get("path"):
            continue
        att_copy = dict(att)
        kind = str(att.get("type") or "")
        if kind == "image":
            image_atts.append(att_copy)
            q.append(RecentAttachmentEntry(att_copy, now, sender_key, message_id))
        elif kind == "file":
            fq.append(RecentAttachmentEntry(att_copy, now, sender_key, message_id))
        else:
            continue
        kept.append(att_copy)
    _recent_images.touch(conversation_key)
    _recent_files.touch(conversation_key)
    # 引用索引收全部类型：引用一条文件消息再问，和引用图片一样是明确的指向。
    if kept and message_id:
        _remember_reply_attachments(message_id, conversation_key, sender_qq, kept, created_at=now)


def _remember_reply_attachments(
    message_id: Any,
    conversation_key: str,
    sender_id: str,
    attachments: List[Dict[str, Any]],
    *,
    created_at: float | None = None,
) -> None:
    """持久化引用消息附件索引，供后续主动转发兜底使用。"""
    mid = str(message_id or "").strip()
    if not mid or not attachments:
        return
    now = time.time() if created_at is None else float(created_at)
    kept = [dict(att) for att in attachments if isinstance(att, dict) and att.get("path")]
    if not kept:
        return
    # 旧图绑到新消息时把 mtime 推到现在，否则按 mtime 判龄的清理会在保留期内先删掉源文件。
    _touch_attachment_files(kept)
    images = [att for att in kept if str(att.get("type") or "") != "file"]
    files = [att for att in kept if str(att.get("type") or "") == "file"]
    _reply_attachment_cache[mid] = {
        "conversation_key": str(conversation_key or ""),
        "sender_id": str(sender_id or ""),
        "created_at": now,
        # 两类各按各的上限截断：混在一起用图片那个数，三个文件就能把图全挤掉。
        "attachments": images[:live.max_images_per_turn] + files[:live.max_files_per_turn],
    }
    _trim_reply_attachment_cache()
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_save_reply_attachment_cache())
    except Exception:
        pass


def _trim_reply_attachment_cache() -> None:
    if not _reply_attachment_cache:
        return
    now = time.time()
    cutoff = now - IMAGE_CACHE_TTL_SECONDS
    for mid, item in list(_reply_attachment_cache.items()):
        try:
            created_at = float(item.get("created_at") or 0.0)
        except Exception:
            created_at = 0.0
        if created_at and created_at < cutoff:
            _reply_attachment_cache.pop(mid, None)
    while len(_reply_attachment_cache) > 1000:
        oldest = min(
            _reply_attachment_cache,
            key=lambda key: float(_reply_attachment_cache.get(key, {}).get("created_at") or 0.0),
        )
        _reply_attachment_cache.pop(oldest, None)


def _forward_nodes_from_cached_reply(bot: Bot, reply_message_id: Any) -> list[dict[str, Any]]:
    mid = str(reply_message_id or "").strip()
    if not mid:
        return []
    item = _reply_attachment_cache.get(mid)
    if not isinstance(item, dict):
        return []
    attachments = item.get("attachments") if isinstance(item.get("attachments"), list) else []
    if not attachments:
        return []
    node = _make_forward_node(
        bot,
        "",
        [dict(att) for att in attachments if isinstance(att, dict)],
        nickname="引用图片",
        user_id=item.get("sender_id") or getattr(bot, "self_id", "") or "10000",
    )
    return [node] if node else []


def _cached_reply_image_attachments(message_id: Any, conversation_key: str = "") -> List[Dict[str, Any]]:
    """Return persisted source images bound to an inbound or Bot reply message."""
    mid = str(message_id or "").strip()
    if not mid:
        return []
    item = _reply_attachment_cache.get(mid)
    if not isinstance(item, dict):
        return []
    cached_conversation = str(item.get("conversation_key") or "")
    if conversation_key and cached_conversation and cached_conversation != str(conversation_key):
        return []
    attachments = item.get("attachments") if isinstance(item.get("attachments"), list) else []
    result = [
        dict(att)
        for att in attachments[:live.max_images_per_turn]
        if isinstance(att, dict)
        and str(att.get("type") or "") == "image"
        and att.get("path")
        and (p := _resolve_attachment_path(att)) is not None
        and p.exists()
    ]
    _touch_attachment_files(result)
    return result


def _recent_images_for_text(conversation_key: str, event: Event) -> List[Dict[str, Any]]:
    """Return one unambiguous recent image batch from the current sender only."""
    return select_recent_attachment_entries(
        _recent_images.get(conversation_key, ()),
        sender_id=_event_sender_key(event),
        now=time.time(),
        max_age_seconds=RECENT_IMAGE_MAX_AGE_SECONDS,
        max_items=live.max_images_per_turn,
    )


def _recent_files_for_text(conversation_key: str, event: Event) -> List[Dict[str, Any]]:
    """Return one unambiguous recent file batch from the current sender only."""
    return select_recent_attachment_entries(
        _recent_files.get(conversation_key, ()),
        sender_id=_event_sender_key(event),
        now=time.time(),
        max_age_seconds=RECENT_FILE_MAX_AGE_SECONDS,
        max_items=live.max_files_per_turn,
    )


def _text_looks_like_image_query(text: str) -> bool:
    return looks_like_image_query(text)


def _text_looks_like_file_query(text: str) -> bool:
    return looks_like_file_query(text)


async def _merge_recent_attachments_after_text(
    *,
    event: Event,
    conversation_key: str,
    user_text: str,
    attachments: List[Dict[str, Any]],
) -> None:
    reply_message_id = _extract_reply_message_id(
        event.get_message() if hasattr(event, "get_message") else None,
        getattr(event, "raw_message", None),
    )
    # A reply must stay on its explicit message chain. _build_reply_context will
    # recover an image directly from the quoted message or from the Bot reply's
    # persisted source-image binding. Falling back to an unrelated recent image
    # here caused "再仔细看看图" to replace the intended PNG with an older GIF.
    if should_fallback_to_recent_attachments(
        has_attachments=bool(attachments),
        input_enabled=live.enable_image_input,
        looks_like_query=_text_looks_like_image_query(user_text),
        reply_message_id=reply_message_id,
    ):
        if live.image_wait_after_text_sec > 0:
            await asyncio.sleep(live.image_wait_after_text_sec)
        attachments.extend(_recent_images_for_text(conversation_key, event))

    # 文件走同一套约束，但不等：群文件那条消息早就发完了，不像图那样可能还在路上。
    # 上面真取到图时 has_attachments 已经变真，这一段自己就不会再进。
    if should_fallback_to_recent_attachments(
        has_attachments=bool(attachments),
        input_enabled=live.enable_file_input,
        looks_like_query=_text_looks_like_file_query(user_text),
        reply_message_id=reply_message_id,
    ):
        attachments.extend(_recent_files_for_text(conversation_key, event))


def _segment_image_url(data: Mapping[str, Any]) -> str:
    return str(data.get("url") or data.get("path") or data.get("file") or "").strip()


@dataclass(frozen=True)
class ImageSource:
    """一张待下载的图。sticker 决定回填进正文时叫「图片」还是「表情包」。"""

    url: str
    sticker: bool = False


def _attachment_label(attachment: Mapping[str, Any]) -> str:
    """回填进正文时的名字。表情包和照片对模型是两回事：一个是情绪，一个是内容。"""
    return "表情包" if attachment.get("sticker") else "图片"


def _iter_qq_image_sources(message: Any) -> List[ImageSource]:
    """从 OneBot Message、segment list 或 CQ 字符串中提取图片下载地址。"""
    sources: List[ImageSource] = []
    for seg_type, data in _iter_segments(message):
        if seg_type not in IMAGE_SEGMENT_TYPES:
            continue
        url = _segment_image_url(data)
        if url:
            sources.append(ImageSource(url, seg_type in STICKER_SEGMENT_TYPES))
    return sources


def _safe_attachment_name(name: str, default: str = "attachment") -> str:
    """清理 QQ 文件名，避免路径穿越和控制字符进入附件目录。"""
    raw = Path(str(name or "").replace("\\", "/")).name.strip().strip(". ")
    raw = re.sub(r"[\x00-\x1f\x7f]", "", raw)
    raw = re.sub(r"[<>:\"/\\|?*]+", "_", raw)
    if not raw:
        raw = default
    return raw[:120]


def _segment_file_source(data: Mapping[str, Any]) -> Dict[str, Any] | None:
    """把一个 file 段摊成可下载/可复制的来源。地址和文件名都没有时返回 None。"""
    src = str(data.get("url") or data.get("path") or data.get("file") or "").strip()
    name = str(data.get("name") or data.get("file_name") or data.get("filename") or "").strip()
    if not name and src:
        name = Path(src.split("?", 1)[0].split("#", 1)[0]).name
    if not src and not name:
        return None
    size_raw = data.get("size") or data.get("file_size") or data.get("filesize")
    try:
        size = int(size_raw) if size_raw is not None and str(size_raw).strip() else 0
    except Exception:
        size = 0
    return {"source": src, "name": _safe_attachment_name(name, "file"), "size": size}


def _iter_qq_file_sources(message: Any) -> List[Dict[str, Any]]:
    """从 OneBot file 段中提取可下载/可复制的普通文件来源。"""
    sources: List[Dict[str, Any]] = []
    for seg_type, data in _iter_segments(message):
        if seg_type != "file":
            continue
        source = _segment_file_source(data)
        if source is not None:
            sources.append(source)
    return sources


def _file_attachment_error_text(error: str) -> str:
    if error == "too_large":
        return f"文件太大，已超过当前限制 {live.file_max_bytes // 1024 // 1024}MB。"
    if error == "no_source":
        return "我收到了文件消息，但当前 OneBot 事件没有提供可下载链接。"
    if error == "download_failed":
        return "我收到了文件，但下载失败了，可能是 QQ 临时链接已过期。"
    if error == "unsupported_type":
        return "这个文件类型我不接收，只收文档、文本、图片、音视频和压缩包。"
    if error == "executable_content":
        return "这个文件的内容是可执行程序，我不会接收。"
    if error == "local_denied":
        return "我收到了文件，但它指向 Clonoth 工作区之外的本机路径，已拒绝读取（如为同机部署，请把该目录加入 ONEBOT_LOCAL_SOURCE_ROOTS）。"
    return "我收到了文件，但处理文件时失败了。"


def _local_source_path(source: str) -> Path | None:
    """把 file:// / 本地绝对路径解析成允许读取的真实文件；越界或不存在返回 None。

    事件里的 path 字段由 OneBot 实现决定，只允许工作区与显式配置的 ONEBOT_LOCAL_SOURCE_ROOTS，
    否则等于按对端内容读本机任意文件。
    """
    raw = source[7:] if source.startswith("file://") else source
    try:
        resolved = Path(raw).resolve()
    except Exception:
        return None
    if not resolved.is_file():
        return None
    for root in (CLONOTH_WORKSPACE, *LOCAL_SOURCE_ROOTS):
        try:
            if resolved.is_relative_to(Path(root).resolve()):
                return resolved
        except Exception:
            continue
    logger.warning(
        "collect QQ attachment refused local path outside workspace: %s "
        "(set ONEBOT_LOCAL_SOURCE_ROOTS to allow this directory)",
        resolved,
    )
    return None


async def _read_remote_bytes(client: httpx.AsyncClient, url: str, *, max_bytes: int) -> tuple[bytes, str]:
    """流式读取远端附件，超过 max_bytes 立刻断流并抛 ValueError("too_large")。"""
    content = bytearray()
    content_type = "application/octet-stream"
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        content_type = response.headers.get("content-type", content_type)
        declared = str(response.headers.get("content-length") or "").strip()
        # Content-Length 可信时一个 body 字节都不必读。
        if declared.isdigit() and int(declared) > max_bytes:
            raise ValueError("too_large")
        async for chunk in response.aiter_bytes():
            if not chunk:
                continue
            content.extend(chunk)
            if len(content) > max_bytes:
                raise ValueError("too_large")
    return bytes(content), content_type


async def _read_file_source_bytes(client: httpx.AsyncClient, source: str) -> tuple[bytes, str]:
    """读取 QQ 文件来源；支持 URL、file:// 和本地路径，并限制最大字节数。"""
    if source.startswith("file://") or re.match(r"^[a-zA-Z]:[\\/]", source) or source.startswith("/"):
        local = _local_source_path(source)
        if local is None:
            raise ValueError("local_denied")
        if local.stat().st_size > live.file_max_bytes:
            raise ValueError("too_large")
        return local.read_bytes(), "application/octet-stream"

    return await _read_remote_bytes(client, source, max_bytes=live.file_max_bytes)


async def _file_sources_to_attachments(file_sources: List[Dict[str, Any]], conversation_key: str) -> tuple[list[dict], list[str]]:
    """下载/复制 OneBot 普通文件，返回 Clonoth 附件描述。"""
    result: list[dict] = []
    errors: list[str] = []
    if not live.enable_file_input or not file_sources:
        return result, errors

    _cleanup_old_qq_attachments()
    workspace = Path(CLONOTH_WORKSPACE)
    att_dir = workspace / "data" / "attachments" / conversation_key.replace(":", "_")
    try:
        att_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning("collect QQ file attachment cannot create directory %s: %s", att_dir, exc)
        return result, [_file_attachment_error_text("download_failed")]

    async with httpx.AsyncClient(timeout=IMAGE_DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
        for item in file_sources[:live.max_files_per_turn]:
            source = str(item.get("source") or "").strip()
            name = _safe_attachment_name(str(item.get("name") or "file"), "file")
            reject = inbound_file_reject_reason(name, extra_allowed=live.file_extra_allowed_extensions)
            if reject:
                logger.warning("collect QQ file attachment refused: name=%s reason=%s", name, reject)
                errors.append(_file_attachment_error_text(reject))
                continue
            try:
                size = int(item.get("size") or 0)
            except Exception:
                size = 0
            if size and size > live.file_max_bytes:
                errors.append(_file_attachment_error_text("too_large"))
                continue
            if not source:
                errors.append(_file_attachment_error_text("no_source"))
                continue
            try:
                content, content_type = await _read_file_source_bytes(client, source)
                if not content:
                    errors.append(_file_attachment_error_text("download_failed"))
                    continue
                if len(content) > live.file_max_bytes:
                    errors.append(_file_attachment_error_text("too_large"))
                    continue
                reject = inbound_file_reject_reason(name, content, extra_allowed=live.file_extra_allowed_extensions)
                if reject:
                    logger.warning("collect QQ file attachment refused: name=%s reason=%s", name, reject)
                    errors.append(_file_attachment_error_text(reject))
                    continue
                target_name = f"{os.urandom(8).hex()}_{name}"
                file_path = att_dir / target_name
                file_path.write_bytes(content)
                rel_path = file_path.relative_to(workspace).as_posix()
                result.append({
                    "type": "file",
                    "path": rel_path,
                    "mime_type": content_type or "application/octet-stream",
                    "name": name,
                    "source": "onebot",
                })
            except ValueError as exc:
                errors.append(_file_attachment_error_text(str(exc) or "download_failed"))
            except Exception as exc:
                logger.warning("collect QQ file attachment failed: source=%s error=%s", source, exc)
                errors.append(_file_attachment_error_text("download_failed"))
    return result, errors


async def _image_sources_to_attachments(
    image_sources: List[ImageSource], conversation_key: str,
) -> tuple[list[dict], list[str]]:
    """下载 OneBot 图片 URL/path 列表，并返回 Clonoth 附件描述与用户可读错误。"""
    result: list[dict] = []
    errors: list[str] = []
    if not live.enable_image_input or not image_sources:
        return result, errors

    _cleanup_old_qq_attachments()
    workspace = Path(CLONOTH_WORKSPACE)
    att_dir = workspace / "data" / "attachments" / conversation_key.replace(":", "_")
    try:
        att_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning("collect QQ attachments cannot create directory %s: %s", att_dir, exc)
        return result, [_attachment_error_text("download_failed")]

    async with httpx.AsyncClient(timeout=IMAGE_DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
        for image in image_sources[:live.max_images_per_turn]:
            try:
                source = image.url.strip()
                if not source:
                    continue
                if source.startswith("file://") or re.match(r"^[a-zA-Z]:[\\/]", source) or source.startswith("/"):
                    local = _local_source_path(source)
                    if local is None:
                        errors.append(_attachment_error_text("local_denied"))
                        continue
                    if local.stat().st_size > live.image_max_bytes:
                        raise ValueError("too_large")
                    content = local.read_bytes()
                else:
                    content, _content_type = await _read_remote_bytes(client, source, max_bytes=live.image_max_bytes)
                if not content:
                    logger.warning("collect QQ image attachment skipped empty response: %s", source)
                    errors.append(_attachment_error_text("download_failed"))
                    continue
                mime_type = sniff_image_mime(content)
                if not mime_type:
                    logger.warning("collect QQ image attachment unsupported header: url=%s", source)
                    errors.append(_attachment_error_text("unsupported_mime"))
                    continue
                ext = _IMAGE_EXT_BY_MIME[mime_type]
                filename = f"{os.urandom(16).hex()}{ext}"
                file_path = att_dir / filename
                file_path.write_bytes(content)
                rel_path = file_path.relative_to(workspace).as_posix()
                result.append({
                    "type": "image",
                    "path": rel_path,
                    "mime_type": mime_type,
                    "name": f"image{ext}",
                    "source": "onebot",
                    "sticker": image.sticker,
                })
            except ValueError as exc:
                errors.append(_attachment_error_text(str(exc) or "download_failed"))
            except Exception as exc:
                logger.warning("collect QQ image attachment failed: url=%s error=%s", image.url, exc)
                errors.append(_attachment_error_text("download_failed"))
    return result, errors


async def _iter_message_media(
    bot: Bot,
    message: Any,
    *,
    depth: int,
    visited: set[str],
    remaining: List[int],
) -> tuple[List[ImageSource], List[Dict[str, Any]]]:
    """按正文顺序收集图片地址与文件来源，转发卡片就地展开。

    顺序必须和展开后的正文一致：引用块是按 [图片] 占位符出现的先后回填路径的。
    """
    urls: List[ImageSource] = []
    files: List[Dict[str, Any]] = []
    for seg_type, data in _iter_segments(message):
        if seg_type in IMAGE_SEGMENT_TYPES:
            url = _segment_image_url(data)
            if url:
                urls.append(ImageSource(url, seg_type in STICKER_SEGMENT_TYPES))
            continue
        if seg_type == "file":
            source = _segment_file_source(data)
            if source is not None:
                files.append(source)
            continue
        if seg_type != "forward" or depth <= 0 or remaining[0] <= 0:
            continue
        card = _forward_source_from_data(data)
        if card is None:
            continue
        if card.forward_id:
            if card.forward_id in visited:
                continue
            visited.add(card.forward_id)
        for item in (await _forward_source_messages(bot, card)) or []:
            if remaining[0] <= 0:
                break
            remaining[0] -= 1
            content = item.get("content") if item.get("content") is not None else item.get("message")
            sub_urls, sub_files = await _iter_message_media(
                bot, content, depth=depth - 1, visited=visited, remaining=remaining,
            )
            urls.extend(sub_urls)
            files.extend(sub_files)
        visited.discard(card.forward_id)
    return urls, files


def _scan_top_level_media(message: Any) -> tuple[List[ImageSource], List[Dict[str, Any]]]:
    """只扫当前这一层。图片挂了不该连累文件，所以两边各自兜底。"""
    try:
        urls = _iter_qq_image_sources(message)
    except Exception as exc:
        logger.warning("collect QQ image attachments skipped current message: %s", exc)
        urls = []
    try:
        files = _iter_qq_file_sources(message)
    except Exception as exc:
        logger.warning("collect QQ file attachments skipped current message: %s", exc)
        files = []
    return urls, files


async def _collect_message_media(
    bot: Bot, message: Any, *, expand_forward: bool = True,
) -> tuple[List[ImageSource], List[Dict[str, Any]]]:
    """当前消息里的图片和文件，转发卡片按开关决定要不要下钻。

    正文展开已经把卡片内容拉进缓存，下钻这一趟通常不再走协议往返。
    """
    if not expand_forward:
        return _scan_top_level_media(message)
    if not live.enable_forward_msg_input or not live.enable_forward_msg_media:
        return _scan_top_level_media(message)
    if not _extract_forward_sources(message):
        return _scan_top_level_media(message)
    timeout = float(live.forward_msg_timeout_sec)
    try:
        return await asyncio.wait_for(
            _iter_message_media(
                bot,
                message,
                depth=int(live.forward_msg_max_depth),
                visited=set(),
                remaining=[int(live.forward_msg_max_messages)],
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("forward media scan timed out after %.1fs", timeout)
    except Exception as exc:
        logger.warning("forward media scan failed: %s", exc)
    return _scan_top_level_media(message)


async def _collect_qq_attachments(
    bot: Bot, event, conversation_key: str, *, expand_forward: bool = True,
) -> tuple[list[dict], list[str]]:
    """下载 QQ 当前消息中的图片/普通文件，并返回 Clonoth 附件列表。引用消息由增强 reply 逻辑单独处理。"""
    message = event.get_message() if hasattr(event, "get_message") else None
    image_sources, file_sources = await _collect_message_media(bot, message, expand_forward=expand_forward)
    image_attachments, image_errors = await _image_sources_to_attachments(image_sources, conversation_key)
    file_attachments, file_errors = await _file_sources_to_attachments(file_sources, conversation_key)
    return image_attachments + file_attachments, image_errors + file_errors


def _attachment_abs_path(attachment: Dict[str, Any]) -> Path | None:
    """把 Clonoth 附件描述解析成本地绝对路径，用于 NapCat add_custom_face。"""
    raw = str(attachment.get("path") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = Path(CLONOTH_WORKSPACE) / raw
    return path


async def _collect_reply_image_attachments(bot: Bot, event: Event, conversation_key: str) -> tuple[list[dict], list[str]]:
    """为“收藏表情”命令补充引用消息中的图片。"""
    reply_message_id = _extract_reply_message_id(
        event.get_message() if hasattr(event, "get_message") else None,
        getattr(event, "raw_message", None),
    )
    if reply_message_id is None:
        return [], []
    reply_obj = await _get_reply_message(bot, reply_message_id)
    if not reply_obj:
        return [], []
    image_sources: list[ImageSource] = []
    for key in ("message", "raw_message"):
        value = reply_obj.get(key)
        if value:
            image_sources.extend(_iter_qq_image_sources(value))
    # 去重，避免同一引用图被重复下载。
    image_sources = list(dict.fromkeys(image_sources))
    return await _image_sources_to_attachments(image_sources, conversation_key)


async def _custom_face_command_attachments(
    *,
    bot: Bot,
    event: Event,
    conversation_key: str,
    current_attachments: List[Dict[str, Any]],
) -> tuple[list[dict], list[str]]:
    """为收藏表情命令选择图片：当前消息 > 引用消息 > 最近图片。"""
    if current_attachments:
        return current_attachments, []

    reply_attachments, reply_errors = await _collect_reply_image_attachments(bot, event, conversation_key)
    if reply_attachments:
        return reply_attachments, reply_errors

    recent = _recent_images_for_text(conversation_key, event)
    if recent:
        return recent, []
    return [], reply_errors


def _load_custom_face_names_file() -> List[str]:
    """读取 AI 可见收藏表情名称文件。"""
    return load_custom_face_names(CUSTOM_FACE_NAMES_PATH)


def _load_custom_face_metadata_file() -> List[Dict[str, Any]]:
    """读取程序内部使用的收藏表情元数据文件（md5/resId/emojiId/url 等）。"""
    return load_custom_face_metadata(CUSTOM_FACE_METADATA_PATH)


def _current_custom_face_names() -> List[str]:
    """读取当前 AI 可见表情名，并同步进内存缓存。手动编辑文件后无需重启。"""
    global _custom_face_names
    _custom_face_names = _load_custom_face_names_file()
    return list(_custom_face_names)


def _current_custom_face_metadata() -> List[Dict[str, Any]]:
    """读取当前收藏表情内部元数据，并同步进内存缓存。"""
    global _custom_face_metadata
    _custom_face_metadata = _load_custom_face_metadata_file()
    return list(_custom_face_metadata)


_sticker_store: Optional[StickerStore] = None
_sticker_collector: Optional[StickerCollector] = None
_sticker_tagger: Optional[StickerTagger] = None


def _sticker_store_handle() -> Optional[StickerStore]:
    """惰性开库。开不了就当没这个功能，不能连累消息处理。"""
    global _sticker_store
    if _sticker_store is None:
        try:
            _sticker_store = StickerStore(sticker_store_path(Path(CLONOTH_WORKSPACE)))
        except Exception:
            logger.warning("表情包库打不开，相关功能停用", exc_info=True)
            return None
    return _sticker_store


def _sticker_collect_config() -> CollectConfig:
    return CollectConfig(
        enabled=bool(live.sticker_collect),
        strategy=str(live.sticker_strategy),
        groups=tuple(live.sticker_groups),
        auto_accept=bool(live.sticker_auto_accept),
        pending_limit=int(live.sticker_pending_limit),
        library_limit=int(live.sticker_library_limit),
        pending_ttl_sec=int(live.sticker_pending_ttl_days) * 86400,
        max_bytes=int(live.image_max_bytes),
    )


def _sticker_collector_handle() -> Optional[StickerCollector]:
    global _sticker_collector
    if _sticker_collector is None:
        _sticker_collector = StickerCollector(
            Path(CLONOTH_WORKSPACE),
            config=_sticker_collect_config,
            open_store=_sticker_store_handle,
        )
    return _sticker_collector


def _collect_stickers_from_event(
    event: GroupMessageEvent, attachments: List[Dict[str, Any]],
) -> None:
    """把这条消息里的图丢进收集队列。只入队，落盘和查库都在后台。"""
    if not attachments or not live.sticker_collect:
        return
    collector = _sticker_collector_handle()
    if collector is None:
        return
    try:
        marked = any_marked_segment(list(_iter_segments(event.get_message())))
        collector.submit(
            attachments,
            group_id=event.group_id,
            # 存化名而非真实 QQ 号：这张图会长期留在共享库里。
            user_key=_event_user_alias(event),
            marked=marked,
        )
    except Exception:
        logger.debug("表情包入队失败", exc_info=True)


# 发图时要记"这个会话刚发过这张"，而 process_emojis 的签名里没有会话键。
_sticker_send_conversation: contextvars.ContextVar[str] = contextvars.ContextVar(
    "sticker_send_conversation", default="",
)
# 超过这个大小就不发了：base64 会再胀三分之一，而表情包本来就该是小图。
_STICKER_SEND_MAX_BYTES = 3 * 1024 * 1024


def _sticker_conversation_key(target: Dict[str, Any]) -> str:
    kind = _target_forward_kind(target)
    if kind is None:
        return ""
    prefix = "qq_group" if kind[0] == "group" else "qq_private"
    return _stable_conversation_key(f"{prefix}:{kind[1]}")


async def _resolve_sticker(name: str) -> str:
    """按名字取图库里的表情包，返回可直接塞进 image 段的地址。

    走 base64 而不是本地路径：图库是几个实例共享的，NapCat 容器里没有那个挂载。
    """
    store = _sticker_store_handle()
    if store is None:
        return ""
    try:
        row = store.by_name(name)
        if row is None:
            # 名单里没有中意的时，模型会直接写想表达的情绪。拿这个词去标签里找 ——
            # 这一跳的检索词是它自己挑的，比拿群友那句话去猜要准。
            key = _sticker_send_conversation.get("")
            skip = store.recently_sent(
                key, within_sec=int(live.sticker_repeat_window_sec),
            ) if key else set()
            row = store.by_tag(name, exclude=skip)
        if row is None or not row.usable or not row.rel_path:
            return ""
        raw = await asyncio.to_thread((Path(CLONOTH_WORKSPACE) / row.rel_path).read_bytes)
        if not raw or len(raw) > _STICKER_SEND_MAX_BYTES:
            return ""
        key = _sticker_send_conversation.get("")
        if key:
            await asyncio.to_thread(store.record_sent, key, row.sha256)
        return "base64://" + base64.b64encode(raw).decode("ascii")
    except OSError:
        return ""
    except Exception:
        logger.warning("表情包取图失败: %s", name, exc_info=True)
        return ""


_sticker_combat = CombatTracker()


def _sticker_combat_config() -> CombatConfig:
    return CombatConfig(
        enabled=bool(live.sticker_combat),
        burst_probability=float(live.sticker_burst_probability),
    )


async def _maybe_join_sticker_combat(
    bot: Bot, event: GroupMessageEvent, *, has_image: bool, text: str,
) -> None:
    """群里连着刷图时跟一张。

    语境取群历史里最近的文本，取不到就不发 —— 为了接一张图再调一次多模态不划算，
    而没有语境的随机发图看着就是个乱按键的机器人。
    """
    config = _sticker_combat_config()
    if not config.enabled:
        return
    group = str(event.group_id)
    now = time.monotonic()
    if has_image:
        _sticker_combat.on_image(group, user=str(event.user_id), now=now, config=config)
    else:
        if text.strip():
            _sticker_combat.on_text(group, now=now)
        return
    if not _sticker_combat.should_battle(group, now=now, config=config):
        return

    conversation_key = _stable_conversation_key(f"qq_group:{int(event.group_id)}")
    history = list(_group_history[int(event.group_id)])[-6:]
    context_text = " ".join(entry.text for entry in history if entry.text)
    payload = await _pick_combat_sticker(context_text, conversation_key)
    if not payload:
        return
    try:
        await bot.send_group_msg(
            group_id=int(event.group_id),
            message=_message_from_processed_segments(
                [{"type": "image", "url": payload, "emoji": True}],
            ),
        )
    except Exception:
        logger.warning("表情包接梗发送失败", exc_info=True)
        return
    # 自己发完必须清 streak，否则这张图会算进下一轮，自己跟自己斗下去。
    sent_at = time.monotonic()
    _sticker_combat.mark_battled(group, now=sent_at)
    _sticker_combat.on_self_send(group, now=sent_at)

    if not _sticker_combat.should_burst(
        group, now=sent_at, config=config, roll=random.random(),
    ):
        return
    # 刚发那张已经记进 sent_log，检索时会自动排除，补的一定是另一张。
    extra = await _pick_combat_sticker(context_text, conversation_key)
    if not extra:
        return
    await asyncio.sleep(0.9)
    try:
        await bot.send_group_msg(
            group_id=int(event.group_id),
            message=_message_from_processed_segments(
                [{"type": "image", "url": extra, "emoji": True}],
            ),
        )
    except Exception:
        logger.warning("表情包连发失败", exc_info=True)
        return
    _sticker_combat.mark_burst(group, now=time.monotonic())


async def _pick_combat_sticker(context_text: str, conversation_key: str) -> str:
    """按语境挑一张，挑不出就返回空串。"""
    store = _sticker_store_handle()
    if store is None or not context_text.strip():
        return ""
    try:
        usable = store.all_usable()
        if not usable:
            return ""
        recent = store.recently_sent(
            conversation_key, within_sec=int(live.sticker_repeat_window_sec),
        )
        result = rank(context_text, usable, recently_sent=recent, limit=1)
        # generic 是闲聊闸门：语境不明确时宁可不发。
        if result.generic or result.best is None:
            return ""
        token = _sticker_send_conversation.set(conversation_key)
        try:
            return await _resolve_sticker(result.best.name)
        finally:
            _sticker_send_conversation.reset(token)
    except Exception:
        logger.debug("表情包接梗选图失败", exc_info=True)
        return ""


def _start_sticker_tagger() -> None:
    """图库没启用也照跑：手动传的图同样要打标，只是平时没活会一直空转睡着。

    每轮循环都重新读一次开关，所以关掉之后正在跑的这一批做完就停，不用重启。
    """
    global _sticker_tagger
    if _sticker_tagger is None:
        _sticker_tagger = StickerTagger(
            Path(CLONOTH_WORKSPACE),
            config=lambda: TaggerConfig(enabled=bool(live.sticker_auto_tag)),
            open_store=_sticker_store_handle,
        )
    _sticker_tagger.start()


async def _stop_stickers() -> None:
    global _sticker_store, _sticker_collector, _sticker_tagger
    if _sticker_tagger is not None:
        await _sticker_tagger.stop()
        _sticker_tagger = None
    if _sticker_collector is not None:
        await _sticker_collector.stop()
        _sticker_collector = None
    if _sticker_store is not None:
        _sticker_store.close()
        _sticker_store = None


def _sticker_prompt_entries(conversation_key: str) -> list[str]:
    """这一轮摆给模型看的表情包清单。

    不按群友那句话预筛。表情包配的是 bot 自己要回的语气，而候选是在模型开口之前
    就得定下来的 —— 拿对方说的话去筛，筛出来的情绪往往正好是反的。清单直接摊开，
    让唯一同时知道「对方说了什么」和「我要回什么」的角色去挑。
    """
    limit = int(live.sticker_prompt_limit)
    if limit <= 0 or random.random() >= float(live.sticker_send_probability):
        return []
    store = _sticker_store_handle()
    if store is None:
        return []
    try:
        recent = store.recently_sent(
            conversation_key, within_sec=int(live.sticker_repeat_window_sec),
        )
        usable = [row for row in store.all_usable() if row.sha256 not in recent]
        # 装不下就让发得最少的先上，轮着来；否则冷门图永远排在字典序后面没人见过。
        usable.sort(key=lambda row: (row.sent_count, row.name))
        return [
            f"{row.name}（{'、'.join(row.tags[:8]) or '无标签'}）"
            for row in usable[:limit]
        ]
    except Exception:
        logger.debug("表情包清单读取失败", exc_info=True)
        return []


def _custom_face_prompt_block(conversation_key: str = "") -> str:
    """构造注入给 AI 的表情使用说明。收藏表情按名单给全，表情包这一轮抽中了才给。"""
    current_names = _current_custom_face_names()
    faces = current_names[:live.face_prompt_limit] if live.face_prompt_limit > 0 else []
    stickers = _sticker_prompt_entries(conversation_key)
    if not faces and not stickers:
        return ""

    lines = ["【可用表情】", "在回复里写 [表情:名称] 就能发。"]
    if faces:
        more = (
            "" if len(current_names) <= live.face_prompt_limit
            else f"（另有 {len(current_names) - live.face_prompt_limit} 个未展示）"
        )
        lines.append(f"收藏表情（只能用这些名字）：{'、'.join(faces)}{more}")
    if stickers:
        lines.append("表情包（括号里是它的标签）：" + "；".join(stickers))
        lines.extend([
            "配不配表情包你自己定，不合适就别写 —— 每句话都配图很烦人。",
            "只想甩一张图不说话时，整条回复就写一个 [表情:名称]，别硬凑话。",
            # 名单外的词照样能用：模型想表达的情绪未必对得上某张图的名字，
            # 但多半对得上它的某个标签。查不到的标记会被丢掉，不会漏成文字。
            "名单里挑不出想要的，也可以直接写想表达的情绪，比如 [表情:无语]，会去标签里找。",
        ])
    return "\n".join(lines)


def _reaction_prompt_block() -> str:
    """构造模型可见的 QQ 表态说明；白名单外的 ID 会被丢弃，所以只列白名单。"""
    if not live.enable_reactions:
        return ""
    items = "、".join(f"{eid}={name}" for eid, name in _REACT_MODEL_EMOJIS.items())
    return (
        "【QQ表情表态】\n"
        "想对用户这条消息表态时，在回复里写 [REACT:ID]，平台会把对应表情贴到用户那条消息上。\n"
        f"只能使用下列 ID：{items}。\n"
        "标记本身不会出现在回复文本里；表态是点缀，一条回复最多 1 个。"
    )


async def _sync_custom_face_names_file(bot: Bot) -> List[str]:
    """从 NapCat 收藏表情详情同步已命名表情到 AI 名称文件和内部元数据文件。

    AI 名称文件只写去重后的基础名；内部元数据保留全部同名项（带 (1)/(2) 后缀）。
    """
    global _custom_face_names, _custom_face_metadata
    faces = await fetch_custom_face_details(bot, force=True)
    metadata = extract_named_custom_face_metadata(faces, _bqbs)
    names = extract_named_custom_face_names(faces, _bqbs)
    write_custom_face_names(CUSTOM_FACE_NAMES_PATH, names)
    write_custom_face_metadata(CUSTOM_FACE_METADATA_PATH, metadata)
    _custom_face_names = names
    _custom_face_metadata = metadata
    return names


def _custom_face_value(face: Dict[str, Any], *keys: str) -> Any:
    """按多个可能字段名取收藏表情字段；保留 0 这类合法值。"""
    if not isinstance(face, dict):
        return None
    for key in keys:
        if key not in face:
            continue
        value = face.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


async def _set_custom_face_desc(bot: Bot, face: Dict[str, Any], desc: str) -> tuple[bool, str]:
    """调用 NapCat set_custom_face_desc 给已有收藏表情设置描述。"""
    emoji_id = _custom_face_value(face, "emojiId", "emoji_id", "emoId", "emoid")
    res_id = _custom_face_value(face, "resId", "res_id", "id")
    md5 = _custom_face_value(face, "md5", "MD5")
    if emoji_id is None or not res_id or not md5:
        missing = []
        if emoji_id is None:
            missing.append("emoji_id")
        if not res_id:
            missing.append("res_id")
        if not md5:
            missing.append("md5")
        return False, "找到了该表情，但缺少重命名所需字段：" + "、".join(missing)
    try:
        await bot.call_api(
            "set_custom_face_desc",
            emoji_id=emoji_id,
            res_id=str(res_id),
            md5=str(md5),
            desc=desc,
        )
    except Exception as exc:
        logger.warning("set_custom_face_desc failed: %s", exc, exc_info=True)
        return False, "重命名收藏表情失败：当前 OneBot 实现可能不支持 set_custom_face_desc，或该表情资源信息已失效。"
    invalidate_custom_face_cache(bot)
    try:
        await _sync_custom_face_names_file(bot)
    except Exception:
        logger.warning("sync custom face names after rename failed", exc_info=True)
    return True, f"已将收藏表情重命名为：{desc}\n之后模型可用 [表情:{desc}] 调用。"


async def _add_custom_face_from_attachment(bot: Bot, alias: str, attachment: Dict[str, Any]) -> str:
    """调用 NapCat add_custom_face，并尽量把收藏描述设置为 alias。"""
    path = _attachment_abs_path(attachment)
    if path is None or not path.exists():
        return "找不到要收藏的图片文件，请重新发送图片后再试。"

    content = path.read_bytes()
    md5 = hashlib.md5(content).hexdigest()
    try:
        await bot.call_api(
            "add_custom_face",
            file=str(path),
            md5=md5,
            file_name=path.name,
            is_origin=True,
        )
    except Exception as exc:
        logger.warning("add_custom_face failed: %s", exc, exc_info=True)
        return (
            "收藏表情失败：NapCat add_custom_face 调用失败。\n"
            "提示：NapCat 要求 file 是 NapCat 运行环境可访问的本地路径；"
            "如果 NoneBot 和 NapCat 分容器部署，请把 Clonoth data/attachments 挂载到相同路径。"
        )

    invalidate_custom_face_cache(bot)

    # NapCat 修改描述需要 emoji_id + res_id + md5。add_custom_face 的返回不一定包含这些字段，
    # 因此刷新列表后用 md5 反查新表情，再调用 set_custom_face_desc。
    try:
        await asyncio.sleep(0.8)
        faces = await fetch_custom_face_details(bot, force=True)
        target_face = None
        for face in faces:
            if isinstance(face, dict) and str(face.get("md5") or "").lower() == md5.lower():
                target_face = face
                break
        if isinstance(target_face, dict):
            emoji_id = _custom_face_value(target_face, "emojiId", "emoji_id", "emoId", "emoid")
            res_id = _custom_face_value(target_face, "resId", "res_id")
            if emoji_id is not None and res_id:
                await bot.call_api(
                    "set_custom_face_desc",
                    emoji_id=emoji_id,
                    res_id=str(res_id),
                    md5=md5,
                    desc=alias,
                )
                invalidate_custom_face_cache(bot)
                try:
                    await _sync_custom_face_names_file(bot)
                except Exception:
                    logger.warning("sync custom face names after add failed", exc_info=True)
    except Exception:
        # 描述设置失败不影响“已收藏”的主体结果，模型仍可用序号/md5/文件名兜底调用。
        logger.warning("set_custom_face_desc after add_custom_face failed", exc_info=True)

    return f"已尝试收藏表情：{alias}\n之后模型可用 [表情:{alias}] 调用；如描述设置失败，也可用 /表情列表 查看实际名称。"


_HELP_RE = _cmd_re(("帮助", "命令", "命令列表", "菜单", "help", "commands"))

# (能力, 用法, 说明)。能力为空 = 谁都能用。这张表是 /帮助 的唯一数据源，
# 加命令不更新它，用户就永远不知道它存在。
_COMMAND_CATALOG: tuple[tuple[str, str, str], ...] = (
    ("", "/生图 <描述>", "直接出图，不走闲聊"),
    ("", "/生图帮助", "绘图的参数与写法"),
    ("", "/画师串列表", "可用画风预设"),
    ("draw_preset", "/切换画师串 <名>", "改所有人的默认画风"),
    ("model", "/当前模型", "看主模型是哪个"),
    ("model", "/切换模型 <名>", "改全局主模型"),
    ("custom_face", "/表情包帮助", "收藏表情的全部命令"),
    ("clear_memory", "/清除群记忆 [群名]", "清掉群的长期记忆"),
    ("dream", "/整理记忆", "立刻整理一遍记忆：合并重复、清掉过期"),
    ("proactive", "/主动发送帮助", "借 bot 发消息、文件、合并转发"),
    ("send_dead_letter", "/发送死信", "查看并放行卡住的投递（只能私聊）"),
    ("approval", "/审批 同意 <ID>", "处理审批（只能私聊）"),
)


def _help_text(event: Any) -> str:
    """按调用者实际拿得到的能力过滤命令表。"""
    lines = [f"{usage} —— {desc}" for cap, usage, desc in _COMMAND_CATALOG if not cap or _can(cap, event)]
    if not lines:
        return "你当前没有可用命令。"
    return "【可用命令】\n" + "\n".join(lines) + "\n\n命令都要以 / 开头，不带斜杠的话我会当成普通聊天。"


async def _maybe_handle_help_command(*, event: Event, user_text: str) -> str | None:
    """总命令表。不设权限门 —— 列出来的本来就只有你能用的那些。"""
    if not _HELP_RE.match((user_text or "").strip()):
        return None
    return _help_text(event)


async def _maybe_handle_private_only_in_group(*, event: Event, user_text: str) -> str | None:
    """群里命中「只能私聊」的命令时说清楚，别默默丢给模型烧一轮。

    没这项能力的人拿到 None，照常走聊天 —— 提示本身也会暴露命令存在。
    """
    if not _DEAD_LETTER_RE.match((user_text or "").strip()):
        return None
    if not _can("send_dead_letter", event):
        return None
    return "死信清单里有真实群号和 QQ 号，只在私聊里出。私聊我发 /发送死信。"


def _drawtools_help_text() -> str:
    return (
        "【NovelAI 生图命令】\n"
        "1) /生图 画一个穿着西装的帅哥\n"
        "   直接进入绘图节点并生成图片。\n"
        "2) /生图 初音未来，画风用可爱风\n"
        "   可在需求里指定画风/预设。\n"
        "3) /生图 给我初音未来的NAI提示词，不要画\n"
        "   只输出绘图 tag / prompt，不调用生图。\n"
        "4) /画师串列表\n"
        "   查看可用绘图预设。\n"
        "5) /切换画师串 可爱风\n"
        "   切换默认绘图预设。\n"
        "支持别名：/画图、/绘图、/nai、/draw。"
    )


def _load_drawtools_preset_manager():
    drawtools_dir = Path(CLONOTH_WORKSPACE) / "tools" / "drawtools"
    if str(drawtools_dir) not in sys.path:
        sys.path.insert(0, str(drawtools_dir))
    from preset_manager import PresetNotFoundError, list_presets, switch_preset  # type: ignore
    return list_presets, switch_preset, PresetNotFoundError


async def _maybe_handle_drawtools_command(*, event: Event, user_text: str) -> str | None:
    """处理 QQ 侧绘图帮助/预设命令；返回回复文本，None 表示不是命令。

    帮助与列表对所有人开放；切换预设会写全局 settings.yaml 改掉所有人的默认
    生图风格，与 /切换模型 同属全局配置修改，因此限管理员。
    """
    text = (user_text or "").strip()
    if not text:
        return None
    if _DRAW_HELP_RE.match(text):
        return _drawtools_help_text()
    if _DRAW_PRESET_LIST_RE.match(text):
        try:
            list_presets, _switch_preset, _preset_not_found = _load_drawtools_preset_manager()
            presets = list_presets()
        except Exception as exc:
            logger.warning("list draw presets failed: %s", exc, exc_info=True)
            return f"读取绘图预设失败：{exc}"
        if not presets:
            return "当前没有可用绘图预设。"
        lines = []
        for item in presets:
            mark = "✅ " if item.get("selected") else "   "
            lines.append(f"{mark}{item.get('name') or item.get('id')}（id: {item.get('id')}，model: {item.get('model')}，CFG: {item.get('scale')}，steps: {item.get('steps')}）")
        return "【可用画师串/绘图预设】\n" + "\n".join(lines) + "\n切换：/切换画师串 <名称或id>"
    switch_match = _DRAW_PRESET_SWITCH_RE.match(text)
    if switch_match:
        if not _can("draw_preset", event):
            return _capability_denial(
                "draw_preset", event, "切换默认画师串会影响所有人，仅限 Clonoth 管理员使用。",
            )
        preset_ref = switch_match.group(1).strip()
        try:
            _list_presets, switch_preset, preset_not_found = _load_drawtools_preset_manager()
        except Exception as exc:
            logger.warning("load draw preset manager failed: %s", exc, exc_info=True)
            return f"切换绘图预设失败：{exc}"
        try:
            preset = switch_preset(preset_ref)
        except preset_not_found as exc:
            return f"切换失败：{exc}。可用：" + "、".join(exc.available) + "\n可发送 /画师串列表 查看。"
        except Exception as exc:
            logger.warning("switch draw preset failed: %s", exc, exc_info=True)
            return f"切换绘图预设失败：{exc}"
        return f"已切换默认画师串：{preset.get('name') or preset.get('id')}（id: {preset.get('id')}）"
    return None


async def _maybe_handle_model_command(
    *,
    event: Event,
    user_text: str,
) -> str | None:
    """处理 QQ 管理员 /切换模型 <模型名> 命令，切换全局主模型。

    返回回复文本；None 表示不是本命令。切换通过 Supervisor 的
    POST /v1/config/openai 实时写入 data/config.yaml；Engine 每次处理任务
    前都会重新拉取该配置（fetch_openai_secret），因此无需重启服务。
    """
    text = (user_text or "").strip()
    if not text:
        return None

    if _client is None:
        # 不是本命令时返回 None；确实是本命令但客户端未就绪时才提示。
        if _MODEL_SWITCH_RE.match(text) or _MODEL_SHOW_RE.match(text):
            return "Clonoth Agent 尚未初始化，请稍后重试。"
        return None

    allowed = _can("model", event)

    # 查看当前模型 / 帮助（仅管理员，避免向普通用户暴露底层模型名）。
    if _MODEL_SHOW_RE.match(text):
        if not allowed:
            return _capability_denial("model", event, "模型命令仅限 Clonoth 管理员使用。")
        try:
            cfg = await _client.get_openai_config()
        except Exception as exc:
            logger.warning("get openai config failed: %s", exc, exc_info=True)
            return f"❌ 获取当前模型失败：{exc}"
        return (
            f"ℹ️ 当前 model → {cfg.model or '(未设置)'}\n"
            "切换：/切换模型 <模型名>"
        )

    switch_match = _MODEL_SWITCH_RE.match(text)
    if switch_match:
        if not allowed:
            # 不向非管理员暴露切换能力。
            return _capability_denial("model", event, "模型命令仅限 Clonoth 管理员使用。")
        model_name = switch_match.group(1).strip()
        # 去掉可能的包裹引号/反引号。
        model_name = model_name.strip("`\"'").strip()
        if not model_name:
            return "用法：/切换模型 <模型名>"
        try:
            out = await _client.update_openai_config(model=model_name)
        except Exception as exc:
            logger.warning("switch global model failed: %s", exc, exc_info=True)
            return f"❌ 切换失败：{exc}"
        new_model = ""
        try:
            new_model = str((out or {}).get("openai", {}).get("model") or "").strip()
        except Exception:
            new_model = ""
        logger.info("QQ admin %s switched global model -> %s", getattr(event, "user_id", ""), new_model or model_name)
        return f"✅ model → {new_model or model_name}"

    return None


_STICKER_HELP = (
    "表情包库命令：\n"
    "1) /表情包 统计\n"
    "2) /表情包 列表 或 /表情包 列表 30\n"
    "3) /表情包 待审 —— 看还没过筛的\n"
    "4) /表情包 通过 名字 —— 待审转入库\n"
    "5) /表情包 丢弃 名字 —— 丢掉并记住，同一张不会再收\n"
    "6) /表情包 删除 名字 —— 彻底删，之后还能被重新收\n"
    "7) /表情包 改名 旧名 新名\n"
    "8) /表情包 标签 名字 开心,猫\n"
    "9) /表情包 重打标 或 /表情包 重打标 失败\n"
    "10) /表情包 备份\n"
    "提示：AI 发表情包和发收藏表情用的是同一个 [表情:名称]；开关在控制台的表情包页。"
)


def _sticker_line(row: Any) -> str:
    tags = "、".join(row.tags[:5]) or "还没打标"
    return f"{row.name}（{tags}）"


def _find_sticker(store: Any, token: str) -> Any:
    """先按名字找，找不到再当哈希前缀。名字是给人用的，哈希是给出问题时兜底的。"""
    row = store.by_name(token)
    if row is not None:
        return row
    token = token.strip().lower()
    if len(token) < 6:
        return None
    return next((item for item in store.browse(limit=500) if item.sha256.startswith(token)), None)


async def _maybe_handle_sticker_command(
    *, event: Event, user_text: str,
) -> str | None:
    """处理表情包库管理命令；返回回复文本，None 表示不是命令。"""
    match = _STICKER_RE.match((user_text or "").strip())
    if match is None:
        return None
    rest = (match.group(1) or "").strip()
    if not rest or rest in ("帮助", "help", "?", "？"):
        return _STICKER_HELP

    store = _sticker_store_handle()
    if store is None:
        return "表情包库打不开，去看看 bot 日志。"
    head, _, tail = rest.partition(" ")
    tail = tail.strip()

    if head in ("统计", "状态"):
        counts = store.counts()
        return (
            f"在库 {counts['library']} 张，其中 {counts['usable']} 张可用；"
            f"待审 {counts['pending']}，已弃 {counts['discarded']}，"
            f"等打标 {counts['awaiting_caption']}。"
        )

    if head in ("列表", "待审"):
        state = STATE_LIBRARY if head == "列表" else STATE_PENDING
        try:
            limit = min(max(int(tail), 1), 50) if tail else 20
        except ValueError:
            limit = 20
        rows = store.browse(state=state, limit=limit)
        if not rows:
            return "待审池是空的。" if state == STATE_PENDING else "图库里还没有图。"
        return "\n".join([f"共 {len(rows)} 张：", *(_sticker_line(row) for row in rows)])

    allowed = _can("custom_face", event)
    if not allowed:
        return _capability_denial("custom_face", event, "表情包库的写操作仅限 Clonoth 管理员。")

    if head == "重打标":
        queued = store.reset_captions(only_failed=tail in ("失败", "failed"))
        return f"已把 {queued} 张排进打标队列，后台慢慢跑。"

    if head == "备份":
        from stickers.backup import export_library

        try:
            result = await asyncio.to_thread(export_library, Path(CLONOTH_WORKSPACE))
        except Exception as exc:
            logger.warning("表情包备份失败", exc_info=True)
            return f"备份失败：{exc}"
        return f"已备份 {result.stickers} 条记录、{result.images} 张图到 {result.path.name}。"

    if not tail:
        return f"这个命令要带名字。{_STICKER_HELP}"

    if head == "改名":
        old, _, new = tail.partition(" ")
        row = _find_sticker(store, old.strip())
        if row is None:
            return f"没找到：{old}"
        if not new.strip():
            return "要给新名字。"
        return f"已改名为：{store.rename(row.sha256, new.strip())}"

    if head == "标签":
        name, _, raw = tail.partition(" ")
        row = _find_sticker(store, name.strip())
        if row is None:
            return f"没找到：{name}"
        tags = [item.strip() for item in re.split(r"[,，、\s]+", raw) if item.strip()]
        if not tags:
            return "要给至少一个标签。"
        store.set_manual_tags(row.sha256, tags, override=True)
        return f"{row.name} 的标签已改为：{'、'.join(tags)}（人工标签覆盖自动标签）"

    row = _find_sticker(store, tail)
    if row is None:
        return f"没找到：{tail}"

    if head == "通过":
        from stickers.collect import accept_pending

        if not accept_pending(Path(CLONOTH_WORKSPACE), store, row.sha256):
            return f"{row.name} 不在待审池里。"
        return f"{row.name} 已入库，打完标就能用。"

    if head == "丢弃":
        from stickers.collect import drop_sticker_file

        drop_sticker_file(Path(CLONOTH_WORKSPACE), row.rel_path)
        store.discard(row.sha256)
        return f"已丢弃 {row.name}，同一张图不会再被收进来。"

    if head == "删除":
        from stickers.collect import drop_sticker_file

        drop_sticker_file(Path(CLONOTH_WORKSPACE), row.rel_path)
        store.forget(row.sha256)
        return f"已删除 {row.name}。它再出现在群里还会被重新收。"

    return _STICKER_HELP


async def _maybe_handle_custom_face_command(
    *,
    bot: Bot,
    event: Event,
    user_text: str,
    conversation_key: str,
    current_attachments: List[Dict[str, Any]],
) -> str | None:
    """处理 QQ 侧收藏表情管理命令；返回回复文本，None 表示不是命令。"""
    text = (user_text or "").strip()
    if not text:
        return None

    allowed = _can("custom_face", event)

    if _CUSTOM_FACE_HELP_RE.match(text):
        if not allowed:
            return _capability_denial("custom_face", event, "表情包命令仅限 Clonoth 管理员使用。")
        return (
            "【表情包管理命令示例（仅管理员）】\n"
            "1) /同步表情列表\n"
            "   从 NapCat 收藏同步已命名表情到本地文件。\n"
            "2) /表情列表 或 /表情列表 50\n"
            "   查看 AI 当前可用的表情名称。\n"
            "3) /表情详情列表 或 /表情详情列表 50\n"
            "   查看收藏详情（含未命名项）与序号。\n"
            "4) /收藏表情 开心\n"
            "   收藏当前消息/引用/最近的一张图片，并命名为“开心”。\n"
            "5) /命名表情 3 开心\n"
            "   给第 3 个收藏表情命名/改名（也可用 md5/resId/文件名定位）。\n"
            "6) /删除表情 开心\n"
            "   删除名为“开心”的收藏表情。\n"
            "提示：AI 发送表情用 [表情:名称]；未命名表情不会给 AI 使用。"
        )

    # 以下写操作命令仅限管理员：同步 / 收藏 / 命名 / 删除。
    if _CUSTOM_FACE_SYNC_RE.match(text):
        if not allowed:
            return _capability_denial("custom_face", event, "同步表情列表仅限 Clonoth 管理员使用。")
        try:
            names = await _sync_custom_face_names_file(bot)
        except Exception as exc:
            logger.warning("sync custom face names failed: %s", exc, exc_info=True)
            return "同步表情列表失败：当前 OneBot 实现可能不支持 fetch_custom_face_detail。"
        if not names:
            return f"已同步，但没有发现已命名收藏表情。未命名表情不会写入 AI 表情列表文件：{CUSTOM_FACE_NAMES_PATH}"
        duplicates = count_duplicate_face_names(_custom_face_metadata)
        total = len(_custom_face_metadata)
        dup_note = ""
        if duplicates:
            dup_desc = "、".join(f"{name}(x{count})" for name, count in list(duplicates.items())[:20])
            dup_note = (
                f"\n⚠ 检测到 {len(duplicates)} 组同名表情：{dup_desc}\n"
                "同名表情已全部保留，AI 名称列表仅显示一个基础名；发送时会在同名表情中随机选择一个。"
            )
        return (
            f"已同步 {total} 个已命名收藏表情（AI 可见基础名 {len(names)} 个）。\n"
            f"AI 名称文件：{CUSTOM_FACE_NAMES_PATH}\n"
            f"内部元数据文件：{CUSTOM_FACE_METADATA_PATH}\n"
            "AI 只看到名称文件里的基础名，md5/resId/emojiId 保存在元数据文件里。"
            + dup_note
        )

    list_match = _CUSTOM_FACE_LIST_RE.match(text)
    if list_match:
        if not allowed:
            return _capability_denial("custom_face", event, "表情列表仅限 Clonoth 管理员使用。")
        limit = int(list_match.group(1) or "30")
        limit = max(1, min(100, limit))
        names = _load_custom_face_names_file()
        if not names:
            return f"AI 表情列表文件为空：{CUSTOM_FACE_NAMES_PATH}\n可发送 /同步表情列表 从 NapCat 写入已命名表情；未命名表情不会写入。"
        shown = names[:limit]
        lines = [f"・{name}" for name in shown]
        more = "" if len(names) <= limit else f"\n……还有 {len(names) - limit} 个，可发送“表情列表 {min(len(names), 100)}”查看更多。"
        return "AI 可用收藏表情：\n" + "\n".join(lines) + more + "\n模型可用格式：[表情:名称]\n（此处不显示定位序号，命名/删除请先发“表情详情列表”）"

    detail_match = _CUSTOM_FACE_DETAIL_LIST_RE.match(text)
    if detail_match:
        if not allowed:
            return _capability_denial("custom_face", event, "表情详情列表仅限 Clonoth 管理员使用。")
        limit = int(detail_match.group(1) or "50")
        limit = max(1, min(100, limit))
        try:
            items = await list_custom_face_details(bot, _bqbs, count=max(limit, 48))
        except Exception as exc:
            logger.warning("list custom face details failed: %s", exc, exc_info=True)
            return "获取收藏表情详情失败：当前 OneBot 实现可能不支持 fetch_custom_face_detail。"
        if not items:
            return "当前没有可识别的收藏表情。"
        shown = items[:limit]
        dups = duplicated_detail_names(items)
        lines = [format_custom_face_detail_line(item, dups) for item in shown]
        more = "" if len(items) <= limit else f"\n……还有 {len(items) - limit} 个，可发送“表情详情列表 {min(len(items), 100)}”查看更多。"
        return "收藏表情详情（含未命名项）：\n" + "\n".join(lines) + more + "\n未命名表情可用：命名表情 <序号> <新名字>；序号会随收藏列表变动，同名项请用括号里的 md5 定位。"

    rename_match = _CUSTOM_FACE_RENAME_RE.match(text)
    if rename_match:
        if not allowed:
            return _capability_denial("custom_face", event, "命名/重命名表情仅限 Clonoth 管理员使用。")
        target = rename_match.group(1).strip()
        desc = rename_match.group(2).strip()
        if not target or not desc:
            return "请指定要命名的表情和新名字，例如：命名表情 3 开心"
        # 若定位词是纯基础名且存在多个同名，不直接改，列序号让管理员指定具体一个。
        try:
            siblings = await find_custom_faces_by_base_name(bot, target, _bqbs)
        except Exception as exc:
            logger.warning("find custom faces (rename) failed: %s", exc, exc_info=True)
            return "命名表情失败：当前 OneBot/NapCat 可能不支持 fetch_custom_face_detail。请确认 NapCat 版本支持该详情接口。"
        if len(siblings) > 1:
            lines = []
            for s in siblings:
                extra = s.get("md5") or s.get("res_id") or s.get("file_name") or ""
                extra_note = f"（md5:{str(extra)[:8]}）" if s.get("md5") else (f"（{extra}）" if extra else "")
                lines.append(f"{s['index']}. {s['base_name']}{extra_note}")
            return (
                f"检测到 {len(siblings)} 个同名表情“{target}”，为避免改错，请指定要命名哪一个：\n"
                + "\n".join(lines)
                + f"\n请改用序号，例如：命名表情 {siblings[0]['index']} {desc}"
                + "\n也可用 md5 或 resId 精确定位。"
            )
        try:
            face = await resolve_custom_face(bot, target, _bqbs)
        except Exception as exc:
            logger.warning("resolve custom face (rename) failed: %s", exc, exc_info=True)
            return "命名表情失败：当前 OneBot/NapCat 可能不支持 fetch_custom_face_detail。请确认 NapCat 版本支持该详情接口。"
        if face is not None and not isinstance(face, dict):
            return (
                f"找到了收藏表情：{target}，但当前 OneBot/NapCat 只返回图片 URL，没有 emojiId/resId/md5，无法命名。\n"
                "请确认 NapCat 版本支持 fetch_custom_face_detail；仅旧 fetch_custom_face 返回的 URL 不能用于 set_custom_face_desc。"
            )
        if not isinstance(face, dict):
            return f"没有找到收藏表情：{target}\n可先发送“表情详情列表 50”查看序号，再用“命名表情 <序号> <新名字>”。"
        ok, message = await _set_custom_face_desc(bot, face, desc)
        return message

    delete_match = _CUSTOM_FACE_DELETE_RE.match(text)
    if delete_match:
        if not allowed:
            return _capability_denial("custom_face", event, "删除表情仅限 Clonoth 管理员使用。")
        name = delete_match.group(1).strip()
        if not name:
            return "请指定要删除的表情名称，例如：删除表情 开心"
        # 若输入是纯基础名（不是序号/md5/resId 这类唯一定位），且存在多个同名，
        # 则不直接删除，改为列出同名项的序号，让管理员明确指定删哪个。
        try:
            siblings = await find_custom_faces_by_base_name(bot, name, _bqbs)
        except Exception as exc:
            logger.warning("find custom faces (delete) failed: %s", exc, exc_info=True)
            return "删除表情失败：当前 OneBot/NapCat 可能不支持 fetch_custom_face_detail。请确认 NapCat 版本支持该详情接口。"
        if len(siblings) > 1:
            lines = []
            for s in siblings:
                extra = s.get("md5") or s.get("res_id") or s.get("file_name") or ""
                extra_note = f"（md5:{str(extra)[:8]}）" if s.get("md5") else (f"（{extra}）" if extra else "")
                lines.append(f"{s['index']}. {s['base_name']}{extra_note}")
            return (
                f"检测到 {len(siblings)} 个同名表情“{name}”，为避免误删，请指定要删除哪一个：\n"
                + "\n".join(lines)
                + "\n请改用序号删除，例如：删除表情 " + str(siblings[0]["index"])
                + "\n也可用 md5 或 resId 精确删除。"
            )
        try:
            face = await resolve_custom_face(bot, name, _bqbs)
        except Exception as exc:
            logger.warning("resolve custom face (delete) failed: %s", exc, exc_info=True)
            return "删除表情失败：当前 OneBot/NapCat 可能不支持 fetch_custom_face_detail。请确认 NapCat 版本支持该详情接口。"
        if face is not None and not isinstance(face, dict):
            return (
                f"找到了收藏表情：{name}，但当前 OneBot/NapCat 只返回图片 URL，没有 resId，无法删除。\n"
                "请确认 NapCat 版本支持 fetch_custom_face_detail；仅旧 fetch_custom_face 返回的 URL 不能用于 delete_custom_face。"
            )
        if not isinstance(face, dict):
            return f"没有找到收藏表情：{name}"
        res_id = face.get("resId") or face.get("res_id") or face.get("id")
        if not res_id:
            return f"找到了表情 {name}，但没有 resId，无法删除。"
        try:
            await bot.call_api("delete_custom_face", res_id=str(res_id))
            invalidate_custom_face_cache(bot)
            try:
                await _sync_custom_face_names_file(bot)
            except Exception:
                logger.warning("sync custom face names after delete failed", exc_info=True)
            return f"已删除收藏表情：{name}"
        except Exception as exc:
            logger.warning("delete_custom_face failed: %s", exc, exc_info=True)
            return "删除收藏表情失败：当前 OneBot 实现可能不支持 delete_custom_face，或 resId 已失效。"

    add_match = _CUSTOM_FACE_ADD_RE.match(text)
    if add_match:
        if not allowed:
            return _capability_denial("custom_face", event, "收藏表情仅限 Clonoth 管理员使用。")
        alias = add_match.group(1).strip()
        if not alias:
            return "请给表情起一个名字，例如：收藏表情 开心"
        attachments, errors = await _custom_face_command_attachments(
            bot=bot,
            event=event,
            conversation_key=conversation_key,
            current_attachments=current_attachments,
        )
        if not attachments:
            suffix = "\n" + "\n".join(dict.fromkeys(errors)) if errors else ""
            return "没有找到可收藏的图片。请在同一条消息里带图，或引用/紧接着回复一张图片：收藏表情 名称" + suffix
        return await _add_custom_face_from_attachment(bot, alias, attachments[0])

    return None


# ---------------------------------------------------------------------------
# 管理员主动发送 / 合并转发命令
# ---------------------------------------------------------------------------

_PROACTIVE_PRIVATE_KINDS = {"私聊", "好友", "private", "pm", "user"}
_PROACTIVE_GROUP_KINDS = {"群聊", "群", "group"}

_PROACTIVE_HELP_WORDS = {"主动发送帮助", "主动转发帮助", "通知帮助", "转发帮助"}
_PROACTIVE_LIST_WORDS = {"主动目标", "通知目标", "转发目标", "目标列表"}
_PROACTIVE_PRIVATE_WORDS = {"私信", "私聊通知"}
_PROACTIVE_GROUP_WORDS = {"群发", "群通知"}
_PROACTIVE_FILE_WORDS = {"发文件", "发送文件"}
# 英文别名并进同一个集合。加了斜杠之后中英已经对等，再单开 startswith 分支
# 就是第三份逐字相同的代码。
_PROACTIVE_SEND_WORDS = {"发送", "通知", "主动发送", "send"}
_PROACTIVE_FORWARD_WORDS = {"合并转发", "转发", "转发到", "转发给", "合并转发到", "合并转发给", "forward"}


def _normalize_target_ref(text: str) -> str:
    """把管理员输入的目标名规整为可匹配 token，不写入模型上下文。"""
    return re.sub(r"[\s_\-—]+", "", str(text or "").strip().lower())


def _proactive_help_text() -> str:
    return (
        "【管理员主动发送 / 转发命令】\n"
        "权限：仅 CLONOTH_ADMIN_QQ_USERS 中的管理员可用；命令在 QQ 适配器本地处理，不进入普通模型上下文。\n"
        "目标：使用联系人显示名/好友备注/群名/配置别名；目标列表不会展示真实 QQ 号。\n\n"
        "1) 查看可用目标\n"
        "   /主动目标 或 /主动目标 私聊 或 /主动目标 群\n"
        "2) 主动发文本（可在同条消息附图，图片会一起发送）\n"
        "   /私信 <联系人名> <内容>\n"
        "   /群发 <群名> <内容>\n"
        "   /发送 私聊 <联系人名> <内容>\n"
        "   /发送 群 <群名> <内容>\n"
        "3) 主动发本地文件（路径限制在 Clonoth 工作区内，推荐 data/attachments/）\n"
        "   /发文件 私聊 <联系人名> data/attachments/xxx.png\n"
        "   /发文件 群 <群名> data/attachments/xxx.zip 展示文件名.zip\n"
        "4) 合并转发\n"
        "   /合并转发 私聊 <联系人名> <内容>\n"
        "   /合并转发 群 <群名> <内容>\n"
        "   也可以引用一条消息/合并转发卡片后发送：/合并转发 群 <群名>\n"
        "   文本中用单独一行 --- 可拆成多条转发 node。"
    )


def _parse_proactive_command(text: str) -> dict[str, str] | None:
    """解析管理员主动发送/转发命令。

    返回字段：action(send/file/forward/list/help), target_type(private/group), target_ref, body。
    目标名按单个 token 解析；如群名含空格，请在配置里设置无空格别名/显示名。
    """
    raw = _strip_command_prefix(text)
    if not raw:
        return None
    if raw in _PROACTIVE_HELP_WORDS:
        return {"action": "help"}
    parts = raw.split()
    if not parts:
        return None
    head = parts[0]
    head_l = head.lower()
    if head in _PROACTIVE_LIST_WORDS:
        kind = parts[1] if len(parts) >= 2 else ""
        return {"action": "list", "target_type": _canonical_proactive_target_type(kind)}
    if head in _PROACTIVE_PRIVATE_WORDS and len(parts) >= 3:
        return {"action": "send", "target_type": "private", "target_ref": parts[1], "body": raw.split(None, 2)[2]}
    if head in _PROACTIVE_GROUP_WORDS and len(parts) >= 3:
        return {"action": "send", "target_type": "group", "target_ref": parts[1], "body": raw.split(None, 2)[2]}
    if head_l in _PROACTIVE_SEND_WORDS and len(parts) >= 4:
        target_type = _canonical_proactive_target_type(parts[1])
        if target_type:
            return {"action": "send", "target_type": target_type, "target_ref": parts[2], "body": raw.split(None, 3)[3]}
    if head in _PROACTIVE_FILE_WORDS and len(parts) >= 4:
        target_type = _canonical_proactive_target_type(parts[1])
        if target_type:
            body = raw.split(None, 3)[3]
            return {"action": "file", "target_type": target_type, "target_ref": parts[2], "body": body}
    if head_l in _PROACTIVE_FORWARD_WORDS and len(parts) >= 3:
        target_type = _canonical_proactive_target_type(parts[1])
        if target_type:
            body = raw.split(None, 3)[3] if len(parts) >= 4 else ""
            return {"action": "forward", "target_type": target_type, "target_ref": parts[2], "body": body}
    return None


def _canonical_proactive_target_type(kind: str) -> str:
    token = str(kind or "").strip().lower()
    if not token:
        return ""
    if token in _PROACTIVE_PRIVATE_KINDS:
        return "private"
    if token in _PROACTIVE_GROUP_KINDS:
        return "group"
    return ""


def _target_display_label(target_type: str, name: str) -> str:
    prefix = "私聊" if target_type == "private" else "群聊"
    return f"{prefix}「{_sanitize_name(name, max_len=40)}」"


def _profile_aliases_for_user(user_id: Any) -> list[str]:
    profile = _qq_user_profile(user_id)
    aliases: list[str] = []
    for key in ("display_name", "address_as", "title"):
        value = str(profile.get(key) or "").strip()
        if value:
            aliases.append(value)
    return aliases


async def _safe_call_onebot_list(bot: Bot, api_name: str) -> list[dict[str, Any]]:
    try:
        data = await bot.call_api(api_name)
    except Exception:
        logger.debug("OneBot list API failed: %s", api_name, exc_info=True)
        return []
    payload = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), list) else data
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


async def _private_target_candidates(bot: Bot) -> list[ProactiveTarget]:
    """获取管理员可主动私聊的目标；返回值不包含真实 QQ 字符串展示。"""
    friend_rows = await _safe_call_onebot_list(bot, "get_friend_list") if live.allow_private_friends else []
    friend_ids: set[int] = set()
    names_by_id: dict[int, list[str]] = defaultdict(list)
    for row in friend_rows:
        try:
            uid = int(row.get("user_id"))
        except Exception:
            continue
        friend_ids.add(uid)
        for key in ("remark", "nickname", "card"):
            name = str(row.get(key) or "").strip()
            if name:
                names_by_id[uid].append(name)
    allowed_ids = set(live.admin_users) | set(live.allowed_private_users) | friend_ids
    # 只把 profile 作为别名来源；是否允许发送仍由 allowed_ids/friend list 决定。
    for raw_uid in _QQ_USER_PROFILES:
        try:
            uid = int(raw_uid)
        except Exception:
            continue
        if uid in allowed_ids:
            names_by_id[uid].extend(_profile_aliases_for_user(uid))
    candidates: list[ProactiveTarget] = []
    for uid in sorted(allowed_ids):
        aliases = [a for a in dict.fromkeys(names_by_id.get(uid, []) + _profile_aliases_for_user(uid)) if a]
        label = aliases[0] if aliases else f"User{len(candidates) + 1}"
        candidates.append(ProactiveTarget("private", uid, label))
    return candidates


async def _group_target_candidates(bot: Bot) -> list[ProactiveTarget]:
    rows = await _safe_call_onebot_list(bot, "get_group_list")
    allowed = set(live.allowed_groups)
    candidates: list[ProactiveTarget] = []
    seen: set[int] = set()
    for row in rows:
        try:
            gid = int(row.get("group_id"))
        except Exception:
            continue
        if not _is_group_allowed(gid):
            continue
        seen.add(gid)
        name = str(row.get("group_name") or row.get("group_remark") or "").strip()
        candidates.append(ProactiveTarget("group", gid, _sanitize_name(name or f"Group{len(candidates) + 1}", max_len=40)))
    # 若 get_group_list 不可用，也允许配置白名单群通过“当前群/群名缓存”之外的显式候选参与解析。
    for gid in allowed:
        if gid not in seen:
            candidates.append(ProactiveTarget("group", int(gid), _anonymize_group_id(gid)))
    return candidates


def _candidate_alias_tokens(target: ProactiveTarget) -> set[str]:
    tokens = {_normalize_target_ref(target.label)}
    if target.target_type == "private":
        for alias in _profile_aliases_for_user(target.target_id):
            tokens.add(_normalize_target_ref(alias))
    elif target.target_type == "group":
        tokens.add(_normalize_target_ref(_anonymize_group_id(target.target_id)))
    return {t for t in tokens if t}


def _target_scope_denied(
    capability_key: str, requester: capability.Requester, target: ProactiveTarget,
) -> str:
    """目标不是群时传 None：群主在「本群」之外没有任何可主张的作用域。"""
    return capability.scope_deny_reason(
        capability_key, requester,
        target.target_id if target.target_type == "group" else None,
    )


def _scoped_targets(
    capability_key: str,
    requester: capability.Requester,
    candidates: list[ProactiveTarget],
) -> list[ProactiveTarget]:
    return [t for t in candidates if not _target_scope_denied(capability_key, requester, t)]


def _unresolved_target_text(target_type: str, ref_text: str) -> str:
    """够不着目标清单的人只能得到这一句：「没找到」和「有好几个」分开说，
    这个命令就是一台群名/好友昵称探测器。"""
    return f"没有找到{'私聊' if target_type == 'private' else '群聊'}目标：{ref_text}"


async def _resolve_proactive_target(
    bot: Bot,
    requester: capability.Requester,
    target_type: str,
    ref: str,
    *,
    capability_key: str,
) -> tuple[ProactiveTarget | None, str]:
    ref_text = str(ref or "").strip()
    if not ref_text:
        return None, "缺少目标名称。"
    ref_norm = _normalize_target_ref(ref_text)
    # 管理员显式输入 id 时允许解析，但目标列表/模型上下文不会展示真实 QQ 号。
    explicit = ref_text
    for prefix in ("qq:", "user:", "u:", "群:", "group:", "g:"):
        if explicit.lower().startswith(prefix):
            explicit = explicit[len(prefix):]
            break
    if explicit.isdigit():
        target_id = int(explicit)
        if target_type == "group":
            if not _is_group_allowed(target_id):
                return None, "该群不在允许的主动群聊目标中。请先把群号加进 config/qq.yaml 的 channels.allowed_groups。"
            return ProactiveTarget("group", target_id, _anonymize_group_id(target_id)), ""
        allowed_private_ids = {t.target_id for t in await _private_target_candidates(bot)}
        if target_id not in allowed_private_ids:
            return None, "该私聊目标不在好友/管理员/允许私聊白名单中。"
        return ProactiveTarget("private", target_id, _qq_profile_display_name(target_id) or _anonymize_user_id(target_id)), ""

    if target_type == "group" and ref_norm in {_normalize_target_ref("当前群"), _normalize_target_ref("本群")} and requester.group_id is not None:
        gid = int(requester.group_id)
        if not _is_group_allowed(gid):
            return None, "当前群不在允许的主动群聊目标中。"
        return ProactiveTarget("group", gid, "当前群"), ""

    # 名字这条路先按作用域收候选：拿别人的群名来问「有没有这个群」本身就是一次探测，
    # 答案不能取决于那个名字。
    if target_type == "private":
        blanket = capability.scope_deny_reason(capability_key, requester, None)
        if blanket:
            return None, blanket
    candidates = _scoped_targets(
        capability_key, requester,
        await (_private_target_candidates(bot) if target_type == "private" else _group_target_candidates(bot)),
    )
    matches = [target for target in candidates if ref_norm in _candidate_alias_tokens(target)]
    if len(matches) == 1:
        return matches[0], ""
    if not capability.may_see_target_roster(requester):
        return None, _unresolved_target_text(target_type, ref_text)
    kind = "私聊" if target_type == "private" else "群聊"
    if not matches:
        return None, f"{_unresolved_target_text(target_type, ref_text)}\n可发送“主动目标 {kind}”查看可用名称。"
    labels = "、".join(_sanitize_name(m.label, max_len=24) for m in matches[:10])
    return None, f"目标名称不唯一：{ref_text}\n匹配到：{labels}\n请在配置中设置唯一显示名，或使用管理员显式 id 前缀。"


async def _proactive_target_list_text(bot: Bot, target_type: str = "") -> str:
    sections: list[str] = []
    if target_type in ("", "private"):
        privates = await _private_target_candidates(bot)
        names = [_sanitize_name(t.label, max_len=30) for t in privates if t.label]
        sections.append("【可主动私聊目标】\n" + ("、".join(names[:80]) if names else "（无；需好友列表、管理员或允许私聊白名单）"))
    if target_type in ("", "group"):
        groups = await _group_target_candidates(bot)
        names = [_sanitize_name(t.label, max_len=30) for t in groups if t.label]
        sections.append("【可主动群聊目标】\n" + ("、".join(names[:80]) if names else "（无；需配置 CLONOTH_ALLOWED_GROUPS 或 get_group_list 可用）"))
    return "\n\n".join(sections) + "\n\n提示：目标列表不展示真实 QQ 号；普通模型上下文也不会接触这些真实 id。"


def _target_to_send_dict(target: ProactiveTarget) -> Dict[str, Any]:
    if target.target_type == "private":
        return {"type": "private", "user_id": target.target_id}
    return {"type": "group", "group_id": target.target_id}


def _attachment_path_under_workspace(raw_path: str) -> Path | None:
    raw = str(raw_path or "").strip()
    if not raw:
        return None
    if raw.startswith("file://"):
        raw = raw[7:]
    path = Path(raw)
    if not path.is_absolute():
        path = Path(CLONOTH_WORKSPACE) / path
    try:
        resolved = path.resolve()
        workspace = Path(CLONOTH_WORKSPACE).resolve()
        if workspace not in resolved.parents and resolved != workspace:
            return None
    except Exception:
        return None
    return resolved


def _parse_file_send_body(body: str) -> tuple[Dict[str, Any] | None, str]:
    parts = str(body or "").strip().split(maxsplit=1)
    if not parts:
        return None, "缺少文件路径。"
    path = _attachment_path_under_workspace(parts[0])
    if path is None:
        return None, "文件路径必须位于 Clonoth 工作区内。"
    if not path.exists() or not path.is_file():
        return None, f"文件不存在：{parts[0]}"
    display_name = parts[1].strip() if len(parts) > 1 else path.name
    rel_path = str(path)
    try:
        rel_path = str(path.relative_to(Path(CLONOTH_WORKSPACE).resolve()))
    except Exception:
        pass
    return {"type": "file", "path": rel_path, "name": display_name}, ""


def _forward_media_text(message: Any, bot_self_id: Any = None) -> str:
    """为合并转发提取文本，去掉会重复出现的图片占位符。"""
    text = _message_to_text_generic(message, bot_self_id)
    if _iter_qq_image_sources(message):
        # 转发 node 会单独携带 image 段；留着占位就是卡片里先出现「[图片]」文字再跟真实图片。
        text = re.sub(r"\s+", " ", text.replace(IMAGE_PLACEHOLDER, "")).strip()
    return text


def _forward_content_segments(text: str = "", attachments: list[dict[str, Any]] | None = None, image_urls: list[str] | None = None) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    if text:
        segments.append({"type": "text", "data": {"text": str(text)}})
    for url in image_urls or []:
        if url:
            segments.append({"type": "image", "data": {"file": str(url)}})
    for att in attachments or []:
        path = _resolve_attachment_path(att)
        if path and path.exists() and path.suffix.lower() in _IMAGE_SUFFIXES:
            segments.append({"type": "image", "data": {"file": f"file://{str(path.resolve())}"}})
        elif att:
            name = _attachment_filename(att) or "附件"
            segments.append({"type": "text", "data": {"text": f"[附件: {name}]"}})
    return segments


def _make_forward_node(bot: Bot, text: str = "", attachments: list[dict[str, Any]] | None = None, *, nickname: str = "Clonoth 通知", user_id: Any = None, image_urls: list[str] | None = None) -> dict[str, Any] | None:
    content = _forward_content_segments(text, attachments, image_urls)
    if not content:
        return None
    return {
        "type": "node",
        "data": {
            "user_id": str(user_id or getattr(bot, "self_id", "") or "10000"),
            "nickname": _sanitize_name(nickname, max_len=32),
            "content": content,
        },
    }


def _append_attachment_notes(text: str, errors: list[str]) -> str:
    """把附件处理错误（图片太大/格式不支持）并进转发文本，避免被引用/合并转发路径静默丢图。"""
    notes = "\n".join(dict.fromkeys(errors))
    if not notes:
        return text
    return f"{text}\n{notes}" if text else notes


async def _forward_messages_to_nodes(bot: Bot, messages: list[dict[str, Any]], conversation_key: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for item in messages[:live.forward_msg_max_messages]:
        sender = item.get("sender") if isinstance(item.get("sender"), dict) else {}
        sender_id = sender.get("user_id") or item.get("user_id") or getattr(bot, "self_id", "")
        nickname = sender.get("card") or sender.get("nickname") or "转发消息"
        content = item.get("content") if item.get("content") is not None else item.get("message")
        text = _forward_media_text(content, getattr(bot, "self_id", None))
        image_sources = _iter_qq_image_sources(content)
        attachments: list[dict[str, Any]] = []
        if image_sources:
            attachments, errors = await _image_sources_to_attachments(image_sources, conversation_key)
            text = _append_attachment_notes(text, errors)
        node = _make_forward_node(bot, text, attachments, nickname=nickname, user_id=sender_id)
        if node:
            nodes.append(node)
    return nodes


async def _forward_nodes_from_reply(bot: Bot, event: Event, conversation_key: str) -> list[dict[str, Any]]:
    reply_message_id = _extract_reply_message_id(
        event.get_message() if hasattr(event, "get_message") else None,
        getattr(event, "raw_message", None),
    )
    reply = await _resolve_reply_payload(bot, event, reply_message_id)
    if not reply:
        cached_nodes = _forward_nodes_from_cached_reply(bot, reply_message_id)
        if cached_nodes:
            return cached_nodes
        logger.info("forward reply skipped: no reply object/id=%s", reply_message_id)
        return []
    message = reply.get("message") if reply.get("message") is not None else reply.get("raw_message")
    sources = _extract_forward_sources(message)
    if sources:
        nodes: list[dict[str, Any]] = []
        for source in sources[:3]:
            messages = await _forward_source_messages(bot, source)
            if messages:
                nodes.extend(await _forward_messages_to_nodes(bot, messages, conversation_key))
        if nodes:
            return nodes
    sender = reply.get("sender") if isinstance(reply.get("sender"), dict) else {}
    sender_id = sender.get("user_id") or reply.get("user_id") or getattr(bot, "self_id", "")
    nickname = sender.get("card") or sender.get("nickname") or "引用消息"
    text = _forward_media_text(message, getattr(bot, "self_id", None))
    image_sources = _iter_qq_image_sources(message)
    attachments: list[dict[str, Any]] = []
    if image_sources:
        attachments, errors = await _image_sources_to_attachments(image_sources, conversation_key)
        text = _append_attachment_notes(text, errors)
        if attachments and reply_message_id is not None:
            _remember_reply_attachments(reply_message_id, conversation_key, str(sender_id or ""), attachments)
    if not attachments and reply_message_id is not None:
        cached_nodes = _forward_nodes_from_cached_reply(bot, reply_message_id)
        if cached_nodes:
            return cached_nodes
    node = _make_forward_node(bot, text, attachments, nickname=nickname, user_id=sender_id)
    return [node] if node else []


def _request_send_context(
    scope: str, request_identity: str, conversation_key: str = "",
) -> OutboundSendContext:
    digest = hashlib.sha256(request_identity.encode("utf-8", "ignore")).hexdigest()
    return OutboundSendContext(
        conversation_key=conversation_key,
        idempotency_key=f"request:{scope}:{digest}",
    )


async def _send_forward_nodes(
    bot: Bot,
    target: ProactiveTarget,
    nodes: list[dict[str, Any]],
    *,
    send_context: OutboundSendContext | None = None,
    image_attachments: list[Any] | None = None,
) -> None:
    target_dict = _target_to_send_dict(target)
    identity = "forward-nodes:" + hashlib.sha256(
        json.dumps(nodes, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    context = (send_context or OutboundSendContext()).child(identity)
    key = make_idempotency_key(
        target_dict, identity, event_id=context.idempotency_key or context.event_id,
    )
    claim = await _outbound_idempotency.begin(key)
    if not claim.acquired and claim.state == "pending":
        resolved = await _outbound_idempotency.wait_for_resolution(key)
        claim = (
            await _outbound_idempotency.begin(key)
            if resolved is None else IdempotencyClaim(key, False, resolved)
        )
    if not claim.acquired:
        return
    heartbeat = asyncio.create_task(_heartbeat_idempotency_claim(claim))
    try:
        async def send_forward() -> Any:
            if target.target_type == "group":
                return await bot.call_api(
                    "send_group_forward_msg", group_id=int(target.target_id), messages=nodes,
                )
            return await bot.call_api(
                "send_private_forward_msg", user_id=int(target.target_id), messages=nodes,
            )

        result = await protected_claim_send(
            _outbound_idempotency,
            claim,
            send_forward,
            sent_ttl=_sent_ttl_for_context(context),
            message_id_getter=_extract_sent_message_id,
        )
        if image_attachments:
            message_id = _extract_sent_message_id(result)
            if message_id:
                # 合并转发只返回卡片一个 message_id，整批图都挂在卡片上
                _remember_message_attachments(message_id, list(image_attachments))
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat


# 注意：自然语言转发（“把 xx 转发给 xx”等）不再在 Bot 入口做本地正则拦截，
# 避免把本应交给 AI（qq.orchestrator）分析“哪些条目需要转发”的请求误譍为
# 本地转发指令。这类需求原样进入 Agent，由其调用 qq_forward 工具完成。


def _filter_attachments_by_kind(
    attachments: List[Dict[str, Any]],
    *,
    include_images: bool,
    include_files: bool,
) -> List[Dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for att in attachments or []:
        if not isinstance(att, dict):
            continue
        kind = str(att.get("type") or "")
        if kind == "image" and include_images:
            selected.append(dict(att))
        elif kind == "file" and include_files:
            selected.append(dict(att))
        elif include_images and include_files:
            selected.append(dict(att))
    return selected


async def _maybe_handle_proactive_command(
    *,
    bot: Bot,
    event: Event,
    user_text: str,
    conversation_key: str,
    current_attachments: List[Dict[str, Any]],
) -> str | None:
    """处理管理员主动私聊/群聊发送、文件发送、合并转发等显式命令请求。

    注意：这里只处理带明确命令前缀（如“合并转发/私信/群发/转发到”）的结构化命令。
    自然语言转发需求（例如“把上面聊到的 xxx 转发给我”）不在此拦截，而是原样交给
    AI（qq.orchestrator 节点）分析，由其调用 qq_forward 工具挑选并转发对应条目，
    避免与“让 QQ Bot 分析哪些条目需要转发”的初衷冲突。
    """
    command = _parse_proactive_command(user_text)
    if command is None:
        return None
    if not _can("proactive", event):
        # 不向没有这项能力的人暴露“主动发送/转发”能力、目标列表或命令名称。
        return _capability_denial("proactive", event, "该请求不可用。")
    requester = _requester(event)
    action = command.get("action") or ""
    if action == "help":
        return _proactive_help_text()
    if action == "list":
        if not capability.may_see_target_roster(requester):
            # 目标清单里是 bot 待过的每个群和能私聊的每个人。群主的权限只到自己那个群，
            # 这份清单本身就超出了他该知道的范围。
            return "你只能向本群发送，没有可选目标清单。"
        return await _proactive_target_list_text(bot, command.get("target_type") or "")

    target_type = command.get("target_type") or ""
    target, error = await _resolve_proactive_target(
        bot, requester, target_type, command.get("target_ref") or "",
        capability_key="proactive",
    )
    if target is None:
        return error or "目标解析失败。"
    scope_denied = _target_scope_denied("proactive", requester, target)
    if scope_denied:
        return scope_denied
    send_target = _target_to_send_dict(target)
    label = _target_display_label(target.target_type, target.label)
    request_id = str(getattr(event, "message_id", "") or uuid.uuid4().hex)
    send_context = _request_send_context(
        "proactive", f"{conversation_key}:{request_id}", conversation_key,
    )

    if action == "send":
        body = str(command.get("body") or "").strip()
        attachments = current_attachments or []
        if not body and not attachments:
            return "发送内容为空。"
        await _send_text_and_attachments(
            bot, send_target, body, attachments, send_context=send_context,
        )
        return f"已发送到{label}。"

    if action == "file":
        attachment, file_error = _parse_file_send_body(command.get("body") or "")
        if attachment is None:
            return file_error
        await _send_attachments(
            bot, send_target, [attachment], send_context=send_context,
        )
        return f"已向{label}发送文件：{attachment.get('name') or attachment.get('path')}"

    if action == "forward":
        body = str(command.get("body") or "").strip()
        payload_attachments = current_attachments or []
        nodes: list[dict[str, Any]] = []
        if body:
            chunks = [chunk.strip() for chunk in re.split(r"\n\s*---\s*\n", body) if chunk.strip()]
            for chunk in chunks or [body]:
                node = _make_forward_node(bot, chunk, payload_attachments if not nodes else None)
                if node:
                    nodes.append(node)
        if not nodes:
            nodes = await _forward_nodes_from_reply(bot, event, conversation_key)
        if not nodes and payload_attachments:
            node = _make_forward_node(bot, "", payload_attachments)
            if node:
                nodes.append(node)
        if not nodes:
            return "没有可转发内容。请提供文本，或引用一条消息/合并转发卡片。"
        card_images = _filter_attachments_by_kind(
            payload_attachments, include_images=True, include_files=False,
        ) if payload_attachments else []
        try:
            await _send_forward_nodes(
                bot, target, nodes, send_context=send_context,
                image_attachments=card_images,
            )
        except Exception as exc:
            logger.warning("send forward message failed: %s", exc, exc_info=True)
            return "合并转发发送失败：当前 OneBot/NapCat 可能不支持该接口，或目标不可达。"
        return f"已向{label}发送合并转发（{len(nodes)} 条 node）。"

    return None



# ---------------------------------------------------------------------------
#  管理员命令：/清除群记忆 —— 清空指定 QQ 群的长期 memory namespace
# ---------------------------------------------------------------------------

# 命令别名：群聊里直接清当前群；私聊里列可清理群名或按群名清指定群。
_CLEAR_GROUP_MEMORY_RE = _cmd_re(
    ("清除群记忆", "清空群记忆", "清理群记忆", "清除群聊记忆", "清空群聊记忆", "清理群聊记忆"), _CMD_REST,
)


def _parse_clear_group_memory_command(text: str) -> Optional[str]:
    """识别 /清除群记忆 命令。返回目标群引用（可为空字符串表示当前群/需列表）。

    返回 None 表示不是本命令。返回 "" 表示命令无参数；返回非空字符串表示
    管理员显式指定了群名/群号。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    match = _CLEAR_GROUP_MEMORY_RE.match(raw)
    if match is None:
        return None
    return match.group(1).strip()


def _conversation_memory_namespace_for_group(group_id: int) -> str:
    """计算 QQ 群对应的 memory namespace 目录名。

    落盘用的是稳定哈希 key 的摘要，不是真实群号的摘要：engine 侧收到的
    conversation_key 已经被 _stable_conversation_key 换成 qq_group:<digest>，
    knowledge_inject 再对它做一次 SHA256。少算这一层就会去删一个从不存在的目录。
    这里直接调 _conversation_digest 而非 _stable_conversation_key，后者会顺带
    给群分配匿名别名。
    """
    stable_key = f"qq_group:{_conversation_digest(f'qq_group:{int(group_id)}')}"
    digest = hashlib.sha256(stable_key.encode("utf-8")).hexdigest()[:24]
    return f"conv_{digest}"


def _clear_group_memory_namespace(group_id: int) -> tuple[bool, int]:
    """删除指定群的 memory namespace 目录及其命中缓存条目。

    返回 (是否存在过目录, 删除的 memory book 文件数)。删除后只摘掉这个 namespace
    的命中记录，不再整份删掉全局 .hit_cache.json 抹平其他群的命中。
    """
    namespace = _conversation_memory_namespace_for_group(group_id)
    mem_root = Path(CLONOTH_WORKSPACE) / "data" / "memory"
    ns_dir = mem_root / namespace
    deleted_books = 0
    existed = ns_dir.exists() and ns_dir.is_dir()
    if existed:
        try:
            deleted_books = sum(1 for _ in ns_dir.glob("*.yaml"))
        except Exception:
            deleted_books = 0
        import shutil as _shutil
        try:
            _shutil.rmtree(ns_dir)
        except Exception:
            logger.exception("remove memory namespace failed: %s", ns_dir)
            existed = False
    # engine 侧的 catalog 缓存靠它自己 2 秒的 TTL 过期。这里不能 import 它来 invalidate：
    # engine worker 是独立进程，import 只会在 bot 进程里新建一份空缓存对象，
    # 操作它等于什么都没做（而且会把整条 engine 依赖链拉进 NoneBot 进程）。
    _prune_hit_cache_for_namespace(namespace)
    return existed, deleted_books


def _prune_hit_cache_for_namespace(namespace: str) -> None:
    """删掉这个 namespace 在 .hit_cache.json 里的命中记录。

    命中缓存是全局一个文件，整份删掉会把其他群的「最近命中」一起抹平，
    之后 14 天扫除只能按 updated_at 判龄，老而常被召回的条目会被误删。
    """
    try:
        prune_namespace_hits(Path(CLONOTH_WORKSPACE), namespace)
    except Exception:
        logger.debug("prune hit cache failed", exc_info=True)


async def _clearable_group_targets(bot: Bot) -> list[ProactiveTarget]:
    """列出管理员可清理记忆的群（复用主动群目标候选逻辑）。"""
    return await _group_target_candidates(bot)


async def _clear_group_memory_list_text(bot: Bot) -> str:
    """生成私聊场景下"可清理群记忆"的群名列表提示。"""
    groups = await _clearable_group_targets(bot)
    names = [_sanitize_name(t.label, max_len=30) for t in groups if t.label]
    body = "、".join(names[:80]) if names else "（无；需配置 CLONOTH_ALLOWED_GROUPS 或 get_group_list 可用）"
    return (
        "【可清理群记忆的群】\n"
        + body
        + "\n\n用法：/清除群记忆 <群名>\n"
        "例如：/清除群记忆 " + (names[0] if names else "某群")
        + "\n提示：群名可用 get_group_list 中的群名或配置别名；也可用 /清除群记忆 群号"
    )


async def _maybe_handle_clear_group_memory_command(
    *,
    bot: Bot,
    event: Event,
    user_text: str,
) -> str | None:
    """处理 /清除群记忆 命令。

    - 群聊中无参数：清空当前群记忆。
    - 私聊中无参数：返回可清理群列表 + 用法。
    - 任意场景带参数（群名/群号）：解析目标群并清空其记忆。
    没有这项能力的人一律拒绝，且不暴露命令能力细节。
    """
    target_ref = _parse_clear_group_memory_command(user_text)
    if target_ref is None:
        return None
    if not _can("clear_memory", event):
        return _capability_denial("clear_memory", event, "该请求不可用。")
    requester = _requester(event)

    # 无参数：群聊直接清当前群；私聊给出可清理列表。
    if not target_ref:
        group_id = _as_qq_id(getattr(event, "group_id", None))
        if group_id is not None:
            existed, count = _clear_group_memory_namespace(group_id)
            if existed:
                return f"已清空当前群的记忆（删除 {count} 个记忆本）。"
            return "当前群没有可清理的记忆（记忆目录为空或从未生成）。"
        scope_denied = capability.scope_deny_reason("clear_memory", requester, None)
        if scope_denied:
            # 私聊里没有「本群」，群主的作用域为空；这份清单也不该给他看。
            return scope_denied
        return await _clear_group_memory_list_text(bot)

    # 带参数：解析目标群（复用主动发送的目标解析，支持群名/别名/显式群号）。
    target, error = await _resolve_proactive_target(
        bot, requester, "group", target_ref, capability_key="clear_memory",
    )
    if target is None:
        # 附带可清理群列表，方便管理员纠正群名。名单之外的人拿不到这份清单。
        if not capability.may_see_target_roster(requester):
            return error or "目标群解析失败。"
        list_hint = await _clear_group_memory_list_text(bot)
        return (error or "目标群解析失败。") + "\n\n" + list_hint
    scope_denied = _target_scope_denied("clear_memory", requester, target)
    if scope_denied:
        return scope_denied
    existed, count = _clear_group_memory_namespace(int(target.target_id))
    label = _sanitize_name(target.label, max_len=40)
    if existed:
        return f"已清空群「{label}」的记忆（删除 {count} 个记忆本）。"
    return f"群「{label}」没有可清理的记忆（记忆目录为空或从未生成）。"


# ---------------------------------------------------------------------------
#  管理员命令：/整理记忆 —— 手动触发一次 dream
# ---------------------------------------------------------------------------

_DREAM_RE = _cmd_re(("整理记忆", "记忆整理", "整理一下记忆", "dream"))


async def _maybe_handle_dream_command(
    *,
    event: Event,
    user_text: str,
    conversation_key: str,
) -> str | None:
    """处理 /整理记忆：手动跑一次记忆整理。

    整理要几分钟，这里只等到「已受理」；摘要由引擎跑完后推回本会话。
    """
    if not _DREAM_RE.match((user_text or "").strip()):
        return None
    if not _can("dream", event):
        return _capability_denial("dream", event, "该请求不可用。")
    if _client is None:
        return "Clonoth Agent 尚未初始化，请稍后重试。"

    try:
        out = await _client.run_dream_now(notify_conversation_key=conversation_key)
    except Exception as exc:
        logger.warning("trigger dream failed: %s", exc, exc_info=True)
        return f"❌ 触发整理失败：{exc}"

    status = str((out or {}).get("status") or "")
    if status == "started":
        return "已开始整理记忆，跑完把摘要发这儿。通常要几分钟。"
    if status == "busy":
        return "上一轮整理还在跑，等它结束再来。"
    return "引擎没接住这次触发（任务通道不可用），看一下服务状态。"


# ---------------------------------------------------------------------------
#  管理员命令：/发送死信 —— 查看并放行 ambiguous 投递死信
# ---------------------------------------------------------------------------

_DEAD_LETTER_RE = _cmd_re(("发送死信", "投递死信"), _CMD_REST)
_DEAD_LETTER_CLEAR_RE = re.compile(r"^(?:清理|清除|放行)\s*(\S+)$")
_DEAD_LETTER_ALL_WORDS = frozenset({"全部", "所有", "all"})
_DEAD_LETTER_LIST_LIMIT = 20


def _parse_dead_letter_command(text: str) -> tuple[str, str] | None:
    """解析 /发送死信 命令，返回 (动作, 参数)；不是本命令返回 None。"""
    match = _DEAD_LETTER_RE.match(str(text or "").strip())
    if not match:
        return None
    tail = match.group(1).strip()
    if not tail:
        return ("list", "")
    clear_match = _DEAD_LETTER_CLEAR_RE.match(tail)
    if not clear_match:
        # 「发送死信吧」这类尾串不是编号，回用法提示而不是拿它去查记录。
        return ("unknown", tail)
    arg = clear_match.group(1).strip()
    if arg in _DEAD_LETTER_ALL_WORDS:
        return ("clear_all", "")
    return ("clear", arg)


def _dead_letter_target_label(key: str) -> str:
    """把幂等 key 还原成人类可读的目标标签，不查 bot API（NapCat 断线也要能打印）。"""
    target = target_from_idempotency_key(key)
    if target.startswith("group:"):
        return f"群{target.split(':', 1)[1]}"
    if target.startswith("private:"):
        return f"私聊{target.split(':', 1)[1]}"
    return "未知目标"


def _format_dead_letter_lines(claims: list[AmbiguousClaim]) -> str:
    lines = []
    for claim in claims:
        when = dt.datetime.fromtimestamp(claim.updated).strftime("%m-%d %H:%M")
        lines.append(f"{claim.handle} {_dead_letter_target_label(claim.key)} {when} {claim.last_error[:60]}")
    return "\n".join(lines)


async def _maybe_handle_dead_letter_command(*, event: Event, user_text: str) -> str | None:
    """处理 /发送死信 命令：无参数列出死信，带「清理 <编号>」放行一条。

    只挂在私聊入口：清单里带真实群号，群里打出来等于把别的群号广播给群成员。
    """
    parsed = _parse_dead_letter_command(user_text)
    if parsed is None:
        return None
    if not _can("send_dead_letter", event):
        return _capability_denial("send_dead_letter", event, "该请求不可用。")
    action, arg = parsed
    if action == "unknown":
        return "用法：/发送死信 查看卡住的投递；/发送死信 清理 <编号> 放行一条；/发送死信 清理 全部。"
    if action == "list":
        claims = await _outbound_idempotency.ambiguous_claims(limit=_DEAD_LETTER_LIST_LIMIT)
        total = await _outbound_idempotency.ambiguous_count()
        if not claims:
            return "当前没有卡住的投递记录。"
        header = f"【卡住的投递（共 {total} 条，显示前 {_DEAD_LETTER_LIST_LIMIT} 条）】"
        usage = (
            "放行：/发送死信 清理 <编号>；全部放行：/发送死信 清理 全部\n"
            "放行等于承认这条消息平台可能已经收到过，之后同样内容再发一次就会重复。"
        )
        return f"{header}\n{_format_dead_letter_lines(claims)}\n\n{usage}"
    if action == "clear_all":
        count = await _outbound_idempotency.clear_all_ambiguous()
        logger.warning(
            "onebot_dead_letter_cleared",
            extra={"idempotency_key": "*", "operator": str(getattr(event, "user_id", "")), "count": count},
        )
        return f"已放行 {count} 条。其中任意一条都可能已经送到过，重发会重复。"
    candidates = [
        claim for claim in await _outbound_idempotency.ambiguous_claims(limit=0)
        if claim.handle.startswith(arg)
    ]
    if not candidates:
        return "没有这个编号的记录，可能已到期自动放行。"
    if len(candidates) > 1:
        return "编号不唯一，请多输几位。"
    claim = candidates[0]
    if not await _outbound_idempotency.clear_ambiguous(claim.key):
        return "没有这个编号的记录，可能已到期自动放行。"
    logger.warning(
        "onebot_dead_letter_cleared",
        extra={"idempotency_key": claim.key, "operator": str(getattr(event, "user_id", ""))},
    )
    label = _dead_letter_target_label(claim.key)
    return f"已放行 {claim.handle}（{label}）。这条消息平台可能已经收到过，同样内容再发一次就会重复。"


def _append_group_history(group_id: int, line: str) -> int:
    """把一行写入群历史缓存并返回它的序号。所有写入点都必须走这里。"""
    gid = int(group_id)
    seq = _group_history_seq[gid] + 1
    _group_history_seq[gid] = seq
    lines = _group_history[gid]
    _evict_delivered_history(gid, lines)
    if lines.maxlen and len(lines) == lines.maxlen:
        # group_history_max 允许配 0（关掉群历史），那不是缺口，所以先看 maxlen。
        _note_history_gap(gid, lines[0].seq)
    lines.append(GroupHistoryLine(seq=seq, text=line))
    return seq


def _evict_delivered_history(group_id: int, lines: Deque["GroupHistoryLine"]) -> None:
    """软上限之上的空间只留给还没送到 engine 的行，已送达的行到点就让位。"""
    soft_cap = max(int(live.group_history_max), 0)
    watermark = _group_history_watermark(group_id)
    while lines and len(lines) >= soft_cap and lines[0].seq <= watermark:
        lines.popleft()


def _note_history_gap(group_id: int, dropped_seq: int) -> None:
    """记下一条因为缓存满而被丢掉的行；只有还没送到 engine 的行才算缺口。"""
    gid = int(group_id)
    if int(dropped_seq) <= _group_history_watermark(gid):
        return
    if gid not in _group_history_gap:
        logger.warning("QQ group history overflow: group=%s dropped undelivered seq=%s", gid, dropped_seq)
    _group_history_gap[gid] = max(_group_history_gap.get(gid, 0), int(dropped_seq))


def _group_history_lost(group_id: int, watermark: int) -> int:
    """缓存上限吃掉了几条还没送出去的行。

    seq 从 1 开始逐 1 递增，缺的是 (watermark, gap] 这一段；水位 -1（什么都没送出去）
    要按 0 算，否则第一行会被多数一条。
    """
    return max(0, _group_history_gap.get(int(group_id), 0) - max(int(watermark), 0))


def _group_history_watermark(group_id: int) -> int:
    """engine 侧已确认收到的最大群历史序号；-1 表示这一轮要带完整历史。"""
    if _session_state is None:
        return -1
    return _session_state.get_high_watermark(int(group_id))


def _reset_group_history_watermark(group_id: int) -> None:
    """重置水位，让下一轮重新带完整历史。

    SDK 的 reset 路径（event_router._handle_context_reset）按 `prefix:channel_id`
    解析 conv_key，而 QQ 用的是 conv_<sha256> 哈希，解析不出群号，那条路径对 QQ 恒不生效。
    """
    if _session_state is not None:
        _session_state.reset_channel_watermark(int(group_id))
    # 缺口是按旧水位算出来的差值，水位一被重定义它就不再成立。
    _group_history_gap.pop(int(group_id), None)


def _note_inbound_seq(seq: Any) -> None:
    """记下见过的最大 inbound 序号。清空上下文时拿它划界。"""
    global _last_inbound_seq
    value = int(seq or 0)
    if value > _last_inbound_seq:
        _last_inbound_seq = value


def _raise_context_clear_barrier(group_id: int, cleaned_triggers: Any = None) -> None:
    """把清空时刻记成一道门槛，序号不高于它的投递都算上一段上下文的产物。"""
    seqs = [
        int(getattr(trigger, "inbound_seq", 0) or 0)
        for trigger in (cleaned_triggers or [])
    ]
    _context_clear_barrier[int(group_id)] = max([_last_inbound_seq, *seqs])


def _is_stale_delivery(group_id: Any, send_context: Any) -> bool:
    """这次投递是不是清空之前那段上下文的产物。"""
    if group_id is None:
        return False
    barrier = _context_clear_barrier.get(int(group_id), 0)
    if not barrier:
        return False
    seq = int(getattr(send_context, "source_inbound_seq", 0) or 0)
    # 取不到来源序号就放行：宁可多留一条，也不能把正常回复从群历史里抹掉。
    return bool(seq) and seq <= barrier


def _purge_conversation_side_state(conversation_key: str, target: Dict[str, Any]) -> None:
    """清掉一个会话散落在 bot 进程与附件目录里的上下文副本，让它真正从零重新积累。

    只在 clear 语义下调用 —— 漏掉任何一份，重置后它都会把旧上下文重新喂回模型。
    """
    _recent_images.pop(conversation_key, None)
    _recent_files.pop(conversation_key, None)
    _sticky_subjects.pop(conversation_key, None)
    bucket = _sent_attachment_bucket_key(target) or f"conv:{conversation_key}"
    _recent_sent_attachments.pop(bucket, None)
    _sent_attachment_seq.pop(bucket, None)
    group_id = target.get("group_id")
    if target.get("type") == "group" and group_id is not None:
        _group_history.pop(int(group_id), None)
        # qq_forward 的转发候选是群历史的第二份副本，只清前者等于没清。
        _group_content_records.pop(int(group_id), None)
        # 清了会话还让它继续静默没有道理：运营者刚说「重新开始」，下一句就该有人应。
        _trigger_cooldown.forget_group(int(group_id))
    _remove_conversation_attachment_dir(conversation_key)


def _remove_conversation_attachment_dir(conversation_key: str) -> None:
    """删掉这个会话的入站附件目录。历史一清，里面的路径就再没有东西引用得到。"""
    name = str(conversation_key or "").replace(":", "_")
    # 前缀校验挡住异常 key：conversation_key 为空时拼出的会是 data/attachments 本身。
    if not name.startswith(_QQ_ATTACHMENT_DIR_PREFIXES):
        return
    att_dir = Path(CLONOTH_WORKSPACE) / "data" / "attachments" / name
    if not att_dir.is_dir():
        return
    import shutil as _shutil
    try:
        _shutil.rmtree(att_dir)
    except OSError:
        logger.warning("remove conversation attachment dir failed: %s", att_dir, exc_info=True)


def _compose_history_line(
    *,
    timestamp: Any,
    sender: Any,
    sender_id: Any,
    text: str,
    name_override: str = "",
    alias_override: str = "",
) -> str:
    """群历史行的唯一拼装点：时间、显示名、匿名 ID、正文的匿名化与压缩都在这里做。

    匿名消息的显示名/别名由调用方预解析后传入，避免占位号被当成一个真实用户。
    """
    safe_text = _anonymize_text_for_ai(_compact_text(text))
    name = _anonymize_text_for_ai(name_override or _sender_display_name(sender, sender_id))
    alias = alias_override or _anonymize_user_id(sender_id)
    return f"[{_format_hhmm(timestamp)}] {name}({alias}): {safe_text}"


def _format_history_line(event: GroupMessageEvent, bot: Bot, override_text: str = "") -> str:
    """把群消息格式化为 tangqiu_main 提示词要求的历史行，并匿名化 QQ ID。"""
    return _compose_history_line(
        timestamp=getattr(event, "time", None),
        sender=getattr(event, "sender", None),
        sender_id=event.user_id,
        text=override_text or _message_to_text(event.get_message(), getattr(bot, "self_id", None)),
        name_override=_event_display_name(event),
        alias_override=_event_user_alias(event),
    )


def _append_group_record(
    group_id: int,
    line: str,
    *,
    text: str,
    sender_name: str,
    sender_id: str,
    timestamp: float,
    message_id: str = "",
    attachments: List[Dict[str, Any]] | None = None,
) -> int:
    """一条群历史同时进水位队列和结构化副本；两边缺一个，转发筛选就会看不到这条。

    返回这一行在群历史里的序号。
    """
    gid = int(group_id)
    seq = _append_group_history(gid, line)
    _group_content_records[gid].append(GroupContentRecord(
        formatted_line=line,
        text=text,
        sender_name=sender_name,
        sender_id=sender_id,
        timestamp=timestamp,
        message_id=message_id,
        seq=seq,
        attachments=[dict(att) for att in (attachments or []) if isinstance(att, dict)],
    ))
    return seq


def _record_group_message(
    event: GroupMessageEvent,
    bot: Bot,
    override_text: str = "",
    attachments: List[Dict[str, Any]] | None = None,
) -> int:
    """记录群最近消息；@Bot 触发消息由 Agent matcher 手动记录，避免被 block 跳过。

    返回这条消息在群历史里的序号，0 = 正文为空没有记。
    """
    text = override_text or _message_to_text(event.get_message(), getattr(bot, "self_id", None))
    if text.strip():
        # 转发 node 的 user_id 要真实号；匿名占位号发不出去，落库留空由 self_id 兜底。
        anonymous = bool(_anonymous_identity(event))
        sender_id = "" if anonymous else str(getattr(event, "user_id", "") or "")
        return _append_group_record(
            int(event.group_id),
            _format_history_line(event, bot, override_text=text),
            text=_compact_text(text, limit=max(live.history_text_limit, 1200)),
            sender_name=_event_display_name(event),
            sender_id=sender_id,
            timestamp=float(getattr(event, "time", None) or time.time()),
            message_id=str(getattr(event, "message_id", "") or ""),
            attachments=attachments,
        )
    return 0


def _record_bot_reply(group_id: int, text: str) -> None:
    """把 Bot 最终回复写回群历史，保持后续对话连续性。"""
    if not text:
        return
    text = strip_output_markers(
        text,
        strip_asterisk_styles=live.strip_asterisk_styles,
        strip_underscore_styles=live.strip_underscore_styles,
    ).replace(_SPLIT_SIGNAL, " ")
    text = _QQ_EMOJI_MARK_RE.sub(lambda m: f"[表情:{m.group(1)}]", text)
    text = _anonymize_text_for_ai(_compact_text(text))
    if text:
        # Bot 自己的回复同样进历史块：它在 durable history 里已有一条 assistant 消息，
        # 这里是第二份，但 compact 重置水位后重发的那 20 行需要完整时序，不能只有别人的话。
        # Bot 行没有发送者身份，不走 _compose_history_line。
        _append_group_record(
            int(group_id),
            f"[{_format_hhmm(None)}] Bot: {text}",
            text=text,
            sender_name="Bot",
            sender_id="",
            timestamp=time.time(),
        )


async def _build_reply_context(event: Event, bot: Bot, conversation_key: str) -> tuple[str, List[Dict[str, Any]]]:
    """增强引用消息解析：event.reply → 本地缓存 → NapCat get_msg，并采集引用图片附件。"""
    reply_message_id = _extract_reply_message_id(
        event.get_message() if hasattr(event, "get_message") else None,
        getattr(event, "raw_message", None),
    )
    reply = await _resolve_reply_payload(bot, event, reply_message_id)

    if not reply:
        if reply_message_id is not None:
            return _anonymize_text_for_ai(f"（无法获取引用消息内容：message_id={reply_message_id}）"), []
        return "", []

    message = reply.get("message")
    raw_message = reply.get("raw_message")
    text = await _message_to_text_with_forward(bot, message, getattr(bot, "self_id", None))
    if not text:
        text = await _message_to_text_with_forward(bot, raw_message, getattr(bot, "self_id", None))

    # 判断引用的消息是否为 Bot 自己发出的。QQ Bot 的回复里可能带表情包图片，
    # 用户引用这类回复时不应该把 Bot 自己的图片下载成附件再送去识图——那既浪费
    # 算力，也会让模型误以为用户在要求识别一张图片。这里仅当被引用消息不是 Bot
    # 自身发出时才采集图片附件，否则保留 [图片] 文本占位符。
    reply_sender = reply.get("sender") if isinstance(reply.get("sender"), dict) else None
    reply_sender_qq = ""
    if reply_sender is not None:
        reply_sender_qq = str(reply_sender.get("user_id") or reply.get("user_id") or "")
    else:
        reply_sender_qq = str(reply.get("user_id") or "")
    bot_ids = _bot_self_id_candidates(event, bot)
    reply_from_bot = _qq_matches_bot(reply_sender_qq, bot_ids)

    if reply_from_bot:
        # Bot 自己的回复：默认不下载图片附件，避免把表情包送去识图。
        # 但若这条消息是 Bot 发出的真实附件（如绘图产物，已在发送时登记
        # message_id -> 本地路径），则把原图作为附件送给 AI，支持“引用该图继续改”。
        quoted_attachments = []
        bot_msg_atts = _message_attachment_records(reply_message_id, only_images=True)
        if not bot_msg_atts:
            # Text-only Bot replies are bound to the source images of their task
            # when sent. This persistent cache lets a user's follow-up reply keep
            # the original image even after adapter restart.
            bot_msg_atts = _cached_reply_image_attachments(reply_message_id, conversation_key)
        # 合并转发把整批图绑到一个卡片 message_id，引用卡片时按每轮上限截断，避免一次喂几张。
        bot_msg_atts = bot_msg_atts[: live.max_images_per_turn]
        if bot_msg_atts:
            for att in bot_msg_atts:
                rel_path = _to_workspace_rel_path(att["path"])
                quoted_attachments.append({
                    "type": "image",
                    "path": rel_path,
                    "name": att.get("name", "") or Path(att["path"]).name,
                    "source": "onebot_bot_reply",
                })
                text = text.replace(IMAGE_PLACEHOLDER, f"[{_attachment_label(att)}: {rel_path}]", 1)
            # 剩余未匹配的图片占位符（如同消息里的表情包）仍标为表情包。
            text = text.replace(IMAGE_PLACEHOLDER, STICKER_PLACEHOLDER)
        else:
            text = text.replace(IMAGE_PLACEHOLDER, STICKER_PLACEHOLDER)
    else:
        images, files = await _collect_message_media(bot, message)
        if not images and not files:
            images, files = await _collect_message_media(bot, raw_message)
        quoted_attachments, quoted_errors = await _image_sources_to_attachments(images, conversation_key)
        for att in quoted_attachments:
            text = text.replace(IMAGE_PLACEHOLDER, f"[{_attachment_label(att)}: {att['path']}]", 1)
        # 引用一条群文件消息是最明确的「读这个」，而群文件带不了 @，只能这样指。
        # 正文里没有占位符可替，直接挂到附件上让 engine 去读内容。
        if files and live.enable_file_input:
            file_atts, file_errors = await _file_sources_to_attachments(files, conversation_key)
            quoted_attachments.extend(file_atts)
            quoted_errors.extend(file_errors)
        # 下载失败的图（太大/格式不支持）把占位符换成原因，否则引用块里会静默少一张图。
        for note in dict.fromkeys(quoted_errors):
            text = text.replace(IMAGE_PLACEHOLDER, f"[{note}]", 1)

    text = _anonymize_text_for_ai(_compact_text(text))
    if not text:
        if reply_message_id is not None:
            return _anonymize_text_for_ai(f"（引用消息为空或暂不支持的消息类型：message_id={reply_message_id}）"), quoted_attachments
        return "", quoted_attachments

    sender = reply.get("sender")
    sender_id = ""
    if isinstance(sender, dict):
        sender_id = str(sender.get("user_id") or reply.get("user_id") or "")
        name_raw = _sanitize_name(str(sender.get("card") or sender.get("nickname") or sender_id or "原作者"))
    elif sender is not None:
        sender_id = str(getattr(sender, "user_id", "") or "")
        name_raw = _sender_display_name(sender, sender_id)
    else:
        sender_id = str(reply.get("user_id") or "")
        name_raw = _sanitize_name(sender_id or "原作者")
    name = _anonymize_text_for_ai(name_raw)
    user = _anonymize_user_id(sender_id) if sender_id else "UserUnknown"
    return f"[{_format_hhmm(reply.get('time'))}] {name}({user}): {text}", quoted_attachments


def _alias_from_index(prefix: str, index: int) -> str:
    letters = ""
    index = max(0, index)
    while True:
        index, rem = divmod(index, 26)
        letters = chr(ord("A") + rem) + letters
        if index == 0:
            break
        index -= 1
    return f"{prefix}{letters}"


def _mark_anon_map_dirty() -> None:
    """标记匿名映射需要回写；带最小写盘间隔（默认 5s）的节流合并落盘。

    2026-07-09 修改原因：原实现每次新登记都 create_task 一次全量写盘，新用户
    集中涌入时会短时间多次写盘。改为节流：距上次落盘不足 _ANON_MAP_SAVE_MIN_INTERVAL
    时，只调度一个延迟 flush task（已存在则复用），把这段时间内的多次登记合并成一次写盘。
    关闭时的显式 flush 仍会强制写入最新别名。
    """
    global _anon_map_dirty, _anon_map_save_task
    _anon_map_dirty = True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 没有运行中的事件循环（如同步测试上下文），保持 dirty，由下次显式保存处理。
        return
    # 已有等待中的 flush task 时直接复用，避免重复调度。
    if _anon_map_save_task is not None and not _anon_map_save_task.done():
        return
    _anon_map_save_task = loop.create_task(_anon_map_save_throttled())


async def _anon_map_save_throttled() -> None:
    """trailing 节流：触发后固定等待一个写盘窗口再写，窗口内的多次 dirty 合并为一次。

    为何固定等待而不是“距上次写盘”计算：后者在首次/间隔已满足时会立即写，
    导致高频场景下每隔一个间隔就写一次；固定等待一个窗口能保证“窗口内只写一次”。
    """
    global _anon_map_save_task
    try:
        if _ANON_MAP_SAVE_MIN_INTERVAL > 0:
            await asyncio.sleep(_ANON_MAP_SAVE_MIN_INTERVAL)
        await _save_anon_map()
    finally:
        _anon_map_save_task = None


def _coerce_last_seen(value: Any) -> float:
    """读入持久化的 last_seen；缺失或非法按当前时间，避免刚加载就被 TTL 判成过期。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return time.time()


def _load_anon_map() -> None:
    """以文件为准恢复 _anon_users/_anon_groups、反向表及“下一个编号”计数器。

    2026-07-09 新增：匿名别名映射持久化，保障跨重启同一人/群别名一致与可反解。
    加载优先从持久化的 next 计数器恢复；若旧文件无该字段，则根据已有别名推断。
    """
    global _anon_user_next, _anon_group_next
    path = Path(ANON_MAP_FILE)
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("failed to load OneBot anon map: %s", path, exc_info=True)
        return
    if not isinstance(data, dict):
        return

    max_user_idx = -1
    skipped_dirty = 0
    users = data.get("users")
    if isinstance(users, dict):
        for real, item in users.items():
            real = str(real or "").strip()
            if not real or not isinstance(item, dict):
                continue
            alias = str(item.get("alias") or "").strip()
            if not alias:
                continue
            # 跳过历史脏数据：明显非法的短数字 QQ 号（如 "1"）与群匿名占位号不再载入，
            # 并标脏触发下次写盘自清理（避免 @UserAP、折叠匿名成员这类脏别名回流群里）。
            if real in _ANONYMOUS_PLACEHOLDER_QQ_IDS or (real.isdigit() and not _is_plausible_qq_id(real)):
                skipped_dirty += 1
                continue
            _anon_users[real] = alias
            _anon_user_reverse[alias] = real
            _anon_user_last_seen[real] = _coerce_last_seen(item.get("last_seen"))
            max_user_idx = max(max_user_idx, _alias_to_index("User", alias))
    if skipped_dirty:
        _mark_anon_map_dirty()
        logger.warning("dropped %d invalid short-qq entries from anon map", skipped_dirty)

    max_group_idx = -1
    groups = data.get("groups")
    if isinstance(groups, dict):
        for real, item in groups.items():
            real = str(real or "").strip()
            if not real or not isinstance(item, dict):
                continue
            alias = str(item.get("alias") or "").strip()
            if not alias:
                continue
            _anon_groups[real] = alias
            _anon_group_reverse[alias] = real
            _anon_group_last_seen[real] = _coerce_last_seen(item.get("last_seen"))
            max_group_idx = max(max_group_idx, _alias_to_index("Group", alias))

    # 下一个编号：优先用文件里显式保存的 next；否则用“最大已用 index + 1”兼容旧文件。
    try:
        _anon_user_next = max(int(data.get("user_next", 0) or 0), max_user_idx + 1)
    except Exception:
        _anon_user_next = max_user_idx + 1
    try:
        _anon_group_next = max(int(data.get("group_next", 0) or 0), max_group_idx + 1)
    except Exception:
        _anon_group_next = max_group_idx + 1
    _anon_user_next = max(0, _anon_user_next)
    _anon_group_next = max(0, _anon_group_next)
    # 剪枝必须在 next 计数器恢复之后：先剪会让 max_*_idx 变小，旧文件恢复的 next 回退，别名被复用。
    pruned = _prune_anon_map()
    _invalidate_anon_text_pattern()
    logger.info(
        "loaded OneBot anon map from %s (users=%d groups=%d pruned=%d)",
        path, len(_anon_users), len(_anon_groups), pruned,
    )
    if len(_anon_users) + len(_anon_groups) > _ANON_MAP_LARGE_WARN:
        logger.warning(
            "OneBot anon map has %d entries; consider setting ONEBOT_ANON_MAP_RETENTION_DAYS to enable pruning",
            len(_anon_users) + len(_anon_groups),
        )


def _prune_anon_map() -> int:
    """按保留期清理久未出现的别名条目；返回清掉的条数。

    只删映射不回收编号：编号复用会让旧记忆里的 UserA 指向另一个人。
    """
    if ANON_MAP_RETENTION_DAYS <= 0:
        return 0
    cutoff = time.time() - ANON_MAP_RETENTION_DAYS * 86400.0
    # 已建档者必须能反解（_enrolled_subject_display_names 依赖反向表），一律豁免。
    protected = set(memory_subjects.enrolled_subjects(Path(CLONOTH_WORKSPACE)))
    removed = 0
    for store, last_seen, reverse in (
        (_anon_users, _anon_user_last_seen, _anon_user_reverse),
        (_anon_groups, _anon_group_last_seen, _anon_group_reverse),
    ):
        stale = [
            real for real, alias in store.items()
            if alias not in protected and last_seen.get(real, 0.0) < cutoff
        ]
        for real in stale:
            alias = store.pop(real, None)
            last_seen.pop(real, None)
            if alias is not None:
                reverse.pop(alias, None)
            removed += 1
    if removed:
        _mark_anon_map_dirty()
        _invalidate_anon_text_pattern()
    return removed


async def _save_anon_map() -> None:
    """把匿名别名映射原子写入单独文件；复用 _route_state_lock + tmp→replace。"""
    global _anon_map_dirty, _anon_map_last_saved_at
    async with _route_state_lock:
        if not _anon_map_dirty:
            return
        path = Path(ANON_MAP_FILE)
        now = time.time()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "user_next": _anon_user_next,
                "group_next": _anon_group_next,
                "users": {
                    real: {"alias": alias, "last_seen": _anon_user_last_seen.get(real, now)}
                    for real, alias in _anon_users.items()
                },
                "groups": {
                    real: {"alias": alias, "last_seen": _anon_group_last_seen.get(real, now)}
                    for real, alias in _anon_groups.items()
                },
            }
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
            _anon_map_dirty = False
            _anon_map_last_saved_at = time.time()
        except Exception:
            logger.warning("failed to save OneBot anon map: %s", path, exc_info=True)


def _alias_to_index(prefix: str, alias: str) -> int:
    """把 UserA/GroupAB 这类别名反解为 0-based 索引；非法别名返回 -1。"""
    if not alias.startswith(prefix):
        return -1
    letters = alias[len(prefix):]
    if not letters or not letters.isalpha() or not letters.isupper():
        return -1
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - ord("A") + 1)
    return index - 1


# QQ 号至少 5 位（最小靳号也远大于此）；低于此位数的“数字”基本不可能是真实 QQ 号，
# 多半是模型凭空捧造的 at目标（如 qq=1）或把纯数字群名片误当成的 QQ 号。
_MIN_REAL_QQ_LEN = 5


def _is_plausible_qq_id(value: str) -> bool:
    """粗略判断一个字符串是否看上去像真实 QQ 号（纯数字且不至于过短）。"""
    real = str(value or "").strip()
    return real.isdigit() and len(real) >= _MIN_REAL_QQ_LEN


def _touch_anon_seen(store: Dict[str, float], real: str) -> None:
    """刷新某条别名的真实最后出现时间；节流到最小间隔一次，供 TTL 回收判定。"""
    now = time.time()
    if now - store.get(real, 0.0) > _ANON_MAP_TOUCH_MIN_INTERVAL:
        store[real] = now
        _mark_anon_map_dirty()


def _anonymize_user_id(user_id: Any) -> str:
    global _anon_user_next
    real = str(user_id or "").strip()
    if not real:
        return "UserUnknown"
    # 群匿名占位号全群共用，登记会把所有匿名成员折叠成一个人并落盘。
    if real in _ANONYMOUS_PLACEHOLDER_QQ_IDS:
        return _ANONYMOUS_UNKNOWN_ALIAS
    # 不把明显非法的短数字（如 qq=1）登记进永存匿名表，避免脏号占用稳定别名并回流到群里。
    if real.isdigit() and not _is_plausible_qq_id(real):
        return "UserUnknown"
    alias = _anon_users.get(real)
    if alias is None:
        alias = _alias_from_index("User", _anon_user_next)
        _anon_user_next += 1
        _anon_users[real] = alias
        _anon_user_reverse[alias] = real
        _invalidate_anon_text_pattern()
        _mark_anon_map_dirty()
    _touch_anon_seen(_anon_user_last_seen, real)
    return alias


def _anonymize_group_id(group_id: Any) -> str:
    global _anon_group_next
    real = str(group_id or "").strip()
    if not real:
        return "GroupUnknown"
    alias = _anon_groups.get(real)
    if alias is None:
        alias = _alias_from_index("Group", _anon_group_next)
        _anon_group_next += 1
        _anon_groups[real] = alias
        _anon_group_reverse[alias] = real
        _invalidate_anon_text_pattern()
        _mark_anon_map_dirty()
    _touch_anon_seen(_anon_group_last_seen, real)
    return alias


def _enrolled_subject_display_names() -> Dict[str, str]:
    """已建档者的别名 -> 显示名，用于在自由文本里认出「提到了谁」。

    profile 配的名字跨会话稳定，优先；没配就回落到名册里记下的最近显示名 ——
    群名片确实每群不同，但没有它这条召回路径在没配 profile 的部署上完全是死的。
    重名的已经在名册侧剔除，认错人比认不出更糟。
    """
    workspace = Path(CLONOTH_WORKSPACE)
    names = dict(memory_subjects.subject_display_names(workspace))
    for alias in memory_subjects.enrolled_subjects(workspace):
        real = _anon_user_reverse.get(alias)
        if not real:
            continue
        display = _qq_profile_display_name(real)
        if display:
            names[alias] = display
    return names


def _record_memory_subject_interaction(event: Event, direct: bool = True) -> str:
    """主动找过 bot 的人才建档；返回发起人的匿名别名（无论建不建档都返回）。

    建档意味着「以后提到他就全量加载他的档案」，所以门槛是本人主动发起（@ bot /
    触发前缀 / 私聊）。别名照常返回：他若早已建档，本轮仍该加载他的记忆。
    """
    alias = _event_user_alias(event)
    # 匿名身份不可核验且按天轮换，建档等于把不同人的记忆混进同一份档案。
    if direct and alias and alias != "UserUnknown" and not alias.startswith("Anon"):
        display = _event_display_name(event)
        # 没名片时显示名就是别名本身，别名另有匹配路径，存进去只是冗余。
        memory_subjects.record_interaction(
            Path(CLONOTH_WORKSPACE), alias,
            display_name="" if display == alias else display,
        )
    return alias


_SUBJECT_STICKY_TTL_SEC = 300.0
_SUBJECT_STICKY_MAX_KEYS = 512
_sticky_subjects: "OrderedDict[str, tuple[float, List[str]]]" = OrderedDict()


def _sticky_memory_subjects(conversation_key: str, subjects: List[str]) -> List[str]:
    """把上一轮涉及的人并进本轮。

    「@张三」的下一轮通常是「他电话多少」，那句里没有任何别名，只看当前这句会整轮
    召回不到。存回去的始终是本轮实际出现的人，不含继承来的 —— 否则谁进来一次就永久驻留。
    """
    now = time.monotonic()
    prev_at, prev = _sticky_subjects.get(conversation_key, (0.0, []))
    _sticky_subjects[conversation_key] = (now, list(subjects))
    _sticky_subjects.move_to_end(conversation_key)
    while len(_sticky_subjects) > _SUBJECT_STICKY_MAX_KEYS:
        _sticky_subjects.popitem(last=False)
    if now - prev_at > _SUBJECT_STICKY_TTL_SEC:
        return list(subjects)
    merged = list(subjects)
    merged.extend(alias for alias in prev if alias not in merged)
    return merged


def _collect_memory_subjects(
    event: Event, user_text: str, conversation_key: str, direct: bool = True,
) -> List[str]:
    """收集本轮涉及的人的别名，供 engine 按人加载长期记忆。

    传的是匿名别名而非 QQ 号：记忆目录名、注入内容都以别名为准，真实号不出工作区。
    只认发言人自己这句话 —— 群历史每行都带 `名字(UserX):` 前缀，拿整块历史去扫等于把近
    20 行里说过话的人全部拉进来，每人一整份档案。
    """
    sender_alias = _record_memory_subject_interaction(event, direct)
    try:
        message = event.get_message()
    except Exception:
        message = None
    subjects = collect_memory_subjects(
        sender_alias=sender_alias,
        anonymized_text=_anonymize_text_for_ai(user_text),
        at_user_ids=at_segment_user_ids(message),
        alias_of_user_id=_anonymize_user_id,
        display_names=_enrolled_subject_display_names(),
    )
    return _sticky_memory_subjects(conversation_key, subjects)


def _resolve_at_alias_to_real(token: str) -> str:
    """把模型写的 [at:xxx] 里的 xxx 反查为真实 QQ 号。

    2026-07-14 新增：模型看到的是匿名别名（UserA/UserAF）或群昵称/显示名，
    会输出 [at:UserAF] 而非真实 QQ 号。emoji_handler 在遇到非数字 token 时
    回调本函数：
      1. 先按匿名反向表 _anon_user_reverse 精确匹配别名 -> 真实 QQ 号；
      2. 再按已知用户 profile 的显示名/群名片/别名尝试匹配。
    解析不到时返回空串，由 emoji_handler 回退为可读文本。
    """
    raw = str(token or "").strip()
    if not raw:
        return ""
    # 去掉可能带上的 @ 前缀。
    if raw.startswith("@"):
        raw = raw[1:].strip()
    if not raw:
        return ""
    if raw.isdigit():
        return raw
    # 1) 匿名别名精确反查（UserA/UserAF 等）。
    real = _anon_user_reverse.get(raw)
    if real:
        return str(real)
    # 2) 按已知用户 profile 的显示名/别名匹配（忽略大小写与首尾空白）。
    norm = raw.casefold()
    try:
        for uid in list(_QQ_USER_PROFILES.keys()):
            names = [_qq_profile_display_name(uid) or ""]
            names.extend(_profile_aliases_for_user(uid))
            for name in names:
                if name and str(name).strip().casefold() == norm:
                    return str(uid)
    except Exception:
        logger.debug("resolve at alias by profile failed for %r", raw, exc_info=True)
    return ""


_anon_text_pattern: Optional["re.Pattern[str]"] = None
_anon_text_replacements: Dict[str, str] = {}


def _invalidate_anon_text_pattern() -> None:
    global _anon_text_pattern
    _anon_text_pattern = None


def _anon_text_pattern_and_map() -> tuple[Optional["re.Pattern[str]"], Dict[str, str]]:
    """把全部已知真实号编成一条备选正则并缓存。

    逐条 re.sub 在映射表超过 re 模块 512 条编译缓存后每次调用都要重新编译，
    而本函数在每条入站消息上会被调用多次。
    """
    global _anon_text_pattern, _anon_text_replacements
    if _anon_text_pattern is not None:
        return _anon_text_pattern, _anon_text_replacements
    mapping: Dict[str, str] = dict(_anon_users)
    # 群号与 QQ 号字面相同时按群号替换，与旧的"先群后人"两趟顺序保持一致。
    mapping.update(_anon_groups)
    if not mapping:
        return None, {}
    keys = sorted(mapping, key=len, reverse=True)
    _anon_text_replacements = mapping
    _anon_text_pattern = re.compile(
        rf"(?<!\d)(?:{'|'.join(re.escape(key) for key in keys)})(?!\d)"
    )
    return _anon_text_pattern, _anon_text_replacements


def _anonymize_text_for_ai(text: str) -> str:
    """把文本中"系统已知的真实 QQ 号/群号"替换为稳定匿名别名后交给模型。

    2026-07-09 修改原因：原实现用 `_SENSITIVE_ID_RE`（5~12 位数字）兜底匿名化，
    会把金额、验证码、订单号、日期等普通数字误伤成 UserX，导致模型收到失真内容。
    做法改为"已知 ID 精确替换"：只替换 `_anon_groups` / `_anon_users` 映射表里
    真实出现过、并已在采集入口登记的群号/QQ 号；不再对任意数字串做泛匹配。
    目的是在保证"上下文里真实存在的隐私标识仍被匿名"的同时，避免误伤日常数字。
    注意：真正需要匿名的 QQ 号/群号必须在采集入口（发送者、@某人、引用消息、
    历史行等）主动调用 `_anonymize_user_id` / `_anonymize_group_id` 登记，
    这样它们才会进入映射表并在此处被替换。
    """
    if not text:
        return ""
    safe = str(text)
    pattern, mapping = _anon_text_pattern_and_map()
    if pattern is None:
        return safe
    return pattern.sub(lambda m: mapping[m.group(0)], safe)


_CONVERSATION_SECRET, CONVERSATION_DIGEST_SALTED = resolve_secret(
    env_secret=CONVERSATION_HASH_SECRET,
    secret_file=Path(CONVERSATION_HASH_SECRET_FILE),
    workspace=Path(CLONOTH_WORKSPACE),
    route_state_file=Path(ONEBOT_STATE_FILE),
)


# 当前登录的 QQ 号，参与会话键摘要。进程启动时从盘上读回来：等第一条消息才知道
# 账号的话，那之前算出的键会落在空作用域里，记忆静默分叉成两份。
_ACTIVE_BOT_SCOPE: str = _bot_scope.load_scope(Path(CLONOTH_WORKSPACE))


def _active_bot_scope() -> str:
    return _ACTIVE_BOT_SCOPE


def _purge_account_scoped_caches() -> None:
    """丢掉按真实群号缓存、实为上一个账号视角的进程内状态。

    群历史会直接进新号第一条 inbound 的上下文；成员名片、复读判定、冷却窗口也都是
    旧号看见的样子。按会话键分桶的那些换号后自然另起，不在此列。
    """
    _group_history.clear()
    _group_history_seq.clear()
    _group_history_gap.clear()
    _context_clear_barrier.clear()
    # qq_forward 的转发候选是群历史的第二份副本，只清前者等于没清。
    _group_content_records.clear()
    _echo_recent.clear()
    _echo_last.clear()
    _group_member_cache.clear()
    _trigger_cooldown.forget_all()


def _adopt_bot_scope(bot: Any) -> None:
    """记住当前账号；换号时丢掉按旧账号算出的键缓存，让它们按新作用域重算。"""
    global _ACTIVE_BOT_SCOPE
    scope = _bot_scope.normalize(getattr(bot, "self_id", None))
    if not scope or scope == _ACTIVE_BOT_SCOPE:
        return
    previous = _ACTIVE_BOT_SCOPE
    _ACTIVE_BOT_SCOPE = scope
    _bot_scope.save_scope(Path(CLONOTH_WORKSPACE), scope)
    # real→stable 必须重算。反向表留着：旧键的在途回调还要靠它找回真实会话。
    _stable_conversation_keys.clear()
    _purge_account_scoped_caches()
    logger.warning(
        "QQ account changed (%s -> %s): conversations and memory now use a separate namespace",
        previous or "<none>", scope,
    )


def _conversation_digest(conversation_key: str) -> str:
    return _digest_conversation_key(
        conversation_key, _CONVERSATION_SECRET, bot_scope=_active_bot_scope(),
    )


def _scoped_stable_key(real_conversation_key: str) -> str:
    """按当前作用域算稳定键。纯计算，不登记映射也不分配别名。"""
    if real_conversation_key.startswith("qq_group:"):
        prefix = "qq_group"
    elif real_conversation_key.startswith("qq_private:"):
        prefix = "qq_private"
    else:
        prefix = "qq_unknown"
    return f"{prefix}:{_conversation_digest(real_conversation_key)}"


def _current_scope_keys() -> List[str]:
    """已知会话在当前号下的稳定键。

    摘要带 bot 作用域，换号后同一个群算出的键完全不同，两个号的会话于是并排堆在
    控制台里分不出谁是谁。密钥只有本进程有，所以这里正推一遍结果交给 supervisor，
    它拿集合做归类就行，不必知道密钥。
    """
    keys: set[str] = set()
    for real in set(_real_conversation_keys.values()):
        try:
            keys.add(_scoped_stable_key(str(real)))
        except Exception:
            continue
    return sorted(keys)


def _stable_conversation_key(real_conversation_key: str) -> str:
    """把真实 QQ 会话键转换为可持久化的稳定哈希键，避免 Supervisor 泄漏群号/QQ号。"""
    if real_conversation_key.startswith("qq_group:"):
        prefix = "qq_group"
        _anonymize_group_id(real_conversation_key.split(":", 1)[1])
    elif real_conversation_key.startswith("qq_private:"):
        prefix = "qq_private"
        _anonymize_user_id(real_conversation_key.split(":", 1)[1])
    else:
        prefix = "qq_unknown"
    stable = f"{prefix}:{_conversation_digest(real_conversation_key)}"
    _real_conversation_keys[stable] = real_conversation_key
    _stable_conversation_keys[real_conversation_key] = stable
    return stable


def _real_conversation_key(conversation_key: str) -> str:
    return _real_conversation_keys.get(conversation_key, conversation_key)


def _serializable_target(target: Dict[str, Any]) -> Dict[str, Any]:
    """提取可持久化的 QQ 回复目标，避免把 Bot/Event 对象写进状态文件。"""
    allowed = {"type", "group_id", "user_id", "conversation_key"}
    return {key: target[key] for key in allowed if key in target and target[key] is not None}


def _load_route_state() -> None:
    """加载 stable conversation/session 到真实 QQ 目标的本地路由状态。"""
    path = Path(ONEBOT_STATE_FILE)
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("failed to load OneBot route state: %s", path, exc_info=True)
        return
    if not isinstance(data, dict):
        return

    real_map = data.get("real_conversation_keys")
    if isinstance(real_map, dict):
        _real_conversation_keys.update({str(k): str(v) for k, v in real_map.items() if str(k) and str(v)})
        _stable_conversation_keys.update({str(v): str(k) for k, v in _real_conversation_keys.items() if str(k) and str(v)})

    targets = data.get("session_targets")
    if isinstance(targets, dict):
        for sid, target in targets.items():
            if isinstance(target, dict):
                _persisted_session_targets[str(sid)] = _serializable_target(target)

    logger.info("loaded OneBot route state from %s", path)


async def _save_route_state() -> None:
    """保存本地路由状态，保障 bot.py 重启后能把 stable key 映射回真实 QQ 目标。"""
    async with _route_state_lock:
        path = Path(ONEBOT_STATE_FILE)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "real_conversation_keys": dict(_real_conversation_keys),
                "session_targets": {
                    sid: _serializable_target(target)
                    for sid, target in {**_persisted_session_targets, **_session_targets}.items()
                    if isinstance(target, dict)
                },
            }
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            logger.warning("failed to save OneBot route state: %s", path, exc_info=True)


def _load_reply_attachment_cache() -> None:
    """加载独立的引用消息附件索引缓存。"""
    path = Path(REPLY_ATTACHMENT_CACHE_FILE)
    source_path = path
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("failed to load OneBot reply attachment cache: %s", path, exc_info=True)
            return
    else:
        # 兼容拆分前短暂写入 onebot_plugin_state.json 的旧字段；加载后下一次保存会
        # 写入独立 cache 文件，route state 后续保存也不会再包含 reply_attachments。
        legacy_path = Path(ONEBOT_STATE_FILE)
        if not legacy_path.exists():
            return
        try:
            legacy_data = json.loads(legacy_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(legacy_data, dict) or not isinstance(legacy_data.get("reply_attachments"), dict):
            return
        data = {"reply_attachments": legacy_data.get("reply_attachments")}
        source_path = legacy_path
    if not isinstance(data, dict):
        return
    payload = data.get("reply_attachments") if isinstance(data.get("reply_attachments"), dict) else data
    if not isinstance(payload, dict):
        return
    now = time.time()
    cutoff = now - IMAGE_CACHE_TTL_SECONDS
    loaded = 0
    for mid, item in payload.items():
        if not isinstance(item, dict):
            continue
        try:
            created_at = float(item.get("created_at") or 0.0)
        except Exception:
            created_at = 0.0
        attachments = item.get("attachments") if isinstance(item.get("attachments"), list) else []
        if not attachments or (created_at and created_at < cutoff):
            continue
        _reply_attachment_cache[str(mid)] = {
            "conversation_key": str(item.get("conversation_key") or ""),
            "sender_id": str(item.get("sender_id") or ""),
            "created_at": created_at or now,
            "attachments": [dict(att) for att in attachments if isinstance(att, dict)],
        }
        loaded += 1
    _trim_reply_attachment_cache()
    if loaded:
        logger.info("loaded %d OneBot reply attachment cache entries from %s", loaded, source_path)


async def _save_reply_attachment_cache() -> None:
    """保存引用消息附件索引到独立缓存文件。"""
    async with _route_state_lock:
        _trim_reply_attachment_cache()
        path = Path(REPLY_ATTACHMENT_CACHE_FILE)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "ttl_seconds": IMAGE_CACHE_TTL_SECONDS,
                "reply_attachments": dict(_reply_attachment_cache),
            }
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            logger.warning("failed to save OneBot reply attachment cache: %s", path, exc_info=True)


async def _remember_route_state(
    *,
    stable_conversation_key: str,
    real_conversation_key: str,
    session_id: str = "",
    target: Optional[Dict[str, Any]] = None,
) -> None:
    if stable_conversation_key and real_conversation_key:
        _real_conversation_keys[stable_conversation_key] = real_conversation_key
        _stable_conversation_keys[real_conversation_key] = stable_conversation_key
    if session_id and target:
        _persisted_session_targets[session_id] = _serializable_target(target)
    await _save_route_state()


def _bot_self_id_candidates(event: Event | None, bot: Bot | None) -> set[str]:
    """收集 Bot 自身 QQ 号候选值，兼容不同 OneBot/NoneBot 对 self_id 的暴露方式。"""
    candidates: set[str] = set()
    for source in (bot, event):
        if source is None:
            continue
        for attr in ("self_id", "bot_id"):
            value = getattr(source, attr, None)
            if value is not None and str(value).strip():
                candidates.add(str(value).strip())
    return candidates


def _qq_matches_bot(qq: Any, bot_ids: set[str]) -> bool:
    value = str(qq or "").strip()
    return bool(value and value in bot_ids)


def _group_message_mentions_bot(message: Any, bot_ids: set[str]) -> bool:
    try:
        segments = list(message) if message is not None and not isinstance(message, str) else []
    except Exception:
        segments = []
    for segment in segments:
        seg_type, data = _segment_type_and_data(segment)
        if seg_type != "at":
            continue
        if _qq_matches_bot(data.get("qq"), bot_ids):
            return True
    if isinstance(message, str):
        return _raw_message_mentions_bot(message, bot_ids)
    return False


def _raw_message_mentions_bot(raw_message: Any, bot_ids: set[str]) -> bool:
    raw = str(raw_message or "")
    if not raw or not bot_ids:
        return False
    for match in _AT_QQ_RE.finditer(raw):
        if _qq_matches_bot(match.group(1), bot_ids):
            return True
    return False


def _event_replies_to_bot(event: GroupMessageEvent, bot: Bot) -> bool:
    """这条群消息是不是在回复 bot 自己的消息。

    必须看 event.reply 而不是 to_me：NoneBot 的 _check_reply / _check_at_me /
    _check_nickname 三个 preprocessor 都往同一个 to_me 里写，混在一起就没法单独关掉
    「被回复」。get_msg 失败时 _check_reply 会提前返回、reply 为 None，那种情况保守判为
    「不是回复 bot」—— 此时它也没置 to_me，不会漏触发。
    """
    reply = getattr(event, "reply", None)
    if reply is None:
        return False
    sender = getattr(reply, "sender", None)
    sender_id = getattr(sender, "user_id", None)
    if sender_id is None:
        return False
    return str(sender_id) in _bot_self_id_candidates(event, bot)


def _event_at_mentions_bot(event: GroupMessageEvent, bot: Bot) -> bool:
    """这条群消息是不是 @ 了 bot（区别于「回复了 bot」）。

    _check_at_me 命中首尾 @ 时会把那一段从消息里删掉，只留下 to_me，所以句中 @ 能从
    消息段看出来，首尾 @ 只能靠 to_me 推。扣掉「被回复」那个来源之后剩下的 to_me
    就是被剥掉的 @ —— 前提是没配 NoneBot 的 NICKNAME，那一条由启动告警盯着。
    """
    bot_ids = _bot_self_id_candidates(event, bot)
    try:
        if _group_message_mentions_bot(event.get_message(), bot_ids):
            return True
    except Exception:
        pass
    if _raw_message_mentions_bot(getattr(event, "raw_message", ""), bot_ids):
        return True
    if not _event_to_me(event):
        return False
    return not _event_replies_to_bot(event, bot)


def _event_to_me(event: GroupMessageEvent) -> bool:
    """NoneBot 置的 to_me。三个来源混在一起，只能当作「其中之一命中了」。"""
    try:
        if bool(getattr(event, "to_me", False)):
            return True
    except Exception:
        pass
    try:
        is_tome = getattr(event, "is_tome", None)
        return bool(callable(is_tome) and is_tome())
    except Exception:
        return False


def _event_identity(event: Event) -> tuple[str, ...]:
    """这条消息的身份。message_id 可能缺失、也可能被补发事件复用，所以四项一起取。"""
    return tuple(
        str(getattr(event, name, "") or "")
        for name in ("group_id", "user_id", "message_id", "time")
    )


def _trigger_roll(event: GroupMessageEvent) -> float:
    """随机插话的骰子，[0,1)。同一条消息掷几次都是同一个值。"""
    # 判定可能对同一条消息跑多遍，骰子必须同值：一遍掷中一遍没中的话，
    # 显式信号那条路和意愿判断那条路会双双放手，这条消息就没人处理了。
    identity = "|".join(_event_identity(event)).encode("utf-8")
    digest = hashlib.blake2b(_TRIGGER_ROLL_SALT + identity, digest_size=8).digest()
    return int.from_bytes(digest, "big") / float(1 << 64)


def _trigger_input(event: GroupMessageEvent, bot: Bot, text: str) -> TriggerInput:
    """把 NoneBot 事件摊成纯判定要的事实。dry-run 从 JSON 构造同一个结构。"""
    return TriggerInput(
        text=text or "",
        group_id=int(getattr(event, "group_id", 0) or 0),
        user_id=int(getattr(event, "user_id", 0) or 0),
        at_me=_event_at_mentions_bot(event, bot),
        reply_to_bot=_event_replies_to_bot(event, bot),
        now=time.time(),
        roll=_trigger_roll(event),
    )


def _group_trigger_decision(event: GroupMessageEvent, bot: Bot, text: str) -> TriggerDecision:
    """群消息的完整判定。命中信号与被否决的原因都带回来，供后续记账与试听对照。"""
    return trigger_evaluate(
        _trigger_input(event, bot, text),
        TriggerConfig.from_live(live),
        _trigger_cooldown,
    )


def _group_trigger_decision_once(event: GroupMessageEvent, bot: Bot, text: str) -> TriggerDecision:
    """同一条消息只判一次，两个 rule 共用这一份结论。

    NoneBot 会为一条群消息分别跑显式信号与意愿判断两个 rule，判定要遍历正文、装配一次
    配置快照、结算一次随机骰子 —— 判第二遍是纯重复，而且结论必须逐字相同。
    """
    key = _event_identity(event) + (text or "",)
    cached = _trigger_decisions.get(key)
    if cached is not None:
        _trigger_decisions.move_to_end(key)
        return cached
    decision = _group_trigger_decision(event, bot, text)
    if decision.triggered:
        # 接手了就没人会再判它（后面的 matcher 都被 block 掉）。这种结论存下来只会让重复
        # 投递的同一个事件绕过刚记下的冷却。
        return decision
    _trigger_decisions[key] = decision
    while len(_trigger_decisions) > _TRIGGER_DECISION_CACHE_MAX:
        _trigger_decisions.popitem(last=False)
    return decision


def _group_should_trigger(event: GroupMessageEvent, bot: Bot, text: str) -> bool:
    return _group_trigger_decision(event, bot, text).triggered


async def _judge_llm_intent(bot: Bot, event: GroupMessageEvent, text: str) -> IntentVerdict:
    """问 supervisor：这条群消息是不是在跟 bot 说话。

    送过去的全是匿名化之后的文本 —— 群号和 QQ 号本来就不该进模型上下文，判定这条
    新链路没有理由成为例外。
    """
    if _client is None:
        return IntentVerdict(error="client not ready")
    group_id = int(event.group_id)
    limit = int(live.llm_intent_context_messages)
    context_lines = [entry.text for entry in list(_group_history[group_id])[-limit:]] if limit > 0 else []
    speaker = _anonymize_text_for_ai(_event_display_name(event))
    return await _client.judge_qq_intent(
        conversation_key=_stable_conversation_key(f"qq_group:{group_id}"),
        text=_anonymize_text_for_ai(text),
        channel="qq_group",
        context_lines=context_lines,
        bot_names=list(live.name_words),
        speaker=speaker,
        node_id=live.llm_intent_node_id,
        timeout_sec=float(live.llm_intent_timeout_sec),
        max_inflight=int(live.llm_intent_max_inflight),
    )


def _is_direct_bot_interaction(event: GroupMessageEvent, bot: Bot, text: str) -> bool:
    """这条群消息是不是本人主动找 bot（决定要不要给他建长期记忆档案）。

    和触发判定各算一次：全量 / 关键词 / 随机插话下 bot 会回一堆不是在找它的消息，但
    「在群里说了句话」不等于「主动找 bot」—— 照触发结论建档，群一活跃全员都有跨会话
    档案，注入预算被一堆无关的人占满。只认被 @、回复 bot、带触发前缀这三件事。
    """
    if _event_at_mentions_bot(event, bot) or _event_replies_to_bot(event, bot):
        return True
    return bool(matched_prefix(text, live.trigger_prefixes))


def _strip_trigger_prefix(text: str) -> str:
    value = (text or "").strip()
    for prefix in live.trigger_prefixes_strippable:
        if value.startswith(prefix):
            return value[len(prefix):].strip() or value
    return value


def _warn_startup_config() -> None:
    """启动时校验权限与白名单：空值都是 fail-closed，但静默会让人误以为部署成功。

    消息里指的是 qq.yaml 的路径而不是环境变量名：热载生效后运营者是去那里改的，
    报一个已经不是主来源的变量名只会让人找错地方。
    """
    if not live.admin_users:
        logger.warning(
            "permissions.admin_users is empty: only group owners/admins can use the "
            "capabilities granted to them, and every approval request will be auto-denied"
        )
    for cap in capability.CAPABILITIES:
        # 关掉的能力在群里只会回一句「已关闭」，日志里不提就查不到是谁关的。
        if _grant_of(cap.key) == capability.OFF:
            logger.warning("permissions.capabilities.%s is off: nobody can use it", cap.key)
    if not live.allowed_groups:
        logger.warning("channels.allowed_groups is empty: the bot will not respond in any group")
    if live.enable_forward_bridge and not FORWARD_BRIDGE_TOKEN:
        if _forward_bridge_token():
            logger.info(
                "ONEBOT_FORWARD_BRIDGE_TOKEN is empty: using self-signed bridge token file %s",
                FORWARD_BRIDGE_TOKEN_FILE,
            )
        else:
            logger.warning(
                "qq_forward bridge token unavailable (%s not writable): bridge will not start",
                FORWARD_BRIDGE_TOKEN_FILE,
            )
    logger.info(
        "QQ permissions: admins=%d allowed_groups=%d allowed_private=%d allow_friends=%s",
        len(live.admin_users), len(live.allowed_groups), len(live.allowed_private_users), live.allow_private_friends,
    )


def _nonebot_nicknames() -> tuple[str, ...]:
    """NoneBot 自己的 NICKNAME 配置。空 = 没配。"""
    try:
        raw = getattr(driver.config, "nickname", None) or ()
        return tuple(str(item).strip() for item in raw if str(item).strip())
    except Exception:
        return ()


def _warn_trigger_config() -> None:
    """启动时校验触发配置：未知 mode 仍收紧到 mention_only，但不再静默。"""
    config = TriggerConfig.from_live(live)
    if not live.group_trigger_is_known:
        logger.warning(
            "unknown trigger.group_mode=%r, using mention_only; valid values: %s",
            live.group_trigger,
            ", ".join(sorted(GROUP_TRIGGER_MODES)),
        )
    if not live.trigger_prefixes:
        logger.warning("trigger.prefixes parsed empty; prefix mode can only trigger by @Bot")
    full_width = [p for p in live.trigger_prefixes if "，" in p]
    if full_width:
        logger.warning("trigger.prefixes separator must be a half-width comma, got %r", full_width)
    if config.name and not config.name_words:
        logger.warning("trigger.signals.name is on but trigger.name.words is empty; it can never match")
    if config.keyword and not config.keyword_words:
        logger.warning("trigger.signals.keyword is on but trigger.keyword.words is empty; it can never match")
    if config.random and config.random_probability <= 0:
        logger.warning("trigger.signals.random is on but the probability is 0; it can never match")
    if not config.cooldown_exempt_at and (config.cooldown_group_sec or config.cooldown_user_sec):
        logger.warning(
            "trigger.cooldown.exempt_at is off: users who @ the bot directly can be silently ignored"
        )
    nicknames = _nonebot_nicknames()
    if nicknames:
        # _check_nickname 命中后会把名字从正文里就地删掉，还会置 to_me —— 于是「用户点名了」
        # 这个信号在进模型前丢失，而且 @ 与点名再也分不开。名字触发要用 trigger.name。
        logger.warning(
            "NoneBot NICKNAME=%s strips the name out of the message body and sets to_me; "
            "use trigger.signals.name instead so both the original text and the signal survive",
            list(nicknames),
        )
    logger.info(
        "QQ trigger policy: signals=%s prefixes=%s cooldown=group %.0fs/user %.0fs exempt_at=%s llm_intent=%s",
        list(config.enabled_signals()), live.trigger_prefixes,
        config.cooldown_group_sec, config.cooldown_user_sec, config.cooldown_exempt_at,
        config.llm_intent,
    )


async def _auto_like_user(bot: Bot, user_id: int) -> None:
    if not live.enable_auto_like:
        return
    today = dt.datetime.now(_CST).strftime("%Y-%m-%d")
    if _auto_like_today.get(user_id) == today:
        return
    try:
        await bot.call_api("send_like", user_id=int(user_id), times=int(live.auto_like_times))
        _auto_like_today[user_id] = today
    except Exception:
        logger.debug("send_like failed for user=%s", user_id, exc_info=True)


async def _build_inbound_text(
    event: GroupMessageEvent,
    bot: Bot,
    user_text: str,
    conversation_key: str,
    attachments: List[Dict[str, Any]],
    *,
    exclude_seq: int = 0,
) -> tuple[str, int]:
    """组装提交给 tangqiu_main 的群聊 inbound 文本，并匿名化 QQ 群号/用户号。

    第二个返回值是本次带出的最大历史序号，提交成功后据此推进水位。exclude_seq 是
    当前这条触发消息的序号，它由【当前用户指令】带出，不再进历史块。
    """
    group_id = int(event.group_id)
    watermark = _group_history_watermark(group_id)
    pending = [entry for entry in _group_history[group_id] if entry.seq > watermark]
    # 水位仍然盖过被排除那一行：它已随指令块进了 durable history，下一轮不该再当历史重发。
    covered_seq = max((entry.seq for entry in pending), default=watermark)
    fresh = [entry for entry in pending if entry.seq != exclude_seq]
    lost = _group_history_lost(group_id, watermark)
    current_name = _anonymize_text_for_ai(_event_display_name(event))
    current_user = _event_user_alias(event)
    now = dt.datetime.now(_CST).strftime("%Y-%m-%d %H:%M CST")

    parts: List[str] = ["【群聊上下文记录】"]
    if lost > 0:
        # 不说这一句，模型会把中间断掉的一段群聊当连续的推理。
        parts.append(f"（此前 {lost} 条群消息超出缓存上限，未包含在内）")
    if fresh:
        parts.extend(entry.text for entry in fresh)
    elif lost <= 0:
        # 已带过历史却没有新行，和「这个群从没说过话」是两回事，不能都说“暂无”。
        parts.append("（无新消息）" if watermark >= 0 else "（暂无）")

    reply_context, quoted_attachments = await _build_reply_context(event, bot, conversation_key)
    if quoted_attachments:
        attachments.extend(quoted_attachments)
    if reply_context:
        parts.extend(["", "【当前消息引用】", reply_context])
    custom_face_prompt = _custom_face_prompt_block(conversation_key)
    if custom_face_prompt:
        parts.extend(["", custom_face_prompt])
    reaction_prompt = _reaction_prompt_block()
    if reaction_prompt:
        parts.extend(["", reaction_prompt])

    parts.extend([
        "",
        f"当前时间: {now}",
        "【当前用户身份】",
        *_user_identity_lines(event, current_name),
        "",
        "【当前用户指令】",
        f"{current_name}（{current_user}）: {_anonymize_text_for_ai(user_text)}",
        "",
        "请根据以上上下文，执行当前用户的指令并给出回复。",
    ])
    return "\n".join(parts), covered_seq


def _parse_direct_draw_command(user_text: str) -> Optional[str]:
    """解析 QQ 侧 /生图 直达命令；返回清理后的绘图需求，None 表示不是命令。"""
    text = (user_text or "").strip()
    if not text:
        return None
    match = _DRAW_DIRECT_RE.match(text)
    if not match:
        return None
    prompt = (match.group(1) or "").strip()
    if not prompt:
        prompt = "请根据上下文生成一张合适的图片"
    return prompt


async def _build_draw_direct_inbound_text(event: Event, user_text: str, is_dm: bool) -> str:
    """构造直达绘图节点的入站文本，避免普通入口 AI 再判断一次意图。"""
    now = dt.datetime.now(_CST).strftime("%Y-%m-%d %H:%M CST")
    name = _anonymize_text_for_ai(_event_display_name(event))
    user = _event_user_alias(event)
    target = "私聊" if is_dm else "群聊"
    return "\n".join([
        f"当前时间: {now}",
        f"来源: QQ{target} / /生图 直达命令",
        "【当前用户身份】",
        *_user_identity_lines(event, name),
        "",
        "【绘图请求】",
        f"{name}（{user}）: {user_text}",
        "",
        "请直接按绘图节点规则处理：如果用户要求画图/生图则生成图片；如果用户明确只要提示词/tag则只给提示词。",
    ])


async def _build_private_inbound_text(event: PrivateMessageEvent, bot: Bot, user_text: str, conversation_key: str, attachments: List[Dict[str, Any]]) -> str:
    """组装提交给 tangqiu_main 的私聊 inbound 文本，并匿名化 QQ 用户号。"""
    fallback_text = await _event_text_with_forward(bot, event)
    text = _anonymize_text_for_ai((user_text or fallback_text).strip() or "你好")
    name = _anonymize_text_for_ai(_event_display_name(event))
    user = _event_user_alias(event)
    now = dt.datetime.now(_CST).strftime("%Y-%m-%d %H:%M CST")
    parts: List[str] = [f"当前时间: {now}"]
    reply_context, quoted_attachments = await _build_reply_context(event, bot, conversation_key)
    if quoted_attachments:
        attachments.extend(quoted_attachments)
    if reply_context:
        parts.extend(["", "【当前消息引用】", reply_context])
    custom_face_prompt = _custom_face_prompt_block(conversation_key)
    if custom_face_prompt:
        parts.extend(["", custom_face_prompt])
    parts.extend([
        "",
        "【当前用户身份】",
        *_user_identity_lines(event, name),
        "",
        "【当前用户指令】",
        f"{name}（{user}）: {text}",
        "",
        "请根据以上上下文，执行当前用户的指令并给出回复。",
    ])
    return "\n".join(parts)


# 拿不到可确认的图片时必须显式禁止模型从历史里猜，否则它会编出一张不存在的图。
_IMAGE_UNBOUND_HINT = (
    "当前消息或引用链未能绑定到可确认的图片；不得从聊天历史猜测图片内容，请让用户重新发送或直接引用原图。"
)

# 绘图节点只按文字出图，参考图用不上；丢弃时当场告知，别让用户以为图被采纳。
_DRAW_REFERENCE_IMAGE_NOTICE = "绘图暂不支持参考图，已忽略你发送的图片，仅按文字描述生成。"


def _apply_attachment_hints(
    inbound_text: str,
    user_text: str,
    attachments: List[Dict[str, Any]],
    attachment_errors: List[str],
) -> str:
    """把图片处理提示挂到 inbound 文本上，并把附件路径回填进 [图片] 占位。"""
    if _text_looks_like_image_query(user_text) and not attachments:
        # 就地 append：调用方在此之后不再读这个列表。
        attachment_errors.append(_IMAGE_UNBOUND_HINT)
    if attachment_errors:
        inbound_text += "\n\n【图片处理提示】\n" + "\n".join(dict.fromkeys(attachment_errors))
    for att in attachments:
        # 只有图片占 [图片] 位。文件附件混进来会顶掉后面那张图的位置，路径就对错人了。
        if att.get("type") != "image":
            continue
        inbound_text = inbound_text.replace(
            IMAGE_PLACEHOLDER, f"[{_attachment_label(att)}: {att['path']}]", 1,
        )
    return inbound_text


async def _try_preempt_running_task(
    *,
    bot: Bot,
    event: Event,
    conversation_key: str,
    inbound_text: str,
    attachments: Optional[List[Dict[str, Any]]],
    is_dm: bool,
    platform_updates: Dict[str, Any],
) -> bool:
    """尝试把 QQ 新消息注入当前会话正在运行的入口任务。"""
    # 先查询当前 session 的入口任务，再用 preempt_task 注入新消息：让同一会话的
    # 新指令打断并接续旧任务，而不是在已有入口任务运行时并发开新任务。
    if _client is None or _session_state is None:
        return False

    existing_sid = _session_state.get_session_id(conversation_key)
    if not existing_sid:
        return False

    try:
        running_tasks = await _client.get_running_tasks(existing_sid)
    except Exception:
        logger.exception("preempt_v2: get running tasks failed")
        return False

    for rt in running_tasks:
        if not rt.task_id or not rt.is_user_entry:
            continue

        rt_src_seq = rt.source_inbound_seq or 0
        rt_trigger = _session_state.get_trigger(rt_src_seq) if rt_src_seq else None
        if not rt_trigger:
            result = _session_state.find_trigger_by_session(existing_sid)
            rt_trigger = result[1] if result else None

        if not is_dm:
            # 2026-05-03 修改原因：群聊里多个用户共享同一个 conversation_key。
            # 这里必须用旧 trigger 保存的 event.user_id 校验来源；无法确认同一用户
            # 时不执行 preempt，目的是避免一个群成员打断另一个群成员的任务。
            if not rt_trigger:
                continue
            rt_event = rt_trigger.platform_data.get("event")
            if getattr(rt_event, "user_id", None) != getattr(event, "user_id", None):
                continue

        try:
            ok = await _client.preempt_task(
                rt.task_id,
                message=inbound_text,
                attachments=attachments,
            )
        except Exception:
            logger.exception("preempt_v2: preempt task failed")
            continue

        if not ok:
            continue

        # 2026-05-03 修改原因：Preempt 成功后不会生成新的 inbound_seq。
        # 因此必须把旧 trigger 的平台对象更新为本次 QQ event 和 bot；目的
        # 是让最终回复引用新的触发消息，并继续发到正确的 QQ 会话。
        fresh_platform = dict(platform_updates)
        fresh_platform["last_typing_time"] = time.time()
        if rt_trigger:
            rt_trigger.platform_data.update(fresh_platform)
        elif rt_src_seq:
            _session_state.register_trigger(
                TriggerInfo(
                    inbound_seq=rt_src_seq,
                    conversation_key=conversation_key,
                    session_id=existing_sid,
                    is_dm=is_dm,
                    platform_data=fresh_platform,
                )
            )

        _session_targets[existing_sid] = dict(fresh_platform)
        _conversation_bots[conversation_key] = bot
        await _remember_route_state(
            stable_conversation_key=conversation_key,
            real_conversation_key=_real_conversation_key(conversation_key),
            session_id=existing_sid,
            target=fresh_platform,
        )
        logger.info("preempt_v2: injected into QQ task %s", rt.task_id[:8])
        return True

    return False


async def _submit_or_preempt_inbound(
    *,
    bot: Bot,
    event: Event,
    channel: str,
    real_conversation_key: str,
    stable_conversation_key: str,
    inbound_text: str,
    user_text: str,
    attachments: List[Dict[str, Any]],
    is_dm: bool,
    platform_updates: Dict[str, Any],
    entry_node_id: str = "",
    history_watermark: int = -1,
    direct_interaction: bool = True,
) -> bool:
    """提交或打断 QQ 入站消息；对外使用稳定哈希 conversation_key，对内保留真实路由。"""
    if _client is None or _session_state is None:
        return False

    group_id = platform_updates.get("group_id") if platform_updates.get("type") == "group" else None

    # 显式命令自带入口节点，打断注入会落到正在跑的那个节点上。
    if live.enable_preempt and not entry_node_id:
        preempt_ok = await _try_preempt_running_task(
            bot=bot,
            event=event,
            conversation_key=stable_conversation_key,
            inbound_text=inbound_text,
            attachments=attachments or None,
            is_dm=is_dm,
            platform_updates=platform_updates,
        )
        if preempt_ok:
            # preempt 注入的消息会被写进 ConversationStore（engine/builtin/preempt.py），
            # 但不产生新的 inbound_seq，没有 inbound_accepted 可等，只能就地推进。
            if group_id is not None and history_watermark >= 0:
                _session_state.advance_watermark(int(group_id), history_watermark)
            return True

    result = await _client.submit_inbound(
        channel=channel,
        conversation_key=stable_conversation_key,
        text=inbound_text,
        message_id=str(getattr(event, "message_id", "")),
        attachments=attachments or None,
        use_context=True,
        entry_node_id=entry_node_id or ENTRY_NODE_ID,
        # 以未兜底的原始 entry_node_id 为准：兜底值人人都非空，会把每条消息都钉住，视觉路由和 AI switch_node 全线失效。
        entry_node_pinned=bool(entry_node_id),
        platform_auth={
            "platform": "qq",
            "user_id": str(getattr(event, "user_id", "")),
            "is_admin": _is_admin_user(getattr(event, "user_id", "")),
        },
        route_hints={
            "platform": "qq",
            "channel": channel,
            "has_image": any(str(a.get("type") or "") == "image" for a in (attachments or [])),
            "target_type": "private" if is_dm else "group",
        },
        memory_hints={"subjects": _collect_memory_subjects(
            event, user_text, stable_conversation_key, direct_interaction,
        )},
    )
    if not result.session_id or not result.accepted:
        return False

    _note_inbound_seq(result.inbound_seq)

    if group_id is not None and history_watermark >= 0:
        if result.inbound_seq:
            # 等 inbound_accepted 再推进：supervisor 没真正收下就推，那几行历史就没人再发了。
            _session_state.register_watermark(int(result.inbound_seq), int(group_id), history_watermark)
        else:
            _session_state.advance_watermark(int(group_id), history_watermark)

    _real_conversation_keys[stable_conversation_key] = real_conversation_key
    _session_state.register_session(stable_conversation_key, result.session_id)
    target = dict(platform_updates)
    target["conversation_key"] = stable_conversation_key
    _session_targets[result.session_id] = target
    _conversation_bots[stable_conversation_key] = bot
    await _remember_route_state(
        stable_conversation_key=stable_conversation_key,
        real_conversation_key=real_conversation_key,
        session_id=result.session_id,
        target=target,
    )

    if result.inbound_seq:
        react_meta: Dict[str, Any] = {}
        trigger = TriggerInfo(
            inbound_seq=result.inbound_seq,
            conversation_key=stable_conversation_key,
            session_id=result.session_id,
            is_dm=is_dm,
            platform_data={
                **platform_updates,
                "last_typing_time": time.time(),
                **react_meta,
            },
        )
        _session_state.register_trigger(trigger)
    return True


async def _enqueue_or_submit_inbound(item: QueuedInbound) -> bool:
    if not live.enable_queue:
        return await _submit_or_preempt_inbound(
            bot=item.bot,
            event=item.event,
            channel=item.channel,
            real_conversation_key=item.real_conversation_key,
            stable_conversation_key=item.stable_conversation_key,
            inbound_text=item.text,
            user_text=item.user_text,
            attachments=item.attachments,
            is_dm=item.is_dm,
            platform_updates=item.platform_updates,
            entry_node_id=item.entry_node_id,
            history_watermark=item.history_watermark,
            direct_interaction=item.direct_interaction,
        )

    async with _qq_queue_condition:
        existing = _qq_queue_by_key.get(item.stable_conversation_key)
        # 入口节点不同的两条不能并成一轮：合并会把 /生图 选定的节点吃掉。
        if existing is not None and existing.entry_node_id == item.entry_node_id:
            existing.text = f"{existing.text}\n\n【排队期间追加消息】\n{item.text}"
            # 合并后的文本包含两条各自的历史块，水位取两者较高的那个。
            existing.history_watermark = max(existing.history_watermark, item.history_watermark)
            # 排队期间只要有一条是主动找 bot，合并后的这一轮就算主动。
            existing.direct_interaction = existing.direct_interaction or item.direct_interaction
            existing.attachments.extend(item.attachments)
            existing.event = item.event
            existing.platform_updates = dict(item.platform_updates)
            # The merged task uses all accumulated attachments, so its eventual
            # Bot text reply must bind all of those same source images rather than
            # only the last queued message's list.
            existing.platform_updates["_source_attachments"] = source_attachments_from_merged(
                existing.attachments
            )
            existing.user_text = item.user_text
        else:
            _qq_queue.append(item)
            _qq_queue_by_key[item.stable_conversation_key] = item
        _qq_queue_condition.notify()
    return True


def _queue_worker_should_exit(worker_index: int) -> bool:
    """这个 worker 该退了吗。

    缩容时多出来的那几个立刻退，留下的接着干；整个队列被关掉时最后一批要先把已排队的
    消息清完再退 —— 关开关不该让排在队里的用户消息无声消失。
    调用方必须持有 _qq_queue_condition。
    """
    target = live.queue_workers if live.enable_queue else 0
    if worker_index < target:
        return False
    return target > 0 or not _qq_queue


async def _qq_queue_worker_forever(worker_index: int) -> None:
    """串行队列 worker。

    退出判定读实时配置而不是钉住的快照：这个 task 活得比任何一条消息都长，钉住就永远
    看不到运营者刚改的 workers 数量。取到件之后才钉住，处理这一条期间配置不再变。
    """
    while True:
        async with _qq_queue_condition:
            while True:
                if _queue_worker_should_exit(worker_index):
                    return
                if _qq_queue:
                    break
                await _qq_queue_condition.wait()
            item = _qq_queue.popleft()
            # 只删指向本件的合并槽：同会话已允许存在多件时，别把指向后来那件的槽误删。
            if _qq_queue_by_key.get(item.stable_conversation_key) is item:
                _qq_queue_by_key.pop(item.stable_conversation_key, None)

        with pinned_live_config():
            reply_event: Optional[asyncio.Event] = None
            if live.queue_wait_for_reply:
                reply_event = asyncio.Event()
                _qq_waiting_replies[item.stable_conversation_key] = reply_event
            try:
                await _submit_or_preempt_inbound(
                    bot=item.bot,
                    event=item.event,
                    channel=item.channel,
                    real_conversation_key=item.real_conversation_key,
                    stable_conversation_key=item.stable_conversation_key,
                    inbound_text=item.text,
                    user_text=item.user_text,
                    attachments=item.attachments,
                    is_dm=item.is_dm,
                    platform_updates=item.platform_updates,
                    entry_node_id=item.entry_node_id,
                    history_watermark=item.history_watermark,
                    direct_interaction=item.direct_interaction,
                )
                if reply_event is not None:
                    try:
                        await asyncio.wait_for(reply_event.wait(), timeout=live.queue_reply_timeout)
                    except asyncio.TimeoutError:
                        logger.warning("QQ queue reply wait timed out conversation=%s timeout=%ss", item.real_conversation_key, live.queue_reply_timeout)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("failed to process QQ queue item conversation=%s", item.real_conversation_key)
            finally:
                if reply_event is not None:
                    _qq_waiting_replies.pop(item.stable_conversation_key, None)
            interval = live.queue_interval
        if interval > 0:
            await asyncio.sleep(interval)



def _group_id_from_conversation_key(conversation_key: str) -> Optional[int]:
    """从 qq_group:{group_id} 会话键解析群号。"""
    prefix = "qq_group:"
    if not conversation_key.startswith(prefix):
        return None
    try:
        return int(conversation_key[len(prefix):])
    except ValueError:
        return None


def _private_user_id_from_conversation_key(conversation_key: str) -> Optional[int]:
    """从 qq_private:{user_id} 会话键解析私聊用户号。"""
    prefix = "qq_private:"
    if not conversation_key.startswith(prefix):
        return None
    try:
        return int(conversation_key[len(prefix):])
    except ValueError:
        return None


def _target_from_conversation_key(conversation_key: str) -> Optional[Dict[str, Any]]:
    """根据 conversation_key 还原 QQ 回复目标，供无 trigger 的回调兜底使用。"""
    # 2026-05-01 修改原因：EventRouter 的 fallback 回调只有 conversation_key；
    # 这里集中解析 group/private 两种 key，避免发送逻辑继续硬编码群聊。
    conversation_key = _real_conversation_key(conversation_key)
    group_id = _group_id_from_conversation_key(conversation_key)
    if group_id is not None:
        return {"type": "group", "group_id": group_id}
    user_id = _private_user_id_from_conversation_key(conversation_key)
    if user_id is not None:
        return {"type": "private", "user_id": user_id}
    return None


def _target_from_platform_data(platform_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从 trigger 平台数据中提取 QQ 回复目标。"""
    # 2026-05-01 修改原因：主回调会携带 matcher 保存的平台数据；优先使用
    # 显式 type 字段区分私聊和群聊，避免只有 user_id 时误判群聊成员为私聊。
    # 2026-05-03 新增：提取触发消息 ID 和发送者 ID，发第一条回复时自动引用 + @
    event = platform_data.get("event")
    msg_id = getattr(event, "message_id", None) if event else None
    sender_id = getattr(event, "user_id", None) if event else None
    # 匿名消息的 user_id 是占位号，at 出去群里只会看到一串数字。
    if event is not None and _anonymous_identity(event):
        sender_id = None
    target_type = platform_data.get("type")
    conversation_key = str(platform_data.get("conversation_key") or "")
    if target_type == "private" and platform_data.get("user_id") is not None:
        t: Dict[str, Any] = {"type": "private", "user_id": int(platform_data["user_id"])}
        if conversation_key:
            t["conversation_key"] = conversation_key
        if msg_id is not None:
            t["reply_message_id"] = int(msg_id)
        return t
    if target_type == "group" and platform_data.get("group_id") is not None:
        t = {"type": "group", "group_id": int(platform_data["group_id"])}
        if conversation_key:
            t["conversation_key"] = conversation_key
        if msg_id is not None:
            t["reply_message_id"] = int(msg_id)
        if sender_id is not None:
            t["reply_sender_id"] = int(sender_id)
        return t
    if platform_data.get("group_id") is not None:
        t = {"type": "group", "group_id": int(platform_data["group_id"])}
        if conversation_key:
            t["conversation_key"] = conversation_key
        if msg_id is not None:
            t["reply_message_id"] = int(msg_id)
        if sender_id is not None:
            t["reply_sender_id"] = int(sender_id)
        return t
    return None


def _get_fallback_bot() -> Optional[Bot]:
    """获取 fallback 发送用 Bot；EventRouter 可能在 matcher 外触发回调。"""
    if _last_bot is not None:
        return _last_bot
    try:
        return get_bot()
    except Exception:
        return None


def _truncate_qq_text(text: str) -> str:
    """限制单条 QQ 消息长度，避免超过 OneBot 实现的消息上限。"""
    if len(text) <= live.message_limit:
        return text
    suffix = "\n（内容过长，已截断）"
    return text[: live.message_limit - len(suffix)] + suffix


def _message_from_processed_segments(segments: List[Dict[str, Any]]) -> Message:
    """把 emoji_handler 的轻量段描述转换为 OneBot Message。"""
    message_segments: List[MessageSegment] = []
    for segment in segments:
        if segment.get("type") == "text":
            content = str(segment.get("content", ""))
            if content:
                message_segments.append(MessageSegment.text(content))
        elif segment.get("type") == "image":
            url = segment.get("url")
            if url:
                if segment.get("emoji"):
                    # QQ 收藏表情：在普通 image 段基础上补 sub_type=1（表情子类型），
                    # 让 QQ 客户端按小图/贴纸渲染而不是普通大图；summary 提供
                    # "[表情]" 占位，兼容不支持 sub_type 的客户端摘要显示。
                    emoji_seg = MessageSegment.image(url)
                    emoji_seg.data["sub_type"] = 1
                    emoji_seg.data.setdefault("summary", "[表情]")
                    message_segments.append(emoji_seg)
                else:
                    message_segments.append(MessageSegment.image(url))
        elif segment.get("type") == "at":
            qq_id = segment.get("qq")
            if qq_id:
                message_segments.append(MessageSegment.at(qq_id))
    return Message(message_segments)


def _mark_qq_reply_finished(conversation_key: str) -> None:
    for key in {conversation_key, _real_conversation_key(conversation_key)}:
        event = _qq_waiting_replies.get(key)
        if event is not None:
            event.set()


def _message_dedup_text(message: Any) -> str:
    """生成用于 QQ 发送幂等判断的稳定文本。"""
    try:
        if isinstance(message, Message):
            return str(message)
    except Exception:
        pass
    return str(message or "")


# 群成员缓存：group_id -> (过期时间, {user_id 集合}, {名片/昵称小写 -> user_id},
#                         {user_id -> (card, nickname)})。
# 用于（1）发送前校验 at 目标是否本群成员，避免 NapCat 对无法解析的 uid
# 抛 Get Uid Error 使整条消息失败；（2）按当前群名片/昵称实时反查真实 QQ 号，
# 让 [at:昵称] 能命中只存在于本群、未录入 profile/匿名表的成员（包括昵称叫 all 的人）；
# （3）2026-07-17 新增 uid -> (card, nickname) 正向映射，让 @ 群友时的显示名
# 优先取“本群群名片(card)”，其次“QQ 昵称(nickname)”，避免直接用 QQ 昵称。
_group_member_cache: Dict[int, tuple[float, set[str], Dict[str, str], Dict[str, tuple[str, str]]]] = {}
_GROUP_MEMBER_CACHE_TTL = float(os.environ.get("ONEBOT_GROUP_MEMBER_CACHE_TTL", "300") or "300")


async def _load_group_members(
    bot: Bot, group_id: int
) -> tuple[set[str], Dict[str, str], Dict[str, tuple[str, str]]] | None:
    """拉取并缓存群成员。

    返回 ({user_id 集合}, {名片/昵称小写 -> user_id}, {user_id -> (card, nickname)})。
    取不到时返回 None（表示无法校验/反查，由调用方决定不做拦截）。
    """
    try:
        gid = int(group_id)
    except Exception:
        return None
    now = time.time()
    cached = _group_member_cache.get(gid)
    if cached and cached[0] > now:
        return cached[1], cached[2], cached[3]
    try:
        data = await bot.call_api("get_group_member_list", group_id=gid)
    except Exception:
        logger.debug("get_group_member_list failed for group=%s", gid, exc_info=True)
        return None
    members: set[str] = set()
    name_map: Dict[str, str] = {}
    card_map: Dict[str, tuple[str, str]] = {}
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict) or item.get("user_id") is None:
                continue
            uid = str(item.get("user_id"))
            members.add(uid)
            card = str(item.get("card") or "").strip()
            nickname = str(item.get("nickname") or "").strip()
            card_map[uid] = (card, nickname)
            # 名片(card)优先于昵称(nickname)；同名时不覆盖已有映射（保留先遇到的）。
            for key in (card, nickname):
                name = str(key or "").strip()
                if name:
                    name_map.setdefault(name.casefold(), uid)
    if not members:
        return None
    for stale_gid in [g for g, item in _group_member_cache.items() if item[0] <= now]:
        _group_member_cache.pop(stale_gid, None)
    _group_member_cache[gid] = (now + _GROUP_MEMBER_CACHE_TTL, members, name_map, card_map)
    return members, name_map, card_map


async def _group_member_ids(bot: Bot, group_id: int) -> set[str] | None:
    """获取群成员 QQ 号集合（带 TTL 缓存）。取不到时返回 None。"""
    loaded = await _load_group_members(bot, group_id)
    return loaded[0] if loaded else None


def _group_member_display_name(group_id: Any, user_id: Any) -> str:
    """返回 @ 某群友时应展示的名字：本群群名片(card) > 本群 QQ 昵称(nickname)。

    仅使用已缓存的群成员数据（不发网络请求）；缓存未就绪或未命中时返回空串，
    由调用方回退到 profile 显示名 / 匿名别名。
    """
    uid = str(user_id or "").strip()
    if not uid:
        return ""
    try:
        gid = int(group_id)
    except Exception:
        return ""
    cached = _group_member_cache.get(gid)
    if not cached:
        return ""
    card, nickname = cached[3].get(uid, ("", ""))
    name = card or nickname
    return _sanitize_name(name) if name else ""


def _resolve_at_alias_in_group(group_id: Any, token: str) -> str:
    """在已缓存的群成员名片/昵称里反查 token 对应的真实 QQ 号（小写匹配）。

    仅使用缓存（不发网络请求）；缓存未就绪或未命中时返回空串。
    """
    raw = str(token or "").strip()
    if not raw:
        return ""
    if raw.startswith("@"):
        raw = raw[1:].strip()
    if not raw:
        return ""
    try:
        gid = int(group_id)
    except Exception:
        return ""
    cached = _group_member_cache.get(gid)
    if not cached:
        return ""
    return cached[2].get(raw.casefold(), "")


async def _sanitize_group_at_segments(bot: Bot, group_id: Any, message: Any) -> Any:
    """把群消息里"非本群成员/无法解析"的 at 段降级为文本，避免整条消息发送失败。

    2026-07-14 修复：一条消息 @ 多人时，只要其中一个 at 的 uid 被 NapCat 判为
    Get Uid Error，send_group_msg 就会整条失败。这里在发送前用群成员列表校验，
    命中不了的 at 用 @昵称/@别名 文本代替，@全体成员(all) 放行。
    """
    if not isinstance(message, Message):
        return message
    loaded = await _load_group_members(bot, group_id)
    if loaded is None:
        # 拿不到成员列表时不拦截，保持原样（避免误伤把所有 at 变文本）。
        return message
    members, name_map, _card_map = loaded
    new_segments: List[MessageSegment] = []
    for seg in message:
        if getattr(seg, "type", "") == "at":
            qq = str((getattr(seg, "data", {}) or {}).get("qq", "")).strip()
            # 特例：[at:all] 但群里真有成员名片/昵称叫 "all"，优先当成 @ 那个人，
            # 避免把他误伤为 @全体成员。
            if qq.lower() == "all":
                mapped = name_map.get("all")
                if mapped:
                    logger.info("resolve [at:all] to group member %s in group=%s", mapped, group_id)
                    new_segments.append(MessageSegment.at(mapped))
                    continue
                # 确实是 @全体成员，放行。
                new_segments.append(seg)
                continue
            if not qq:
                new_segments.append(seg)
                continue
            # 以 members（本群真实 QQ 号集合）为权威，按理想顺序判定：
            #   1) qq 是数字且 ∈ members  -> 就是这个真实 QQ 号，放行。
            #   2) 否则先按 name_map（群名片/昵称，card 优先）反查：命中则改用真实 QQ 号。
            #     这能修复“群名片恰好是纯数字（如名片叫 1）”被误当成 QQ 号的问题。
            #   3) 都不中 -> 丢弃这个非法 at（不再降级成 @匿名代号发到群里）。
            if qq in members:
                new_segments.append(seg)
                continue
            mapped = _resolve_at_alias_in_group(group_id, qq)
            if mapped and mapped in members:
                logger.info("resolve at token %r to group member %s in group=%s", qq, mapped, group_id)
                new_segments.append(MessageSegment.at(mapped))
                continue
            # 既不是本群成员 QQ 号，也没有群友名片/昵称叫这个名字：判为无效 at，直接丢弃。
            # Why: 这种情况多半是模型凭空捧造的 at（如 qq=1），降级成 @UserXX 反而产生脗肿文本。
            logger.warning("drop invalid at: token=%r not a member/card in group=%s", qq, group_id)
            continue
        new_segments.append(seg)
    return Message(new_segments)


def _strip_at_to_text(message: Any, group_id: Any = None) -> Any:
    """把消息里所有 at 段降级为 @文本，用于发送失败后的兜底重发。

    传入 group_id 时，@文本优先取本群群名片/昵称，其次 profile 显示名。
    拿不到可读名字时直接丢弃这个 at（不再用 _anonymize_user_id 把匿名代号发到群里）。
    """
    if not isinstance(message, Message):
        return message
    new_segments: List[MessageSegment] = []
    for seg in message:
        if getattr(seg, "type", "") == "at":
            data = getattr(seg, "data", {}) or {}
            qq = str(data.get("qq", "")).strip()
            if qq.lower() == "all":
                new_segments.append(MessageSegment.text("@全体成员"))
            elif qq:
                name = _group_member_display_name(group_id, qq) or _qq_profile_display_name(qq)
                if name:
                    new_segments.append(MessageSegment.text(f"@{name}"))
                # 无可读名字：丢弃该 at，避免 @匿名代号泄露到群里。
            continue
        new_segments.append(seg)
    return Message(new_segments)


def _extract_sent_message_id(result: Any) -> str:
    """从 send_*_msg 返回值里提取 message_id（兼容 dict / 标量 两种形式）。"""
    if isinstance(result, dict):
        mid = result.get("message_id")
        if mid is None:
            mid = result.get("data", {}).get("message_id") if isinstance(result.get("data"), dict) else None
        return str(mid).strip() if mid is not None else ""
    if isinstance(result, (int, str)):
        return str(result).strip()
    return ""


def _resend_plan(exc: Exception, sent_message: Any, at_as_text: Any | None) -> tuple[str, Any] | None:
    """决定失败后要不要再物理发一次、发哪条；None = 不重发。

    这一层重发发生在幂等 claim 内部，claim 既看不见也拦不住第二次投递，因此只认
    “确定没发出”的失败；歧义 ack 一律不重发。
    """
    if not classify_send_exception(exc).definitely_not_sent:
        return None
    if at_as_text is not None and is_at_uid_failure(exc) and str(at_as_text) != str(sent_message):
        return "at_as_text", at_as_text
    if is_temp_file_missing(exc):
        return "same_message", sent_message
    return None


async def _send_qq_message_once(
    bot: Bot, target: Dict[str, Any], message: Any, *, idempotency_key: str = "",
) -> str:
    """执行一次 OneBot 发送；失败后最多再物理发一次，且只在确认没发出时才发。"""
    sender: Callable[[Any], Awaitable[Any]]
    if target.get("type") == "private":
        user_id = target.get("user_id")
        if user_id is None:
            raise ValueError("private target missing user_id")

        async def send_private(payload: Any) -> Any:
            return await bot.send_private_msg(user_id=int(user_id), message=payload)

        sender, outbound = send_private, message
    elif target.get("type") == "group":
        group_id = target.get("group_id")
        if group_id is None:
            raise ValueError("group target missing group_id")

        async def send_group(payload: Any) -> Any:
            return await bot.send_group_msg(group_id=int(group_id), message=payload)

        # 非本群/无法解析的 at 会让 NapCat 整条失败，发送前先清洗。
        sender, outbound = send_group, await _sanitize_group_at_segments(bot, group_id, message)
    else:
        raise OneBotSendContractError(f"unknown QQ target type: {target!r}")
    try:
        return _extract_sent_message_id(await sender(outbound))
    except ActionFailed as exc:
        at_as_text = (
            _strip_at_to_text(outbound, target.get("group_id"))
            if target.get("type") == "group" else None
        )
        plan = _resend_plan(exc, outbound, at_as_text)
        if plan is None:
            raise
        reason, resend_message = plan
        logger.warning(
            "onebot_send_resend",
            extra={
                "idempotency_key": idempotency_key,
                "resend_reason": reason,
                "target": target_identity(target),
                "send_error": str(exc),
            },
        )
        return _extract_sent_message_id(await sender(resend_message))


def _send_audit_fields(
    context: OutboundSendContext,
    *,
    platform_message_id: str,
    attempt: int,
    idempotency_key: str,
) -> Dict[str, Any]:
    return {
        "event_seq": context.event_seq,
        "event_id": context.event_id,
        "task_id": context.task_id,
        "source_inbound_seq": context.source_inbound_seq,
        "conversation_key": context.conversation_key,
        "platform_message_id": platform_message_id,
        "attempt": attempt,
        "idempotency_key": idempotency_key,
    }


def _sent_ttl_for_context(context: OutboundSendContext) -> float | None:
    """Use short retention only for the last-resort context-free content key."""
    if context.idempotency_key or context.event_id:
        return None
    return ONEBOT_IDEMPOTENCY_FALLBACK_SENT_TTL_SECONDS


async def _heartbeat_idempotency_claim(claim: IdempotencyClaim) -> None:
    """Renew one logical-send owner until its platform operation finishes."""
    interval = max(0.05, min(30.0, _outbound_idempotency.lease_seconds / 3))
    while True:
        await asyncio.sleep(interval)
        try:
            await _outbound_idempotency.heartbeat(claim)
        except IdempotencyOwnershipError:
            # The protected send commits pending -> sent/ambiguous atomically. A
            # heartbeat already queued at that boundary will then observe that its
            # pending ownership is gone; this is the normal terminal condition.
            # A real takeover is still detected by the protected operation's strict
            # owner-checked commit/release path.
            return


async def _send_qq_message(
    bot: Bot,
    target: Dict[str, Any],
    message: Any,
    *,
    send_context: OutboundSendContext | None = None,
    content_identity: str = "",
    idempotency_key: str = "",
    attempt: int = 1,
) -> str:
    """Send with an explicit contract and pending/sent two-phase idempotency."""
    validate_send_request(bot, target)
    context = send_context or OutboundSendContext(
        conversation_key=str(target.get("conversation_key") or "")
    )
    identity = content_identity or _message_dedup_text(message).strip()
    key = idempotency_key or make_idempotency_key(
        target, identity, event_id=context.idempotency_key or context.event_id,
    )
    claim = await _outbound_idempotency.begin(key)
    if not claim.acquired and claim.state == "pending":
        resolved = await _outbound_idempotency.wait_for_resolution(key)
        if resolved is None:
            claim = await _outbound_idempotency.begin(key)
        else:
            claim = IdempotencyClaim(key=key, acquired=False, state=resolved)
    fields = _send_audit_fields(
        context,
        platform_message_id="",
        attempt=attempt,
        idempotency_key=key,
    )
    if not claim.acquired:
        logger.info("onebot_send_duplicate", extra={**fields, "claim_state": claim.state})
        return f"idempotent:{key}"
    logger.info("onebot_send_started", extra=fields)
    heartbeat = asyncio.create_task(_heartbeat_idempotency_claim(claim))
    try:
        async def send_once() -> str:
            return await _send_qq_message_once(bot, target, message, idempotency_key=key)

        platform_message_id = await protected_claim_send(
            _outbound_idempotency,
            claim,
            send_once,
            sent_ttl=_sent_ttl_for_context(context),
            message_id_getter=lambda result: str(result or ""),
        )
        logger.info(
            "onebot_send_sent",
            extra=_send_audit_fields(
                context, platform_message_id=platform_message_id,
                attempt=attempt, idempotency_key=key,
            ),
        )
        return platform_message_id
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat


async def _send_split_text(
    bot: Bot,
    target: Dict[str, Any],
    text: str,
    *,
    source_attachments: List[Dict[str, Any]] | None = None,
    send_context: OutboundSendContext | None = None,
) -> bool:
    """按 [SPLIT] 拆分文本，并把每段回复绑定到本轮来源图片。"""
    # 2026-05-01 修改原因：文本拆分逻辑对群聊和私聊相同，实际发送交给
    # _send_qq_message 处理，确保私聊也能复用表情替换和分段发送能力。
    sent_any = False
    parts = text.split(_SPLIT_SIGNAL) if text else []
    conversation_token = _sticker_send_conversation.set(_sticker_conversation_key(target))
    try:
        sent_any = await _send_split_parts(
            bot, target, parts,
            source_attachments=source_attachments, send_context=send_context,
        )
    finally:
        _sticker_send_conversation.reset(conversation_token)
    return sent_any


async def _send_split_parts(
    bot: Bot,
    target: Dict[str, Any],
    parts: List[str],
    *,
    source_attachments: List[Dict[str, Any]] | None = None,
    send_context: OutboundSendContext | None = None,
) -> bool:
    sent_any = False
    for index, raw_part in enumerate(parts):
        part = _truncate_qq_text(raw_part.strip())
        if not part:
            continue
        segments = await process_emojis(
            part,
            bot,
            _bqbs,
            _current_custom_face_names(),
            _current_custom_face_metadata(),
            strip_asterisk_styles=live.strip_asterisk_styles,
            strip_underscore_styles=live.strip_underscore_styles,
        )
        if not segments:
            continue
        msg = _message_from_processed_segments(segments)
        # 第一条消息带引用回复 + @发送者
        if live.reply_to_trigger and not sent_any and target.get("reply_message_id"):
            prefix = MessageSegment.reply(target["reply_message_id"])
            if target.get("reply_sender_id"):
                prefix = prefix + MessageSegment.at(target["reply_sender_id"]) + MessageSegment.text(" ")
            msg = prefix + msg
        segment_identity = f"text:{index}:{_message_dedup_text(msg).strip()}"
        segment_context = send_context.child(segment_identity) if send_context else None
        sent_message_id = await _send_qq_message(
            bot,
            target,
            msg,
            send_context=segment_context,
            content_identity=segment_identity,
        )
        if sent_message_id and source_attachments:
            _remember_reply_attachments(
                sent_message_id,
                str(target.get("conversation_key") or ""),
                str(getattr(bot, "self_id", "") or ""),
                source_attachments,
            )
        sent_any = True
        if index < len(parts) - 1:
            await asyncio.sleep(0.5)
    return sent_any


def _to_workspace_rel_path(raw_path: Any) -> str:
    """把绝对/任意路径尽量转为工作区相对 posix 路径，与其他附件字段保持一致。

    无法相对化（不在工作区内）时，直接返回原始绝对路径字符串。
    """
    raw = str(raw_path or "").strip()
    if not raw:
        return ""
    try:
        p = Path(raw)
        if p.is_absolute():
            return p.relative_to(Path(CLONOTH_WORKSPACE)).as_posix()
        return p.as_posix()
    except Exception:
        return raw


def _resolve_attachment_path(attachment: Any) -> Optional[Path]:
    """把 Clonoth 附件解析为本地路径，兼容 dict 与字符串路径。"""
    # OneBot 适配层同时接受 dict 和 str，并统一解析 file://、绝对路径和工作区相对路径
    if isinstance(attachment, dict):
        raw_path = attachment.get("original_path") or attachment.get("path") or attachment.get("file")
    elif isinstance(attachment, str):
        raw_path = attachment
    else:
        raw_path = None
    if not raw_path:
        return None
    raw_text = str(raw_path)
    if raw_text.startswith("file://"):
        raw_text = raw_text[7:]
    path = Path(raw_text)
    return path if path.is_absolute() else Path(CLONOTH_WORKSPACE) / path


def _attachment_filename(attachment: Any) -> str:
    """从附件对象中提取展示文件名。"""
    if isinstance(attachment, dict):
        return str(attachment.get("name") or attachment.get("filename") or "")
    if isinstance(attachment, str):
        text = attachment[7:] if attachment.startswith("file://") else attachment
        return Path(text).name
    return ""


async def _send_attachment_path(
    bot: Bot,
    target: Dict[str, Any],
    path: Path,
    filename: str = "",
    *,
    send_context: OutboundSendContext | None = None,
) -> str:
    """Send one attachment under one persistent logical owner claim."""
    display_name = filename or path.name
    if not path.exists():
        raise OneBotSendContractError(f"attachment does not exist: {path}")
    raw_bytes = path.read_bytes()
    context = send_context or OutboundSendContext(
        conversation_key=str(target.get("conversation_key") or "")
    )

    if path.suffix.lower() in _IMAGE_SUFFIXES:
        identity = image_content_identity(raw_bytes)
        context = context.child(identity)
        key = make_idempotency_key(
            target, identity, event_id=context.idempotency_key or context.event_id,
        )
        claim = await _outbound_idempotency.begin(key)
        if not claim.acquired and claim.state == "pending":
            resolved = await _outbound_idempotency.wait_for_resolution(key)
            claim = (
                await _outbound_idempotency.begin(key)
                if resolved is None else IdempotencyClaim(key, False, resolved)
            )
        if not claim.acquired:
            return f"idempotent:{key}"
        heartbeat = asyncio.create_task(_heartbeat_idempotency_claim(claim))
        errors: list[BaseException] = []
        file_path = str(path.resolve())
        encoded = "base64://" + base64.b64encode(raw_bytes).decode("ascii")
        attempts = (
            MessageSegment.image(file=file_path),
            MessageSegment.image(file=encoded),
            MessageSegment.image(file=f"file://{file_path}"),
        )
        fields = _send_audit_fields(
            context, platform_message_id="", attempt=context.attempt,
            idempotency_key=key,
        )
        try:
            async def send_image() -> str:
                for index, message in enumerate(attempts, start=1):
                    try:
                        return await _send_qq_message_once(
                            bot, target, message, idempotency_key=key,
                        )
                    except Exception as exc:
                        classified = classify_send_exception(exc)
                        if classified.ambiguous_ack:
                            raise classified from exc
                        errors.append(classified)
                failure = OneBotAttachmentBatchError(errors)
                logger.warning(
                    "onebot_send_failed",
                    extra={**fields, "retryable": failure.retryable, "send_error": str(failure)},
                )
                raise failure

            message_id = await protected_claim_send(
                _outbound_idempotency,
                claim,
                send_image,
                sent_ttl=_sent_ttl_for_context(context),
                message_id_getter=lambda result: str(result or ""),
            )
            logger.info(
                "onebot_send_sent",
                extra=_send_audit_fields(
                    context, platform_message_id=message_id,
                    attempt=context.attempt, idempotency_key=key,
                ),
            )
            return message_id
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    identity = f"file:sha256:{hashlib.sha256(raw_bytes).hexdigest()}"
    context = context.child(identity)
    key = make_idempotency_key(
        target, identity, event_id=context.idempotency_key or context.event_id,
    )
    claim = await _outbound_idempotency.begin(key)
    if not claim.acquired and claim.state == "pending":
        resolved = await _outbound_idempotency.wait_for_resolution(key)
        claim = (
            await _outbound_idempotency.begin(key)
            if resolved is None else IdempotencyClaim(key, False, resolved)
        )
    if not claim.acquired:
        return f"idempotent:{key}"
    heartbeat = asyncio.create_task(_heartbeat_idempotency_claim(claim))
    fields = _send_audit_fields(
        context, platform_message_id="", attempt=context.attempt,
        idempotency_key=key,
    )
    try:
        file_str = "base64://" + base64.b64encode(raw_bytes).decode("ascii")

        async def upload_file() -> Any:
            if target.get("type") == "group" and target.get("group_id") is not None:
                return await bot.call_api(
                    "upload_group_file", group_id=target["group_id"],
                    file=file_str, name=display_name,
                )
            if target.get("type") == "private" and target.get("user_id") is not None:
                return await bot.call_api(
                    "upload_private_file", user_id=target["user_id"],
                    file=file_str, name=display_name,
                )
            raise OneBotSendContractError(f"attachment target is invalid: {target!r}")

        result = await protected_claim_send(
            _outbound_idempotency,
            claim,
            upload_file,
            sent_ttl=_sent_ttl_for_context(context),
            message_id_getter=_extract_sent_message_id,
        )
        message_id = _extract_sent_message_id(result)
        logger.info(
            "onebot_file_upload_sent",
            extra=_send_audit_fields(
                context, platform_message_id=message_id,
                attempt=context.attempt, idempotency_key=key,
            ),
        )
        return message_id or f"uploaded:{key}"
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat



def _target_forward_kind(target: Dict[str, Any]) -> tuple[str, int] | None:
    """从发送 target 解析出合并转发所需的 (类型, id)。无法解析时返回 None。"""
    ttype = str(target.get("type") or "")
    if ttype == "group" and target.get("group_id") is not None:
        try:
            return "group", int(target["group_id"])
        except (TypeError, ValueError):
            return None
    if ttype == "private" and target.get("user_id") is not None:
        try:
            return "private", int(target["user_id"])
        except (TypeError, ValueError):
            return None
    return None


async def _try_send_images_as_forward(
    bot: Bot,
    target: Dict[str, Any],
    image_attachments: List[Any],
    *,
    send_context: OutboundSendContext | None = None,
) -> bool:
    """尝试把多张图片用合并转发（forward node）一次性发送。

    Why: NapCat/NTQQ 逐张 send_group_msg 发大图时，每张都要 base64 上传并
    等 NTQQ ack，单张常超 baseTimeout(10s) 而报 retcode=1200，多图连发还会
    把 WS 心跳拖断（已在日志中观察到 keepalive ping timeout）。
    How: send_group_forward_msg / send_private_forward_msg 只需一次 API 调用，
    把每张图包成一个 node 一次提交。Purpose: 大幅减少往返与逐张超时。

    返回 True 表示已通过合并转发发出；歧义 ack（可能已送达）抛出，交由既有
    ambiguous claim 机制处理；其余失败返回 False，由调用方回退到逐张直发。
    """
    kind = _target_forward_kind(target)
    if kind is None:
        return False
    forward_type, forward_id = kind
    self_id = getattr(bot, "self_id", "") or "10000"
    nodes: list[dict[str, Any]] = []
    for attachment in image_attachments:
        path = _resolve_attachment_path(attachment)
        if path is None or not path.exists():
            continue
        node = {
            "type": "node",
            "data": {
                "user_id": str(self_id),
                "nickname": _sanitize_name(IMAGE_FORWARD_MERGE_NICKNAME, max_len=32),
                "content": [
                    {"type": "image", "data": {"file": f"file://{str(path.resolve())}"}},
                ],
            },
        }
        nodes.append(node)
    if len(nodes) < 2:
        return False
    batch_identity = "forward:" + hashlib.sha256(
        "|".join(
            image_content_identity((_resolve_attachment_path(att) or Path()).read_bytes())
            for att in image_attachments
            if _resolve_attachment_path(att) is not None
        ).encode("utf-8")
    ).hexdigest()
    context = send_context.child(batch_identity) if send_context else OutboundSendContext()
    key = make_idempotency_key(
        target, batch_identity, event_id=context.idempotency_key or context.event_id,
    )
    claim = await _outbound_idempotency.begin(key)
    if not claim.acquired and claim.state == "pending":
        resolved = await _outbound_idempotency.wait_for_resolution(key)
        if resolved is None:
            claim = await _outbound_idempotency.begin(key)
        else:
            claim = IdempotencyClaim(key, False, resolved)
    if not claim.acquired:
        return True
    heartbeat = asyncio.create_task(_heartbeat_idempotency_claim(claim))
    try:
        call_timeout = min(240.0, max(60.0, len(nodes) * 45.0))

        async def send_forward() -> Any:
            if forward_type == "group":
                return await bot.call_api(
                    "send_group_forward_msg", group_id=forward_id, messages=nodes, _timeout=call_timeout,
                )
            return await bot.call_api(
                "send_private_forward_msg", user_id=forward_id, messages=nodes, _timeout=call_timeout,
            )

        result = await protected_claim_send(
            _outbound_idempotency,
            claim,
            send_forward,
            sent_ttl=_sent_ttl_for_context(context),
            message_id_getter=_extract_sent_message_id,
        )
        message_id = _extract_sent_message_id(result)
        logger.info(
            "onebot_forward_sent",
            extra=_send_audit_fields(
                context, platform_message_id=message_id,
                attempt=context.attempt, idempotency_key=key,
            ),
        )
        if message_id:
            # 合并转发只返回卡片一个 message_id，协议上没有逐张的，整批图都挂在卡片上
            _remember_message_attachments(message_id, list(image_attachments))
        return True
    except Exception as exc:
        classified = classify_send_exception(exc)
        logger.warning(
            "onebot_forward_failed",
            extra={
                **_send_audit_fields(context, platform_message_id="", attempt=context.attempt, idempotency_key=key),
                "retryable": classified.retryable, "send_error": str(classified),
            },
        )
        # 歧义 ack 可能已经送达，回退逐张会双发；其余失败都是真没发出去
        if classified.ambiguous_ack:
            raise classified from exc
        return False
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat


async def _send_attachments(
    bot: Bot,
    target: Dict[str, Any],
    attachments: List[Any],
    *,
    send_context: OutboundSendContext | None = None,
) -> None:
    """发送 Clonoth 返回的附件列表。

    每发出一个附件，就把返回的 message_id -> 本地附件路径 登记下来，
    让用户引用这条（如绘图产物）时能把原图作为附件送回给 AI。
    """
    # [2026-07-17] 多图合并转发优化：当图片张数 ≥ 阈值时，优先用
    # send_group_forward_msg 一次性发送，避免逐张 sendMsg 超时与 WS 拖断。
    if live.enable_image_forward_merge:
        image_atts = [
            att for att in attachments
            if (p := _resolve_attachment_path(att)) is not None
            and p.exists() and p.suffix.lower() in _IMAGE_SUFFIXES
        ]
        non_image_atts = [
            att for att in attachments
            if (p := _resolve_attachment_path(att)) is None
            or not p.exists() or p.suffix.lower() not in _IMAGE_SUFFIXES
        ]
        if len(image_atts) >= live.image_forward_merge_threshold:
            merged = await _try_send_images_as_forward(
                bot, target, image_atts, send_context=send_context,
            )
            if merged:
                # 整批图已登记在卡片 message_id 上；协议上拿不到逐张映射。
                # 非图片附件继续逐个发送。
                if non_image_atts:
                    await _send_attachments_one_by_one(
                        bot, target, non_image_atts, send_context=send_context
                    )
                return
            # 合并转发失败，回退到逐张直发（下方通用循环）。
    await _send_attachments_one_by_one(
        bot, target, attachments, send_context=send_context
    )


async def _send_attachments_one_by_one(
    bot: Bot,
    target: Dict[str, Any],
    attachments: List[Any],
    *,
    send_context: OutboundSendContext | None = None,
) -> None:
    """逐张/逐个发送附件，单个失败不阻断后续。"""
    # [2026-07-17] 批量发图（如一次生 4 张）时，单张 sendMsg 可能耗时较长；
    # 若其中一张抛异常（如 NapCat 超时/WS 短暂断开），旧逻辑会让整个循环
    # 中断，导致后续图片全部丢失（现象：“要 4 张只发出 1 张”）。
    # 这里逐张单独容错，单张失败不阻断后续图片发送。
    total = len(attachments)
    failures: list[BaseException] = []
    for idx, attachment in enumerate(attachments):
        path = _resolve_attachment_path(attachment)
        filename = _attachment_filename(attachment)
        if path is None:
            if filename:
                await _send_qq_message(
                    bot, target, f"Clonoth 生成了文件：{filename}",
                    send_context=(send_context.child(f"unsupported:{filename}") if send_context else None),
                )
            else:
                logger.warning("skip unsupported QQ attachment payload: %r", attachment)
            continue
        try:
            message_id = await _send_attachment_path(
                bot,
                target,
                path,
                filename=filename,
                send_context=send_context,
            )
        except Exception as exc:  # noqa: BLE001
            classified = classify_send_exception(exc)
            failures.append(classified)
            logger.warning(
                "onebot_attachment_failed",
                extra={
                    "attachment_index": idx + 1, "attachment_total": total,
                    "attachment_name": filename or path.name,
                    "retryable": classified.retryable,
                    "event_id": (send_context.event_id if send_context else ""),
                    "attempt": (send_context.attempt if send_context else 1),
                    "idempotency_key": (send_context.idempotency_key if send_context else ""),
                },
            )
            continue
        if message_id:
            _remember_message_attachments(message_id, [attachment])
    if failures:
        raise OneBotAttachmentBatchError(failures)


def _sent_attachment_bucket_key(target: Dict[str, Any]) -> str:
    """为最近发送附件索引生成稳定的会话桶 key。

    优先用 group_id/user_id（Bot 进程内部值），避免 real/stable conversation_key
    不一致导致记录与查询对不上。
    """
    ttype = str(target.get("type") or "")
    if ttype == "group" and target.get("group_id") is not None:
        return f"group:{int(target['group_id'])}"
    if ttype == "private" and target.get("user_id") is not None:
        return f"private:{int(target['user_id'])}"
    conv_key = str(target.get("conversation_key") or "")
    return f"conv:{conv_key}" if conv_key else ""


def _record_sent_attachments(target: Dict[str, Any], attachments: List[Any]) -> None:
    """把 Bot 发出/生成的附件记入会话级最近附件索引，供 qq_forward 后续检索。

    Why: 生图插件产出的图片文件名随机，用户说“把刚才那张图发给 xx”时
    AI 无从得知具体路径。How: 发送时把已解析的本地路径 + 显示名 + 时间戳
    按会话桶缓存。Purpose: 让后续转发能按“最近生成/发送”定位图片。
    """
    bucket = _sent_attachment_bucket_key(target)
    if not bucket or not attachments:
        return
    now = time.time()
    for attachment in attachments:
        path = _resolve_attachment_path(attachment)
        if path is None:
            continue
        try:
            resolved = str(path.resolve())
        except Exception:
            resolved = str(path)
        name = _attachment_filename(attachment) or path.name
        is_image = path.suffix.lower() in _IMAGE_SUFFIXES
        seq = _sent_attachment_seq[bucket] + 1
        _sent_attachment_seq[bucket] = seq
        _recent_sent_attachments[bucket].append({
            "path": resolved,
            "name": name,
            "type": "image" if is_image else "file",
            "timestamp": now,
            "seq": seq,
        })
    _recent_sent_attachments.touch(bucket)


def _remember_message_attachments(message_id: Any, attachments: List[Any]) -> None:
    """登记 Bot 已发出消息的 message_id -> 本地附件路径列表。

    仅记录能解析出本地路径的附件（图片/文件），供用户引用这条消息时回取原图。
    """
    mid = str(message_id or "").strip()
    if not mid or not attachments:
        return
    records: List[Dict[str, Any]] = []
    for attachment in attachments:
        path = _resolve_attachment_path(attachment)
        if path is None:
            continue
        try:
            resolved = str(path.resolve())
        except Exception:
            resolved = str(path)
        name = _attachment_filename(attachment) or path.name
        is_image = path.suffix.lower() in _IMAGE_SUFFIXES
        records.append({
            "path": resolved,
            "name": name,
            "type": "image" if is_image else "file",
        })
    if not records:
        return
    _sent_message_attachments[mid] = records
    _sent_message_attachments.move_to_end(mid)
    while len(_sent_message_attachments) > _MESSAGE_ATTACHMENT_MAX_ITEMS:
        _sent_message_attachments.popitem(last=False)


def _message_attachment_records(message_id: Any, *, only_images: bool = False) -> list[dict[str, Any]]:
    """返回某条 Bot 已发消息登记的本地附件记录（过滤不存在的文件）。"""
    mid = str(message_id or "").strip()
    if not mid:
        return []
    records: list[dict[str, Any]] = []
    for item in _sent_message_attachments.get(mid, ()):  # type: ignore[arg-type]
        if not isinstance(item, dict):
            continue
        if only_images and str(item.get("type") or "") != "image":
            continue
        raw = str(item.get("path") or "").strip()
        if not raw or not Path(raw).exists():
            continue
        records.append(dict(item))
    _touch_attachment_files(records)
    return records


def _recent_sent_attachment_records(bucket_key: str, *, only_images: bool = False) -> list[dict[str, Any]]:
    """返回会话最近发出/生成的附件（旧→新），可选只要图片，并过滤不存在的文件。"""
    bucket = str(bucket_key or "")
    if not bucket:
        return []
    records: list[dict[str, Any]] = []
    for item in _recent_sent_attachments.get(bucket, ()):  # type: ignore[arg-type]
        if not isinstance(item, dict):
            continue
        if only_images and str(item.get("type") or "") != "image":
            continue
        raw = str(item.get("path") or "").strip()
        if not raw or not Path(raw).exists():
            continue
        records.append(dict(item))
    _touch_attachment_files(records)
    return records


# 附件按会话串行发送；锁表按会话桶增长，需要有界。
_ATTACHMENT_SEND_LOCK_MAX_KEYS = 256


@dataclass
class _AttachmentSendLock:
    lock: asyncio.Lock
    users: int = 0


_attachment_send_locks: "OrderedDict[str, _AttachmentSendLock]" = OrderedDict()


@contextlib.asynccontextmanager
async def _attachment_send_order(target: Dict[str, Any]) -> AsyncIterator[None]:
    """按会话桶串行化附件发送，退出时回收空闲锁。"""
    key = _sent_attachment_bucket_key(target) or str(target.get("conversation_key") or "default")
    entry = _attachment_send_locks.get(key)
    if entry is None:
        entry = _AttachmentSendLock(asyncio.Lock())
        _attachment_send_locks[key] = entry
    _attachment_send_locks.move_to_end(key)
    entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.users -= 1
        _prune_attachment_send_locks()


def _prune_attachment_send_locks() -> None:
    """只淘汰没人持有/等待的锁：丢掉在用的锁会让后来者拿到新锁，同会话串行就断了。"""
    overflow = len(_attachment_send_locks) - _ATTACHMENT_SEND_LOCK_MAX_KEYS
    for stale_key, entry in list(_attachment_send_locks.items()):
        if overflow <= 0:
            break
        if entry.users:
            continue
        del _attachment_send_locks[stale_key]
        overflow -= 1


def _ensure_bucket_target(target: Dict[str, Any]) -> Dict[str, Any]:
    """补齐 target 的 group_id/user_id，使最近附件登记 bucket 稳定可查。

    [2026-07-19] 生图（gpt_image_2 / gemini_image）经 emit_intermediate 后台
    推图时，走 send_to_channel fallback，其 target 可能只有 conversation_key
    而缺 group_id/user_id。若不补齐，_sent_attachment_bucket_key 会退化成
    conv:<key> 桶，而 qq_forward op=recent 用 group:<群号>/private:<qq> 桶查询，
    两者对不上导致查不到刚生成的图。这里从 conversation_key 反解出
    group_id/user_id 补进 target，保证登记与查询命中同一个桶。
    """
    ttype = str(target.get("type") or "")
    conv_key = _real_conversation_key(str(target.get("conversation_key") or ""))
    if ttype == "group" and target.get("group_id") is None and conv_key:
        gid = _group_id_from_conversation_key(conv_key)
        if gid is not None:
            target["group_id"] = gid
    if ttype == "private" and target.get("user_id") is None and conv_key:
        uid = _private_user_id_from_conversation_key(conv_key)
        if uid is not None:
            target["user_id"] = uid
    return target


async def _send_attachments_confirmed(
    bot: Bot,
    target: Dict[str, Any],
    attachments: List[Any],
    *,
    send_context: OutboundSendContext | None = None,
) -> None:
    """串行发送附件，并仅在全部平台确认后登记最近附件索引。"""
    try:
        async with _attachment_send_order(target):
            await _send_attachments(
                bot, target, attachments, send_context=send_context
            )
            # Register/finish only after every platform-visible send is confirmed.
            # 清空上下文之前派出去的那轮除外：图照发，但索引不该留在刚清空的会话里。
            if not _is_stale_delivery(target.get("group_id"), send_context):
                _record_sent_attachments(_ensure_bucket_target(dict(target)), attachments)
    except Exception:
        logger.warning("onebot_attachment_batch_failed", exc_info=True)
        raise
    conv_key = target.get("conversation_key")
    if conv_key:
        _mark_qq_reply_finished(str(conv_key))


async def _send_text_and_attachments(
    bot: Bot,
    target: Dict[str, Any],
    text: str,
    attachments: List[Any],
    *,
    source_attachments: List[Dict[str, Any]] | None = None,
    send_context: OutboundSendContext | None = None,
) -> None:
    """统一发送最终文本/附件，并持久绑定回复所依据的来源图片。"""
    conv_key = target.get("conversation_key")
    # 上下文被清空时这一轮还在飞，回复照发（群友问了总得有个答复），但一律不落缓存 ——
    # 它是上一段上下文的产物，落回去下一轮又会被带给模型，等于那次清空没生效。
    stale = target.get("type") == "group" and _is_stale_delivery(target.get("group_id"), send_context)
    if stale:
        logger.info("skip context write-back for group %s: delivery predates the reset", target.get("group_id"))
    # 2026-05-01 修改原因：私聊没有群历史缓存，不应写入 _group_history；群聊
    # 仍保留原来的 Bot 回复入库逻辑，维持后续 @Bot 请求的上下文连续性。
    if text:
        if await _send_split_text(
            bot,
            target,
            text,
            source_attachments=source_attachments,
            send_context=send_context,
        ) and target.get("type") == "group":
            group_id = target.get("group_id")
            if group_id is not None and not stale:
                _record_bot_reply(int(group_id), text)
    if attachments:
        # Delivery completion means the platform-confirmed final attachment send,
        # not merely scheduling a background task.  Failures propagate to the SDK
        # so its outbox cannot acknowledge/consume the trigger prematurely.
        await _send_attachments_confirmed(
            bot, target, attachments, send_context=send_context
        )
        return
    if conv_key:
        _mark_qq_reply_finished(str(conv_key))


async def _set_message_react(bot: Bot, event: Any, emoji_id: str, enabled: bool) -> bool:
    """设置或移除触发消息上的 QQ React，失败时静默返回 False。"""
    # 2026-05-03 修改原因：React 阶段切换需要多处复用 OneBot 扩展 API。
    # 做法是把单次 set_msg_emoji_like 调用包进独立函数，并强制 emoji_id 为 str；
    # 目的在于满足每次 API 调用都单独容错，避免 React 失败影响消息回复。
    if not live.enable_reactions or not bot or not event or not hasattr(event, "message_id"):
        return False
    try:
        await bot.call_api(
            "set_msg_emoji_like",
            message_id=int(event.message_id),
            emoji_id=str(emoji_id),
            set=enabled,
        )
        return True
    except Exception:
        return False


def _progress_mentions_search(record: str) -> bool:
    """判断进度记录是否与联网搜索工具相关。"""
    text = str(record or "").lower()
    return any(keyword in text for keyword in _SEARCH_PROGRESS_KEYWORDS)


async def _maybe_send_search_progress_notice(
    bot: Bot,
    target: Dict[str, Any],
    platform_data: Dict[str, Any],
) -> None:
    """在 QQ 侧为长搜索任务发送低频可见进度提示。至多两条：开工一条，久等一条。"""
    now = time.time()
    # 答案已经在投递路上就闭嘴：两者同一秒并发时，「还在搜索」会紧贴着结果出现。
    if platform_data.get("_qq_final_reply_sent"):
        return
    request_identity = str(
        platform_data.setdefault("_qq_search_request_identity", uuid.uuid4().hex)
    )
    context = _request_send_context(
        "search-progress", request_identity,
        str(target.get("conversation_key") or ""),
    )
    if not platform_data.get("_qq_search_notice_sent"):
        platform_data["_qq_search_notice_sent"] = True
        platform_data["_qq_search_notice_started_at"] = now
        try:
            await _send_qq_message(
                bot, target, _SEARCH_PROGRESS_FIRST_NOTICE,
                send_context=context.child("first"),
            )
        except Exception:
            logger.debug("send QQ search progress notice failed", exc_info=True)
        return

    if platform_data.get("_qq_search_still_notice_sent"):
        return
    started_at = float(platform_data.get("_qq_search_notice_started_at") or now)
    waited = now - started_at
    if waited < _SEARCH_PROGRESS_STILL_RUNNING_DELAY_SEC:
        return
    platform_data["_qq_search_still_notice_sent"] = True
    try:
        await _send_qq_message(
            bot, target,
            _SEARCH_PROGRESS_STILL_RUNNING_NOTICE.format(waited=int(waited)),
            send_context=context.child("still"),
        )
    except Exception:
        logger.debug("send QQ search still-running notice failed", exc_info=True)

class TangQiuCallbacks:
    """Clonoth SDK 的 QQ 平台回调实现。

    发送最终回复、附件和主节点中间回复；其余进度、审批、typing、子任务日志均静默。
    """

    async def send_reply(
        self,
        trigger: TriggerInfo,
        text: str,
        attachments: List[Dict[str, Any]],
        *,
        main_state: Optional[MainTaskState] = None,
        delivery_context: OutboundSendContext | None = None,
        **callback_data: Any,
    ) -> None:
        """发送主节点最终回复。"""
        platform_data = trigger.platform_data
        bot = platform_data.get("bot") or _get_fallback_bot()
        target = _target_from_platform_data(platform_data) or _target_from_conversation_key(trigger.conversation_key)
        if not bot:
            raise OneBotSendContractError(
                f"send_reply missing bot for session={trigger.session_id}"
            )
        if not target:
            raise OneBotSendContractError(
                f"send_reply missing target for session={trigger.session_id}"
            )
        # 进度提示靠这个标记闭嘴。设在校验之后：发不出去的回复不该顺带把提示也堵掉。
        platform_data["_qq_final_reply_sent"] = True
        send_context = delivery_context or context_from_sources(
            trigger=trigger,
            main_state=main_state,
            platform_data=callback_data.get("platform_data"),
        )
        # 提取 [REACT:ID] 标记
        final_text = text or ""
        if final_text:
            from .emoji_handler import _extract_reactions
            final_text, reactions = _extract_reactions(final_text)
            if reactions:
                await self.add_reactions(trigger, reactions)
        source_attachments = platform_data.get("_source_attachments")
        if not isinstance(source_attachments, list):
            source_attachments = []
        await _send_text_and_attachments(
            bot,
            target,
            final_text,
            attachments or [],
            source_attachments=source_attachments,
            send_context=send_context,
        )

    async def send_reply_attachment(self, session_id: str, path: str, *args: Any, **kwargs: Any) -> None:
        """兼容旧式 session_id 附件回调；当前 SDK 通常把附件放在 send_reply 中。"""
        target = _session_targets.get(session_id) or _persisted_session_targets.get(session_id)
        if not target:
            raise OneBotSendContractError(
                f"send_reply_attachment missing target for session={session_id}"
            )
        bot = target.get("bot") or _get_fallback_bot()
        if not bot:
            raise OneBotSendContractError(
                f"send_reply_attachment missing bot for session={session_id}"
            )
        await _send_attachment_path(bot, target, Path(path))

    async def send_intermediate_reply(
        self,
        trigger: TriggerInfo,
        text: str,
        *,
        delivery_context: OutboundSendContext | None = None,
        **callback_data: Any,
    ) -> None:
        """发送主节点中间回复，但不写入群历史。"""
        if not text:
            return
        platform_data = trigger.platform_data
        bot = platform_data.get("bot") or _get_fallback_bot()
        target = _target_from_platform_data(platform_data) or _target_from_conversation_key(trigger.conversation_key)
        if not bot:
            raise OneBotSendContractError(
                f"send_intermediate_reply missing bot for session={trigger.session_id}"
            )
        if not target:
            raise OneBotSendContractError(
                f"send_intermediate_reply missing target for session={trigger.session_id}"
            )
        send_context = delivery_context or context_from_sources(
            trigger=trigger,
            platform_data=callback_data.get("platform_data"),
        )
        # 提取 [REACT:ID] 标记
        from .emoji_handler import _extract_reactions
        text, reactions = _extract_reactions(text)
        if reactions:
            await self.add_reactions(trigger, reactions)
        if text:
            source_attachments = platform_data.get("_source_attachments")
            if not isinstance(source_attachments, list):
                source_attachments = []
            if await _send_split_text(
                bot,
                target,
                text,
                source_attachments=source_attachments,
                send_context=send_context,
            ):
                conv_key = target.get("conversation_key")
                if conv_key:
                    _mark_qq_reply_finished(str(conv_key))

    async def send_to_channel(
        self,
        conversation_key: str,
        text: str,
        attachments: List[Dict[str, Any]],
        *,
        node_id: str = "",
        delivery_context: OutboundSendContext | None = None,
        **callback_data: Any,
    ) -> None:
        """处理没有 trigger 的 fallback 最终输出。"""
        target = _target_from_conversation_key(conversation_key)
        if target is None:
            raise OneBotSendContractError(
                f"send_to_channel missing target for conversation={conversation_key}"
            )
        # [2026-07-19] 修复生图 op=recent 查不到图：_target_from_conversation_key
        # 只返回 {type, group_id/user_id}，缺 conversation_key，导致
        # _send_attachments_confirmed 的成功登记链路信息不全。
        # 这里补回 conversation_key（用真实会话 key），让附件登记 bucket 与
        # qq_forward op=recent 的查询 bucket 保持一致。
        if not target.get("conversation_key"):
            target["conversation_key"] = _real_conversation_key(conversation_key)
        bot = _conversation_bots.get(conversation_key) or _get_fallback_bot()
        if not bot:
            raise OneBotSendContractError(
                f"send_to_channel missing bot for conversation={conversation_key}"
            )
        send_context = delivery_context or context_from_sources(
            platform_data=callback_data.get("platform_data"),
            conversation_key=conversation_key,
        )
        await _send_text_and_attachments(
            bot,
            target,
            text or "",
            attachments or [],
            send_context=send_context,
        )

    async def delete_status_message(self, trigger: TriggerInfo) -> None:
        return None

    async def edit_status_message(self, trigger: TriggerInfo, content: str) -> None:
        return None

    async def update_progress(self, trigger: TriggerInfo, state: MainTaskState) -> None:
        """根据主任务进度切换触发消息上的 React 表情。"""
        # 2026-05-03 修改原因：EventRouter 不展示 QQ 进度文本，但 QQ 侧需要补齐
        # React 链中的 LLM 推理和工具阶段。做法是读取 MainTaskState.stream_parts
        # 与 progress_records：首次流式文本进入 thinking，真实工具执行进度进入 tool；
        # 目的在于复用 SDK sweep 回调并用阶段元数据避免高频 stream_delta 抖动。
        platform_data = trigger.platform_data
        event = platform_data.get("event")
        if not event or not hasattr(event, "message_id"):
            return
        bot = platform_data.get("bot") or _get_fallback_bot()
        if not bot:
            return

        target = _target_from_platform_data(platform_data) or _target_from_conversation_key(trigger.conversation_key)
        has_tool_progress = any("执行" in record and "个工具" in record for record in state.progress_records)
        # 只看最新一条：progress_records 是累积的，用 any() 扫全量的话，搜索工具跑完之后
        # 那条记录还留着，条件就永远成立 —— 后面明明在跑别的命令，报的还是「还在联网搜索中」。
        has_search_progress = bool(state.progress_records) and _progress_mentions_search(
            state.progress_records[-1]
        )
        if has_tool_progress or has_search_progress:
            if has_search_progress and target:
                await _maybe_send_search_progress_notice(bot, target, platform_data)
            return

    async def create_child_progress(
        self,
        task_key: str,
        state: ChildTaskState,
        *,
        trigger: Optional[TriggerInfo] = None,
        conversation_key: str = "",
        session_id: str = "",
    ) -> None:
        return None

    async def update_child_progress(self, task_key: str, state: ChildTaskState) -> None:
        return None

    async def finalize_child_progress(
        self,
        task_key: str,
        state: ChildTaskState,
        status: str,
        *,
        is_dm: bool = False,
    ) -> None:
        return None

    async def show_approval_ui(
        self,
        approval_id: str,
        operation: str,
        details: Dict[str, Any],
        *,
        conversation_key: str = "",
        session_id: str = "",
    ) -> None:
        """QQ 端审批必须由管理员私聊确认；不再自动放行。"""
        if _client is None:
            logger.warning(
                "approval review skipped on QQ: client unavailable id=%s operation=%s",
                approval_id,
                operation,
            )
            return

        _prune_expired_approvals()
        _pending_approvals[approval_id] = {
            "operation": operation,
            "details": dict(details or {}),
            "conversation_key": conversation_key,
            "session_id": session_id,
            "created_at": time.time(),
        }

        # 档位关掉和名单为空是同一种局面：没有任何人能作出决定，挂着只会等到超时。
        approval_off = _grant_of("approval") == capability.OFF
        if approval_off or not live.admin_users:
            why = (
                "permissions.capabilities.approval is off"
                if approval_off
                else "CLONOTH_ADMIN_QQ_USERS is not configured"
            )
            try:
                await _client.approve(
                    approval_id,
                    decision="deny",
                    comment=f"QQ adapter denied because {why}.",
                )
            except Exception:
                logger.exception("approval deny failed on QQ: id=%s operation=%s", approval_id, operation)
            _pending_approvals.pop(approval_id, None)
            _record_settled_approval(approval_id, admin_id=0, decision="deny", operation=operation)
            logger.warning("approval denied on QQ: %s id=%s operation=%s", why, approval_id, operation)
            return

        bot = _get_fallback_bot()
        if not bot:
            logger.warning("approval review pending but no QQ bot available: id=%s operation=%s", approval_id, operation)
            return

        summary = _approval_summary(approval_id, operation, details or {})
        delivered = 0
        for admin_id in live.admin_users:
            try:
                approval_context = _request_send_context(
                    "approval-admin", f"{approval_id}:{admin_id}", conversation_key,
                )
                sent_mid = await _send_qq_message(
                    bot, {"type": "private", "user_id": int(admin_id)}, summary,
                    send_context=approval_context,
                )
                delivered += 1
                # 记录审批消息 id -> 卡片锚点，使管理员可直接引用该消息回复“审批同意/拒绝”。
                _remember_approval_message(sent_mid, approval_id, int(admin_id))
            except Exception:
                logger.exception("send approval request to QQ admin failed: admin=%s id=%s", admin_id, approval_id)

        target = _target_from_conversation_key(conversation_key)
        if target:
            try:
                await _send_qq_message(
                    bot, target, "当前操作需要 Clonoth 管理员审批，已提交等待处理。",
                    send_context=_request_send_context(
                        "approval", approval_id, conversation_key,
                    ),
                )
            except Exception:
                logger.debug("send approval pending notice failed: id=%s", approval_id, exc_info=True)

        logger.info(
            "approval pending on QQ: id=%s operation=%s admins_notified=%s",
            approval_id,
            operation,
            delivered,
        )

    async def refresh_typing(self, trigger: TriggerInfo) -> None:
        return None

    async def add_reactions(self, trigger: TriggerInfo, reactions: List[str]) -> None:
        """按白名单在触发消息上贴模型表态；开关与 API 容错由 _set_message_react 统一负责。"""
        platform_data = trigger.platform_data
        bot = platform_data.get("bot") or _get_fallback_bot()
        event = platform_data.get("event")
        if not bot or not event:
            return
        # 提示词里那句「一条回复最多一个」只是请求：模型能一次写好几个标记，中间回复和
        # 最终回复还会各解析一次。按触发消息记账，贴上一个就不再贴。
        if platform_data.get("_react_done"):
            return
        for emoji_id in reactions:
            eid = str(emoji_id or "").strip()
            if eid not in _REACT_MODEL_EMOJIS:
                continue
            if await _set_message_react(bot, event, eid, True):
                platform_data["_react_done"] = True
            break

    async def on_task_created(self, trigger: TriggerInfo, task_id: str) -> None:
        return None

    async def on_restart_signal(self, conversation_key: str) -> None:
        return None

    async def on_context_reset(self, conversation_key: str, reason: str, cleaned_triggers: List[TriggerInfo]) -> None:
        """上下文重置时同步清理 QQ 侧的上下文副本并重置高水位。"""
        target = _target_from_conversation_key(conversation_key)
        if not target:
            return
        # compact 只是把早期原文换成摘要，缓存要留着：重置水位后下一轮重发这 20 行，
        # 是把摘要里丢掉的近期细节补回来最便宜的办法。clear 则连缓存一起丢。
        if reason != "compact":
            _purge_conversation_side_state(conversation_key, target)
        group_id = target.get("group_id")
        if target.get("type") == "group" and group_id is not None:
            _reset_group_history_watermark(int(group_id))
            if reason != "compact":
                # compact 不清缓存，也就没有「旧产物落回空缓存」这回事。
                _raise_context_clear_barrier(int(group_id), cleaned_triggers)

    async def on_engine_restarted(self, payload: Dict[str, Any]) -> None:
        """Engine 重启后无法确认各群历史是否还在 engine 侧，全部重置以重发一次完整历史。"""
        # 序号由 supervisor 分配，重启后不保证还在原来那条线上；留着旧门槛会把之后
        # 每一条正常回复都判成过期。此时在飞的任务也已经全没了，门槛没有意义。
        # 放在 _session_state 守卫之前：这件事跟水位存不存在无关。
        _context_clear_barrier.clear()
        if _session_state is None:
            return
        for group_id in list(_group_history.keys()):
            _reset_group_history_watermark(int(group_id))

    async def on_task_complete(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def on_task_fail(self, *args: Any, **kwargs: Any) -> None:
        return None


# ---------------------------------------------------------------------------
#  QQ 自然语言转发 Bridge Server
#
#  Why: 让 AI（qq.orchestrator 节点）能用自然语言“帮我把上面聊到的 xxx 私发给我 /
#  合并转发到群 xxx / 转发这张图给 xx / 提醒 xx 明天带笔记本”，并支持多选多条聊天
#  消息一次性合并转发。
#  How: qq_forward 外部工具在 Engine 子进程中运行，经本地 HTTP Bridge 调用运行在
#  QQ Bot 进程内的以下处理器；真正的 QQ 群号/QQ 号只留在 Bot 进程，模型上下文只看到
#  匿名下标/关键词，避免把副作用能力和真实目标暴露给普通模型。
# ---------------------------------------------------------------------------

_forward_bridge_runner: Any = None
_forward_bridge_site: Any = None
_forward_bridge_started: bool = False
_forward_bridge_token_cache: str = ""


def _forward_bridge_token() -> str:
    """返回 Bridge 令牌：env 优先，否则读/自签令牌文件；都拿不到返回空串，Bridge 不启动。"""
    global _forward_bridge_token_cache
    if FORWARD_BRIDGE_TOKEN:
        return FORWARD_BRIDGE_TOKEN
    if _forward_bridge_token_cache:
        return _forward_bridge_token_cache
    path = Path(FORWARD_BRIDGE_TOKEN_FILE)
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    except Exception:
        logger.error("qq_forward bridge token 文件不可读：%s", FORWARD_BRIDGE_TOKEN_FILE)
        return ""
    if existing:
        _forward_bridge_token_cache = existing
        return existing
    try:
        token = secrets.token_urlsafe(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(token, encoding="utf-8")
        os.replace(tmp, path)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    except Exception:
        logger.error("qq_forward bridge token 文件不可写：%s", FORWARD_BRIDGE_TOKEN_FILE)
        return ""
    _forward_bridge_token_cache = token
    return token


def _forward_bridge_lookup_target(session_id: str) -> Dict[str, Any] | None:
    """在内存/持久化两张表中按单个 session_id 查找目标。"""
    sid = str(session_id or "").strip()
    if not sid:
        return None
    for source in (_session_targets, _persisted_session_targets):
        target = source.get(sid)
        if isinstance(target, dict) and target:
            return dict(target)
    return None


def _forward_bridge_session_target(
    session_id: str,
    *,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> Dict[str, Any] | None:
    """根据 Engine 传来的 session_id 找回该会话对应的真实 QQ 目标。

    2026-07-14 修复原因：入口任务实际运行在 entry branch session（branch_xxx），
    ctx.session_id 为 branch，而 _session_targets / _persisted_session_targets 只按
    用户可见的 parent session 登记。若直接拿 branch 去查会 miss，导致
    op=remind + target_type=current 在群聊中误报“当前会话不是群聊”。
    做法：依次尝试 parent_session_id -> 传入 session_id -> runtime_session_id，
    任一命中即返回，对旧调用（仅传 session_id）完全兼容。
    """
    for candidate in (parent_session_id, session_id, runtime_session_id):
        target = _forward_bridge_lookup_target(candidate)
        if target:
            return target
    return None


def _forward_bridge_origin_group_id(
    session_id: str,
    *,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> int | None:
    target = _forward_bridge_session_target(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    if not target:
        return None
    if str(target.get("type") or "") == "group" and target.get("group_id") is not None:
        try:
            return int(target.get("group_id"))
        except Exception:
            return None
    return None


def _forward_bridge_origin_user_id(
    session_id: str,
    *,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> int | None:
    """当前会话触发者 QQ 号，用于把内容“私发给我”。"""
    target = _forward_bridge_session_target(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    if not target:
        return None
    if target.get("user_id") is not None:
        try:
            return int(target.get("user_id"))
        except Exception:
            return None
    return None


def _forward_bridge_requester(
    session_id: str,
    *,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> capability.Requester:
    """转发桥的请求者身份。桥自己没有身份，发起人来自 session 归属。

    group_role 留空：拿群角色要多跑一次 get_group_member_info，而这条路上的
    cross_session 最宽只给到名单，群角色不参与判定。
    """
    uid = _forward_bridge_origin_user_id(
        session_id, parent_session_id=parent_session_id, runtime_session_id=runtime_session_id,
    )
    return capability.Requester(
        user_id=uid,
        listed_admin=uid is not None and _is_admin_user(uid),
        group_id=_forward_bridge_origin_group_id(
            session_id, parent_session_id=parent_session_id, runtime_session_id=runtime_session_id,
        ),
    )


def _forward_bridge_record_ref(record: GroupContentRecord) -> str:
    """群消息的稳定标识：deque 淘汰后位置下标会整体左移，只有 seq 不变。"""
    return f"m{record.seq}" if record.seq > 0 else ""


def _forward_bridge_attachment_ref(record: Mapping[str, Any]) -> str:
    """最近附件的稳定标识：新附件入队和缺文件过滤都会让位置下标漂移，只有 seq 不变。"""
    seq = int(record.get("seq") or 0)
    return f"a{seq}" if seq > 0 else ""


def _forward_bridge_parse_refs(raw: Any, prefix: str) -> list[int]:
    """把模型给的 ref 列表容错解析成 seq：接受 "m41"/"#m41"/"41"/41，去空白、去前导 # 与前缀字母，去重保序，解析不出的丢弃。"""
    if not isinstance(raw, (list, tuple)):
        return []
    seen: set[int] = set()
    out: list[int] = []
    for item in raw:
        token = str(item).strip().lstrip("#").strip()
        if token[:1].lower() == prefix:
            token = token[1:].strip()
        try:
            value = int(token)
        except (TypeError, ValueError):
            continue
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _forward_bridge_parse_record_refs(raw: Any) -> list[int]:
    """群消息 ref（m<seq>）的容错解析。"""
    return _forward_bridge_parse_refs(raw, "m")


def _forward_bridge_parse_attachment_refs(raw: Any) -> list[int]:
    """最近附件 ref（a<seq>）的容错解析。"""
    return _forward_bridge_parse_refs(raw, "a")


def _forward_bridge_read_origin(
    session_id: str,
    *,
    parent_session_id: str = "",
    runtime_session_id: str = "",
    op: str,
) -> tuple[Dict[str, Any] | None, str]:
    """读接口（list/recent）统一鉴门：来源必须可解析且仍在白名单内，否则给出拒绝文案。

    返回 (target, "") 放行并写审计日志（只记匿名别名）；(None, reason) 拒绝。
    """
    target = _forward_bridge_session_target(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    if not target:
        return None, "无法定位来源会话，请在 QQ 会话内使用该工具。"
    ttype = str(target.get("type") or "")
    if ttype == "group":
        try:
            gid = int(target.get("group_id"))
        except (TypeError, ValueError):
            return None, "无法定位来源会话，请在 QQ 会话内使用该工具。"
        # 群移出白名单后内存里的历史仍在，读接口必须重新过一次白名单
        if not _is_group_allowed(gid):
            logger.warning(
                "qq_forward_read denied: group not allowed (op=%s origin=%s)",
                op, _anonymize_group_id(gid),
            )
            return None, "当前群不在允许列表中，无法读取历史。"
        logger.info(
            "qq_forward_read",
            extra={"op": op, "origin_type": "group", "origin": _anonymize_group_id(gid)},
        )
        return target, ""
    if ttype == "private":
        try:
            uid = int(target.get("user_id"))
        except (TypeError, ValueError):
            return None, "无法定位来源会话，请在 QQ 会话内使用该工具。"
        if not _is_private_origin_allowed(uid):
            logger.warning(
                "qq_forward_read denied: private not allowed (op=%s origin=%s)",
                op, _anonymize_user_id(uid),
            )
            return None, "当前私聊不在允许列表中。"
        logger.info(
            "qq_forward_read",
            extra={"op": op, "origin_type": "private", "origin": _anonymize_user_id(uid)},
        )
        return target, ""
    return None, "无法定位来源会话，请在 QQ 会话内使用该工具。"


def _forward_bridge_list_messages(
    session_id: str,
    query: str = "",
    limit: int = 30,
    *,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> list[dict[str, Any]]:
    """列出当前会话（群）最近消息，供 AI 按 ref/关键词多选转发。

    每条带一个稳定 ref（m<seq>）；只包含匿名后可展示给模型的字段。
    """
    group_id = _forward_bridge_origin_group_id(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    if group_id is None:
        return []
    records = list(_group_content_records.get(int(group_id), ()))
    if not records:
        return []
    query_norm = str(query or "").strip().lower()
    limit = max(1, min(int(limit or 30), live.forward_bridge_max_messages))
    items: list[dict[str, Any]] = []
    for record in records:
        ref = _forward_bridge_record_ref(record)
        if not ref:
            # 拿不到稳定标识（seq<=0，多为热更新前的旧 record）就不给模型挑。
            continue
        line = record.formatted_line or ""
        text = record.text or ""
        if query_norm and query_norm not in line.lower() and query_norm not in text.lower():
            continue
        att_kinds = sorted({str(att.get("type") or "") for att in (record.attachments or []) if isinstance(att, dict)})
        items.append({
            "ref": ref,
            "preview": _anonymize_text_for_ai(_compact_text(line or text, limit=200)),
            "sender": _anonymize_text_for_ai(record.sender_name or ""),
            "has_image": "image" in att_kinds,
            "has_file": "file" in att_kinds,
        })
    # 只回传最近 limit 条，保留时间顺序（旧→新）。
    return items[-limit:]


def _forward_bridge_origin_bucket_key(
    session_id: str,
    *,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> str:
    """把来源会话映射为“最近发送/生成附件”索引的桶 key。"""
    target = _forward_bridge_session_target(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    if not target:
        return ""
    return _sent_attachment_bucket_key(target)


def _forward_bridge_list_recent(
    session_id: str,
    *,
    only_images: bool = True,
    limit: int = 20,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> list[dict[str, Any]]:
    """列出本会话最近由 Bot 发出/生成的附件（如生图产出的图片），带稳定 ref（a<seq>）。"""
    bucket = _forward_bridge_origin_bucket_key(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    records = _recent_sent_attachment_records(bucket, only_images=only_images)
    limit = max(1, min(int(limit or 20), live.forward_bridge_max_messages))
    records = records[-limit:]
    items: list[dict[str, Any]] = []
    for rec in records:
        ref = _forward_bridge_attachment_ref(rec)
        if not ref:
            continue
        items.append({
            "ref": ref,
            "name": _sanitize_name(rec.get("name") or Path(str(rec.get("path") or "")).name, max_len=60),
            "type": str(rec.get("type") or "file"),
        })
    return items


def _forward_bridge_pick_recent_attachments(
    session_id: str,
    *,
    refs: list[int] | None,
    only_images: bool = False,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> list[dict[str, Any]]:
    """按 ref（附件 seq）从最近发送/生成附件里挑选；无 ref 时默认取最新一个。

    给了 refs 却全落空 → 返回 []：退回最新一张会把没人要的图发出去。
    返回可直接发送的附件 dict（带绝对 path + name + type）。
    """
    bucket = _forward_bridge_origin_bucket_key(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    records = _recent_sent_attachment_records(bucket, only_images=only_images)
    if not records:
        return []
    selected: list[dict[str, Any]]
    if refs:
        by_seq = {int(rec.get("seq") or 0): rec for rec in records if int(rec.get("seq") or 0) > 0}
        selected = [by_seq[seq] for seq in refs if seq in by_seq]
        if not selected:
            return []
    else:
        selected = [records[-1]]
    attachments: list[dict[str, Any]] = []
    for rec in selected:
        attachments.append({
            "type": str(rec.get("type") or "file"),
            "path": str(rec.get("path") or ""),
            "name": str(rec.get("name") or ""),
        })
    return attachments


def _forward_bridge_pick_records(
    session_id: str,
    *,
    refs: list[int] | None,
    query: str,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> ForwardSelection:
    """按 ref（群历史 seq）或关键词挑选要转发的群消息。

    给了 refs 却一条都没命中 → 带 error 的空选择；部分命中 → records 带命中项、
    missing_refs 记未命中项。没给 refs 时保留 query → 最近 N 条两级回退。
    """
    group_id = _forward_bridge_origin_group_id(
        session_id,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    if group_id is None:
        return ForwardSelection([])
    records = list(_group_content_records.get(int(group_id), ()))
    if refs:
        by_seq = {r.seq: r for r in records if r.seq}
        picked: list[GroupContentRecord] = []
        missing: list[str] = []
        for seq in refs:
            record = by_seq.get(seq)
            if record is not None:
                picked.append(record)
            else:
                missing.append(f"m{seq}")
        if not picked:
            # ref 给了却一条都没命中就报错：回退到最近 N 条会把没人要的消息发给别人。
            return ForwardSelection([], tuple(missing), "所选消息已不在缓存中，请重新 op=list 并用返回的 ref 挑选。")
        return ForwardSelection(picked, tuple(missing))
    if not records:
        return ForwardSelection([])
    query_norm = str(query or "").strip().lower()
    if query_norm:
        matched = [r for r in records if query_norm in (r.formatted_line or "").lower() or query_norm in (r.text or "").lower()]
        if matched:
            return ForwardSelection(matched[-live.forward_bridge_max_messages:])
    return ForwardSelection(records[-live.forward_bridge_max_messages:])


async def _forward_bridge_resolve_target(
    bot: Bot,
    session_id: str,
    target_type: str,
    target_ref: str,
    *,
    parent_session_id: str = "",
    runtime_session_id: str = "",
) -> tuple[ProactiveTarget | None, str]:
    """把工具给出的目标（含 self/current 语义）解析为真实 ProactiveTarget。"""
    target_type = str(target_type or "").strip().lower()
    ref = str(target_ref or "").strip()
    if target_type == "self" or ref in {"我", "自己", "me", "self"}:
        uid = _forward_bridge_origin_user_id(
            session_id,
            parent_session_id=parent_session_id,
            runtime_session_id=runtime_session_id,
        )
        if uid is None:
            return None, "无法确定当前用户，无法私发给你。"
        # 用户被移出私聊白名单后不应还能借 Bot 身份私发给他，与 current 分支同款复核。
        if not _is_private_origin_allowed(uid):
            return None, "当前私聊不在允许列表中。"
        return ProactiveTarget("private", uid, _qq_profile_display_name(uid) or _anonymize_user_id(uid)), ""
    if target_type == "current" or ref in {"当前群", "本群", "这个群", "此群"}:
        gid = _forward_bridge_origin_group_id(
            session_id,
            parent_session_id=parent_session_id,
            runtime_session_id=runtime_session_id,
        )
        if gid is None:
            return None, "当前会话不是群聊，无法发送到本群。"
        if not _is_group_allowed(gid):
            return None, "当前群不在允许的主动群聊目标中。"
        return ProactiveTarget("group", gid, "当前群"), ""
    # 其余交给既有的目标解析（支持显式 id 前缀与配置的显示名/别名）。
    resolve_type = "group" if target_type == "group" else "private"
    return await _resolve_proactive_target(
        bot, _forward_bridge_requester(
            session_id,
            parent_session_id=parent_session_id,
            runtime_session_id=runtime_session_id,
        ),
        resolve_type, ref, capability_key="cross_session",
    )


async def _forward_bridge_records_to_nodes(
    bot: Bot,
    records: list[GroupContentRecord],
    *,
    include_images: bool,
    include_files: bool,
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for record in records[:live.forward_bridge_max_messages]:
        text = record.text or record.formatted_line or ""
        attachments: list[dict[str, Any]] = []
        if include_images or include_files:
            attachments = _filter_attachments_by_kind(
                record.attachments or [],
                include_images=include_images,
                include_files=include_files,
            )
        node = _make_forward_node(
            bot,
            text,
            attachments,
            nickname=record.sender_name or "转发消息",
            user_id=record.sender_id or getattr(bot, "self_id", None),
        )
        if node:
            nodes.append(node)
    return nodes


def _forward_bridge_resolve_files(raw_paths: Any, display_names: Any = None) -> tuple[list[dict[str, Any]], list[str]]:
    """把工具传来的仓库相对/绝对路径解析为可发送的 file 附件。

    Why: 管理员“把仓库里的 xx 文件发出来”需要把工作区内文件发到 QQ。
    How: 复用 _attachment_path_under_workspace 限定在工作区内，防止目录穿越。
    Purpose: 只允许发送工作区内存在的普通文件，逐个回报错误。
    """
    paths: list[str] = []
    if isinstance(raw_paths, str):
        paths = [raw_paths]
    elif isinstance(raw_paths, list):
        paths = [str(item) for item in raw_paths if str(item).strip()]
    names: list[str] = []
    if isinstance(display_names, str):
        names = [display_names]
    elif isinstance(display_names, list):
        names = [str(item) for item in display_names]

    attachments: list[dict[str, Any]] = []
    errors: list[str] = []
    for idx, raw in enumerate(paths):
        raw = str(raw or "").strip()
        if not raw:
            continue
        path = _attachment_path_under_workspace(raw)
        if path is None:
            errors.append(f"{raw}：路径必须位于 Clonoth 工作区内。")
            continue
        if not path.exists() or not path.is_file():
            errors.append(f"{raw}：文件不存在。")
            continue
        rel_path = str(path)
        try:
            rel_path = str(path.relative_to(Path(CLONOTH_WORKSPACE).resolve()))
        except Exception:
            pass
        # 黑名单按工作区相对路径判定，故必须在 relative_to 之后。
        deny_reason = forward_file_deny_reason(rel_path)
        if deny_reason:
            logger.warning("qq_forward blocked sensitive file: %s", rel_path)
            errors.append(deny_reason)
            continue
        display_name = names[idx].strip() if idx < len(names) and str(names[idx]).strip() else path.name
        attachments.append({"type": "file", "path": rel_path, "name": display_name})
    return attachments, errors


async def _forward_bridge_execute(payload: dict[str, Any]) -> dict[str, Any]:
    """执行一次自然语言转发/发送/提醒请求。仅在 Bot 进程内运行。

    The payload is mutated with ``_request_identity`` once. Re-entering the same
    request object or retrying with the same caller request_id is stable; a future
    independent request receives a new identity even when content is identical.
    """
    request_identity = str(
        payload.get("_request_identity")
        or payload.get("request_id")
        or payload.get("idempotency_key")
        or ""
    ).strip()
    if not request_identity:
        request_identity = uuid.uuid4().hex
    payload.setdefault("_request_identity", request_identity)
    try:
        bot = get_bot()
    except Exception:
        return {"ok": False, "error": "QQ Bot 尚未连接，无法执行转发。"}
    session_id = str(payload.get("session_id") or "").strip()
    # [2026-07-14] 入口任务运行在 branch session 上，因此工具会额外透传 parent/runtime
    # session id；这里全部收集下来供 target 解析做多级回退。
    parent_session_id = str(payload.get("parent_session_id") or "").strip()
    runtime_session_id = str(payload.get("runtime_session_id") or "").strip()
    action = str(payload.get("action") or "forward").strip().lower()
    target_type = str(payload.get("target_type") or "").strip().lower()
    target_ref = str(payload.get("target_ref") or "").strip()
    extra_text = _truncate_qq_text(str(payload.get("text") or "").strip())
    query = str(payload.get("query") or "").strip()
    include_images = bool(payload.get("include_images", True))
    include_files = bool(payload.get("include_files", True))
    if payload.get("message_refs") is None and payload.get("message_indices") is not None:
        # 位置下标和 seq 都是合法整数，静默当 ref 会选错消息发给第三方，宁可让模型重跑 op=list。
        return {"ok": False, "error": "message_indices 已不再支持，请重新 op=list 并用返回的 ref 填 message_refs。"}
    refs = _forward_bridge_parse_record_refs(payload.get("message_refs"))
    # “最近生成/发出的图片”系列参数：use_recent 启用；recent_refs 按 ref 挑选。
    use_recent = bool(payload.get("use_recent", False))
    if payload.get("recent_refs") is None and payload.get("recent_indices") is not None:
        return {"ok": False, "error": "recent_indices 已不再支持，请重新 op=recent 并用返回的 ref 填 recent_refs。"}
    recent_refs = _forward_bridge_parse_attachment_refs(payload.get("recent_refs"))
    if recent_refs:
        use_recent = True

    target, error = await _forward_bridge_resolve_target(
        bot,
        session_id,
        target_type,
        target_ref,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    if target is None:
        return {"ok": False, "error": error or "目标解析失败。"}

    # 跨会话投递等于借 Bot 身份向任意人/群发消息，只有管理员可用；
    # 非管理员仍可把内容发回自己或本群，那些内容他本来就看得到。
    requester = _forward_bridge_requester(
        session_id, parent_session_id=parent_session_id, runtime_session_id=runtime_session_id,
    )
    origin_user_id = requester.user_id
    origin_group_id = requester.group_id
    is_admin = requester.listed_admin
    # 这项能力最宽只能给到名单，群角色不参与判定 —— 也就没必要为它多跑一次
    # get_group_member_info。走 allows 是为了让 off 档对名单里的人同样生效。
    may_cross = capability.allows("cross_session", _grant_of("cross_session"), requester)
    cross_session_denied = forward_delivery_deny_reason(
        is_admin=may_cross,
        target_type=target.target_type,
        target_id=target.target_id,
        origin_user_id=origin_user_id,
        origin_group_id=origin_group_id,
    )
    if cross_session_denied:
        logger.warning(
            "qq_forward denied cross-session delivery: action=%s target=%s",
            action, target.target_type,
        )
        return {"ok": False, "error": cross_session_denied}

    send_target = _target_to_send_dict(target)
    label = _target_display_label(target.target_type, target.label)
    conversation_key = f"qq_forward:{target.target_type}:{target.target_id}"
    send_context = _request_send_context(
        "qq_forward", request_identity, conversation_key,
    )

    # 纯提醒/通知：不涉及历史挑选，直接发一段文本。
    if action == "remind":
        body = extra_text
        if not body:
            return {"ok": False, "error": "提醒内容为空。"}
        await _send_text_and_attachments(
            bot, send_target, body, [], send_context=send_context,
        )
        return {"ok": True, "result": f"已向{label}发送提醒。"}

    # 发送工作区文件：“把仓库里的 xx 文件发出来”。支持一次多个文件。
    if action == "file":
        raw_file_paths = payload.get("file_paths")
        file_paths_denied = forward_file_paths_deny_reason(
            is_admin=is_admin, raw_file_paths=raw_file_paths,
        )
        if file_paths_denied:
            logger.warning("qq_forward denied file exfiltration by non-admin")
            return {"ok": False, "error": file_paths_denied}
        attachments, file_errors = _forward_bridge_resolve_files(
            raw_file_paths,
            payload.get("file_names"),
        )
        # 允许 op=file 时不给 file_paths、而是从最近生成/发出的附件里挑（use_recent）。
        if not attachments and use_recent:
            recent_atts = _forward_bridge_pick_recent_attachments(
                session_id,
                refs=recent_refs,
                only_images=False,
                parent_session_id=parent_session_id,
                runtime_session_id=runtime_session_id,
            )
            attachments = _forward_bridge_resolve_files(
                [att.get("path") for att in recent_atts],
                [att.get("name") for att in recent_atts],
            )[0]
        if not attachments:
            hint = "；".join(file_errors) if file_errors else "请在 file_paths 里给出工作区内的文件路径，或用 use_recent 发送最近生成的图片。"
            return {"ok": False, "error": f"没有可发送的文件：{hint}"}
        if extra_text:
            await _send_text_and_attachments(
                bot, send_target, extra_text, [], send_context=send_context,
            )
        await _send_attachments(
            bot, send_target, attachments, send_context=send_context,
        )
        names = "、".join(_sanitize_name(att.get("name") or att.get("path"), max_len=40) for att in attachments)
        result = f"已向{label}发送 {len(attachments)} 个文件：{names}"
        if file_errors:
            result += "\n（部分文件未发送：" + "；".join(file_errors) + "）"
        return {"ok": True, "result": result}

    # use_recent：把“最近生成/发出的图片”作为附件直接发送（不依赖群历史）。
    if use_recent and action in {"send", "forward"}:
        recent_atts = _forward_bridge_pick_recent_attachments(
            session_id,
            refs=recent_refs,
            only_images=False,
            parent_session_id=parent_session_id,
            runtime_session_id=runtime_session_id,
        )
        recent_atts = _forward_bridge_resolve_files(
            [att.get("path") for att in recent_atts],
            [att.get("name") for att in recent_atts],
        )[0]
        if not recent_atts:
            return {"ok": False, "error": "没有找到最近生成/发送的图片。可先用 op=recent 查看可选图片。"}
        await _send_text_and_attachments(
            bot, send_target, extra_text, recent_atts, send_context=send_context,
        )
        return {"ok": True, "result": f"已向{label}发送 {len(recent_atts)} 张最近生成/发送的图片。"}

    selection = _forward_bridge_pick_records(
        session_id,
        refs=refs,
        query=query,
        parent_session_id=parent_session_id,
        runtime_session_id=runtime_session_id,
    )
    if selection.error:
        return {"ok": False, "error": selection.error}
    records = selection.records
    missing_note = (
        f"（其中 {len(selection.missing_refs)} 条已过期，未包含）"
        if selection.missing_refs else ""
    )

    if action == "send":
        # 直接把挑选到的消息拼成一段文本 + 附件发送（非合并转发卡片）。
        lines = [r.formatted_line or r.text for r in records if (r.formatted_line or r.text)]
        body = "\n".join(part for part in ([extra_text] if extra_text else []) + lines).strip()
        attachments: list[dict[str, Any]] = []
        if include_images or include_files:
            for r in records:
                attachments.extend(_filter_attachments_by_kind(
                    r.attachments or [],
                    include_images=include_images,
                    include_files=include_files,
                ))
        if not body and not attachments:
            return {"ok": False, "error": "没有可发送的内容。"}
        await _send_text_and_attachments(
            bot, send_target, _truncate_qq_text(body), attachments,
            send_context=send_context,
        )
        return {"ok": True, "result": f"已发送到{label}（{len(records)} 条消息）。{missing_note}"}

    # 默认 action == "forward"：合并转发卡片，支持多选多条消息。
    nodes = await _forward_bridge_records_to_nodes(
        bot,
        records,
        include_images=include_images,
        include_files=include_files,
    )
    card_images: list[dict[str, Any]] = []
    if include_images:
        for r in records:
            card_images.extend(_filter_attachments_by_kind(
                r.attachments or [], include_images=True, include_files=False,
            ))
    if extra_text:
        head = _make_forward_node(bot, extra_text, None, nickname="Clonoth 通知")
        if head:
            nodes.insert(0, head)
    if not nodes:
        return {"ok": False, "error": "没有可转发的消息。请先用 list 查看上文消息并给出 message_refs 或 query。"}
    try:
        await _send_forward_nodes(
            bot, target, nodes, send_context=send_context,
            image_attachments=card_images,
        )
    except Exception as exc:
        logger.warning("qq_forward bridge send forward failed: %s", exc, exc_info=True)
        return {"ok": False, "error": "合并转发发送失败：当前 OneBot/NapCat 可能不支持该接口，或目标不可达。"}
    return {"ok": True, "result": f"已向{label}发送合并转发（{len(nodes)} 条）。{missing_note}"}


def _forward_bridge_check_token(request: "Any") -> bool:
    token = _forward_bridge_token()
    if not token:
        return False
    presented = request.headers.get("X-Forward-Token", "")
    # 逐字节比较会把令牌问出来
    if not (presented and hmac.compare_digest(presented, token)):
        logger.warning(
            "qq_forward bridge rejected request: token mismatch (peer=%s)",
            getattr(request, "remote", ""),
        )
        return False
    return True


async def _forward_bridge_http_handler(request: "Any") -> "Any":
    from aiohttp import web  # 局部导入，未启用 Bridge 时不强依赖 aiohttp。

    # aiohttp 每个请求一个 task，在这里钉住整次转发用同一份配置。
    refresh_live_config()
    if not _forward_bridge_check_token(request):
        return web.json_response(
            {
                "ok": False,
                "error": "Bridge 令牌不匹配：请确认 ONEBOT_FORWARD_BRIDGE_TOKEN 两侧一致，"
                "或 data/onebot_forward_bridge_token 对工具进程可读。",
            },
            status=403,
        )
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "error": "invalid payload"}, status=400)
    caller_request_id = str(
        payload.get("request_id")
        or request.headers.get("Idempotency-Key", "")
        or request.headers.get("X-Request-ID", "")
        or uuid.uuid4().hex
    ).strip()
    # The private field is internal-only: always overwrite it with the identity from
    # the public HTTP contract so a retry cannot accidentally fork the send claim.
    payload["request_id"] = caller_request_id
    payload["_request_identity"] = caller_request_id
    op = str(payload.get("op") or "").strip().lower()
    try:
        _parent_sid = str(payload.get("parent_session_id") or "")
        _runtime_sid = str(payload.get("runtime_session_id") or "")
        if op == "list":
            _origin, denied = _forward_bridge_read_origin(
                str(payload.get("session_id") or ""),
                parent_session_id=_parent_sid,
                runtime_session_id=_runtime_sid,
                op="list",
            )
            if denied:
                return web.json_response({"ok": False, "error": denied}, status=403)
            messages = _forward_bridge_list_messages(
                str(payload.get("session_id") or ""),
                query=str(payload.get("query") or ""),
                limit=int(payload.get("limit") or live.forward_bridge_max_messages),
                parent_session_id=_parent_sid,
                runtime_session_id=_runtime_sid,
            )
            return web.json_response({"ok": True, "messages": messages})
        if op == "recent":
            _origin, denied = _forward_bridge_read_origin(
                str(payload.get("session_id") or ""),
                parent_session_id=_parent_sid,
                runtime_session_id=_runtime_sid,
                op="recent",
            )
            if denied:
                return web.json_response({"ok": False, "error": denied}, status=403)
            recent = _forward_bridge_list_recent(
                str(payload.get("session_id") or ""),
                only_images=bool(payload.get("only_images", True)),
                limit=int(payload.get("limit") or 20),
                parent_session_id=_parent_sid,
                runtime_session_id=_runtime_sid,
            )
            return web.json_response({"ok": True, "recent": recent})
        if op in {"forward", "send", "remind", "file", ""}:
            if op:
                payload.setdefault("action", op)
            result = await _forward_bridge_execute(payload)
            status = 200 if result.get("ok") else 400
            return web.json_response(result, status=status)
        return web.json_response({"ok": False, "error": f"unknown op: {op}"}, status=400)
    except Exception as exc:
        logger.warning("qq_forward bridge handler error: %s", exc, exc_info=True)
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


async def _start_forward_bridge() -> None:
    """在 QQ Bot 进程内启动 qq_forward Bridge Server。"""
    global _forward_bridge_runner, _forward_bridge_site, _forward_bridge_started
    if _forward_bridge_started or not live.enable_forward_bridge:
        return
    try:
        from aiohttp import web
    except Exception:
        logger.warning("qq_forward bridge disabled: aiohttp 未安装。")
        return
    if not _forward_bridge_token():
        logger.error(
            "qq_forward bridge disabled: 无法确定 Bridge 令牌（%s 不可写）",
            FORWARD_BRIDGE_TOKEN_FILE,
        )
        return
    app = web.Application()
    app.router.add_post("/qq_forward", _forward_bridge_http_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, FORWARD_BRIDGE_HOST, FORWARD_BRIDGE_PORT)
    await site.start()
    _forward_bridge_runner = runner
    _forward_bridge_site = site
    _forward_bridge_started = True
    logger.info(
        "qq_forward bridge server started: http://%s:%s/qq_forward (token source=%s)",
        FORWARD_BRIDGE_HOST,
        FORWARD_BRIDGE_PORT,
        "env" if FORWARD_BRIDGE_TOKEN else "file",
    )


async def _stop_forward_bridge() -> None:
    global _forward_bridge_runner, _forward_bridge_site, _forward_bridge_started
    if _forward_bridge_runner is not None:
        with contextlib.suppress(Exception):
            await _forward_bridge_runner.cleanup()
    _forward_bridge_runner = None
    _forward_bridge_site = None
    _forward_bridge_started = False


def _reconcile_group_history_capacity() -> bool:
    """群历史条数改了就按新上限重建每个群的 deque。

    maxlen 不可变，只能重建。序号水位在 _group_history_seq 里不受影响，但调小容量丢的
    最旧几行里可能有还没送到 engine 的，那几条要记进缺口，否则 prompt 里悄悄少一段。
    """
    target = _group_history_capacity()
    records_target = max(live.group_history_max, 20)
    changed = False
    for group_id, lines in list(_group_history.items()):
        if lines.maxlen != target:
            for entry in list(lines)[:max(0, len(lines) - target)]:
                _note_history_gap(group_id, entry.seq)
            _group_history[group_id] = deque(lines, maxlen=target)
            changed = True
    for group_id, records in list(_group_content_records.items()):
        if records.maxlen != records_target:
            _group_content_records[group_id] = deque(records, maxlen=records_target)
            changed = True
    return changed


async def _reconcile_queue_workers() -> bool:
    """把 worker task 的数量对齐到配置。

    扩容立刻建 task；缩容只叫醒它们，让每个 worker 在自己的安全点退出 —— 直接 cancel
    会打断正在提交的那一条消息。
    """
    for index, task in list(_qq_queue_tasks.items()):
        if task.done():
            _qq_queue_tasks.pop(index, None)
    target = live.queue_workers if live.enable_queue else 0
    running = len(_qq_queue_tasks)
    if running == target:
        return False
    if running < target:
        for index in range(target):
            if index not in _qq_queue_tasks:
                _qq_queue_tasks[index] = asyncio.create_task(_qq_queue_worker_forever(index))
    else:
        async with _qq_queue_condition:
            _qq_queue_condition.notify_all()
    logger.info(
        "QQ queue reconciled: workers %d -> %d (enabled=%s pending=%d)",
        running, target, live.enable_queue, len(_qq_queue),
    )
    return True


async def _reconcile_forward_bridge() -> bool:
    """按开关起停本地转发 Bridge。监听地址仍在启动时绑定，改地址要重启。"""
    if live.enable_forward_bridge and not _forward_bridge_started:
        await _start_forward_bridge()
        return _forward_bridge_started
    if not live.enable_forward_bridge and _forward_bridge_started:
        await _stop_forward_bridge()
        logger.info("qq_forward bridge server stopped: disabled in qq.yaml")
        return True
    return False


def _live_state_file() -> Path:
    return Path(CLONOTH_WORKSPACE) / "data" / "qq_live_state.json"


def _live_runtime_facts() -> Dict[str, Any]:
    """配置之外、只有 bot 进程知道的运行期事实。

    volatile 里的计数每时每刻都在动，单独一层是为了让 dedup 只看会长期稳定的部分。
    """
    try:
        # 没有 OneBot 实现连上来时 get_bot() 抛，这不是错误，是「NapCat 还没连」。
        # 也要挡住返回 None 的情况，否则「没连」会被报成「已连」。
        onebot_connected = get_bot() is not None
    except Exception:
        onebot_connected = False
    return {
        "pid": os.getpid(),
        "onebot_connected": onebot_connected,
        "queue_workers_running": len([t for t in _qq_queue_tasks.values() if not t.done()]),
        "forward_bridge_running": bool(_forward_bridge_started),
        "forward_bridge_endpoint": f"http://{FORWARD_BRIDGE_HOST}:{FORWARD_BRIDGE_PORT}/qq_forward",
        "forward_bridge_token_set": bool(_forward_bridge_token()),
        "group_history_capacities": sorted({int(d.maxlen or 0) for d in _group_history.values()}),
        # 改一次就要重启的那几个键，控制台只读展示。密钥一律只报「设没设」——
        # 回传值等于把它摊在任何能打开控制台的人面前。
        "environment": {
            "supervisor_url": CLONOTH_BASE_URL,
            "workspace": str(CLONOTH_WORKSPACE),
            "hash_secret_set": bool(CONVERSATION_HASH_SECRET),
            "conversation_digest_salted": CONVERSATION_DIGEST_SALTED,
        },
        # 迁移功能靠它认「当前号」。空串 = 还没连上过任何账号，此时全部会话键都没作用域。
        "bot_scope": _active_bot_scope(),
        "volatile": {
            "queue_pending": len(_qq_queue),
            "cached_groups": len(_group_history),
            "history_gap_groups": len([
                gid for gid in _group_history_gap
                if _group_history_lost(gid, _group_history_watermark(gid)) > 0
            ]),
        },
    }


_group_names: Dict[str, str] = {}
_group_names_at = 0.0
_group_names_seeded = False
# 群名极少变，而 reconcile 每 2 秒转一圈，跟着它调 API 纯属浪费。
_GROUP_NAME_REFRESH_SEC = 300.0


def _seed_group_names() -> None:
    """把上次记下的群名读回来。

    这份字典是「已知群名」而不是「当前登录号的群」：get_group_list 只给得出当前
    号看得见的群，进程重启或换号后直接整体覆盖，历史会话的群名就没了，控制台上
    只剩 GroupA 这种脱敏别名。已经见过的群名是既成事实，退群或换号都不该抹掉。
    """
    global _group_names_seeded
    if _group_names_seeded:
        return
    _group_names_seeded = True
    try:
        known = json.loads(_live_state_file().read_text(encoding="utf-8")).get("group_names")
    except Exception:
        return
    if not isinstance(known, dict):
        return
    for gid, name in known.items():
        text = str(name or "").strip()
        # 内存里已有的更新，不要被磁盘上的旧值盖回去。
        if text and str(gid) not in _group_names:
            _group_names[str(gid)] = text


async def _refresh_group_names() -> None:
    """把群号→群名缓存下来发给控制台。

    只有 bot 进程连着 OneBot，supervisor 自己拿不到群名；不给的话控制台上
    只能显示 GroupA 这种脱敏别名，管理员分不清是哪个群。
    """
    global _group_names_at
    now = time.time()
    if now - _group_names_at < _GROUP_NAME_REFRESH_SEC:
        return
    bot = _get_fallback_bot()
    if bot is None:
        return
    _group_names_at = now
    for row in await _safe_call_onebot_list(bot, "get_group_list"):
        try:
            gid = int(row.get("group_id"))
        except Exception:
            continue
        name = str(row.get("group_name") or row.get("group_remark") or "").strip()
        if name:
            _group_names[str(gid)] = _sanitize_name(name, max_len=40)


def _publish_live_state(*, force: bool = False) -> None:
    """把「bot 进程实际生效的配置」写给 supervisor 的 GET /qq/state 读。

    不走转发 Bridge：Bridge 自己的开关就是一个热载键，关掉之后状态就瞎了，而
    「界面显示已生效、进程其实没读到」正是热重载最需要防的那种假象。
    """
    global _live_state_published_at, _live_state_signature
    payload: Dict[str, Any] = live_config_state_payload()
    payload["runtime"] = _live_runtime_facts()
    _seed_group_names()
    payload["group_names"] = dict(_group_names)
    payload["scope_conversation_keys"] = _current_scope_keys()
    signature = json.dumps(
        {k: v for k, v in payload.items() if k != "runtime"}
        | {k: v for k, v in payload["runtime"].items() if k != "volatile"},
        ensure_ascii=False, sort_keys=True,
    )
    now = time.time()
    if not force and signature == _live_state_signature and now - _live_state_published_at < _LIVE_STATE_HEARTBEAT_SEC:
        return
    payload["published_at"] = now
    path = _live_state_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 先写同目录 tmp 再 replace：supervisor 随时可能在读，不能让它读到半截 JSON。
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.warning("failed to publish QQ live state to %s", path, exc_info=True)
        return
    _live_state_signature = signature
    _live_state_published_at = now


async def _reconcile_live_config() -> bool:
    """把 reconciled 组的键落到运行期对象上。返回是否真的动了东西。"""
    changed = _reconcile_group_history_capacity()
    changed = await _reconcile_queue_workers() or changed
    changed = await _reconcile_forward_bridge() or changed
    return changed


async def _live_config_reconcile_forever() -> None:
    """定期对齐 reconciled 组的键，并公布生效快照。

    刻意不钉快照：钉住以后这个 task 会一直拿着启动那一刻的配置，于是永远认为无事可做。
    """
    previous: Dict[str, Any] = {}
    while True:
        try:
            snapshot = {name: getattr(live, name) for name in RECONCILED_KEYS}
            if snapshot != previous:
                if previous:
                    logger.info(
                        "QQ live config changed: %s",
                        {k: v for k, v in snapshot.items() if previous.get(k) != v},
                    )
                await _reconcile_live_config()
                previous = snapshot
            await _refresh_group_names()
            _publish_live_state()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("QQ live config reconcile failed", exc_info=True)
        await asyncio.sleep(_LIVE_RECONCILE_INTERVAL_SEC)


async def _attachment_cleanup_forever() -> None:
    """定期清理 QQ 附件目录。原来只在有图片/文件下载时才顺手清一次，长期不收图就永远不清。"""
    while True:
        try:
            _cleanup_old_qq_attachments()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("QQ attachment cleanup failed", exc_info=True)
        await asyncio.sleep(_ATTACHMENT_CLEANUP_INTERVAL_SEC)


@driver.on_startup
async def _startup() -> None:
    """NoneBot 启动时初始化 Clonoth SDK 与事件路由。"""
    global _client, _session_state, _event_router, _router_task, _callbacks, _bqbs, _custom_face_names, _custom_face_metadata
    global _live_reconcile_task, _attachment_cleanup_task
    if _router_task is not None and not _router_task.done():
        return

    _warn_startup_config()
    _warn_trigger_config()

    global _QQ_USER_PROFILES
    _bqbs = load_bqbs(BQBS_PATH) if BQBS_PATH else []
    _custom_face_names = _load_custom_face_names_file()
    _custom_face_metadata = _load_custom_face_metadata_file()
    _QQ_USER_PROFILES = _load_qq_user_profiles(USER_PROFILES_PATH)
    if _QQ_USER_PROFILES:
        logger.info("loaded %d QQ user profiles", len(_QQ_USER_PROFILES))
    _load_route_state()
    _load_reply_attachment_cache()
    _start_sticker_tagger()
    _load_anon_map()
    # [2026-07-14] 注入 at 别名反查，让 emoji_handler 在处理 [at:UserAF]/[at:显示名]
    # 时能把匿名别名/群昵称回解为真实 QQ 号，避免直接把代号当纯文本 @ 出去。
    set_at_alias_resolver(_resolve_at_alias_to_real)
    set_sticker_resolver(_resolve_sticker)
    # [AutoC] QQ 管理员 /切换模型 命令需要调用受 admin_token 保护的
    # POST /v1/config/openai，因此这里把 Supervisor 写出的 data/.admin_token
    # 传给 ClonothClient（每次请求实时读取，token 轮换也能跟上）。
    _client = ClonothClient(
        CLONOTH_BASE_URL,
        admin_token=os.environ.get("CLONOTH_ADMIN_TOKEN", "").strip(),
        admin_token_path=str(Path(CLONOTH_WORKSPACE) / "data" / ".admin_token"),
    )
    _session_state = SessionState()
    for sid, target in list(_persisted_session_targets.items()):
        conv_key = str(target.get("conversation_key") or "")
        if conv_key:
            _session_state.register_session(conv_key, sid)
    _callbacks = TangQiuCallbacks()

    bot_config = BotConfig(
        base_url=CLONOTH_BASE_URL,
        entry_node_id=ENTRY_NODE_ID,
        conversation_key_prefix="qq_group",
        # [AutoC] QQ 私聊会话键前缀为 qq_private，需一并声明归属，
        # 否则私聊触发的 approval_requested 会因前缀不匹配被 SDK 丢弃，
        # 导致管理员收不到审批、任务空等到超时。
        extra_conversation_key_prefixes=["qq_private"],
        workspace_root=Path(CLONOTH_WORKSPACE),
        # QQ 侧不再自动审批内部操作；所有 approval_requested 都必须由
        # 管理员名单里的人私聊明确同意后才会放行。
        auto_approve_internal=False,
    )
    _event_router = EventRouter(
        _client,
        _session_state,
        _callbacks,
        bot_config,
        entry_node_id=ENTRY_NODE_ID,
        poll_interval=1.0,
    )
    _router_task = asyncio.create_task(_event_router.run())
    if live.enable_queue:
        logger.info(
            "QQ queue enabled: workers=%s interval=%ss wait_for_reply=%s reply_timeout=%ss preempt=%s",
            live.queue_workers,
            live.queue_interval,
            live.queue_wait_for_reply,
            live.queue_reply_timeout,
            live.enable_preempt,
        )
    # 队列 worker 和转发 Bridge 都由 reconcile 建立，启动和运行期改配置走同一条路径 ——
    # 两套代码就会漂移，而漂移的那一侧只有改配置时才会被发现。
    with contextlib.suppress(Exception):
        await _reconcile_live_config()
    _publish_live_state(force=True)
    _live_reconcile_task = asyncio.create_task(_live_config_reconcile_forever())
    _attachment_cleanup_task = asyncio.create_task(_attachment_cleanup_forever())
    logger.info("Clonoth Agent QQ adapter started: %s", CLONOTH_BASE_URL)


@driver.on_bot_connect
async def _on_bot_connect(bot: Bot) -> None:
    """连上就认账号。等第一条消息再认的话，那条消息本身还是按上一个号算键。"""
    _adopt_bot_scope(bot)
    _publish_live_state(force=True)


@driver.on_shutdown
async def _shutdown() -> None:
    """NoneBot 关闭时停止事件路由并释放 HTTP 连接。"""
    global _client, _event_router, _router_task, _callbacks, _anon_map_save_task, _live_reconcile_task, _attachment_cleanup_task
    if _event_router is not None:
        _event_router.stop()
    if _router_task is not None:
        _router_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _router_task
        _router_task = None
    if _live_reconcile_task is not None:
        _live_reconcile_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _live_reconcile_task
        _live_reconcile_task = None
    if _attachment_cleanup_task is not None:
        _attachment_cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _attachment_cleanup_task
        _attachment_cleanup_task = None
    await _stop_stickers()
    if _qq_queue_tasks:
        for task in _qq_queue_tasks.values():
            task.cancel()
        for task in _qq_queue_tasks.values():
            with contextlib.suppress(asyncio.CancelledError):
                await task
        _qq_queue_tasks.clear()
    if _client is not None:
        await _client.close()
        _client = None
    _event_router = None
    _callbacks = None
    await _stop_forward_bridge()
    # 关闭前先取消可能在睡等间隔的节流 flush task，再同步 flush 一次，
    # 避免延迟写盘 task 未到点就退出导致最新别名丢失。
    if _anon_map_save_task is not None and not _anon_map_save_task.done():
        _anon_map_save_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await _anon_map_save_task
    _anon_map_save_task = None
    with contextlib.suppress(Exception):
        await _save_anon_map()
    logger.info("Clonoth Agent QQ adapter stopped")


# 普通群消息记录器。priority 较低且 block=False，只负责维护上下文缓存。
_history_matcher = on_message(rule=Rule(_allowed_group_rule), priority=99, block=False)


@_history_matcher.handle()
async def _handle_history(bot: Bot, event: GroupMessageEvent) -> None:
    """记录非触发消息，供后续 @Bot 请求作为群聊上下文。"""
    await _record_non_trigger_message(bot, event)


async def _record_non_trigger_message(bot: Bot, event: GroupMessageEvent) -> None:
    """把一条不回复的群消息写进上下文缓存。

    意愿判断那条路 block 住了这个 matcher（判定结果要等一次模型往返，不能让整条链
    干等），判成不接话时得由它自己补记，否则这些消息会从群历史里整段消失。
    """
    _remember_message_for_reply_context(event)
    if str(event.user_id) != str(getattr(bot, "self_id", "")):
        expanded_text = await _event_text_with_forward(bot, event)
        # QQ 客户端经常把“问图文本 + 图片”拆成两条事件；第二条纯图片
        # 通常不会 @bot，因此不会进入 agent matcher。这里在低优先级历史
        # matcher 中把非触发附件也下载并写入近期缓存，让后续自然语言转发
        # 和图片提问能选到对应内容。
        real_conversation_key = f"qq_group:{int(event.group_id)}"
        stable_conversation_key = _stable_conversation_key(real_conversation_key)
        # 不回复的消息只扫顶层。下钻是为了让模型看见卡片里的图，而这条根本不会进模型；
        # 真被人引用时 _build_reply_context 会再下钻一次，那时候才值得付这几次下载。
        attachments, _errors = await _collect_qq_attachments(
            bot, event, stable_conversation_key, expand_forward=False,
        )
        _remember_recent_images(stable_conversation_key, event, attachments)
        _collect_stickers_from_event(event, attachments)
        _record_group_message(event, bot, override_text=expanded_text, attachments=attachments)
        await _maybe_echo_group_message(bot, event, expanded_text)
        await _maybe_join_sticker_combat(
            bot, event,
            has_image=any(item.get("type") == "image" for item in attachments),
            text=expanded_text,
        )


def _echo_key_for_message(message: Any, text: str) -> str:
    """这条消息用于复读比对的内容指纹。跟不了就返回空串。

    表情包不能用正文比：三个人各发一张不同的图，_message_to_text 都会渲染成
    「[表情包]」，光比文本会把它们判成复读。所以图片一律拿 file/url 当指纹。
    """
    segments = list(message) if message is not None else []
    kinds = set()
    image_id = ""
    face_ids: List[str] = []
    for segment in segments:
        seg_type, data = _segment_type_and_data(segment)
        if seg_type == "text":
            if str(data.get("text") or "").strip():
                kinds.add("text")
            continue
        if seg_type in IMAGE_SEGMENT_TYPES:
            kinds.add("image")
            # emoji_id 是商城表情的稳定标识，url 每次取都可能带不同的鉴权参数。
            image_id = str(
                data.get("emoji_id") or data.get("file") or _segment_image_url(data) or ""
            ).strip()
            continue
        if seg_type == "face":
            # 系统表情有稳定 id，跟文字一样能精确比对，复读它也不指向任何人。群里复读
            # 的句子十句有九句夹着表情，把它算成不可跟的类型，这个功能就等于没开。
            kinds.add("text")
            face_ids.append(str(data.get("id") or "").strip())
            continue
        # 语音、视频、卡片、@ 之类一律不跟。
        kinds.add("other")

    if kinds == {"image"} and image_id:
        return f"img:{image_id}"
    if kinds == {"text"}:
        # id 单独进指纹：正文里的「[QQ表情:捂脸]」是查表得来的显示名，两个 id 撞名就会
        # 把不同的表情比成同一条。
        return f"txt:{text.strip()}" + (f"|face:{','.join(face_ids)}" if face_ids else "")
    return ""


async def _maybe_echo_group_message(bot: Bot, event: GroupMessageEvent, text: str) -> None:
    """连着 N 个不同的人刷同一句，Bot 也跟一条。全程不进模型。"""
    if not live.enable_echo:
        return
    group_id = int(event.group_id)
    try:
        message = event.get_message()
    except Exception:
        return
    key = _echo_key_for_message(message, text)
    if not key:
        return
    # 校验正文本身而不是 key[4:]：指纹尾部还挂着表情 id，会把长度算多。
    if key.startswith("txt:") and not is_echoable_text(text, max_length=live.echo_max_length):
        return

    now = time.time()
    _echo_recent[group_id].append(EchoEntry(key, str(event.user_id), now))
    last_key, last_at = _echo_last.get(group_id, ("", 0.0))
    if now - last_at < live.echo_cooldown_sec:
        return
    hit = detect_echo(
        _echo_recent[group_id],
        threshold=live.echo_threshold,
        now=now,
        max_age_seconds=live.echo_window_sec,
        already_echoed=last_key,
    )
    if not hit:
        return

    # 命中的必然是刚进来这条：detect_echo 要求尾部 N 条指纹全同，而这条就在尾部。
    # 所以文本直接原样回发 —— 拿指纹反解会把「[QQ表情:捂脸]」当字面量发出去。
    outgoing = Message(MessageSegment.image(hit[4:])) if hit.startswith("img:") else message
    try:
        await bot.send_group_msg(group_id=group_id, message=outgoing)
    except Exception:
        # 跟读失败无所谓，不值得惊动用户，更不该把异常抛回 matcher 链。
        logger.debug("echo send failed for group %s", group_id, exc_info=True)
        return
    _echo_last[group_id] = (hit, now)


# 群文件上传通知记录器。部分 OneBot 实现把普通文件作为 notice 上报，而不是 message file 段。
_group_upload_matcher = on_notice(priority=99, block=False)


@_group_upload_matcher.handle()
async def _handle_group_upload_notice(bot: Bot, event: Event) -> None:
    if not isinstance(event, GroupUploadNoticeEvent):
        return
    # notice matcher 没有 rule，这里就是最早入口。
    refresh_live_config()
    if not _is_group_allowed(int(event.group_id)):
        return
    _remember_message_for_reply_context(event)
    real_conversation_key = f"qq_group:{int(event.group_id)}"
    stable_conversation_key = _stable_conversation_key(real_conversation_key)
    file_info = getattr(event, "file", None)
    if not isinstance(file_info, dict):
        file_info = {}
    source = str(file_info.get("url") or file_info.get("path") or file_info.get("file") or "").strip()
    name = _safe_attachment_name(str(file_info.get("name") or file_info.get("file_name") or file_info.get("filename") or "文件"), "file")
    size = file_info.get("size") or file_info.get("file_size") or file_info.get("filesize") or 0
    attachments, _errors = await _file_sources_to_attachments([
        {"source": source, "name": name, "size": size},
    ], stable_conversation_key)
    display_text = _segment_to_text("file", {"name": name}, set()) or ""
    sender_id = str(getattr(event, "user_id", "") or "")
    # GroupUploadNoticeEvent 没有 sender/anonymous 属性，helper 会退回 profile / 匿名别名。
    line = _compose_history_line(
        timestamp=getattr(event, "time", None),
        sender=getattr(event, "sender", None),
        sender_id=sender_id,
        text=display_text,
        name_override=_event_display_name(event),
        alias_override=_event_user_alias(event),
    )
    _append_group_record(
        int(event.group_id),
        line,
        text=display_text,
        sender_name=_event_display_name(event),
        sender_id=sender_id,
        timestamp=float(getattr(event, "time", None) or time.time()),
        message_id=str(getattr(event, "message_id", "") or ""),
        attachments=attachments,
    )


# Agent 入口 matcher。默认只处理 @Bot；也可通过 ONEBOT_GROUP_TRIGGER=prefix/all 切换触发策略。
_agent_matcher = on_message(rule=Rule(_agent_group_rule), priority=10, block=True)


@_agent_matcher.handle()
async def _handle_agent(bot: Bot, event: GroupMessageEvent) -> None:
    """把当前 QQ 群请求提交给 ClonothZX。"""
    await _process_group_message(bot, event, _agent_matcher)


async def _finish_local_command(
    matcher: Any,
    bot: Bot,
    event: GroupMessageEvent,
    *,
    user_text: str,
    attachments: List[Dict[str, Any]],
    reply: str,
) -> None:
    """回掉一条本地命令，并把命令行与回复一起写进群历史（末尾总会抛出 FinishedException）。

    本地命令不经 engine，也已经被 agent matcher block 掉了 priority=99 的历史 matcher：
    不在这里记，整段交互在群历史里就消失，后面的对话看不到「刚才有人改过模型」。
    """
    _record_group_message(event, bot, override_text=user_text, attachments=attachments)
    _record_bot_reply(int(event.group_id), reply)
    await matcher.finish(reply)


async def _process_group_message(bot: Bot, event: GroupMessageEvent, matcher: Any) -> None:
    """一条已判定要回的群消息的完整处理。

    matcher 由调用方传进来：显式信号和意愿判断走两个不同优先级的 matcher，
    但从本地命令到 inbound 提交的整条链路必须是同一份，否则两条路会各自长歪。
    """
    global _last_bot
    _last_bot = bot
    # on_bot_connect 已经认过一次，这里是兜底：漏认一次就是记忆分叉，代价不对等。
    _adopt_bot_scope(bot)

    if _client is None or _session_state is None:
        await matcher.finish("Clonoth Agent 尚未初始化，请稍后重试。")

    _remember_message_for_reply_context(event)
    group_id = int(event.group_id)
    real_conversation_key = f"qq_group:{group_id}"
    stable_conversation_key = _stable_conversation_key(real_conversation_key)
    raw_user_text = (await _event_text_with_forward(bot, event)).strip() or "你好"
    # 前缀判定必须在剥前缀之前做，否则 all 模式下所有人都会被当成主动找 bot。
    direct_interaction = _is_direct_bot_interaction(event, bot, raw_user_text)
    user_text = _strip_trigger_prefix(raw_user_text)
    if not _anonymous_identity(event):
        asyncio.create_task(_auto_like_user(bot, int(event.user_id)))

    attachments, attachment_errors = await _collect_qq_attachments(bot, event, stable_conversation_key)
    _remember_recent_images(stable_conversation_key, event, attachments)
    help_reply = await _maybe_handle_help_command(event=event, user_text=user_text)
    if help_reply is not None:
        await _finish_local_command(matcher, bot, event, user_text=user_text, attachments=attachments, reply=help_reply)
    private_only_reply = await _maybe_handle_private_only_in_group(event=event, user_text=user_text)
    if private_only_reply is not None:
        await _finish_local_command(
            matcher, bot, event, user_text=user_text, attachments=attachments, reply=private_only_reply,
        )
    clear_mem_reply = await _maybe_handle_clear_group_memory_command(
        bot=bot,
        event=event,
        user_text=user_text,
    )
    if clear_mem_reply is not None:
        await _finish_local_command(matcher, bot, event, user_text=user_text, attachments=attachments, reply=clear_mem_reply)
    dream_reply = await _maybe_handle_dream_command(
        event=event, user_text=user_text, conversation_key=stable_conversation_key,
    )
    if dream_reply is not None:
        await _finish_local_command(matcher, bot, event, user_text=user_text, attachments=attachments, reply=dream_reply)
    model_reply = await _maybe_handle_model_command(event=event, user_text=user_text)
    if model_reply is not None:
        await _finish_local_command(matcher, bot, event, user_text=user_text, attachments=attachments, reply=model_reply)
    drawtools_reply = await _maybe_handle_drawtools_command(event=event, user_text=user_text)
    if drawtools_reply is not None:
        await _finish_local_command(matcher, bot, event, user_text=user_text, attachments=attachments, reply=drawtools_reply)
    custom_face_reply = await _maybe_handle_custom_face_command(
        bot=bot,
        event=event,
        user_text=user_text,
        conversation_key=stable_conversation_key,
        current_attachments=attachments,
    )
    if custom_face_reply is not None:
        await _finish_local_command(matcher, bot, event, user_text=user_text, attachments=attachments, reply=custom_face_reply)
    sticker_reply = await _maybe_handle_sticker_command(event=event, user_text=user_text)
    if sticker_reply is not None:
        await _finish_local_command(matcher, bot, event, user_text=user_text, attachments=attachments, reply=sticker_reply)
    proactive_reply = await _maybe_handle_proactive_command(
        bot=bot,
        event=event,
        user_text=user_text,
        conversation_key=stable_conversation_key,
        current_attachments=attachments,
    )
    if proactive_reply is not None:
        await _finish_local_command(matcher, bot, event, user_text=user_text, attachments=attachments, reply=proactive_reply)
    draw_direct_prompt = _parse_direct_draw_command(user_text)
    if draw_direct_prompt is None:
        # /生图 不吃历史图：混进来的图会把这一轮拽到视觉节点。
        await _merge_recent_attachments_after_text(event=event, conversation_key=stable_conversation_key, user_text=user_text, attachments=attachments)
    current_seq = _record_group_message(event, bot, override_text=user_text, attachments=attachments)
    entry_node_id = DRAW_NODE_ID if draw_direct_prompt is not None else ""
    if draw_direct_prompt is not None:
        if attachments:
            attachments.clear()
            await matcher.send(_DRAW_REFERENCE_IMAGE_NOTICE)
        # 直达绘图不带群历史，水位保持不动，那些行留给下一条正常消息带出去。
        inbound_text, history_watermark = await _build_draw_direct_inbound_text(event, draw_direct_prompt, False), -1
    else:
        inbound_text, history_watermark = await _build_inbound_text(
            event, bot, user_text, stable_conversation_key, attachments, exclude_seq=current_seq,
        )
        inbound_text = _apply_attachment_hints(inbound_text, user_text, attachments, attachment_errors)

    platform_updates = {
        "bot": bot,
        "event": event,
        "type": "group",
        "group_id": group_id,
        # 群会话也登记触发者：qq_forward 的“私发给我”与权限判定都要靠它反查发起人。
        # 消费端一律先判 type 再读 user_id，因此不会被误认成私聊目标。
        "user_id": event.user_id,
        "conversation_key": stable_conversation_key,
        "_source_attachments": [dict(att) for att in attachments if isinstance(att, dict)],
    }
    try:
        ok = await _enqueue_or_submit_inbound(QueuedInbound(
            matcher=matcher,
            bot=bot,
            event=event,
            channel="qq_group",
            real_conversation_key=real_conversation_key,
            stable_conversation_key=stable_conversation_key,
            text=inbound_text,
            attachments=attachments or [],
            is_dm=False,
            platform_updates=platform_updates,
            user_text=user_text,
            entry_node_id=entry_node_id,
            history_watermark=history_watermark,
            direct_interaction=direct_interaction,
        ))
    except Exception as exc:
        logger.exception("submit inbound failed")
        await matcher.finish(f"无法连接到 Clonoth Agent：{exc}")

    if not ok:
        await matcher.finish("Clonoth Agent 未接受本次请求。")
    await matcher.finish()


# 接话意愿 matcher。优先级排在显式信号之后、历史记录之前：显式信号不用问模型，
# 而判定期间必须把历史记录挡住，等判成不接话再由 handler 自己补记。
_intent_matcher = on_message(rule=Rule(_intent_group_rule), priority=20, block=True)


@_intent_matcher.handle()
async def _handle_intent(bot: Bot, event: GroupMessageEvent) -> None:
    """问一次模型再决定这条群消息要不要接。"""
    text = await _group_trigger_text(bot, event)
    verdict = await _judge_llm_intent(bot, event, text)
    inp = _trigger_input(event, bot, text)
    decision = resolve_llm_intent(
        inp, TriggerConfig.from_live(live), _trigger_cooldown,
        agreed=verdict.agreed, reason=verdict.reason,
    )
    if not decision.triggered:
        logger.debug(
            "QQ intent declined group=%s decided=%s error=%s blocked_by=%s reason=%s",
            getattr(event, "group_id", None), verdict.decided, verdict.error,
            decision.blocked_by, decision.reason,
        )
        await _record_non_trigger_message(bot, event)
        return
    _trigger_cooldown.record(inp)
    logger.info(
        "QQ intent accepted group=%s reason=%s",
        getattr(event, "group_id", None), decision.reason,
    )
    await _process_group_message(bot, event, _intent_matcher)


# 私聊入口 matcher。私聊不需要 @Bot，直接阻断后续 matcher，避免重复响应。
_private_matcher = on_message(rule=Rule(_private_message_rule), priority=10, block=True)


async def _claim_pending_approval(approval_id: str, admin_id: int, decision: str) -> Optional[Dict[str, Any]]:
    """原子领取待审批：抢到才返回 info，其余返回 None。领取即登记 settled。"""
    async with _approval_decision_lock:
        _prune_expired_approvals()
        info = _pending_approvals.pop(approval_id, None)
        if info is None:
            return None
        # 领取后进程崩溃则这条在 QQ 侧变 settled、无法再批，engine 侧超时 auto-deny，属 fail-closed。
        _record_settled_approval(
            approval_id, admin_id=admin_id, decision=decision,
            operation=str(info.get("operation") or ""),
        )
        return info


async def _release_approval_claim(approval_id: str, info: Dict[str, Any]) -> None:
    async with _approval_decision_lock:
        # 一次网络错误不该把审批永久吞掉，放回待审批让人重试。
        _settled_approvals.pop(approval_id, None)
        _pending_approvals[approval_id] = info


async def _finish_approval_decision(user_id: int, approval_id: str, decision: str) -> None:
    """提交审批决策并结束当前私聊处理（末尾总会抛出 FinishedException）。

    同时服务于“引用回复审批”和“手输审批命令”两个入口，避免重复代码。
    """
    # 先摘走再提交：多个管理员同时决策时只有抢到的人能提交。
    info = await _claim_pending_approval(approval_id, user_id, decision)
    if info is None:
        await _private_matcher.finish(_settled_approval_notice(approval_id))
    try:
        ok = await _client.approve(
            approval_id,
            decision=decision,
            comment=f"QQ admin {user_id} {decision}ed via private message.",
        )
    except Exception as exc:
        await _release_approval_claim(approval_id, info)
        logger.exception("submit QQ approval decision failed")
        await _private_matcher.finish(f"提交审批失败：{exc}")
    if not ok:
        await _release_approval_claim(approval_id, info)
        await _private_matcher.finish("审批提交被 Supervisor 拒绝，请检查日志。")
    logger.info(
        "approval decided on QQ: id=%s decision=%s admin=%s", approval_id, decision, user_id,
    )
    await _private_matcher.finish(
        f"已{('同意' if decision == 'allow' else '拒绝')}审批：{approval_id}\n"
        f"操作：{info.get('operation') or 'unknown'}"
    )


@_private_matcher.handle()
async def _handle_private_agent(bot: Bot, event: PrivateMessageEvent) -> None:
    """把当前 QQ 私聊请求提交给 ClonothZX。"""
    global _last_bot
    _last_bot = bot
    # on_bot_connect 已经认过一次，这里是兜底：漏认一次就是记忆分叉，代价不对等。
    _adopt_bot_scope(bot)

    if not _is_private_allowed(event):
        # 未放行的人连「这个号是不是 bot」都不该确认；要提示就自己填 channels.private_denied_reply。
        logger.info("QQ private message ignored: user=%s not allowed", getattr(event, "user_id", None))
        notice = live.private_denied_reply
        if notice:
            await _private_matcher.finish(notice)
        await _private_matcher.finish()

    if _client is None or _session_state is None:
        await _private_matcher.finish("Clonoth Agent 尚未初始化，请稍后重试。")

    _remember_message_for_reply_context(event)
    user_id = int(event.user_id)
    user_text = (await _event_text_with_forward(bot, event)).strip() or "你好"

    # 裸发“同意”不是审批意图：必须引用 bot 发出的审批卡片才认。
    reply_verb = _parse_approval_reply_verb(user_text)
    if reply_verb is not None:
        target = await _resolve_approval_reply_target(bot, event)
        if target.matched():
            if not _can("approval", event):
                denial = _capability_denial(
                    "approval", event, "你不是 Clonoth 审批管理员，不能处理审批请求。",
                )
                if denial is not None:
                    await _private_matcher.finish(denial)
                # denial is None：档位关掉且不是名单，当作没有这个命令，往下走普通聊天。
            else:
                if target.settled_id:
                    await _private_matcher.finish(_settled_approval_notice(target.settled_id))
                if target.approval_id:
                    await _finish_approval_decision(user_id, target.approval_id, reply_verb)
                await _private_matcher.finish(_APPROVAL_REPLY_UNRESOLVED_HINT)

    approval_command = _parse_approval_command(user_text)
    if approval_command is not None:
        if not _can("approval", event):
            denial = _capability_denial(
                "approval", event, "你不是 Clonoth 审批管理员，不能处理审批请求。",
            )
            if denial is not None:
                await _private_matcher.finish(denial)
            # denial is None：当作没有这个命令，往下走普通聊天。
        else:
            decision, approval_token = approval_command
            approval_id, error = _resolve_pending_approval_id(approval_token)
            if not approval_id:
                await _private_matcher.finish(error)
            await _finish_approval_decision(user_id, approval_id, decision)

    # 收到卡片后最自然的回复就是「同意」两个字，既没引用也没带 ID。这条没有卡片可依，
    # 所以要求带斜杠 —— 否则私聊里一句「ok」就能批掉一条待审批。只认本人名下恰好
    # 一条待审批的情况：0 条说明这就是句普通聊天，多条则无从判断指的是哪一条。
    if live.approval_bare_verb_unique:
        bare_verb = _parse_approval_reply_verb(_strip_command_prefix(user_text))
        if bare_verb is not None and _can("approval", event):
            unique_id = _unique_pending_approval_for(user_id)
            if unique_id:
                await _finish_approval_decision(user_id, unique_id, bare_verb)
            elif _pending_approvals_for(user_id) > 1:
                await _private_matcher.finish(
                    "你名下有多条待审批，分不清是哪一条。请引用那张卡片回复，"
                    "或发送：/审批 同意 <ID>。"
                )

    # 命令顺序与群侧一致：清记忆 / 死信 / 切模型在私聊里只有名单能用，而名单恒过闸门。
    help_reply = await _maybe_handle_help_command(event=event, user_text=user_text)
    if help_reply is not None:
        await _private_matcher.finish(help_reply)
    clear_mem_reply = await _maybe_handle_clear_group_memory_command(
        bot=bot,
        event=event,
        user_text=user_text,
    )
    if clear_mem_reply is not None:
        await _private_matcher.finish(clear_mem_reply)
    dead_letter_reply = await _maybe_handle_dead_letter_command(event=event, user_text=user_text)
    if dead_letter_reply is not None:
        await _private_matcher.finish(dead_letter_reply)
    model_reply = await _maybe_handle_model_command(event=event, user_text=user_text)
    if model_reply is not None:
        await _private_matcher.finish(model_reply)

    real_conversation_key = f"qq_private:{user_id}"
    stable_conversation_key = _stable_conversation_key(real_conversation_key)
    attachments, attachment_errors = await _collect_qq_attachments(bot, event, stable_conversation_key)
    _remember_recent_images(stable_conversation_key, event, attachments)
    dream_reply = await _maybe_handle_dream_command(
        event=event, user_text=user_text, conversation_key=stable_conversation_key,
    )
    if dream_reply is not None:
        await _private_matcher.finish(dream_reply)
    drawtools_reply = await _maybe_handle_drawtools_command(event=event, user_text=user_text)
    if drawtools_reply is not None:
        await _private_matcher.finish(drawtools_reply)
    custom_face_reply = await _maybe_handle_custom_face_command(
        bot=bot,
        event=event,
        user_text=user_text,
        conversation_key=stable_conversation_key,
        current_attachments=attachments,
    )
    if custom_face_reply is not None:
        await _private_matcher.finish(custom_face_reply)
    sticker_reply = await _maybe_handle_sticker_command(event=event, user_text=user_text)
    if sticker_reply is not None:
        await _private_matcher.finish(sticker_reply)
    proactive_reply = await _maybe_handle_proactive_command(
        bot=bot,
        event=event,
        user_text=user_text,
        conversation_key=stable_conversation_key,
        current_attachments=attachments,
    )
    if proactive_reply is not None:
        await _private_matcher.finish(proactive_reply)
    draw_direct_prompt = _parse_direct_draw_command(user_text)
    if draw_direct_prompt is None:
        # /生图 不吃历史图：混进来的图会把这一轮拽到视觉节点。
        await _merge_recent_attachments_after_text(event=event, conversation_key=stable_conversation_key, user_text=user_text, attachments=attachments)
    entry_node_id = DRAW_NODE_ID if draw_direct_prompt is not None else ""
    if draw_direct_prompt is not None:
        if attachments:
            attachments.clear()
            await _private_matcher.send(_DRAW_REFERENCE_IMAGE_NOTICE)
        inbound_text = await _build_draw_direct_inbound_text(event, draw_direct_prompt, True)
    else:
        inbound_text = await _build_private_inbound_text(event, bot, user_text, stable_conversation_key, attachments)
        inbound_text = _apply_attachment_hints(inbound_text, user_text, attachments, attachment_errors)

    platform_updates = {
        "bot": bot,
        "event": event,
        "type": "private",
        "user_id": user_id,
        "conversation_key": stable_conversation_key,
        "_source_attachments": [dict(att) for att in attachments if isinstance(att, dict)],
    }
    try:
        ok = await _enqueue_or_submit_inbound(QueuedInbound(
            matcher=_private_matcher,
            bot=bot,
            event=event,
            channel="qq_private",
            real_conversation_key=real_conversation_key,
            stable_conversation_key=stable_conversation_key,
            text=inbound_text,
            attachments=attachments or [],
            is_dm=True,
            platform_updates=platform_updates,
            user_text=user_text,
            entry_node_id=entry_node_id,
        ))
        try:
            await bot.call_api("set_input_status", user_id=int(event.user_id), event_type=1)
        except Exception:
            pass
    except Exception as exc:
        logger.exception("submit private inbound failed")
        await _private_matcher.finish(f"无法连接到 Clonoth Agent：{exc}")

    if not ok:
        await _private_matcher.finish("Clonoth Agent 未接受本次请求。")
    await _private_matcher.finish()
