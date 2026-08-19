"""在没有 discord.py 的环境里加载 Discord 适配层的测试夹具。

stub 只覆盖适配层实际触碰的接口。共享一份，避免多个测试文件各存一套 stub 后
先跑的那个把后跑的那个饿死。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType


def install_discord_stub() -> ModuleType:
    """装一份够适配层 import 的 discord 替身；已经装过就复用同一份。"""
    existing = sys.modules.get("discord")
    if existing is not None:
        return existing

    stub = ModuleType("discord")

    class _HTTPException(RuntimeError):
        status = 0

    class _File:
        def __init__(self, path: str, *, filename: str = "") -> None:
            self.fp = open(path, "rb")
            self.filename = filename or Path(path).name

    stub.HTTPException = _HTTPException
    stub.NotFound = type("NotFound", (_HTTPException,), {})
    stub.Forbidden = type("Forbidden", (_HTTPException,), {})
    stub.File = _File
    stub.Message = object
    stub.Member = object
    stub.User = object
    stub.Guild = object
    sys.modules["discord"] = stub
    return stub
