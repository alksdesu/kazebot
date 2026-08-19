"""自动记忆提取门控回归测试。

门控曾比对全局 shell.entry_node_id，而各平台入口是 per-session 的
（qq.orchestrator / qq.vision / draw.*），全局标量不可能同时等于它们，
导致 memory.auto_extract.enabled=true 从未生效。
"""
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from engine.builtin.memory_extract import MemoryExtractHandler

_PASS_MARKER = "passed gates"
_BLOCK_MARKER = "gate: blocked by"


def _write_runtime(
    tmp_path: Path,
    *,
    enabled: bool = True,
    entry_node_id: str = "bootstrap.shell_orchestrator",
) -> None:
    cfg = {
        "memory": {"auto_extract": {"enabled": enabled, "node_id": "system.memory_extractor"}},
        "shell": {"entry_node_id": entry_node_id},
    }
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "runtime.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _task(
    *,
    node_id: str = "qq.orchestrator",
    source_inbound_seq: int | None = 7,
    action: str = "finish",
    caller_task_id: str | None = None,
    task_id: str = "task-1",
    task_input: dict[str, Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        task_id=task_id,
        node_id=node_id,
        kind="node",
        result={"action": action},
        input=task_input if task_input is not None else {},
        source_inbound_seq=source_inbound_seq,
        caller_task_id=caller_task_id,
        session_id="sess-1",
    )


def _gate_passed(tmp_path: Path, task: SimpleNamespace, caplog: pytest.LogCaptureFixture) -> bool:
    """跑门控并按日志判定是否放行；门控之后的调度逻辑不在本测试范围内。"""
    handler = MemoryExtractHandler()
    ctx: dict[str, Any] = {
        "workspace_root": str(tmp_path),
        "task": task,
        "session_id": "sess-1",
        "get_conversation_messages": lambda *a, **k: [],
        "read_memory_extract_intent": lambda *a, **k: None,
        "write_memory_extract_intent": lambda *a, **k: None,
        "cancel_memory_extract_intents": lambda *a, **k: None,
        "acquire_lock": None,
    }
    with caplog.at_level(logging.DEBUG, logger="engine.builtin.memory_extract"):
        handler.on_task_complete(ctx)
    try:
        for timer in handler._memory_extract_timers.values():
            timer.cancel()
    except Exception:
        pass
    text = caplog.text
    assert not (_PASS_MARKER in text and _BLOCK_MARKER in text), "门控结论不应同时放行与拦截"
    return _PASS_MARKER in text


def test_qq_entry_node_passes_gate(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """QQ 入口节点与全局 shell.entry_node_id 不同，仍应放行。"""
    _write_runtime(tmp_path)

    assert _gate_passed(tmp_path, _task(node_id="qq.orchestrator"), caplog)


def test_vision_entry_node_passes_gate(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """带图消息被路由到 qq.vision，同样是真实用户轮次。"""
    _write_runtime(tmp_path)

    assert _gate_passed(tmp_path, _task(node_id="qq.vision"), caplog)


def test_draw_node_passes_gate(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _write_runtime(tmp_path)

    assert _gate_passed(tmp_path, _task(node_id="draw.novelai_planner"), caplog)


def test_task_without_inbound_source_is_blocked(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """非 inbound 创建的 task（系统内部任务）不应触发提取。"""
    _write_runtime(tmp_path)

    assert not _gate_passed(tmp_path, _task(source_inbound_seq=None), caplog)


def test_non_finish_action_is_blocked(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _write_runtime(tmp_path)

    assert not _gate_passed(tmp_path, _task(action="dispatch"), caplog)


def test_system_task_is_blocked(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _write_runtime(tmp_path)

    assert not _gate_passed(tmp_path, _task(task_input={"_system_task": True}), caplog)


def test_fresh_child_task_is_blocked(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """一次性子节点会话 24h 内清理，提取无意义。"""
    _write_runtime(tmp_path)

    task = _task(caller_task_id="parent-1", task_input={"dispatch_context_mode": "fresh"})

    assert not _gate_passed(tmp_path, task, caplog)


def test_persistent_child_task_passes_gate(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """长驻子节点持有有价值的上下文，应放行（旧门控把这条路堵死了）。"""
    _write_runtime(tmp_path)

    task = _task(caller_task_id="parent-1", task_input={"dispatch_context_mode": "persistent"})

    assert _gate_passed(tmp_path, task, caplog)


def test_branch_task_is_blocked(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _write_runtime(tmp_path)

    assert not _gate_passed(tmp_path, _task(task_id="branch_abc"), caplog)


def test_disabled_auto_extract_is_blocked(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _write_runtime(tmp_path, enabled=False)

    assert not _gate_passed(tmp_path, _task(), caplog)


@pytest.mark.parametrize("entry_node_id", ["bootstrap.shell_orchestrator", "ereuna_main", ""])
def test_gate_no_longer_depends_on_global_entry_node(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, entry_node_id: str
) -> None:
    """无论全局 shell.entry_node_id 配成什么，都不影响门控结果。"""
    _write_runtime(tmp_path, entry_node_id=entry_node_id)

    assert _gate_passed(tmp_path, _task(node_id="qq.orchestrator"), caplog)
