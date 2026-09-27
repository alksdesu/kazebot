"""匿名别名映射、记忆主体与送给模型的文本脱敏。"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List
from nonebot.adapters.onebot.v11 import Event
from engine import memory_subjects
from platforms.builtin import QQ_NAPCAT
from ..config import CLONOTH_WORKSPACE
from adapters._shared.conversation_hash import ConversationKeys, platform_conversation_secret
from adapters._shared.memory_hints import collect_subjects as collect_memory_subjects
from ..segments import at_segment_user_ids

from . import common, identity, proactive, state

logger = logging.getLogger("nonebot.plugin.clonoth_agent")


# 条目数超过此值时提示可以开保留期回收。
_ANON_MAP_LARGE_WARN = 20000


# QQ 号至少 5 位；低于此位数的“数字”基本不可能是真实 QQ 号，
# 多半是模型凭空捏造的 at 目标（如 qq=1）或把纯数字群名片误当成的 QQ 号。
_MIN_REAL_QQ_LEN = 5


def _is_plausible_qq_id(value: str) -> bool:
    """粗略判断一个字符串是否看上去像真实 QQ 号（纯数字且不至于过短）。"""
    real = str(value or "").strip()
    return real.isdigit() and len(real) >= _MIN_REAL_QQ_LEN


def _storable_user_id(real: str) -> bool:
    """能不能进永存别名表：群匿名占位号全群共用，明显非法的短数字多半是模型编的 at 目标。"""
    if real in identity._ANONYMOUS_PLACEHOLDER_QQ_IDS:
        return False
    return not real.isdigit() or _is_plausible_qq_id(real)


def _load_anon_map() -> None:
    """以文件为准恢复别名表。脏号不载入，下次写盘顺带清出文件（避免 @UserAP 这类脏别名回流群里）。"""
    aliases = state._adapter.aliases
    loaded = aliases.load(accept_user=_storable_user_id)
    if loaded is None:
        return
    if loaded.rejected:
        common.logger.warning("dropped %d invalid short-qq entries from anon map", loaded.rejected)
    common.logger.info(
        "loaded OneBot anon map from %s (users=%d groups=%d pruned=%d)",
        aliases.path, loaded.users, loaded.groups, loaded.pruned,
    )
    if loaded.users + loaded.groups > _ANON_MAP_LARGE_WARN:
        common.logger.warning(
            "OneBot anon map has %d entries; consider setting ONEBOT_ANON_MAP_RETENTION_DAYS to enable pruning",
            loaded.users + loaded.groups,
        )


def _anonymize_user_id(user_id: Any) -> str:
    real = str(user_id or "").strip()
    aliases = state._adapter.aliases
    if not real:
        return aliases.users.unknown
    # 群匿名占位号全群共用，登记会把所有匿名成员折叠成一个人并落盘。
    if real in identity._ANONYMOUS_PLACEHOLDER_QQ_IDS:
        return identity._ANONYMOUS_UNKNOWN_ALIAS
    # 不把明显非法的短数字（如 qq=1）登记进永存匿名表，避免脏号占用稳定别名并回流到群里。
    if not _storable_user_id(real):
        return aliases.users.unknown
    return aliases.user_alias(real)


def _anonymize_group_id(group_id: Any) -> str:
    return state._adapter.aliases.group_alias(str(group_id or "").strip())


def _enrolled_subject_display_names() -> Dict[str, str]:
    """已建档者的别名 -> 显示名，用于在自由文本里认出「提到了谁」。

    profile 配的名字跨会话稳定，优先；没配就回落到名册里记下的最近显示名 ——
    群名片确实每群不同，但没有它这条召回路径在没配 profile 的部署上完全是死的。
    重名的已经在名册侧剔除，认错人比认不出更糟。
    """
    workspace = Path(CLONOTH_WORKSPACE)
    names = dict(memory_subjects.subject_display_names(workspace))
    for alias in memory_subjects.enrolled_subjects(workspace):
        real = state._adapter.aliases.user_real_of(alias)
        if not real:
            continue
        display = identity._qq_profile_display_name(real)
        if display:
            names[alias] = display
    return names


def _record_memory_subject_interaction(event: Event, direct: bool = True) -> str:
    """主动找过 bot 的人才建档；返回发起人的匿名别名（无论建不建档都返回）。

    建档意味着「以后提到他就全量加载他的档案」，所以门槛是本人主动发起（@ bot /
    触发前缀 / 私聊）。别名照常返回：他若早已建档，本轮仍该加载他的记忆。
    """
    alias = identity._event_user_alias(event)
    # 匿名身份不可核验且按天轮换，建档等于把不同人的记忆混进同一份档案。
    if direct and alias and alias != "UserUnknown" and not alias.startswith("Anon"):
        display = identity._event_display_name(event)
        # 没名片时显示名就是别名本身，别名另有匹配路径，存进去只是冗余。
        memory_subjects.record_interaction(
            Path(CLONOTH_WORKSPACE), alias,
            display_name="" if display == alias else display,
        )
    return alias


_SUBJECT_STICKY_TTL_SEC = 300.0


_SUBJECT_STICKY_MAX_KEYS = 512


def _sticky_memory_subjects(conversation_key: str, subjects: List[str]) -> List[str]:
    """把上一轮涉及的人并进本轮。

    「@张三」的下一轮通常是「他电话多少」，那句里没有任何别名，只看当前这句会整轮
    召回不到。存回去的始终是本轮实际出现的人，不含继承来的 —— 否则谁进来一次就永久驻留。
    """
    now = time.monotonic()
    prev_at, prev = state._adapter.sticky_subjects.get(conversation_key, (0.0, []))
    state._adapter.sticky_subjects[conversation_key] = (now, list(subjects))
    state._adapter.sticky_subjects.move_to_end(conversation_key)
    while len(state._adapter.sticky_subjects) > _SUBJECT_STICKY_MAX_KEYS:
        state._adapter.sticky_subjects.popitem(last=False)
    if now - prev_at > _SUBJECT_STICKY_TTL_SEC:
        return list(subjects)
    merged = list(subjects)
    merged.extend(alias for alias in prev if alias not in merged)
    return merged


def _collect_memory_subjects(
    event: Event, user_text: str, conversation_key: str, direct: bool = True,
    *, mentioned_user_ids: Iterable[str] = (),
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
        at_user_ids=[*at_segment_user_ids(message), *mentioned_user_ids],
        alias_of_user_id=_anonymize_user_id,
        display_names=_enrolled_subject_display_names(),
    )
    return _sticky_memory_subjects(conversation_key, subjects)


def _resolve_at_alias_to_real(token: str) -> str:
    """把模型写的 [at:xxx] 里的 xxx 反查为真实 QQ 号。

    模型看到的是匿名别名（UserA/UserAF）或群昵称/显示名，写出来的是 [at:UserAF] 而非真实 QQ 号。
    先按别名表精确反查，再按已知用户 profile 的显示名/群名片/别名匹配；
    解析不到返回空串，由 emoji_handler 回退为可读文本。
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
    real = state._adapter.aliases.user_real_of(raw)
    if real:
        return str(real)
    # 2) 按已知用户 profile 的显示名/别名匹配（忽略大小写与首尾空白）。
    norm = raw.casefold()
    try:
        for uid in list(state._adapter.qq_user_profiles.keys()):
            names = [identity._qq_profile_display_name(uid) or ""]
            names.extend(proactive._profile_aliases_for_user(uid))
            for name in names:
                if name and str(name).strip().casefold() == norm:
                    return str(uid)
    except Exception:
        common.logger.debug("resolve at alias by profile failed for %r", raw, exc_info=True)
    return ""


def _anonymize_text_for_ai(text: str) -> str:
    """把文本里系统已知的真实 QQ 号/群号换成稳定匿名别名后交给模型。

    只换别名表里登记过的号，不对任意数字串泛匹配：金额、验证码、订单号、日期不能被误伤成 UserX。
    所以需要匿名的号必须先在采集入口（发送者、@某人、引用消息、历史行等）调
    `_anonymize_user_id` / `_anonymize_group_id` 登记。
    """
    return state._adapter.aliases.substitute(text)


_CONVERSATION_SECRET, CONVERSATION_DIGEST_SALTED = platform_conversation_secret(
    QQ_NAPCAT, Path(CLONOTH_WORKSPACE), legacy_hint="Run deploy/migrate_qq_conversation_hash.py to salt them.",
)
CONVERSATION_KEYS = ConversationKeys(QQ_NAPCAT, _CONVERSATION_SECRET)
