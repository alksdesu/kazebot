from __future__ import annotations

import copy
import json
import math
from typing import Any, Callable


def _callback(value: Any) -> Callable:
    if not callable(value):
        raise TypeError("Plugin callback must be callable")
    return value


class Routes:
    def __init__(self, plugin_id: str):
        self.plugin_id = plugin_id
        self.entries: list[dict] = []

    def register(self, router: Any, prefix: str | None = None, *, replace: bool = False) -> Any:
        prefix = f"/v1/plugins/{self.plugin_id}" if prefix is None else prefix
        if prefix and (not prefix.startswith("/") or prefix.endswith("/")):
            raise ValueError("Route prefix must begin with / and have no trailing /")
        self.entries.append({"router": router, "prefix": prefix, "replace": bool(replace)})
        return router


class Middleware:
    def __init__(self):
        self.entries: list[dict] = []

    def register(self, factory: Callable, *, global_scope: bool = False) -> Callable:
        self.entries.append({"factory": _callback(factory), "global_scope": bool(global_scope)})
        return factory


class Services:
    def __init__(self):
        self.entries: list[dict] = []

    def register(self, name: str, factory: Callable | None = None, *, instance: Any = None,
                 replace: bool = False, managed: bool = True) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Service name must be nonempty")
        if (factory is None) == (instance is None):
            raise ValueError("Supply exactly one service factory or instance")
        if factory is not None:
            _callback(factory)
        if any(entry["name"] == name for entry in self.entries):
            raise ValueError(f"Duplicate service: {name}")
        self.entries.append(dict(name=name, factory=factory, instance=instance,
                                 replace=bool(replace), managed=bool(managed)))


class Lifecycle:
    def __init__(self):
        self.startups: list[Callable] = []
        self.shutdowns: list[Callable] = []

    def startup(self, callback: Callable) -> Callable:
        self.startups.append(_callback(callback))
        return callback

    def shutdown(self, callback: Callable) -> Callable:
        self.shutdowns.append(_callback(callback))
        return callback


class Jobs:
    def __init__(self):
        self.entries: list[dict] = []

    def register(self, name: str, callback: Callable, *, interval: float | None = None) -> Callable:
        if not name or any(entry["name"] == name for entry in self.entries):
            raise ValueError("Job names must be nonempty and unique within a plugin")
        if interval is not None and (isinstance(interval, bool) or not isinstance(interval, (int, float))
                                     or not math.isfinite(interval) or interval <= 0):
            raise ValueError("Job interval must be positive")
        self.entries.append(dict(name=name, callback=_callback(callback), interval=interval))
        return callback


class Events:
    def __init__(self, host: Any):
        self.host = host
        self.entries: list[dict] = []

    def subscribe(self, topic: str, callback: Callable) -> Callable:
        if not topic:
            raise ValueError("Event topic must be nonempty")
        self.entries.append(dict(topic=topic, callback=_callback(callback)))
        return callback

    async def emit(self, topic: str, payload: Any = None) -> None:
        if self.host is None:
            raise RuntimeError("Application host has not started")
        await self.host.events.emit(topic, payload)


class Tools:
    def __init__(self):
        self.entries: list[dict] = []

    def register(self, spec: dict, handler: Callable, *, replace: bool = False) -> Callable:
        if not isinstance(spec, dict) or not isinstance(spec.get("name"), str) or not spec["name"]:
            raise ValueError("Tool spec needs a nonempty name")
        spec = copy.deepcopy(spec)
        spec["name"] = spec["name"].strip()
        spec.setdefault("description", "")
        spec.setdefault("input_schema", {"type": "object", "properties": {}})
        if not spec["name"] or not isinstance(spec["description"], str) or not isinstance(spec["input_schema"], dict):
            raise ValueError("Tool description must be text and input_schema must be an object")
        if spec.get("guard") is not None and not isinstance(spec["guard"], dict):
            raise ValueError("Tool guard must be an object")
        json.dumps(spec, allow_nan=False)
        if any(entry["spec"]["name"] == spec["name"] for entry in self.entries):
            raise ValueError(f"Duplicate tool: {spec['name']}")
        self.entries.append(dict(spec=copy.deepcopy(spec), handler=_callback(handler), replace=bool(replace)))
        return handler
