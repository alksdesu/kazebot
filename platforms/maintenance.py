"""平台定期维护的约定：数据清理跑到平台这一步时，把一个上下文交给平台声明的 maintenance 钩子。

钩子模块提供 maintenance_tasks()，返回要跑的几件事；删文件一律经上下文，演练模式才看得清会删什么。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


class MaintenanceContext(Protocol):
    @property
    def workspace_root(self) -> Path: ...

    # 就是 workspace_root / "data"，给出来省得各平台自己拼。
    @property
    def data_dir(self) -> Path: ...

    @property
    def dry_run(self) -> bool: ...

    def remove_file(self, path: Path) -> None:
        """删一个文件；演练模式只记日志。"""

    def remove_empty_dir(self, path: Path) -> None:
        """删一个空目录；演练模式只记日志。"""

    def purge_dir(self, directory: Path, max_age: float, label: str, *, recursive: bool = False) -> None:
        """删 directory 下超过 max_age 秒没改过的文件；recursive 时连子目录一起，顺带删掉空了的子目录。"""


@dataclass(frozen=True)
class MaintenanceTask:
    # 写进清理日志的名字，出错时也按它报。
    name: str
    run: Callable[[MaintenanceContext], None]
