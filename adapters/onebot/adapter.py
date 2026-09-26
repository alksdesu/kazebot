"""QQ 适配器的全部进程内状态，收在一个 OneBotAdapter 实例里。

不 import NoneBot：状态本身与平台库无关，插件入口负责造出唯一的实例。
"""
from __future__ import annotations

import asyncio
import contextvars
from collections import OrderedDict, defaultdict, deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, DefaultDict, Deque, Dict, List, Optional

from adapters._shared.active_account import ActiveAccountStore
from adapters._shared.aliases import AliasBook
from adapters._shared.approvals import ApprovalBook
from adapters._shared.conversation_queue import ConversationQueue
from adapters._shared.history_window import ClearBarriers, HistoryWindows
from adapters._shared.trigger_policy import CooldownState, TriggerDecision
from platforms.builtin import QQ_NAPCAT
from stickers.combat import CombatTracker

from .config import (
    ANON_MAP_FILE,
    ANON_MAP_RETENTION_DAYS,
    ANON_MAP_SAVE_MIN_INTERVAL,
    ONEBOT_IDEMPOTENCY_AMBIGUOUS_TTL_SECONDS,
    ONEBOT_IDEMPOTENCY_MAX_ITEMS,
    ONEBOT_IDEMPOTENCY_SENT_TTL_SECONDS,
    ONEBOT_IDEMPOTENCY_STORE_FILE,
    PENDING_APPROVAL_TTL_SECONDS,
    RECENT_FILE_MAX_ITEMS,
    RECENT_IMAGE_MAX_ITEMS,
)
from .emoji_handler import EmojiRenderer
from .segments import normalize_message_id
from .send_contract import OneBotIdempotencyStore

if TYPE_CHECKING:
    from adapters._shared.echo_policy import EchoEntry
    from adapters._shared.live_config import LiveReader
    from clonoth_sdk import ClonothClient, EventRouter, SessionState
    from stickers.collect import StickerCollector
    from stickers.store import StickerStore
    from stickers.tagger import StickerTagger

    from .feature_gateway import FeatureGateway
    from .napcat.callbacks import TangQiuCallbacks
    from .napcat.models import (
        GroupContentRecord,
        QueuedInbound,
        QueueReplyWaiter,
        _AttachmentSendLock,
    )

CONVERSATION_BUCKET_MAX_KEYS = 512


class BucketMap(OrderedDict):
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


class OneBotAdapter:
    """一个 NapCat 连接对应的全部可变状态。容量类参数读热载配置，改了由 reconcile 重建。"""

    def __init__(self, *, workspace: Path, live: "LiveReader", features: "FeatureGateway") -> None:
        self.workspace = Path(workspace)
        self.live = live
        self.features = features

        # ---- 与 Clonoth 的连接 ----
        self.client: Optional["ClonothClient"] = None
        self.session_state: Optional["SessionState"] = None
        self.event_router: Optional["EventRouter"] = None
        self.callbacks: Optional["TangQiuCallbacks"] = None
        self.router_task: Optional[asyncio.Task] = None
        self.live_reconcile_task: Optional[asyncio.Task] = None
        self.attachment_cleanup_task: Optional[asyncio.Task] = None
        self.last_bot: Any = None

        # ---- 群历史与水位 ----
        # 群聊上下文只保留最近 N 条：给入口节点社交语境，又不让每次 inbound 带过长历史。
        # engine 确认收下某轮 inbound 后水位推进到那轮带过的最大序号，之后只补新行 ——
        # 否则每条群消息都会被 20 轮 inbound 各带一次，在 durable history 里存 20 份。
        self.group_history = HistoryWindows(
            capacity=self.group_history_capacity,
            soft_cap=lambda: self.live.group_history_max,
            watermark=self.group_history_watermark,
        )
        # 群号 → 清空上下文那一刻的 inbound 序号。
        self.clear_barriers = ClearBarriers()
        # 借 group_history_max 定容量，但语义不同：这份副本只供 qq_forward 挑选转发候选，没有送达账本，
        # 因此不参与缺口记账、也不吃未送达倍数。调小群历史条数会顺带缩小可转发范围，至少 20 条。
        self.group_content_records: DefaultDict[int, Deque["GroupContentRecord"]] = defaultdict(
            lambda: deque(maxlen=self.group_content_capacity())
        )
        self.group_names: Dict[str, str] = {}
        self.group_names_at: float = 0.0
        self.group_names_seeded: bool = False
        # group_id -> (过期时间, {user_id}, {名片/昵称小写 -> user_id}, {user_id -> (card, nickname)})。
        # 发送前校验 at 目标是否本群成员（NapCat 解析不了 uid 会让整条消息失败），
        # 按名片/昵称反查真实 QQ 号，以及 @ 群友时优先用本群名片作显示名。
        self.group_member_cache: Dict[int, tuple[float, set[str], Dict[str, str], Dict[str, tuple[str, str]]]] = {}

        # ---- 会话路由 ----
        # EventRouter 回调只拿到 session/trigger，因此这里保存发送最终回复所需的平台对象。
        self.session_targets: Dict[str, Dict[str, Any]] = {}
        self.conversation_bots: Dict[str, Any] = {}
        self.real_conversation_keys: Dict[str, str] = {}
        self.stable_conversation_keys: Dict[str, str] = {}
        self.persisted_session_targets: Dict[str, Dict[str, Any]] = {}
        self.route_state_lock = asyncio.Lock()
        # 当前登录的 QQ 号，参与会话键摘要。进程启动时从盘上读回来：等第一条消息才知道
        # 账号的话，那之前算出的键会落在空作用域里，记忆静默分叉成两份。
        self.active_account_store = ActiveAccountStore(QQ_NAPCAT, self.workspace)
        self.active_bot_scope: str = self.active_account_store.load()
        self.refused_self_ids: set[str] = set()

        # ---- 附件 ----
        self.recent_images: BucketMap = BucketMap(
            lambda: deque(maxlen=RECENT_IMAGE_MAX_ITEMS), CONVERSATION_BUCKET_MAX_KEYS,
        )
        # QQ 群文件是独立一条消息、带不了 @，那条永远不会触发 bot。不在这里留一手，
        # 下一条「读一下上面那个文件」就再也拿不到它了。
        self.recent_files: BucketMap = BucketMap(
            lambda: deque(maxlen=RECENT_FILE_MAX_ITEMS), CONVERSATION_BUCKET_MAX_KEYS,
        )
        # 按会话记录 Bot 最近发出/生成的附件，供「把刚才生成的那张图发给 xx」检索。
        # 只保存工作区内本地路径与显示名，不涉及真实 QQ 号。
        self.recent_sent_attachments: BucketMap = BucketMap(
            lambda: deque(maxlen=max(RECENT_IMAGE_MAX_ITEMS, 20)), CONVERSATION_BUCKET_MAX_KEYS,
        )
        # 每个桶单调递增的附件序号：下标会随新附件入队和缺文件过滤而漂移，挑选只能按这个号。
        self.sent_attachment_seq: DefaultDict[str, int] = defaultdict(int)
        # Bot 发出的附件消息 message_id -> 本地附件路径。上下文里 Bot 的图被换成了占位符，
        # 用户引用这条消息要求「基于这张图继续改」时，靠它把原图带给 AI；普通表情包没有这条映射，仍然屏蔽。
        self.sent_message_attachments: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
        self.last_attachment_cleanup_at: float = 0.0
        self.attachment_send_locks: "OrderedDict[str, _AttachmentSendLock]" = OrderedDict()

        # ---- 复读 ----
        # 复读判定只看内容指纹和发言人，不用整条历史：判完就丢，不参与任何送达账本。
        self.echo_recent: DefaultDict[int, Deque["EchoEntry"]] = defaultdict(lambda: deque(maxlen=12))
        # 每个群最近跟过的那句，防止人再补一条又凑够数、Bot 一路接力。
        self.echo_last: Dict[int, tuple[str, float]] = {}

        # ---- 排队与发送 ----
        self.qq_queue: ConversationQueue["QueuedInbound"] = ConversationQueue()
        self.qq_waiting_replies: Dict[str, "QueueReplyWaiter"] = {}
        self.qq_completed_replies: "OrderedDict[int, None]" = OrderedDict()
        # 按 worker 下标存而不是列表：下标决定缩容时谁该退，某个 worker 意外退出后
        # 补位也要补回它原来那个空位，否则会出现两个同下标的 worker。
        self.qq_queue_tasks: Dict[int, asyncio.Task] = {}
        # 出站防重放分 pending / sent 两段：发送失败释放 pending，只有确认送达的才记成 sent。
        self.outbound_idempotency = OneBotIdempotencyStore(
            ONEBOT_IDEMPOTENCY_STORE_FILE,
            sent_ttl=ONEBOT_IDEMPOTENCY_SENT_TTL_SECONDS,
            max_items=ONEBOT_IDEMPOTENCY_MAX_ITEMS,
            ambiguous_ttl=ONEBOT_IDEMPOTENCY_AMBIGUOUS_TTL_SECONDS,
        )

        # ---- 触发判定 ----
        # 触发冷却。进程内内存：重启之后继续静默毫无道理，窗口本来就是秒级的。
        self.trigger_cooldown = CooldownState()
        self.trigger_decisions: "OrderedDict[tuple[str, ...], TriggerDecision]" = OrderedDict()
        self.auto_like_today: Dict[int, str] = {}

        # ---- 引用与消息文本缓存 ----
        self.reply_message_cache: Dict[str, Dict[str, Any]] = {}
        self.reply_message_cache_order: Deque[str] = deque()
        # 持久化的「message_id -> 本地附件」索引。NapCat get_msg 取不到引用消息时，
        # 仍能转发此前已经下载到 Clonoth 的图片/表情包。只保存路径和少量路由元数据。
        self.reply_attachment_cache: Dict[str, Dict[str, Any]] = {}
        self.forward_msg_cache: "OrderedDict[str, tuple[float, List[Dict[str, Any]]]]" = OrderedDict()
        self.event_text_cache: "OrderedDict[tuple[str, ...], str]" = OrderedDict()
        self.sticky_subjects: "OrderedDict[str, tuple[float, List[str]]]" = OrderedDict()

        # ---- 匿名映射 ----
        self.aliases = AliasBook(
            ANON_MAP_FILE,
            workspace=self.workspace,
            id_shape=QQ_NAPCAT.id_shape,
            retention_days=ANON_MAP_RETENTION_DAYS,
            save_min_interval=ANON_MAP_SAVE_MIN_INTERVAL,
        )
        self.anon_senders: "OrderedDict[str, str]" = OrderedDict()
        self.anon_sender_next: int = 0
        # QQ 用户身份/称呼 Profile。只影响模型可见的称呼和身份说明，不授予任何权限。
        self.qq_user_profiles: Dict[str, Dict[str, Any]] = {}

        # ---- 审批 ----
        self.approvals = ApprovalBook(
            id_shape=QQ_NAPCAT.id_shape,
            normalize_ref=normalize_message_id,
            ttl_seconds=PENDING_APPROVAL_TTL_SECONDS,
        )

        # ---- 表情 ----
        self.bqbs: List[str] = []
        self.custom_face_names: List[str] = []
        self.custom_face_metadata: List[Dict[str, Any]] = []
        self.sticker_store: Optional["StickerStore"] = None
        self.sticker_collector: Optional["StickerCollector"] = None
        self.sticker_tagger: Optional["StickerTagger"] = None
        # 发图时要记「这个会话刚发过这张」，而 process_emojis 的签名里没有会话键。
        self.sticker_send_conversation: contextvars.ContextVar[str] = contextvars.ContextVar(
            "sticker_send_conversation", default="",
        )
        self.sticker_selected: contextvars.ContextVar[set[str] | None] = contextvars.ContextVar(
            "sticker_selected", default=None,
        )
        self.sticker_delivery_locks: Dict[str, tuple[asyncio.Lock, int]] = {}
        self.sticker_receipt_task: Optional[asyncio.Task] = None
        self.sticker_combat = CombatTracker()
        # 出站 [表情:]/[at:] 渲染；resolver 在启动时注入，收藏表情缓存按账号分。
        self.emoji = EmojiRenderer()

        # ---- 热载状态发布 ----
        self.live_state_signature: str = ""
        self.live_state_published_at: float = 0.0

        # ---- 转发桥 ----
        self.forward_bridge_runner: Any = None
        self.forward_bridge_site: Any = None
        self.forward_bridge_started: bool = False
        self.forward_bridge_token_cache: str = ""

    def group_history_watermark(self, group_id: Any) -> int:
        """engine 侧已确认收到的最大群历史序号；-1 表示这一轮要带完整历史。"""
        if self.session_state is None:
            return -1
        return self.session_state.get_high_watermark(int(group_id))

    def group_history_capacity(self) -> int:
        """群历史 deque 的硬上限：软上限之上的位置只租给还没送到 engine 的行。"""
        return int(max(self.live.group_history_max, 0) * self.live.group_history_undelivered_ratio)

    def group_content_capacity(self) -> int:
        return max(self.live.group_history_max, 20)
