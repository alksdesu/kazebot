"""工作区根目录解析。

代码目录与数据目录解耦：同一份代码可被多个实例以不同 workspace 启动。
"""
from __future__ import annotations

import os
from pathlib import Path

# 与 adapters/onebot/config.py、tools/qq_forward.py 认同一组变量名：
# 值不一致时附件、记忆、admin token 会静默落到两个地方。
_ENV_NAMES = ("CLONOTH_WORKSPACE", "ONEBOT_WORKSPACE_ROOT")


def resolve_workspace_root(fallback: Path | str) -> Path:
    """按 env 解析工作区根；没设就用调用方给的回落值。

    回落值是各调用方原本硬编码的代码目录，所以不设 env 时行为与改造前逐字相同。
    """
    for name in _ENV_NAMES:
        raw = str(os.environ.get(name) or "").strip()
        if raw:
            return Path(raw).expanduser().resolve()
    return Path(fallback).resolve()
