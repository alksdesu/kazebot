from __future__ import annotations

import asyncio
import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, HTTPException

from plugin_runtime import PluginHost, get_manager
from plugin_runtime.host import PluginDispatcher
from plugin_runtime.store import PluginStore
from toolbox.registry import ToolRegistry


def example_zip():
    directory = Path(__file__).resolve().parents[1] / "examples/plugins/system-workspace"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in ("plugin.json", "backend.py", "frontend.js", "style.css"):
            archive.write(directory / name, name)
    return buffer.getvalue()


def test_shipped_package_routes_jobs_tools_and_disable(tmp_path):
    store = PluginStore(tmp_path)
    plugin = store.install_package("system-workspace.zip", example_zip())
    assert plugin["enabled"] is False
    store.configure("system-workspace", enabled=True, config={"interval": 1}, selectors={}, base_hash=plugin["desired_hash"])
    frontend = store.frontend_catalog()["plugins"][0]
    assert frontend["entry"]["path"] == "frontend.js"
    assert frontend["config"]["workspace_label"] == "插件工作区"

    async def supervisor_run():
        manager = get_manager(tmp_path, process="supervisor")
        app = FastAPI()
        host = PluginHost(manager, app=app)

        def authenticate(request):
            if request.headers.get("authorization") != "Bearer sample-test":
                raise HTTPException(401)

        app.add_middleware(PluginDispatcher, host=host, authenticate=authenticate)
        await host.start()
        try:
            counter = host.resolve("system-workspace.counter")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                path = "/v1/plugins/system-workspace/status"
                assert (await client.get(path)).status_code == 401
                client.headers["Authorization"] = "Bearer sample-test"
                for _ in range(50):
                    result = await client.get(path)
                    if result.json()["ticks"]:
                        break
                    await asyncio.sleep(0.02)
                assert result.json()["ticks"] >= 1
                assert result.json()["process"] == "supervisor"
                assert (await client.post("/v1/plugins/system-workspace/reset")).json()["last_event"] == "reset"
                item = store.get("system-workspace")
                store.configure("system-workspace", enabled=False, config={}, selectors={}, base_hash=item["desired_hash"])
                await manager.arefresh()
                count = counter.ticks
                assert (await client.get(path)).status_code == 404
                await asyncio.sleep(0.05)
                assert counter.ticks == count
        finally:
            await host.stop()

    async def engine_run():
        item = store.get("system-workspace")
        store.configure("system-workspace", enabled=True, config={}, selectors={}, base_hash=item["desired_hash"])
        manager = get_manager(tmp_path, process="engine")
        host = PluginHost(manager)
        await host.start()
        try:
            registry = ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools")
            result = await registry.execute(name="system_plugin_echo", arguments={"text": "working"}, ctx=SimpleNamespace(node_id="chat"))
            assert result["data"]["result"] == "working"
            item = store.get("system-workspace")
            store.configure("system-workspace", enabled=False, config={}, selectors={}, base_hash=item["desired_hash"])
            await manager.arefresh()
            assert registry.get_spec("system_plugin_echo") is None
        finally:
            await host.stop()

    asyncio.run(supervisor_run())
    asyncio.run(engine_run())
