"""记忆条目的到期时间：写入时怎么读模型和控制台给的值，注入和清理时怎么判到期。

落盘一律是带时区偏移的完整时间；只写日期的算到那天结束，不带偏移的按 memory.timezone 读。
"""
from __future__ import annotations

import logging
import re
import threading
from collections import OrderedDict
from datetime import date, datetime, time, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clonoth_runtime import get_str, load_runtime_config

log = logging.getLogger(__name__)

EXPIRES_AT = "expires_at"
# dream 审过这一版的时效和长期价值。工具和控制台改写条目时不带它，内容一改就重新进审。
REVIEWED_AT = "reviewed_at"

EXPIRY_NONE = "none"
EXPIRY_ACTIVE = "active"
EXPIRY_EXPIRED = "expired"
EXPIRY_UNREADABLE = "unreadable"

EXPIRY_FORMS = "YYYY-MM-DD (valid through that day) or YYYY-MM-DD HH:MM, optionally with an offset such as +08:00"

_DEFAULT_ZONE = "Asia/Shanghai"
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")
_END_OF_DAY = time(23, 59, 59)
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 注入两秒就重读一次记忆本：同一个坏值只记一次日志，记过的满了丢最早的。
_WARN_LIMIT = 256
_warned_lock = threading.Lock()
_warned: OrderedDict[tuple[str, str], None] = OrderedDict()


class ExpiryValueError(ValueError):
    """到期时间读不懂。"""


class ExpiryPassed(ExpiryValueError):
    """写进来的到期时间已经过去。"""

    def __init__(self, moment: datetime) -> None:
        super().__init__(f"expires_at {expiry_text(moment)} has already passed")
        self.moment = moment


def _warn_once(key: tuple[str, str], message: str, *args: Any) -> None:
    with _warned_lock:
        if key in _warned:
            return
        _warned[key] = None
        while len(_warned) > _WARN_LIMIT:
            _warned.popitem(last=False)
    log.warning(message, *args)


def _load_zone(name: str) -> tzinfo | None:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def memory_timezone(workspace_root: Path | None) -> tzinfo:
    """memory.timezone 指的时区。写错了退回默认时区；默认的也载不了（系统没有时区库）就用 UTC。"""
    name = _DEFAULT_ZONE
    if workspace_root is not None:
        configured = get_str(load_runtime_config(Path(workspace_root)), "memory.timezone", _DEFAULT_ZONE)
        name = configured.strip() or _DEFAULT_ZONE
    zone = _load_zone(name)
    if zone is not None:
        return zone
    _warn_once(("timezone", name), "memory.timezone %r is not a known time zone; using %s", name, _DEFAULT_ZONE)
    zone = _load_zone(_DEFAULT_ZONE)
    if zone is not None:
        return zone
    _warn_once(("timezone", _DEFAULT_ZONE), "time zone %s is unavailable here; memory dates are read as UTC", _DEFAULT_ZONE)
    return timezone.utc


def zone_label(zone: tzinfo) -> str:
    return str(getattr(zone, "key", "") or zone)


def local_stamp(moment: datetime, zone: tzinfo) -> str:
    """带星期的本地时间：换算「下周一」这类说法要用到。"""
    local = moment.astimezone(zone)
    return f"{local:%Y-%m-%d %H:%M} {_WEEKDAYS[local.weekday()]}"


def now_text(zone: tzinfo, now: datetime | None = None) -> str:
    """给模型看的当前时间。"""
    return f"{local_stamp(now or datetime.now(timezone.utc), zone)}（{zone_label(zone)}）"


def expiry_text(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def parse_expiry(value: Any, zone: tzinfo) -> datetime | None:
    """读成带时区的时刻：没写是 None，读不懂抛 ExpiryValueError。

    手写的 YAML 不加引号时，日期读出来是 date / datetime，照样认。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        moment = datetime.combine(value, _END_OF_DAY)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            if _DATE_ONLY.fullmatch(text):
                moment = datetime.combine(date.fromisoformat(text), _END_OF_DAY)
            else:
                moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ExpiryValueError(f"unreadable expires_at {text!r}") from exc
    else:
        raise ExpiryValueError(f"expires_at is a {type(value).__name__}, not a date")
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=zone)


def new_expiry(value: Any, zone: tzinfo, now: datetime) -> datetime | None:
    """写入时给的到期时间：空值是长期有效，读不懂抛 ExpiryValueError，已经过去的抛 ExpiryPassed。"""
    moment = parse_expiry(value, zone)
    if moment is not None and moment <= now:
        raise ExpiryPassed(moment)
    return moment


def entry_expiry(entry: dict[str, Any], zone: tzinfo) -> datetime | None:
    """条目上的到期时刻。写得读不懂的当成没写：照常注入、不删，日志按条记一次。"""
    raw = entry.get(EXPIRES_AT)
    try:
        return parse_expiry(raw, zone)
    except ExpiryValueError as exc:
        _warn_once(("entry", f"{entry.get('id')}={raw!r}"), "memory %s: %s; it never expires until fixed", entry.get("id"), exc)
        return None


def expiry_field(entry: dict[str, Any], zone: tzinfo) -> str:
    """给人和模型看的到期时间：统一成带偏移的完整时间，没写是空串，读不懂的原样给出。"""
    raw = entry.get(EXPIRES_AT)
    try:
        moment = parse_expiry(raw, zone)
    except ExpiryValueError:
        return str(raw)
    return expiry_text(moment) if moment is not None else ""


def expiry_state(entry: dict[str, Any], zone: tzinfo, now: datetime) -> str:
    try:
        moment = parse_expiry(entry.get(EXPIRES_AT), zone)
    except ExpiryValueError:
        return EXPIRY_UNREADABLE
    if moment is None:
        return EXPIRY_NONE
    return EXPIRY_EXPIRED if moment <= now else EXPIRY_ACTIVE


def entry_expired(entry: dict[str, Any], zone: tzinfo, now: datetime) -> bool:
    moment = entry_expiry(entry, zone)
    return moment is not None and moment <= now
