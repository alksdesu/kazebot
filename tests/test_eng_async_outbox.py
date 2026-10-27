"""异步工具结果送不到 supervisor 时不能悄悄丢：瞬时错误重试，重试完落盘，worker 之后补投。"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.inference import async_delivery
from engine.inference.ai_step import _run_async_deliver
from engine.inference.async_delivery import (
    build_async_result_payload,
    deliver_async_result,
    flush_async_result_outbox,
    outbox_dir,
)
from file_lock import exclusive_lock


class _Http:
    def __init__(self, *answers: object) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, dict]] = []

    async def post(self, url: str, *, json: dict) -> object:
        self.calls.append((url, json))
        answer = self.answers.pop(0) if self.answers else 200
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(status_code=answer)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(async_delivery, "_RETRY_DELAYS_SEC", (0.0, 0.0, 0.0))


def _payload(text: str = "done") -> dict:
    return build_async_result_payload(message=text, tool_name="demo", task_id="task", node_id="entry")


def _queued(root: Path) -> list[Path]:
    directory = outbox_dir(root)
    return sorted(directory.glob("*.json")) if directory.is_dir() else []


def test_transient_failures_are_retried_until_delivered(tmp_path: Path) -> None:
    http = _Http(503, OSError("reset"), 200)

    assert asyncio.run(deliver_async_result(http, "http://sup", "session", _payload(), workspace_root=tmp_path)) is True

    assert len(http.calls) == 3
    assert http.calls[0][0] == "http://sup/v1/async_tools/results"
    assert http.calls[0][1] == {**_payload(), "session_id": "session"}
    assert _queued(tmp_path) == []


@pytest.mark.parametrize("missing", [404, 405])
def test_a_supervisor_without_the_task_route_gets_it_by_session(tmp_path: Path, missing: int) -> None:
    http = _Http(missing, 200)

    assert asyncio.run(deliver_async_result(http, "http://sup", "session", _payload(), workspace_root=tmp_path)) is True

    assert [url for url, _body in http.calls] == [
        "http://sup/v1/async_tools/results", "http://sup/v1/sessions/session/async_tool_result",
    ]
    assert http.calls[1][1] == _payload()


def test_an_old_supervisor_that_no_longer_knows_the_session_rejects_it(tmp_path: Path) -> None:
    http = _Http(404, 404)

    assert asyncio.run(deliver_async_result(http, "http://sup", "session", _payload(), workspace_root=tmp_path)) is False

    assert len(http.calls) == 2
    assert _queued(tmp_path) == []


def test_a_flush_to_an_older_supervisor_also_falls_back(tmp_path: Path) -> None:
    _queue(tmp_path, "kept")
    http = _Http(404, 200)

    assert asyncio.run(flush_async_result_outbox(http, "http://sup", tmp_path)) == 1

    assert http.calls[1][0] == "http://sup/v1/sessions/session/async_tool_result"
    assert _queued(tmp_path) == []


def test_only_a_cancelled_result_carries_the_flag() -> None:
    plain = build_async_result_payload(message="m", tool_name="t", task_id="task", node_id="n", failure_error="boom")
    cancelled = build_async_result_payload(
        message="m", tool_name="t", task_id="task", node_id="n", failure_error="task cancelled", cancelled=True,
    )

    assert "cancelled" not in plain and plain["success"] is False
    assert cancelled["cancelled"] is True and cancelled["success"] is False


def test_a_rejected_result_is_not_retried_or_queued(tmp_path: Path) -> None:
    http = _Http(400)

    assert asyncio.run(deliver_async_result(http, "http://sup", "session", _payload(), workspace_root=tmp_path)) is False

    assert len(http.calls) == 1
    assert _queued(tmp_path) == []


def test_an_undeliverable_result_is_queued_on_disk(tmp_path: Path) -> None:
    http = _Http(503, 503, 503, 503)

    assert asyncio.run(deliver_async_result(http, "http://sup", "session", _payload("kept"), workspace_root=tmp_path)) is False

    assert len(http.calls) == 4
    [queued] = _queued(tmp_path)
    record = json.loads(queued.read_text(encoding="utf-8"))
    assert record["session_id"] == "session" and record["payload"]["message"] == "kept"


def _queue(root: Path, *texts: str) -> None:
    for text in texts:
        asyncio.run(deliver_async_result(_Http(503, 503, 503, 503), "http://sup", "session", _payload(text), workspace_root=root))


def test_the_flush_redelivers_queued_results_in_order_and_removes_them(tmp_path: Path) -> None:
    _queue(tmp_path, "first", "second")
    http = _Http(200, 200)

    assert asyncio.run(flush_async_result_outbox(http, "http://sup", tmp_path)) == 2

    assert [body["message"] for _url, body in http.calls] == ["first", "second"]
    assert _queued(tmp_path) == []


def test_the_flush_stops_while_the_supervisor_is_still_unreachable(tmp_path: Path) -> None:
    _queue(tmp_path, "first", "second")
    http = _Http(OSError("still down"))

    assert asyncio.run(flush_async_result_outbox(http, "http://sup", tmp_path)) == 0

    assert len(http.calls) == 1
    assert len(_queued(tmp_path)) == 2


def test_expired_and_unreadable_entries_are_dropped_without_posting(tmp_path: Path) -> None:
    directory = outbox_dir(tmp_path)
    directory.mkdir(parents=True)
    (directory / "1-old.json").write_text(json.dumps({
        "session_id": "session", "payload": _payload("stale"), "queued_at": time.time() - 3 * 24 * 3600,
    }), encoding="utf-8")
    (directory / "2-bad.json").write_text("{not json", encoding="utf-8")
    http = _Http()

    assert asyncio.run(flush_async_result_outbox(http, "http://sup", tmp_path)) == 0

    assert http.calls == []
    assert _queued(tmp_path) == []


def test_only_one_flusher_works_the_outbox_at_a_time(tmp_path: Path) -> None:
    _queue(tmp_path, "first")
    http = _Http(200)

    with exclusive_lock(outbox_dir(tmp_path) / ".flush.lock"):
        assert asyncio.run(flush_async_result_outbox(http, "http://sup", tmp_path)) == 0

    assert http.calls == []
    assert len(_queued(tmp_path)) == 1


def test_no_outbox_means_no_supervisor_traffic(tmp_path: Path) -> None:
    assert asyncio.run(flush_async_result_outbox(object(), "http://sup", tmp_path)) == 0


class _Registry:
    def get_spec(self, name: str) -> None:
        return None


def test_a_finished_async_tool_whose_callback_fails_is_queued_not_dropped(tmp_path: Path) -> None:
    asyncio.run(_run_async_deliver(
        registry=_Registry(), http=_Http(OSError("down"), OSError("down"), OSError("down"), OSError("down")),
        supervisor_url="http://sup", task_id="task", session_id="session", tool_name="demo", tool_args={},
        async_tool_id="async-1", started_at=time.monotonic(), node_id="entry",
        result={"ok": True, "data": {"result": "done"}}, workspace_root=tmp_path,
    ))

    [queued] = _queued(tmp_path)
    record = json.loads(queued.read_text(encoding="utf-8"))
    assert record["payload"]["success"] is True
    assert record["payload"]["message"].startswith('✅ Async tool "demo"')


def test_a_cancelled_async_tool_is_reported_as_a_failure(tmp_path: Path) -> None:
    http = _Http(200)
    asyncio.run(_run_async_deliver(
        registry=_Registry(), http=http, supervisor_url="http://sup", task_id="task", session_id="session",
        tool_name="demo", tool_args={}, async_tool_id="async-2", started_at=time.monotonic(), node_id="entry",
        result={"ok": False, "error": "task cancelled", "data": {"cancelled": True}},
    ))
    body = http.calls[0][1]
    assert body["success"] is False and body["error"] == "task cancelled"
    assert body["cancelled"] is True
