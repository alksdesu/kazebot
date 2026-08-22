"""QQ 群消息该不该回的判定。

七个信号各自独立开关，命中任一即触发；冷却是最后一层，管的是「命中也不说」。
纯逻辑不依赖 NoneBot：适配层从事件里提取事实，dry-run 端点从 JSON 构造同一份输入。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# 信号名。命中哪个要报给试听，所以是稳定字面量而不是随手写的字符串。
SIGNAL_ALL = "all"
SIGNAL_AT = "at"
SIGNAL_REPLY = "reply"
SIGNAL_NAME = "name"
SIGNAL_PREFIX = "prefix"
SIGNAL_KEYWORD = "keyword"
SIGNAL_RANDOM = "random"
SIGNAL_LLM_INTENT = "llm_intent"

# 判定顺序。llm_intent 不在其中：它是「前面全不命中」之后才问的那一步。
SIGNAL_ORDER: tuple[str, ...] = (
    SIGNAL_ALL, SIGNAL_AT, SIGNAL_REPLY, SIGNAL_NAME, SIGNAL_PREFIX, SIGNAL_KEYWORD, SIGNAL_RANDOM,
)

BLOCKED_BY_COOLDOWN = "cooldown"

# 冷却维度的内部标识 → 说法。reason 会被控制台的试听整句显示出来，中文句子里不该夹
# per_group 这种标识。
_COOLDOWN_SCOPE_NAMES = {"per_group": "本群", "per_user": "这个人"}

# 旧的单选枚举。新配置没写信号开关时用它反推，行为逐位不变。
_LEGACY_ALL_MODES = frozenset({"all", "always"})
_LEGACY_PREFIX_MODES = frozenset({"prefix", "prefix_or_mention", "mention_or_prefix"})


@dataclass(frozen=True)
class TriggerConfig:
    """一次判定用的全部配置。

    从 live 读还是从 dry-run 的 JSON 读，到这里已经没有区别 —— 试听和真实判定因此
    不可能漂移。
    """

    all_messages: bool = False
    at: bool = True
    reply: bool = True
    name: bool = False
    name_words: tuple[str, ...] = ()
    name_anywhere: bool = True
    prefix: bool = False
    prefix_words: tuple[str, ...] = ()
    keyword: bool = False
    keyword_words: tuple[str, ...] = ()
    random: bool = False
    random_probability: float = 0.0
    llm_intent: bool = False
    # 问一次模型要多久。冷却剩得比这还短就照样去问：答案到手时窗口已经过去了。
    llm_intent_timeout_sec: float = 0.0
    cooldown_group_sec: float = 0.0
    cooldown_user_sec: float = 0.0
    cooldown_exempt_at: bool = True

    @classmethod
    def from_live(cls, live: Any) -> "TriggerConfig":
        """按热载配置装配。三态信号开关为 None 时回落旧的 group_mode 语义。"""
        mode = str(live.group_trigger or "mention_only").lower()
        legacy_all = mode in _LEGACY_ALL_MODES
        legacy_prefix = mode in _LEGACY_PREFIX_MODES
        return cls(
            all_messages=_tri(live.signal_all, legacy_all),
            # @ 与被回复在旧实现里共用 event.to_me，两者当时都是开着的。
            at=_tri(live.signal_at, True),
            reply=_tri(live.signal_reply, True),
            name=bool(live.signal_name),
            name_words=tuple(live.name_words),
            name_anywhere=bool(live.name_anywhere),
            prefix=_tri(live.signal_prefix, legacy_prefix),
            prefix_words=tuple(live.trigger_prefixes),
            keyword=bool(live.signal_keyword),
            keyword_words=tuple(live.keyword_words),
            random=bool(live.signal_random),
            random_probability=float(live.random_probability),
            llm_intent=bool(live.llm_intent_enabled),
            llm_intent_timeout_sec=float(live.llm_intent_timeout_sec),
            cooldown_group_sec=float(live.cooldown_group_sec),
            cooldown_user_sec=float(live.cooldown_user_sec),
            cooldown_exempt_at=bool(live.cooldown_exempt_at),
        )

    def enabled_signals(self) -> tuple[str, ...]:
        """当前开着的信号，按判定顺序。给试听显示「哪些规则在生效」。"""
        active = {
            SIGNAL_ALL: self.all_messages, SIGNAL_AT: self.at, SIGNAL_REPLY: self.reply,
            SIGNAL_NAME: self.name, SIGNAL_PREFIX: self.prefix,
            SIGNAL_KEYWORD: self.keyword, SIGNAL_RANDOM: self.random,
        }
        return tuple(name for name in SIGNAL_ORDER if active[name])


def _tri(value: Any, legacy: bool) -> bool:
    """三态开关：None = 没配，跟随旧的 group_mode 推导值。"""
    return legacy if value is None else bool(value)


@dataclass(frozen=True)
class TriggerInput:
    """判定需要的全部事实。

    text 是原文 —— 未剥前缀、未删名字。NoneBot 的三个 to_me preprocessor 都会就地改
    正文（删 reply 段、删首尾 @、删昵称），拿改过的文本判定会让「用户点名了」这个
    信号在进模型前就丢掉。
    """

    text: str = ""
    group_id: int = 0
    user_id: int = 0
    at_me: bool = False
    reply_to_bot: bool = False
    now: float = 0.0
    # 随机插话的骰子，[0,1)。由外部注入而不是就地 random()：dry-run 要可复现。
    roll: float = 1.0


@dataclass(frozen=True)
class TriggerDecision:
    """判定结果。试听直接渲染它，所以每个字段都要能单独显示。"""

    triggered: bool = False
    signal: str = ""
    blocked_by: str = ""
    reason: str = ""
    # random / llm_intent 这类结果取决于运行时，试听不能伪造答案。
    undetermined: bool = False
    cooldown_remaining: float = 0.0

    def awaits_llm_intent(self) -> bool:
        """要不要真的去问模型。"""
        return self.signal == SIGNAL_LLM_INTENT and self.undetermined and not self.blocked_by

    def as_dict(self) -> dict[str, Any]:
        return {
            "triggered": self.triggered,
            "signal": self.signal,
            "blocked_by": self.blocked_by,
            "reason": self.reason,
            "undetermined": self.undetermined,
            "cooldown_remaining": round(self.cooldown_remaining, 3),
        }


class CooldownState:
    """按群/按人记最近一次触发时间。

    只放进程内内存，不落盘：重启之后继续静默毫无道理，而且冷却窗口本来就是秒级的。
    """

    __slots__ = ("_by_group", "_by_user")

    def __init__(self) -> None:
        self._by_group: dict[int, float] = {}
        self._by_user: dict[tuple[int, int], float] = {}

    def remaining(self, inp: TriggerInput, config: TriggerConfig) -> tuple[float, str]:
        """还要等多久，以及是被哪一维挡住的。返回 (0.0, "") 表示可以说话。"""
        checks = (
            (config.cooldown_group_sec, self._by_group.get(int(inp.group_id)), "per_group"),
            (config.cooldown_user_sec, self._by_user.get((int(inp.group_id), int(inp.user_id))), "per_user"),
        )
        worst = 0.0
        scope = ""
        for window, last, name in checks:
            if window <= 0 or last is None:
                continue
            left = window - (inp.now - last)
            if left > worst:
                worst, scope = left, name
        return (worst, scope) if worst > 0 else (0.0, "")

    def record(self, inp: TriggerInput) -> None:
        """记下这一次真的开口了。只有实际触发才调，被冷却挡住的不算。"""
        self._by_group[int(inp.group_id)] = inp.now
        self._by_user[(int(inp.group_id), int(inp.user_id))] = inp.now

    def forget_group(self, group_id: int) -> None:
        """清掉一个群的冷却。会话被重置时用，否则重置完还要干等一轮。"""
        gid = int(group_id)
        self._by_group.pop(gid, None)
        for key in [k for k in self._by_user if k[0] == gid]:
            self._by_user.pop(key, None)

    def forget_all(self) -> None:
        """清掉全部冷却。换号时用：上一个号的静默窗口跟新号没有关系。"""
        self._by_group.clear()
        self._by_user.clear()


def _matches_name(text: str, words: tuple[str, ...], *, anywhere: bool) -> str:
    """命中的名字，空串表示没命中。

    ASCII 名字要求词边界（"nai" 不该被 "naive" 命中），中日韩没有词边界概念，
    只能按子串比 —— 两种脚本各按各自的规则，不能一刀切。
    """
    value = (text or "").strip()
    if not value:
        return ""
    lowered = value.lower()
    for word in words:
        token = (word or "").strip()
        if not token:
            continue
        needle = token.lower()
        if not anywhere:
            if lowered.startswith(needle):
                return token
            continue
        if needle.isascii() and needle.isalnum():
            if re.search(rf"(?<![0-9a-z]){re.escape(needle)}(?![0-9a-z])", lowered):
                return token
            continue
        if needle in lowered:
            return token
    return ""


def _matches_keyword(text: str, words: tuple[str, ...]) -> str:
    lowered = (text or "").strip().lower()
    if not lowered:
        return ""
    for word in words:
        token = (word or "").strip()
        if token and token.lower() in lowered:
            return token
    return ""


def matched_prefix(text: str, words: tuple[str, ...]) -> str:
    """命中的触发前缀，空串表示没命中。"""
    value = (text or "").strip()
    for word in words:
        token = (word or "").strip()
        if token and value.startswith(token):
            return token
    return ""


def _first_signal(inp: TriggerInput, config: TriggerConfig) -> tuple[str, str, bool]:
    """按顺序找第一个命中的信号，返回 (信号名, 说明, 是否取决于运行时)。"""
    if config.all_messages:
        return SIGNAL_ALL, "全量模式：群内任意消息都回", False
    if config.at and inp.at_me:
        return SIGNAL_AT, "被 @", False
    if config.reply and inp.reply_to_bot:
        return SIGNAL_REPLY, "被回复", False
    if config.name:
        hit = _matches_name(inp.text, config.name_words, anywhere=config.name_anywhere)
        if hit:
            return SIGNAL_NAME, f"出现名字「{hit}」", False
    if config.prefix:
        hit = matched_prefix(inp.text, config.prefix_words)
        if hit:
            return SIGNAL_PREFIX, f"命中前缀「{hit}」", False
    if config.keyword:
        hit = _matches_keyword(inp.text, config.keyword_words)
        if hit:
            return SIGNAL_KEYWORD, f"命中关键词「{hit}」", False
    if config.random:
        probability = max(0.0, min(1.0, config.random_probability))
        if probability <= 0:
            return "", "", False
        return SIGNAL_RANDOM, f"随机插话（概率 {probability:.0%}）", True
    return "", "", False


def evaluate(
    inp: TriggerInput,
    config: TriggerConfig,
    cooldown: CooldownState | None = None,
    *,
    resolve_random: bool = True,
) -> TriggerDecision:
    """判这一条群消息要不要回。

    resolve_random=False 给试听用：随机与 LLM 意愿判断报成「取决于运行时」，不伪造答案 ——
    试听说会回、实际不回，比没有试听更糟。
    """
    signal, reason, runtime_dependent = _first_signal(inp, config)

    if signal == SIGNAL_RANDOM:
        if not resolve_random:
            return TriggerDecision(
                triggered=False, signal=SIGNAL_RANDOM, reason=reason, undetermined=True,
            )
        if inp.roll >= max(0.0, min(1.0, config.random_probability)):
            signal, reason, runtime_dependent = "", "", False

    if not signal:
        if config.llm_intent:
            # 冷却期内先挡下：问完再丢答案等于每条不命中消息白花一次模型调用。
            blocked = _cooldown_block(
                inp, config, cooldown, SIGNAL_LLM_INTENT,
                "其他规则都不命中，本来要交给模型判断",
                grace_sec=config.llm_intent_timeout_sec,
            )
            if blocked is not None:
                return blocked
            # 让模型判断的那一步不在这里做：它要一次异步往返，而这个函数是同步纯判定。
            return TriggerDecision(
                triggered=False, signal=SIGNAL_LLM_INTENT,
                reason="其他规则都不命中，交给模型判断要不要接话",
                undetermined=True,
            )
        return TriggerDecision(triggered=False, reason="没有命中任何开着的信号")

    return _apply_cooldown(inp, config, cooldown, signal, reason, runtime_dependent)


def _cooldown_block(
    inp: TriggerInput,
    config: TriggerConfig,
    cooldown: CooldownState | None,
    signal: str,
    hit_phrase: str,
    *,
    grace_sec: float = 0.0,
) -> TriggerDecision | None:
    """冷却挡住了就返回那个决定，没挡住返回 None。

    grace_sec 是「这个结论多久之后才用得上」：剩余冷却短于它，等结论到手窗口已经过去，
    这一次就不算被挡住。
    """
    if config.cooldown_exempt_at and signal == SIGNAL_AT:
        return None
    if cooldown is None:
        return None
    left, scope = cooldown.remaining(inp, config)
    if left <= max(0.0, grace_sec):
        return None
    return TriggerDecision(
        triggered=False, signal=signal, blocked_by=BLOCKED_BY_COOLDOWN,
        reason=f"{hit_phrase}，但{_COOLDOWN_SCOPE_NAMES.get(scope, scope)}的冷却还剩 {left:.1f}s",
        cooldown_remaining=left,
    )


def _apply_cooldown(
    inp: TriggerInput,
    config: TriggerConfig,
    cooldown: CooldownState | None,
    signal: str,
    reason: str,
    runtime_dependent: bool,
) -> TriggerDecision:
    """信号已经命中，再过一遍冷却。冷却是「即使命中也不说」，和信号是不同维度。"""
    blocked = _cooldown_block(inp, config, cooldown, signal, f"命中{reason}")
    if blocked is not None:
        return blocked

    return TriggerDecision(
        triggered=True, signal=signal, reason=reason, undetermined=runtime_dependent,
    )


def resolve_llm_intent(
    inp: TriggerInput,
    config: TriggerConfig,
    cooldown: CooldownState | None = None,
    *,
    agreed: bool,
    reason: str = "",
) -> TriggerDecision:
    """模型答完之后的收尾判定。

    单独开一个入口而不是给 evaluate 加参数：意愿判断要一次异步往返，调用方拿到答案时
    早就离开 evaluate 了。冷却层必须在这里再走一遍 —— 漏掉就等于「开了意愿判断 =
    冷却失效」，而这条路恰恰是最容易刷屏的那条。
    """
    if not agreed:
        return TriggerDecision(
            triggered=False, signal=SIGNAL_LLM_INTENT,
            reason=reason or "模型判断这句话不是在跟 bot 说话",
        )
    return _apply_cooldown(
        inp, config, cooldown, SIGNAL_LLM_INTENT,
        reason or "模型判断这句话是在跟 bot 说话", False,
    )
