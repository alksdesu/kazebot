"""QQ 接话意愿判断的同步 RPC。

supervisor 从不直接调模型，所以判定要建一个 node task 交给 engine worker，再等它回来。
在飞数量必须封顶：worker 只有两三个，判定占满了真实对话就得排队。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 判定失败时统一按「不接话」处理。群里的 bot 突然插话比漏接一句更让人反感，
# 而模型不可用时「插话」恰恰是判定层最容易退化成的那个方向。
DEFAULT_AGREED = False


@dataclass
class IntentResult:
    agreed: bool = DEFAULT_AGREED
    reason: str = ""
    # 为什么没拿到模型答案："" 表示拿到了。试听和日志要能区分超时和被闸限流。
    error: str = ""
    raw: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "agreed": self.agreed,
            "reason": self.reason,
            "error": self.error,
            "decided": not self.error,
        }


@dataclass
class _Pending:
    conversation_key: str
    future: "asyncio.Future[IntentResult]"
    loop: asyncio.AbstractEventLoop


@dataclass
class IntentRegistry:
    """在飞判定表。task_id → 等结果的那个协程。"""

    _pending: dict[str, _Pending] = field(default_factory=dict)
    _by_conversation: dict[str, str] = field(default_factory=dict)

    def inflight(self) -> int:
        return len(self._pending)

    def has_conversation(self, conversation_key: str) -> bool:
        return str(conversation_key or "") in self._by_conversation

    def register(self, task_id: str, conversation_key: str) -> "asyncio.Future[IntentResult]":
        loop = asyncio.get_running_loop()
        future: asyncio.Future[IntentResult] = loop.create_future()
        self._pending[task_id] = _Pending(str(conversation_key or ""), future, loop)
        if conversation_key:
            self._by_conversation[str(conversation_key)] = task_id
        return future

    def discard(self, task_id: str) -> None:
        entry = self._pending.pop(task_id, None)
        if entry is None:
            return
        if self._by_conversation.get(entry.conversation_key) == task_id:
            self._by_conversation.pop(entry.conversation_key, None)

    def settle(self, task_id: str, result: IntentResult) -> bool:
        """交付结果。engine worker 的完成回调可能不在等待方那个线程上。"""
        entry = self._pending.get(task_id)
        if entry is None:
            return False
        self.discard(task_id)
        if entry.future.done():
            return False
        entry.loop.call_soon_threadsafe(_set_if_pending, entry.future, result)
        return True


def _set_if_pending(future: "asyncio.Future[IntentResult]", result: IntentResult) -> None:
    if not future.done():
        future.set_result(result)


def parse_intent_text(raw: str) -> IntentResult:
    """把节点的 finish 文本解析成判定。

    约定是 `yes|理由` / `no|理由`，但模型答成整句是常态，所以按开头的词判，
    认不出来就按「不接话」—— 不能把一句解析失败变成一次插话。
    """
    text = str(raw or "").strip()
    if not text:
        return IntentResult(error="empty", raw=raw or "")
    head, _, tail = text.partition("|")
    token = head.strip().strip(".。!！,，\"'").lower()
    reason = tail.strip() or text
    if token in {"yes", "y", "true", "1", "是", "要", "回", "接话"}:
        return IntentResult(agreed=True, reason=reason, raw=text)
    if token in {"no", "n", "false", "0", "否", "不", "不要", "不回", "不接话"}:
        return IntentResult(agreed=False, reason=reason, raw=text)
    lowered = text.lower()
    if lowered.startswith("yes"):
        return IntentResult(agreed=True, reason=reason, raw=text)
    if lowered.startswith("no"):
        return IntentResult(agreed=False, reason=reason, raw=text)
    logger.info("qq intent verdict unparsable, treating as no: %r", text[:200])
    return IntentResult(agreed=False, reason="判定结果无法解析", error="unparsable", raw=text)


def result_from_task(task: Any) -> IntentResult:
    """从完成的 task 里取判定。失败/取消也要给出结论，调用方不该再判一次分支。"""
    raw_result = getattr(task, "result", None)
    result = raw_result if isinstance(raw_result, dict) else {}
    action = str(result.get("action") or "").strip()
    if action in {"fail", "cancelled"}:
        return IntentResult(error=action, reason=str(result.get("error") or "").strip())
    inner = result.get("result") if isinstance(result.get("result"), dict) else {}
    return parse_intent_text(str(inner.get("text") or ""))


def build_instruction(
    *,
    text: str,
    context_lines: list[str],
    bot_names: list[str],
    speaker: str = "",
) -> str:
    """组装判定用的 instruction。

    只放已匿名化的群历史行和昵称，不放群号/QQ 号 —— 匿名化整套机制就是为了不让真实
    号码进模型上下文，判定这条新链路没有理由成为例外。
    """
    parts: list[str] = []
    names = [n for n in (str(x).strip() for x in bot_names) if n]
    if names:
        parts.append(f"群里这个 bot 平时被叫作：{'、'.join(names)}")
    if context_lines:
        parts.append("【最近的群聊】\n" + "\n".join(str(line) for line in context_lines))
    speaker_label = str(speaker or "").strip()
    current = str(text or "").strip() or "（空消息）"
    parts.append(f"【待判定的这一条】\n{speaker_label + ': ' if speaker_label else ''}{current}")
    parts.append("这句话是在跟 bot 说话吗？按要求调用 finish 返回 yes 或 no。")
    return "\n\n".join(parts)
