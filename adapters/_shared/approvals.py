"""审批从挂起到定案的流转：待决、已决、卡片锚点，以及管理员回复里的决策词。

卡片怎么渲染和发送、平台消息 ID 怎么归一化由适配器给；回执怎么措辞也由适配器按查询结果决定。
"""
from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable

from platforms.ids import normalize_id

DECISION_ALLOW = "allow"
DECISION_DENY = "deny"
DECISION_EXPIRED = "expired"

ALLOW_VERBS = frozenset({
    "同意", "批准", "通过", "允许", "allow", "approve", "approved", "ok", "yes", "y",
})
DENY_VERBS = frozenset({
    "拒绝", "驳回", "不同意", "不批准", "deny", "reject", "rejected", "no", "n",
})
COMMAND_PREFIXES = ("审批", "approval")
_VERB_STRIP = " 　。，、！!.~"
# 形状与 approval_id（uuid4）绑定：裸动词命令只在 token 像 id 时才当审批，避免劫持普通私聊。
_TOKEN_RE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{3,}$")

LOOKUP_FOUND = "found"
LOOKUP_NO_TOKEN = "no_token"
LOOKUP_AMBIGUOUS = "ambiguous"
LOOKUP_SETTLED = "settled"
LOOKUP_UNKNOWN = "unknown"

_CARD_LIMIT = 500
_SETTLED_LIMIT = 200


def approval_verb(token: str) -> str | None:
    """命令和引用回复共用的动词判定：整词匹配，返回 allow / deny / None。"""
    word = (token or "").strip().strip(_VERB_STRIP).lower()
    if not word:
        return None
    if word in DENY_VERBS:
        return DECISION_DENY
    if word in ALLOW_VERBS:
        return DECISION_ALLOW
    return None


def parse_command(body: str) -> tuple[str, str] | None:
    """解析已去掉命令引导符的审批命令，返回 (decision, approval_id 或前缀)。

    「审批 同意 <ID>」照收；省掉「审批」的裸动词命令，token 必须像 approval_id，否则「通过 微信发给我」会被当成审批。
    """
    normalized = re.sub(r"\s+", " ", body or "")
    if not normalized:
        return None
    parts = normalized.split(" ")
    if len(parts) < 2:
        return None
    if parts[0] in COMMAND_PREFIXES and len(parts) >= 3:
        decision = approval_verb(parts[1])
        token = parts[2]
        explicit_prefix = True
    else:
        decision = approval_verb(parts[0])
        token = parts[1]
        explicit_prefix = False
    if decision is None:
        return None
    if not explicit_prefix and not _TOKEN_RE.match(token):
        return None
    return decision, token


def parse_reply_verb(text: str) -> str | None:
    """引用卡片回复里的审批意图：整句必须就是一个动词，可以带「审批」前缀。"""
    # 子串匹配会让 know/note/now 命中 no，所以整词。
    normalized = re.sub(r"\s+", "", (text or "").strip()).lower()
    if not normalized:
        return None
    for prefix in COMMAND_PREFIXES:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):].lstrip(":：")
    return approval_verb(normalized)


@dataclass(frozen=True)
class ApprovalCard:
    """一张审批卡片的锚点：卡片对应的审批与收件管理员。"""

    approval_id: str
    admin_id: str


@dataclass(frozen=True)
class ApprovalReplyTarget:
    """引用快捷审批的反查结果。from_card 为真表示确认引用的是 bot 发出的审批卡片。"""

    approval_id: str = ""
    settled_id: str = ""
    from_card: bool = False

    def matched(self) -> bool:
        return bool(self.approval_id or self.settled_id or self.from_card)


@dataclass(frozen=True)
class ApprovalLookup:
    """按 ID 或前缀查审批的结果。FOUND 时 approval_id 是待审批，SETTLED 时是已处理的那条。"""

    outcome: str
    token: str = ""
    approval_id: str = ""


class ApprovalBook:
    """待决、已决和卡片锚点。

    账号 ID 按平台的 ID 形状规范化，卡片消息 ID 按构造方给的规则归一化：写入和反查走同一条规则才对得上。
    """

    def __init__(
        self,
        *,
        id_shape: str,
        normalize_ref: Callable[[Any], str],
        ttl_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._id_shape = id_shape
        self._normalize_ref = normalize_ref
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        # approval_id → 操作、详情、来源会话、挂起时间。
        self.pending: dict[str, dict[str, Any]] = {}
        # 已决或已失效：重复决策、引用已处理的卡片时告知，而不是误批另一条。
        self.settled: OrderedDict[str, dict[str, Any]] = OrderedDict()
        # 卡片消息 ID → 锚点。记下收件人，反查时校验引用者就是这张卡片的收件人。
        self.cards: OrderedDict[str, ApprovalCard] = OrderedDict()
        # 多个管理员同时决策同一条时，只有抢到锁的人能提交。
        self.lock = asyncio.Lock()

    @property
    def ttl_seconds(self) -> float:
        return self._ttl_seconds

    def _account(self, value: Any) -> str:
        return normalize_id(value, self._id_shape)

    def open(
        self,
        approval_id: str,
        *,
        operation: str,
        details: dict[str, Any] | None,
        conversation_key: str = "",
        session_id: str = "",
    ) -> None:
        """挂起一条审批，顺手收掉已经超时的。"""
        self.prune_expired()
        self.pending[approval_id] = {
            "operation": operation,
            "details": dict(details or {}),
            "conversation_key": conversation_key,
            "session_id": session_id,
            "created_at": self._clock(),
        }

    def remember_card(self, message_ref: Any, approval_id: str, admin_id: Any) -> None:
        """记下发给某位管理员的卡片，供引用卡片回复时反查。"""
        if not approval_id:
            return
        key = self._normalize_ref(message_ref)
        admin = self._account(admin_id)
        if not key or not admin:
            return
        self.cards[key] = ApprovalCard(approval_id=approval_id, admin_id=admin)
        self.cards.move_to_end(key)
        while len(self.cards) > _CARD_LIMIT:
            self.cards.popitem(last=False)

    def card_recipients(self, approval_id: str) -> set[str]:
        """这条审批的卡片实际发给了哪些管理员。"""
        if not approval_id:
            return set()
        return {card.admin_id for card in self.cards.values() if card.approval_id == approval_id}

    def settle(self, approval_id: str, *, decision: str, admin_id: Any = "", operation: str = "") -> None:
        """登记一条已决或已失效的审批。admin_id 留空表示不是人作的决定（超时、自动拒绝）。"""
        if not approval_id:
            return
        self.settled[approval_id] = {
            "decision": decision,
            "admin_id": self._account(admin_id),
            "at": self._clock(),
            "operation": operation,
        }
        self.settled.move_to_end(approval_id)
        while len(self.settled) > _SETTLED_LIMIT:
            self.settled.popitem(last=False)

    def close(self, approval_id: str, *, decision: str, admin_id: Any = "") -> dict[str, Any] | None:
        """摘掉一条待审批并登记定案，返回它挂起时的信息；本来就不在待审批里也照样登记。"""
        info = self.pending.pop(approval_id, None)
        self.settle(
            approval_id, decision=decision, admin_id=admin_id,
            operation=str((info or {}).get("operation") or ""),
        )
        return info

    def prune_expired(self) -> None:
        """超过 TTL 的待审批转成已失效。engine 那边到点已经自动拒绝，这里同步收尾。"""
        if not self.pending:
            return
        now = self._clock()
        expired = [
            approval_id for approval_id, info in self.pending.items()
            if now - (float(info.get("created_at") or 0) or now) > self._ttl_seconds
        ]
        for approval_id in expired:
            self.close(approval_id, decision=DECISION_EXPIRED)

    async def claim(self, approval_id: str, admin_id: Any, decision: str) -> dict[str, Any] | None:
        """原子领取待审批：抢到才返回挂起时的信息，领取即登记定案。"""
        async with self.lock:
            self.prune_expired()
            info = self.pending.pop(approval_id, None)
            if info is None:
                return None
            # 领取后进程崩溃，这条就停在已决、再也批不了；engine 到点自动拒绝，属于 fail-closed。
            self.settle(
                approval_id, decision=decision, admin_id=admin_id,
                operation=str(info.get("operation") or ""),
            )
            return info

    async def release(self, approval_id: str, info: dict[str, Any]) -> None:
        """提交没成功：放回待审批让人重试，一次网络错误不该把审批永久吞掉。"""
        async with self.lock:
            self.settled.pop(approval_id, None)
            self.pending[approval_id] = info

    def resolve_card(self, message_ref: Any, user_id: Any) -> ApprovalReplyTarget:
        """按被引用的消息 ID 反查卡片，收件人必须是引用者本人。"""
        key = self._normalize_ref(message_ref)
        if not key:
            return ApprovalReplyTarget()
        card = self.cards.get(key)
        if card is None:
            return ApprovalReplyTarget()
        user = self._account(user_id)
        if not user or card.admin_id != user:
            return ApprovalReplyTarget()
        return self.target_for(card.approval_id)

    def target_for(self, approval_id: str) -> ApprovalReplyTarget:
        """已经确认引用的是审批卡片，按那条审批现在的状态给出目标。"""
        if approval_id and approval_id in self.pending:
            return ApprovalReplyTarget(approval_id=approval_id, from_card=True)
        if approval_id and approval_id in self.settled:
            return ApprovalReplyTarget(settled_id=approval_id, from_card=True)
        return ApprovalReplyTarget(from_card=True)

    def unique_pending_for(self, user_id: Any) -> str:
        """恰好一条待审批、且那张卡片确实发给了本人时返回它的 ID，否则空串。"""
        self.prune_expired()
        if len(self.pending) != 1:
            return ""
        only_id = next(iter(self.pending))
        user = self._account(user_id)
        if not user or user not in self.card_recipients(only_id):
            return ""
        return only_id

    def pending_count_for(self, user_id: Any) -> int:
        """本人收到过卡片的待审批条数。"""
        user = self._account(user_id)
        if not user:
            return 0
        return sum(1 for approval_id in self.pending if user in self.card_recipients(approval_id))

    def known_ids(self) -> list[str]:
        """待审批在前、已处理在后的全部审批 ID。"""
        return [*self.pending, *self.settled]

    def lookup(self, token: str) -> ApprovalLookup:
        """按完整 ID 或唯一前缀找待审批；找不到时看是不是已经处理过。"""
        self.prune_expired()
        token = (token or "").strip()
        if not token:
            return ApprovalLookup(LOOKUP_NO_TOKEN)
        if token in self.pending:
            return ApprovalLookup(LOOKUP_FOUND, token, token)
        matches = [approval_id for approval_id in self.pending if approval_id.startswith(token)]
        if len(matches) == 1:
            return ApprovalLookup(LOOKUP_FOUND, token, matches[0])
        if matches:
            return ApprovalLookup(LOOKUP_AMBIGUOUS, token)
        settled = [
            approval_id for approval_id in self.settled
            if approval_id == token or approval_id.startswith(token)
        ]
        if len(settled) == 1:
            return ApprovalLookup(LOOKUP_SETTLED, token, settled[0])
        return ApprovalLookup(LOOKUP_UNKNOWN, token)
