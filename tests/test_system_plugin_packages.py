from __future__ import annotations

import asyncio
import base64
import io
import json
import stat
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

import config_layout
from plugin_runtime import packages
from plugin_runtime.store import PluginStore
from supervisor import admin_api
from supervisor.plugin_api import create_plugin_router


def package_files(version="1.0", *, backend=True):
    manifest = {
        "id": "example", "name": "Example", "version": version, "sdk_version": 1,
        "frontend": "frontend.js", "styles": ["style.css"], "assets": ["assets/logo.svg"],
        "params_schema": [{"key": "prefix", "type": "text", "default": "hello"}],
    }
    files = {
        "frontend.js": f"export function activate(api) {{ return {version!r}; }}".encode(),
        "style.css": b".example { color: var(--accent); }",
        "assets/logo.svg": b"<svg xmlns='http://www.w3.org/2000/svg'/>",
        "helper.py": b"VALUE = 'original'\n",
    }
    if backend:
        manifest["backend"] = "backend.py"
        files["backend.py"] = b"from .helper import VALUE\ndef setup(api):\n    pass\n"
    files["plugin.json"] = json.dumps(manifest).encode()
    return files


def zip_bytes(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            item = zipfile.ZipInfo(name)
            item.filename = name
            archive.writestr(item, content)
    return output.getvalue()


def install(store, files=None, base_hash=None):
    return store.install_package("example.zip", zip_bytes(files or package_files()), base_hash)


def enable(store, item, config=None):
    return store.configure(item["id"], enabled=True, config=config or {}, selectors={}, base_hash=item["desired_hash"])


def test_package_static_inspection_and_public_fields(tmp_path):
    store = PluginStore(tmp_path)
    files = package_files()
    marker = tmp_path / "executed"
    files["backend.py"] = f"from pathlib import Path\nPath({str(marker)!r}).touch()\ndef setup(api): pass\n".encode()
    item = install(store, files)
    assert item["kind"] == "package"
    assert not item["enabled"] and not item["source_editable"]
    assert item["config"] == {"prefix": "hello"}
    assert {entry["path"] for entry in item["files"]} == set(files)
    assert item["frontend"]["entry"]["path"] == "frontend.js"
    assert all(not isinstance(value, Path) for value in item.values())
    json.dumps(item)
    definition = store.get("example")
    assert definition["package_root"].is_dir()
    assert definition["path"] == definition["package_root"] / "backend.py"
    assert not store.source("example")["editable"]
    assert not marker.exists()
    with pytest.raises(PermissionError):
        store.update_source("example", "def setup(api): pass", item["source_hash"])


def test_frontend_only_package_and_catalog(tmp_path):
    store = PluginStore(tmp_path)
    item = install(store, package_files(backend=False))
    assert store.get("example")["path"] is None
    assert item["source_hash"] == ""
    assert store.source("example")["filename"] == "plugin.json"
    assert store.frontend_catalog()["plugins"] == []
    enabled = enable(store, item, {"prefix": "saved"})
    catalog = store.frontend_catalog()
    assert catalog["plugins"][0]["config"] == {"prefix": "saved"}
    assert catalog["plugins"][0]["desired_hash"] == enabled["desired_hash"]


def test_upgrade_pins_backend_and_frontend_versions_and_preserves_settings(tmp_path):
    store = PluginStore(tmp_path)
    first = enable(store, install(store), {"prefix": "keep"})
    first_root = store.get("example")["package_root"]
    files = package_files("2.0")
    files["helper.py"] = b"VALUE = 'changed'\n"
    second = install(store, files, first["desired_hash"])
    assert second["enabled"] and second["config"]["prefix"] == "keep"
    assert second["source_hash"] == first["source_hash"]
    assert second["package_hash"] != first["package_hash"]
    assert second["desired_hash"] != first["desired_hash"]
    assert (first_root / "helper.py").read_bytes() == b"VALUE = 'original'\n"
    assert store.asset("example", first["package_hash"], "frontend.js")[0] == package_files()["frontend.js"]
    assert store.asset("example", second["package_hash"], "frontend.js")[0] == files["frontend.js"]
    store.remove("example", second["desired_hash"])
    assert store.frontend_catalog()["plugins"] == []
    assert store.asset("example", first["package_hash"], "frontend.js")[0]
    assert not install(store)["enabled"]


def test_dependency_only_change_changes_package_generation(tmp_path):
    store = PluginStore(tmp_path)
    first = install(store)
    files = package_files()
    files["helper.py"] += b"ANOTHER = 2\n"
    second = install(store, files, first["desired_hash"])
    assert second["source_hash"] == first["source_hash"]
    assert second["package_hash"] != first["package_hash"]
    assert second["desired_hash"] != first["desired_hash"]


def test_stale_and_invalid_upgrades_preserve_active_pointer(tmp_path, monkeypatch):
    store = PluginStore(tmp_path)
    first = enable(store, install(store))
    pointer = store.directory / "example.package.json"
    before_pointer, before_settings = pointer.read_bytes(), store.settings_path.read_bytes()
    with pytest.raises(config_layout.ConfigConflict):
        install(store, package_files("2.0"))
    with pytest.raises(config_layout.ConfigConflict):
        install(store, package_files("2.0"), "0" * 64)
    broken = package_files("2.0")
    broken["helper.py"] = b"def broken("
    with pytest.raises(ValueError):
        install(store, broken, first["desired_hash"])
    original_write = config_layout.write_config_text

    def fail_pointer(path, *args, **kwargs):
        if path == pointer:
            raise OSError("pointer failed")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(config_layout, "write_config_text", fail_pointer)
    with pytest.raises(OSError, match="pointer failed"):
        install(store, package_files("2.0"), first["desired_hash"])
    assert pointer.read_bytes() == before_pointer
    assert store.settings_path.read_bytes() == before_settings
    assert store.get("example")["desired_hash"] == first["desired_hash"]


def test_upgrade_with_incompatible_saved_config_preserves_version(tmp_path):
    store = PluginStore(tmp_path)
    first = enable(store, install(store), {"prefix": "saved"})
    files = package_files("2.0")
    manifest = json.loads(files["plugin.json"])
    manifest["params_schema"][0]["type"] = "integer"
    files["plugin.json"] = json.dumps(manifest).encode()
    with pytest.raises(ValueError, match="数字"):
        install(store, files, first["desired_hash"])
    assert store.get("example")["desired_hash"] == first["desired_hash"]


@pytest.mark.parametrize("path", ["../escape", "/absolute", "C:/escape", "a\\escape", "a/../escape", "a//escape", "a/./escape", "a/evil.", "NUL.txt", "a/COM1"])
def test_unsafe_zip_paths_rejected_before_writes(tmp_path, path):
    store = PluginStore(tmp_path)
    files = package_files()
    files[path] = b"bad"
    with pytest.raises(ValueError):
        install(store, files)
    assert not store.directory.exists()


def test_symlink_zip_rejected(tmp_path):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        item = zipfile.ZipInfo("backend.py")
        item.create_system = 3
        item.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(item, "../../escape")
    with pytest.raises(ValueError, match="链接"):
        PluginStore(tmp_path).install_package("bad.zip", output.getvalue())


def test_zip_bomb_and_file_count_limits(tmp_path, monkeypatch):
    store = PluginStore(tmp_path)
    monkeypatch.setattr(packages, "MAX_PACKAGE_FILES", 2)
    with pytest.raises(ValueError, match="条目"):
        install(store)
    monkeypatch.setattr(packages, "MAX_PACKAGE_FILES", 512)
    monkeypatch.setattr(packages, "MAX_FILE_BYTES", 10)
    with pytest.raises(ValueError, match="大小"):
        install(store)
    monkeypatch.setattr(packages, "MAX_FILE_BYTES", 1024 * 1024)
    monkeypatch.setattr(packages, "MAX_PACKAGE_BYTES", 100)
    with pytest.raises(ValueError, match="大小"):
        install(store)
    assert not store.directory.exists()


@pytest.mark.parametrize("manifest_change", [
    {"id": "../bad"}, {"sdk_version": 2}, {"sdk_version": True}, {"frontend": "../bad.js"},
    {"frontend": "missing.js"}, {"styles": ["backend.py"]}, {"assets": ["backend.py"]},
    {"assets": ["plugin.json"]}, {"processes": []}, {"params_schema": [{"key": "bad", "type": 4}]},
])
def test_manifest_validation(tmp_path, manifest_change):
    files = package_files()
    manifest = json.loads(files["plugin.json"])
    manifest.update(manifest_change)
    files["plugin.json"] = json.dumps(manifest).encode()
    with pytest.raises(ValueError):
        install(PluginStore(tmp_path), files)


def test_manual_directory_is_snapshotted_without_execution(tmp_path):
    store = PluginStore(tmp_path)
    directory = store.directory / "example"
    files = package_files()
    for name, raw in files.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    first = store.get("example")
    assert first["package_root"] != directory
    (directory / "helper.py").write_text("VALUE = 42\n")
    second = store.get("example")
    assert second["package_hash"] != first["package_hash"]
    assert (first["package_root"] / "helper.py").read_bytes() == files["helper.py"]
    store.remove("example", second["desired_hash"])
    with pytest.raises(FileNotFoundError):
        store.get("example")


def test_invalid_manual_update_is_not_reported_as_frontend_removal(tmp_path):
    store = PluginStore(tmp_path)
    directory = store.directory / "example"
    for name, raw in package_files().items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    enable(store, store.get("example"))
    before = store.frontend_catalog()["plugins"][0]
    (directory / "plugin.json").write_text("{unfinished", encoding="utf-8")
    catalog = store.frontend_catalog()
    assert catalog["plugins"] == []
    assert catalog["errors"][0]["id"] == "example"
    assert catalog["errors"][0]["enabled"] is True
    assert catalog["errors"][0]["error"]
    assert store.asset("example", before["package_hash"], "frontend.js")[0]
    item = store.get("example")
    store.configure("example", enabled=False, config={}, selectors={}, base_hash=item["desired_hash"])
    assert store.frontend_catalog()["errors"] == []


def test_single_wrapper_directory_and_local_import(tmp_path):
    store = PluginStore(tmp_path / "workspace")
    item = install(store, {f"release/{name}": raw for name, raw in package_files().items()})
    source = tmp_path / "source"
    for name, raw in package_files("2.0").items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    changed = store.import_directory(source, item["desired_hash"])
    assert changed["version"] == "2.0"


def test_packages_follow_shared_config_directory(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    monkeypatch.setattr(config_layout, "config_dir", lambda root: shared)
    monkeypatch.setattr(config_layout, "custom_plugins_dir", lambda root: shared / "plugins")
    monkeypatch.setattr(config_layout, "plugins_path", lambda root: shared / "plugins.yaml")
    first, second = PluginStore(tmp_path / "first"), PluginStore(tmp_path / "second")
    item = install(first)
    assert second.get("example")["desired_hash"] == item["desired_hash"]
    changed = enable(second, item, {"prefix": "shared"})
    assert first.get("example")["config"] == changed["config"]
    updated = install(first, package_files("2.0"), changed["desired_hash"])
    assert second.get("example")["package_hash"] == updated["package_hash"]


def test_package_api_auth_assets_catalog_and_conflicts(tmp_path, monkeypatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", "package-test")
    monkeypatch.setattr(admin_api, "_admin_token", "")
    app = FastAPI()
    app.include_router(create_plugin_router(tmp_path), prefix="/plugins")
    body = {"filename": "example.zip", "content_base64": base64.b64encode(zip_bytes(package_files())).decode()}

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.post("/plugins/packages", json=body)).status_code == 401
            client.headers["Authorization"] = "Bearer package-test"
            response = await client.post("/plugins/packages", json=body)
            assert response.status_code == 200, response.text
            item = response.json()
            assert (await client.post("/plugins/packages", json=body)).status_code == 409
            assert (await client.post("/plugins/packages", json={**body, "content_base64": "!invalid!"})).status_code == 400
            response = await client.put("/plugins/example/config", json={"enabled": True, "config": {}, "selectors": {}, "base_hash": item["desired_hash"]})
            assert response.status_code == 200, response.text
            assert (await client.get("/plugins/frontend")).json()["plugins"][0]["id"] == "example"
            asset_url = f"/plugins/example/assets/{item['package_hash']}"
            asset = await client.get(asset_url + "/frontend.js")
            assert asset.status_code == 200 and asset.content == package_files()["frontend.js"]
            assert "immutable" in asset.headers["cache-control"]
            for path in ("backend.py", "helper.py", "plugin.json"):
                assert (await client.get(asset_url + "/" + path)).status_code == 404
            client.headers.clear()
            assert (await client.get(asset_url + "/frontend.js")).status_code == 401
            assert (await client.get("/plugins/frontend")).status_code == 401
    asyncio.run(scenario())


@pytest.mark.parametrize("extra", [
    {"Frontend.js": b"different"}, {"assets": b"file"}, {"Assets/another.svg": b"<svg/>"},
    {"__pycache__/cached.pyc": b"compiled"},
])
def test_zip_tree_aliases_rejected(tmp_path, extra):
    with pytest.raises(ValueError):
        install(PluginStore(tmp_path), {**package_files(), **extra})


def test_package_id_case_collisions_rejected(tmp_path):
    store = PluginStore(tmp_path)
    first = install(store)
    files = package_files("2.0")
    manifest = json.loads(files["plugin.json"])
    manifest["id"] = "Example"
    files["plugin.json"] = json.dumps(manifest).encode()
    with pytest.raises(config_layout.ConfigConflict, match="大小写"):
        install(store, files, first["desired_hash"])
    assert store.get("example")["desired_hash"] == first["desired_hash"]


def test_apply_awaits_host_lifecycle(tmp_path, monkeypatch):
    import plugin_runtime

    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", "package-test")
    monkeypatch.setattr(admin_api, "_admin_token", "")
    calls = []

    class Manager:
        async def arefresh(self):
            await asyncio.sleep(0)
            calls.append("applied")

    monkeypatch.setattr(plugin_runtime, "get_manager", lambda *args, **kwargs: Manager())
    app = FastAPI()
    app.include_router(create_plugin_router(tmp_path), prefix="/plugins")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer package-test"}) as client:
            response = await client.post("/plugins/apply")
            assert response.status_code == 200, response.text
            assert calls == ["applied"]
    asyncio.run(scenario())
