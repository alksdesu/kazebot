"""策略文件和运行参数文件写坏时，健康检查要说出来。

两份文件解析失败时沿用上一份能用的配置，起进程时就坏的用内置默认，服务照常跑；不报出来的话，
人只会看到自定义的规则、参数悄悄不生效。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config_layout  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
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
