"""控制台的账号清单。

多开时每个号是一套独立进程，各自挂在自己的 URL 前缀下，靠这份共享文件互相看见。
端口与序号的推导也在这里，shell 侧同样从这里取，两套算法漂移会让新号静默抢占旧号的端口。
"""
from __future__ import annotations

import contextlib
import os
import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

_ENV_FILE = "CLONOTH_INSTANCES_FILE"
_ENV_PREFIX = "CLONOTH_URL_PREFIX"

# (基准端口, 每号步长)。序号 0 正好落在单实例部署原有的那一组上。
_PORTS: dict[str, tuple[int, int]] = {
    "supervisor": (8765, 10),
    "bot": (8080, 10),
    "bridge": (8769, 10),
    "napcat": (6099, 100),
}

# 序号无上限会一路撞进别的服务的端口，磁盘也撑不住这么多号。
MAX_INDEX = 8

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


def ports_for(idx: int) -> dict[str, int]:
    """按序号推导四个监听端口。"""
    return {name: base + step * idx for name, (base, step) in _PORTS.items()}


def prefix_for(idx: int, uin: str) -> str:
    """序号 0 占站点根，其余各挂一层，几个号共用一个域名。"""
    return "" if idx == 0 else f"/i/{uin}"


def url_prefix() -> str:
    """本实例挂在哪个 URL 前缀下。单实例为空串，路由与挂载前逐字相同。"""
    raw = str(os.environ.get(_ENV_PREFIX) or "").strip().strip("/")
    return f"/{raw}" if raw else ""


def instances_file(workspace_root: Path) -> Path:
    """清单位置。多实例要都指向同一份，否则各自只看得见自己。"""
    raw = str(os.environ.get(_ENV_FILE) or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(workspace_root) / "config" / "instances.yaml"


def _clean(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    uin = str(entry.get("uin") or "").strip()
    if not uin:
        return None
    path = str(entry.get("path") or "").strip().strip("/")
    raw_idx = entry.get("idx")
    if isinstance(raw_idx, int) and not isinstance(raw_idx, bool) and 0 <= raw_idx <= MAX_INDEX:
        idx = raw_idx
    else:
        # 老清单没这个字段。根实例必然是序号 0；带前缀的推不出来，标 -1 让分配退回
        # 端口探测 —— 宁可多跳一个序号，也不能算出一个正在跑的实例的端口。
        idx = 0 if not path else -1
    return {
        "uin": uin,
        "label": str(entry.get("label") or "").strip() or uin,
        "path": f"/{path}" if path else "",
        "idx": idx,
    }


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
        # 同一个号写两条会让切换器多出一个永远点不到的重复项。
        if row and row["uin"] not in seen:
            seen.add(row["uin"])
            cleaned.append(row)
    return cleaned


def load_instances(workspace_root: Path) -> list[dict[str, Any]]:
    """读共享清单。文件不存在就是单实例部署，空表让前端不必渲染切换器。"""
    return _read(instances_file(workspace_root))


def port_in_use(port: int, *, host: str = "127.0.0.1", timeout: float = 0.35) -> bool:
    """连得上就是有人在听。不用 bind 探测：那会短暂占住端口，撞上正要启动的实例。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout)
        return probe.connect_ex((host, port)) == 0


def allocate_index(
    rows: list[dict[str, Any]],
    *,
    probe: Any = None,
) -> int:
    """挑一个没人用的序号。

    清单是软状态（可能被手改、可能漏记），所以除了避开已登记的序号，还要实测端口。
    """
    check = probe if probe is not None else port_in_use
    taken = {row["idx"] for row in rows if isinstance(row.get("idx"), int) and row["idx"] >= 0}
    for idx in range(MAX_INDEX + 1):
        if idx in taken:
            continue
        if any(check(port) for port in ports_for(idx).values()):
            continue
        return idx
    raise ValueError(f"没有空闲序号了（上限 {MAX_INDEX}）")


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """跨实例互斥。几个 supervisor 各自的进程锁管不到彼此，得落到文件上。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    key = str(lock_path)
    with _LOCKS_GUARD:
        process_lock = _LOCKS.setdefault(key, threading.Lock())
    with process_lock, lock_path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    body = yaml.safe_dump({"instances": rows}, allow_unicode=True, sort_keys=False)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)


def save_instance(
    workspace_root: Path,
    *,
    uin: str,
    label: str,
    idx: int,
) -> list[dict[str, Any]]:
    """登记一个号。同 uin 视为改写，不会留下两条。"""
    path = instances_file(workspace_root)
    row = {"uin": uin, "label": label or uin, "path": prefix_for(idx, uin), "idx": idx}
    with _locked(path):
        rows = [r for r in _read(path) if r["uin"] != uin]
        rows.append(row)
        rows.sort(key=lambda r: (r["idx"] if r["idx"] >= 0 else MAX_INDEX + 1, r["uin"]))
        _write(path, rows)
        return rows


def remove_instance(workspace_root: Path, uin: str) -> list[dict[str, Any]]:
    """摘掉一个号。不碰它的数据目录，那是 root 侧的事。"""
    path = instances_file(workspace_root)
    with _locked(path):
        rows = [r for r in _read(path) if r["uin"] != uin]
        _write(path, rows)
        return rows
