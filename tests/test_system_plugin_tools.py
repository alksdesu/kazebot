from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugin_runtime import PluginHost, get_manager, plugin_scope
from plugin_runtime.store import PluginStore
from supervisor.tool_catalog import tool_catalog
from toolbox.registry import ToolRegistry


def install(root: Path, name="sample", *, replace=False, priority=100, extra="", tool_name="plugin_echo", body=None):
    body = body or 'return {"ok": True, "data": {"result": str(args.get("value", "plugin"))}}'
    source = f'''PLUGIN_META = {{"name": {name!r}, "priority": {priority}, "processes": ["engine"]}}
{extra}
def setup(api):
    async def echo(args, ctx):
        {body}
    api.tools.register({{"name": {tool_name!r}, "description": {name!r}, "input_schema": {{"type": "object"}}}}, echo, replace={replace!r})
'''
    store = PluginStore(root)
    entry = store.install(name + ".py", source)
    store.configure(name, enabled=True, config={}, selectors={}, base_hash=entry["desired_hash"])
    return store


def disable(store: PluginStore, name="sample"):
    item = store.get(name)
    store.configure(name, enabled=False, config={}, selectors={}, base_hash=item["desired_hash"])


@pytest.fixture
def root(tmp_path):
    yield tmp_path
    get_manager(tmp_path).close()


def registry(root):
    return ToolRegistry(workspace_root=root, tools_dir=root / "tools")


def execute(registry, name="plugin_echo", ctx=None):
    return asyncio.run(registry.execute(name=name, arguments={"value": "hello"}, ctx=ctx or SimpleNamespace(node_id="chat")))


def test_plugin_tool_register_execute_and_unload(root):
    store = install(root)
    tools = registry(root)
    assert tools.get_spec("plugin_echo")["description"] == "sample"
    assert execute(tools)["data"]["result"] == "hello"
    assert tools.reserved_name_reason("plugin_echo") == "taken by an application plugin"
    tools.reload()
    assert tools.get_spec("plugin_echo") is not None
    disable(store)
    assert tools.get_spec("plugin_echo") is None
    assert execute(tools)["ok"] is False
    assert tools.reserved_name_reason("plugin_echo") is None


def test_explicit_override_restores_original_after_disable(root):
    store = install(root, tool_name="base", replace=True)
    tools = registry(root)

    async def original(args, ctx):
        return {"ok": True, "data": {"result": "base"}}

    tools.register_builtin_tool("base", "original", {}, original)
    assert execute(tools, "base")["data"]["result"] == "hello"
    assert tools.get_spec("base")["description"] == "sample"
    assert next(spec for spec in tools.builtin_specs() if spec["name"] == "base")["description"] == "original"
    disable(store)
    tools.reload()
    assert execute(tools, "base")["data"]["result"] == "base"


def test_undeclared_override_does_not_shadow_base(root):
    install(root, tool_name="list_dir")
    tools = registry(root)
    assert tools.get_spec("list_dir")["description"] != "sample"
    assert "explicit replace" in get_manager(root).status()["plugins"][0]["error"]


def test_engine_control_tool_cannot_be_shadowed(root):
    install(root, tool_name="finish", replace=True)
    tools = registry(root)
    assert tools.get_spec("finish") is None
    assert "engine protocol" in get_manager(root).status()["plugins"][0]["error"]


def test_priority_and_disable_reveal_previous_layer(root):
    store = install(root, "lower", priority=10, replace=True)
    install(root, "higher", priority=90, replace=True)
    tools = registry(root)
    assert tools.get_spec("plugin_echo")["description"] == "higher"
    disable(store, "higher")
    assert tools.get_spec("plugin_echo")["description"] == "lower"
    disable(store, "lower")
    assert tools.get_spec("plugin_echo") is None


def test_async_execution_holds_old_version_through_disable(root):
    store = install(root, extra='def teardown(api):\n    (api.workspace_root / "closed").touch()', body='ctx.started.set(); await ctx.resume.wait(); return {"ok": True, "data": {"result": "done"}}')
    tools = registry(root)

    async def run():
        ctx = SimpleNamespace(started=asyncio.Event(), resume=asyncio.Event(), node_id="chat")
        task = asyncio.create_task(tools.execute(name="plugin_echo", arguments={}, ctx=ctx))
        await asyncio.wait_for(ctx.started.wait(), 2)
        disable(store)
        assert tools.get_spec("plugin_echo") is None
        assert not (root / "closed").exists()
        ctx.resume.set()
        assert (await task)["data"]["result"] == "done"
        assert (root / "closed").exists()

    asyncio.run(run())


def test_cancelled_sync_handler_keeps_lease_until_thread_finishes(root):
    source = '''def setup(api):
    def tool(args, ctx):
        ctx.started.set()
        ctx.resume.wait(timeout=5)
        return {"ok": True, "data": {"result": "finished"}}
    api.tools.register({"name": "sync_tool"}, tool)
def teardown(api):
    (api.workspace_root / "closed").touch()
'''
    store = PluginStore(root)
    item = store.install("sample.py", source)
    store.configure("sample", enabled=True, config={}, selectors={}, base_hash=item["desired_hash"])
    tools = registry(root)

    async def run():
        ctx = SimpleNamespace(started=threading.Event(), resume=threading.Event(), node_id="chat")
        task = asyncio.create_task(tools.execute(name="sync_tool", arguments={}, ctx=ctx))
        await asyncio.to_thread(ctx.started.wait, 2)
        task.cancel()
        disable(store)
        get_manager(root).refresh()
        await asyncio.sleep(0)
        assert not (root / "closed").exists()
        assert not task.done()
        ctx.resume.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (root / "closed").exists()

    asyncio.run(run())


def test_node_scope_applies_to_standalone_origin(root):
    store = install(root)
    item = store.get("sample")
    store.configure("sample", enabled=True, config={}, selectors={"nodes": ["allowed"]}, base_hash=item["desired_hash"])
    tools = registry(root)
    with plugin_scope(node_id="other"):
        assert tools.get_spec("plugin_echo") is None
    assert execute(tools, ctx=SimpleNamespace(node_id="other"))["ok"] is False
    assert execute(tools, ctx=SimpleNamespace(node_id="tool", origin_node_id="allowed"))["ok"] is True


def test_plugin_guard_uses_existing_policy_adapter(root, monkeypatch):
    source = '''def setup(api):
    async def tool(args, ctx):
        raise AssertionError("must not execute")
    api.tools.register({"name": "guarded", "guard": {"op": "write_file"}}, tool)
'''
    store = PluginStore(root)
    item = store.install("sample.py", source)
    store.configure("sample", enabled=True, config={}, selectors={}, base_hash=item["desired_hash"])

    async def denied(guard, name, args, ctx):
        assert name == "guarded" and guard["op"] == "write_file"
        return {"ok": False, "error": "denied"}

    monkeypatch.setattr("toolbox.registry._script_guard_error", denied)
    assert execute(registry(root), "guarded")["error"] == "denied"


def test_catalog_reads_running_engine_without_importing_plugins(root):
    store = install(root, extra='raise RuntimeError("listing must not import")')
    assert not any(entry.plugin_id == "sample" for entry in tool_catalog(root))
    item = store.get("sample")
    source = store.source("sample")["content"].replace('raise RuntimeError("listing must not import")', '')
    store.update_source("sample", source, item["source_hash"])
    get_manager(root, process="engine").refresh()
    entry = next(entry for entry in tool_catalog(root) if entry.name == "plugin_echo")
    assert entry.plugin_id == "sample" and entry.source == "plugin" and not entry.editable
    disable(store)
    get_manager(root).refresh()
    assert not any(entry.name == "plugin_echo" for entry in tool_catalog(root))


def test_tool_service_snapshot_survives_disable(root):
    source = '''class Answer:
    def value(self):
        return "leased answer"
def setup(api):
    api.services.register("answer", instance=Answer(), managed=False)
    async def tool(args, ctx):
        ctx.started.set()
        await ctx.resume.wait()
        return {"ok": True, "data": {"result": api.host.service("answer").value()}}
    api.tools.register({"name": "leased_tool"}, tool)
'''
    store = PluginStore(root)
    item = store.install("sample.py", source)
    store.configure("sample", enabled=True, config={}, selectors={}, base_hash=item["desired_hash"])
    manager = get_manager(root, process="engine")
    host = PluginHost(manager)

    async def run():
        await host.start()
        try:
            tools = registry(root)
            ctx = SimpleNamespace(started=asyncio.Event(), resume=asyncio.Event(), node_id="chat")
            task = asyncio.create_task(tools.execute(name="leased_tool", arguments={}, ctx=ctx))
            await asyncio.wait_for(ctx.started.wait(), 2)
            disable(store)
            await manager.arefresh()
            ctx.resume.set()
            assert (await task)["data"]["result"] == "leased answer"
        finally:
            await host.stop()

    asyncio.run(run())
