"""账号作用域、会话键摘要与路由状态落盘。"""
from __future__ import annotations

import logging
import datetime as dt
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from ..config import (
    CLONOTH_WORKSPACE,
    IMAGE_CACHE_TTL_SECONDS,
    ONEBOT_STATE_FILE,
    REPLY_ATTACHMENT_CACHE_FILE,
)
from .. import bot_scope as _bot_scope
from ..conversation_hash import digest as _digest_conversation_key

from . import anonymize, common, media, state

logger = logging.getLogger("nonebot.plugin.clonoth_agent")


def _active_bot_scope() -> str:
    return state._adapter.active_bot_scope


def _purge_account_scoped_caches() -> None:
    """丢掉按真实群号缓存、实为上一个账号视角的进程内状态。

    群历史会直接进新号第一条 inbound 的上下文；成员名片、复读判定、冷却窗口也都是
    旧号看见的样子。按会话键分桶的那些换号后自然另起，不在此列。
    """
    state._adapter.group_history.clear()
    state._adapter.group_history_seq.clear()
    state._adapter.group_history_gap.clear()
    state._adapter.context_clear_barrier.clear()
    # qq_forward 的转发候选是群历史的第二份副本，只清前者等于没清。
    state._adapter.group_content_records.clear()
    state._adapter.echo_recent.clear()
    state._adapter.echo_last.clear()
    state._adapter.group_member_cache.clear()
    state._adapter.trigger_cooldown.forget_all()


def _adopt_bot_scope(bot: Any) -> None:
    """记住当前账号；换号时丢掉按旧账号算出的键缓存，让它们按新作用域重算。"""
    raw_self_id = getattr(bot, "self_id", None)
    scope = _bot_scope.normalize(raw_self_id)
    if not scope:
        _warn_unscoped_self_id(raw_self_id)
        return
    if scope == state._adapter.active_bot_scope:
        return
    previous = state._adapter.active_bot_scope
    state._adapter.active_bot_scope = scope
    _bot_scope.save_scope(Path(CLONOTH_WORKSPACE), scope)
    # real→stable 必须重算。反向表留着：旧键的在途回调还要靠它找回真实会话。
    state._adapter.stable_conversation_keys.clear()
    _purge_account_scoped_caches()
    common.logger.warning(
        "QQ account changed (%s -> %s): conversations and memory now use a separate namespace",
        previous or "<none>", scope,
    )


def _warn_unscoped_self_id(raw_self_id: Any) -> None:
    """账号 ID 不合法时不加作用域，但必须说出来：多账号隔离被关掉不能是静默的。"""
    text = str(raw_self_id if raw_self_id is not None else "").strip()
    if not text or text in state._adapter.refused_self_ids:
        return
    state._adapter.refused_self_ids.add(text)
    common.logger.warning(
        "QQ self_id %r is not a valid account id; conversations stay in scope %s",
        text[:64], state._adapter.active_bot_scope or "<none>",
    )


def _conversation_digest(conversation_key: str) -> str:
    return _digest_conversation_key(
        conversation_key, anonymize._CONVERSATION_SECRET, bot_scope=_active_bot_scope(),
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
    for real in set(state._adapter.real_conversation_keys.values()):
        try:
            keys.add(_scoped_stable_key(str(real)))
        except Exception:
            continue
    return sorted(keys)


def _stable_conversation_key(real_conversation_key: str) -> str:
    """把真实 QQ 会话键转换为可持久化的稳定哈希键，避免 Supervisor 泄漏群号/QQ号。"""
    if real_conversation_key.startswith("qq_group:"):
        prefix = "qq_group"
        anonymize._anonymize_group_id(real_conversation_key.split(":", 1)[1])
    elif real_conversation_key.startswith("qq_private:"):
        prefix = "qq_private"
        anonymize._anonymize_user_id(real_conversation_key.split(":", 1)[1])
    else:
        prefix = "qq_unknown"
    stable = f"{prefix}:{_conversation_digest(real_conversation_key)}"
    state._adapter.real_conversation_keys[stable] = real_conversation_key
    state._adapter.stable_conversation_keys[real_conversation_key] = stable
    return stable


def _real_conversation_key(conversation_key: str) -> str:
    return state._adapter.real_conversation_keys.get(conversation_key, conversation_key)


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
        common.logger.warning("failed to load OneBot route state: %s", path, exc_info=True)
        return
    if not isinstance(data, dict):
        return

    real_map = data.get("real_conversation_keys")
    if isinstance(real_map, dict):
        state._adapter.real_conversation_keys.update({str(k): str(v) for k, v in real_map.items() if str(k) and str(v)})
        state._adapter.stable_conversation_keys.update({str(v): str(k) for k, v in state._adapter.real_conversation_keys.items() if str(k) and str(v)})

    targets = data.get("session_targets")
    if isinstance(targets, dict):
        for sid, target in targets.items():
            if isinstance(target, dict):
                state._adapter.persisted_session_targets[str(sid)] = _serializable_target(target)

    common.logger.info("loaded OneBot route state from %s", path)


async def _save_route_state() -> None:
    """保存本地路由状态，保障 bot.py 重启后能把 stable key 映射回真实 QQ 目标。"""
    async with state._adapter.route_state_lock:
        path = Path(ONEBOT_STATE_FILE)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "real_conversation_keys": dict(state._adapter.real_conversation_keys),
                "session_targets": {
                    sid: _serializable_target(target)
                    for sid, target in {**state._adapter.persisted_session_targets, **state._adapter.session_targets}.items()
                    if isinstance(target, dict)
                },
            }
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            common.logger.warning("failed to save OneBot route state: %s", path, exc_info=True)


def _load_reply_attachment_cache() -> None:
    """加载独立的引用消息附件索引缓存。"""
    path = Path(REPLY_ATTACHMENT_CACHE_FILE)
    source_path = path
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            common.logger.warning("failed to load OneBot reply attachment cache: %s", path, exc_info=True)
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
        state._adapter.reply_attachment_cache[str(mid)] = {
            "conversation_key": str(item.get("conversation_key") or ""),
            "sender_id": str(item.get("sender_id") or ""),
            "created_at": created_at or now,
            "attachments": [dict(att) for att in attachments if isinstance(att, dict)],
        }
        loaded += 1
    media._trim_reply_attachment_cache()
    if loaded:
        common.logger.info("loaded %d OneBot reply attachment cache entries from %s", loaded, source_path)


async def _save_reply_attachment_cache() -> None:
    """保存引用消息附件索引到独立缓存文件。"""
    async with state._adapter.route_state_lock:
        media._trim_reply_attachment_cache()
        path = Path(REPLY_ATTACHMENT_CACHE_FILE)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "ttl_seconds": IMAGE_CACHE_TTL_SECONDS,
                "reply_attachments": dict(state._adapter.reply_attachment_cache),
            }
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            common.logger.warning("failed to save OneBot reply attachment cache: %s", path, exc_info=True)


async def _remember_route_state(
    *,
    stable_conversation_key: str,
    real_conversation_key: str,
    session_id: str = "",
    target: Optional[Dict[str, Any]] = None,
) -> None:
    if stable_conversation_key and real_conversation_key:
        state._adapter.real_conversation_keys[stable_conversation_key] = real_conversation_key
        state._adapter.stable_conversation_keys[real_conversation_key] = stable_conversation_key
    if session_id and target:
        state._adapter.persisted_session_targets[session_id] = _serializable_target(target)
    await _save_route_state()
