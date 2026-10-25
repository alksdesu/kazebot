"""控制台的实例清单。

多开时每个 bot 是一套独立进程，各自挂在自己的 URL 前缀下，靠这份共享文件互相看见；一个实例只跑一个平台、登一个号。
端口与序号的推导也在这里，shell 侧同样从这里取，两套算法漂移会让新实例静默抢占旧实例的端口。
"""
from __future__ import annotations

import logging
import os
import re
import socket
import threading
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Literal

import yaml

import config_layout
from platforms.builtin import QQ_NAPCAT
from platforms.descriptor import InstancePort, normalize_name
from platforms.registry import DEFAULT_PLATFORM_ID, all_platforms

_log = logging.getLogger(__name__)

_ENV_FILE = config_layout.INSTANCES_FILE_ENV
_ENV_PREFIX = "CLONOTH_URL_PREFIX"

# 实例名会一路传进 root 侧的 systemd 单元名，宽一格都是提权面。纯数字那支认改造前按 QQ 号开的实例。
INSTANCE_ID_PATTERN = r"[a-z][a-z0-9_]{1,31}|[1-9][0-9]{4,10}"
_INSTANCE_ID_RE = re.compile(rf"(?:{INSTANCE_ID_PATTERN})")
# 数据根下已有的共享目录：表情包库和共享配置组。实例叫这个名字，工作区就和它是同一个目录。
RESERVED_IDS = frozenset({"stickers", config_layout.SHARED_DIR})

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


def is_instance_id(name: Any) -> bool:
    text = name if isinstance(name, str) else ""
    return bool(_INSTANCE_ID_RE.fullmatch(text)) and text not in RESERVED_IDS


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


class InstanceDirectoryError(ValueError):
    pass


def _navigation_prefix(raw: Any) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise InstanceDirectoryError("实例目录包含无效路径")
    text = raw.strip()
    if not re.fullmatch(r"/?[\w.~/-]*", text, flags=re.ASCII) or text.startswith("//"):
        raise InstanceDirectoryError("实例目录包含无效路径")
    path = normalize_prefix(text)
    if path and any(part in {"", ".", ".."} for part in path[1:].split("/")):
        raise InstanceDirectoryError("实例目录包含无效路径")
    return path


_ReadMode = Literal["tolerant", "required", "strict"]

# 已经为哪个文件版本记过「读不懂」：清单每次请求都重读，不去重的话坏文件每次刷一条。
_unreadable_stamps: dict[Path, tuple[int, int, int] | None] = {}
_unreadable_lock = threading.Lock()


def _report_unreadable(path: Path, error: Exception) -> None:
    stamp = config_layout.file_stamp(path)
    with _unreadable_lock:
        if path in _unreadable_stamps and _unreadable_stamps[path] == stamp:
            return
        _unreadable_stamps[path] = stamp
    _log.warning("实例清单 %s 读不懂，按没有别的实例算，修好之前拦不住双登：%s", path, error)


def _read(path: Path, *, mode: _ReadMode = "tolerant") -> list[dict[str, Any]]:
    """清单原样读出来洗一遍。

    tolerant 读不懂就当成空表，只为展示读的一方用。required 是写入方用的：文件读不懂就抛，
    拿空表写回去，别的实例就全从清单里消失了。strict 连单条写坏了也抛，给逐条信任清单的导航接口用。
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        if mode == "tolerant":
            _report_unreadable(path, error)
            return []
        raise InstanceDirectoryError("实例目录暂时不可用") from error
    rows = raw.get("instances") if isinstance(raw, dict) else raw
    if rows is None and mode != "strict":
        rows = []
    if not isinstance(rows, list):
        if mode == "tolerant":
            _report_unreadable(path, InstanceDirectoryError("实例目录格式无效"))
            return []
        raise InstanceDirectoryError("实例目录格式无效")
    strict = mode == "strict"
    seen: set[str] = set()
    cleaned: list[dict[str, Any]] = []
    for entry in rows:
        if strict:
            if not isinstance(entry, dict) or any(
                value is not None and not isinstance(value, (str, int))
                for value in (entry.get("id"), entry.get("uin"), entry.get("label"))
            ):
                raise InstanceDirectoryError("实例目录条目无效")
            _navigation_prefix(entry.get("path"))
        row = _clean(entry)
        if strict and row is None:
            raise InstanceDirectoryError("实例目录条目无效")
        # 同一个实例写两条会让切换器多出一个永远点不到的重复项。
        if row and row["id"] not in seen:
            seen.add(row["id"])
            cleaned.append(row)
    return cleaned


def instance_workspace(workspace_root: Path, row: dict[str, Any]) -> Path | None:
    """清单里这个实例的工作区：本实例就是 workspace_root；序号 0 是代码目录里的主实例；其余在数据根下按实例名放。

    实例名不合规的不认：清单手改得动，拿任意字符串拼出来的路径可能落在数据根外面。
    """
    if row["path"] == url_prefix():
        return Path(workspace_root)
    if row["idx"] == 0:
        return config_layout.CODE_ROOT
    data = config_layout.data_root()
    if data is None or not is_instance_id(row["id"]):
        return None
    return data / row["id"]


def _account_report(workspace: Path) -> dict[str, str] | None:
    """实例自己上报的平台和账号。没有、读不懂、缺字段都是 None，回去用清单里记的。"""
    path = config_layout.instance_account_path(workspace)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        _log.debug("跳过读不懂的账号上报 %s：%s", path, error)
        return None
    if not isinstance(raw, dict):
        return None
    report = {
        "id": str(raw.get("id") or "").strip(),
        "platform": normalize_name(raw.get("platform")),
        "account": str(raw.get("account") or "").strip(),
    }
    return report if all(report.values()) else None


def _with_reported_accounts(workspace_root: Path, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """叠上各实例自己上报的账号：多开实例写不了数据根下的清单，换号只记在自己的工作区里，它说的才是现在登着的号。"""
    for row in rows:
        workspace = instance_workspace(workspace_root, row)
        report = _account_report(workspace) if workspace is not None else None
        # 名字对不上的不认：代码目录里那份可能是改名之前的主实例留下的。
        if report is not None and report["id"] == row["id"]:
            row["platform"], row["account"] = report["platform"], report["account"]
    return rows


def load_instances(
    workspace_root: Path, *, strict: bool = False, required: bool = False,
) -> list[dict[str, Any]]:
    """读共享清单，叠上各实例自己上报的账号。文件不存在就是单实例部署，空表让前端不必渲染切换器。

    required 时清单读不懂抛 InstanceDirectoryError：要拿结果去分配序号、查重名的一方不能把坏文件当成没有别的实例。
    """
    mode: _ReadMode = "strict" if strict else "required" if required else "tolerant"
    return _with_reported_accounts(workspace_root, _read(instances_file(workspace_root), mode=mode))


def load_instance_directory(workspace_root: Path) -> dict[str, Any]:
    current = _navigation_prefix(os.environ.get(_ENV_PREFIX))
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in load_instances(workspace_root, strict=True):
        path = row["path"]
        if path in seen:
            continue
        seen.add(path)
        label = row["label"]
        if not path and (label.isdecimal() or label == row["account"]):
            label = "主实例"
        entries.append({"label": label, "path": path})
    if current not in seen:
        entries.append({"label": current.rsplit("/", 1)[-1] or "主实例", "path": current})
    return {"current_path": current, "instances": entries}


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
    """跨实例互斥。几个 supervisor 各自的进程锁管不到彼此，得落到文件上；root 侧删实例时 flock 的也是这个锁文件。"""
    return config_layout.locked(path)


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    """调用方已经拿着 _locked(path)。"""
    config_layout.write_locked(
        path, yaml.safe_dump({"instances": [_stored(row) for row in rows]}, allow_unicode=True, sort_keys=False),
    )


def save_instance(
    workspace_root: Path,
    *,
    instance_id: str,
    label: str,
    idx: int,
    platform: str = DEFAULT_PLATFORM_ID,
    account: str = "",
) -> list[dict[str, Any]]:
    """登记一个实例。同名视为改写，不会留下两条；没给账号时留着原来记下的那个，平台换了就不留。

    清单读不懂时抛 InstanceDirectoryError，一个字节都不写。
    """
    instance_id = str(instance_id or "").strip()
    if not instance_id:
        raise ValueError("实例名不能为空")
    # 读取时序号越界会被当成 -1，照写就成了读回来和写进去对不上的一条。
    if not _is_index(idx):
        raise ValueError(f"序号要在 0-{MAX_INDEX} 之间：{idx!r}")
    platform_id = normalize_name(platform) or DEFAULT_PLATFORM_ID
    path = instances_file(workspace_root)
    with _locked(path):
        rows = _read(path, mode="required")
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


def _write_account_report(workspace_root: Path, instance_id: str, platform_id: str, account: str) -> None:
    config_layout.write_config_text(
        config_layout.instance_account_path(workspace_root),
        yaml.safe_dump(
            {"id": instance_id, "platform": platform_id, "account": account}, allow_unicode=True, sort_keys=False,
        ),
    )


def _mirror_account(path: Path, where: str, platform_id: str, account: str) -> None:
    """清单写得动时把上报同步进去，直接读文件的人也看得到。没同步上不要紧：读清单的人会叠上报。"""
    try:
        with _locked(path):
            rows = _read(path, mode="required")
            row = next((r for r in rows if r["path"] == where), None)
            if row is None or (row["platform"], row["account"]) == (platform_id, account):
                return
            row["platform"], row["account"] = platform_id, account
            _write(path, rows)
    except (OSError, InstanceDirectoryError) as error:
        _log.warning("账号已记进本实例的上报文件，同步进清单 %s 失败：%s", path, error)


def record_account(workspace_root: Path, *, prefix: str, platform_id: str, account: str) -> bool:
    """本实例在它的平台上换了号，记下来，别的实例据此拦着同一个号双登。返回记下的号变没变。

    记在自己工作区的上报文件里：多开实例的 systemd 沙箱只放开它自己的工作区，数据根下的清单它写不了，
    读清单的人会把上报叠上去。清单写得动（主实例）时顺带同步一份。单实例部署没有清单，什么也不做。
    上报写不进去抛 OSError，清单读不懂抛 InstanceDirectoryError：那就是真没记上，调用方得告诉人。
    """
    path = instances_file(workspace_root)
    key, value = normalize_name(platform_id), str(account or "").strip()
    if not key or not value or not path.is_file():
        return False
    where = normalize_prefix(prefix)
    rows = _with_reported_accounts(workspace_root, _read(path, mode="required"))
    row = next((r for r in rows if r["path"] == where), None)
    if row is None:
        return False
    # 清单里的平台是建实例时声明的；上报的对不上时以实例实际在跑的那个为准。
    changed = (row["platform"], row["account"]) != (key, value)
    if changed:
        _write_account_report(workspace_root, row["id"], key, value)
    # 只读挂载上 access 也报不可写，免得去拿一把建不出来的锁。
    if os.access(path.parent, os.W_OK):
        _mirror_account(path, where, key, value)
    return changed


def remove_instance(workspace_root: Path, instance_id: str) -> list[dict[str, Any]]:
    """摘掉一个实例。不碰它的数据目录，那是 root 侧的事。清单读不懂时抛 InstanceDirectoryError，一个字节都不写。"""
    key = str(instance_id or "").strip()
    path = instances_file(workspace_root)
    with _locked(path):
        rows = [r for r in _read(path, mode="required") if r["id"] != key]
        _write(path, rows)
        return rows
