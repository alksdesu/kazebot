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

from platforms.builtin import QQ_NAPCAT
from platforms.descriptor import PlatformDescriptor
from platforms.registry import PLATFORM_ENV, platform_config_path, register_platform, unregister_platform
from supervisor.operations.service import OperationsService

# 没有热载配置、也没声明排查提示的平台，看诊断是不是按通用说法给。
PLAIN = PlatformDescriptor(id="plainchat", display_name="Plain", short_name="P")


def service(root):
    secret = SimpleNamespace(provider="missing", model="", api_key="secret-test-key")
    state = SimpleNamespace(
        workspace_root=root, tasks={}, _lock=threading.RLock(), _task_terminal=lambda _: False,
        feature_config_store=SimpleNamespace(get_openai_secret=lambda: secret),
        feature_process_manager=SimpleNamespace(worker_health=lambda: {"worker": {"alive": False, "last_log": "api_key=secret-test-key"}}),
    )
    return OperationsService(state)


def qq_status(operations):
    return next(item for item in operations.status()["platforms"] if item["id"] == "qq")


def check(report, name):
    return next(item for item in report["checks"] if item["name"] == name)


def publish(root, **fields):
    path = root / "data/qq_live_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


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
    assert platform_config_path(QQ_NAPCAT, tmp_path) == custom
    assert qq_status(operations)["config_applied"] is True
    assert not (tmp_path / "config/qq.yaml").exists()


def test_diagnostics_reports_mismatched_bot_config_path_without_following_it(tmp_path):
    operations = service(tmp_path)
    live = tmp_path / "data/qq_live_state.json"
    live.parent.mkdir(parents=True)
    live.write_text(json.dumps({"config_path": str(tmp_path / "other/qq.yaml")}), encoding="utf-8")
    assert qq_status(operations)["config_path_mismatch"] is True
    report = asyncio.run(operations.diagnose())
    found = check(report, "QQ · NapCat 配置路径")
    assert found["status"] == "failed"
    assert found["action"] == "统一两个进程的 CLONOTH_QQ_CONFIG_PATH 后重新检查"
    assert not platform_config_path(QQ_NAPCAT, tmp_path).exists()
    assert not (tmp_path / "other").exists()


def test_a_matching_fingerprint_from_another_file_is_not_applied(tmp_path, monkeypatch):
    # bot 读的是另一份文件，大小和时间碰巧对上，也不能说这边这份已经生效。
    monkeypatch.delenv("CLONOTH_QQ_CONFIG_PATH", raising=False)
    operations = service(tmp_path)
    config = platform_config_path(QQ_NAPCAT, tmp_path)
    config.parent.mkdir(parents=True)
    config.write_text("{}", encoding="utf-8")
    stat = config.stat()
    publish(
        tmp_path, published_at=time.time(), config_path=str(tmp_path / "other/qq.yaml"),
        loaded={"exists": True, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size},
    )
    status = qq_status(operations)
    assert (status["config_path_mismatch"], status["config_applied"]) == (True, False)


def test_the_same_config_file_written_another_way_is_not_a_mismatch(tmp_path, monkeypatch):
    # bot 报的路径和 supervisor 算的写法不同（相对、带 ..），指的却是同一个文件。
    monkeypatch.delenv("CLONOTH_QQ_CONFIG_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    operations = service(tmp_path)
    publish(tmp_path, config_path="data/../config/qq.yaml", published_at=time.time())
    assert qq_status(operations)["config_path_mismatch"] is False


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


def test_fleet_status_uses_the_live_convention_and_applied_fingerprint(tmp_path):
    operations = service(tmp_path)
    config = platform_config_path(QQ_NAPCAT, tmp_path)
    config.parent.mkdir(parents=True)
    config.write_text("{}", encoding="utf-8")
    stat = config.stat()
    live = {
        "published_at": time.time(), "connected": True, "account": "10001", "backlog": 8,
        "loaded": {"exists": True, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size},
        # 平台自己 runtime 里的同名字段不算数，只认约定里的顶层几项。
        "runtime": {"onebot_connected": False, "volatile": {"queue_pending": 99}},
    }
    path = publish(tmp_path, **live)
    status = qq_status(operations)
    assert status == {
        "id": "qq", "display_name": "QQ · NapCat", "published": True, "adapter_online": True,
        "connected": True, "account": "10001", "heartbeat_age": status["heartbeat_age"], "backlog": 8,
        "config_applied": True, "config_path_mismatch": False, "parse_error": "",
    }
    assert 0 <= status["heartbeat_age"] < 60
    live["published_at"] = time.time() - 120
    path.write_text(json.dumps(live), encoding="utf-8")
    status = qq_status(operations)
    assert (status["adapter_online"], status["connected"], status["config_applied"]) == (False, False, False)


def test_status_lists_only_the_platform_this_instance_runs(tmp_path, monkeypatch):
    # 别的平台注册着不等于这台在跑它，列出来只会让人以为它掉线了。
    register_platform(PLAIN)
    try:
        rows = service(tmp_path).status()["platforms"]
        monkeypatch.setenv(PLATFORM_ENV, PLAIN.id)
        switched = service(tmp_path).status()["platforms"]
    finally:
        unregister_platform(PLAIN.id)
    assert [row["id"] for row in rows] == ["qq"]
    assert [row["id"] for row in switched] == ["plainchat"]
    assert switched[0]["published"] is False and switched[0]["config_applied"] is None


def test_a_platform_that_never_published_is_a_failure(tmp_path):
    # 一个实例只跑这一个平台，它从没公布过状态就是 bot 没起来或连不上，不是「本来就不跑」。
    report = asyncio.run(service(tmp_path).diagnose())
    found = check(report, "QQ · NapCat 适配进程")
    assert found["status"] == "failed"
    assert found["detail"] == "还没收到过它公布的状态"
    assert found["action"] == "检查 bot 进程是否在跑、连的是不是这个 Supervisor 的地址"
    assert not any(item["name"] == "QQ · NapCat 连接" for item in report["checks"])


def test_another_platform_left_behind_state_is_not_diagnosed(tmp_path, monkeypatch):
    # 换过平台的实例盘上还留着旧平台的快照，算进来就成了「两个适配进程」。
    publish(tmp_path, published_at=time.time(), connected=True)
    register_platform(PLAIN)
    try:
        monkeypatch.setenv(PLATFORM_ENV, PLAIN.id)
        report = asyncio.run(service(tmp_path).diagnose())
    finally:
        unregister_platform(PLAIN.id)
    assert check(report, "Plain 适配进程")["status"] == "failed"
    assert not any(item["name"].startswith("QQ · NapCat") for item in report["checks"])


def test_a_silent_adapter_fails_and_its_connection_is_not_judged(tmp_path):
    publish(tmp_path, published_at=time.time() - 120, connected=True)
    report = asyncio.run(service(tmp_path).diagnose())
    assert check(report, "QQ · NapCat 适配进程")["status"] == "failed"
    assert check(report, "QQ · NapCat 连接")["status"] == "skipped"


def test_an_unconnected_platform_points_at_its_declared_hint(tmp_path):
    publish(tmp_path, published_at=time.time(), connected=False)
    report = asyncio.run(service(tmp_path).diagnose())
    assert check(report, "QQ · NapCat 适配进程")["status"] == "passed"
    found = check(report, "QQ · NapCat 连接")
    assert (found["status"], found["action"]) == ("failed", QQ_NAPCAT.connection_hint)


def test_a_platform_without_a_hint_or_config_gets_the_generic_wording(tmp_path, monkeypatch):
    register_platform(PLAIN)
    monkeypatch.setenv(PLATFORM_ENV, PLAIN.id)
    try:
        live = tmp_path / "data/platforms/plainchat/live.json"
        live.parent.mkdir(parents=True)
        live.write_text(json.dumps({"published_at": time.time(), "config_path": str(tmp_path / "x.yaml")}), encoding="utf-8")
        report = asyncio.run(service(tmp_path).diagnose())
    finally:
        unregister_platform(PLAIN.id)
    assert check(report, "Plain 连接")["action"] == "检查平台账号登录和适配进程的连接配置"
    # 没有配置文件的平台不去比配置路径。
    assert not any(item["name"] == "Plain 配置路径" for item in report["checks"])


def test_a_platform_that_keeps_no_config_env_points_at_the_workspace(tmp_path, monkeypatch):
    other = PlatformDescriptor(id="filechat", display_name="File", short_name="F", config_path="config/file.yaml")
    register_platform(other)
    monkeypatch.setenv(PLATFORM_ENV, other.id)
    try:
        live = tmp_path / "data/platforms/filechat/live.json"
        live.parent.mkdir(parents=True)
        live.write_text(json.dumps({"published_at": time.time(), "config_path": str(tmp_path / "elsewhere.yaml")}), encoding="utf-8")
        report = asyncio.run(service(tmp_path).diagnose())
    finally:
        unregister_platform(other.id)
    assert check(report, "File 配置路径")["action"] == "确认两个进程用的是同一个工作区后重新检查"


def test_a_broken_config_file_is_reported(tmp_path):
    publish(tmp_path, published_at=time.time(), connected=True, parse_error="ParserError: line 1")
    report = asyncio.run(service(tmp_path).diagnose())
    assert qq_status(service(tmp_path))["parse_error"] == "ParserError: line 1"
    assert check(report, "QQ · NapCat 配置文件")["status"] == "failed"
    publish(tmp_path, published_at=time.time(), connected=True, parse_error=None)
    report = asyncio.run(service(tmp_path).diagnose())
    assert not any(item["name"] == "QQ · NapCat 配置文件" for item in report["checks"])


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
