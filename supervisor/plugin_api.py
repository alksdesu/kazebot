"""控制台的插件文件、配置和运行状态接口。"""
from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path
from typing import Any, Callable, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool

import config_layout
from plugin_runtime.store import MAX_SOURCE_BYTES, PluginStore
from plugin_runtime.packages import MAX_ARCHIVE_BYTES
from .admin_api import verify_admin_token

T = TypeVar("T")


class _PluginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PluginInstall(_PluginInput):
    filename: str = Field(min_length=4, max_length=200)
    content: str = Field(max_length=MAX_SOURCE_BYTES)


class PluginSource(_PluginInput):
    content: str = Field(max_length=MAX_SOURCE_BYTES)
    base_hash: str = Field(min_length=64, max_length=64)


class PluginPackage(_PluginInput):
    filename: str = Field(min_length=5, max_length=200)
    content_base64: str = Field(max_length=((MAX_ARCHIVE_BYTES + 2) // 3) * 4)
    base_hash: str | None = Field(default=None, min_length=64, max_length=64)


class PluginConfig(_PluginInput):
    enabled: StrictBool
    config: dict[str, Any] = Field(default_factory=dict)
    selectors: dict[str, list[str]] = Field(default_factory=dict)
    base_hash: str = Field(min_length=64, max_length=64)


def _handle(operation: Callable[[], T]) -> T:
    try:
        return operation()
    except config_layout.ConfigConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, TypeError, UnicodeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, f"插件文件操作失败：{exc}") from exc


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def runtime_status(root: Path) -> list[dict[str, Any]]:
    from plugin_runtime.manager import runtime_stale

    result: list[dict[str, Any]] = []
    directory = root / "data" / "plugin_runtime"
    for path in sorted(directory.glob("*.json")):
        if path.is_symlink():
            continue
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
            pid = raw["pid"]
            if isinstance(pid, bool) or not isinstance(pid, int) or not 0 < pid <= 0xFFFFFFFF:
                continue
            if runtime_stale(raw.get("updated_at", "")):
                continue
            alive = _pid_alive(pid) and raw.get("alive", True) is True
            plugins = []
            for item in raw.get("plugins", []):
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    continue
                state = item.get("state", item.get("status", "error"))
                if state == "enabled":
                    state = "loaded"
                details = item.get("error_details")
                errors = item.get("errors") if isinstance(item.get("errors"), list) else []
                if isinstance(details, dict):
                    message = f"{details.get('type', 'Error')}: {details.get('message', '')}"
                    frames = details.get("frames")
                    if isinstance(frames, list):
                        trace = [f"{frame.get('file', '?')}:{frame.get('line', '?')} in {frame.get('function', '?')}" for frame in frames if isinstance(frame, dict)]
                        if trace:
                            message += "\n" + "\n".join(trace)
                    errors = [{"phase": str(details.get("phase") or "runtime"), "message": message, "at": raw.get("updated_at", "")}]
                plugins.append({
                    **item,
                    "status": state if alive else "stopped",
                    "desired_hash": item.get("applied_hash") or item.get("desired_hash", ""),
                    "configured_hash": item.get("desired_hash", ""),
                    "error": item.get("error") or None,
                    "errors": errors,
                })
            result.append({
                **raw, "pid": pid, "alive": bool(alive), "plugins": plugins,
                "process": raw.get("process", "engine"),
                "generation": raw.get("applied_generation", raw.get("generation", "")),
            })
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            continue
    return result


def create_plugin_router(workspace_root: Path) -> APIRouter:
    store = PluginStore(workspace_root)
    router = APIRouter(dependencies=[Depends(verify_admin_token)])

    @router.get("")
    def list_plugins() -> dict[str, Any]:
        def read() -> dict[str, Any]:
            definitions = store.definitions()
            return {
                "plugins": [store.public(item) for item in definitions],
                "generation": store.generation(definitions),
                "shared_group": config_layout.config_share_group(workspace_root),
                "runtime": runtime_status(workspace_root),
            }
        return _handle(read)

    @router.post("")
    def install_plugin(body: PluginInstall) -> dict[str, Any]:
        return _handle(lambda: store.install(body.filename, body.content))

    @router.post("/packages")
    def install_package(body: PluginPackage) -> dict[str, Any]:
        def install() -> dict[str, Any]:
            try:
                content = base64.b64decode(body.content_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("插件 ZIP 的 Base64 编码无效") from exc
            return store.install_package(body.filename, content, body.base_hash)
        return _handle(install)

    @router.get("/frontend")
    def frontend_plugins() -> dict[str, Any]:
        return _handle(store.frontend_catalog)

    @router.post("/apply")
    async def apply_plugins() -> dict[str, Any]:
        from plugin_runtime import get_manager

        manager = _handle(lambda: get_manager(workspace_root, process="supervisor"))
        try:
            await manager.arefresh()
        except (config_layout.ConfigConflict, PermissionError, FileNotFoundError, ValueError, TypeError, UnicodeError, OSError) as exc:
            def fail() -> None:
                raise exc
            _handle(fail)
        return _handle(lambda: {
            "generation": store.generation(), "runtime": runtime_status(workspace_root),
            "message": "插件配置已应用；引擎在下一次刷新时接入，新请求使用新版本，正在执行的请求继续使用原版本。",
        })

    @router.get("/{plugin_id}/source")
    def read_source(plugin_id: str) -> dict[str, Any]:
        return _handle(lambda: store.source(plugin_id))

    @router.get("/{plugin_id}/assets/{package_hash}/{path:path}")
    def read_asset(plugin_id: str, package_hash: str, path: str) -> Response:
        content, media_type = _handle(lambda: store.asset(plugin_id, package_hash, path))
        return Response(content, media_type=media_type, headers={
            "Cache-Control": "private, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff",
        })

    @router.put("/{plugin_id}/source")
    def update_source(plugin_id: str, body: PluginSource) -> dict[str, Any]:
        return _handle(lambda: store.update_source(plugin_id, body.content, body.base_hash))

    @router.put("/{plugin_id}/config")
    def update_config(plugin_id: str, body: PluginConfig) -> dict[str, Any]:
        return _handle(lambda: store.configure(plugin_id, **body.model_dump()))

    @router.delete("/{plugin_id}")
    def delete_plugin(plugin_id: str, base_hash: str = Query(min_length=64, max_length=64)) -> dict[str, bool]:
        _handle(lambda: store.remove(plugin_id, base_hash))
        return {"ok": True}

    return router
