import json

from engine.signals import bridge
from engine.signals.bus import SignalBus
from engine.signals.types import Signal


def _install_test_bridge(monkeypatch, tmp_path, **kwargs) -> SignalBus:
    monkeypatch.setattr(bridge, "_bridge_installed", False)
    monkeypatch.setattr(bridge, "_SIGNALS_LOG", None)
    monkeypatch.setattr(bridge, "_bridge_patterns", None)
    monkeypatch.setattr(bridge, "_bridge_exclude_patterns", None)

    bus = SignalBus()
    bridge.install_event_bridge(bus, log_dir=tmp_path, **kwargs)
    return bus


def _written_names(tmp_path) -> list[str]:
    path = tmp_path / "signals.jsonl"
    return [
        json.loads(line)["name"]
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def test_bridge_exclude_patterns_drop_high_volume_signals(monkeypatch, tmp_path):
    bus = _install_test_bridge(
        monkeypatch,
        tmp_path,
        exclude_patterns=["stream_delta", "tool_call_delta", "tool_call_end"],
    )

    bus.emit(Signal(name="stream_delta"))
    bus.emit(Signal(name="tool_call_delta"))
    bus.emit(Signal(name="tool_call_end"))
    bus.emit(Signal(name="tool.call.end"))
    bus.emit(Signal(name="llm.call.end"))

    assert _written_names(tmp_path) == ["tool.call.end", "llm.call.end"]


def test_bridge_denylist_is_applied_after_allowlist(monkeypatch, tmp_path):
    bus = _install_test_bridge(
        monkeypatch,
        tmp_path,
        patterns=["llm.*"],
        exclude_patterns=["llm.retry"],
    )

    bus.emit(Signal(name="node_started"))
    bus.emit(Signal(name="llm.retry"))
    bus.emit(Signal(name="llm.call.start"))

    assert _written_names(tmp_path) == ["llm.call.start"]
