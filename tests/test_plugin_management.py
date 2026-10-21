from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config_layout
from plugin_runtime.store import PluginStore, inspect_source
from supervisor import admin_api
from supervisor.plugin_api import create_plugin_router, runtime_status


def _fresh() -> str:
    return datetime.now(timezone.utc).isoformat()


SOURCE = '''PARAMS_SCHEMA = [
    {"key": "prefix", "type": "text", "default": "test"},
    {"key": "count", "type": "integer", "default": 1, "minimum": 0},
]
PLUGIN_META = {"name": "Demo", "version": "1.0", "params_schema": PARAMS_SCHEMA}
def setup(api):
    pass
'''


def test_upload_inspects_literals_without_executing_code(tmp_path):
    marker = tmp_path / "must-not-exist"
    source = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n" + SOURCE
    store = PluginStore(tmp_path)
    entry = store.install("plugin(10.18).py", source)
    assert entry["id"] == "plugin_10_18"
    assert entry["enabled"] is False
    assert entry["config"] == {"prefix": "test", "count": 1}
    assert entry["params_schema"][0]["key"] == "prefix"
    assert not marker.exists()
    store.definitions()
    store.source(entry["id"])
    assert not marker.exists()


def test_invalid_upload_does_not_write(tmp_path):
    store = PluginStore(tmp_path)
    for filename, source in [("../escape.py", SOURCE), ("bad.py", "def broken("), ("no_entry.py", "x = 1")]:
        with pytest.raises(ValueError):
            store.install(filename, source)
    assert not store.directory.exists()
    assert not store.settings_path.exists()


def test_update_hash_conflicts_and_validation_preserve_files(tmp_path):
    store = PluginStore(tmp_path)
    installed = store.install("example.py", SOURCE)
    changed = store.update_source("example", SOURCE.replace('"1.0"', '"2.0"'), installed["source_hash"])
    with pytest.raises(config_layout.ConfigConflict):
        store.update_source("example", SOURCE, installed["source_hash"])
    with pytest.raises(config_layout.ConfigConflict):
        store.configure("example", enabled=True, config={}, selectors={}, base_hash=installed["desired_hash"])
    with pytest.raises(ValueError):
        store.update_source("example", "class invalid(", changed["source_hash"])
    assert store.get("example")["metadata"]["version"] == "2.0"


def test_config_defaults_validation_and_selectors(tmp_path):
    store = PluginStore(tmp_path)
    entry = store.install("example.py", SOURCE)
    for config in ({"count": True}, {"count": -1}, {"prefix": 3}, {"count": float("nan")}):
        with pytest.raises(ValueError):
            store.configure("example", enabled=True, config=config, selectors={}, base_hash=entry["desired_hash"])
    enabled = store.configure("example", enabled=True, config={"prefix": "hello"}, selectors={"nodes": ["chat", "chat"]}, base_hash=entry["desired_hash"])
    assert enabled["config"] == {"prefix": "hello", "count": 1}
    assert enabled["selectors"]["nodes"] == ["chat"]
    assert enabled["desired_hash"] != entry["desired_hash"]
    assert store.generation()


def test_bundled_plugins_stay_readonly_and_toggle_per_workspace(tmp_path):
    directory = tmp_path / "plugins"
    directory.mkdir()
    (directory / "example.py").write_text(SOURCE)
    store = PluginStore(tmp_path)
    item = store.get("example")
    assert item["source"] == "bundled" and item["enabled"]
    for operation in (
        lambda: store.update_source("example", SOURCE, item["source_hash"]),
        lambda: store.remove("example", item["desired_hash"]),
    ):
        with pytest.raises(PermissionError):
            operation()
    changed = store.configure("example", enabled=False, config={}, selectors={}, base_hash=item["desired_hash"])
    assert not changed["enabled"]
    assert (directory / "example.py").read_text() == SOURCE


def test_deleted_override_does_not_enable_bundled_plugin(tmp_path):
    store = PluginStore(tmp_path)
    item = store.install("example.py", SOURCE)
    item = store.configure("example", enabled=True, config={}, selectors={}, base_hash=item["desired_hash"])
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "example.py").write_text(SOURCE)
    store.remove("example", item["desired_hash"])
    assert store.get("example")["source"] == "bundled"
    assert store.get("example")["enabled"] is False


def test_reinstall_is_disabled(tmp_path):
    store = PluginStore(tmp_path)
    item = store.install("example.py", SOURCE)
    item = store.configure("example", enabled=True, config={}, selectors={}, base_hash=item["desired_hash"])
    store.remove("example", item["desired_hash"])
    assert not store.install("example.py", SOURCE)["enabled"]


def test_broken_external_source_remains_visible(tmp_path):
    store = PluginStore(tmp_path)
    store.install("example.py", SOURCE)
    (store.directory / "example.py").write_text("class broken(")
    item = store.public(store.get("example"))
    assert item["id"] == "example"
    assert "语法错误" in item["error"]


def test_invalid_config_is_not_replaced(tmp_path):
    store = PluginStore(tmp_path)
    store.settings_path.parent.mkdir()
    store.settings_path.write_text("plugins: [broken]")
    with pytest.raises(ValueError):
        store.install("example.py", SOURCE)
    assert store.settings_path.read_text() == "plugins: [broken]"


def test_plugin_directory_symlink_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "workspace"
    (root / "config").mkdir(parents=True)
    try:
        (root / "config" / "plugins").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    with pytest.raises(ValueError):
        PluginStore(root).install("example.py", SOURCE)
    assert list(outside.iterdir()) == []


def test_config_share_group_directory_is_supported(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    first, second = tmp_path / "first", tmp_path / "second"
    for root in (first, second):
        root.mkdir()
        try:
            (root / "config").symlink_to(shared, target_is_directory=True)
        except OSError:
            pytest.skip("directory symlinks unavailable")
    first_store, second_store = PluginStore(first), PluginStore(second)
    item = first_store.install("example.py", SOURCE)
    assert second_store.get("example")["desired_hash"] == item["desired_hash"]
    second_store.configure("example", enabled=True, config={}, selectors={}, base_hash=item["desired_hash"])
    assert first_store.get("example")["enabled"]


def test_api_auth_conflicts_and_full_entry_responses(tmp_path, monkeypatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", "plugin-test")
    monkeypatch.setattr(admin_api, "_admin_token", "")
    app = FastAPI()
    app.include_router(create_plugin_router(tmp_path), prefix="/plugins")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/plugins")).status_code == 401
            client.headers["Authorization"] = "Bearer plugin-test"
            response = await client.post("/plugins", json={"filename": "example.py", "content": SOURCE})
            assert response.status_code == 200, response.text
            item = response.json()
            assert item["name"] == "Demo" and item["enabled"] is False
            assert (await client.post("/plugins", json={"filename": "example.py", "content": SOURCE})).status_code == 409
            changed = await client.put("/plugins/example/config", json={"enabled": True, "config": {}, "selectors": {}, "base_hash": item["desired_hash"]})
            assert changed.status_code == 200, changed.text
            assert changed.json()["enabled"]
            assert (await client.delete("/plugins/example", params={"base_hash": item["desired_hash"]})).status_code == 409
            assert (await client.get("/plugins/example/source")).json()["content"] == SOURCE
            assert (await client.get("/plugins")).json()["plugins"][0]["enabled"]
            assert (await client.delete("/plugins/example", params={"base_hash": changed.json()["desired_hash"]})).status_code == 200
            assert (await client.get("/plugins/example/source")).status_code == 404
    asyncio.run(scenario())


def test_stopped_runtime_not_reported_as_loaded(tmp_path, monkeypatch):
    directory = tmp_path / "data" / "plugin_runtime"
    directory.mkdir(parents=True)
    (directory / "1.json").write_text(json.dumps({"pid": 1, "process": "engine", "updated_at": _fresh(), "plugins": [{"id": "example", "state": "loaded", "desired_hash": "new", "applied_hash": "old"}]}))
    monkeypatch.setattr("supervisor.plugin_api._pid_alive", lambda _: False)
    state = runtime_status(tmp_path)[0]
    assert state["alive"] is False
    assert state["plugins"][0]["status"] == "stopped"
    assert state["plugins"][0]["desired_hash"] == "old"


def test_runtime_status_filters_stale_processes(tmp_path, monkeypatch):
    directory = tmp_path / "data" / "plugin_runtime"
    directory.mkdir(parents=True)
    stale = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
    (directory / "111.json").write_text(json.dumps({"pid": 111, "process": "engine", "updated_at": stale, "plugins": []}))
    (directory / "222.json").write_text(json.dumps({"pid": 222, "process": "engine", "updated_at": _fresh(), "plugins": []}))
    monkeypatch.setattr("supervisor.plugin_api._pid_alive", lambda _: True)
    result = runtime_status(tmp_path)
    assert [entry["pid"] for entry in result] == [222]
    assert result[0]["alive"] is True


def test_prune_runtime_dir_removes_only_stale(tmp_path):
    from plugin_runtime.manager import _prune_runtime_dir
    directory = tmp_path / "data" / "plugin_runtime"
    directory.mkdir(parents=True)
    stale = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
    keep = directory / "1.json"
    keep.write_text(json.dumps({"pid": 1, "updated_at": stale}))
    dead = directory / "2.json"
    dead.write_text(json.dumps({"pid": 2, "updated_at": stale}))
    alive = directory / "3.json"
    alive.write_text(json.dumps({"pid": 3, "updated_at": _fresh()}))
    corrupt = directory / "4.json"
    corrupt.write_text("{ not json")
    _prune_runtime_dir(directory, keep=keep)
    assert keep.exists() and alive.exists()
    assert not dead.exists()
    assert not corrupt.exists()


def test_write_status_prunes_dead_runtime_files(tmp_path):
    from plugin_runtime.manager import PluginManager
    directory = tmp_path / "data" / "plugin_runtime"
    directory.mkdir(parents=True)
    stale = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
    (directory / "99999.json").write_text(json.dumps({"pid": 99999, "updated_at": stale, "plugins": []}))
    manager = PluginManager(tmp_path)
    with manager._lock:
        manager._write_status_locked()
    assert (directory / f"{os.getpid()}.json").exists()
    assert not (directory / "99999.json").exists()


def test_static_metadata_unpack_and_dynamic_metadata_rejection(tmp_path):
    source = 'BASE = {"name": "shared", "processes": ["engine"]}\nPLUGIN_META = {**BASE, "version": "2"}\ndef setup(api):\n    pass\n'
    assert inspect_source(source, "example.py") == {"name": "shared", "version": "2", "processes": ["engine"]}
    for metadata in ('make_metadata()', '{**unknown}', '{**[]}'):
        with pytest.raises(ValueError, match="静态"):
            inspect_source(f"PLUGIN_META = {metadata}\ndef setup(api):\n    pass\n", "example.py")
    for processes in ('[]', '["worker"]', '"engine"'):
        with pytest.raises(ValueError, match="processes"):
            inspect_source(f'PLUGIN_META = {{"processes": {processes}}}\ndef setup(api):\n    pass\n', "example.py")


@pytest.mark.parametrize("metadata", [
    {"priority": True}, {"priority": "high"}, {"params_schema": {}},
    {"params_schema": [{"key": ["invalid"]}]},
    {"params_schema": [{"key": "a", "options": {}}]},
    {"params_schema": [{"key": "a", "label": {}}]},
    {"params_schema": [{"key": "a"}, {"key": "a"}]},
])
def test_malformed_plugin_metadata_rejected_before_install(tmp_path, metadata):
    source = f"PLUGIN_META = {metadata!r}\ndef setup(api):\n    pass\n"
    with pytest.raises(ValueError):
        PluginStore(tmp_path).install("invalid.py", source)
    assert not (tmp_path / "config" / "plugins" / "invalid.py").exists()


@pytest.mark.parametrize("existing_settings", [False, True])
def test_failed_upload_preserves_original_settings(tmp_path, monkeypatch, existing_settings):
    store = PluginStore(tmp_path)
    previous = b'version: 1\nplugins:\n  example:\n    enabled: true\n    config: {preserve: "value"}\n'
    if existing_settings:
        store.settings_path.parent.mkdir(parents=True)
        store.settings_path.write_bytes(previous)

    def fail(*args, **kwargs):
        raise OSError("source write failed")

    monkeypatch.setattr(config_layout, "write_config_text", fail)
    with pytest.raises(OSError, match="source write failed"):
        store.install("example.py", SOURCE)
    if existing_settings:
        assert store.settings_path.read_bytes() == previous
    else:
        assert not store.settings_path.exists()
    assert not (store.directory / "example.py").exists()


def test_failed_delete_preserves_saved_config_and_source(tmp_path, monkeypatch):
    store = PluginStore(tmp_path)
    item = store.install("example.py", SOURCE)
    item = store.configure("example", enabled=True, config={"prefix": "keep"}, selectors={"nodes": ["chat"]}, base_hash=item["desired_hash"])
    previous = store.settings_path.read_bytes()

    def conflict(*args, **kwargs):
        raise config_layout.ConfigConflict("source changed")

    monkeypatch.setattr(config_layout, "remove_config_file", conflict)
    with pytest.raises(config_layout.ConfigConflict, match="source changed"):
        store.remove("example", item["desired_hash"])
    assert store.settings_path.read_bytes() == previous
    assert store.source("example")["content"] == SOURCE
    assert store.get("example")["desired_hash"] == item["desired_hash"]


def test_invalid_plugin_config_can_be_disabled(tmp_path):
    store = PluginStore(tmp_path)
    item = store.install("example.py", SOURCE)
    changed = store.update_source("example", SOURCE.replace('"default": 1', '"default": -1'), item["source_hash"])
    assert changed["error"]
    disabled = store.configure("example", enabled=False, config={"count": -2}, selectors={}, base_hash=changed["desired_hash"])
    assert disabled["enabled"] is False
    with pytest.raises(ValueError, match="minimum"):
        store.configure("example", enabled=True, config={"count": -2}, selectors={}, base_hash=disabled["desired_hash"])


def test_concurrent_plugin_updates_preserve_other_plugin_settings(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store = PluginStore(tmp_path)
    first = store.install("first.py", SOURCE)
    second = store.install("second.py", SOURCE)

    def save(item):
        return PluginStore(tmp_path).configure(item["id"], enabled=True, config={"prefix": item["id"]}, selectors={}, base_hash=item["desired_hash"])

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert all(item["enabled"] for item in executor.map(save, [first, second]))
    assert store.get("first")["config"]["prefix"] == "first"
    assert store.get("second")["config"]["prefix"] == "second"


def test_runtime_diagnostics_include_frames_and_skip_corrupt_entries(tmp_path, monkeypatch):
    directory = tmp_path / "data" / "plugin_runtime"
    directory.mkdir(parents=True)
    raw = {
        "pid": 123, "updated_at": _fresh(), "plugins": [None, {
            "id": "example", "state": "error", "desired_hash": "new", "applied_hash": "old",
            "error": "request failed", "error_details": {"phase": "request", "type": "ValueError", "message": "invalid setting", "frames": [{"file": "example.py", "line": 17, "function": "before_request"}]},
        }],
    }
    (directory / "123.json").write_text(json.dumps(raw))
    (directory / "invalid.json").write_text(json.dumps({"pid": 2 ** 1000}))
    monkeypatch.setattr("supervisor.plugin_api._pid_alive", lambda _: True)
    result = runtime_status(tmp_path)
    assert len(result) == 1
    entry = result[0]["plugins"][0]
    assert entry["status"] == "error"
    assert entry["errors"] == [{"phase": "request", "message": "ValueError: invalid setting\nexample.py:17 in before_request", "at": raw["updated_at"]}]


def test_generation_can_use_one_definition_snapshot(tmp_path):
    store = PluginStore(tmp_path)
    item = store.install("example.py", SOURCE)
    snapshot = store.definitions()
    generation = store.generation(snapshot)
    store.configure("example", enabled=True, config={}, selectors={}, base_hash=item["desired_hash"])
    assert store.generation(snapshot) == generation
    assert store.generation() != generation
