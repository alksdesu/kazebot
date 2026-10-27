"""工具重载要盯的两份指纹：本实例的工具目录 config/tools，和 config/mcp.yaml。

共享组里别的实例、控制台、手改都不会通知这边，engine 靠 reload-seq 变了才跟上。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import config_layout
from real_paths import real_path
from toolbox import mcp_runtime

# engine 每一轮循环都来问 reload-seq，扫目录、看文件的频率得单独压住。
_MIN_INTERVAL_SEC = 2.0


class _Watch:
    def __init__(self, workspace_root: Path, *, min_interval: float = _MIN_INTERVAL_SEC) -> None:
        self._root = Path(workspace_root)
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._checked_at = time.monotonic()
        self._signature = self._scan()

    def _scan(self) -> Any:
        raise NotImplementedError

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


class ToolTreeWatch(_Watch):
    def _scan(self) -> tuple[str, tuple[tuple[str, int, int, int], ...]]:
        """目录解析到哪（换了共享组也算变化），加上每个脚本的相对路径和 file_stamp。"""
        base = config_layout.custom_tools_dir(self._root)
        try:
            where = str(real_path(base)) if base.is_dir() else ""
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


class McpConfigWatch(_Watch):
    def _scan(self) -> tuple[str, tuple[int, int, int] | None]:
        """文件解析到哪（换了共享组也算变化），加上它的 file_stamp。"""
        path = config_layout.mcp_path(self._root)
        try:
            where = str(real_path(path))
        except OSError:
            where = ""
        # 每一版都过一遍本进程的读取：写坏时诊断说「沿用上一份」还是「按没有客户端算」，看的是这个进程读成过没有
        mcp_runtime.config_error(self._root)
        return where, config_layout.file_stamp(path)
