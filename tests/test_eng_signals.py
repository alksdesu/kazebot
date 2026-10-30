"""signals.jsonl 是给监控看的：默认不落整段带工具参数和正文的事件，其余事件的正文字段换成占位，文件落在本实例工作区。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from engine.signals import bridge
from engine.signals.bus import SignalBus
from engine.signals.types import Signal

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _fresh_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "_bridge_installed", False)
    monkeypatch.setattr(bridge, "_SIGNALS_LOG", None)
    monkeypatch.setattr(bridge, "_bridge_patterns", None)
    monkeypatch.setattr(bridge, "_bridge_exclude_patterns", None)
    monkeypatch.setattr(bridge, "_redacted_keys", bridge.DEFAULT_REDACTED_PAYLOAD_KEYS)


def _install(tmp_path: Path, **kwargs: object) -> SignalBus:
    bus = SignalBus()
    bridge.install_event_bridge(bus, log_dir=tmp_path, **kwargs)
    return bus


def _entries(tmp_path: Path) -> list[dict]:
    assert bridge.flush_signal_writes()
    return [json.loads(line) for line in (tmp_path / "signals.jsonl").read_text(encoding="utf-8").splitlines()]


def test_full_content_events_are_dropped_by_default(tmp_path: Path) -> None:
    bus = _install(tmp_path)
    for name in bridge.DEFAULT_BRIDGE_EXCLUDE_PATTERNS:
        bus.emit(Signal(name=name, payload={"arguments": {"api_key": "sk-secret"}}))
    bus.emit(Signal(name="tool.call.end", payload={"tool": "write_file", "elapsed_ms": 3}))

    assert [entry["name"] for entry in _entries(tmp_path)] == ["tool.call.end"]


def test_payload_content_is_redacted_but_its_shape_is_kept(tmp_path: Path) -> None:
    bus = _install(tmp_path)
    summary = '{"content": "secret"}'
    bus.emit(Signal(name="tool.call.start", payload={
        "tool": "write_file", "args_summary": summary, "attachments": ["a", "b"],
        "result": {"ok": True}, "text": "", "elapsed_ms": 4,
    }))

    payload = _entries(tmp_path)[0]["payload"]

    assert payload["tool"] == "write_file" and payload["elapsed_ms"] == 4
    assert payload["args_summary"] == f"[redacted {len(summary)} chars]"
    assert payload["attachments"] == "[redacted 2 items]"
    assert payload["result"] == "[redacted]"
    assert payload["text"] == ""
    assert "secret" not in (tmp_path / "signals.jsonl").read_text(encoding="utf-8")


def test_an_explicit_exclude_list_replaces_the_default_but_redaction_still_applies(tmp_path: Path) -> None:
    bus = _install(tmp_path, exclude_patterns=[])
    bus.emit(Signal(name="tool_call_start", payload={"tool_name": "x", "arguments": {"token": "sk-secret"}}))

    entry = _entries(tmp_path)[0]

    assert entry["name"] == "tool_call_start"
    assert entry["payload"] == {"tool_name": "x", "arguments": "[redacted]"}


def test_redaction_can_be_switched_off_explicitly(tmp_path: Path) -> None:
    bus = _install(tmp_path, redact_keys=())
    bus.emit(Signal(name="custom", payload={"text": "kept"}))
    assert _entries(tmp_path)[0]["payload"] == {"text": "kept"}


def test_without_a_log_dir_the_file_lands_in_the_instance_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "resolve_workspace_root", lambda fallback: tmp_path / "instance")
    bridge.install_event_bridge(SignalBus())
    assert bridge._SIGNALS_LOG == tmp_path / "instance" / "data" / "signals.jsonl"


def test_the_bundled_runtime_defaults_cover_the_code_defaults() -> None:
    config = yaml.safe_load((_ROOT / "defaults" / "runtime.yaml").read_text(encoding="utf-8"))
    configured = set(config["engine"]["signals"]["bridge_exclude_patterns"])
    assert set(bridge.DEFAULT_BRIDGE_EXCLUDE_PATTERNS) <= configured


def test_the_runner_points_the_bridge_at_the_workspace_and_no_diagnostic_dump_is_left() -> None:
    runner_source = (_ROOT / "engine" / "runner.py").read_text(encoding="utf-8")
    assert 'log_dir=ws_root / "data"' in runner_source
    assert "DIAG-AISTEP" not in (_ROOT / "engine" / "inference" / "ai_step.py").read_text(encoding="utf-8")
