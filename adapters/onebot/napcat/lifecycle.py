"""热载对齐、live state 发布、启动与关闭。"""
from __future__ import annotations

import logging
import asyncio
import contextlib
import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict
from nonebot import get_bot
from nonebot.adapters.onebot.v11 import Bot
from ..config import (
    BQBS_PATH,
    CLONOTH_BASE_URL,
    CLONOTH_WORKSPACE,
    CONVERSATION_HASH_SECRET,
    FORWARD_BRIDGE_HOST,
    FORWARD_BRIDGE_PORT,
    ENTRY_NODE_ID,
    USER_PROFILES_PATH,
)
from ..live_config import RECONCILED_KEYS, live, state_payload as live_config_state_payload
from ..emoji_handler import load_bqbs
from clonoth_sdk import BotConfig, ClonothClient, EventRouter, SessionState

from . import anonymize, callbacks, common, emotes, forward_bridge, identity, inbound, media, models, proactive, queueing, routing, state

logger = logging.getLogger("nonebot.plugin.clonoth_agent")


# 热载键落到运行期对象上的对齐周期。停等一次 stat 的代价换「改完两秒内生效」。
_LIVE_RECONCILE_INTERVAL_SEC = 2.0


# 快照没变也定期重写一次，supervisor 靠这个时间戳判断 bot 进程是不是还活着。
_LIVE_STATE_HEARTBEAT_SEC = 30.0


def _reconcile_group_history_capacity() -> bool:
    """群历史条数改了就按新上限重建每个群的 deque。

    maxlen 不可变，只能重建。序号不受影响，但调小容量丢的最旧几行里可能有还没送到
    engine 的，那几条要记进缺口，否则 prompt 里悄悄少一段。
    """
    records_target = state._adapter.group_content_capacity()
    changed = state._adapter.group_history.resize()
    for group_id, records in list(state._adapter.group_content_records.items()):
        if records.maxlen != records_target:
            state._adapter.group_content_records[group_id] = deque(records, maxlen=records_target)
            changed = True
    return changed


async def _reconcile_queue_workers() -> bool:
    """把 worker task 的数量对齐到配置。

    扩容立刻建 task；缩容只叫醒它们，让每个 worker 在自己的安全点退出 —— 直接 cancel
    会打断正在提交的那一条消息。
    """
    for index, task in list(state._adapter.qq_queue_tasks.items()):
        if task.done():
            state._adapter.qq_queue_tasks.pop(index, None)
    target = live.queue_workers if live.enable_queue else 0
    running = len(state._adapter.qq_queue_tasks)
    if running == target:
        return False
    if running < target:
        for index in range(target):
            if index not in state._adapter.qq_queue_tasks:
                state._adapter.qq_queue_tasks[index] = asyncio.create_task(queueing._qq_queue_worker_forever(index))
    else:
        await state._adapter.qq_queue.wake_all()
    common.logger.info(
        "QQ queue reconciled: workers %d -> %d (enabled=%s pending=%d)",
        running, target, live.enable_queue, len(state._adapter.qq_queue),
    )
    return True


async def _reconcile_forward_bridge() -> bool:
    """按开关起停本地转发 Bridge。监听地址仍在启动时绑定，改地址要重启。"""
    if live.enable_forward_bridge and not state._adapter.forward_bridge_started:
        await forward_bridge._start_forward_bridge()
        return state._adapter.forward_bridge_started
    if not live.enable_forward_bridge and state._adapter.forward_bridge_started:
        await forward_bridge._stop_forward_bridge()
        common.logger.info("qq_forward bridge server stopped: disabled in qq.yaml")
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
        "queue_workers_running": len([t for t in state._adapter.qq_queue_tasks.values() if not t.done()]),
        "forward_bridge_running": bool(state._adapter.forward_bridge_started),
        "forward_bridge_endpoint": f"http://{FORWARD_BRIDGE_HOST}:{FORWARD_BRIDGE_PORT}/qq_forward",
        "forward_bridge_token_set": bool(forward_bridge._forward_bridge_token()),
        "group_history_capacities": state._adapter.group_history.capacities(),
        # 改一次就要重启的那几个键，控制台只读展示。密钥一律只报「设没设」——
        # 回传值等于把它摊在任何能打开控制台的人面前。
        "environment": {
            "supervisor_url": CLONOTH_BASE_URL,
            "workspace": str(CLONOTH_WORKSPACE),
            "hash_secret_set": bool(CONVERSATION_HASH_SECRET),
            "conversation_digest_salted": anonymize.CONVERSATION_DIGEST_SALTED,
        },
        # 迁移功能靠它认「当前号」。空串 = 还没连上过任何账号，此时全部会话键都没作用域。
        "bot_scope": routing._active_bot_scope(),
        "volatile": {
            "queue_pending": len(state._adapter.qq_queue),
            "cached_groups": len(state._adapter.group_history),
            "history_gap_groups": len(state._adapter.group_history.gapped_channels()),
        },
    }


# 群名极少变，而 reconcile 每 2 秒转一圈，跟着它调 API 纯属浪费。
_GROUP_NAME_REFRESH_SEC = 300.0


def _seed_group_names() -> None:
    """把上次记下的群名读回来。

    这份字典是「已知群名」而不是「当前登录号的群」：get_group_list 只给得出当前
    号看得见的群，进程重启或换号后直接整体覆盖，历史会话的群名就没了，控制台上
    只剩 GroupA 这种脱敏别名。已经见过的群名是既成事实，退群或换号都不该抹掉。
    """
    if state._adapter.group_names_seeded:
        return
    state._adapter.group_names_seeded = True
    try:
        known = json.loads(_live_state_file().read_text(encoding="utf-8")).get("group_names")
    except Exception:
        return
    if not isinstance(known, dict):
        return
    for gid, name in known.items():
        text = str(name or "").strip()
        # 内存里已有的更新，不要被磁盘上的旧值盖回去。
        if text and str(gid) not in state._adapter.group_names:
            state._adapter.group_names[str(gid)] = text


async def _refresh_group_names() -> None:
    """把群号→群名缓存下来发给控制台。

    只有 bot 进程连着 OneBot，supervisor 自己拿不到群名；不给的话控制台上
    只能显示 GroupA 这种脱敏别名，管理员分不清是哪个群。
    """
    now = time.time()
    if now - state._adapter.group_names_at < _GROUP_NAME_REFRESH_SEC:
        return
    bot = queueing._get_fallback_bot()
    if bot is None:
        return
    state._adapter.group_names_at = now
    for row in await proactive._safe_call_onebot_list(bot, "get_group_list"):
        try:
            gid = int(row.get("group_id"))
        except Exception:
            continue
        name = str(row.get("group_name") or row.get("group_remark") or "").strip()
        if name:
            state._adapter.group_names[str(gid)] = identity._sanitize_name(name, max_len=40)


def _publish_live_state(*, force: bool = False) -> None:
    """把「bot 进程实际生效的配置」写给 supervisor 的 GET /qq/state 读。

    不走转发 Bridge：Bridge 自己的开关就是一个热载键，关掉之后状态就瞎了，而
    「界面显示已生效、进程其实没读到」正是热重载最需要防的那种假象。
    """
    payload: Dict[str, Any] = live_config_state_payload()
    payload["runtime"] = _live_runtime_facts()
    _seed_group_names()
    payload["group_names"] = dict(state._adapter.group_names)
    payload["scope_conversation_keys"] = routing._current_scope_keys()
    signature = json.dumps(
        {k: v for k, v in payload.items() if k != "runtime"}
        | {k: v for k, v in payload["runtime"].items() if k != "volatile"},
        ensure_ascii=False, sort_keys=True,
    )
    now = time.time()
    if not force and signature == state._adapter.live_state_signature and now - state._adapter.live_state_published_at < _LIVE_STATE_HEARTBEAT_SEC:
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
        common.logger.warning("failed to publish QQ live state to %s", path, exc_info=True)
        return
    state._adapter.live_state_signature = signature
    state._adapter.live_state_published_at = now


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
                    common.logger.info(
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
            common.logger.warning("QQ live config reconcile failed", exc_info=True)
        await asyncio.sleep(_LIVE_RECONCILE_INTERVAL_SEC)


async def _attachment_cleanup_forever() -> None:
    """定期清理 QQ 附件目录。原来只在有图片/文件下载时才顺手清一次，长期不收图就永远不清。"""
    while True:
        try:
            media._cleanup_old_qq_attachments()
        except asyncio.CancelledError:
            raise
        except Exception:
            common.logger.warning("QQ attachment cleanup failed", exc_info=True)
        await asyncio.sleep(models._ATTACHMENT_CLEANUP_INTERVAL_SEC)


@common.driver.on_startup
async def _startup() -> None:
    """NoneBot 启动时初始化 Clonoth SDK 与事件路由。"""
    if state._adapter.router_task is not None and not state._adapter.router_task.done():
        return

    inbound._warn_startup_config()
    inbound._warn_trigger_config()

    state._adapter.bqbs = load_bqbs(BQBS_PATH) if BQBS_PATH else []
    state._adapter.custom_face_names = emotes._load_custom_face_names_file()
    state._adapter.custom_face_metadata = emotes._load_custom_face_metadata_file()
    state._adapter.qq_user_profiles = identity._load_qq_user_profiles(USER_PROFILES_PATH)
    if state._adapter.qq_user_profiles:
        common.logger.info("loaded %d QQ user profiles", len(state._adapter.qq_user_profiles))
    routing._load_route_state()
    routing._load_reply_attachment_cache()
    emotes._start_sticker_tagger()
    anonymize._load_anon_map()
    await emotes._reconcile_sticker_receipts()
    state._adapter.sticker_receipt_task = asyncio.create_task(emotes._sticker_receipts_forever())
    # [2026-07-14] 注入 at 别名反查，让 emoji_handler 在处理 [at:UserAF]/[at:显示名]
    # 时能把匿名别名/群昵称回解为真实 QQ 号，避免直接把代号当纯文本 @ 出去。
    state._adapter.emoji.bind(
        at_alias_resolver=anonymize._resolve_at_alias_to_real,
        sticker_resolver=emotes._resolve_sticker,
    )
    # [AutoC] QQ 管理员 /切换模型 命令需要调用受 admin_token 保护的
    # POST /v1/config/openai，因此这里把 Supervisor 写出的 data/.admin_token
    # 传给 ClonothClient（每次请求实时读取，token 轮换也能跟上）。
    state._adapter.client = ClonothClient(
        CLONOTH_BASE_URL,
        admin_token=os.environ.get("CLONOTH_ADMIN_TOKEN", "").strip(),
        admin_token_path=str(Path(CLONOTH_WORKSPACE) / "data" / ".admin_token"),
    )
    state._adapter.session_state = SessionState()
    for sid, target in list(state._adapter.persisted_session_targets.items()):
        conv_key = str(target.get("conversation_key") or "")
        if conv_key:
            state._adapter.session_state.register_session(conv_key, sid)
    state._adapter.callbacks = callbacks.TangQiuCallbacks()

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
    state._adapter.event_router = EventRouter(
        state._adapter.client,
        state._adapter.session_state,
        state._adapter.callbacks,
        bot_config,
        entry_node_id=ENTRY_NODE_ID,
        poll_interval=1.0,
    )
    state._adapter.router_task = asyncio.create_task(state._adapter.event_router.run())
    if live.enable_queue:
        common.logger.info(
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
    state._adapter.live_reconcile_task = asyncio.create_task(_live_config_reconcile_forever())
    state._adapter.attachment_cleanup_task = asyncio.create_task(_attachment_cleanup_forever())
    state._adapter.features.poll_task = asyncio.create_task(state._adapter.features.poll())
    common.logger.info("Clonoth Agent QQ adapter started: %s", CLONOTH_BASE_URL)


@common.driver.on_bot_connect
async def _on_bot_connect(bot: Bot) -> None:
    """连上就认账号。等第一条消息再认的话，那条消息本身还是按上一个号算键。"""
    routing._adopt_bot_scope(bot)
    _publish_live_state(force=True)


@common.driver.on_shutdown
async def _shutdown() -> None:
    """NoneBot 关闭时停止事件路由并释放 HTTP 连接。"""
    if state._adapter.features.poll_task is not None:
        state._adapter.features.poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await state._adapter.features.poll_task
        state._adapter.features.poll_task = None
    if state._adapter.event_router is not None:
        state._adapter.event_router.stop()
    if state._adapter.router_task is not None:
        state._adapter.router_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await state._adapter.router_task
        state._adapter.router_task = None
    if state._adapter.live_reconcile_task is not None:
        state._adapter.live_reconcile_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await state._adapter.live_reconcile_task
        state._adapter.live_reconcile_task = None
    if state._adapter.attachment_cleanup_task is not None:
        state._adapter.attachment_cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await state._adapter.attachment_cleanup_task
        state._adapter.attachment_cleanup_task = None
    if state._adapter.sticker_receipt_task is not None:
        state._adapter.sticker_receipt_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await state._adapter.sticker_receipt_task
        state._adapter.sticker_receipt_task = None
    if state._adapter.qq_queue_tasks:
        for task in state._adapter.qq_queue_tasks.values():
            task.cancel()
        for task in state._adapter.qq_queue_tasks.values():
            with contextlib.suppress(asyncio.CancelledError):
                await task
        state._adapter.qq_queue_tasks.clear()
    await emotes._reconcile_sticker_receipts()
    await emotes._stop_stickers()
    if state._adapter.client is not None:
        await state._adapter.client.close()
        state._adapter.client = None
    state._adapter.event_router = None
    state._adapter.callbacks = None
    await forward_bridge._stop_forward_bridge()
    with contextlib.suppress(Exception):
        await state._adapter.aliases.flush()
    common.logger.info("Clonoth Agent QQ adapter stopped")
