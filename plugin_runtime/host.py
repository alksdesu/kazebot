from __future__ import annotations

import asyncio
import inspect
import logging
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.middleware.asyncexitstack import AsyncExitStackMiddleware
from starlette.middleware.exceptions import ExceptionMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Match

logger = logging.getLogger(__name__)
_leases: ContextVar[tuple | None] = ContextVar("plugin_host_leases", default=None)
_lifecycle: ContextVar[bool] = ContextVar("plugin_host_lifecycle", default=False)


async def _invoke(callback, *args):
    if not inspect.signature(callback).parameters:
        args = ()
    if inspect.iscoroutinefunction(callback):
        return await callback(*args)
    task = asyncio.create_task(asyncio.to_thread(callback, *args))
    try:
        result = await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise
    return await result if inspect.isawaitable(result) else result


@dataclass
class Generation:
    services: dict[str, Any] = field(default_factory=dict)
    managed: set[str] = field(default_factory=set)
    routes: list[dict] = field(default_factory=list)
    middleware: list[Any] = field(default_factory=list)
    jobs: set[asyncio.Task] = field(default_factory=set)
    published: asyncio.Event = field(default_factory=asyncio.Event)
    shutdown_started: bool = False


class MiddlewareBinding:
    def __init__(self, entry):
        self.global_scope = entry["global_scope"]
        self._next = ContextVar("plugin_middleware_next")
        self.app = entry["factory"](self.call_next)
        if not callable(self.app):
            close = getattr(self.app, "close", None)
            if close:
                close()
            raise TypeError("Middleware factory must return an ASGI callable")

    async def call_next(self, scope, receive, send):
        await self._next.get()(scope, receive, send)

    def wrap(self, target):
        async def run(scope, receive, send):
            token = self._next.set(target)
            try:
                await self.app(scope, receive, send)
            finally:
                self._next.reset(token)
        return run


class ServiceProxy:
    def __init__(self, host, name):
        object.__setattr__(self, "_host", host)
        object.__setattr__(self, "_name", name)

    def __getattr__(self, name):
        host = self._host
        versions = host.acquire_services()
        try:
            with host.bind_services(versions):
                value = getattr(host.resolve(self._name, versions), name)
            if not callable(value):
                return value
        finally:
            host.manager.release(versions)

        def invoke(*args, **kwargs):
            versions = host.acquire_services()
            try:
                with host.bind_services(versions):
                    result = getattr(host.resolve(self._name, versions), name)(*args, **kwargs)
            except BaseException:
                host.manager.release(versions)
                raise
            if inspect.isawaitable(result):
                async def finish():
                    try:
                        with host.bind_services(versions):
                            return await result
                    finally:
                        host.manager.release(versions)
                return finish()
            if hasattr(result, "__aenter__") and hasattr(result, "__aexit__"):
                @asynccontextmanager
                async def context():
                    try:
                        with host.bind_services(versions):
                            value = await result.__aenter__()
                        try:
                            yield value
                        except BaseException as exc:
                            with host.bind_services(versions):
                                suppress = await result.__aexit__(type(exc), exc, exc.__traceback__)
                            if not suppress:
                                raise
                        else:
                            with host.bind_services(versions):
                                await result.__aexit__(None, None, None)
                    finally:
                        host.manager.release(versions)
                return context()
            if hasattr(result, "__enter__") and hasattr(result, "__exit__"):
                @contextmanager
                def context():
                    try:
                        with host.bind_services(versions):
                            value = result.__enter__()
                        try:
                            yield value
                        except BaseException as exc:
                            with host.bind_services(versions):
                                suppress = result.__exit__(type(exc), exc, exc.__traceback__)
                            if not suppress:
                                raise
                        else:
                            with host.bind_services(versions):
                                result.__exit__(None, None, None)
                    finally:
                        host.manager.release(versions)
                return context()
            if inspect.isasyncgen(result):
                async def stream():
                    try:
                        while True:
                            with host.bind_services(versions):
                                try:
                                    item = await anext(result)
                                except StopAsyncIteration:
                                    break
                            yield item
                    finally:
                        try:
                            with host.bind_services(versions):
                                await result.aclose()
                        finally:
                            host.manager.release(versions)
                return stream()
            if inspect.isgenerator(result):
                def stream():
                    try:
                        while True:
                            with host.bind_services(versions):
                                try:
                                    item = next(result)
                                except StopIteration:
                                    break
                            yield item
                    finally:
                        try:
                            with host.bind_services(versions):
                                result.close()
                        finally:
                            host.manager.release(versions)
                return stream()
            host.manager.release(versions)
            return result
        return invoke

    def __setattr__(self, name, value):
        versions = self._host.acquire_services()
        try:
            with self._host.bind_services(versions):
                setattr(self._host.resolve(self._name, versions), name, value)
        finally:
            self._host.manager.release(versions)


class HostEvents:
    def __init__(self, host):
        self.host = host

    async def emit(self, topic: str, payload: Any = None):
        versions = self.host.manager.acquire(refresh=False)
        token = _leases.set(tuple(versions))
        try:
            for version in versions:
                for entry in version.api.events.entries:
                    if entry["topic"] == topic:
                        try:
                            await _invoke(entry["callback"], payload)
                        except Exception as exc:
                            self.host.manager.record_error(version, "event", exc)
                            raise
        finally:
            _leases.reset(token)
            self.host.manager.release(versions)


class PluginHost:
    def __init__(self, manager, *, state=None, app=None):
        self.manager = manager
        self.state = state
        self.app = app
        self.workspace_root = manager.workspace_root
        self.process = manager.process
        self.events = HostEvents(self)
        self.loop = None
        self.running = False
        self._base: dict[str, Any] = {}
        self._managed_base: set[str] = set()
        self._base_running: set[str] = set()
        self._running_services: dict[int, Any] = {}
        self._paused_services: set[int] = set()
        self._refresh_task = None
        self._watch_task = None
        self._cleanup_tasks: set[asyncio.Task] = set()
        self._stopping = False
        manager.host = self
        manager._file_versions = None

    def register_base(self, name: str, instance: Any, *, managed=False) -> ServiceProxy:
        self._base[name] = instance
        if managed:
            self._managed_base.add(name)
        return self.service(name)

    def service(self, name: str) -> ServiceProxy:
        return ServiceProxy(self, name)

    def base_service(self, name: str):
        return self._base[name]

    def pin_services(self, owners=()):
        versions = self.acquire_services()
        owner_ids = {version.api.plugin_id for version in owners}
        displaced = [version for version in versions if version.api.plugin_id in owner_ids]
        versions = [version for version in versions if version.api.plugin_id not in owner_ids]
        try:
            for version in owners:
                if not self.manager.pin(version, allow_closing=_lifecycle.get()):
                    raise RuntimeError("Plugin callback generation is no longer available")
                versions.append(version)
            return versions
        except BaseException:
            self.manager.release(versions)
            raise
        finally:
            self.manager.release(displaced)

    @contextmanager
    def bind_services(self, versions):
        token = _leases.set(tuple(versions))
        try:
            yield
        finally:
            _leases.reset(token)

    @contextmanager
    def lease_context(self, owners):
        versions = self.pin_services(owners)
        try:
            with self.bind_services(versions):
                yield
        finally:
            self.manager.release(versions)

    def acquire_services(self):
        scoped = _leases.get()
        if scoped is not None:
            versions = []
            for version in scoped:
                if not self.manager.pin(version, allow_closing=_lifecycle.get()):
                    self.manager.release(versions)
                    break
                versions.append(version)
            else:
                return versions
        return self.manager.acquire(refresh=False)

    def resolve(self, name: str, versions=None):
        versions = list(self.manager._versions.values()) if versions is None else versions
        versions = sorted(versions, key=lambda item: (-int(item.definition.get("metadata", {}).get("priority", 100)), item.api.plugin_id))
        value = self._base.get(name)
        found = name in self._base
        for version in reversed(versions):
            generation = version.host_state
            if generation is not None and name in generation.services:
                entry = next(item for item in version.api.services.entries if item["name"] == name)
                if not found or entry["replace"]:
                    value, found = generation.services[name], True
        if not found:
            raise LookupError(f"Unknown host service: {name}")
        return value

    def on_loop(self):
        try:
            return asyncio.get_running_loop() is self.loop
        except RuntimeError:
            return False

    def in_lifecycle(self):
        return _lifecycle.get()

    def run(self, coroutine):
        if not self.running or self.loop is None or not self.loop.is_running():
            coroutine.close()
            raise RuntimeError("Application host is not running")
        if self.on_loop():
            coroutine.close()
            raise RuntimeError("Use the asynchronous host API from its event loop")
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result()

    def request_refresh(self):
        if not self._stopping and (self._refresh_task is None or self._refresh_task.done()):
            self._refresh_task = asyncio.create_task(self.manager.arefresh())

    def defer_close(self, version):
        task = asyncio.create_task(asyncio.to_thread(self.manager._close_version, version))
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    async def start(self):
        if self.running:
            return
        self.loop = asyncio.get_running_loop()
        self.running = True
        self._stopping = False
        self.manager._attempted.clear()
        self.manager._file_versions = None
        with self.manager._lock:
            previous = list(self.manager._versions.values())
            self.manager._versions.clear()
        for version in previous:
            await asyncio.to_thread(self.manager._retire, version)
        await self.manager.arefresh()
        await self._reconcile_base()
        self._watch_task = asyncio.create_task(self._watch())

    async def _watch(self):
        while True:
            await asyncio.sleep(2)
            try:
                await asyncio.to_thread(self.manager.heartbeat)
            except Exception:
                logger.exception("Plugin host refresh failed")

    async def prepare(self, version):
        generation = version.host_state = Generation()
        peers = [item for item in self.manager._versions.values() if item.api.plugin_id != version.api.plugin_id]
        for entry in version.api.services.entries:
            name = entry["name"]
            occupied = name in self._base or any(item.host_state and name in item.host_state.services for item in peers)
            if occupied and not entry["replace"]:
                raise ValueError(f"Service {name} exists; replace=True is required")
        occupied_routes = list(getattr(self.app, "routes", []))
        for peer in peers:
            if peer.host_state:
                occupied_routes.extend(item["route"] for item in peer.host_state.routes)
        for entry in version.api.routes.entries:
            router = APIRouter()
            router.include_router(entry["router"], prefix=entry["prefix"])
            for route in router.routes:
                for existing in occupied_routes:
                    same_path = getattr(existing, "path", None) == getattr(route, "path", None)
                    existing_methods = getattr(existing, "methods", None)
                    methods = getattr(route, "methods", None)
                    if same_path and ((existing_methods is None and methods is None) or (existing_methods and methods and existing_methods & methods)) and not entry["replace"]:
                        raise ValueError(f"Route {route.path} exists; replace=True is required")
                generation.routes.append({"route": route, "replace": entry["replace"]})
                occupied_routes.append(route)
        token = _leases.set(tuple([*peers, version]))
        lifecycle_token = _lifecycle.set(True)
        try:
            for entry in version.api.services.entries:
                service = entry["instance"] if entry["factory"] is None else await _invoke(entry["factory"], self)
                generation.services[entry["name"]] = service
                if entry["managed"]:
                    generation.managed.add(entry["name"])
                    try:
                        previous = self.resolve(entry["name"])
                    except LookupError:
                        previous = None
                    if previous is not service and id(previous) in self._running_services:
                        await self._pause(previous)
                    start = getattr(service, "start", None)
                    if start:
                        if not callable(getattr(service, "stop", None) or getattr(service, "aclose", None) or getattr(service, "close", None)):
                            raise TypeError(f"Managed service {entry['name']} with start() must expose stop() or close()")
                        self._running_services[id(service)] = service
                        await _invoke(start)
            generation.middleware = [MiddlewareBinding(entry) for entry in version.api.middleware.entries]
            for callback in version.api.lifecycle.startups:
                await _invoke(callback, version.api)
            for entry in version.api.jobs.entries:
                task = asyncio.create_task(self._job(version, entry), name=f"plugin:{version.api.plugin_id}:{entry['name']}")
                generation.jobs.add(task)
                task.add_done_callback(generation.jobs.discard)
        finally:
            _lifecycle.reset(lifecycle_token)
            _leases.reset(token)

    async def publish(self, version):
        await self._reconcile_base()
        version.host_state.published.set()

    async def _job(self, version, entry):
        _lifecycle.set(False)
        await version.host_state.published.wait()
        while not version.retired and not self._stopping:
            versions = self.manager.acquire(refresh=False)
            if version not in versions:
                if not self.manager.pin(version):
                    self.manager.release(versions)
                    return
                versions.append(version)
            token = _leases.set(tuple(versions))
            try:
                await _invoke(entry["callback"], version.api)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.manager.record_error(version, "job", exc)
            finally:
                _leases.reset(token)
                self.manager.release(versions)
            if entry["interval"] is None:
                return
            await asyncio.sleep(entry["interval"])

    async def _reconcile_base(self):
        for name in self._managed_base:
            active = not self._stopping and self.resolve(name) is self._base[name]
            if active and name not in self._base_running:
                await _invoke(self._base[name].start)
                self._base_running.add(name)
                self._running_services[id(self._base[name])] = self._base[name]
            elif active:
                await self._resume(self._base[name])
            elif name in self._base_running and self._stopping:
                service = self._base[name]
                await _invoke(getattr(service, "stop", None) or service.close)
                self._base_running.remove(name)
                self._running_services.pop(id(service), None)
                self._paused_services.discard(id(service))
            elif name in self._base_running:
                await self._pause(self._base[name])
        for version in list(self.manager._versions.values()):
            generation = version.host_state
            if generation is None:
                continue
            for name in generation.managed:
                service = generation.services[name]
                active = not self._stopping and self.resolve(name) is service
                if active and id(service) not in self._running_services and getattr(service, "start", None):
                    await _invoke(service.start)
                    self._running_services[id(service)] = service
                elif active:
                    await self._resume(service)
                elif not active and id(service) in self._running_services:
                    await self._pause(service)

    async def _pause(self, service):
        pause = getattr(service, "pause", None)
        if pause is not None and id(service) not in self._paused_services:
            await _invoke(pause)
            self._paused_services.add(id(service))

    async def _resume(self, service):
        if id(service) in self._paused_services:
            await _invoke(getattr(service, "resume", None) or service.start)
            self._paused_services.remove(id(service))

    async def retire(self, version):
        generation = version.host_state
        if generation is None:
            return
        tasks = list(generation.jobs)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for name in generation.managed:
            service = generation.services[name]
            if id(service) in self._running_services:
                try:
                    await self._pause(service)
                except Exception as exc:
                    self.manager.record_error(version, "service_pause", exc)
        await self._reconcile_base()

    async def cleanup(self, version):
        generation = version.host_state
        if generation.shutdown_started:
            return
        generation.shutdown_started = True
        token = _leases.set((version,))
        lifecycle_token = _lifecycle.set(True)
        try:
            for callback in reversed(version.api.lifecycle.shutdowns):
                try:
                    await _invoke(callback, version.api)
                except Exception as exc:
                    self.manager.record_error(version, "shutdown", exc)
            for name in reversed(list(generation.services)):
                if name not in generation.managed:
                    continue
                service = generation.services[name]
                stop = getattr(service, "stop", None) or getattr(service, "aclose", None) or getattr(service, "close", None)
                if stop and (id(service) in self._running_services or not getattr(service, "start", None)):
                    try:
                        await _invoke(stop)
                    except Exception as exc:
                        self.manager.record_error(version, "service_shutdown", exc)
                    finally:
                        self._running_services.pop(id(service), None)
                        self._paused_services.discard(id(service))
                close = getattr(service, "aclose", None) or getattr(service, "close", None)
                if close and close != stop:
                    try:
                        await _invoke(close)
                    except Exception as exc:
                        self.manager.record_error(version, "service_close", exc)
        finally:
            _lifecycle.reset(lifecycle_token)
            _leases.reset(token)
        if not generation.published.is_set():
            await self._reconcile_base()

    async def stop(self):
        if not self.running:
            return
        self._stopping = True
        if self._watch_task:
            self._watch_task.cancel()
            await asyncio.gather(self._watch_task, return_exceptions=True)
        if self._refresh_task:
            await asyncio.gather(self._refresh_task, return_exceptions=True)
        await asyncio.to_thread(self.manager.close)
        while self.manager._retired:
            await asyncio.sleep(0.01)
        if self._cleanup_tasks:
            await asyncio.gather(*list(self._cleanup_tasks), return_exceptions=True)
        await self._reconcile_base()
        self.running = False


class PluginDispatcher:
    def __init__(self, app, host: PluginHost, authenticate):
        self.app = app
        self.host = host
        self.authenticate = authenticate

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        await self.host.manager.arefresh()
        versions = self.host.manager.acquire(refresh=False)
        token = _leases.set(tuple(versions))
        try:
            selected = None
            selected_scope = None
            owner = None
            base_match = any(route.matches(scope)[0] is Match.FULL for route in self.host.app.routes)
            for version in reversed(versions):
                if version.host_state is None:
                    continue
                for entry in version.host_state.routes:
                    match, child_scope = entry["route"].matches(scope)
                    if match is Match.FULL and (not base_match or entry["replace"]) and (selected is None or entry["replace"]):
                        selected, selected_scope, owner = entry["route"], child_scope, version
            middleware = [(version, binding) for version in versions if version.host_state
                          for binding in version.host_state.middleware if binding.global_scope or version is owner]
            request_scope = {**scope, "type": "http"}
            request = Request(request_scope, receive=receive)
            if selected is None and not request.headers.get("authorization") and not request.query_params.get("token"):
                middleware = []
            if selected is not None or middleware:
                try:
                    result = self.authenticate(request)
                    if inspect.isawaitable(result):
                        await result
                except HTTPException as exc:
                    if selected is None:
                        middleware = []
                    elif scope["type"] == "websocket":
                        await send({"type": "websocket.close", "code": 1008, "reason": "Unauthorized"})
                    else:
                        await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(scope, receive, send)
                    if selected is not None:
                        return
            target = self.app
            if selected is not None:
                scope.update(selected_scope)
                target = ExceptionMiddleware(AsyncExitStackMiddleware(selected.handle),
                                             handlers=self.host.app.exception_handlers)
            for version, binding in reversed(middleware):
                target = binding.wrap(target)
            await target(scope, receive, send)
        finally:
            _leases.reset(token)
            self.host.manager.release(versions)
