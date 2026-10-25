"""本机来源的解析与受限读取：file:// URL 和本机路径，入站下载和出站附件共用这一份。"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote

logger = logging.getLogger(__name__)

FILE_SCHEME = "file://"

# 错误分类同时是入站附件给用户看的错误码。
LOCAL_DENIED = "local_denied"
LOCAL_UNAVAILABLE = "local_unavailable"
TOO_LARGE = "too_large"

_DRIVE_RE = re.compile(r"^[a-zA-Z]:[\\/]")
_SLASHED_DRIVE_RE = re.compile(r"^/[a-zA-Z]:/")
_LOCAL_HOSTS = frozenset({"", "localhost"})


class LocalSourceError(ValueError):
    """str(exc) 是错误码，和下载失败的错误码走同一套文案。"""


def is_local_source(source: str) -> bool:
    """file:// URL 或本机绝对路径（POSIX 或带盘符）。"""
    return source.startswith(FILE_SCHEME) or source.startswith("/") or bool(_DRIVE_RE.match(source))


def source_path(source: str) -> Path:
    """还原成本机路径，不查存在和范围。

    file:///abs、file://localhost/abs 和带盘符的写法是标准 URL，按百分号编码还原；
    file://data/attachments/x.png 是引擎记附件的写法，表示工作区相对路径，原样保留。
    """
    if not source.startswith(FILE_SCHEME):
        return Path(source)
    rest = source[len(FILE_SCHEME):]
    if _DRIVE_RE.match(rest):
        return Path(unquote(rest))
    host, slash, tail = rest.partition("/")
    if slash and host.lower() in _LOCAL_HOSTS:
        path = unquote("/" + tail)
        if os.name == "nt" and _SLASHED_DRIVE_RE.match(path):
            path = path[1:]
        return Path(path)
    return Path(rest)


def workspace_path(source: str, workspace: Path | str) -> Path:
    """出站附件的写法：相对路径落在工作区里，绝对路径原样。"""
    path = source_path(source)
    return path if path.is_absolute() else Path(workspace) / path


def resolve_inbound_source(source: str, *, roots: Iterable[Path | str]) -> Path:
    """入站事件指向的本机文件，只认 roots 之内的。

    路径由 OneBot 实现决定，不限范围就等于按对端内容读本机任意文件。文件不在算 local_unavailable
    （常见于 NapCat 在容器里、路径在 bot 这边不存在），在但越界算 local_denied。
    """
    try:
        path = source_path(source)
        # 相对路径和 file://别的主机 都不是这台机器上能确认的位置；只看 root，Windows 上 /a/b 也算有根。
        if not path.root:
            raise LocalSourceError(LOCAL_DENIED)
        resolved = path.resolve()
        exists = resolved.is_file()
    except LocalSourceError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise LocalSourceError(LOCAL_UNAVAILABLE) from exc
    if not exists:
        raise LocalSourceError(LOCAL_UNAVAILABLE)
    for root in roots:
        try:
            if resolved.is_relative_to(Path(root).resolve()):
                return resolved
        except (OSError, RuntimeError, ValueError):
            continue
    logger.warning(
        "refused local source outside the allowed roots: %s (set ONEBOT_LOCAL_SOURCE_ROOTS to allow this directory)",
        resolved,
    )
    raise LocalSourceError(LOCAL_DENIED)


def read_inbound_source(source: str, *, roots: Iterable[Path | str], max_bytes: int) -> bytes:
    """读入站本机文件，最多读 max_bytes + 1 字节判超限：stat 之后文件还可能被写大。"""
    path = resolve_inbound_source(source, roots=roots)
    try:
        if path.stat().st_size > max_bytes:
            raise LocalSourceError(TOO_LARGE)
        with path.open("rb") as stream:
            content = stream.read(max_bytes + 1)
    except LocalSourceError:
        raise
    except OSError as exc:
        raise LocalSourceError(LOCAL_UNAVAILABLE) from exc
    if len(content) > max_bytes:
        raise LocalSourceError(TOO_LARGE)
    return content
