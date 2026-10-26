"""文本文件落盘：已有的文件换行符和 UTF-8 BOM 跟原文件走，新文件照内容原样写；整份写回走同目录临时文件替换。

Path.write_text 会按平台转换换行：Windows 上 LF 文件整份变 CRLF，Linux 上 CRLF 文件整份变 LF。"""
from __future__ import annotations

import contextlib
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

LF = "\n"
CRLF = "\r\n"
BOM = "\ufeff"
_BOM_BYTES = BOM.encode("utf-8")
# Windows 上别的进程开着目标文件时 os.replace 报拒绝访问，多半是一瞬间的读，隔一会儿再试
_REPLACE_RETRY_DELAYS_SEC = (0.05, 0.1, 0.2, 0.4)


def newline_in(data: bytes) -> str:
    """原文用的换行符：出现过 CRLF 就算 CRLF，否则是 LF。"""
    return CRLF if b"\r\n" in data else LF


def newline_of(path: Path) -> str | None:
    """已有文件用的换行符：原文里出现过 CRLF 就算 CRLF，否则是 LF；文件还不存在是 None。"""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    return newline_in(data)


@dataclass(frozen=True)
class TextLayout:
    """一份文件落盘的样子：换行符、开头有没有 BOM。两项都是 None 表示新文件，内容照原样写。"""

    newline: str | None
    bom: bool | None

    @classmethod
    def of_bytes(cls, data: bytes) -> "TextLayout":
        return cls(newline_in(data), data.startswith(_BOM_BYTES))


NEW_FILE = TextLayout(None, None)


def layout_of(path: Path) -> TextLayout:
    try:
        return TextLayout.of_bytes(path.read_bytes())
    except FileNotFoundError:
        return NEW_FILE


def decode_text(data: bytes) -> str:
    """严格按 UTF-8 解码并去掉开头的 BOM；不是 UTF-8 时抛 UnicodeDecodeError。"""
    if data.startswith(_BOM_BYTES):
        data = data[len(_BOM_BYTES):]
    return data.decode("utf-8")


def normalize_newlines(text: str) -> str:
    """CRLF 统一成 LF。孤立的 \\r 是正文里的控制字符，不是行尾，原样留着。"""
    return text.replace(CRLF, LF)


def split_lines(text: str) -> list[str]:
    """按 \\n 分行，结尾的换行不多算一行。text 须先过 normalize_newlines；行号口径和 apply_diff 数 \\n 的一致。"""
    lines = text.split(LF)
    if lines[-1] == "":
        lines.pop()
    return lines


def encode_as(content: str, newline: str | None) -> bytes:
    """按给定的换行符编码成 UTF-8；newline 为 None 时照内容原样。"""
    text = content
    if newline is not None:
        text = content.replace(CRLF, LF)
        if newline != LF:
            text = text.replace(LF, newline)
    return text.encode("utf-8")


def encode_text(content: str, layout: TextLayout) -> bytes:
    """按 layout 编码：已有文件的换行符和 BOM 照旧，内容自带的 BOM 不会叠出第二个；新文件照内容原样。"""
    if layout.bom is None:
        return encode_as(content, layout.newline)
    body = content[len(BOM):] if content.startswith(BOM) else content
    data = encode_as(body, layout.newline)
    return _BOM_BYTES + data if layout.bom else data


def _fsync_dir(directory: Path) -> None:
    if os.name == "nt":
        return
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _replace(source: Path, target: Path) -> None:
    for delay in _REPLACE_RETRY_DELAYS_SEC:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if os.name != "nt":
                raise
            time.sleep(delay)
    os.replace(source, target)


def replace_bytes(path: Path, data: bytes) -> int:
    """把 path 整份换成 data，返回落盘的字节数。目录须已存在。

    同目录临时文件写完 fsync 再 os.replace：读的人要么看到旧的整份，要么看到新的整份。
    已有文件的权限照旧；path 是软链时换的是它指向的文件，链接本身不动。
    """
    target = Path(os.path.realpath(path))
    mode: int | None = None
    with contextlib.suppress(OSError):
        mode = target.stat().st_mode & 0o7777
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    try:
        # 建出来就是原文件的权限：先按 umask 建再 chmod 的话，中间那一瞬同机别的用户读得到内容。
        fd = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o666 if mode is None else mode,
        )
        # 在写句柄上 fsync：Windows 对只读句柄做 fsync 会报 EBADF。
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            with contextlib.suppress(OSError):
                os.chmod(tmp, mode)
        _replace(tmp, target)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
    _fsync_dir(target.parent)
    return len(data)


def write_text_as(path: Path, content: str, newline: str | None) -> int:
    """按给定的换行符原子地写 UTF-8 文本，返回落盘的字节数；newline 为 None 时照内容原样写。"""
    return replace_bytes(path, encode_as(content, newline))


def rewrite_text(path: Path, content: str) -> int:
    """整份原子写回，返回落盘的字节数：已有的文件换行符和 BOM 跟原文件走，新文件照内容原样写。"""
    return replace_bytes(path, encode_text(content, layout_of(path)))
