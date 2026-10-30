"""策略文件、运行参数文件、MCP 配置文件，以及执行计划和提醒的设置文件写坏时，健康检查要说出来。

这几份文件解析失败时沿用上一份能用的配置，起进程时就坏的用内置默认（MCP 配置一份都没读成过时按没有客户端算），服务照常跑；
不报出来的话，人只会看到自定义的规则、参数、MCP 工具、上限悄悄不生效。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config_layout  # noqa: E402
import toolbox.mcp_runtime as mcp_runtime  # noqa: E402
from clonoth_runtime import SettingsFile  # noqa: E402
from engine.memory_hit_cache import hit_cache_path  # noqa: E402
from supervisor.execution.service import EXECUTION_SETTINGS  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.reminders.service import REMINDER_SETTINGS  # noqa: E402
from tests.test_operations_features import check, service  # noqa: E402


def _names(report: dict) -> set[str]:
    return {item["name"] for item in report["checks"]}


def test_a_policy_that_fell_back_is_reported(tmp_path: Path) -> None:
    operations = service(tmp_path)
    reason = f"{config_layout.POLICY_FILE} 用不了，沿用上一份能用的策略：line 3"
    operations.state.policy = SimpleNamespace(load_error=reason)

    report = asyncio.run(operations.diagnose())

    item = check(report, "策略文件")
    assert item["status"] == "warning"
    assert item["detail"] == reason
    assert config_layout.POLICY_FILE in item["action"]


def test_a_policy_broken_from_the_start_says_the_built_in_default_is_in_effect(tmp_path: Path) -> None:
    path = config_layout.policy_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("- not a mapping\n", encoding="utf-8")
    operations = service(tmp_path)
    operations.state.policy = PolicyEngine(workspace_root=tmp_path)

    item = check(asyncio.run(operations.diagnose()), "策略文件")

    assert item["status"] == "warning"
    assert "生效的是内置默认策略" in item["detail"] and "上一份" not in item["detail"]


def test_a_healthy_policy_adds_no_check(tmp_path: Path) -> None:
    operations = service(tmp_path)
    operations.state.policy = SimpleNamespace(load_error="")

    report = asyncio.run(operations.diagnose())

    assert "策略文件" not in _names(report)


def test_a_broken_runtime_file_is_reported(tmp_path: Path) -> None:
    runtime = tmp_path / config_layout.RUNTIME_FILE
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_text("engine: [unclosed\n", encoding="utf-8")

    report = asyncio.run(service(tmp_path).diagnose())

    item = check(report, "运行参数文件")
    assert item["status"] == "warning"
    assert config_layout.RUNTIME_FILE in item["detail"]
    assert config_layout.RUNTIME_FILE in item["action"]


def test_a_healthy_runtime_adds_no_check(tmp_path: Path) -> None:
    report = asyncio.run(service(tmp_path).diagnose())

    assert "运行参数文件" not in _names(report)


def _mcp_file(root: Path, text: str) -> Path:
    path = root / config_layout.MCP_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    config_layout.write_config_text(path, text)
    return path


def test_an_mcp_file_broken_while_running_is_reported_as_kept(tmp_path: Path) -> None:
    path = _mcp_file(tmp_path, "clients:\n  fs:\n    command: npx\n")
    assert mcp_runtime.config_error(tmp_path) == ""
    config_layout.write_config_text(path, "clients: [unclosed\n")

    report = asyncio.run(service(tmp_path).diagnose())

    item = check(report, "MCP 配置文件")
    assert item["status"] == "warning"
    assert item["detail"].startswith(f"{config_layout.MCP_FILE} 用不了，沿用上一份能用的")
    assert config_layout.MCP_FILE in item["action"]


def test_an_mcp_file_broken_from_the_start_says_there_are_no_clients(tmp_path: Path) -> None:
    _mcp_file(tmp_path, "clients: [unclosed\n")

    report = asyncio.run(service(tmp_path).diagnose())

    item = check(report, "MCP 配置文件")
    assert item["status"] == "warning"
    assert item["detail"].startswith(f"{config_layout.MCP_FILE} 用不了，现在按没有客户端算")


@pytest.mark.parametrize("text", [None, "clients:\n  fs:\n    command: npx\n"])
def test_a_missing_or_healthy_mcp_file_adds_no_check(tmp_path: Path, text: str | None) -> None:
    if text is not None:
        _mcp_file(tmp_path, text)

    report = asyncio.run(service(tmp_path).diagnose())

    assert "MCP 配置文件" not in _names(report)


SETTINGS_FILES = pytest.mark.parametrize("name,settings,key", [
    pytest.param("执行计划设置文件", EXECUTION_SETTINGS, "max_parallel_steps", id="execution"),
    pytest.param("提醒设置文件", REMINDER_SETTINGS, "max_open_per_owner", id="reminders"),
])


def _settings_file(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    config_layout.write_config_text(path, text)
    return path


@SETTINGS_FILES
def test_a_settings_file_broken_from_the_start_says_the_defaults_are_in_effect(
    tmp_path: Path, name: str, settings: SettingsFile, key: str,
) -> None:
    _settings_file(tmp_path, settings.rel, f"{key}: [unclosed\n")

    item = check(asyncio.run(service(tmp_path).diagnose()), name)

    assert item["status"] == "warning"
    assert item["detail"].startswith(f"{settings.rel} 用不了，各项按默认值算：YAML 解析失败：第 2 行第 1 列")
    assert "unclosed" not in item["detail"]
    assert settings.rel in item["action"]


@SETTINGS_FILES
def test_a_settings_file_broken_while_running_is_reported_as_kept(
    tmp_path: Path, name: str, settings: SettingsFile, key: str,
) -> None:
    path = _settings_file(tmp_path, settings.rel, f"{key}: 2\n")
    assert settings.load(path) == {key: 2}
    config_layout.write_config_text(path, "- not a mapping\n")

    item = check(asyncio.run(service(tmp_path).diagnose()), name)

    assert item["status"] == "warning"
    assert item["detail"] == f"{settings.rel} 用不了，沿用上一份能用的：顶层必须是映射（key: value）"


@SETTINGS_FILES
@pytest.mark.parametrize("text", [None, "", "{key}: 2\n"], ids=["missing", "empty", "healthy"])
def test_a_missing_empty_or_healthy_settings_file_adds_no_check(
    tmp_path: Path, name: str, settings: SettingsFile, key: str, text: str | None,
) -> None:
    if text is not None:
        _settings_file(tmp_path, settings.rel, text.format(key=key))

    report = asyncio.run(service(tmp_path).diagnose())

    assert name not in _names(report)


HIT_CACHE = "记忆命中记录"
HIT_CACHE_REL = "data/memory/.hit_cache.json"


def _hit_cache(root: Path, text: str) -> None:
    path = hit_cache_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _says_what_stops_working(item: dict) -> None:
    assert item["status"] == "warning"
    for consequence in ("新的命中只留在各 engine 进程里", "数据清理不按多久没被用到淘汰记忆", "dream 也不提清理、提升和重新启用"):
        assert consequence in item["detail"]
    assert HIT_CACHE_REL in item["action"]


# 不报的话，人只看得到命中时间不再更新、记忆不再按年龄清理，找不到是哪份文件挡着。
def test_a_broken_hit_cache_names_the_place_and_what_stops_working(tmp_path: Path) -> None:
    _hit_cache(tmp_path, '{"conv_abc/m1": "sk-live-SECRET" "x"}')

    report = asyncio.run(service(tmp_path).diagnose())

    item = check(report, HIT_CACHE)
    _says_what_stops_working(item)
    assert item["detail"].startswith(f"{HIT_CACHE_REL} 读不了：第 1 行第 ")
    assert "Expecting ',' delimiter" in item["detail"]
    assert "sk-live-SECRET" not in json.dumps(report, ensure_ascii=False)


def test_a_hit_cache_that_is_not_an_object_is_reported(tmp_path: Path) -> None:
    _hit_cache(tmp_path, "[]")

    item = check(asyncio.run(service(tmp_path).diagnose()), HIT_CACHE)

    _says_what_stops_working(item)
    assert item["detail"].startswith(f"{HIT_CACHE_REL} 读不了：top level is a list, not an object。")


@pytest.mark.parametrize("text", [None, "{}", '{"conv_abc/m1": "2026-10-01T00:00:00+00:00"}'], ids=["missing", "empty", "healthy"])
def test_a_missing_or_readable_hit_cache_adds_no_check(tmp_path: Path, text: str | None) -> None:
    if text is not None:
        _hit_cache(tmp_path, text)

    report = asyncio.run(service(tmp_path).diagnose())

    assert HIT_CACHE not in _names(report)


def test_a_key_on_the_broken_line_of_models_yaml_is_hidden_in_the_export(tmp_path: Path) -> None:
    # 文件写坏时整份跳过的话，旧日志里按值记下的那把密钥（没有键名可认）就原样进了导出。
    models = config_layout.models_path(tmp_path)
    models.parent.mkdir(parents=True, exist_ok=True)
    models.write_text("openai:\n  api_key: relay-SECRET-value: x\n", encoding="utf-8")
    logs = tmp_path / "data" / "logs"
    logs.mkdir(parents=True)
    (logs / "supervisor.log").write_text(
        "[config] config/models.yaml 字段不合法：input_value=['relay-SECRET-value']\n", encoding="utf-8",
    )

    report = asyncio.run(service(tmp_path).diagnose(include_logs=True))

    assert "input_value=" in report["logs"]["supervisor.log"]
    assert "relay-SECRET-value" not in json.dumps(report, ensure_ascii=False)


def test_a_session_registry_that_could_not_be_read_says_it_is_not_written(tmp_path: Path) -> None:
    # 启动时读不出来就从空表开始、这次运行不写：不报出来的话，人只会看到旧对话都另开了新会话
    operations = service(tmp_path)
    reason = "data/sessions.json 读不出来（PermissionError），这次运行不写它，会话登记从空表开始；修好后重启"
    operations.state._session_store = SimpleNamespace(load_error=reason, read_only=True)

    item = check(asyncio.run(operations.diagnose()), "会话登记")

    assert item["status"] == "warning" and item["detail"] == reason
    assert "重启" in item["action"] and "不写" in item["action"]


def test_a_session_registry_set_aside_points_at_the_kept_copy(tmp_path: Path) -> None:
    operations = service(tmp_path)
    reason = "data/sessions.json 格式不对，已改名为 sessions.json.corrupt-1 留存，会话登记从空表开始"
    operations.state._session_store = SimpleNamespace(load_error=reason, read_only=False)

    item = check(asyncio.run(operations.diagnose()), "会话登记")

    assert item["detail"] == reason and "改名留存" in item["action"]


def test_a_readable_session_registry_adds_no_check(tmp_path: Path) -> None:
    operations = service(tmp_path)
    operations.state._session_store = SimpleNamespace(load_error="", read_only=False)

    assert "会话登记" not in _names(asyncio.run(operations.diagnose()))
