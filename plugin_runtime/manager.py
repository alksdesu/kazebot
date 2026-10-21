from __future__ import annotations

import asyncio
import atexit
import copy
import hashlib
import importlib.util
import inspect
import json
import logging
import os
import re
import sys
import threading
import traceback
import types
import uuid
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from plugin_sdk import PluginAPI, PluginExecutionError, RequestContext

logger = logging.getLogger(__name__)
RUNTIME_STALE_SECONDS = 30  # 15 missed 2s heartbeats → process is dead
_scope: ContextVar[dict[str, Any]] = ContextVar("plugin_scope", default={})
_loading: ContextVar[bool] = ContextVar("plugin_loading", default=False)
_managers: dict[tuple[int, str], "PluginManager"] = {}
_managers_lock = threading.RLock()
_SENSITIVE = re.compile(r"key|token|secret|password|authorization|cookie|credential", re.I)


def _secret_values(value: Any) -> set[str]:
    secrets: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if _SENSITIVE.search(str(key)) and isinstance(item, str) and item:
                secrets.add(item)
                if item.lower().startswith("bearer "):
                    secrets.add(item[7:])
            secrets.update(_secret_values(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            secrets.update(_secret_values(item))
    return secrets


def _diagnostic(phase: str, exc: BaseException, secrets: set[str] | None = None) -> dict:
    message = str(exc)
    for secret in sorted(secrets or (), key=len, reverse=True):
        message = message.replace(secret, "[redacted]")
    message = re.sub(r"(?i)(bearer\s+)[^\s,'\"}]+", r"\1[redacted]", message)
    message = re.sub(r"(?i)((?:api[_-]?key|token|secret|password|authorization|cookie)\s*['\"]?\s*[:=]\s*['\"]?)[^\s,'\"}]+", r"\1[redacted]", message)
    return {
        "phase": phase, "type": type(exc).__name__, "message": message[:2000],
        "frames": [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                   for frame in traceback.extract_tb(exc.__traceback__)],
    }


@contextmanager
def plugin_scope(**values: Any):
    token = _scope.set({**_scope.get(), **values})
    try:
        yield
    finally:
        _scope.reset(token)


def current_scope() -> dict[str, Any]:
    return dict(_scope.get())


def runtime_stale(updated_at: str, *, now: float | None = None) -> bool:
    reference = now if now is not None else datetime.now(timezone.utc).timestamp()
    try:
        return datetime.fromisoformat(updated_at).timestamp() < reference - RUNTIME_STALE_SECONDS
    except (ValueError, TypeError):
        return True


def _prune_runtime_dir(directory: Path, *, keep: Path) -> None:
    now = datetime.now(timezone.utc).timestamp()
    for path in directory.glob("*.json"):
        if path == keep or path.is_symlink():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            updated_at = raw.get("updated_at", "") if isinstance(raw, dict) else ""
        except OSError:
            continue
        except (ValueError, TypeError):
            updated_at = ""
        if runtime_stale(updated_at, now=now):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                continue


def get_manager(workspace_root: Path | str, process: str | None = None) -> "PluginManager":
    root = Path(workspace_root).resolve()
    key = (os.getpid(), os.path.normcase(str(root)))
    with _managers_lock:
        if key not in _managers or not _managers[key]._alive:
            _managers[key] = PluginManager(root)
        if process and _managers[key].process != process:
            _managers[key].process = process
            with _managers[key]._lock:
                _managers[key]._file_versions = None
        return _managers[key]


def _sync_lifecycle(callback: Any, api: PluginAPI) -> None:
    if not callable(callback):
        return
    value = callback(api)
    if inspect.isawaitable(value):
        close = getattr(value, "close", None)
        if close:
            close()
        raise TypeError("Plugin setup and teardown must be synchronous")


@dataclass(eq=False)
class _Version:
    definition: dict[str, Any]
    module: Any
    api: PluginAPI
    active: int = 0
    retired: bool = False
    closed: bool = False
    closing: bool = False
    host_state: Any = None


def _matches(selectors: dict, scope: dict) -> bool:
    for selector, field in (("nodes", "node_id"), ("channels", "channel"),
                            ("providers", "provider"), ("purposes", "purpose")):
        allowed = selectors.get(selector) or []
        if allowed and "*" not in allowed and str(scope.get(field) or "") not in allowed:
            return False
    return True


class PluginManager:
    def __init__(self, workspace_root: Path):
        from .store import PluginStore
        self.workspace_root = workspace_root
        self.store = PluginStore(workspace_root)
        self._lock = threading.RLock()
        self._refresh_lock = threading.RLock()
        self.host = None
        self._versions: dict[str, _Version] = {}
        self._retired: list[_Version] = []
        self._definitions: list[dict] = []
        self._errors: dict[str, str] = {}
        self._error_details: dict[str, dict] = {}
        self._retired_errors: dict[str, list[dict]] = {}
        self._attempted: dict[str, str] = {}
        self.desired_generation = ""
        self.applied_generation = ""
        self._alive = True
        self.process = "engine"
        self._file_versions = None
        self._configuration_error: dict | None = None

    def _file_stamps(self) -> tuple:
        from config_layout import file_stamp
        roots = (self.workspace_root / "plugins", self.workspace_root / "config" / "plugins")
        paths = [self.workspace_root / "config" / "plugins.yaml", *roots]
        for directory in roots:
            if directory.is_dir():
                paths.extend(sorted(path for path in directory.rglob("*")
                                    if "__pycache__" not in path.parts and path.suffix != ".pyc"))
        return tuple((str(path), file_stamp(path)) for path in paths)

    def _load(self, definition: dict) -> _Version:
        from engine.hooks.loader import register_plugin_module
        from engine.hooks.registry import HookRegistry
        plugin_id = definition["id"]
        api = PluginAPI(plugin_id, self.workspace_root, definition.get("config") or {}, HookRegistry(), self.host)
        module_name = "clonoth_plugin_" + uuid.uuid4().hex
        module = types.ModuleType(module_name)
        version = _Version(copy.deepcopy(definition), module, api)
        sys.modules[module_name] = module
        token = _loading.set(True)
        try:
            if type(definition.get("metadata", {}).get("priority", 100)) is not int:
                raise TypeError("Plugin priority must be an integer")
            package_root = definition.get("package_root")
            if package_root:
                module.__path__ = [str(package_root)]
                module.__package__ = module_name
                module.__spec__ = importlib.util.spec_from_loader(module_name, loader=None, is_package=True)
            raw_path = definition.get("path")
            if raw_path:
                path = Path(raw_path)
                source = path.read_bytes()
                if hashlib.sha256(source).hexdigest() != definition["source_hash"]:
                    raise RuntimeError("Plugin changed while loading")
                module.__file__ = str(path)
                exec(compile(source, str(path), "exec"), module.__dict__)
                setup = getattr(module, "setup", None)
                if callable(setup):
                    _sync_lifecycle(setup, api)
                else:
                    register_plugin_module(api.hooks, module, path)
            if self.host is not None:
                _loading.set(False)
                self.host.run(self.host.prepare(version))
            elif (api.routes.entries or api.services.entries or api.jobs.entries or api.lifecycle.startups
                  or api.lifecycle.shutdowns or api.middleware.entries or api.events.entries):
                raise RuntimeError("Application declarations require a running PluginHost")
            return version
        except BaseException:
            self._close_version(version)
            raise
        finally:
            _loading.reset(token)

    def _close_version(self, version: _Version) -> None:
        with self._lock:
            if version.closed or version.closing or version.active:
                return
            version.closing = True
        try:
            if self.host is not None and version.host_state is not None:
                try:
                    self.host.run(self.host.cleanup(version))
                except Exception as exc:
                    self.record_error(version, "shutdown", exc)
            _sync_lifecycle(getattr(version.module, "teardown", None), version.api)
        except Exception as exc:
            self.record_error(version, "teardown", exc)
        finally:
            prefix = version.module.__name__
            for name in list(sys.modules):
                if name == prefix or name.startswith(prefix + "."):
                    sys.modules.pop(name, None)
            with self._lock:
                version.closed = True
                version.closing = False
                if version in self._retired:
                    self._retired.remove(version)

    def _retire(self, version: _Version) -> None:
        with self._lock:
            version.retired = True
            if version not in self._retired:
                self._retired.append(version)
        if self.host is not None and version.host_state is not None:
            try:
                self.host.run(self.host.retire(version))
            except Exception as exc:
                self.record_error(version, "retire", exc)
        self._close_version(version)

    async def arefresh(self) -> None:
        await asyncio.to_thread(self.refresh)

    refresh_async = arefresh

    def refresh(self) -> None:
        if _loading.get():
            return
        if self.host is not None:
            if not self.host.running or self.host.in_lifecycle():
                return
            if self.host.on_loop():
                self.host.request_refresh()
                return
        with self._refresh_lock:
            self._refresh()

    def _refresh(self) -> None:
        if not self._alive:
            return
        stamps = self._file_stamps()
        with self._lock:
            if stamps == self._file_versions:
                return
        try:
            definitions = self.store.definitions()
            desired = self.store.generation(definitions)
        except Exception as exc:
            with self._lock:
                self._configuration_error = _diagnostic("configuration", exc)
                self._configuration_error["message"] = "Plugin settings could not be read; retaining the last applied generation"
                self.desired_generation = "invalid:" + hashlib.sha256(repr(stamps).encode()).hexdigest()
                self._file_versions = stamps
                self._write_status_locked()
            return
        with self._lock:
            self._configuration_error = None
        current_ids = {entry["id"] for entry in definitions}
        by_id = {entry["id"]: entry for entry in definitions}
        for plugin_id, version in list(self._versions.items()):
            definition = by_id.get(plugin_id)
            if definition is None or not definition.get("enabled") or not self._applies(definition):
                with self._lock:
                    self._versions.pop(plugin_id, None)
                self._retire(version)
        for definition in definitions:
            plugin_id = definition["id"]
            revision = definition["desired_hash"]
            if not definition.get("enabled") or not self._applies(definition):
                with self._lock:
                    self._attempted.pop(plugin_id, None)
                    self._errors.pop(plugin_id, None)
                    self._error_details.pop(plugin_id, None)
                continue
            if definition.get("error"):
                with self._lock:
                    detail = _diagnostic("validation", ValueError(str(definition["error"])), _secret_values(definition.get("config")))
                    self._errors[plugin_id] = f"validation: {detail['message']}"
                    self._error_details[plugin_id] = detail
                continue
            old = self._versions.get(plugin_id)
            if old is not None and old.definition["desired_hash"] == revision or self._attempted.get(plugin_id) == revision:
                continue
            self._attempted[plugin_id] = revision
            with self._lock:
                self._errors.pop(plugin_id, None)
                self._error_details.pop(plugin_id, None)
            try:
                candidate = self._load(definition)
                with self._lock:
                    self._versions[plugin_id] = candidate
                try:
                    if self.host is not None:
                        self.host.run(self.host.publish(candidate))
                except BaseException:
                    with self._lock:
                        if old is None:
                            self._versions.pop(plugin_id, None)
                        else:
                            self._versions[plugin_id] = old
                    self._retire(candidate)
                    raise
                if old is not None:
                    self._retire(old)
            except Exception as exc:
                with self._lock:
                    detail = _diagnostic("setup", exc, _secret_values(definition.get("config")))
                    self._errors[plugin_id] = f"setup: {detail['type']}: {detail['message']}"
                    self._error_details[plugin_id] = detail
                logger.warning("Plugin %s failed to load (%s)", plugin_id, type(exc).__name__)
        with self._lock:
            for plugin_id in self._attempted.keys() - current_ids:
                self._attempted.pop(plugin_id, None)
                self._errors.pop(plugin_id, None)
                self._error_details.pop(plugin_id, None)
            self._definitions = definitions
            self._file_versions = stamps
            self.desired_generation = desired
            applied = [(key, value.definition["desired_hash"]) for key, value in sorted(self._versions.items())]
            complete = all(not definition.get("enabled") or not self._applies(definition)
                           or (definition["id"] in self._versions and self._versions[definition["id"]].definition["desired_hash"] == definition["desired_hash"])
                           for definition in definitions)
            self.applied_generation = desired if complete else hashlib.sha256(json.dumps(applied).encode()).hexdigest()
            self._write_status_locked()

    def _applies(self, definition: dict) -> bool:
        return self.process in definition.get("metadata", {}).get("processes", ["engine", "supervisor"])

    def acquire(self, scope: dict | None = None, *, refresh: bool = True) -> list[_Version]:
        if refresh:
            self.refresh()
        with self._lock:
            versions = [version for version in self._versions.values()
                        if scope is None or _matches(version.definition.get("selectors") or {}, scope)]
            versions.sort(key=lambda version: (-int(version.definition.get("metadata", {}).get("priority", 100)), version.api.plugin_id))
            for version in versions:
                version.active += 1
            return versions

    def pin(self, version: _Version, *, allow_closing: bool = False) -> bool:
        with self._lock:
            if version.closed or (version.closing and not allow_closing):
                return False
            version.active += 1
            return True

    def release(self, versions: list[_Version]) -> None:
        closing = []
        with self._lock:
            for version in versions:
                if version.active <= 0:
                    raise RuntimeError("Plugin lease released more than once")
                version.active -= 1
                if version.retired and not version.active:
                    closing.append(version)
            self._write_status_locked()
        for version in closing:
            if self.host is not None and self.host.on_loop():
                self.host.defer_close(version)
            else:
                self._close_version(version)

    def record_error(self, version: _Version, phase: str, exc: BaseException, secrets: set[str] | None = None) -> None:
        with self._lock:
            detail = _diagnostic(phase, exc, _secret_values(version.api.config) | (secrets or set()))
            detail["desired_hash"] = version.definition["desired_hash"]
            plugin_id = version.definition["id"]
            if version.retired:
                history = self._retired_errors.setdefault(plugin_id, [])
                history.append(detail)
                del history[:-10]
            else:
                self._errors[plugin_id] = f"{phase}: {detail['type']}: {detail['message']}"
                self._error_details[plugin_id] = detail
            self._write_status_locked()

    def _status_locked(self) -> dict:
        plugins = []
        for definition in self._definitions:
            plugin_id = definition["id"]
            version = self._versions.get(plugin_id)
            error = self._errors.get(plugin_id)
            plugins.append({
                "id": plugin_id, "source_hash": definition["source_hash"],
                "desired_hash": definition["desired_hash"],
                "applied_hash": (version.definition["desired_hash"] if version else
                                 definition["desired_hash"] if not self._applies(definition) or not definition.get("enabled") else None),
                "state": ("not_applicable" if not self._applies(definition) else
                          "error" if error else "loaded" if version else "disabled"),
                "priority": int((version.definition if version else definition).get("metadata", {}).get("priority", 100)),
                "tools": [{"spec": entry["spec"], "replace": entry["replace"]} for entry in version.api.tool_definitions] if version else [],
                "services": list(version.host_state.services) if version and version.host_state else [],
                "error": error, "error_details": self._error_details.get(plugin_id),
                "retired_errors": list(self._retired_errors.get(plugin_id, [])),
                "active_requests": sum(item.active for item in [*self._versions.values(), *self._retired]
                                                          if item.definition["id"] == plugin_id),
            })
        return {"pid": os.getpid(), "alive": self._alive, "process": self.process,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "desired_generation": self.desired_generation,
                "applied_generation": self.applied_generation, "plugins": plugins,
                "configuration_error": self._configuration_error}

    def status(self) -> dict:
        self.refresh()
        with self._lock:
            return self._status_locked()

    def heartbeat(self) -> None:
        self.refresh()
        with self._lock:
            self._write_status_locked()

    def _write_status_locked(self) -> None:
        directory = self.workspace_root / "data" / "plugin_runtime"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{os.getpid()}.json"
            temporary = directory / f".{os.getpid()}.{threading.get_ident()}.tmp"
            temporary.write_text(json.dumps(self._status_locked(), ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, path)
            _prune_runtime_dir(directory, keep=path)
        except OSError:
            logger.warning("Could not write plugin runtime status")

    def close(self) -> None:
        if self.host is not None and self.host.on_loop():
            raise RuntimeError("Await host.stop() to close a running application host")
        with self._refresh_lock:
            with self._lock:
                versions = list(self._versions.values())
                self._versions.clear()
                self._alive = False
            for version in versions:
                self._retire(version)
            with self._lock:
                self._write_status_locked()


class _ProviderRequest:
    def __init__(self, provider: Any, streaming: bool, workspace_root: Path | str | None = None):
        self.scope = current_scope()
        if workspace_root is not None:
            self.scope["workspace_root"] = workspace_root
        self.scope["provider"] = (provider if isinstance(provider, str) else
                                  getattr(provider, "provider_name", "") or getattr(provider, "name", ""))
        self.scope.setdefault("channel", getattr(provider, "plugin_channel", ""))
        self.streaming = streaming
        self.manager = None
        self.versions: list[_Version] = []
        self.contexts: dict[str, RequestContext] = {}
        self.outcome = {"ok": True, "cancelled": False, "error": None}
        self.active = False
        self._finished = False
        self._secrets: set[str] = set()
        self._service_versions = []

    async def __aenter__(self):
        root = self.scope.get("workspace_root")
        if root is not None:
            self.manager = get_manager(root)
            if self.manager.host is not None:
                await self.manager.arefresh()
            self.versions = self.manager.acquire(self.scope, refresh=self.manager.host is None)
            if self.manager.host is not None:
                self._service_versions = self.manager.host.pin_services(self.versions)
            request_id = uuid.uuid4().hex
            for version in self.versions:
                api = version.api
                self.contexts[api.plugin_id] = RequestContext(
                    plugin_id=api.plugin_id, workspace_root=Path(root), config=copy.deepcopy(api.config),
                    request_id=request_id, streaming=self.streaming,
                    **{key: str(self.scope.get(key) or "") for key in ("node_id", "channel", "provider", "task_id")},
                    purpose=str(self.scope.get("purpose") or "inference"),
                )
            self.active = bool(self._service_versions) or any(any(version.api.callbacks.values()) for version in self.versions)
        return self

    def set_outcome(self, outcome: dict | None = None, **values: Any) -> None:
        self.outcome.update(outcome or {})
        self.outcome.update(values)

    async def _invoke(self, version: _Version, phase: str, callback: Any, value: Any):
        self._secrets.update(_secret_values(value))
        try:
            host = self.manager.host
            with host.bind_services(self._service_versions) if host is not None else nullcontext():
                result = callback(self.contexts[version.api.plugin_id], value)
                if inspect.isawaitable(result):
                    result = await result
                return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.manager.record_error(version, phase, exc, self._secrets)
            raise PluginExecutionError(version.api.plugin_id, phase, type(exc).__name__) from None

    def _validate(self, version: _Version, phase: str, value: Any) -> None:
        valid = isinstance(value, dict)
        if valid and phase in {"request", "response"}:
            valid = "body" in value and isinstance(value.get("headers"), dict)
            if valid:
                valid = all(isinstance(key, str) and isinstance(item, str) for key, item in value["headers"].items())
            if phase == "request":
                valid = valid and isinstance(value.get("method"), str) and isinstance(value.get("url"), str)
            else:
                valid = valid and type(value.get("status_code")) is int and 100 <= value["status_code"] <= 599
        elif valid and phase == "stream_event":
            valid = isinstance(value.get("data"), str)
            valid = valid and isinstance(value.get("event", ""), str)
            valid = valid and (value.get("id") is None or isinstance(value["id"], str))
            valid = valid and (value.get("retry") is None or type(value["retry"]) is int)
        if not valid:
            error = TypeError(f"Invalid {phase} callback result")
            self.manager.record_error(version, phase, error)
            raise PluginExecutionError(version.api.plugin_id, phase, "InvalidReturnType")

    async def _transform(self, phase: str, value: dict) -> dict:
        for version in self.versions:
            for callback in version.api.callbacks[phase]:
                result = await self._invoke(version, phase, callback, value)
                if result is not None:
                    value = result
                self._validate(version, phase, value)
        return value

    async def request(self, value: dict) -> dict:
        return await self._transform("request", value)

    async def response(self, value: dict) -> dict:
        return await self._transform("response", value)

    async def stream_event(self, value: dict) -> list[dict]:
        events = [value]
        for version in self.versions:
            for callback in version.api.callbacks["stream_event"]:
                transformed = []
                for event in events:
                    result = await self._invoke(version, "stream_event", callback, event)
                    replacements = [event] if result is None else result if isinstance(result, list) else [result]
                    for replacement in replacements:
                        self._validate(version, "stream_event", replacement)
                    transformed.extend(replacements)
                events = transformed
        return events

    async def __aexit__(self, exc_type, exc, traceback):
        if self._finished:
            return False
        self._finished = True
        if exc is not None:
            self.outcome.update(ok=False, cancelled=isinstance(exc, asyncio.CancelledError), error=type(exc).__name__)
        first_error = None
        try:
            for version in self.versions:
                for callback in version.api.callbacks["end"]:
                    try:
                        await self._invoke(version, "end", callback, dict(self.outcome))
                    except BaseException as end_error:
                        first_error = first_error or end_error
        finally:
            if self.manager:
                try:
                    self.manager.release(self._service_versions)
                finally:
                    self.manager.release(self.versions)
        if exc is None and first_error is not None:
            raise first_error
        return False


def provider_request(*, provider: Any, streaming: bool = False, workspace_root: Path | str | None = None):
    return _ProviderRequest(provider, streaming, workspace_root)


def _close_managers() -> None:
    for manager in list(_managers.values()):
        if manager._alive:
            manager.close()


atexit.register(_close_managers)
