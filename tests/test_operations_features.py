from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from supervisor.operations.service import OperationsService


def service(root):
    secret = SimpleNamespace(provider="missing", model="", api_key="secret-test-key")
    state = SimpleNamespace(
        workspace_root=root, tasks={}, _lock=threading.RLock(), _task_terminal=lambda _: False,
        feature_config_store=SimpleNamespace(get_openai_secret=lambda: secret),
        feature_process_manager=SimpleNamespace(worker_health=lambda: {"worker": {"alive": False, "last_log": "api_key=secret-test-key"}}),
    )
    return OperationsService(state)


def test_status_uses_explicit_instance_config_path(tmp_path, monkeypatch):
    custom = tmp_path / "config/qq-secondary.yaml"
    custom.parent.mkdir(parents=True)
    custom.write_text("queue:\n  workers: 2\n", encoding="utf-8")
    monkeypatch.setenv("CLONOTH_QQ_CONFIG_PATH", str(custom))
    operations = service(tmp_path)
    stat = custom.stat()
    live = tmp_path / "data/qq_live_state.json"
    live.parent.mkdir(parents=True)
    live.write_text(json.dumps({
        "config_path": str(custom), "published_at": time.time(),
        "loaded": {"exists": True, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size},
    }), encoding="utf-8")
    assert operations.qq_file == custom
    assert operations.status()["config_applied"] is True
    assert not (tmp_path / "config/qq.yaml").exists()


def test_diagnostics_reports_mismatched_bot_config_path_without_following_it(tmp_path):
    operations = service(tmp_path)
    live = tmp_path / "data/qq_live_state.json"
    live.parent.mkdir(parents=True)
    live.write_text(json.dumps({"config_path": str(tmp_path / "other/qq.yaml")}), encoding="utf-8")
    assert operations.status()["config_path_mismatch"] is True
    report = asyncio.run(operations.diagnose())
    check = next(check for check in report["checks"] if check["name"] == "QQ配置路径")
    assert check["status"] == "failed"
    assert "模板" not in check["action"]
    assert not operations.qq_file.exists()
    assert not (tmp_path / "other").exists()


def test_diagnostics_redacts_and_reports_real_unavailable_worker(tmp_path):
    operations = service(tmp_path)
    logs = tmp_path / "data/logs"
    logs.mkdir(parents=True)
    (logs / "test.log").write_text("api_key=secret-test-key\nAuthorization: Bearer something-secret", encoding="utf-8")
    report = asyncio.run(operations.diagnose(include_logs=True))
    assert report["status"] == "failed"
    assert next(check for check in report["checks"] if check["name"] == "Engine")["status"] == "warning"
    encoded = json.dumps(report)
    assert "secret-test-key" not in encoded
    assert "something-secret" not in encoded
    assert operations.get_report(report["id"]) == report
    with pytest.raises(ValueError):
        operations.get_report("../../config")


def test_fleet_status_uses_adapter_volatile_queue_and_applied_fingerprint(tmp_path):
    operations = service(tmp_path)
    operations.qq_file.parent.mkdir(parents=True)
    operations.qq_file.write_text("{}", encoding="utf-8")
    stat = operations.qq_file.stat()
    live = {"published_at": time.time(), "loaded": {"exists": True, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size}, "runtime": {"onebot_connected": True, "volatile": {"queue_pending": 8}}}
    path = tmp_path / "data/qq_live_state.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(live), encoding="utf-8")
    assert operations.status()["queue_pending"] == 8
    assert operations.status()["config_applied"] is True
    live["published_at"] = time.time() - 120
    path.write_text(json.dumps(live), encoding="utf-8")
    assert operations.status()["adapter_online"] is False
    assert operations.status()["config_applied"] is False


def test_diagnostics_reports_missing_material_dependencies_without_stopping_other_checks(tmp_path):
    operations = service(tmp_path)
    operations.state.materials = SimpleNamespace(capabilities=lambda: {"modules": {"pdf": True, "docx": False}, "office_renderer": False})
    report = asyncio.run(operations.diagnose())
    assert next(check for check in report["checks"] if check["name"] == "文档处理")["status"] == "warning"

    def unavailable():
        raise RuntimeError("renderer could not start")

    operations.state.materials.capabilities = unavailable
    report = asyncio.run(operations.diagnose())
    assert next(check for check in report["checks"] if check["name"] == "文档处理")["status"] == "failed"
    assert next(check for check in report["checks"] if check["name"] == "Supervisor")["status"] == "passed"
