"""插件运行时的陈旧状态：写状态死在半路的临时文件、卸载后还挂着的错误记录、换下来的插件包版本，
以及卸载、安装中断留下的目录。

清扫按引用、前缀和年龄判断；还在用的版本和正在进行的操作不能被当成垃圾。
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from plugin_runtime import manager as pm
from plugin_runtime import packages
from plugin_runtime.store import PluginStore

_STALE_ISO = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
_PAST_RETENTION = packages.RETENTION_SECONDS + 600


def _fresh_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(path: Path, data: bytes = b"{}") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _age(path: Path, seconds: float) -> Path:
    old = time.time() - seconds
    os.utime(path, (old, old))
    return path


# ── data/plugin_runtime ──

def test_a_status_temp_left_by_a_crashed_writer_is_pruned(tmp_path: Path) -> None:
    directory = tmp_path / "data" / "plugin_runtime"
    keep = _write(directory / "1.json", json.dumps({"pid": 1, "updated_at": _fresh_iso()}).encode())
    crashed = _age(_write(directory / ".4242.140000.tmp"), pm.RUNTIME_STALE_SECONDS + 60)
    writing = _write(directory / ".4243.140001.tmp")
    foreign = _age(_write(directory / ".notes.tmp"), pm.RUNTIME_STALE_SECONDS + 60)

    pm._prune_runtime_dir(directory, keep=keep)

    assert not crashed.exists()
    assert writing.exists() and foreign.exists() and keep.exists()


def test_a_failed_status_write_cleans_up_its_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    manager = pm.PluginManager(tmp_path)

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(pm.os, "replace", boom)
    with caplog.at_level("WARNING"):
        with manager._lock:
            manager._write_status_locked()
    monkeypatch.undo()

    try:
        assert list((tmp_path / "data" / "plugin_runtime").iterdir()) == []
        assert "Could not write plugin runtime status" in caplog.text
    finally:
        manager.close()


def test_pruning_follows_the_heartbeat_pace_instead_of_every_status_write(tmp_path: Path) -> None:
    manager = pm.PluginManager(tmp_path)
    directory = tmp_path / "data" / "plugin_runtime"
    try:
        with manager._lock:
            manager._write_status_locked()
        dead = _write(directory / "99999.json", json.dumps({"pid": 99999, "updated_at": _STALE_ISO}).encode())
        with manager._lock:
            manager._write_status_locked()
        assert dead.exists()

        manager._pruned_at -= pm._PRUNE_INTERVAL_SECONDS + 1
        with manager._lock:
            manager._write_status_locked()
        assert not dead.exists()
    finally:
        manager.close()


# ── 卸载后残留的错误记录 ──

def _install_module(root: Path, plugin_id: str, source: str, **entry) -> None:
    _write(root / "config" / "plugins" / f"{plugin_id}.py", source.encode("utf-8"))
    settings = root / "config" / "plugins.yaml"
    doc = yaml.safe_load(settings.read_text(encoding="utf-8")) if settings.exists() else {"version": 1, "plugins": {}}
    doc["plugins"][plugin_id] = {"enabled": True, **entry}
    settings.write_text(yaml.safe_dump(doc), encoding="utf-8")


def _uninstall_module(root: Path, plugin_id: str) -> None:
    (root / "config" / "plugins" / f"{plugin_id}.py").unlink()
    settings = root / "config" / "plugins.yaml"
    doc = yaml.safe_load(settings.read_text(encoding="utf-8"))
    doc["plugins"].pop(plugin_id, None)
    settings.write_text(yaml.safe_dump(doc), encoding="utf-8")


def test_errors_of_an_uninstalled_plugin_do_not_linger(tmp_path: Path) -> None:
    _install_module(tmp_path, "sample", "def setup(api):\n    api.request(lambda ctx, value: None)\n")
    manager = pm.PluginManager(tmp_path)
    try:
        versions = manager.acquire()
        (version,) = versions
        _uninstall_module(tmp_path, "sample")
        manager.refresh()
        assert version.retired

        # 卸载之后，还在执行的旧版本收尾时又记了一笔错。
        manager.record_error(version, "end", ValueError("late failure"))
        assert "sample" in manager._retired_errors
        manager.refresh()

        for table in (manager._retired_errors, manager._errors, manager._error_details, manager._attempted):
            assert "sample" not in table
        manager.release(versions)
    finally:
        manager.close()


def test_a_plugin_that_failed_validation_leaves_no_error_after_removal(tmp_path: Path) -> None:
    _install_module(tmp_path, "broken", "def setup(api):\n    pass\n", config="not a mapping")
    manager = pm.PluginManager(tmp_path)
    try:
        manager.refresh()
        assert manager._errors["broken"].startswith("validation")

        _uninstall_module(tmp_path, "broken")
        manager.refresh()

        assert "broken" not in manager._errors
        assert "broken" not in manager._error_details
    finally:
        manager.close()


def test_retired_errors_of_a_plugin_that_is_still_installed_are_kept(tmp_path: Path) -> None:
    _install_module(tmp_path, "sample", "def setup(api):\n    api.request(lambda ctx, value: None)\n")
    manager = pm.PluginManager(tmp_path)
    try:
        versions = manager.acquire()
        (old,) = versions
        _install_module(tmp_path, "sample", "def setup(api):\n    api.request(lambda ctx, value: value)\n")
        manager.refresh()
        assert old.retired

        manager.record_error(old, "end", ValueError("old failure"))
        manager.refresh()

        assert manager.status()["plugins"][0]["retired_errors"][0]["type"] == "ValueError"
        manager.release(versions)
    finally:
        manager.close()


# ── 插件包版本 ──

def _package_files(version: str) -> dict[str, bytes]:
    manifest = {"id": "example", "name": "Example", "version": version, "sdk_version": 1, "frontend": "frontend.js"}
    return {
        "plugin.json": json.dumps(manifest).encode(),
        "frontend.js": f"export const version = {version!r};".encode(),
    }


def _zip(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def _install(store: PluginStore, version: str, base_hash: str | None = None) -> dict:
    return store.install_package("example.zip", _zip(_package_files(version)), base_hash)


def _version_dir(store: PluginStore, item: dict) -> Path:
    return store.directory / ".packages" / "example" / item["package_hash"]


def _mark(store: PluginStore, item: dict) -> Path:
    return store.directory / ".packages" / "example" / f".{item['package_hash']}.orphaned"


def test_a_replaced_version_is_marked_first_and_reclaimed_after_the_retention(tmp_path: Path) -> None:
    store = PluginStore(tmp_path)
    first = _install(store, "1.0")
    second = _install(store, "2.0", first["desired_hash"])

    assert store.sweep_packages() == []
    assert _version_dir(store, first).is_dir()
    assert _mark(store, first).is_file()
    assert not _mark(store, second).exists()

    _age(_mark(store, first), _PAST_RETENTION)
    assert store.sweep_packages() == [_version_dir(store, first)]
    assert not _version_dir(store, first).exists()
    assert not _mark(store, first).exists()
    assert store.get("example")["package_hash"] == second["package_hash"]


def test_reinstalling_a_marked_version_takes_it_off_the_list(tmp_path: Path) -> None:
    store = PluginStore(tmp_path)
    first = _install(store, "1.0")
    second = _install(store, "2.0", first["desired_hash"])
    store.sweep_packages()
    assert _mark(store, first).is_file()

    again = _install(store, "1.0", second["desired_hash"])
    assert again["package_hash"] == first["package_hash"]
    _age(_mark(store, first), _PAST_RETENTION)
    store.sweep_packages()

    assert _version_dir(store, first).is_dir()
    assert not _mark(store, first).exists()
    assert _mark(store, second).is_file()


def test_uninstalling_schedules_every_version_for_reclamation(tmp_path: Path) -> None:
    store = PluginStore(tmp_path)
    first = _install(store, "1.0")
    second = _install(store, "2.0", first["desired_hash"])

    store.remove("example", second["desired_hash"])

    # 卸载当下只记时刻：还在执行的请求可能正读着旧版本的文件。
    for item in (first, second):
        assert _version_dir(store, item).is_dir()
        assert _mark(store, item).is_file()
    assert store.asset("example", second["package_hash"], "frontend.js")[0]

    for item in (first, second):
        _age(_mark(store, item), _PAST_RETENTION)
    store.sweep_packages()
    assert not (store.directory / ".packages" / "example").exists()


def test_a_version_this_process_still_runs_is_kept(tmp_path: Path) -> None:
    store = PluginStore(tmp_path)
    first = _install(store, "1.0")
    _install(store, "2.0", first["desired_hash"])
    store.sweep_packages()
    _age(_mark(store, first), _PAST_RETENTION)

    store.sweep_packages(in_use={("example", first["package_hash"])})

    assert _version_dir(store, first).is_dir()
    assert not _mark(store, first).exists()


def test_a_plugin_whose_pointer_cannot_be_read_keeps_every_version(tmp_path: Path) -> None:
    store = PluginStore(tmp_path)
    first = _install(store, "1.0")
    second = _install(store, "2.0", first["desired_hash"])
    store.sweep_packages()
    _age(_mark(store, first), _PAST_RETENTION)
    (store.directory / "example.package.json").write_text("{broken", encoding="utf-8")

    assert store.sweep_packages() == []
    assert _version_dir(store, first).is_dir()
    assert _version_dir(store, second).is_dir()
    assert not _mark(store, second).exists()


def test_interrupted_installs_and_old_removals_are_cleared_by_age(tmp_path: Path) -> None:
    store = PluginStore(tmp_path)
    _install(store, "1.0")
    plugin_dir = store.directory / ".packages" / "example"
    stale_pending = plugin_dir / ".pending-aaaa"
    _age(_write(stale_pending / "plugin.json"), _PAST_RETENTION)
    _age(stale_pending, _PAST_RETENTION)
    fresh_pending = plugin_dir / ".pending-bbbb"
    fresh_pending.mkdir()
    stale_removed = store.directory / ".removed-gone-0123456789ab"
    _age(_write(stale_removed / "plugin.json"), _PAST_RETENTION)
    _age(stale_removed, _PAST_RETENTION)
    stale_temp = _age(_write(store.directory / ".gone.py.123.abcdef.tmp"), _PAST_RETENTION)
    fresh_temp = _write(store.directory / ".other.py.456.abcdef.tmp")

    removed = store.sweep_packages()

    assert set(removed) == {stale_pending, stale_removed, stale_temp}
    assert fresh_pending.is_dir() and fresh_temp.is_file()
    assert store.get("example")["error"] is None


def test_a_removed_manual_directory_counts_its_age_from_the_removal(tmp_path: Path) -> None:
    store = PluginStore(tmp_path)
    manual = store.directory / "example"
    for name, raw in _package_files("1.0").items():
        _age(_write(manual / name, raw), _PAST_RETENTION)
    _age(manual, _PAST_RETENTION)
    item = store.get("example")

    store.remove("example", item["desired_hash"])

    retired = [path for path in store.directory.iterdir() if path.name.startswith(".removed-example-")]
    assert len(retired) == 1
    assert store.sweep_packages() == []
    assert retired[0].is_dir()


# ── 心跳 ──

def test_only_the_supervisor_heartbeat_sweeps_packages_and_not_on_every_beat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = pm.PluginManager(tmp_path)
    calls: list[dict] = []
    monkeypatch.setattr(manager.store, "sweep_packages", lambda **kwargs: calls.append(kwargs) or [])
    try:
        manager.heartbeat()
        assert calls == []

        manager.process = "supervisor"
        manager.heartbeat()
        manager.heartbeat()
        assert calls == [{"in_use": set()}]

        manager._packages_swept_at -= pm._PACKAGE_SWEEP_INTERVAL_SECONDS + 1
        manager.heartbeat()
        assert len(calls) == 2
    finally:
        manager.close()


def test_the_heartbeat_tells_the_sweep_which_versions_this_process_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PluginStore(tmp_path)
    item = _install(store, "1.0")
    store.configure("example", enabled=True, config={}, selectors={}, base_hash=item["desired_hash"])
    manager = pm.PluginManager(tmp_path)
    manager.process = "supervisor"
    calls: list[dict] = []
    monkeypatch.setattr(manager.store, "sweep_packages", lambda **kwargs: calls.append(kwargs) or [])
    try:
        manager.heartbeat()
        assert calls == [{"in_use": {("example", item["package_hash"])}}]
    finally:
        manager.close()


def test_a_failing_package_sweep_does_not_stop_the_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    manager = pm.PluginManager(tmp_path)
    manager.process = "supervisor"

    def boom(**_kwargs):
        raise ValueError("plugins.yaml 读不了")

    monkeypatch.setattr(manager.store, "sweep_packages", boom)
    try:
        with caplog.at_level("WARNING"):
            manager.heartbeat()
        assert (tmp_path / "data" / "plugin_runtime" / f"{os.getpid()}.json").is_file()
        assert "Plugin package sweep failed" in caplog.text
    finally:
        manager.close()
