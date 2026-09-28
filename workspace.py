"""工作区根目录解析。

代码目录与数据目录解耦：同一份代码可被多个实例以不同 workspace 启动。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

# 与 adapters/onebot/config.py、tools/qq_forward.py 认同一组变量名：
# 值不一致时附件、记忆、admin token 会静默落到两个地方。
_ENV_NAMES = ("CLONOTH_WORKSPACE", "ONEBOT_WORKSPACE_ROOT")

# 实例配置写错了（sysexits 的 EX_CONFIG），重启也起不来：systemd 单元据此停住等人，不反复拉起刷日志。
CONFIG_ERROR_EXIT_CODE = 78


def workspace_env_root() -> Path | None:
    """env 里设的工作区根；都没设返回 None。"""
    for name in _ENV_NAMES:
        raw = str(os.environ.get(name) or "").strip()
        if raw:
            return Path(raw).expanduser().resolve()
    return None


def pin_workspace_env(root: Path | str) -> None:
    """把这一组变量钉回 root：本进程后来再按 env 解析的地方和它拉起的子进程，认的都是这一个根。

    子进程的 cwd 是工作区，钉相对路径会在那边解析到别处，所以钉解析后的绝对路径。
    """
    value = str(Path(root).expanduser().resolve())
    primary, *aliases = _ENV_NAMES
    os.environ[primary] = value
    for name in aliases:
        # 没设过的别名不补：主名排在前面，补了也不改变谁认到哪。
        if name in os.environ:
            os.environ[name] = value


def resolve_workspace_root(fallback: Path | str) -> Path:
    """按 env 解析工作区根；没设就用调用方给的回落值。

    回落值是各调用方原本硬编码的代码目录，所以不设 env 时行为与改造前逐字相同。
    """
    configured = workspace_env_root()
    return configured if configured is not None else Path(fallback).resolve()


def workspace_instance(workspace_root: Path | str) -> str:
    """工作区的实例号：功能记录没有机器人账号时拿它顶替，用户身份的摘要也拌进它，多开的实例互不相认。"""
    return hashlib.sha256(str(Path(workspace_root).resolve()).encode("utf-8")).hexdigest()[:24]
