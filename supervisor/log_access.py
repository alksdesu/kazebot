"""日志文件的只读访问。出站前脱敏，路径锁死在 data/logs 下。

不指望日志本身干净：写日志的地方太多，漏一处就是明文密钥进浏览器。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from real_paths import real_path
from toolbox.redaction import redact

# 单次最多回多少行、多少字节。cleanup.log 已经 277KB 并且还在长。
MAX_LINES = 2000
MAX_BYTES = 512 * 1024


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
            root = real_path(self.log_dir, strict=True)
            path = real_path(root / name, strict=True)
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
