"""事件日志的回执表：投递 id 和系统任务的幂等键只查表、不读盘，重启后从留存的几份日志补回来，轮转丢掉的那份里的不再认；
带投递 id 的消息只写一次；从某个 seq 往后读时，最后一条不超过它的整份文件不读。"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.eventlog_rotation import EventLogRotationRecoveryError, rotate_event_log  # noqa: E402
from supervisor import eventlog as eventlog_module  # noqa: E402
from supervisor.eventlog import (  # noqa: E402
    INBOUND_DELIVERY,
    MEMORY_EXTRACT_TASK,
    OUTBOUND_DELIVERY,
    POST_PHASE_TASK,
    REPLY_DELIVERY,
    DeliveryIdTaken,
    EventLog,
    ReceiptKey,
)


def _log(tmp_path: Path, run_id: str = "receipts") -> EventLog:
    return EventLog(tmp_path / "data" / "events.jsonl", run_id=run_id)


def _append(log: EventLog, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return log.append(session_id="session-1", component="test", type_=event_type, payload=payload)


def _filler(log: EventLog, count: int) -> None:
    for index in range(count):
        _append(log, "probe", {"index": index})


def _reply(
    log: EventLog, delivery_id: str, text: str = "稍等", *, session_id: str = "session-1",
) -> tuple[dict[str, Any], bool]:
    return log.append_delivery(
        REPLY_DELIVERY, session_id=session_id, component="shell", payload={"delivery_id": delivery_id, "text": text},
    )


def _forbid_disk_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """之后再碰日志文件就算读了盘：扫留存日志、按 seq 跳读、拿跨进程文件锁都不许。"""

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("回执查询读了盘")

    monkeypatch.setattr(EventLog, "_iter_persisted_events_locked", refuse)
    monkeypatch.setattr(EventLog, "_iter_persisted_events_after_locked", refuse)
    monkeypatch.setattr(eventlog_module, "eventlog_file_lock", refuse)


def _write_retained(path: Path, files: list[range], pad: int = 60) -> None:
    """按从旧到新的顺序写几份留存日志（.N … .1、活动文件），每份里的 seq 由 range 给出。"""
    names = [f"{path.name}.{index}" for index in range(len(files) - 1, 0, -1)] + [path.name]
    path.parent.mkdir(parents=True, exist_ok=True)
    for name, seqs in zip(names, files):
        rows = [
            json.dumps({"seq": seq, "event_id": f"e-{seq}", "session_id": "s", "type": "probe", "payload": {"pad": "x" * pad}})
            for seq in seqs
        ]
        (path.parent / name).write_text("".join(row + "\n" for row in rows), encoding="utf-8")


class TestLookups:
    @pytest.mark.parametrize("receipt_key, payload", [
        pytest.param(INBOUND_DELIVERY, {"delivery_id": "inbound:d-1"}, id="inbound"),
        pytest.param(OUTBOUND_DELIVERY, {"delivery_id": "d-1"}, id="outbound"),
        pytest.param(REPLY_DELIVERY, {"delivery_id": "reply:d-1"}, id="reply"),
        pytest.param(POST_PHASE_TASK, {"task_id": "t-1", "input": {"_post_phase_id": "post:1:summary"}}, id="post-phase"),
        pytest.param(MEMORY_EXTRACT_TASK, {"task_id": "t-1", "input": {"_memory_extract_intent_id": "intent-1"}}, id="memory"),
    ])
    def test_each_kind_is_found_by_its_key_with_the_basic_fields_only(
        self, tmp_path: Path, receipt_key: ReceiptKey, payload: dict[str, Any],
    ) -> None:
        log = _log(tmp_path)
        event = _append(log, receipt_key.event_type, payload)
        value = receipt_key.value_in(payload)

        found = log.find_receipt(receipt_key, value)

        assert found == {
            "seq": event["seq"], "event_id": event["event_id"], "session_id": "session-1",
            "type": receipt_key.event_type, "ts": event["ts"],
        }
        assert log.find_receipt(receipt_key, f"{value}-other") is None
        assert log.find_receipt(receipt_key, "") is None

    def test_a_key_only_counts_for_its_own_kind(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        _append(log, "outbound_message", {"delivery_id": "d-1"})
        _append(log, "probe", {"delivery_id": "d-2"})

        assert log.find_outbound_delivery("d-1") is not None
        assert log.find_delivery_event("d-1", event_type="inbound_message") is None
        assert log.find_delivery_event("d-1", event_type="intermediate_reply") is None
        assert log.find_delivery_event("d-2", event_type="inbound_message") is None

    def test_one_task_created_event_can_answer_for_both_task_keys(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        event = _append(log, "task_created", {"input": {"_post_phase_id": "p-1", "_memory_extract_intent_id": "m-1"}})

        assert log.find_receipt(POST_PHASE_TASK, "p-1")["seq"] == event["seq"]
        assert log.find_receipt(MEMORY_EXTRACT_TASK, "m-1")["seq"] == event["seq"]
        assert log.find_receipt(POST_PHASE_TASK, "m-1") is None

    def test_a_post_phase_task_is_known_by_its_phase_id(self, tmp_path: Path) -> None:
        # 轮摘要原来就按 _post_phase_id 认；只带它的老事件照样认得，只带 _post_delivery_id 的不算
        log = _log(tmp_path)
        old = _append(log, "task_created", {"input": {"_post_phase_id": "post:old:summary"}})
        _append(log, "task_created", {"input": {"_post_delivery_id": "post:other:summary"}})

        assert log.find_receipt(POST_PHASE_TASK, "post:old:summary")["seq"] == old["seq"]
        assert log.find_receipt(POST_PHASE_TASK, "post:other:summary") is None

    def test_task_snapshots_keep_their_own_delivery_ids_out_of_the_table(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        for event_type in ("task_created", "task_completed", "task_routed"):
            _append(log, event_type, {"task_id": "t-1", "delivery_id": "task:t-1:completion:1:v1", "input": {}})

        assert log._receipts == {}

    def test_only_the_listed_keys_can_be_asked_for(self, tmp_path: Path) -> None:
        log = _log(tmp_path)

        with pytest.raises(ValueError):
            log.find_receipt(ReceiptKey("task_created", ("input", "_target_task_id")), "t-1")
        with pytest.raises(ValueError):
            log.find_delivery_event("d-1", event_type="task_created")
        with pytest.raises(ValueError):
            log.append_delivery(POST_PHASE_TASK, session_id="s", component="test", payload={"input": {}})


class TestAcrossRestartsAndRotation:
    def test_receipts_from_before_a_restart_are_found_without_reading_the_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(eventlog_module, "_MAX_MEMORY_EVENTS", 3)
        first = _log(tmp_path)
        outbound = _append(first, "outbound_message", {"delivery_id": "d-old"})
        created = _append(first, "task_created", {"input": {"_memory_extract_intent_id": "intent-old"}})
        _filler(first, 10)

        restarted = _log(tmp_path, run_id="restarted")
        assert outbound["seq"] not in {event["seq"] for event in restarted.events}
        _forbid_disk_reads(monkeypatch)

        assert restarted.find_outbound_delivery("d-old")["event_id"] == outbound["event_id"]
        assert restarted.find_receipt(MEMORY_EXTRACT_TASK, "intent-old")["event_id"] == created["event_id"]

    def test_receipts_in_backups_are_found_after_a_restart(self, tmp_path: Path) -> None:
        first = _log(tmp_path)
        old = _append(first, "inbound_message", {"delivery_id": "inbound:d-1"})
        first.path.rename(first.path.with_name(f"{first.path.name}.1"))
        _append(first, "probe", {})

        restarted = _log(tmp_path, run_id="restarted")

        assert restarted.find_delivery_event("inbound:d-1", event_type="inbound_message")["seq"] == old["seq"]

    def test_a_receipt_is_forgotten_once_online_rotation_drops_its_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(eventlog_module, "_ONLINE_ROTATE_MAX_BYTES", 1)
        monkeypatch.setattr(eventlog_module, "_ONLINE_ROTATE_BACKUPS", 1)
        monkeypatch.setattr(eventlog_module, "_ONLINE_ROTATE_CHECK_EVERY", 1)
        log = _log(tmp_path)
        _append(log, "outbound_message", {"delivery_id": "dropped"})
        assert log.find_outbound_delivery("dropped") is not None

        kept = _append(log, "outbound_message", {"delivery_id": "kept"})

        assert [event["seq"] for event in log.iter_persisted_events()] == [kept["seq"]]
        assert log.find_outbound_delivery("dropped") is None
        assert log.find_outbound_delivery("kept")["seq"] == kept["seq"]

    def test_rotation_by_another_process_is_noticed_at_the_next_size_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # data_cleanup 在别的进程里轮转，本进程只能在下一次检查大小时对一遍最旧那份
        monkeypatch.setattr(eventlog_module, "_ONLINE_ROTATE_BACKUPS", 1)
        monkeypatch.setattr(eventlog_module, "_ONLINE_ROTATE_CHECK_EVERY", 1)
        log = _log(tmp_path)
        _append(log, "outbound_message", {"delivery_id": "dropped"})
        rotate_event_log(log.path, max_bytes=1, backups=1)
        kept = _append(log, "outbound_message", {"delivery_id": "kept"})
        assert log.find_outbound_delivery("dropped") is not None
        rotate_event_log(log.path, max_bytes=1, backups=1)

        latest = _append(log, "outbound_message", {"delivery_id": "latest"})

        assert log.find_outbound_delivery("dropped") is None
        assert log.find_outbound_delivery("kept")["seq"] == kept["seq"]
        assert log.find_outbound_delivery("latest")["seq"] == latest["seq"]


class TestWriteOnce:
    def test_a_repeat_is_not_written_and_gets_the_first_event(self, tmp_path: Path) -> None:
        log = _log(tmp_path)

        first, first_fresh = _reply(log, "reply:r-1")
        again, again_fresh = _reply(log, "reply:r-1", text="稍等（重发）")

        assert first_fresh is True and again_fresh is False
        assert (again["event_id"], again["seq"]) == (first["event_id"], first["seq"])
        assert [event["payload"]["text"] for event in log.iter_persisted_events()] == ["稍等"]

    def test_a_repeat_after_a_restart_is_not_written_either(self, tmp_path: Path) -> None:
        first, _fresh = _reply(_log(tmp_path), "reply:r-1")

        again, fresh = _reply(_log(tmp_path, run_id="restarted"), "reply:r-1")

        assert fresh is False and again["event_id"] == first["event_id"]
        assert len(list(_log(tmp_path, run_id="reader").iter_persisted_events())) == 1

    def test_an_id_already_written_by_another_session_is_refused_and_not_written(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        first, _fresh = _reply(log, "reply:r-1")

        with pytest.raises(DeliveryIdTaken):
            _reply(log, "reply:r-1", text="借来的 id", session_id="session-2")
        again, fresh = _reply(log, "reply:r-1")

        assert fresh is False and again["event_id"] == first["event_id"]
        assert [(event["session_id"], event["payload"]["text"]) for event in log.iter_persisted_events()] == [
            ("session-1", "稍等"),
        ]

    def test_messages_without_an_id_are_always_written(self, tmp_path: Path) -> None:
        log = _log(tmp_path)

        answers = [_reply(log, "") for _ in range(2)]

        assert [fresh for _event, fresh in answers] == [True, True]
        assert len(list(log.iter_persisted_events())) == 2

    def test_repeats_arriving_together_land_once(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        barrier = threading.Barrier(8)
        answers: list[tuple[dict[str, Any], bool]] = []

        def send() -> None:
            barrier.wait()
            answers.append(_reply(log, "reply:r-1"))

        threads = [threading.Thread(target=send) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive()

        assert sum(fresh for _event, fresh in answers) == 1
        assert len({event["event_id"] for event, _fresh in answers}) == 1
        assert len(list(log.iter_persisted_events())) == 1

    def test_a_message_on_disk_before_a_failed_rotation_is_not_written_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def broken_rotation(*_args: Any, **_kwargs: Any) -> bool:
            raise EventLogRotationRecoveryError("rotation needs recovery")

        monkeypatch.setattr(eventlog_module, "_ONLINE_ROTATE_CHECK_EVERY", 1)
        monkeypatch.setattr(eventlog_module, "rotate_event_log_locked", broken_rotation)
        log = _log(tmp_path)
        with pytest.raises(EventLogRotationRecoveryError):
            _reply(log, "reply:r-1")

        _again, fresh = _reply(log, "reply:r-1")

        assert fresh is False
        assert len(list(log.iter_persisted_events())) == 1


class TestReadingAfterASeq:
    @pytest.fixture(autouse=True)
    def _small_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(eventlog_module, "_SEEK_WINDOW_BYTES", 2048)
        monkeypatch.setattr(eventlog_module, "_ONLINE_ROTATE_BACKUPS", 2)

    @pytest.mark.parametrize("after_seq, expected", [
        pytest.param(0, range(1, 1201), id="before-everything"),
        pytest.param(399, range(400, 1201), id="end-of-oldest-but-one"),
        pytest.param(400, range(401, 1201), id="exactly-the-end-of-a-file"),
        pytest.param(1000, range(1001, 1201), id="middle-of-the-active-file"),
        pytest.param(1200, range(0), id="nothing-newer"),
    ])
    def test_every_event_after_the_seq_comes_back_in_order(
        self, tmp_path: Path, after_seq: int, expected: range,
    ) -> None:
        log = _log(tmp_path)
        _write_retained(log.path, [range(1, 401), range(401, 801), range(801, 1201)])

        seqs = [event["seq"] for event in log.iter_persisted_events_after(after_seq)]

        assert seqs == list(expected)

    def test_an_unreadable_line_stops_the_seeking_but_not_the_reading(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        _write_retained(log.path, [range(1, 401)])
        lines = log.path.read_text(encoding="utf-8").splitlines(keepends=True)
        # 文件中段整片读不懂，第一次二分就落在里面
        for index in range(150, 251):
            lines[index] = "{torn line\n"
        log.path.write_text("".join(lines), encoding="utf-8")

        seqs = [event["seq"] for event in log.iter_persisted_events_after(300)]

        assert seqs == list(range(301, 401))
