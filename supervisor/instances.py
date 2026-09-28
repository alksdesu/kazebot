"""控制台的实例清单。

多开时每个 bot 是一套独立进程，各自挂在自己的 URL 前缀下，靠这份共享文件互相看见；一个实例只跑一个平台、登一个号。
端口与序号的推导也在这里，shell 侧同样从这里取，两套算法漂移会让新实例静默抢占旧实例的端口。
"""
from __future__ import annotations

import os
import socket
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import yaml

from file_lock import exclusive_lock
from platforms.builtin import QQ_NAPCAT
from platforms.descriptor import InstancePort, normalize_name
from platforms.registry import DEFAULT_PLATFORM_ID, all_platforms

_ENV_FILE = "CLONOTH_INSTANCES_FILE"
_ENV_PREFIX = "CLONOTH_URL_PREFIX"

# 核心自己的端口。平台进程要的端口由平台描述的 instance_ports 声明，序号 0 落在单实例部署原有的那一组上。
_CORE_PORTS: tuple[InstancePort, ...] = (InstancePort("supervisor", 8765, 10),)

# 序号无上限会一路撞进别的服务的端口，磁盘也撑不住这么多实例。
MAX_INDEX = 8


def port_table() -> tuple[InstancePort, ...]:
    """全部要按序号分配的端口。名字重了、两个序号落到同一个端口都直接报错，不能等到新实例起不来才发现。"""
    ports = (*_CORE_PORTS, *(port for platform in all_platforms() for port in platform.instance_ports))
    names = [port.name for port in ports]
    duplicated = sorted({name for name in names if names.count(name) > 1})
    if duplicated:
        raise ValueError(f"端口名重复：{duplicated}")
    owners: dict[int, str] = {}
    for idx in range(MAX_INDEX + 1):
        for port in ports:
            number = port.at(idx)
            if number in owners:
                raise ValueError(f"序号 {idx} 的 {port.name} 端口 {number} 和{owners[number]}撞了")
            owners[number] = f"序号 {idx} 的 {port.name}"
    return ports


def ports_for(idx: int) -> dict[str, int]:
    """按序号推导全部监听端口。"""
    return {port.name: port.at(idx) for port in port_table()}


def normalize_prefix(raw: Any) -> str:
    """URL 前缀一律写成 "" 或 "/a/b"。清单里的 path、env 里的前缀、调用方传进来的都按这个比，差一个斜杠就认不出自己。"""
    text = str(raw or "").strip().strip("/")
    return f"/{text}" if text else ""


def prefix_for(idx: int, instance_id: str) -> str:
    """序号 0 占站点根，其余各挂一层，几个实例共用一个域名。"""
    return "" if idx == 0 else normalize_prefix(f"/i/{instance_id}")


def url_prefix() -> str:
    """本实例挂在哪个 URL 前缀下。单实例为空串，路由与挂载前逐字相同。"""
    return normalize_prefix(os.environ.get(_ENV_PREFIX))


def instances_file(workspace_root: Path) -> Path:
    """清单位置。多实例要都指向同一份，否则各自只看得见自己。"""
    raw = str(os.environ.get(_ENV_FILE) or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(workspace_root) / "config" / "instances.yaml"


def _platform_account(entry: dict[str, Any]) -> tuple[str, str]:
    """一条清单记着的平台和账号。

    读和写走同一套清洗：写进去的脏值读出来就不见了，写的一方却以为记上了。改造前的两种写法照样认：
    实例名就是 QQ 号时的 uin，和一个实例挂几个平台时的 accounts 映射。映射里有几项时认 QQ 那一项，
    改造前能挂多个平台的只有它；其余的是那时的遗留，这个实例已经不跑它们了。
    """
    platform = normalize_name(entry.get("platform"))
    account = str(entry.get("account") or "").strip()
    if platform:
        return platform, account
    if account:
        # 手改的清单只写了账号没写平台：按没声明平台的实例算，账号不能丢。
        return DEFAULT_PLATFORM_ID, account
    legacy = entry.get("accounts")
    if isinstance(legacy, dict):
        cleaned = {
            normalize_name(key): str(value or "").strip()
            for key, value in legacy.items()
            if normalize_name(key) and str(value or "").strip()
        }
        if cleaned:
            chosen = QQ_NAPCAT.id if QQ_NAPCAT.id in cleaned else next(iter(cleaned))
            return chosen, cleaned[chosen]
    uin = str(entry.get("uin") or "").strip()
    return (QQ_NAPCAT.id, uin) if uin else (DEFAULT_PLATFORM_ID, "")


def _is_index(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_INDEX


def _clean(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    legacy_uin = str(entry.get("uin") or "").strip()
    instance_id = str(entry.get("id") or "").strip() or legacy_uin
    if not instance_id:
        return None
    path = normalize_prefix(entry.get("path"))
    raw_idx = entry.get("idx")
    if _is_index(raw_idx):
        idx = raw_idx
    else:
        # 老清单没这个字段。根实例必然是序号 0；带前缀的推不出来，标 -1 让分配退回
        # 端口探测 —— 宁可多跳一个序号，也不能算出一个正在跑的实例的端口。
        idx = 0 if not path else -1
    platform, account = _platform_account(entry)
    return {
        "id": instance_id,
        "label": str(entry.get("label") or "").strip() or instance_id,
        "path": path,
        "idx": idx,
        "platform": platform,
        "account": account,
    }


def _stored(row: dict[str, Any]) -> dict[str, Any]:
    """落盘的写法。QQ 实例另记一份 uin：退回改造前的版本时，那边只认这个字段，没有它整条会被丢掉。"""
    stored = {key: row[key] for key in ("id", "label", "path", "idx", "platform", "account")}
    if row["platform"] == QQ_NAPCAT.id and row["account"]:
        stored["uin"] = row["account"]
    return stored


def _read(path: Path) -> list[dict[str, Any]]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    rows = raw.get("instances") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    seen: set[str] = set()
    cleaned: list[dict[str, Any]] = []
    for entry in rows:
        row = _clean(entry)
        # 同一个实例写两条会让切换器多出一个永远点不到的重复项。
        if row and row["id"] not in seen:
            seen.add(row["id"])
            cleaned.append(row)
    return cleaned


def load_instances(workspace_root: Path) -> list[dict[str, Any]]:
    """读共享清单。文件不存在就是单实例部署，空表让前端不必渲染切换器。"""
    return _read(instances_file(workspace_root))


def account_owner_in(
    rows: list[dict[str, Any]], platform_id: str, account: str, *, prefix: str | None = None,
) -> dict[str, Any] | None:
    """rows 里登记着这个号的那一条。给了 prefix 就不算挂在它下面的那个实例，那是自己。

    比的是平台加账号：同机可能同时跑着 QQ 实例和别的平台的实例，两边的号可以长得一样。
    """
    key, value = normalize_name(platform_id), str(account or "").strip()
    if not key or not value:
        return None
    # None 是谁都不放过，"" 是放过根实例，两者不能折成一个。
    excused = None if prefix is None else normalize_prefix(prefix)
    return next(
        (
            row for row in rows
            if (excused is None or row["path"] != excused) and row["platform"] == key and row["account"] == value
        ),
        None,
    )


def account_owner(
    workspace_root: Path, platform_id: str, account: str, *, prefix: str,
) -> dict[str, Any] | None:
    """别的实例里登记着这个号的那一条。同一个号两边登，会互相把对方踢下线。"""
    return account_owner_in(load_instances(workspace_root), platform_id, account, prefix=prefix)


def port_in_use(port: int, *, host: str = "127.0.0.1", timeout: float = 0.35) -> bool:
    """连得上就是有人在听。不用 bind 探测：那会短暂占住端口，撞上正要启动的实例。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout)
        return probe.connect_ex((host, port)) == 0


class NoFreeIndex(ValueError):
    """序号用完了。要告诉操作的人，和端口表写错那类代码错误分开报。"""


def allocate_index(
    rows: list[dict[str, Any]],
    *,
    probe: Any = None,
) -> int:
    """挑一个没人用的序号。

    清单是软状态（可能被手改、可能漏记），所以除了避开已登记的序号，还要实测端口。
    """
    check = probe if probe is not None else port_in_use
    taken = {row["idx"] for row in rows if _is_index(row.get("idx"))}
    for idx in range(MAX_INDEX + 1):
        if idx in taken:
            continue
        if any(check(port) for port in ports_for(idx).values()):
            continue
        return idx
    raise NoFreeIndex(f"没有空闲序号了（上限 {MAX_INDEX}）")


def _locked(path: Path) -> AbstractContextManager[None]:
    """跨实例互斥。几个 supervisor 各自的进程锁管不到彼此，得落到文件上。"""
    return exclusive_lock(path.with_name(f"{path.name}.lock"))


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    body = yaml.safe_dump({"instances": [_stored(row) for row in rows]}, allow_unicode=True, sort_keys=False)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)


def save_instance(
    workspace_root: Path,
    *,
    instance_id: str,
    label: str,
    idx: int,
    platform: str = DEFAULT_PLATFORM_ID,
    account: str = "",
) -> list[dict[str, Any]]:
    """登记一个实例。同名视为改写，不会留下两条；没给账号时留着原来记下的那个，平台换了就不留。"""
    instance_id = str(instance_id or "").strip()
    if not instance_id:
        raise ValueError("实例名不能为空")
    # 读取时序号越界会被当成 -1，照写就成了读回来和写进去对不上的一条。
    if not _is_index(idx):
        raise ValueError(f"序号要在 0-{MAX_INDEX} 之间：{idx!r}")
    platform_id = normalize_name(platform) or DEFAULT_PLATFORM_ID
    path = instances_file(workspace_root)
    with _locked(path):
        rows = _read(path)
        previous = next((r for r in rows if r["id"] == instance_id), None)
        kept = previous["account"] if previous and previous["platform"] == platform_id else ""
        row = {
            "id": instance_id,
            "label": str(label or "").strip() or instance_id,
            "path": prefix_for(idx, instance_id),
            "idx": idx,
            "platform": platform_id,
            "account": str(account or "").strip() or kept,
        }
        rows = [r for r in rows if r["id"] != instance_id]
        rows.append(row)
        rows.sort(key=lambda r: (r["idx"] if r["idx"] >= 0 else MAX_INDEX + 1, r["id"]))
        _write(path, rows)
        return rows


def record_account(workspace_root: Path, *, prefix: str, platform_id: str, account: str) -> bool:
    """本实例在它的平台上换了号，记进清单，别的实例据此拦着同一个号双登。单实例部署没有清单，什么也不做。"""
    path = instances_file(workspace_root)
    key, value = normalize_name(platform_id), str(account or "").strip()
    if not key or not value or not path.is_file():
        return False
    where = normalize_prefix(prefix)
    with _locked(path):
        rows = _read(path)
        row = next((r for r in rows if r["path"] == where), None)
        if row is None or (row["platform"] == key and row["account"] == value):
            return False
        # 清单里的平台是建实例时声明的；上报的对不上时以实例实际在跑的那个为准。
        row["platform"], row["account"] = key, value
        _write(path, rows)
        return True


def remove_instance(workspace_root: Path, instance_id: str) -> list[dict[str, Any]]:
    """摘掉一个实例。不碰它的数据目录，那是 root 侧的事。"""
    key = str(instance_id or "").strip()
    path = instances_file(workspace_root)
    with _locked(path):
        rows = [r for r in _read(path) if r["id"] != key]
        _write(path, rows)
        return rows
