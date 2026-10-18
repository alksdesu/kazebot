from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .declarations import Events, Jobs, Lifecycle, Middleware, Routes, Services, Tools


class PluginExecutionError(RuntimeError):
    def __init__(self, plugin_id: str, phase: str, error_type: str):
        self.plugin_id = plugin_id
        self.phase = phase
        super().__init__(f"Plugin {plugin_id} failed during {phase} ({error_type})")


@dataclass
class RequestContext:
    plugin_id: str
    workspace_root: Path
    config: dict[str, Any]
    state: dict[str, Any] = field(default_factory=dict)
    request_id: str = ""
    node_id: str = ""
    channel: str = ""
    provider: str = ""
    task_id: str = ""
    purpose: str = "inference"
    streaming: bool = False


class PluginAPI:
    def __init__(self, plugin_id: str, workspace_root: Path, config: dict, hooks: Any, host: Any = None):
        self.plugin_id = plugin_id
        self.workspace_root = workspace_root
        self.config = copy.deepcopy(config)
        self.hooks = hooks
        self.host = host
        self.routes = Routes(plugin_id)
        self.middleware = Middleware()
        self.services = Services()
        self.jobs = Jobs()
        self.lifecycle = Lifecycle()
        self.events = Events(host)
        self.tools = Tools()
        self.tool_definitions = self.tools.entries
        self.callbacks: dict[str, list[Callable]] = {
            phase: [] for phase in ("request", "response", "stream_event", "end")
        }

    def _register(self, phase: str, callback: Callable) -> Callable:
        if not callable(callback):
            raise TypeError("Plugin callback must be callable")
        self.callbacks[phase].append(callback)
        return callback

    def request(self, callback: Callable) -> Callable:
        return self._register("request", callback)

    def response(self, callback: Callable) -> Callable:
        return self._register("response", callback)

    def stream_event(self, callback: Callable) -> Callable:
        return self._register("stream_event", callback)

    def end(self, callback: Callable) -> Callable:
        return self._register("end", callback)

    before_request = request
    after_response = response
    on_stream_event = stream_event
    on_request_end = end

    def register_hook(self, hook_point: str, handler: Any, priority: int | None = None) -> None:
        self.hooks.register(hook_point, handler, priority=priority)


__all__ = ["PluginAPI", "PluginExecutionError", "RequestContext"]
