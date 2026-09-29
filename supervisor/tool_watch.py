"""本实例工具目录 config/tools 的指纹。共享组里别的实例加了、改了工具，这边的 engine 要靠 reload-seq 变了才会重载。"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import config_layout

# engine 每一轮循环都来问 reload-seq，扫目录的频率得单独压住。
_MIN_INTERVAL_SEC = 2.0


class ToolTreeWatch:
    def __init__(self, workspace_root: Path, *, min_interval: float = _MIN_INTERVAL_SEC) -> None:
        self._root = Path(workspace_root)
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._checked_at = time.monotonic()
        self._signature = self._scan()

    def _scan(self) -> tuple[str, tuple[tuple[str, int, int, int], ...]]:
        """目录解析到哪（换了共享组也算变化），加上每个脚本的相对路径和 file_stamp。"""
        base = config_layout.custom_tools_dir(self._root)
        try:
            where = str(base.resolve()) if base.is_dir() else ""
        except OSError:
            where = ""
        entries: list[tuple[str, int, int, int]] = []
        if where:
            try:
                for path in sorted(base.rglob("*.py")):
                    if "__pycache__" in path.parts:
                        continue
                    stamp = config_layout.file_stamp(path)
                    if stamp is not None:
                        entries.append((path.relative_to(base).as_posix(), *stamp))
            except OSError:
                pass
        return where, tuple(entries)

    def changed(self) -> bool:
        """离上次扫过不到 min_interval 秒就直接说没变；扫出来的指纹和上次不一样返回 True。"""
        now = time.monotonic()
        with self._lock:
            if now - self._checked_at < self._min_interval:
                return False
            self._checked_at = now
            signature = self._scan()
            if signature == self._signature:
                return False
            self._signature = signature
            return True
