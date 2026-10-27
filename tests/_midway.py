"""在 Windows 解析路径的半路上，像另一个进程那样建出、删掉目录或文件。

CPython 的 resolve 去掉扩展长度前缀之前会再查一次，两次之间磁盘变了就把前缀留下；钩住这一步就能确定性地复现，用不着起多进程碰运气。
"""
from __future__ import annotations

import ntpath
import os
from pathlib import Path
from typing import Callable

import pytest

EXTENDED_PREFIX = "\\\\?\\"

windows_only = pytest.mark.skipif(os.name != "nt", reason="只有 Windows 上的 resolve 会因为磁盘中途变了换一种写法")


def change_midway(monkeypatch: pytest.MonkeyPatch, watched: Path, change: Callable[[], None]) -> list[Path]:
    """头一回向系统要 watched 的最终路径之后，像另一个进程那样动一下磁盘。返回的列表记着动过没有。"""
    lookup = ntpath._getfinalpathname
    fired: list[Path] = []

    def hooked(path):
        try:
            return lookup(path)
        finally:
            if not fired and path == str(watched):
                fired.append(watched)
                change()

    monkeypatch.setattr(ntpath, "_getfinalpathname", hooked)
    return fired
