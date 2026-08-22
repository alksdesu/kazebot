"""日志文件的只读访问。出站前脱敏，路径锁死在 data/logs 下。

不指望日志本身干净：写日志的地方太多，漏一处就是明文密钥进浏览器。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# 单次最多回多少行、多少字节。cleanup.log 已经 277KB 并且还在长。
MAX_LINES = 2000
MAX_BYTES = 512 * 1024

_KEEP_TAIL = 4

# 各家密钥的前缀。命中就整段打码，只留尾巴几位供人对号。
_TOKEN_PATTERNS = (
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,})"),
    re.compile(r"\b(AIza[A-Za-z0-9_\-]{10,})"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{10,})"),
    re.compile(r"\b(xox[baprs]-[A-Za-z0-9\-]{10,})"),
)

# 键值对写法。键名认得出就不管值长什么样 —— 中转站的 key 可以是任意串。
_ASSIGN_PATTERN = re.compile(
    r"((?:api[_-]?key|apikey|token|secret|password|passwd|authorization|bearer)"
    r"\s*[=:]\s*)(['\"]?)([^\s'\",;)]{6,})",
    re.IGNORECASE,
)

# URL 里带的密钥，Gemini 那套 ?key= 就是这么传的。
_QUERY_PATTERN = re.compile(
    r"([?&](?:key|api[_-]?key|access[_-]?token)=)([^\s&'\"]{6,})",
    re.IGNORECASE,
)


def _mask(value: str) -> str:
    if len(value) <= _KEEP_TAIL:
        return "*" * len(value)
    return "*" * 6 + value[-_KEEP_TAIL:]


def redact(text: str) -> str:
    """把看得出是密钥的东西打码。宁可多打，看日志的人不需要那几位。"""
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(lambda m: _mask(m.group(1)), text)
    text = _ASSIGN_PATTERN.sub(lambda m: m.group(1) + m.group(2) + _mask(m.group(3)), text)
    text = _QUERY_PATTERN.sub(lambda m: m.group(1) + _mask(m.group(2)), text)
    return text


@dataclass(frozen=True)
class LogFile:
    name: str
    size: int
    modified: float


class LogReader:
    """只读 data/logs 下的 *.log。"""

    def __init__(self, log_dir: Path) -> None:
        self.log_dir = Path(log_dir)

    def list(self) -> list[LogFile]:
        if not self.log_dir.is_dir():
            return []
        rows = []
        for path in self.log_dir.glob("*.log"):
            if not path.is_file():
                continue
            stat = path.stat()
            rows.append(LogFile(name=path.name, size=stat.st_size, modified=stat.st_mtime))
        return sorted(rows, key=lambda row: row.modified, reverse=True)

    def _resolve(self, name: str) -> Path | None:
        """名字解析成路径。越界或不是 .log 一律返回 None。

        只查字符串挡不住 symlink 和 ..：拿 resolve() 之后的真实路径比父目录才作数。
        """
        if not name or "/" in name or "\\" in name or not name.endswith(".log"):
            return None
        try:
            root = self.log_dir.resolve(strict=True)
            path = (root / name).resolve(strict=True)
        except OSError:
            return None
        if root not in path.parents or not path.is_file():
            return None
        return path

    def tail(self, name: str, lines: int = 500) -> tuple[str, bool] | None:
        """读尾部若干行，返回（脱敏后的文本, 是否被截断）。找不到返回 None。"""
        path = self._resolve(name)
        if path is None:
            return None
        want = max(1, min(int(lines or 500), MAX_LINES))
        try:
            with path.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                start = max(0, size - MAX_BYTES)
                handle.seek(start)
                raw = handle.read()
        except OSError:
            return None
        text = raw.decode("utf-8", errors="replace")
        # 从中间切进去的话第一行多半是半截，扔掉。
        if start > 0:
            text = text.split("\n", 1)[-1]
        rows = text.splitlines()
        truncated = start > 0 or len(rows) > want
        return redact("\n".join(rows[-want:])), truncated
