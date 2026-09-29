"""manage_secret — inspect and set secrets one key at a time.

Values never leave this module: the supervisor only sees the virtual path ``.secret/<NAME>``.
Shareable keys live in config/secrets.env and travel with a share group; instance identity stays in .env.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import config_layout
from platforms.protection import is_instance_env

from ..context import ToolContext
from .._common import request_guard

_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
# classify_path 会规范化路径，`secret://X` 会被折叠成 `secret:/X` 让规则匹配不上。
_VIRTUAL_ROOT = ".secret"
_INSTANCE_FILE = ".env"


def _error(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, "data": {"result": "ERROR: " + message}, **extra}


def _keys_in(path: Path) -> list[str]:
    """文件里赋过值的键，按出现顺序去重。空值也算：list 报的是「写过」，不管值能不能用。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    names: list[str] = []
    for line in lines:
        key = config_layout.env_line_key(line)
        if key and key not in names:
            names.append(key)
    return names


def _clean_value(raw: Any) -> tuple[str, str]:
    value = str(raw if raw is not None else "").strip()
    # 控制字符能在 .env 里另起一行，等于绕开单键限制往文件里写任意内容。
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return "", "value must not contain control characters or line breaks"
    return value, ""


def _guard_error(err: dict[str, Any] | Any) -> dict[str, Any]:
    text = str(err.get("error", "denied")) if isinstance(err, dict) else str(err)
    cancelled = bool(isinstance(err, dict) and err.get("cancelled"))
    return _error(text, cancelled=cancelled)


async def _list(instance_file: Path, shared_file: Path, ctx: ToolContext) -> dict[str, Any]:
    _op, err = await request_guard(ctx, "read_file", {"path": _VIRTUAL_ROOT})
    if err is not None:
        return _guard_error(err)

    own = _keys_in(instance_file)
    shared = _keys_in(shared_file)
    names = own + [name for name in shared if name not in own]
    lines: list[str] = []
    for name in names:
        if name in own and name in shared:
            where = "本实例 .env，盖过共享的同名项"
        elif name in own:
            where = "本实例 .env"
        else:
            where = "共享 " + config_layout.SHARED_SECRETS_FILE
        lines.append(name + " — 已配置（" + where + "）")
    return {
        "ok": True,
        "data": {
            "result": "\n".join(lines) or "尚未配置任何密钥。",
            "names": names,
            "instance": own,
            "shared": shared,
            "count": len(names),
        },
    }


def _unset(instance_file: Path, shared_file: Path, name: str) -> dict[str, Any]:
    # 两边都删：只删一边的话，另一边那份会接着生效，看上去像没删掉。
    removed_own = config_layout.edit_env_file(instance_file, {name: None})
    removed_shared = config_layout.edit_env_file(shared_file, {name: None})
    if not (removed_own or removed_shared):
        return {"ok": True, "data": {"result": name + " 本来就没有配置，未改动。", "name": name}}
    where = "、".join(
        label for label, removed in (("本实例 .env", removed_own), (config_layout.SHARED_SECRETS_FILE, removed_shared))
        if removed
    )
    return {"ok": True, "data": {"result": name + " 已从" + where + "删除。", "name": name}}


def _set(instance_file: Path, shared_file: Path, name: str, raw_value: Any) -> dict[str, Any]:
    value, value_error = _clean_value(raw_value)
    if value_error:
        return _error(value_error)
    if not value:
        return _error("value must not be empty; use action=unset to remove a key")

    if is_instance_env(name):
        # 端口、地址、平台令牌、路径是这个实例自己的，写进共享文件就会被组里别的实例拿去用。
        created = name not in _keys_in(instance_file)
        config_layout.edit_env_file(instance_file, {name: value})
        return {
            "ok": True,
            "data": {
                "result": name + (" 已写入" if created else " 已更新") + "本实例 .env（实例身份，不跟共享组走）。"
                + "进程在启动时读 .env，需 request_restart 后才会用上。",
                "name": name,
                "created": created,
                "target": _INSTANCE_FILE,
            },
        }

    created = name not in _keys_in(shared_file)
    config_layout.edit_env_file(shared_file, {name: value})
    shadowed = name in _keys_in(instance_file)
    if shadowed:
        # .env 的同名项优先，不跟着改的话本实例还在用旧值。
        config_layout.edit_env_file(instance_file, {name: value})
    result = name + (" 已写入" if created else " 已更新") + "共享密钥文件 " + config_layout.SHARED_SECRETS_FILE + "，"
    if shadowed:
        result += "本实例 .env 里的同名项一并改了。搜索等工具下次调用即生效；主渠道模型密钥需 request_restart 后才会重新加载。"
    else:
        result += "搜索等工具和渠道配置里的 ${" + name + "} 下次取值即生效，共享组里的实例都会用上。"
    return {
        "ok": True,
        "data": {
            "result": result,
            "name": name,
            "created": created,
            "target": config_layout.SHARED_SECRETS_FILE,
            "instance_updated": shadowed,
        },
    }


async def manage_secret(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    action = str(args.get("action") or "").strip().lower()
    if action not in {"list", "set", "unset"}:
        return _error("action must be one of: list, set, unset")

    workspace = Path(ctx.workspace_root)
    instance_file = workspace / _INSTANCE_FILE
    shared_file = config_layout.shared_secrets_path(workspace)
    if action == "list":
        return await _list(instance_file, shared_file, ctx)

    name = str(args.get("name") or "").strip()
    if not _NAME_RE.match(name):
        return _error("name must match ^[A-Z][A-Z0-9_]{0,63}$, got: " + (name[:40] or "(empty)"))

    _op, err = await request_guard(ctx, "write_file", {"path": _VIRTUAL_ROOT + "/" + name})
    if err is not None:
        return _guard_error(err)

    try:
        if action == "unset":
            return _unset(instance_file, shared_file, name)
        return _set(instance_file, shared_file, name, args.get("value"))
    except OSError as exc:
        return _error("failed to update secrets: " + str(exc))
