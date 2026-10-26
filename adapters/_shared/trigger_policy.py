"""群消息该不该回的判定。

七个信号各自独立开关，命中任一即触发；冷却是最后一层，管的是「命中也不说」。
纯逻辑不依赖平台库：适配层从事件里提取事实，dry-run 端点从 JSON 构造同一份输入。
"""
from __future__ import annotations

import math
import re
from collections import deque
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Mapping

from platforms.ids import normalize_id
from platforms.inbound import InboundFacts

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

# 触发来源能取的全部值，也是 output.reply_to_trigger_sources 能写的词。
TRIGGER_SOURCE_NAMES = frozenset({*SIGNAL_ORDER, SIGNAL_LLM_INTENT})

# 这几种来源算有人在找 bot：回复引用原消息、不占主动回复的额度、bot 随之进场。
EXPLICIT_SIGNALS = frozenset({SIGNAL_AT, SIGNAL_REPLY, SIGNAL_NAME, SIGNAL_PREFIX})

BLOCKED_BY_COOLDOWN = "cooldown"
BLOCKED_BY_GATE = "gate"
BLOCKED_BY_DENSITY = "density"

PRESENCE_PRESENT = "present"
PRESENCE_ABSENT = "absent"

# 安静等待里一条消息的去向：在等、被后来的盖过、到点由它交给模型、被显式触发接过去。
QUIET_WAITING = "waiting"
QUIET_MERGED = "merged"
QUIET_DUE = "due"
QUIET_CANCELLED = "cancelled"

GATE_PENDING = "pending"
GATE_PASSED = "passed"
GATE_MISSED = "missed"

# 冷却维度的内部标识 → 说法。reason 会被控制台的试听整句显示出来，中文句子里不该夹
# per_group 这种标识。
_COOLDOWN_SCOPE_NAMES = MappingProxyType({"per_group": "本群", "per_user": "这个人"})

# 旧的单选枚举。新配置没写信号开关时用它反推，行为逐位不变；控制台显示推导值也认这两份。
LEGACY_ALL_MODES = frozenset({"all", "always"})
LEGACY_PREFIX_MODES = frozenset({"prefix", "prefix_or_mention", "mention_or_prefix"})

# 社区按群设置里能改判定的项 → TriggerConfig 字段。值为 None 是这个群没写，跟随平台配置。
GROUP_OVERRIDE_FIELDS = MappingProxyType({
    "interject_presence": "presence",
    "interject_absent_probability": "absent_judge_probability",
})

# 每群留的主动回复时间戳上限。按条数而不是按窗口裁：窗口能热改，按旧窗口裁掉的就找不回来了。
_PROACTIVE_HISTORY_MAX = 256


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
    presence: bool = False
    presence_window_sec: float = 90.0
    absent_judge_probability: float = 0.1
    quiet_wait_sec: float = 4.0
    quiet_max_wait_sec: float = 20.0
    # 0 = 不限。
    proactive_max_replies: int = 3
    proactive_window_sec: float = 300.0

    @classmethod
    def from_live(cls, live: Any) -> "TriggerConfig":
        """按热载配置装配。三态信号开关为 None 时回落旧的 group_mode 语义。

        在场那几项用 getattr 取：试听端点收的是控制台交来的值表，旧页面不带这几个键。
        """
        mode = str(live.group_trigger or "mention_only").lower()
        legacy_all = mode in LEGACY_ALL_MODES
        legacy_prefix = mode in LEGACY_PREFIX_MODES
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
            presence=bool(getattr(live, "presence_enabled", cls.presence)),
            presence_window_sec=float(getattr(live, "presence_window_sec", cls.presence_window_sec)),
            absent_judge_probability=float(getattr(live, "absent_judge_probability", cls.absent_judge_probability)),
            quiet_wait_sec=float(getattr(live, "quiet_wait_sec", cls.quiet_wait_sec)),
            quiet_max_wait_sec=float(getattr(live, "quiet_max_wait_sec", cls.quiet_max_wait_sec)),
            proactive_max_replies=int(getattr(live, "proactive_max_replies", cls.proactive_max_replies)),
            proactive_window_sec=float(getattr(live, "proactive_window_sec", cls.proactive_window_sec)),
        )

    @property
    def presence_active(self) -> bool:
        """在场判断只管「要不要去问模型」，没开意愿判断的群没有这一步。"""
        return self.presence and self.llm_intent

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


def with_group_settings(config: TriggerConfig, settings: Mapping[str, Any] | None) -> TriggerConfig:
    """叠上社区的按群设置：回复策略开着的群等于开了意愿判断，写了值的覆盖项盖过平台配置。

    设置是从 HTTP 拿回来的，类型不对的一律当没写，不让一个坏值改掉整群的判定。
    """
    if not isinstance(settings, Mapping):
        return config
    changes: dict[str, Any] = {}
    if settings.get("response_policy_enabled") is True:
        changes["llm_intent"] = True
    presence = settings.get("interject_presence")
    if isinstance(presence, bool):
        changes[GROUP_OVERRIDE_FIELDS["interject_presence"]] = presence
    probability = settings.get("interject_absent_probability")
    if isinstance(probability, (int, float)) and not isinstance(probability, bool) and math.isfinite(probability):
        changes[GROUP_OVERRIDE_FIELDS["interject_absent_probability"]] = max(0.0, min(1.0, float(probability)))
    return replace(config, **changes) if changes else config


@dataclass(frozen=True)
class TriggerInput:
    """判定需要的全部事实。

    text 是原文 —— 未剥前缀、未删名字。平台框架的预处理常会就地改正文（删回复段、
    删首尾 @、删昵称），拿改过的文本判定会让「用户点名了」这个信号在进模型前就丢掉。
    """

    text: str = ""
    group_id: str = ""
    user_id: str = ""
    at_me: bool = False
    reply_to_bot: bool = False
    now: float = 0.0
    # 随机插话和离场概率门共用的骰子，[0,1)。由外部注入而不是就地 random()：dry-run 要可复现。
    roll: float = 1.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "group_id", normalize_id(self.group_id))
        object.__setattr__(self, "user_id", normalize_id(self.user_id))

    @classmethod
    def from_facts(cls, facts: InboundFacts, *, now: float, roll: float = 1.0) -> "TriggerInput":
        return cls(
            text=facts.text,
            group_id=facts.conversation.raw_id if facts.conversation.is_group else "",
            user_id=facts.sender.platform_id,
            at_me=facts.mentions_self,
            reply_to_bot=facts.reply_to_self,
            now=now,
            roll=roll,
        )


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
    # 这条消息是怎么找上 bot 的，见 trigger_source。发送时据此决定引不引用、点不点赞。
    source: str = ""
    # 下面几项只在开了在场判断的群里有值。
    presence: str = ""
    quiet: str = ""
    gate: str = ""
    gate_probability: float = 0.0
    # 主动回复还剩几次；None = 不限或不适用。
    density_left: int | None = None

    @property
    def explicit(self) -> bool:
        return self.source in EXPLICIT_SIGNALS

    def awaits_llm_intent(self) -> bool:
        """要不要现在就去问模型。安静等待里的消息要等到点，不在此列。"""
        return (
            self.signal == SIGNAL_LLM_INTENT and self.undetermined and not self.blocked_by
            and self.quiet != QUIET_WAITING
        )

    def awaits_quiet(self) -> bool:
        """要不要进这个群的安静等待。"""
        return self.quiet == QUIET_WAITING and not self.blocked_by

    def as_dict(self) -> dict[str, Any]:
        return {
            "triggered": self.triggered,
            "signal": self.signal,
            "blocked_by": self.blocked_by,
            "reason": self.reason,
            "undetermined": self.undetermined,
            "cooldown_remaining": round(self.cooldown_remaining, 3),
        }

    def status_dict(self) -> dict[str, Any]:
        """来源和在场判断带出来的状态，给试听的状态列用；as_dict 只放判定结论本身。"""
        return {
            "source": self.source,
            "presence": self.presence,
            "quiet": self.quiet,
            "gate": self.gate,
            "gate_probability": round(self.gate_probability, 4),
            "density_left": self.density_left,
        }


class CooldownState:
    """按群/按人记最近一次触发时间。

    只放进程内内存，不落盘：重启之后继续静默毫无道理，而且冷却窗口本来就是秒级的。
    """

    __slots__ = ("_by_group", "_by_user")

    def __init__(self) -> None:
        self._by_group: dict[str, float] = {}
        self._by_user: dict[tuple[str, str], float] = {}

    def remaining(self, inp: TriggerInput, config: TriggerConfig) -> tuple[float, str]:
        """还要等多久，以及是被哪一维挡住的。返回 (0.0, "") 表示可以说话。"""
        checks = (
            (config.cooldown_group_sec, self._by_group.get(inp.group_id), "per_group"),
            (config.cooldown_user_sec, self._by_user.get((inp.group_id, inp.user_id)), "per_user"),
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
        self._by_group[inp.group_id] = inp.now
        self._by_user[(inp.group_id, inp.user_id)] = inp.now

    def forget_group(self, group_id: Any) -> None:
        """清掉一个群的冷却。会话被重置时用，否则重置完还要干等一轮。"""
        gid = normalize_id(group_id)
        self._by_group.pop(gid, None)
        for key in [k for k in self._by_user if k[0] == gid]:
            self._by_user.pop(key, None)

    def forget_all(self) -> None:
        """清掉全部冷却。换号时用：上一个号的静默窗口跟新号没有关系。"""
        self._by_group.clear()
        self._by_user.clear()


@dataclass(frozen=True)
class QuietWait:
    """一个群的一轮安静等待。每来一条非显式消息就续一次，到点只拿最后一条去问模型。

    两个时长随最后一条记下：中途热改了，按最新的值算。
    """

    cycle: int
    started: float
    last: float
    count: int
    wait_sec: float
    max_wait_sec: float

    @property
    def deadline(self) -> float:
        return min(self.last + max(0.0, self.wait_sec), self.started + max(0.0, self.max_wait_sec))


class PresenceState:
    """按群记 bot 在不在场、上次说话的时间、主动回复的次数，以及正在进行的安静等待。

    和 CooldownState 一样只放内存：重启后一律从离场开始，这几个窗口本来就只有几分钟。
    """

    __slots__ = ("_entered", "_spoke", "_proactive", "_quiet", "_cycles")

    def __init__(self) -> None:
        self._entered: dict[str, float] = {}
        self._spoke: dict[str, float] = {}
        self._proactive: dict[str, deque[float]] = {}
        self._quiet: dict[str, QuietWait] = {}
        self._cycles = 0

    def enter(self, group_id: Any, now: float) -> None:
        """一次进场事件：被直接叫到，或 bot 刚在群里说了话。晚到的旧事件不把时间往回拨。"""
        gid = normalize_id(group_id)
        self._entered[gid] = max(now, self._entered.get(gid, now))

    def bot_spoke(self, group_id: Any, now: float) -> None:
        """bot 在这个群发出了一条消息：记下时间，同时算一次进场。"""
        gid = normalize_id(group_id)
        self._spoke[gid] = max(now, self._spoke.get(gid, now))
        self.enter(gid, now)

    def is_present(self, group_id: Any, now: float, window_sec: float) -> bool:
        last = self._entered.get(normalize_id(group_id))
        return last is not None and window_sec > 0 and now - last < window_sec

    def present_until(self, group_id: Any, window_sec: float) -> float | None:
        last = self._entered.get(normalize_id(group_id))
        return None if last is None or window_sec <= 0 else last + window_sec

    def since_bot_spoke(self, group_id: Any, now: float) -> float | None:
        last = self._spoke.get(normalize_id(group_id))
        return None if last is None else max(0.0, now - last)

    def proactive_count(self, group_id: Any, now: float, window_sec: float) -> int:
        stamps = self._proactive.get(normalize_id(group_id))
        if not stamps or window_sec <= 0:
            return 0
        return sum(1 for stamp in stamps if 0 <= now - stamp < window_sec)

    def density_left(self, group_id: Any, now: float, config: TriggerConfig) -> int | None:
        """主动回复还剩几次；不限时返回 None。"""
        if config.proactive_max_replies <= 0:
            return None
        used = self.proactive_count(group_id, now, config.proactive_window_sec)
        return max(0, config.proactive_max_replies - used)

    def record_proactive(self, group_id: Any, now: float) -> float:
        """记一次主动回复，返回这一笔的时间戳：最后没发出去时拿它撤销。"""
        stamps = self._proactive.setdefault(normalize_id(group_id), deque())
        stamps.append(now)
        while len(stamps) > _PROACTIVE_HISTORY_MAX:
            stamps.popleft()
        return now

    def refund_proactive(self, group_id: Any, stamp: float) -> bool:
        """撤销一笔主动回复。判了要回，结果模型选择不发言，或者让给了正式请求。"""
        stamps = self._proactive.get(normalize_id(group_id))
        if not stamps:
            return False
        try:
            stamps.remove(stamp)
        except ValueError:
            return False
        return True

    def quiet(self, group_id: Any) -> QuietWait | None:
        return self._quiet.get(normalize_id(group_id))

    def join_quiet(self, group_id: Any, now: float, *, wait_sec: float, max_wait_sec: float) -> QuietWait:
        """登记一条进安静等待的消息。上一轮已经到点就另起一轮，否则续上并重新计时。"""
        gid = normalize_id(group_id)
        current = self._quiet.get(gid)
        if current is None or now >= current.deadline:
            self._cycles += 1
            wait = QuietWait(self._cycles, now, now, 1, wait_sec, max_wait_sec)
        else:
            wait = replace(
                current, last=max(current.last, now), count=current.count + 1,
                wait_sec=wait_sec, max_wait_sec=max_wait_sec,
            )
        self._quiet[gid] = wait
        return wait

    def close_quiet(self, group_id: Any, cycle: int) -> QuietWait | None:
        """到点收尾，交出这一轮。轮次对不上说明已经被收过、取消过或换了新的一轮。"""
        gid = normalize_id(group_id)
        current = self._quiet.get(gid)
        if current is None or current.cycle != cycle:
            return None
        del self._quiet[gid]
        return current

    def cancel_quiet(self, group_id: Any) -> QuietWait | None:
        """作废正在等的这一轮：显式触发或模型已经决定接话，这一轮的回复会带上那几条。"""
        return self._quiet.pop(normalize_id(group_id), None)

    def forget_group(self, group_id: Any) -> None:
        """会话被重置时清掉这个群的在场状态，清空之后从离场开始。"""
        gid = normalize_id(group_id)
        for table in (self._entered, self._spoke, self._proactive, self._quiet):
            table.pop(gid, None)

    def forget_all(self) -> None:
        """换号时全部清掉：上一个号在哪个群说过话跟新号没有关系。"""
        for table in (self._entered, self._spoke, self._proactive, self._quiet):
            table.clear()


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


def trigger_source(inp: TriggerInput, signal: str = "") -> str:
    """这条消息是怎么找上 bot 的，用信号名表示。

    被 @、回复 bot 是消息本身的事实，对应信号关着也算有人在叫它。点名和前缀只认信号命中：
    名字信号关着时名字只是闲聊里的一个词，前缀信号关着时句首的「！」也只是语气。其余按命中的信号报。
    """
    if signal in (SIGNAL_AT, SIGNAL_REPLY, SIGNAL_NAME, SIGNAL_PREFIX):
        return signal
    if inp.at_me:
        return SIGNAL_AT
    if inp.reply_to_bot:
        return SIGNAL_REPLY
    return signal


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
    presence: PresenceState | None = None,
    resolve_random: bool = True,
) -> TriggerDecision:
    """判这一条群消息要不要回。

    resolve_random=False 给试听用：随机、概率门与 LLM 意愿判断报成「取决于运行时」，不伪造
    答案 —— 试听说会回、实际不回，比没有试听更糟。开了在场判断的群走 _evaluate_with_presence。
    """
    if config.presence_active:
        return _evaluate_with_presence(inp, config, cooldown, presence, resolve_random=resolve_random)

    signal, reason, runtime_dependent = _first_signal(inp, config)

    if signal == SIGNAL_RANDOM:
        if not resolve_random:
            return _sourced(inp, config, TriggerDecision(
                triggered=False, signal=SIGNAL_RANDOM, reason=reason, undetermined=True,
            ))
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
                return _sourced(inp, config, blocked)
            # 让模型判断的那一步不在这里做：它要一次异步往返，而这个函数是同步纯判定。
            return _sourced(inp, config, TriggerDecision(
                triggered=False, signal=SIGNAL_LLM_INTENT,
                reason="其他规则都不命中，交给模型判断要不要接话",
                undetermined=True,
            ))
        return _sourced(inp, config, TriggerDecision(triggered=False, reason="没有命中任何开着的信号"))

    return _sourced(inp, config, _apply_cooldown(inp, config, cooldown, signal, reason, runtime_dependent))


def _sourced(inp: TriggerInput, config: TriggerConfig, decision: TriggerDecision) -> TriggerDecision:
    return replace(decision, source=decision.source or trigger_source(inp, decision.signal))


def _gate_probability(config: TriggerConfig) -> float:
    """离场时的概率门。随机插话并进这道门：开着时取两个概率里大的那个，掷中了也是先问模型。"""
    probability = config.absent_judge_probability
    if config.random:
        probability = max(probability, config.random_probability)
    return max(0.0, min(1.0, probability))


def _density_reason(config: TriggerConfig) -> str:
    return f"主动回复已到上限（{config.proactive_window_sec:g}s 内 {config.proactive_max_replies} 次）"


def _evaluate_with_presence(
    inp: TriggerInput,
    config: TriggerConfig,
    cooldown: CooldownState | None,
    presence: PresenceState | None,
    *,
    resolve_random: bool,
) -> TriggerDecision:
    """开了在场判断的群：显式信号照旧，其余消息看 bot 在不在场。

    在场时进安静等待，到点只判最后一条；离场时先过概率门，掷中了才问模型。关键词照旧直接触发，
    但和模型同意接话的那些一样占主动回复的额度。
    """
    state = presence if presence is not None else PresenceState()
    present = state.is_present(inp.group_id, inp.now, config.presence_window_sec)
    status = PRESENCE_PRESENT if present else PRESENCE_ABSENT
    left = state.density_left(inp.group_id, inp.now, config)
    signal, reason, _ = _first_signal(inp, replace(config, random=False))
    source = trigger_source(inp, signal or SIGNAL_LLM_INTENT)

    def decided(decision: TriggerDecision) -> TriggerDecision:
        return replace(decision, source=source, presence=status, density_left=left)

    if signal:
        if signal == SIGNAL_KEYWORD and source not in EXPLICIT_SIGNALS and left == 0:
            return decided(TriggerDecision(
                triggered=False, signal=signal, blocked_by=BLOCKED_BY_DENSITY,
                reason=f"{reason}，但{_density_reason(config)}",
            ))
        return decided(_apply_cooldown(inp, config, cooldown, signal, reason, False))

    if source in EXPLICIT_SIGNALS:
        # 叫到了 bot 但对应信号关着：不等安静也不过概率门，照旧马上问模型。
        blocked = _cooldown_block(
            inp, config, cooldown, SIGNAL_LLM_INTENT, "被直接叫到，本来要交给模型判断",
            grace_sec=config.llm_intent_timeout_sec,
        )
        if blocked is not None:
            return decided(blocked)
        return decided(TriggerDecision(
            triggered=False, signal=SIGNAL_LLM_INTENT, undetermined=True,
            reason="被直接叫到但对应信号没开，交给模型判断要不要接话",
        ))

    if left == 0:
        return decided(TriggerDecision(
            triggered=False, signal=SIGNAL_LLM_INTENT, blocked_by=BLOCKED_BY_DENSITY,
            reason=f"{_density_reason(config)}，这条只记进群历史",
        ))

    if present:
        blocked = _cooldown_block(
            inp, config, cooldown, SIGNAL_LLM_INTENT, "在场，本来要等群里安静下来再判断",
            grace_sec=config.quiet_wait_sec + config.llm_intent_timeout_sec,
        )
        if blocked is not None:
            return decided(blocked)
        return decided(TriggerDecision(
            triggered=False, signal=SIGNAL_LLM_INTENT, undetermined=True, quiet=QUIET_WAITING,
            reason=(
                f"在场：等群里安静 {config.quiet_wait_sec:g}s（最长等 {config.quiet_max_wait_sec:g}s），"
                "到点只拿最后一条问模型"
            ),
        ))

    probability = _gate_probability(config)
    blocked = _cooldown_block(
        inp, config, cooldown, SIGNAL_LLM_INTENT, "离场，本来要过概率门再判断",
        grace_sec=config.llm_intent_timeout_sec,
    )
    if blocked is not None:
        return decided(blocked)
    if probability <= 0:
        return decided(TriggerDecision(
            triggered=False, signal=SIGNAL_LLM_INTENT, blocked_by=BLOCKED_BY_GATE, gate=GATE_MISSED,
            reason="离场，概率门是 0：只记进群历史",
        ))
    if not resolve_random:
        return decided(TriggerDecision(
            triggered=False, signal=SIGNAL_LLM_INTENT, undetermined=True,
            gate=GATE_PENDING, gate_probability=probability,
            reason=f"离场：先过 {probability:.0%} 的概率门，掷中了才交给模型判断",
        ))
    if inp.roll >= probability:
        return decided(TriggerDecision(
            triggered=False, signal=SIGNAL_LLM_INTENT, blocked_by=BLOCKED_BY_GATE,
            gate=GATE_MISSED, gate_probability=probability,
            reason=f"离场：{probability:.0%} 的概率门没掷中，只记进群历史",
        ))
    return decided(TriggerDecision(
        triggered=False, signal=SIGNAL_LLM_INTENT, undetermined=True,
        gate=GATE_PASSED, gate_probability=probability,
        reason=f"离场：{probability:.0%} 的概率门掷中，交给模型判断要不要接话",
    ))


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
    blocked = _cooldown_block(inp, config, cooldown, signal, reason)
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
    presence: PresenceState | None = None,
    agreed: bool,
    reason: str = "",
) -> TriggerDecision:
    """模型答完之后的收尾判定。

    单独开一个入口而不是给 evaluate 加参数：意愿判断要一次异步往返，调用方拿到答案时
    早就离开 evaluate 了。冷却层必须在这里再走一遍 —— 漏掉就等于「开了意愿判断 =
    冷却失效」，而这条路恰恰是最容易刷屏的那条。开了在场判断的群，主动回复的额度也在这里
    按答案到手这一刻再核一次。
    """
    source = trigger_source(inp, SIGNAL_LLM_INTENT)
    if not agreed:
        return TriggerDecision(
            triggered=False, signal=SIGNAL_LLM_INTENT, source=source,
            reason=reason or "模型判断这句话不是在跟 bot 说话",
        )
    if config.presence_active and source not in EXPLICIT_SIGNALS:
        state = presence if presence is not None else PresenceState()
        left = state.density_left(inp.group_id, inp.now, config)
        if left == 0:
            return TriggerDecision(
                triggered=False, signal=SIGNAL_LLM_INTENT, source=source,
                blocked_by=BLOCKED_BY_DENSITY, density_left=0,
                reason=f"{reason or '模型判断可以接话'}，但{_density_reason(config)}",
            )
    decision = _apply_cooldown(
        inp, config, cooldown, SIGNAL_LLM_INTENT,
        reason or "模型判断这句话是在跟 bot 说话", False,
    )
    return replace(decision, source=source)


def observe(inp: TriggerInput, decision: TriggerDecision, presence: PresenceState | None) -> None:
    """每条判过的消息都过一遍：被直接叫到就算 bot 进场，不管这一轮最后回没回。"""
    if presence is not None and decision.explicit:
        presence.enter(inp.group_id, inp.now)


def accept(
    inp: TriggerInput,
    decision: TriggerDecision,
    *,
    cooldown: CooldownState | None = None,
    presence: PresenceState | None = None,
) -> float | None:
    """一条消息确定要回之后的记账，返回主动回复那一笔的时间戳，没记是 None。

    冷却照旧；正在等的安静等待作废，这一轮的回复会带上那几条；不是被叫到的回复记一次主动回复，
    没开在场判断的群也记，判断模型要知道 bot 最近插了几次话。
    """
    if cooldown is not None:
        cooldown.record(inp)
    if presence is None:
        return None
    presence.cancel_quiet(inp.group_id)
    if decision.explicit:
        return None
    return presence.record_proactive(inp.group_id, inp.now)
