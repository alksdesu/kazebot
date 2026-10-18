from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import FastAPI, HTTPException

from plugin_runtime import get_manager
from plugin_runtime.host import PluginDispatcher, PluginHost


def install(root, source, *, enabled=True, plugin_id="sample"):
    directory = root / "config" / "plugins"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{plugin_id}.py").write_text(source, encoding="utf-8")
    config = root / "config" / "plugins.yaml"
    data = yaml.safe_load(config.read_text()) if config.exists() else {"version": 1, "plugins": {}}
    data["plugins"][plugin_id] = {"enabled": enabled}
    config.write_text(yaml.safe_dump(data), encoding="utf-8")


def build(root):
    manager = get_manager(root, process="supervisor")
    app = FastAPI()
    host = PluginHost(manager, app=app)

    def authenticate(request):
        if request.headers.get("authorization") != "Bearer test":
            raise HTTPException(401, "Unauthorized")

    app.add_middleware(PluginDispatcher, host=host, authenticate=authenticate)
    return manager, host, app


def test_routes_replace_auth_and_disable_restore(tmp_path):
    source = """
from fastapi import APIRouter

def setup(api):
    router = APIRouter()
    @router.get('/base')
    async def base():
        return {'value': 'plugin'}
    api.routes.register(router, prefix='', replace=True)
    @api.lifecycle.startup
    async def startup():
        (api.workspace_root / 'started').write_text('yes')
    @api.lifecycle.shutdown
    async def shutdown():
        (api.workspace_root / 'closed').write_text('yes')
"""
    install(tmp_path, source)
    manager, host, app = build(tmp_path)

    @app.get("/base")
    async def base():
        return {"value": "base"}

    async def run():
        await host.start()
        try:
            assert (tmp_path / "started").exists()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                assert (await client.get("/base")).status_code == 401
                assert (await client.get("/base", headers={"authorization": "Bearer test"})).json() == {"value": "plugin"}
                install(tmp_path, source, enabled=False)
                assert (await client.get("/base")).json() == {"value": "base"}
            assert (tmp_path / "closed").exists()
        finally:
            await host.stop()
    asyncio.run(run())


def test_candidate_startup_failure_preserves_active_generation(tmp_path):
    source = """
def setup(api):
    api.services.register('answer', instance='old')
    @api.lifecycle.startup
    async def startup():
        pass
"""
    install(tmp_path, source)
    manager, host, _ = build(tmp_path)

    async def run():
        await host.start()
        try:
            old = manager._versions['sample']
            install(tmp_path, source.replace("pass", "raise RuntimeError('broken')").replace("'old'", "'new'"))
            await manager.arefresh()
            assert host.resolve('answer') == 'old'
            assert manager._versions['sample'] is old
            assert manager.status()['plugins'][0]['state'] == 'error'
            assert manager.applied_generation != manager.desired_generation
        finally:
            await host.stop()
    asyncio.run(run())


def test_route_lease_pins_stream_and_services_through_reload(tmp_path):
    source = """
import asyncio
from fastapi import APIRouter
from starlette.responses import StreamingResponse
entered = asyncio.Event()
resume = asyncio.Event()
class Service:
    def value(self):
        return 'VERSION'
def setup(api):
    api.services.register('answer', instance=Service())
    router = APIRouter()
    @router.get('/stream')
    async def stream():
        async def chunks():
            yield api.host.service('answer').value()
            entered.set()
            await resume.wait()
            yield api.host.service('answer').value()
        return StreamingResponse(chunks())
    api.routes.register(router)
    @api.lifecycle.shutdown
    async def shutdown():
        (api.workspace_root / 'closed_VERSION').write_text('yes')
"""
    install(tmp_path, source.replace('VERSION', 'old'))
    manager, host, app = build(tmp_path)

    async def run():
        await host.start()
        old = manager._versions['sample']
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test', headers={'authorization': 'Bearer test'}) as client:
                request = asyncio.create_task(client.get('/v1/plugins/sample/stream'))
                await old.module.entered.wait()
                install(tmp_path, source.replace('VERSION', 'new'))
                await manager.arefresh()
                assert not (tmp_path / 'closed_old').exists()
                assert host.service('answer').value() == 'new'
                old.module.resume.set()
                assert (await request).text == 'oldold'
                for _ in range(100):
                    if (tmp_path / 'closed_old').exists():
                        break
                    await asyncio.sleep(.01)
                assert (tmp_path / 'closed_old').exists()
        finally:
            await host.stop()
    asyncio.run(run())


def test_jobs_events_and_managed_base_restore(tmp_path):
    source = """
import asyncio
class Service:
    def value(self):
        return 'plugin'
def setup(api):
    api.services.register('answer', instance=Service(), replace=True)
    async def job():
        (api.workspace_root / 'job_started').write_text('yes')
        try:
            await asyncio.Event().wait()
        finally:
            (api.workspace_root / 'job_cancelled').write_text('yes')
    api.jobs.register('wait', job)
    def event(payload):
        (api.workspace_root / 'event').write_text(payload)
    api.events.subscribe('sample', event)
"""
    install(tmp_path, source)
    manager, host, _ = build(tmp_path)

    class Base:
        def __init__(self):
            self.running = False
        def start(self):
            self.running = True
        def stop(self):
            self.running = False
        def value(self):
            return 'base'

    base = Base()
    proxy = host.register_base('answer', base, managed=True)

    async def run():
        await host.start()
        try:
            for _ in range(100):
                if (tmp_path / 'job_started').exists():
                    break
                await asyncio.sleep(.01)
            assert proxy.value() == 'plugin'
            assert not base.running
            await host.events.emit('sample', 'payload')
            assert (tmp_path / 'event').read_text() == 'payload'
            install(tmp_path, source, enabled=False)
            await manager.arefresh()
            assert (tmp_path / 'job_cancelled').exists()
            assert proxy.value() == 'base'
            assert base.running
        finally:
            await host.stop()
        assert not base.running
    asyncio.run(run())


def test_package_relative_imports_and_namespace_cleanup(tmp_path):
    directory = tmp_path / 'config' / 'plugins' / 'pkg'
    directory.mkdir(parents=True)
    (directory / 'plugin.json').write_text('{"id":"pkg","name":"Package","version":"1","sdk_version":1,"backend":"backend.py"}')
    (directory / 'helper.py').write_text('VALUE = "relative"')
    (directory / 'backend.py').write_text("from .helper import VALUE\ndef setup(api):\n    api.services.register('answer', instance=VALUE)\n")
    (tmp_path / 'config' / 'plugins.yaml').write_text('version: 1\nplugins:\n  pkg:\n    enabled: true\n')
    manager, host, _ = build(tmp_path)

    async def run():
        await host.start()
        try:
            assert host.resolve('answer') == 'relative'
            namespace = manager._versions['pkg'].module.__name__
            assert namespace + '.helper' in sys.modules
        finally:
            await host.stop()
        assert not any(name == namespace or name.startswith(namespace + '.') for name in sys.modules)
    asyncio.run(run())



def test_global_middleware_preserves_public_routes(tmp_path):
    install(tmp_path, """
def setup(api):
    def middleware(app):
        async def run(scope, receive, send):
            (api.workspace_root / 'middleware').write_text('called')
            await app(scope, receive, send)
        return run
    api.middleware.register(middleware, global_scope=True)
""")
    manager, host, app = build(tmp_path)

    @app.get('/web/')
    async def public():
        return {'public': True}

    async def run():
        await host.start()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                assert (await client.get('/web/')).json() == {'public': True}
                assert not (tmp_path / 'middleware').exists()
                assert (await client.get('/web/', headers={'authorization': 'Bearer test'})).status_code == 200
                assert (tmp_path / 'middleware').exists()
        finally:
            await host.stop()
    asyncio.run(run())


def test_publish_failure_restores_previous_and_closes_candidate(tmp_path):
    source = """
def setup(api):
    api.services.register('answer', instance='VERSION')
    @api.lifecycle.shutdown
    async def shutdown():
        (api.workspace_root / 'closed_VERSION').write_text('yes')
"""
    install(tmp_path, source.replace('VERSION', 'old'))
    manager, host, _ = build(tmp_path)

    async def run():
        await host.start()
        old = manager._versions['sample']
        original_publish = host.publish
        try:
            async def fail(version):
                raise RuntimeError('publication failed')
            host.publish = fail
            install(tmp_path, source.replace('VERSION', 'new'))
            await manager.arefresh()
            assert manager._versions['sample'] is old
            assert host.resolve('answer') == 'old'
            assert (tmp_path / 'closed_new').exists()
            assert not (tmp_path / 'closed_old').exists()
        finally:
            host.publish = original_publish
            await host.stop()
    asyncio.run(run())


def test_managed_service_start_failure_resumes_base(tmp_path):
    source = """
class Service:
    def start(self):
        raise RuntimeError('start failed')
    def stop(self):
        pass

def setup(api):
    api.services.register('answer', instance=Service(), replace=True)
"""
    manager, host, _ = build(tmp_path)

    class Base:
        running = False
        def start(self):
            self.running = True
        def stop(self):
            self.running = False

    base = Base()
    host.register_base('answer', base, managed=True)

    async def run():
        await host.start()
        try:
            assert base.running
            install(tmp_path, source)
            await manager.arefresh()
            assert base.running
            assert host.resolve('answer') is base
            assert not manager._versions
        finally:
            await host.stop()
    asyncio.run(run())


def test_websocket_keeps_generation_until_disconnect(tmp_path):
    source = """
from fastapi import APIRouter, WebSocket

def setup(api):
    router = APIRouter()
    @router.websocket('/socket')
    async def socket(websocket: WebSocket):
        await websocket.accept()
        await websocket.receive_text()
        await websocket.send_text('old')
        await websocket.close()
    api.routes.register(router)
    @api.lifecycle.shutdown
    async def shutdown():
        (api.workspace_root / 'closed').write_text('yes')
"""
    install(tmp_path, source)
    manager, host, app = build(tmp_path)

    async def run():
        await host.start()
        incoming = asyncio.Queue()
        outgoing = asyncio.Queue()
        scope = {'type': 'websocket', 'asgi': {'version': '3.0'}, 'path': '/v1/plugins/sample/socket',
                 'root_path': '', 'query_string': b'', 'headers': [(b'authorization', b'Bearer test')],
                 'scheme': 'ws', 'server': ('test', 80), 'client': ('test', 1), 'subprotocols': []}
        await incoming.put({'type': 'websocket.connect'})
        request = asyncio.create_task(app(scope, incoming.get, outgoing.put))
        try:
            assert (await asyncio.wait_for(outgoing.get(), 2))['type'] == 'websocket.accept'
            install(tmp_path, source, enabled=False)
            await manager.arefresh()
            assert not (tmp_path / 'closed').exists()
            await incoming.put({'type': 'websocket.receive', 'text': 'continue'})
            assert (await asyncio.wait_for(outgoing.get(), 2))['text'] == 'old'
            await asyncio.wait_for(request, 2)
        finally:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            await host.stop()
        assert (tmp_path / 'closed').exists()
    asyncio.run(run())


def test_sync_job_drains_thread_and_pins_peer_services(tmp_path):
    install(tmp_path, """
class Service:
    def value(self):
        return 'peer'
def setup(api):
    api.services.register('peer', instance=Service())
""", plugin_id='peer')
    source = """
import threading
entered = threading.Event()
resume = threading.Event()
def setup(api):
    def job():
        (api.workspace_root / 'peer_result').write_text(api.host.service('peer').value())
        entered.set()
        resume.wait(5)
        (api.workspace_root / 'thread_done').write_text('yes')
    api.jobs.register('blocking', job)
    @api.lifecycle.shutdown
    async def shutdown():
        assert (api.workspace_root / 'thread_done').exists()
        (api.workspace_root / 'closed').write_text('yes')
"""
    install(tmp_path, source)
    manager, host, _ = build(tmp_path)

    async def run():
        await host.start()
        version = manager._versions['sample']
        try:
            assert await asyncio.to_thread(version.module.entered.wait, 2)
            assert (tmp_path / 'peer_result').read_text() == 'peer'
            install(tmp_path, source, enabled=False)
            disabling = asyncio.create_task(manager.arefresh())
            await asyncio.sleep(.02)
            assert not disabling.done()
            assert not (tmp_path / 'closed').exists()
            version.module.resume.set()
            await asyncio.wait_for(disabling, 2)
        finally:
            version.module.resume.set()
            await host.stop()
        assert (tmp_path / 'closed').exists()
    asyncio.run(run())


def test_mounted_supervisor_runs_child_lifespan(tmp_path):
    from contextlib import asynccontextmanager
    from fastapi.testclient import TestClient
    from supervisor.main import _mount_instance

    install(tmp_path, """
from fastapi import APIRouter
def setup(api):
    router = APIRouter()
    @router.get('/health')
    async def health():
        return {'ok': True}
    api.routes.register(router)
    @api.lifecycle.startup
    async def startup():
        (api.workspace_root / 'started').write_text('yes')
    @api.lifecycle.shutdown
    async def shutdown():
        (api.workspace_root / 'closed').write_text('yes')
""")
    manager, host, app = build(tmp_path)

    @asynccontextmanager
    async def lifespan(_):
        await host.start()
        try:
            yield
        finally:
            await host.stop()

    app.router.lifespan_context = lifespan
    with TestClient(_mount_instance(app, '/i/example')) as client:
        assert (tmp_path / 'started').exists()
        assert client.get('/i/example/v1/plugins/sample/health', headers={'authorization': 'Bearer test'}).json() == {'ok': True}
    assert (tmp_path / 'closed').exists()


def test_managed_service_stop_waits_for_old_request_and_errors_stay_owned(tmp_path):
    source = """
import asyncio
from fastapi import APIRouter
entered = asyncio.Event()
resume = asyncio.Event()
class Service:
    def __init__(self):
        self.running = False
        self.stops = 0
    def start(self):
        self.running = True
    def stop(self):
        self.running = False
        self.stops += 1
    def value(self):
        if not self.running:
            raise RuntimeError('service is closed')
        return 'VERSION'
service = Service()
def setup(api):
    api.services.register('answer', instance=service)
    router = APIRouter()
    @router.get('/wait')
    async def wait():
        entered.set()
        await resume.wait()
        return {'value': api.host.service('answer').value()}
    api.routes.register(router)
    @api.lifecycle.shutdown
    async def shutdown():
        if 'VERSION' == 'old':
            raise RuntimeError('old shutdown failed')
"""
    install(tmp_path, source.replace('VERSION', 'old'))
    manager, host, app = build(tmp_path)

    async def run():
        await host.start()
        old = manager._versions['sample']
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test', headers={'authorization': 'Bearer test'}) as client:
                request = asyncio.create_task(client.get('/v1/plugins/sample/wait'))
                await asyncio.wait_for(old.module.entered.wait(), 2)
                install(tmp_path, source.replace('VERSION', 'new'))
                await manager.arefresh()
                assert old.module.service.running
                assert old.module.service.stops == 0
                assert host.service('answer').value() == 'new'
                old.module.resume.set()
                assert (await asyncio.wait_for(request, 2)).json() == {'value': 'old'}
                for _ in range(100):
                    if old.closed:
                        break
                    await asyncio.sleep(.01)
                assert old.module.service.stops == 1
                assert not old.module.service.running
                status = manager.status()['plugins'][0]
                assert status['state'] == 'loaded'
                assert status['error'] is None
                assert status['retired_errors'][0]['phase'] == 'shutdown'
        finally:
            old.module.resume.set()
            await host.stop()
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['provider', 'hook'])
def test_callback_service_snapshot_survives_owner_and_peer_disable(tmp_path, kind):
    install(tmp_path, """
class Service:
    def value(self):
        return 'peer'
def setup(api):
    api.services.register('peer', instance=Service(), managed=False)
""", plugin_id='peer')
    source = """
import asyncio
entered = asyncio.Event()
resume = asyncio.Event()
class Service:
    def value(self):
        return 'owner'
def setup(api):
    api.services.register('answer', instance=Service(), managed=False)
    async def run(values):
        entered.set()
        await resume.wait()
        values.append(api.host.service('answer').value() + api.host.service('peer').value())
    async def request(ctx, value):
        await run(value['body']['values'])
    api.request(request)
    api.register_hook('event', run)
"""
    install(tmp_path, source)
    manager, host, _ = build(tmp_path)

    async def run():
        from engine.hooks.registry import HookRegistry
        from plugin_runtime import provider_request
        await host.start()
        version = manager._versions['sample']
        values = []
        try:
            async def call():
                if kind == 'provider':
                    async with provider_request(provider='test', workspace_root=tmp_path) as request:
                        await request.request({'method': 'POST', 'url': 'https://invalid.test', 'headers': {}, 'body': {'values': values}})
                else:
                    hooks = HookRegistry()
                    hooks.bind_plugins(manager)
                    await hooks.afire('event', values)
            callback = asyncio.create_task(call())
            await asyncio.wait_for(version.module.entered.wait(), 2)
            install(tmp_path, source, enabled=False)
            config = tmp_path / 'config' / 'plugins.yaml'
            settings = yaml.safe_load(config.read_text())
            settings['plugins']['peer']['enabled'] = False
            config.write_text(yaml.safe_dump(settings))
            await manager.arefresh()
            version.module.resume.set()
            await asyncio.wait_for(callback, 2)
            assert values == ['ownerpeer']
        finally:
            version.module.resume.set()
            await host.stop()
    asyncio.run(run())


def test_public_middleware_does_not_consume_auth_attempts(tmp_path, monkeypatch):
    from collections import OrderedDict
    from supervisor import admin_api
    monkeypatch.setattr(admin_api, '_admin_token', 'test-token')
    monkeypatch.setattr(admin_api, '_auth_failures', OrderedDict())
    install(tmp_path, """
from fastapi import APIRouter
def setup(api):
    router = APIRouter()
    @router.get('/check')
    async def check():
        return {'ok': True}
    api.routes.register(router)
    api.middleware.register(lambda app: app, global_scope=True)
""")
    manager, host, app = build(tmp_path)
    app.user_middleware.clear()
    app.add_middleware(PluginDispatcher, host=host, authenticate=admin_api.verify_admin_token)

    @app.get('/web/')
    async def public():
        return {'public': True}

    async def run():
        await host.start()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                for _ in range(6):
                    assert (await client.get('/web/')).status_code == 200
                assert not admin_api._auth_failures
                response = await client.get('/v1/plugins/sample/check', headers={'authorization': 'Bearer test-token'})
                assert response.status_code == 200
        finally:
            await host.stop()
    asyncio.run(run())


def test_service_stream_context_pins_owner_until_exit(tmp_path):
    source = """
from contextlib import asynccontextmanager, contextmanager
class Service:
    closed = False
    @asynccontextmanager
    async def stream(self):
        yield self
        assert not self.closed
    @contextmanager
    def sync_stream(self):
        yield self
        assert not self.closed
    def close(self):
        self.closed = True
def setup(api):
    api.services.register('client', instance=Service())
"""
    install(tmp_path, source)
    manager, host, _ = build(tmp_path)

    async def run():
        await host.start()
        try:
            service = host.service('client')
            with service.sync_stream() as sync:
                async with service.stream() as stream:
                    install(tmp_path, source, enabled=False)
                    await manager.arefresh()
                    assert not stream.closed
                assert not sync.closed
        finally:
            await host.stop()
        assert stream.closed
    asyncio.run(run())


def test_engine_worker_consumes_replaced_tool_and_http_services(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from engine import runner
    install(tmp_path, """
from types import SimpleNamespace
class Client:
    marker = 'plugin'
    issued = False
    async def get(self, url, **kwargs):
        if url.endswith('/v1/tasks/next') and not self.issued:
            self.issued = True
            return SimpleNamespace(status_code=200, json=lambda: {'task_id': 'sample'})
        return SimpleNamespace(status_code=404, json=lambda: {})
class Tools:
    marker = 'plugin'
def setup(api):
    api.services.register('supervisor_http', instance=Client(), replace=True)
    api.services.register('llm_http', instance=Client(), replace=True)
    api.services.register('tools', instance=Tools(), replace=True)
""")

    class BaseClient:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    class BaseTools:
        def __init__(self, **kwargs):
            pass
        async def load_mcp_tools(self):
            return 0

    async def ready(*args, **kwargs):
        return {}

    monkeypatch.setattr(runner.httpx, 'AsyncClient', BaseClient)
    monkeypatch.setattr(runner, '_create_llm_http_client', lambda _: BaseClient())
    monkeypatch.setattr(runner, 'ToolRegistry', BaseTools)
    monkeypatch.setattr(runner, 'wait_supervisor', ready)
    monkeypatch.setattr(runner, '_register_engine', ready)
    monkeypatch.setattr(runner, 'load_runtime_config', lambda _: {})

    async def run():
        observed = asyncio.Event()
        async def handle(http, llm_http, url, root, registry, item, wid):
            assert http.marker == llm_http.marker == registry.marker == 'plugin'
            observed.set()
        monkeypatch.setattr(runner, '_handle_task', handle)
        worker = asyncio.create_task(runner.worker_loop(supervisor_url='http://test', workspace_root=tmp_path))
        try:
            await asyncio.wait_for(observed.wait(), 2)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        assert not get_manager(tmp_path)._versions
    asyncio.run(run())


def test_queued_material_job_keeps_service_generation(tmp_path):
    from supervisor.materials_api import MaterialRuntime
    source = """
class Service:
    def __init__(self, root):
        self.root = root
        self.closed = False
    def run_job(self, actor, job_id):
        assert not self.closed
        (self.root / 'job_version').write_text('VERSION')
    def close(self):
        self.closed = True
def setup(api):
    api.services.register('materials', instance=Service(api.workspace_root))
"""
    install(tmp_path, source.replace('VERSION', 'old'))
    manager, host, _ = build(tmp_path)

    async def run():
        await host.start()
        old = manager._versions['sample']
        runtime = MaterialRuntime(host.service('materials'))
        runtime.slots = asyncio.Semaphore(0)
        try:
            runtime.launch(None, {'id': 'queued'})
            install(tmp_path, source.replace('VERSION', 'new'))
            await manager.arefresh()
            assert not old.closed
            runtime.slots.release()
            await asyncio.gather(*list(runtime.background))
            assert (tmp_path / 'job_version').read_text() == 'old'
        finally:
            runtime.slots.release()
            await asyncio.gather(*list(runtime.background), return_exceptions=True)
            await host.stop()
    asyncio.run(run())


@pytest.mark.parametrize('style', ['dynamic', 'static', 'mount', 'catchall', 'method'])
@pytest.mark.parametrize('replace', [False, True])
def test_route_overlap_requires_explicit_replace(tmp_path, style, replace):
    paths = {
        'dynamic': ('/items/special', '/items/{name}', '/items/special'),
        'static': ('/items/{name}', '/items/special', '/items/special'),
        'mount': ('/mounted', '/mounted/{name}', '/mounted/special'),
        'catchall': ('/items/{name:path}', '/items/special', '/items/special'),
        'method': ('/items/special', '/items/special', '/items/special'),
    }
    base_path, plugin_path, url = paths[style]
    install(tmp_path, f"""
from fastapi import APIRouter
def setup(api):
    router = APIRouter()
    @router.get({plugin_path!r})
    async def item():
        return {{'value': 'plugin'}}
    api.routes.register(router, prefix='', replace={replace})
""")
    manager, host, app = build(tmp_path)

    async def base():
        return {'value': 'base'}
    if style == 'mount':
        child = FastAPI()
        child.add_api_route('/{name:path}', base)
        app.mount(base_path, child)
    else:
        app.add_api_route(base_path, base, methods=['POST'] if style == 'method' else ['GET'])

    async def run():
        await host.start()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                response = await client.get(url, headers={'authorization': 'Bearer test'})
                assert response.json() == {'value': 'plugin' if replace or style == 'method' else 'base'}
        finally:
            await host.stop()
    asyncio.run(run())


@pytest.mark.parametrize('cancel', [False, True])
def test_provider_stream_cross_task_consumption_and_close(tmp_path, cancel):
    from providers.plugin_transport import wrap_client
    from plugin_runtime import plugin_scope
    install(tmp_path, """
class Service:
    def value(self):
        return 'old'
def setup(api):
    api.services.register('answer', instance=Service(), managed=False)
    @api.stream_event
    async def event(ctx, value):
        value['data'] = api.host.service('answer').value()
    @api.end
    async def end(ctx, outcome):
        (api.workspace_root / 'end_value').write_text(api.host.service('answer').value())
""")
    manager, host, _ = build(tmp_path)

    async def run():
        entered = asyncio.Event()
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: original\n\n'
                if cancel:
                    entered.set()
                    await asyncio.Event().wait()
            async def aclose(self):
                pass
        await host.start()
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Stream()))) as client:
                with plugin_scope(workspace_root=tmp_path):
                    wrapped = wrap_client(client, 'test')
                    response = await wrapped.send(client.build_request('POST', 'https://example.test', json={}), stream=True)
                reader = asyncio.create_task(response.aread())
                if cancel:
                    await asyncio.wait_for(entered.wait(), 2)
                    reader.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await reader
                else:
                    assert b'data: old' in await asyncio.wait_for(reader, 2)
                await asyncio.create_task(response.aclose())
            assert (tmp_path / 'end_value').read_text() == 'old'
            assert manager._versions['sample'].active == 0
        finally:
            await asyncio.wait_for(host.stop(), 2)
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['awaitable', 'async_generator', 'generator', 'async_context', 'context', 'thread'])
def test_direct_service_body_keeps_nested_service_snapshot(tmp_path, kind):
    source = """
import asyncio
import threading
from contextlib import asynccontextmanager, contextmanager
entered = asyncio.Event()
resume = asyncio.Event()
thread_entered = threading.Event()
thread_resume = threading.Event()
class Peer:
    def value(self):
        return 'VERSION'
class Service:
    def __init__(self, api):
        self.api = api
    def value(self):
        return self.api.host.service('peer').value()
    async def awaited(self):
        entered.set()
        await resume.wait()
        return self.value()
    async def async_values(self):
        yield 'first'
        yield self.value()
    def values(self):
        yield 'first'
        yield self.value()
    @asynccontextmanager
    async def async_context(self):
        yield 'first'
        (self.api.workspace_root / 'nested').write_text(self.value())
    @contextmanager
    def context(self):
        yield 'first'
        (self.api.workspace_root / 'nested').write_text(self.value())
    def blocked(self):
        thread_entered.set()
        thread_resume.wait(5)
        return self.value()
def setup(api):
    api.services.register('peer', instance=Peer(), managed=False)
    api.services.register('caller', instance=Service(api), managed=False)
"""
    install(tmp_path, source.replace('VERSION', 'old'))
    manager, host, _ = build(tmp_path)

    async def run():
        await host.start()
        old = manager._versions['sample']
        proxy = host.service('caller')
        try:
            if kind == 'awaitable':
                pending = asyncio.create_task(proxy.awaited())
                await asyncio.wait_for(old.module.entered.wait(), 2)
            elif kind == 'async_generator':
                stream = proxy.async_values()
                assert await anext(stream) == 'first'
            elif kind == 'generator':
                stream = proxy.values()
                assert next(stream) == 'first'
            elif kind == 'async_context':
                context = proxy.async_context()
                assert await context.__aenter__() == 'first'
            elif kind == 'context':
                context = proxy.context()
                assert context.__enter__() == 'first'
            else:
                pending = asyncio.create_task(asyncio.to_thread(proxy.blocked))
                assert await asyncio.to_thread(old.module.thread_entered.wait, 2)
            install(tmp_path, source.replace('VERSION', 'new'))
            await manager.arefresh()
            if kind == 'awaitable':
                old.module.resume.set()
                assert await asyncio.wait_for(pending, 2) == 'old'
            elif kind == 'async_generator':
                assert await anext(stream) == 'old'
                await stream.aclose()
            elif kind == 'generator':
                assert next(stream) == 'old'
                stream.close()
            elif kind == 'async_context':
                await asyncio.create_task(context.__aexit__(None, None, None))
                assert (tmp_path / 'nested').read_text() == 'old'
            elif kind == 'context':
                context.__exit__(None, None, None)
                assert (tmp_path / 'nested').read_text() == 'old'
            else:
                old.module.thread_resume.set()
                assert await asyncio.wait_for(pending, 2) == 'old'
            assert host.service('peer').value() == 'new'
        finally:
            old.module.resume.set()
            old.module.thread_resume.set()
            await asyncio.wait_for(host.stop(), 2)
    asyncio.run(run())
