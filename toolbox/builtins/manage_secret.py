"""manage_secret — inspect and set secrets one key at a time.

Values never leave this module: the supervisor only sees the virtual path ``.secret/<NAME>``.
Shareable keys live in config/secrets.env and travel with a share group; instance identity stays in .env;
names only the process environment serves are refused (platforms/protection.py decides which is which).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import config_layout
from platforms.protection import (
    BASIS_INJECTED,
    BASIS_INTERPRETER,
    BASIS_PROXY,
    ENV_INSTANCE,
    ENV_PROCESS,
    EnvScope,
    env_scope,
)
from platforms.registry import find_platform

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


def _platform_label(scope: EnvScope) -> str:
    platform = find_platform(scope.platform) if scope.platform else None
    return platform.short_name if platform is not None else scope.platform


def _process_reason(scope: EnvScope) -> str:
    """只认进程环境的名字是谁、怎么读的。"""
    if scope.basis == BASIS_INJECTED:
        return "是进程拉起子进程、调工具时现填进去的运行时上下文"
    if scope.basis == BASIS_PROXY:
        return "是网络库从进程环境读的代理设置，config/secrets.env 服务不了它"
    if scope.basis == BASIS_INTERPRETER:
        return "是 Python 解释器自己读的设置"
    if scope.platform:
        return f"是 {_platform_label(scope)} 这一侧直接从进程环境读的设置，config/secrets.env 服务不了它"
    return "是 Clonoth 进程直接从进程环境读的设置，config/secrets.env 服务不了它"


def _process_advice(scope: EnvScope) -> str:
    """只认进程环境的名字该去哪改。"""
    if scope.basis == BASIS_INJECTED:
        return "它不该写进任何文件。"
    if scope.basis == BASIS_PROXY:
        return "模型请求连同密钥头走哪条路由它决定，请管理员在部署配置（systemd 单元或本实例 .env）里改。"
    if scope.basis == BASIS_INTERPRETER:
        return "请管理员在部署配置里改。"
    if scope.platform:
        return (
            "控制台平台页里有的选项到那里改，即时生效；其余的请管理员改本实例 .env，再重启读它的进程"
            f"（engine 用 request_restart，{_platform_label(scope)} 适配进程要另外重启）。"
        )
    return "要改请管理员改本实例 .env，再 request_restart target=all。"


def _process_refusal(
    action: str, name: str, scope: EnvScope, instance_file: Path, shared_file: Path,
) -> dict[str, Any] | None:
    """只认进程环境的名字：set 一律拒；unset 只清共享文件里不起作用的那一行，只在 .env 里的拒。问审批之前判，文件一个不碰。"""
    if action == "set":
        return _error(f"{name} {_process_reason(scope)}，没有写入。{_process_advice(scope)}")
    if name not in _keys_in(shared_file) and name in _keys_in(instance_file):
        return _error(f"{name} 只在本实例 .env 里，它{_process_reason(scope)}，manage_secret 不改它。{_process_advice(scope)}")
    return None


def _restart_hint(scope: EnvScope) -> str:
    """本实例 .env 里的实例身份改了什么时候生效。进程只在启动时灌 .env，request_restart 只重启得了 supervisor 和 engine。"""
    if scope.platform:
        return (
            "读它的进程重启后才会生效：supervisor、engine 用 request_restart target=all，"
            f"{_platform_label(scope)} 适配进程要另外重启，request_restart 管不到；"
            "systemd 单元里也设了同名变量时，适配进程以单元里的为准。"
        )
    return "读它的进程重启后才会生效：supervisor、engine 用 request_restart target=all，适配进程读它的话要另外重启。"


def _misplaced_note(scope: EnvScope) -> str:
    """共享文件里写着、但共享文件不该收的名字。"""
    if scope.kind == ENV_PROCESS:
        return "，这一行不起作用：它只认进程环境"
    if scope.kind == ENV_INSTANCE:
        return "，实例身份只该写在本实例 .env"
    return ""


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
        if name in shared:
            where += _misplaced_note(env_scope(name))
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


def _unset(instance_file: Path, shared_file: Path, name: str, scope: EnvScope) -> dict[str, Any]:
    if scope.kind == ENV_PROCESS:
        # .env 里那份是进程环境的正当来源，不归这里删；共享文件里那一行本来就不起作用。
        removed_shared = config_layout.edit_env_file(shared_file, {name: None})
        if not removed_shared:
            return {"ok": True, "data": {"result": name + " 本来就没有配置，未改动。", "name": name}}
        result = f"{name} 已从{config_layout.SHARED_SECRETS_FILE}删除，那一行本来就不起作用：它{_process_reason(scope)}。"
        if name in _keys_in(instance_file):
            result += "本实例 .env 里的同名项没动。" + _process_advice(scope)
        return {"ok": True, "data": {"result": result, "name": name}}

    # 两边都删：只删一边的话，另一边那份会接着生效，看上去像没删掉。
    removed_own = config_layout.edit_env_file(instance_file, {name: None})
    removed_shared = config_layout.edit_env_file(shared_file, {name: None})
    if not (removed_own or removed_shared):
        return {"ok": True, "data": {"result": name + " 本来就没有配置，未改动。", "name": name}}
    where = "、".join(
        label for label, removed in (("本实例 .env", removed_own), (config_layout.SHARED_SECRETS_FILE, removed_shared))
        if removed
    )
    result = name + " 已从" + where + "删除。"
    if removed_own and scope.kind == ENV_INSTANCE:
        result += _restart_hint(scope)
    elif removed_own:
        # target=engine 重灌 .env 只会补上和改掉，删掉的那项还留在 supervisor 的环境里，engine 照样继承
        result += "进程环境里还是启动时灌进去的那份，request_restart target=all 之后各处才都不再用它。"
    return {"ok": True, "data": {"result": result, "name": name}}


def _set(instance_file: Path, shared_file: Path, name: str, raw_value: Any, scope: EnvScope) -> dict[str, Any]:
    value, value_error = _clean_value(raw_value)
    if value_error:
        return _error(value_error)
    if not value:
        return _error("value must not be empty; use action=unset to remove a key")

    if scope.kind == ENV_INSTANCE:
        # 端口、地址、平台令牌、路径是这个实例自己的，写进共享文件就会被组里别的实例拿去用。
        created = name not in _keys_in(instance_file)
        config_layout.edit_env_file(instance_file, {name: value})
        return {
            "ok": True,
            "data": {
                "result": name + (" 已写入" if created else " 已更新") + "本实例 .env（实例身份，不跟共享组走）。"
                + _restart_hint(scope),
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
        # 先查进程环境的读法拿到的是启动时灌进去的旧值；target=engine 会先让 supervisor 重灌 .env 再拉起 engine。
        result += "本实例 .env 里的同名项一并改了。.env 只在进程启动时灌进环境，request_restart target=engine 之后各处才都用上新值。"
    else:
        result += "搜索等工具和渠道配置里的 ${" + name + "} 下次取值即生效，共享组里的实例都会用上。"
        if os.environ.get(name):
            result += (
                "注意：本进程的环境里已经设了 " + name + "（不是从本实例 .env 来的，多半是 systemd 单元或启动脚本设的），"
                "先看进程环境的读法用的仍是那份。"
            )
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

    scope = env_scope(name)
    if scope.kind == ENV_PROCESS:
        refusal = _process_refusal(action, name, scope, instance_file, shared_file)
        if refusal is not None:
            return refusal

    _op, err = await request_guard(ctx, "write_file", {"path": _VIRTUAL_ROOT + "/" + name})
    if err is not None:
        return _guard_error(err)

    try:
        if action == "unset":
            return _unset(instance_file, shared_file, name, scope)
        return _set(instance_file, shared_file, name, args.get("value"), scope)
    except OSError as exc:
        return _error("failed to update secrets: " + str(exc))
