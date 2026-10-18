from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from engine.hooks.registry import HookRegistry
from plugin_runtime import get_manager, plugin_scope, provider_request
from plugin_sdk import PluginExecutionError


def install(root: Path, source: str, plugin_id: str = "sample", **settings):
    directory = root / "config" / "plugins"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{plugin_id}.py").write_text(source, encoding="utf-8")
    path = root / "config" / "plugins.yaml"
    data = yaml.safe_load(path.read_text()) if path.exists() else {"version": 1, "plugins": {}}
    data["plugins"][plugin_id] = {"enabled": True, **settings}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def payload(value=0):
    return {"method": "POST", "url": "https://example.invalid/api", "headers": {}, "body": {"value": value}}


@pytest.fixture
def root(tmp_path):
    yield tmp_path
    get_manager(tmp_path).close()


def test_lifecycle_once_per_version_and_async_request_state(root):
    install(root, '''
import asyncio
def setup(api):
    (api.workspace_root / "setups").open("a").write("1")
    @api.before_request
    async def request(ctx, value):
        ctx.state["value"] = value["body"]["value"]
        await asyncio.sleep(0)
    @api.after_response
    def response(ctx, value):
        value["body"]["value"] = ctx.state["value"]
    @api.on_request_end
    def end(ctx, outcome):
        (api.workspace_root / "ends").open("a").write(str(ctx.state["value"]))
def teardown(api):
    (api.workspace_root / "teardowns").open("a").write("1")
''')

    async def one(value):
        with plugin_scope(workspace_root=root):
            async with provider_request(provider="openai") as request:
                assert request.active
                await request.request(payload(value))
                return await request.response({"status_code": 200, "headers": {}, "body": {}})

    async def run():
        return await asyncio.gather(one(1), one(2), one(3))

    assert [value["body"]["value"] for value in asyncio.run(run())] == [1, 2, 3]
    assert (root / "setups").read_text() == "1"
    assert sorted((root / "ends").read_text()) == ["1", "2", "3"]
    get_manager(root).close()
    assert (root / "teardowns").read_text() == "1"


def test_disable_pins_inflight_version_until_end(root):
    source = '''
def setup(api):
    @api.request
    def request(ctx, value):
        value["body"]["version"] = 1
    @api.end
    def end(ctx, outcome):
        (api.workspace_root / "ended").write_text("yes")
def teardown(api):
    assert (api.workspace_root / "ended").exists()
    (api.workspace_root / "closed").write_text("yes")
'''
    install(root, source)

    async def run():
        with plugin_scope(workspace_root=root):
            async with provider_request(provider="openai") as request:
                install(root, source, enabled=False)
                get_manager(root).refresh()
                assert not (root / "closed").exists()
                async with provider_request(provider="openai") as new:
                    assert not new.active
                assert (await request.request(payload()))["body"]["version"] == 1
            assert (root / "closed").exists()

    asyncio.run(run())


def test_failed_setup_rolls_back_owned_hooks_and_tears_down(root):
    install(root, '''
def setup(api):
    api.register_hook("before_step", lambda ctx: ctx.append("leaked"))
    raise RuntimeError("setup failed")
def teardown(api):
    (api.workspace_root / "rollback").write_text("done")
''')
    registry = HookRegistry()
    registry.register("before_step", lambda ctx: ctx.append("builtin"))
    registry.bind_plugins(get_manager(root))
    values = []
    registry.fire("before_step", values)
    assert values == ["builtin"]
    assert (root / "rollback").read_text() == "done"
    assert get_manager(root).status()["plugins"][0]["state"] == "error"


def test_legacy_ownership_and_disable_preserve_same_name_builtin(root):
    source = '''
def same(ctx):
    ctx.append("plugin")
def register(registry):
    registry.register("event", same)
'''
    install(root, source)
    registry = HookRegistry()

    def same(ctx):
        ctx.append("builtin")

    same.name = "same"
    registry.register("event", same)
    registry.bind_plugins(get_manager(root))
    values = []
    registry.fire("event", values)
    assert values == ["builtin", "plugin"]
    install(root, source, enabled=False)
    registry.fire("event", values)
    assert values == ["builtin", "plugin", "builtin"]


def test_reload_keeps_old_stream_callbacks_and_new_request_version(root):
    template = '''
def setup(api):
    @api.stream_event
    def event(ctx, value):
        value["data"] += "VERSION"
        return [value, {"data": "tail"}]
def teardown(api):
    (api.workspace_root / "closed_VERSION").write_text("yes")
'''
    install(root, template.replace("VERSION", "one"))

    async def run():
        with plugin_scope(workspace_root=root):
            async with provider_request(provider="openai", streaming=True) as old:
                install(root, template.replace("VERSION", "two"))
                async with provider_request(provider="openai", streaming=True) as new:
                    assert (await new.stream_event({"data": "x"}))[0]["data"] == "xtwo"
                    assert (await old.stream_event({"data": "x"}))[0]["data"] == "xone"
                assert not (root / "closed_one").exists()
            assert (root / "closed_one").exists()

    asyncio.run(run())


def test_scope_filters_are_conjunctive(root):
    install(root, "def setup(api):\n    api.request(lambda ctx, value: None)\n", selectors={
        "nodes": ["system.compactor"], "channels": ["backup"], "providers": ["openai"], "purposes": ["fallback"],
    })

    async def run():
        with plugin_scope(workspace_root=root, node_id="system.compactor", channel="backup", purpose="fallback"):
            async with provider_request(provider="openai") as request:
                assert request.active
            async with provider_request(provider="anthropic") as request:
                assert not request.active
            with plugin_scope(purpose="inference"):
                async with provider_request(provider="openai") as request:
                    assert not request.active

    asyncio.run(run())


def test_cancellation_runs_end_and_releases(root):
    install(root, '''
import asyncio
import json
def setup(api):
    @api.request
    async def request(ctx, value):
        raise asyncio.CancelledError()
    @api.end
    async def end(ctx, outcome):
        await asyncio.sleep(0)
        (api.workspace_root / "outcome").write_text(json.dumps(outcome))
''')

    async def run():
        with plugin_scope(workspace_root=root):
            with pytest.raises(asyncio.CancelledError):
                async with provider_request(provider="openai") as request:
                    await request.request(payload())

    asyncio.run(run())
    assert json.loads((root / "outcome").read_text())["cancelled"] is True
    assert get_manager(root).status()["plugins"][0]["active_requests"] == 0


def test_callback_failure_is_fail_closed_and_status_redacts_secret(root):
    install(root, '''
def setup(api):
    @api.request
    def request(ctx, value):
        raise RuntimeError(value["headers"]["Authorization"])
    @api.end
    def end(ctx, outcome):
        (api.workspace_root / "failed_end").write_text(str(outcome["ok"]))
''')

    async def run():
        with plugin_scope(workspace_root=root):
            with pytest.raises(PluginExecutionError, match="RuntimeError") as error:
                async with provider_request(provider="openai") as request:
                    value = payload()
                    value["headers"]["Authorization"] = "Bearer private-token-abc"
                    await request.request(value)
            assert "private-token" not in str(error.value)

    asyncio.run(run())
    status = get_manager(root).status()
    assert "private-token" not in json.dumps(status)
    assert status["plugins"][0]["error_details"]["frames"][-1]["function"] == "request"
    assert (root / "failed_end").read_text() == "False"


def test_invalid_callback_result_fails_request(root):
    install(root, "def setup(api):\n    api.request(lambda ctx, value: {'body': {}})\n")

    async def run():
        with plugin_scope(workspace_root=root):
            with pytest.raises(PluginExecutionError, match="InvalidReturnType"):
                async with provider_request(provider="openai") as request:
                    await request.request(payload())

    asyncio.run(run())
    assert get_manager(root).status()["plugins"][0]["active_requests"] == 0


def test_registry_scopes_do_not_cross_concurrent_nodes():
    registry = HookRegistry()

    async def node(name):
        with registry.scoped():
            registry.register("event", lambda ctx: ctx.append(name))
            await asyncio.sleep(0)
            result = []
            await registry.afire("event", result)
            return result

    async def run():
        return await asyncio.gather(node("left"), node("right"))

    assert asyncio.run(run()) == [["left"], ["right"]]
    assert registry.list_hooks() == {}


def test_descriptor_does_not_retain_disabled_owner(root):
    source = "def register(registry):\n    registry.register('event', lambda ctx: ctx.append('plugin'))\n"
    install(root, source)
    registry = HookRegistry()
    registry.bind_plugins(get_manager(root))
    descriptor = registry.handlers_for("event")[0]
    install(root, source, enabled=False)
    result = []
    descriptor["callback"](result)
    assert result == []


def test_process_scope_skips_import_entirely(root):
    install(root, '''
PLUGIN_META = {"processes": ["engine"]}
raise RuntimeError("must not import in supervisor")
def setup(api):
    pass
''')
    manager = get_manager(root, process="supervisor")
    status = manager.status()
    assert status["plugins"][0]["state"] == "not_applicable"
    assert status["plugins"][0]["error"] is None
    assert status["applied_generation"] == status["desired_generation"]


def test_async_legacy_hook_pins_version_through_disable(root):
    source = '''
import asyncio
entered = asyncio.Event()
resume = asyncio.Event()
async def handler(ctx):
    entered.set()
    await resume.wait()
    ctx.append("completed")
def register(registry):
    registry.register("event", handler)
def teardown(api):
    (api.workspace_root / "hook_closed").write_text("yes")
'''
    install(root, source)
    registry = HookRegistry()
    manager = get_manager(root)
    registry.bind_plugins(manager)
    manager.refresh()
    module = manager._versions["sample"].module

    async def run():
        values = []
        running = asyncio.create_task(registry.afire("event", values))
        await module.entered.wait()
        install(root, source, enabled=False)
        manager.refresh()
        assert not (root / "hook_closed").exists()
        module.resume.set()
        await running
        assert values == ["completed"]
        assert (root / "hook_closed").exists()

    asyncio.run(run())


def test_retiring_version_error_does_not_mark_replacement_failed(root):
    source = '''
def setup(api):
    @api.request
    def request(ctx, value):
        raise ValueError("old callback failed")
'''
    install(root, source)

    async def run():
        with plugin_scope(workspace_root=root):
            async with provider_request(provider="openai") as old:
                install(root, "def setup(api):\n    api.request(lambda ctx, value: None)\n")
                async with provider_request(provider="openai") as current:
                    await current.request(payload())
                with pytest.raises(PluginExecutionError):
                    await old.request(payload())
            status = get_manager(root).status()
            assert status["plugins"][0]["state"] == "loaded"
            assert status["plugins"][0]["error"] is None
            assert status["plugins"][0]["retired_errors"][0]["type"] == "ValueError"
            assert status["desired_generation"] == status["applied_generation"]

    asyncio.run(run())


def test_middleware_order_priority_then_plugin_id(root):
    template = '''
PLUGIN_META = {"priority": PRIORITY}
def setup(api):
    @api.request
    def request(ctx, value):
        value["body"]["order"].append(ctx.plugin_id)
'''
    install(root, template.replace("PRIORITY", "10"), "early_name")
    install(root, template.replace("PRIORITY", "200"), "z_high")
    install(root, template.replace("PRIORITY", "200"), "a_high")

    async def run():
        with plugin_scope(workspace_root=root):
            async with provider_request(provider="openai") as request:
                value = payload()
                value["body"]["order"] = []
                return (await request.request(value))["body"]["order"]

    assert asyncio.run(run()) == ["a_high", "z_high", "early_name"]


def test_invalid_shared_settings_retain_last_applied_generation(root):
    source = "def setup(api):\n    api.request(lambda ctx, value: None)\n"
    install(root, source)
    manager = get_manager(root)
    initial = manager.status()
    (root / "config" / "plugins.yaml").write_text("plugins: [invalid: yaml", encoding="utf-8")
    manager.heartbeat()
    status = manager.status()
    assert status["configuration_error"]["phase"] == "configuration"
    assert status["applied_generation"] == initial["applied_generation"]
    assert status["desired_generation"].startswith("invalid:")
    versions = manager.acquire({})
    assert len(versions) == 1
    manager.release(versions)
    (root / "config" / "plugins.yaml").unlink()
    install(root, source)
    assert manager.status()["configuration_error"] is None
