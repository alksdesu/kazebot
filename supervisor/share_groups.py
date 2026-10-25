"""共享配置组：几个实例的 config/ 链到数据根下同一份 shared/<组名>/config。

建组抄走建组实例现在的配置；加入的实例把自己那份改名归档进 data/；退出时拷一份组里当时的配置留给自己。
记忆在 data/ 里，从不共享。
"""
from __future__ import annotations

import contextlib
import ctypes
import errno
import functools
import json
import logging
import os
import secrets
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import config_layout
from file_lock import exclusive_lock
from platforms.protection import is_instance_env

from .instances import instance_workspace, load_instances, url_prefix

_log = logging.getLogger(__name__)

# 记建组信息的文件和组的 config/ 并排放，不跟着配置走。
_GROUP_META = "group.json"
# 几个实例同时建组、加入、退出、删组时排队用。
_GROUPS_LOCK = ".groups.lock"
# 抄配置时不带锁文件、写到一半的临时文件和字节码。
_COPY_IGNORE = shutil.ignore_patterns("*.lock", "*.tmp", "__pycache__")
ARCHIVE_PREFIX = "config.archived-"
# 切换 config/ 的空档里别的进程新建出来的 config/，回滚前挪到这个名字下。
INTERRUPTED_PREFIX = "config.interrupted-"

_AT_FDCWD = -100
_RENAME_EXCHANGE = 2


class ShareGroupError(RuntimeError):
    """给调用方直接当 4xx 用的人话错误。"""


def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _shared_root() -> Path:
    root = config_layout.shared_root()
    if root is None:
        raise ShareGroupError(f"单实例部署没有数据根（没设 {config_layout.INSTANCES_FILE_ENV}），用不了共享配置")
    return root


def _checked_name(raw: Any) -> str:
    name = str(raw or "").strip()
    if not config_layout.is_share_group_name(name):
        raise ShareGroupError("组名要以小写字母开头、只含小写字母数字和下划线，2-32 位")
    return name


def _locked_groups(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    return exclusive_lock(root / _GROUPS_LOCK)


def _instance_workspaces(workspace_root: Path) -> list[tuple[dict[str, Any], Path]]:
    """清单里每个找得到工作区的实例，连同它的工作区。"""
    found: list[tuple[dict[str, Any], Path]] = []
    for row in load_instances(workspace_root):
        workspace = instance_workspace(workspace_root, row)
        if workspace is not None:
            found.append((row, workspace))
    return found


def _members(workspace_root: Path) -> dict[str, list[dict[str, Any]]]:
    """按组名列出成员。成员身份只看各自 config/ 链到哪：链接就是唯一的事实，不另外记一份会过期的名单。"""
    prefix = url_prefix()
    members: dict[str, list[dict[str, Any]]] = {}
    for row, workspace in _instance_workspaces(workspace_root):
        group = config_layout.config_share_group(workspace)
        if group is not None:
            members.setdefault(group, []).append(
                {"id": row["id"], "label": row["label"], "current": row["path"] == prefix}
            )
    return members


def _current_row(workspace_root: Path) -> dict[str, Any] | None:
    prefix = url_prefix()
    return next((row for row in load_instances(workspace_root) if row["path"] == prefix), None)


def _read_meta(group_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads((group_dir / _GROUP_META).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _archives(workspace_root: Path) -> list[str]:
    """本实例加入共享组时归档的那几份旧配置，新的在前。"""
    data = Path(workspace_root) / "data"
    try:
        names = [entry.name for entry in data.iterdir() if entry.name.startswith(ARCHIVE_PREFIX) and entry.is_dir()]
    except OSError:
        return []
    return [f"data/{name}" for name in sorted(names, reverse=True)]


def overview(workspace_root: Path) -> dict[str, Any]:
    """控制台的数据源：能不能用、本实例在哪个组、各组有谁、本实例归档过哪些旧配置。"""
    root = config_layout.shared_root()
    current = config_layout.config_share_group(workspace_root)
    if root is None:
        return {
            "available": False,
            "reason": "单实例部署没有数据根，多开之后才能共享配置",
            "current": None,
            "groups": [],
            "archives": _archives(workspace_root),
        }
    members = _members(workspace_root)
    groups: list[dict[str, Any]] = []
    try:
        entries = sorted(root.iterdir()) if root.is_dir() else []
    except OSError:
        entries = []
    for entry in entries:
        if not config_layout.is_share_group_name(entry.name) or not (entry / config_layout.CONFIG_DIR).is_dir():
            continue
        meta = _read_meta(entry)
        groups.append({
            "name": entry.name,
            "members": members.get(entry.name, []),
            "created_at": str(meta.get("created_at") or ""),
            "created_by": str(meta.get("created_by") or ""),
        })
    return {"available": True, "reason": "", "current": current, "groups": groups, "archives": _archives(workspace_root)}


def _make_dir_link(link: Path, target: Path) -> None:
    """建目录链接，指向绝对路径。Windows 上普通账号没有建软链的权限，退而建目录联接：对读写配置来说两者没有区别。"""
    destination = Path(target).absolute()
    try:
        os.symlink(destination, link, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        import _winapi

        _winapi.CreateJunction(str(destination), str(link))


def _remove_link(link: Path) -> None:
    """删掉目录链接本身，不碰它指着的内容。Windows 的联接只能按目录删，POSIX 的软链只能按文件删。"""
    if not config_layout.is_dir_link(link):
        return
    if os.name == "nt":
        os.rmdir(link)
    else:
        os.unlink(link)


def _present(path: Path) -> bool:
    """有东西占着这个名字，悬空的链接也算。"""
    return path.exists() or path.is_symlink()


def _free_data_path(workspace_root: Path, prefix: str) -> Path:
    base = Path(workspace_root) / "data" / f"{prefix}{_now_stamp()}"
    candidate, serial = base, 1
    while _present(candidate):
        serial += 1
        candidate = base.with_name(f"{base.name}-{serial}")
    return candidate


def _free_archive_path(workspace_root: Path) -> Path:
    return _free_data_path(workspace_root, ARCHIVE_PREFIX)


@functools.cache
def _renameat2() -> Callable[..., int] | None:
    """libc 里的 renameat2。不是 Linux、或者 libc 老得没有这个函数，返回 None。"""
    if not sys.platform.startswith("linux"):
        return None
    try:
        func = ctypes.CDLL(None, use_errno=True).renameat2
    except (OSError, AttributeError):
        return None
    func.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    func.restype = ctypes.c_int
    return func


def _exchange(first: Path, second: Path) -> bool:
    """一次系统调用对调两个路径上的东西，中间没有哪一瞬两个名字缺一个。系统或文件系统不支持时返回 False，调用方退回两步改名。"""
    func = _renameat2()
    if func is None:
        return False
    if func(_AT_FDCWD, os.fsencode(first), _AT_FDCWD, os.fsencode(second), _RENAME_EXCHANGE) == 0:
        return True
    code = ctypes.get_errno()
    # 内核早于 3.15 是 ENOSYS；文件系统不认 RENAME_EXCHANGE 是 EINVAL，有的 FUSE 报 EOPNOTSUPP。
    if code in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
        return False
    raise OSError(code, os.strerror(code), str(first), None, str(second))


def _set_aside(workspace: Path, path: Path) -> Path:
    """切换 config/ 的空档里有进程（bot 写配置时会顺手建目录）新建了它：挪到 data/ 下，原来那份才放得回去。"""
    stray = _free_data_path(workspace, INTERRUPTED_PREFIX)
    stray.parent.mkdir(parents=True, exist_ok=True)
    os.rename(path, stray)
    _log.warning("切换 config/ 时有进程新建了 %s，已挪到 %s，确认没用后自行清理", path, stray)
    return stray


def _restore_config(workspace: Path, config: Path, saved: Path) -> None:
    """两步改名的第二步没成：把挪开的那份放回 config/。"""
    if config_layout.is_dir_link(config):
        _remove_link(config)
    elif _present(config):
        _set_aside(workspace, config)
    os.rename(saved, config)


def _archive_exchanged(config: Path, staged: Path, archive: Path) -> None:
    """对调之后旧目录在暂存名上，改名归档。归档不下去就再对调回来，等于什么都没发生。"""
    try:
        os.rename(staged, archive)
    except BaseException:
        try:
            _exchange(staged, config)
        except OSError as error:
            _log.error("归档 %s 失败，换回原来的 config/ 也失败了，旧配置留在 %s：%s", config, staged, error)
        raise


def _link_config(workspace_root: Path, target: Path) -> str:
    """把本实例的 config/ 换成指向 target 的链接，原来那份改名归档进 data/，返回归档到哪（相对工作区）。

    新链接先在旁边建好再对调上去：换的那一下 bot 可能正在读配置。不支持对调的系统退回两次改名，
    空档里别的进程新建的 config/ 在回滚时挪到一边。
    """
    root = Path(workspace_root)
    config = config_layout.config_dir(root)
    if config_layout.is_dir_link(config):
        raise ShareGroupError("本实例的 config/ 已经是一个链接，先退出现在的组")
    staged = root / f".config.{secrets.token_hex(6)}.link"
    _make_dir_link(staged, target)
    try:
        if not _present(config):
            os.replace(staged, config)
            return ""
        archive = _free_archive_path(root)
        archive.parent.mkdir(parents=True, exist_ok=True)
        if _exchange(staged, config):
            _archive_exchanged(config, staged, archive)
        else:
            os.rename(config, archive)
            try:
                os.replace(staged, config)
            except BaseException:
                _restore_config(root, config, archive)
                raise
        return archive.relative_to(root).as_posix()
    finally:
        # 链接还在暂存名上说明没换上去；换下来的旧目录不是链接，这里不会碰它。
        _remove_link(staged)


def _shareable_env(workspace_root: Path) -> dict[str, str]:
    """本实例 .env 里能跟着组走的键：端口、地址、平台令牌、路径这类实例身份不算。"""
    values = config_layout.read_env_file(Path(workspace_root) / ".env")
    return {key: value for key, value in values.items() if not is_instance_env(key)}


def create_group(workspace_root: Path, raw_name: Any) -> dict[str, Any]:
    """用本实例现在的配置建一个组，本实例随即加入。

    本实例 .env 里能共享的密钥一并抄进组的 secrets.env：组里别的实例要靠它们才调得通同一批工具。
    """
    name = _checked_name(raw_name)
    root = _shared_root()
    workspace = Path(workspace_root)
    row = _current_row(workspace)
    with _locked_groups(root):
        joined = config_layout.config_share_group(workspace)
        if joined is not None:
            raise ShareGroupError(f"本实例已经在共享组 {joined} 里，先退出再建新组")
        config_layout.check_config_dir(workspace)
        target = root / name
        if target.exists() or target.is_symlink():
            raise ShareGroupError(f"已经有叫 {name} 的共享组了")
        staging = root / f".{name}.{secrets.token_hex(6)}.tmp"
        staging_config = staging / config_layout.CONFIG_DIR
        try:
            source = config_layout.config_dir(workspace)
            if source.is_dir():
                shutil.copytree(source, staging_config, ignore=_COPY_IGNORE)
            else:
                staging_config.mkdir(parents=True)
            env = _shareable_env(workspace)
            if env:
                config_layout.edit_env_file(config_layout.shared_secrets_path(staging), env)
            meta = {
                "name": name,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "created_by": row["label"] if row else "",
            }
            # 暂存目录名是随机的，改名换上之前谁也看不见它，不必加锁。
            (staging / _GROUP_META).write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(staging, target)
        except BaseException:
            # 只收拾这一步自己刚建出来的暂存目录，别处的东西一概不碰。
            shutil.rmtree(staging, ignore_errors=True)
            raise
        try:
            archived = _link_config(workspace, target / config_layout.CONFIG_DIR)
        except BaseException:
            # 本实例没换过去就当这个组没建过：组目录是这一步刚放上去的，锁还在手里，别的实例来不及加入。
            with contextlib.suppress(OSError):
                os.replace(target, staging)
                shutil.rmtree(staging, ignore_errors=True)
            raise
    return {"group": name, "archived": archived, "copied_env": sorted(env)}


def join_group(workspace_root: Path, raw_name: Any) -> dict[str, Any]:
    """改用组里的配置。本实例原来那份改名归档，不删。"""
    name = _checked_name(raw_name)
    root = _shared_root()
    workspace = Path(workspace_root)
    with _locked_groups(root):
        joined = config_layout.config_share_group(workspace)
        if joined == name:
            raise ShareGroupError(f"本实例已经在共享组 {name} 里了")
        if joined is not None:
            raise ShareGroupError(f"本实例在共享组 {joined} 里，先退出再加入别的组")
        config_layout.check_config_dir(workspace)
        target = config_layout.share_group_config_dir(name)
        if not target.is_dir():
            raise ShareGroupError(f"没有叫 {name} 的共享组")
        archived = _link_config(workspace, target)
    return {"group": name, "archived": archived}


def leave_group(workspace_root: Path) -> dict[str, Any]:
    """退出现在的组：拷一份组里此刻的配置换成本实例自己的 config/，之后两边各改各的。"""
    root = _shared_root()
    workspace = Path(workspace_root)
    link = config_layout.config_dir(workspace)
    with _locked_groups(root):
        group = config_layout.config_share_group(workspace)
        if group is None:
            raise ShareGroupError("本实例不在任何共享组里")
        source = config_layout.share_group_config_dir(group)
        staged = workspace / f".config.{secrets.token_hex(6)}.leaving"
        try:
            shutil.copytree(source, staged, ignore=_COPY_IGNORE)
            exchanged = _exchange(staged, link)
        except BaseException:
            shutil.rmtree(staged, ignore_errors=True)
            raise
        if exchanged:
            # 对调下来的是指向组的链接，删它不碰组里的内容；删不掉也只是留下一个没人用的链接。
            with contextlib.suppress(OSError):
                _remove_link(staged)
            return {"group": group}
        try:
            _remove_link(link)
            os.rename(staged, link)
        except BaseException:
            _relink_config(workspace, link, source)
            shutil.rmtree(staged, ignore_errors=True)
            raise
    return {"group": group}


def _relink_config(workspace: Path, link: Path, source: Path) -> None:
    """退组没换成：config/ 换回指向组的链接。空档里别的进程新建的 config/ 先挪到一边。"""
    if config_layout.is_dir_link(link):
        return
    if _present(link):
        _set_aside(workspace, link)
    _make_dir_link(link, source)


def delete_group(workspace_root: Path, raw_name: Any) -> dict[str, Any]:
    """删一个没有成员的组。改名归档而不是删除：清单漏记的实例也许还链着它，改回名字就能救回来。"""
    name = _checked_name(raw_name)
    root = _shared_root()
    with _locked_groups(root):
        target = root / name
        if not (target / config_layout.CONFIG_DIR).is_dir():
            raise ShareGroupError(f"没有叫 {name} 的共享组")
        members = _members(Path(workspace_root)).get(name, [])
        if members:
            labels = "、".join(member["label"] for member in members)
            raise ShareGroupError(f"组里还有 {labels}，先让它们退出")
        archive = root / f"{name}.deleted-{_now_stamp()}"
        os.rename(target, archive)
    return {"group": name, "archived": archive.name}


__all__ = [
    "ARCHIVE_PREFIX",
    "INTERRUPTED_PREFIX",
    "ShareGroupError",
    "create_group",
    "delete_group",
    "join_group",
    "leave_group",
    "overview",
]
