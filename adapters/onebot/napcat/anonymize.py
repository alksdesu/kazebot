"""匿名别名映射、记忆主体与送给模型的文本脱敏。"""
from __future__ import annotations

import logging
import asyncio
import datetime as dt
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from nonebot.adapters.onebot.v11 import Event
from engine import memory_subjects
from platforms.builtin import QQ_NAPCAT
from ..config import (
    CLONOTH_WORKSPACE,
    CONVERSATION_HASH_SECRET,
    CONVERSATION_HASH_SECRET_FILE,
    ONEBOT_STATE_FILE,
    ANON_MAP_FILE,
    ANON_MAP_RETENTION_DAYS,
)
from adapters._shared.conversation_hash import ConversationKeys, resolve_secret
from adapters._shared.memory_hints import collect_subjects as collect_memory_subjects
from ..segments import at_segment_user_ids

from . import common, identity, proactive, state

logger = logging.getLogger("nonebot.plugin.clonoth_agent")


# 每次命中都写盘会把节流窗口打满，同一小时内只记一次就够 TTL 判定用。
_ANON_MAP_TOUCH_MIN_INTERVAL = 3600.0


# 条目数超过此值时提示可以开保留期回收。
_ANON_MAP_LARGE_WARN = 20000


# 匿名映射写盘节流：新登记合并到至少间隔 5s 后写一次，不再每次立即写盘。
_ANON_MAP_SAVE_MIN_INTERVAL = max(0.0, float(os.environ.get("ONEBOT_ANON_MAP_SAVE_MIN_INTERVAL", "5.0")))


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
    state._adapter.anon_map_dirty = True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 没有运行中的事件循环（如同步测试上下文），保持 dirty，由下次显式保存处理。
        return
    # 已有等待中的 flush task 时直接复用，避免重复调度。
    if state._adapter.anon_map_save_task is not None and not state._adapter.anon_map_save_task.done():
        return
    state._adapter.anon_map_save_task = loop.create_task(_anon_map_save_throttled())


async def _anon_map_save_throttled() -> None:
    """trailing 节流：触发后固定等待一个写盘窗口再写，窗口内的多次 dirty 合并为一次。

    为何固定等待而不是“距上次写盘”计算：后者在首次/间隔已满足时会立即写，
    导致高频场景下每隔一个间隔就写一次；固定等待一个窗口能保证“窗口内只写一次”。
    """
    try:
        if _ANON_MAP_SAVE_MIN_INTERVAL > 0:
            await asyncio.sleep(_ANON_MAP_SAVE_MIN_INTERVAL)
        await _save_anon_map()
    finally:
        state._adapter.anon_map_save_task = None


def _coerce_last_seen(value: Any) -> float:
    """读入持久化的 last_seen；缺失或非法按当前时间，避免刚加载就被 TTL 判成过期。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return time.time()


def _load_anon_map() -> None:
    """以文件为准恢复 anon_users/anon_groups、反向表及“下一个编号”计数器。

    2026-07-09 新增：匿名别名映射持久化，保障跨重启同一人/群别名一致与可反解。
    加载优先从持久化的 next 计数器恢复；若旧文件无该字段，则根据已有别名推断。
    """
    path = Path(ANON_MAP_FILE)
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        common.logger.warning("failed to load OneBot anon map: %s", path, exc_info=True)
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
            if real in identity._ANONYMOUS_PLACEHOLDER_QQ_IDS or (real.isdigit() and not _is_plausible_qq_id(real)):
                skipped_dirty += 1
                continue
            state._adapter.anon_users[real] = alias
            state._adapter.anon_user_reverse[alias] = real
            state._adapter.anon_user_last_seen[real] = _coerce_last_seen(item.get("last_seen"))
            max_user_idx = max(max_user_idx, _alias_to_index("User", alias))
    if skipped_dirty:
        _mark_anon_map_dirty()
        common.logger.warning("dropped %d invalid short-qq entries from anon map", skipped_dirty)

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
            state._adapter.anon_groups[real] = alias
            state._adapter.anon_group_reverse[alias] = real
            state._adapter.anon_group_last_seen[real] = _coerce_last_seen(item.get("last_seen"))
            max_group_idx = max(max_group_idx, _alias_to_index("Group", alias))

    # 下一个编号：优先用文件里显式保存的 next；否则用“最大已用 index + 1”兼容旧文件。
    try:
        state._adapter.anon_user_next = max(int(data.get("user_next", 0) or 0), max_user_idx + 1)
    except Exception:
        state._adapter.anon_user_next = max_user_idx + 1
    try:
        state._adapter.anon_group_next = max(int(data.get("group_next", 0) or 0), max_group_idx + 1)
    except Exception:
        state._adapter.anon_group_next = max_group_idx + 1
    state._adapter.anon_user_next = max(0, state._adapter.anon_user_next)
    state._adapter.anon_group_next = max(0, state._adapter.anon_group_next)
    # 剪枝必须在 next 计数器恢复之后：先剪会让 max_*_idx 变小，旧文件恢复的 next 回退，别名被复用。
    pruned = _prune_anon_map()
    _invalidate_anon_text_pattern()
    common.logger.info(
        "loaded OneBot anon map from %s (users=%d groups=%d pruned=%d)",
        path, len(state._adapter.anon_users), len(state._adapter.anon_groups), pruned,
    )
    if len(state._adapter.anon_users) + len(state._adapter.anon_groups) > _ANON_MAP_LARGE_WARN:
        common.logger.warning(
            "OneBot anon map has %d entries; consider setting ONEBOT_ANON_MAP_RETENTION_DAYS to enable pruning",
            len(state._adapter.anon_users) + len(state._adapter.anon_groups),
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
        (state._adapter.anon_users, state._adapter.anon_user_last_seen, state._adapter.anon_user_reverse),
        (state._adapter.anon_groups, state._adapter.anon_group_last_seen, state._adapter.anon_group_reverse),
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
    """把匿名别名映射原子写入单独文件；复用 route_state_lock + tmp→replace。"""
    async with state._adapter.route_state_lock:
        if not state._adapter.anon_map_dirty:
            return
        path = Path(ANON_MAP_FILE)
        now = time.time()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "user_next": state._adapter.anon_user_next,
                "group_next": state._adapter.anon_group_next,
                "users": {
                    real: {"alias": alias, "last_seen": state._adapter.anon_user_last_seen.get(real, now)}
                    for real, alias in state._adapter.anon_users.items()
                },
                "groups": {
                    real: {"alias": alias, "last_seen": state._adapter.anon_group_last_seen.get(real, now)}
                    for real, alias in state._adapter.anon_groups.items()
                },
            }
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
            state._adapter.anon_map_dirty = False
            state._adapter.anon_map_last_saved_at = time.time()
        except Exception:
            common.logger.warning("failed to save OneBot anon map: %s", path, exc_info=True)


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
    real = str(user_id or "").strip()
    if not real:
        return "UserUnknown"
    # 群匿名占位号全群共用，登记会把所有匿名成员折叠成一个人并落盘。
    if real in identity._ANONYMOUS_PLACEHOLDER_QQ_IDS:
        return identity._ANONYMOUS_UNKNOWN_ALIAS
    # 不把明显非法的短数字（如 qq=1）登记进永存匿名表，避免脏号占用稳定别名并回流到群里。
    if real.isdigit() and not _is_plausible_qq_id(real):
        return "UserUnknown"
    alias = state._adapter.anon_users.get(real)
    if alias is None:
        alias = _alias_from_index("User", state._adapter.anon_user_next)
        state._adapter.anon_user_next += 1
        state._adapter.anon_users[real] = alias
        state._adapter.anon_user_reverse[alias] = real
        _invalidate_anon_text_pattern()
        _mark_anon_map_dirty()
    _touch_anon_seen(state._adapter.anon_user_last_seen, real)
    return alias


def _anonymize_group_id(group_id: Any) -> str:
    real = str(group_id or "").strip()
    if not real:
        return "GroupUnknown"
    alias = state._adapter.anon_groups.get(real)
    if alias is None:
        alias = _alias_from_index("Group", state._adapter.anon_group_next)
        state._adapter.anon_group_next += 1
        state._adapter.anon_groups[real] = alias
        state._adapter.anon_group_reverse[alias] = real
        _invalidate_anon_text_pattern()
        _mark_anon_map_dirty()
    _touch_anon_seen(state._adapter.anon_group_last_seen, real)
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
        real = state._adapter.anon_user_reverse.get(alias)
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

    2026-07-14 新增：模型看到的是匿名别名（UserA/UserAF）或群昵称/显示名，
    会输出 [at:UserAF] 而非真实 QQ 号。emoji_handler 在遇到非数字 token 时
    回调本函数：
      1. 先按匿名反向表 anon_user_reverse 精确匹配别名 -> 真实 QQ 号；
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
    real = state._adapter.anon_user_reverse.get(raw)
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


def _invalidate_anon_text_pattern() -> None:
    state._adapter.anon_text_pattern = None


def _anon_text_pattern_and_map() -> tuple[Optional["re.Pattern[str]"], Dict[str, str]]:
    """把全部已知真实号编成一条备选正则并缓存。

    逐条 re.sub 在映射表超过 re 模块 512 条编译缓存后每次调用都要重新编译，
    而本函数在每条入站消息上会被调用多次。
    """
    if state._adapter.anon_text_pattern is not None:
        return state._adapter.anon_text_pattern, state._adapter.anon_text_replacements
    mapping: Dict[str, str] = dict(state._adapter.anon_users)
    # 群号与 QQ 号字面相同时按群号替换，与旧的"先群后人"两趟顺序保持一致。
    mapping.update(state._adapter.anon_groups)
    if not mapping:
        return None, {}
    keys = sorted(mapping, key=len, reverse=True)
    state._adapter.anon_text_replacements = mapping
    state._adapter.anon_text_pattern = re.compile(
        rf"(?<!\d)(?:{'|'.join(re.escape(key) for key in keys)})(?!\d)"
    )
    return state._adapter.anon_text_pattern, state._adapter.anon_text_replacements


def _anonymize_text_for_ai(text: str) -> str:
    """把文本中"系统已知的真实 QQ 号/群号"替换为稳定匿名别名后交给模型。

    2026-07-09 修改原因：原实现用 `_SENSITIVE_ID_RE`（5~12 位数字）兜底匿名化，
    会把金额、验证码、订单号、日期等普通数字误伤成 UserX，导致模型收到失真内容。
    做法改为"已知 ID 精确替换"：只替换 `anon_groups` / `anon_users` 映射表里
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
    legacy_hint="Run deploy/migrate_qq_conversation_hash.py to salt them.",
)
CONVERSATION_KEYS = ConversationKeys(QQ_NAPCAT, _CONVERSATION_SECRET)
