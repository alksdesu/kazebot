"""文本文件落盘时的换行符：已有的文件跟原文件走，新文件照内容原样写。

Path.write_text 会按平台转换换行：Windows 上 LF 文件整份变 CRLF，Linux 上 CRLF 文件整份变 LF。"""
from __future__ import annotations

from pathlib import Path

LF = "\n"
CRLF = "\r\n"


def newline_of(path: Path) -> str | None:
    """已有文件用的换行符：原文里出现过 CRLF 就算 CRLF，否则是 LF；文件还不存在是 None。"""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    return CRLF if b"\r\n" in data else LF


def write_text_as(path: Path, content: str, newline: str | None) -> int:
    """按给定的换行符写 UTF-8 文本，返回落盘的字节数；newline 为 None 时照内容原样写。"""
    text = content
    if newline is not None:
        text = content.replace(CRLF, LF)
        if newline != LF:
            text = text.replace(LF, newline)
    data = text.encode("utf-8")
    path.write_bytes(data)
    return len(data)


def rewrite_text(path: Path, content: str) -> int:
    """整份写回，返回落盘的字节数：已有的文件换行符跟原文件走，新文件照内容原样写。"""
    return write_text_as(path, content, newline_of(path))
