"""掉线分类、登录动作预算与自动重登状态机。

NapCat 只把踢线文案抛出来，kazebot 以前既不接 bot_offline 也不看心跳，掉了只能等人发现。
这里把三类信号（踢线通知、WS 断开、心跳超时）归成几种原因，能自己登回去的在预算内登，
登不回去的进扫码态并把处境写进 live state 和事件文件，控制台和事后排查都看得到。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Mapping

KIND_KICK = "kick"
KIND_DISCONNECT = "disconnect"
KIND_HEARTBEAT = "heartbeat_timeout"
KIND_SSO_ERROR = "sso_error"
KIND_TICKET_REFRESH = "ticket_refresh"

# 服务端注销了这台设备的登录态：票据失效、安全踢、要求重新验证。本地快速登录凭据多半还能用一次。
REASON_SESSION_INVALID = "session_invalid"
# 人在别的设备上把 PC 踢了。登回去会再被踢，只记不动。
REASON_OTHER_DEVICE = "other_device"
REASON_PASSWORD_CHANGED = "password_changed"
REASON_LOW_VERSION = "low_version"
# 只是链路断了，QQ 核心未必离线。先等 NapCat 自己重连，再问 WebUI。
REASON_LINK_DOWN = "link_down"
REASON_UNKNOWN = "unknown"

AUTO_RELOGIN_REASONS: frozenset[str] = frozenset({REASON_SESSION_INVALID, REASON_UNKNOWN})

STATE_IDLE = "idle"
STATE_WAITING = "waiting"
STATE_RELOGIN = "relogin"
STATE_LOGIN_MODE = "login_mode"
STATE_EXHAUSTED = "exhausted"
STATE_MANUAL = "manual"

EVENTS_FILE_NAME = "qq_offline_events.jsonl"
BUDGET_WINDOW_SEC = 86400.0
# 链路断开后先给 NapCat 自己重连的时间，再去问登录态。
LINK_DOWN_GRACE_SEC = 60.0
# OneBot 心跳默认 30s 一次；三拍没来按断连算，和 live state 的失联判定同口径。
HEARTBEAT_MISSES = 3
DEFAULT_HEARTBEAT_INTERVAL_SEC = 30.0
HEARTBEAT_CHECK_SEC = 5.0
# SsoHeartBeat 每 6 分钟一次，容器重启到核心可用十几秒；等登录态确认留足余量。
RELOGIN_CONFIRM_TIMEOUT_SEC = 90.0
RELOGIN_CONFIRM_POLL_SEC = 2.0
# 预算记录不限长，但读回来只认窗口内的；文件本身由数据清理按保留期处理。
EVENTS_READ_TAIL_BYTES = 256 * 1024

_OTHER_DEVICE_WORDS = ("其他设备", "其它设备", "另一", "手机", "移动设备", "别处", "在其他")
_PASSWORD_WORDS = ("密码",)
_LOW_VERSION_WORDS = ("版本过低", "版本太低", "升级", "低版本")
_SESSION_INVALID_WORDS = ("失效", "安全", "异常", "验证", "冻结", "保护", "重新登录", "风险", "过期")

# 两份文案字段 NapCat 两版都有；带类型的字段只有补过事件面的 NapCat 才给，缺了就按未知处理。
_NOTICE_TITLE_KEYS = ("tag", "tips_title", "tipsTitle", "title")
_NOTICE_MESSAGE_KEYS = ("message", "tips_desc", "tipsDesc", "desc")


@dataclass(frozen=True)
class OfflineEvent:
    """一次掉线信号，分类之后的样子。"""

    kind: str
    reason: str
    title: str = ""
    message: str = ""
    kicked_type: int | None = None
    same_device: bool | None = None
    at: float = 0.0
    fields: Mapping[str, Any] = field(default_factory=dict)

    @property
    def auto_relogin(self) -> bool:
        return self.reason in AUTO_RELOGIN_REASONS

    def summary(self) -> str:
        head = f"{self.kind}/{self.reason}"
        text = "；".join(part for part in (self.title, self.message) if part)
        return f"{head}：{text}" if text else head

    def record(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "reason": self.reason, "title": self.title, "message": self.message,
            "kicked_type": self.kicked_type, "same_device": self.same_device,
            "fields": {k: v for k, v in dict(self.fields).items() if isinstance(v, (str, int, float, bool)) or v is None},
        }


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _first_text(fields: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        text = _text(fields.get(key))
        if text:
            return text
    return ""


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes"}:
            return True
        if lowered in {"0", "false", "no"}:
            return False
    return None


def reason_from_text(title: str, message: str) -> str:
    """按踢线文案归类。QQ 的 kickedType 枚举值还没解出来，文案是两版 NapCat 都有的唯一依据。"""
    text = f"{title} {message}"
    if any(word in text for word in _PASSWORD_WORDS):
        return REASON_PASSWORD_CHANGED
    if any(word in text for word in _LOW_VERSION_WORDS):
        return REASON_LOW_VERSION
    if any(word in text for word in _OTHER_DEVICE_WORDS):
        return REASON_OTHER_DEVICE
    if any(word in text for word in _SESSION_INVALID_WORDS):
        return REASON_SESSION_INVALID
    return REASON_UNKNOWN


def classify_kick(fields: Mapping[str, Any], *, at: float) -> OfflineEvent:
    """bot_offline 通知（或 core 层的 KickedOffLine 信息）→ 分类后的事件。"""
    title = _first_text(fields, _NOTICE_TITLE_KEYS)
    message = _first_text(fields, _NOTICE_MESSAGE_KEYS)
    same_device = _bool_or_none(fields.get("same_device", fields.get("sameDevice")))
    kicked_type = _int_or_none(fields.get("kicked_type", fields.get("kickedType")))
    reason = reason_from_text(title, message)
    # 同一台设备上的另一个实例把这边顶掉，和手机踢 PC 一样是人为的，不该自动顶回去。
    if reason == REASON_UNKNOWN and same_device is True:
        reason = REASON_OTHER_DEVICE
    return OfflineEvent(
        kind=KIND_KICK, reason=reason, title=title, message=message,
        kicked_type=kicked_type, same_device=same_device, at=at, fields=dict(fields),
    )


def classify_link_down(kind: str, *, at: float, detail: str = "") -> OfflineEvent:
    return OfflineEvent(kind=kind, reason=REASON_LINK_DOWN, message=detail, at=at)


class OfflineEventLog:
    """append-only 的事件文件。动作记录同时是预算的依据，进程重启后预算不从零开始。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def append(self, record: Mapping[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(dict(record), ensure_ascii=False) + "\n")
        except OSError:
            # 记不上不该挡住恢复本身；预算宁可偏松也不能让 bot 一直离线。
            pass

    def recent(self, *, since: float) -> list[dict[str, Any]]:
        try:
            size = self.path.stat().st_size
            with open(self.path, "rb") as handle:
                if size > EVENTS_READ_TAIL_BYTES:
                    handle.seek(size - EVENTS_READ_TAIL_BYTES)
                    handle.readline()
                lines = handle.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return []
        found: list[dict[str, Any]] = []
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and isinstance(item.get("at"), (int, float)) and item["at"] >= since:
                found.append(item)
        return found


class ActionBudget:
    """滚动 24 小时内允许的登录动作数与最小间隔。超了只记录、不动手。"""

    def __init__(self, log: OfflineEventLog, *, now: float) -> None:
        self._stamps: Deque[float] = deque(
            sorted(float(item["at"]) for item in log.recent(since=now - BUDGET_WINDOW_SEC) if item.get("type") == "action")
        )

    def _trim(self, now: float) -> None:
        while self._stamps and self._stamps[0] <= now - BUDGET_WINDOW_SEC:
            self._stamps.popleft()

    def used(self, now: float) -> int:
        self._trim(now)
        return len(self._stamps)

    def refusal(self, now: float, *, limit: int, min_interval_sec: float) -> str:
        """为什么现在不能动；空串 = 可以。"""
        self._trim(now)
        if limit <= 0:
            return "自动恢复的日预算为 0"
        if len(self._stamps) >= limit:
            return f"24 小时内已自动登录 {len(self._stamps)} 次，到预算上限 {limit}"
        if self._stamps and now - self._stamps[-1] < min_interval_sec:
            left = int(min_interval_sec - (now - self._stamps[-1]))
            return f"距上次自动登录不足 {int(min_interval_sec)} 秒（还要等 {left} 秒）"
        return ""

    def spend(self, now: float) -> None:
        self._stamps.append(now)


@dataclass
class LinkState:
    """链路与恢复的当前处境，整份写进 live state。"""

    online: bool = False
    offline_reason: str = ""
    offline_kind: str = ""
    offline_detail: str = ""
    last_offline_at: float = 0.0
    last_online_at: float = 0.0
    last_heartbeat_at: float = 0.0
    heartbeat_interval_sec: float = DEFAULT_HEARTBEAT_INTERVAL_SEC
    recovery_state: str = STATE_IDLE
    recovery_note: str = ""
    last_action: str = ""
    last_action_at: float = 0.0
    actions_used: int = 0
    pending_notice: str = ""

    def facts(self) -> dict[str, Any]:
        return {
            "online": self.online,
            "offline_reason": self.offline_reason,
            "offline_kind": self.offline_kind,
            "offline_detail": self.offline_detail,
            "last_offline_at": self.last_offline_at,
            "last_online_at": self.last_online_at,
            "recovery": {
                "state": self.recovery_state,
                "note": self.recovery_note,
                "last_action": self.last_action,
                "last_action_at": self.last_action_at,
                "actions_used_24h": self.actions_used,
            },
        }


class RecoverySettings:
    """热载配置里 recovery 那几个键的只读视图，每次取都是当前值。"""

    def __init__(self, live: Any) -> None:
        self._live = live

    @property
    def enabled(self) -> bool:
        return bool(self._live.recovery_enabled)

    @property
    def daily_limit(self) -> int:
        return int(self._live.recovery_daily_limit)

    @property
    def min_interval_sec(self) -> float:
        return float(self._live.recovery_min_interval_sec)

    @property
    def login_mode_on_failure(self) -> bool:
        return bool(self._live.recovery_login_mode_on_failure)

    @property
    def notify_admins(self) -> bool:
        return bool(self._live.recovery_notify_admins)


class AccountGateway:
    """恢复用到的那几个账号动作。真实现是 account_ops.NapCatAccountOps，测试换成假的。"""

    async def login_state(self) -> dict[str, Any]:
        raise NotImplementedError

    async def quick_relogin(self, uin: str) -> dict[str, Any]:
        raise NotImplementedError

    async def enter_login_mode(self) -> None:
        raise NotImplementedError


Notifier = Callable[[str], Awaitable[bool]]


class Recovery:
    """掉线 → 分类 → 预算内自动恢复 → 记录与通知。所有入口都在事件循环里，不加锁。"""

    def __init__(
        self,
        *,
        workspace: Path,
        settings: RecoverySettings,
        gateway: AccountGateway,
        notifier: Notifier,
        current_account: Callable[[], str],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.state = LinkState()
        self.settings = settings
        self._gateway = gateway
        self._notify = notifier
        self._current_account = current_account
        self._clock = clock
        self.log = OfflineEventLog(Path(workspace) / "data" / EVENTS_FILE_NAME)
        self.budget = ActionBudget(self.log, now=clock())
        self._handling: asyncio.Task | None = None
        self._closed = False
        self._offline_logged = False

    # ---- 入口 ----

    def on_connect(self) -> None:
        now = self._clock()
        was_offline = not self.state.online and self._offline_logged
        self._offline_logged = False
        self.state.online = True
        self.state.last_online_at = now
        self.state.last_heartbeat_at = now
        if self.state.recovery_state in {STATE_RELOGIN, STATE_WAITING, STATE_LOGIN_MODE}:
            self.state.recovery_state = STATE_IDLE
            self.state.recovery_note = "已恢复在线"
        if was_offline:
            self.log.append({"at": now, "type": "recovered", "reason": self.state.offline_reason})

    def on_heartbeat(self, *, online: bool | None, interval_ms: int | None) -> None:
        if self._closed:
            return
        now = self._clock()
        self.state.last_heartbeat_at = now
        if interval_ms and interval_ms > 0:
            self.state.heartbeat_interval_sec = interval_ms / 1000.0
        if online is True and not self.state.online:
            self.on_connect()
        elif online is False and self.state.online:
            self.schedule(classify_link_down(KIND_HEARTBEAT, at=now, detail="心跳报告离线"))

    def on_disconnect(self) -> None:
        if self._closed or not self.state.online:
            return
        self.schedule(classify_link_down(KIND_DISCONNECT, at=self._clock(), detail="OneBot 连接断开"))

    def on_notice(self, fields: Mapping[str, Any]) -> OfflineEvent:
        event = classify_kick(fields, at=self._clock())
        self.schedule(event)
        return event

    def schedule(self, event: OfflineEvent) -> None:
        """同一时刻只处理一条掉线：踢线通知和紧随的 WS 断开说的是同一件事。"""
        if self._closed:
            return
        if self._handling is not None and not self._handling.done():
            if event.kind == KIND_KICK and self.state.offline_kind != KIND_KICK:
                # 先到的是链路断开、后到的才是真正原因：换成有原因的那条，正在跑的任务停掉。
                self._handling.cancel()
            else:
                return
        self._mark_offline(event)
        self._handling = asyncio.get_running_loop().create_task(self._handle(event))

    def _mark_offline(self, event: OfflineEvent) -> None:
        self.state.online = False
        self.state.offline_reason = event.reason
        self.state.offline_kind = event.kind
        self.state.offline_detail = event.summary()
        self.state.last_offline_at = event.at
        # 链路断开要等宽限期过了还没回来才算掉线：进程重启、docker restart 都会断几秒，不该留记录。
        if event.reason != REASON_LINK_DOWN:
            self._log_offline(event)

    def _log_offline(self, event: OfflineEvent) -> None:
        if self._offline_logged:
            return
        self._offline_logged = True
        self.log.append({"at": event.at, "type": "offline", **event.record()})

    # ---- 状态机 ----

    async def _handle(self, event: OfflineEvent) -> None:
        if self.state.online and event.kind != KIND_KICK:
            # 任务排上来之前链路已经自己回来了：这条断开信号过时了。
            return
        try:
            if event.reason == REASON_LINK_DOWN:
                await self._handle_link_down(event)
            elif event.auto_relogin:
                await self._try_recover(event)
            else:
                self._settle(STATE_MANUAL, f"{event.summary()}，不自动重登", event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 恢复本身出错不能让 bot 进程倒下
            self._settle(STATE_MANUAL, f"恢复流程出错：{exc}", event)

    async def _handle_link_down(self, event: OfflineEvent) -> None:
        self.state.recovery_state = STATE_WAITING
        self.state.recovery_note = f"等待 NapCat 自行重连（{int(LINK_DOWN_GRACE_SEC)} 秒）"
        await asyncio.sleep(LINK_DOWN_GRACE_SEC)
        if self.state.online:
            self.state.recovery_state = STATE_IDLE
            self.state.recovery_note = "链路已自行恢复"
            return
        self._log_offline(event)
        login = await self._login_state()
        if login is None:
            self._settle(STATE_WAITING, "NapCat WebUI 没有应答，继续等链路", event)
            return
        if login.get("is_login") and not login.get("is_offline"):
            self._settle(STATE_WAITING, "QQ 核心仍在线，只是 OneBot 链路未恢复", event)
            return
        await self._try_recover(OfflineEvent(
            kind=event.kind, reason=REASON_SESSION_INVALID, title="链路断开后登录态已不在",
            message=_text(login.get("login_error")), at=self._clock(),
        ))

    async def _try_recover(self, event: OfflineEvent) -> None:
        if not self.settings.enabled:
            self._settle(STATE_MANUAL, "自动恢复已关闭", event)
            return
        uin = self._current_account()
        if not uin:
            self._settle(STATE_MANUAL, "不知道当前账号，无法快速登录", event)
            return
        now = self._clock()
        refusal = self.budget.refusal(now, limit=self.settings.daily_limit, min_interval_sec=self.settings.min_interval_sec)
        if refusal:
            self._settle(STATE_EXHAUSTED, refusal, event)
            return
        login = await self._login_state()
        if login is not None and login.get("is_login") and not login.get("is_offline"):
            self._settle(STATE_IDLE, "查询时登录态仍在，无需动作", event)
            return
        self._spend("quick_relogin", uin)
        self.state.recovery_state = STATE_RELOGIN
        self.state.recovery_note = f"正在快速登录 {uin}"
        result = await self._gateway.quick_relogin(uin)
        if result.get("ok"):
            self._settle(STATE_IDLE, f"快速登录成功：{uin}", event)
            await self._say(f"QQ 刚掉线（{event.summary()}），已自动快速登录回 {uin}。")
            return
        detail = _text(result.get("message")) or "快速登录失败"
        if result.get("needs_verify"):
            detail = f"{detail}（QQ 要求验证设备，需要人工处理）"
        if not self.settings.login_mode_on_failure:
            self._settle(STATE_MANUAL, f"{detail}；按配置不进扫码态", event)
            return
        refusal = self.budget.refusal(self._clock(), limit=self.settings.daily_limit, min_interval_sec=0)
        if refusal:
            self._settle(STATE_EXHAUSTED, f"{detail}；{refusal}", event)
            return
        self._spend("enter_login_mode", uin)
        self.state.recovery_state = STATE_LOGIN_MODE
        self.state.recovery_note = f"{detail}；正在重启进扫码态"
        await self._gateway.enter_login_mode()
        self._settle(STATE_LOGIN_MODE, f"{detail}；已进入扫码态，等人扫码", event)

    # ---- 辅助 ----

    async def _login_state(self) -> dict[str, Any] | None:
        try:
            return await self._gateway.login_state()
        except Exception:
            return None

    def _spend(self, action: str, uin: str) -> None:
        now = self._clock()
        self.budget.spend(now)
        self.state.last_action = action
        self.state.last_action_at = now
        self.state.actions_used = self.budget.used(now)
        self.log.append({"at": now, "type": "action", "action": action, "account": uin})

    def _settle(self, state: str, note: str, event: OfflineEvent) -> None:
        self.state.recovery_state = state
        self.state.recovery_note = note
        self.state.actions_used = self.budget.used(self._clock())
        self.log.append({"at": self._clock(), "type": "settled", "state": state, "note": note, "reason": event.reason})
        if state != STATE_IDLE:
            # 这时 QQ 发不出去，先留着，链路回来再补发。
            self.state.pending_notice = f"QQ 掉线（{event.summary()}）：{note}"

    async def _say(self, text: str) -> None:
        if not self.settings.notify_admins:
            return
        with contextlib.suppress(Exception):
            if await self._notify(text):
                self.state.pending_notice = ""

    async def flush_notice(self) -> None:
        """链路回来之后把掉线期间没发出去的那条补给管理员。"""
        if self.state.pending_notice and self.state.online:
            await self._say(self.state.pending_notice)

    # ---- 后台 ----

    async def watch_forever(self) -> None:
        """心跳看门狗。NapCat 的 heartbeat 停了而 WS 还挂着时，这里是唯一能发现的地方。"""
        while True:
            await asyncio.sleep(HEARTBEAT_CHECK_SEC)
            try:
                if self.state.online and self.state.last_heartbeat_at > 0:
                    silence = self._clock() - self.state.last_heartbeat_at
                    if silence > self.state.heartbeat_interval_sec * HEARTBEAT_MISSES:
                        self.schedule(classify_link_down(
                            KIND_HEARTBEAT, at=self._clock(), detail=f"{int(silence)} 秒没有心跳",
                        ))
                if self.state.online and self.state.pending_notice:
                    await self.flush_notice()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    def close(self) -> None:
        """进程自己在退出：之后的断开是我们造成的，不算掉线。"""
        self._closed = True
        if self._handling is not None and not self._handling.done():
            self._handling.cancel()
        self._handling = None


def notice_fields(event: Any) -> dict[str, Any]:
    """NoneBot 对未知 notice_type 只认基类，额外字段在 model_extra / dict() 里。"""
    fields: dict[str, Any] = {}
    extra = getattr(event, "model_extra", None)
    if isinstance(extra, Mapping):
        fields.update(extra)
    for method in ("model_dump", "dict"):
        dump = getattr(event, method, None)
        if callable(dump):
            try:
                data = dump()
            except Exception:
                data = None
            if isinstance(data, Mapping):
                fields.update(data)
                break
    for name in ("tag", "message", "kicked_type", "same_device", "instance_id", "client_type", "user_id", "notice_type"):
        if name not in fields and hasattr(event, name):
            fields[name] = getattr(event, name)
    return fields


def events_path(workspace: Path) -> Path:
    return Path(workspace) / "data" / EVENTS_FILE_NAME
