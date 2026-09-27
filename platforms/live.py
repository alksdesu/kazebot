"""适配器 live 状态文件的约定：bot 进程写，核心只读。

顶层几项各平台一样：心跳、平台连没连上、当前账号、待处理条数，以及生效配置出自哪个文件版本、解析错误。
平台自己的运行事实放在 runtime 下，只有它自己的界面读。
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .descriptor import STATE_LIVE, PlatformDescriptor
from .registry import platform_state_path

PUBLISHED_AT = "published_at"
CONNECTED = "connected"
ACCOUNT = "account"
BACKLOG = "backlog"
# 这四项由热载配置写：读的是哪个文件、磁盘上的指纹、生效值出自哪个版本、解析错误。
CONFIG_PATH = "config_path"
CONFIG_FILE = "file"
CONFIG_LOADED = "loaded"
PARSE_ERROR = "parse_error"
RUNTIME = "runtime"

# 每时每刻都在动，判断内容变没变时不算，否则每个对齐周期都要重写一次。
VOLATILE_FIELDS: frozenset[str] = frozenset({PUBLISHED_AT, BACKLOG})

# 内容没变也至少隔这么久重写一次，核心拿写入时间当心跳。
HEARTBEAT_SEC = 30.0
# 三倍心跳还没重写，就当 bot 失联。
STALE_AFTER_SEC = 3 * HEARTBEAT_SEC


def live_state_path(
    platform: PlatformDescriptor, workspace_root: Path, *, environ: Mapping[str, str] | None = None,
) -> Path:
    return platform_state_path(platform, STATE_LIVE, workspace_root, environ=environ)


def read_live_state(platform: PlatformDescriptor, workspace_root: Path) -> dict[str, Any]:
    """读不到、读不懂、不是对象都当没公布。"""
    try:
        data = json.loads(live_state_path(platform, workspace_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_live_state(path: Path, payload: Mapping[str, Any]) -> None:
    """先写同目录临时文件再替换：核心随时在读，不能让它读到半份。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def file_fingerprint(path: Path) -> dict[str, Any]:
    """配置文件的版本指纹。CONFIG_FILE、CONFIG_LOADED 都是这个形状，核心拿它比「生效值是不是出自当前文件」。"""
    try:
        stat = Path(path).stat()
    except OSError:
        return {"exists": False, "mtime_ns": 0, "size": 0}
    return {"exists": True, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size}


def same_file(reported: Any, path: Path) -> bool:
    """bot 报的配置路径和这边算出来的是不是同一个文件。两边写法可能不同，按真实路径比。"""
    if not isinstance(reported, str) or not reported.strip():
        return False
    return os.path.normcase(os.path.realpath(reported)) == os.path.normcase(os.path.realpath(path))


@dataclass(frozen=True)
class LiveStatus:
    """一份 live 状态按约定读出来的样子。"""

    # 文件在，而且是个对象。
    published: bool
    # 距上次写入多少秒；没写时间戳或读不懂时是 None，时间戳在将来时为负。
    age_sec: float | None
    # 确认在跑：心跳没超时，时间戳也不在将来。健康显示按它判。
    fresh: bool
    # 排除不了还在跑：时间戳在将来说明时钟被拨过，写它的 bot 可能刚写完。要求 bot 停着的操作按它判。
    may_be_running: bool
    connected: bool
    account: str
    backlog: int
    config_path: str
    config_loaded: Mapping[str, Any] | None
    parse_error: str

    @property
    def stale(self) -> bool:
        return self.published and not self.fresh


def _timestamp(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return None
    return stamp if math.isfinite(stamp) and stamp > 0 else None


def _count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, number)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def live_status(state: Mapping[str, Any], *, now: float | None = None) -> LiveStatus:
    published = isinstance(state, Mapping) and bool(state)
    source: Mapping[str, Any] = state if published else {}
    stamp = _timestamp(source.get(PUBLISHED_AT))
    age = None if stamp is None else (time.time() if now is None else now) - stamp
    fresh = age is not None and 0 <= age <= STALE_AFTER_SEC
    account = source.get(ACCOUNT)
    loaded = source.get(CONFIG_LOADED)
    return LiveStatus(
        published=published,
        age_sec=None if age is None else round(age, 3),
        fresh=fresh,
        may_be_running=age is not None and age <= STALE_AFTER_SEC,
        connected=fresh and source.get(CONNECTED) is True,
        account=str(account).strip() if isinstance(account, (str, int)) and not isinstance(account, bool) else "",
        backlog=_count(source.get(BACKLOG)),
        config_path=_text(source.get(CONFIG_PATH)),
        config_loaded=loaded if isinstance(loaded, Mapping) else None,
        parse_error=_text(source.get(PARSE_ERROR)),
    )


def read_live_status(platform: PlatformDescriptor, workspace_root: Path, *, now: float | None = None) -> LiveStatus:
    return live_status(read_live_state(platform, workspace_root), now=now)
