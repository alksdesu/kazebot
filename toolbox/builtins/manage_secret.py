"""manage_secret — inspect and set .env secrets one key at a time.

Secret values never leave this module: only the key name reaches the supervisor,
as the virtual path ``.secret/<NAME>``, so approval cards and the event log
cannot leak them. Nothing is ever written to that path — the real target is .env.
"""
from __future__ import annotations

import contextlib
import os
import re
from pathlib import Path
from typing import Any

from ..context import ToolContext
from .._common import request_guard

_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
# classify_path 会规范化路径，`secret://X` 会被折叠成 `secret:/X` 让规则匹配不上。
_VIRTUAL_ROOT = ".secret"
_ASSIGN_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
_NEW_FILE_MODE = 0o600


def _error(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, "data": {"result": "ERROR: " + message}, **extra}


def _read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _key_of(line: str) -> str:
    match = _ASSIGN_RE.match(line)
    return match.group(1) if match else ""


def _clean_value(raw: Any) -> tuple[str, str]:
    value = str(raw if raw is not None else "").strip()
    # 控制字符能在 .env 里另起一行，等于绕开单键限制往文件里写任意内容。
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return "", "value must not contain control characters or line breaks"
    return value, ""


def _atomic_write(path: Path, lines: list[str]) -> None:
    text = "\n".join(lines)
    if text:
        text += "\n"
    mode = _NEW_FILE_MODE
    with contextlib.suppress(OSError):
        mode = path.stat().st_mode & 0o777
    tmp = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _guard_error(err: dict[str, Any] | Any) -> dict[str, Any]:
    text = str(err.get("error", "denied")) if isinstance(err, dict) else str(err)
    cancelled = bool(isinstance(err, dict) and err.get("cancelled"))
    return _error(text, cancelled=cancelled)


async def _list(path: Path, ctx: ToolContext) -> dict[str, Any]:
    _op, err = await request_guard(ctx, "read_file", {"path": _VIRTUAL_ROOT})
    if err is not None:
        return _guard_error(err)

    names: list[str] = []
    for line in _read_lines(path):
        key = _key_of(line)
        if key and key not in names:
            names.append(key)

    listing = "\n".join(name + " — 已配置" for name in names)
    return {
        "ok": True,
        "data": {
            "result": listing or "尚未配置任何密钥。",
            "names": names,
            "count": len(names),
        },
    }


async def _unset(path: Path, name: str) -> dict[str, Any]:
    lines = _read_lines(path)
    kept = [line for line in lines if _key_of(line) != name]
    if len(kept) == len(lines):
        return {"ok": True, "data": {"result": name + " 本来就没有配置，未改动。", "name": name}}
    _atomic_write(path, kept)
    return {"ok": True, "data": {"result": name + " 已删除。", "name": name}}


async def _set(path: Path, name: str, raw_value: Any) -> dict[str, Any]:
    value, value_error = _clean_value(raw_value)
    if value_error:
        return _error(value_error)
    if not value:
        return _error("value must not be empty; use action=unset to remove a key")

    entry = name + "=" + value
    lines = _read_lines(path)
    updated: list[str] = []
    replaced = False
    for line in lines:
        if _key_of(line) != name:
            updated.append(line)
        elif not replaced:
            updated.append(entry)
            replaced = True
    if not replaced:
        updated.append(entry)
    _atomic_write(path, updated)

    return {
        "ok": True,
        "data": {
            "result": (
                name + (" 已更新。" if replaced else " 已写入。")
                + "搜索等工具进程下次调用即生效；主渠道模型密钥需 request_restart 后才会重新加载。"
            ),
            "name": name,
            "created": not replaced,
        },
    }


async def manage_secret(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    action = str(args.get("action") or "").strip().lower()
    if action not in {"list", "set", "unset"}:
        return _error("action must be one of: list, set, unset")

    path = Path(ctx.workspace_root) / ".env"
    if action == "list":
        return await _list(path, ctx)

    name = str(args.get("name") or "").strip()
    if not _NAME_RE.match(name):
        return _error("name must match ^[A-Z][A-Z0-9_]{0,63}$, got: " + (name[:40] or "(empty)"))

    _op, err = await request_guard(ctx, "write_file", {"path": _VIRTUAL_ROOT + "/" + name})
    if err is not None:
        return _guard_error(err)

    try:
        if action == "unset":
            return await _unset(path, name)
        return await _set(path, name, args.get("value"))
    except OSError as exc:
        return _error("failed to update .env: " + str(exc))
