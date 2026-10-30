import json
import logging
import threading
import time

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
    assert bridge.flush_signal_writes()
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


def test_signals_are_written_on_the_writer_thread_not_by_the_emitter(monkeypatch, tmp_path):
    bus = _install_test_bridge(monkeypatch, tmp_path)
    emitter = threading.get_ident()
    writers: list[int] = []
    real_append = bridge.append_lines

    def recorded(path, data):
        writers.append(threading.get_ident())
        real_append(path, data)

    monkeypatch.setattr(bridge, "append_lines", recorded)

    bus.emit(Signal(name="llm.call.end"))

    assert _written_names(tmp_path) == ["llm.call.end"]
    assert writers and emitter not in writers


def test_a_full_queue_drops_new_signals_and_reports_how_many(monkeypatch, tmp_path, caplog):
    writer = bridge._SignalWriter(limit=2)
    entered, released = threading.Event(), threading.Event()
    real_append = bridge.append_lines

    def stuck(path, data):
        entered.set()
        released.wait(timeout=5)
        real_append(path, data)

    monkeypatch.setattr(bridge, "append_lines", stuck)
    path = tmp_path / "signals.jsonl"

    def job(name: str) -> bridge._Append:
        return bridge._Append(path, name, (json.dumps({"name": name}) + "\n").encode("utf-8"))

    with caplog.at_level(logging.WARNING, logger="engine.signals.bridge"):
        writer.submit(job("first"))
        # 等写线程拿走第一条卡在磁盘上，队列里才正好空着两个位置
        assert entered.wait(timeout=5)
        for name in ("second", "third", "fourth", "fifth"):
            writer.submit(job(name))
        released.set()
        assert writer.flush(timeout=5)
        writer.report_drops(force=True)

    warnings = [record.getMessage() for record in caplog.records if record.name == "engine.signals.bridge"]
    assert warnings == ["signals log queue is full (2 waiting): dropped 1 signal(s)"] * 2
    assert _written_names(tmp_path) == ["first", "second", "third"]


def test_signals_still_queued_at_exit_are_written(monkeypatch, tmp_path):
    bus = _install_test_bridge(monkeypatch, tmp_path)
    assert bridge.flush_signal_writes()
    real_append = bridge.append_lines

    def slow(path, data):
        time.sleep(0.2)
        real_append(path, data)

    monkeypatch.setattr(bridge, "append_lines", slow)
    bus.emit(Signal(name="llm.call.end"))

    bridge._flush_at_exit()

    lines = (tmp_path / "signals.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["name"] for line in lines] == ["llm.call.end"]


def test_the_exit_flush_gives_up_after_its_time_limit_and_says_so(monkeypatch, tmp_path, caplog):
    bus = _install_test_bridge(monkeypatch, tmp_path)
    assert bridge.flush_signal_writes()
    released = threading.Event()
    real_append = bridge.append_lines

    def stuck(path, data):
        released.wait(timeout=5)
        real_append(path, data)

    monkeypatch.setattr(bridge, "append_lines", stuck)
    monkeypatch.setattr(bridge, "_EXIT_FLUSH_SEC", 0.2)
    bus.emit(Signal(name="llm.call.end"))
    try:
        with caplog.at_level(logging.WARNING, logger="engine.signals.bridge"):
            bridge._flush_at_exit()
    finally:
        released.set()

    assert "still unwritten" in caplog.text
    assert _written_names(tmp_path) == ["llm.call.end"]


def test_a_torn_last_line_left_by_a_crash_does_not_swallow_the_next_signal(monkeypatch, tmp_path):
    (tmp_path / "signals.jsonl").write_bytes(b'{"name": "torn", "payl')
    bus = _install_test_bridge(monkeypatch, tmp_path)

    bus.emit(Signal(name="llm.call.end"))
    assert bridge.flush_signal_writes()

    lines = (tmp_path / "signals.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines[0] == '{"name": "torn", "payl'
    assert json.loads(lines[1])["name"] == "llm.call.end"
