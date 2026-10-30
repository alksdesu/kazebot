"""记忆的到期时间：写入时怎么读、到点不再注入、每小时清理删掉，dream 每晚审时效和长期价值。

「明天考试」这种记忆，事情过去了关键词照样天天命中，按「多久没用到」淘汰永远轮不到它；
一次性的调研话题也一样，没人删就一直占着注入预算。
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine import memory_admin, memory_subjects  # noqa: E402
from engine import memory_hit_cache as hits  # noqa: E402
from engine.builtin.dream import DreamHandler  # noqa: E402
from engine.builtin.knowledge_inject import list_memories, load_memory_catalog, save_memory  # noqa: E402
from engine.builtin.memory_extract import MemoryExtractHandler, _format_transcript_for_extract  # noqa: E402
from engine.memory_expiry import (  # noqa: E402
    EXPIRY_ACTIVE,
    EXPIRY_EXPIRED,
    EXPIRY_NONE,
    EXPIRY_UNREADABLE,
    REVIEWED_AT,
    ExpiryPassed,
    ExpiryValueError,
    expiry_state,
    memory_timezone,
    new_expiry,
    now_text,
    parse_expiry,
)
from engine.memory_namespace import conversation_namespace, subject_namespace  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402

_CONV_KEY = "qq_group:bc3f12b0621298e191a49fa7"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_NOW = datetime(2026, 10, 31, 4, 0, tzinfo=timezone.utc)
_PAST = "2020-01-01"
_FUTURE = "2099-12-31"


def _ctx(root: Path) -> ToolContext:
    return ToolContext(
        supervisor_url="http://localhost:0",
        session_id="sess-1",
        run_id="run-1",
        worker_id="worker-1",
        workspace_root=root,
        http=None,  # type: ignore[arg-type]
        registry=None,
        conversation_key=_CONV_KEY,
        node_id="system.memory_extractor",
    )


def _save(root: Path, **args: Any) -> dict[str, Any]:
    base = {"id": "exam", "book": "plans", "content": "UserA 2026-11-01 考试", "keywords": ["考试"]}
    return asyncio.run(save_memory({**base, **args}, _ctx(root)))


def _book_path(root: Path, namespace: str, book: str) -> Path:
    return root / "data" / "memory" / namespace / f"{book}.yaml"


def _write_book(root: Path, namespace: str, book: str, entries: list[dict[str, Any]]) -> Path:
    path = _book_path(root, namespace, book)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"book": book, "entries": entries}, allow_unicode=True), encoding="utf-8")
    return path


def _entries(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("entries") or []


def _stored(root: Path, entry_id: str = "exam", book: str = "plans") -> dict[str, Any]:
    path = _book_path(root, conversation_namespace(_CONV_KEY), book)
    return next(entry for entry in _entries(path) if entry["id"] == entry_id)


def _write_runtime(root: Path, memory: dict[str, Any]) -> None:
    path = root / "config" / "runtime.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"memory": memory}, allow_unicode=True), encoding="utf-8")


class TestParse:
    def test_a_bare_date_lasts_through_that_day_in_the_configured_zone(self) -> None:
        moment = parse_expiry("2026-11-05", _SHANGHAI)

        assert moment == datetime(2026, 11, 5, 23, 59, 59, tzinfo=_SHANGHAI)
        assert moment.utcoffset() == timedelta(hours=8)

    def test_a_time_without_an_offset_is_read_in_the_configured_zone(self) -> None:
        assert parse_expiry("2026-11-05 18:00", _SHANGHAI) == datetime(2026, 11, 5, 18, 0, tzinfo=_SHANGHAI)
        assert parse_expiry("2026-11-05T18:00", _SHANGHAI) == datetime(2026, 11, 5, 18, 0, tzinfo=_SHANGHAI)

    def test_an_explicit_offset_wins_over_the_zone(self) -> None:
        assert parse_expiry("2026-11-05T10:00:00Z", _SHANGHAI) == datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)
        assert parse_expiry("2026-11-05T18:00+09:00", _SHANGHAI).utcoffset() == timedelta(hours=9)

    def test_unquoted_yaml_dates_are_read_too(self) -> None:
        # 手写的 expires_at: 2026-11-05 不加引号，yaml 读出来是 date，不是字符串。
        loaded = yaml.safe_load("a: 2026-11-05\nb: 2026-11-05 18:00:00\n")

        assert isinstance(loaded["a"], date)
        assert parse_expiry(loaded["a"], _SHANGHAI) == datetime(2026, 11, 5, 23, 59, 59, tzinfo=_SHANGHAI)
        assert parse_expiry(loaded["b"], _SHANGHAI) == datetime(2026, 11, 5, 18, 0, tzinfo=_SHANGHAI)

    @pytest.mark.parametrize("blank", [None, "", "   "])
    def test_blank_means_no_expiry(self, blank: Any) -> None:
        assert parse_expiry(blank, _SHANGHAI) is None

    @pytest.mark.parametrize("bad", ["下周", "2026-13-01", "tomorrow", 20261105, True, ["2026-11-05"]])
    def test_anything_else_is_refused(self, bad: Any) -> None:
        with pytest.raises(ExpiryValueError):
            parse_expiry(bad, _SHANGHAI)

    def test_a_new_expiry_in_the_past_is_refused(self) -> None:
        with pytest.raises(ExpiryPassed):
            new_expiry("2026-10-30", _SHANGHAI, _NOW)

        assert new_expiry("2026-10-31", _SHANGHAI, _NOW) == datetime(2026, 10, 31, 23, 59, 59, tzinfo=_SHANGHAI)

    def test_states(self) -> None:
        assert expiry_state({}, _SHANGHAI, _NOW) == EXPIRY_NONE
        assert expiry_state({"expires_at": _FUTURE}, _SHANGHAI, _NOW) == EXPIRY_ACTIVE
        assert expiry_state({"expires_at": _PAST}, _SHANGHAI, _NOW) == EXPIRY_EXPIRED
        assert expiry_state({"expires_at": "下周"}, _SHANGHAI, _NOW) == EXPIRY_UNREADABLE

    def test_the_current_time_carries_the_weekday_and_the_zone(self) -> None:
        assert now_text(_SHANGHAI, _NOW) == "2026-10-31 12:00 周六（Asia/Shanghai）"


class TestTimezoneSetting:
    def test_the_shipped_default_is_shanghai(self, tmp_path: Path) -> None:
        assert memory_timezone(tmp_path) == _SHANGHAI

    def test_the_instance_can_pick_another_zone(self, tmp_path: Path) -> None:
        _write_runtime(tmp_path, {"timezone": "Asia/Tokyo"})

        assert memory_timezone(tmp_path) == ZoneInfo("Asia/Tokyo")

    def test_an_unknown_zone_falls_back_to_the_default(self, tmp_path: Path, caplog) -> None:
        _write_runtime(tmp_path, {"timezone": "Mars/Olympus"})

        with caplog.at_level("WARNING"):
            assert memory_timezone(tmp_path) == _SHANGHAI
        assert "Mars/Olympus" in caplog.text


class TestSaveMemory:
    def test_a_date_is_stored_as_the_end_of_that_day_with_its_offset(self, tmp_path: Path) -> None:
        result = _save(tmp_path, expires_at="2099-11-02")

        assert result["ok"] is True
        assert result["data"]["expires_at"] == "2099-11-02T23:59:59+08:00"
        assert _stored(tmp_path)["expires_at"] == "2099-11-02T23:59:59+08:00"

    def test_a_memory_that_is_already_over_is_not_saved(self, tmp_path: Path) -> None:
        result = _save(tmp_path, expires_at=_PAST)

        assert result["ok"] is False
        assert "already passed" in result["error"]
        assert not _book_path(tmp_path, conversation_namespace(_CONV_KEY), "plans").exists()

    def test_an_unreadable_expiry_is_refused_with_the_accepted_forms(self, tmp_path: Path) -> None:
        result = _save(tmp_path, expires_at="下周")

        assert result["ok"] is False
        assert "YYYY-MM-DD" in result["error"]

    def test_without_expires_at_the_memory_is_lasting(self, tmp_path: Path) -> None:
        _save(tmp_path)

        assert "expires_at" not in _stored(tmp_path)

    def test_an_update_that_leaves_it_out_keeps_the_expiry(self, tmp_path: Path) -> None:
        _save(tmp_path, expires_at=_FUTURE)

        assert _save(tmp_path, content="UserA 2026-11-01 期中考试")["ok"] is True
        assert _stored(tmp_path)["expires_at"] == "2099-12-31T23:59:59+08:00"

    def test_an_empty_string_makes_it_lasting_again(self, tmp_path: Path) -> None:
        _save(tmp_path, expires_at=_FUTURE)

        assert _save(tmp_path, expires_at="")["ok"] is True
        assert "expires_at" not in _stored(tmp_path)

    def test_the_expiry_of_an_existing_constant_memory_cannot_be_changed(self, tmp_path: Path) -> None:
        # 改得动就能先改成马上到期，让定时清理替它删掉常驻记忆。
        _save(tmp_path, constant=True)

        refused = _save(tmp_path, expires_at=_FUTURE)

        assert refused["ok"] is False
        assert "constant" in refused["error"]
        assert "expires_at" not in _stored(tmp_path)
        assert _save(tmp_path, expires_at="")["ok"] is True

    def test_a_new_constant_memory_may_be_temporary(self, tmp_path: Path) -> None:
        assert _save(tmp_path, constant=True, expires_at=_FUTURE)["ok"] is True
        assert _stored(tmp_path)["expires_at"] == "2099-12-31T23:59:59+08:00"

    def test_an_update_does_not_silently_keep_an_expiry_that_has_passed(self, tmp_path: Path) -> None:
        path = _write_book(tmp_path, conversation_namespace(_CONV_KEY), "plans", [{
            "id": "exam", "content": "旧的", "keywords": ["考试"], "source": "auto", "expires_at": _PAST,
        }])

        refused = _save(tmp_path, content="新的")

        assert refused["ok"] is False
        assert "expired at" in refused["error"]
        assert _entries(path)[0]["content"] == "旧的"
        assert _save(tmp_path, content="新的", expires_at=_FUTURE)["ok"] is True

    def test_an_update_drops_the_review_mark(self, tmp_path: Path) -> None:
        # 内容改过就是新的一版，得重新审。
        _write_book(tmp_path, conversation_namespace(_CONV_KEY), "plans", [{
            "id": "exam", "content": "旧的", "keywords": ["考试"], "source": "auto", REVIEWED_AT: "2026-10-30T00:00:00+00:00",
        }])

        _save(tmp_path, content="新的")

        assert REVIEWED_AT not in _stored(tmp_path)

    def test_list_memories_shows_the_expiry_and_whether_it_passed(self, tmp_path: Path) -> None:
        _write_book(tmp_path, conversation_namespace(_CONV_KEY), "plans", [
            {"id": "over", "content": "c", "keywords": ["k"], "source": "auto", "expires_at": _PAST},
            {"id": "soon", "content": "c", "keywords": ["k"], "source": "auto", "expires_at": _FUTURE},
            {"id": "always", "content": "c", "keywords": ["k"], "source": "auto"},
        ])

        rows = {row["id"]: row for row in asyncio.run(list_memories({}, _ctx(tmp_path)))["data"]["entries"]}

        assert (rows["over"]["expires_at"], rows["over"]["expired"]) == ("2020-01-01T23:59:59+08:00", True)
        assert (rows["soon"]["expires_at"], rows["soon"]["expired"]) == ("2099-12-31T23:59:59+08:00", False)
        assert (rows["always"]["expires_at"], rows["always"]["expired"]) == ("", False)


class TestInjection:
    def test_an_expired_memory_is_no_longer_injected(self, tmp_path: Path) -> None:
        namespace = conversation_namespace(_CONV_KEY)
        _write_book(tmp_path, namespace, "plans", [
            {"id": "over", "content": "考完了", "keywords": ["考试"], "source": "auto", "expires_at": _PAST},
            {"id": "soon", "content": "还没考", "keywords": ["考试"], "source": "auto", "expires_at": _FUTURE},
            {"id": "pinned", "content": "常驻也到期", "constant": True, "source": "manual", "expires_at": _PAST},
        ])

        ids = [item["id"] for item in load_memory_catalog(tmp_path, memory_book=namespace, _use_cache=False)]

        assert ids == ["soon"]

    def test_an_unreadable_expiry_never_hides_the_memory(self, tmp_path: Path) -> None:
        namespace = conversation_namespace(_CONV_KEY)
        _write_book(tmp_path, namespace, "plans", [
            {"id": "odd", "content": "写坏了", "keywords": ["考试"], "source": "auto", "expires_at": "下周"},
        ])

        ids = [item["id"] for item in load_memory_catalog(tmp_path, memory_book=namespace, _use_cache=False)]

        assert ids == ["odd"]


class TestConsole:
    def test_an_entry_saved_with_a_date_gets_the_end_of_that_day(self, tmp_path: Path) -> None:
        namespace = conversation_namespace(_CONV_KEY)
        payload = {"id": "outing", "content": "周六团建", "keywords": ["团建"], "expires_at": "2099-11-08"}

        saved = memory_admin.upsert_entry(tmp_path, namespace, payload)

        assert saved["expires_at"] == "2099-11-08T23:59:59+08:00"
        assert _entries(_book_path(tmp_path, namespace, "default"))[0]["expires_at"] == "2099-11-08T23:59:59+08:00"

    @pytest.mark.parametrize("value, message", [(_PAST, "已经过了"), ("下周", "读不懂")])
    def test_a_past_or_unreadable_date_is_refused(self, tmp_path: Path, value: str, message: str) -> None:
        payload = {"id": "outing", "content": "周六团建", "keywords": ["团建"], "expires_at": value}

        with pytest.raises(memory_admin.MemoryAdminError, match=message):
            memory_admin.upsert_entry(tmp_path, conversation_namespace(_CONV_KEY), payload)

    def test_the_listing_reads_the_expiry_the_way_cleanup_does(self, tmp_path: Path) -> None:
        namespace = conversation_namespace(_CONV_KEY)
        path = _book_path(tmp_path, namespace, "plans")
        path.parent.mkdir(parents=True, exist_ok=True)
        # 手写的不加引号：yaml 读出来是 date。
        path.write_text(
            "book: plans\nentries:\n"
            "- {id: soon, content: c, keywords: [k], expires_at: 2099-12-31}\n"
            "- {id: over, content: c, keywords: [k], expires_at: 2020-01-01}\n"
            "- {id: odd, content: c, keywords: [k], expires_at: 下周}\n"
            "- {id: always, content: c, keywords: [k]}\n",
            encoding="utf-8",
        )

        rows = {row["id"]: row for row in memory_admin.list_entries(tmp_path, namespace).entries}

        assert (rows["soon"]["expiry_state"], rows["soon"]["expires_at"]) == ("active", "2099-12-31T23:59:59+08:00")
        assert rows["over"]["expiry_state"] == "expired"
        assert (rows["odd"]["expiry_state"], rows["odd"]["expires_at"]) == ("unreadable", "下周")
        assert rows["always"]["expiry_state"] == "none"
        assert "expires_at" not in rows["always"]


class TestCleanup:
    @pytest.fixture()
    def cleanup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import engine.data_cleanup as dc

        monkeypatch.setattr(dc, "DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(dc, "DRY_RUN", False)
        monkeypatch.setattr(dc, "MEMORY_ENTRY_MAX_AGE", 14 * 24 * 3600)
        hits._hit_pending.clear()
        yield dc
        hits._hit_pending.clear()

    def _fresh(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def test_expired_entries_go_whoever_wrote_them(self, tmp_path: Path, cleanup) -> None:
        # 到期时间是写这条的人自己定的：手写的、常驻的到期也删。
        path = _write_book(tmp_path, conversation_namespace(_CONV_KEY), "plans", [
            {"id": "auto_over", "content": "c", "source": "auto", "updated_at": self._fresh(), "expires_at": _PAST},
            {"id": "manual_over", "content": "c", "source": "manual", "expires_at": _PAST},
            {"id": "pinned_over", "content": "c", "constant": True, "source": "auto", "expires_at": _PAST},
            {"id": "soon", "content": "c", "source": "auto", "updated_at": self._fresh(), "expires_at": _FUTURE},
            {"id": "manual", "content": "c", "source": "manual"},
        ])

        cleanup.purge_expired_memory_entries()

        assert [entry["id"] for entry in _entries(path)] == ["soon", "manual"]

    def test_person_archives_lose_only_what_expired(self, tmp_path: Path, cleanup) -> None:
        path = _write_book(tmp_path, subject_namespace("UserA"), "people", [
            {"id": "trip", "content": "这周出差", "source": "auto", "updated_at": "2020-01-01T00:00:00+00:00", "expires_at": _PAST},
            {"id": "old_fact", "content": "生日在三月", "source": "auto", "updated_at": "2020-01-01T00:00:00+00:00"},
        ])

        cleanup.purge_expired_memory_entries()

        assert [entry["id"] for entry in _entries(path)] == ["old_fact"]

    def test_turning_aging_off_still_removes_expired_entries(self, tmp_path: Path, cleanup, monkeypatch) -> None:
        monkeypatch.setattr(cleanup, "MEMORY_ENTRY_MAX_AGE", 0)
        path = _write_book(tmp_path, conversation_namespace(_CONV_KEY), "plans", [
            {"id": "over", "content": "c", "source": "auto", "expires_at": _PAST},
            {"id": "stale", "content": "c", "source": "auto", "updated_at": "2020-01-01T00:00:00+00:00"},
        ])

        cleanup.purge_expired_memory_entries()

        assert [entry["id"] for entry in _entries(path)] == ["stale"]

    def test_an_unreadable_hit_sidecar_does_not_stop_expiry(self, tmp_path: Path, cleanup) -> None:
        namespace = conversation_namespace(_CONV_KEY)
        path = _write_book(tmp_path, namespace, "plans", [
            {"id": "over", "content": "c", "source": "auto", "expires_at": _PAST},
            {"id": "stale", "content": "c", "source": "auto", "updated_at": "2020-01-01T00:00:00+00:00"},
        ])
        (tmp_path / "data" / "memory" / ".hit_cache.json").write_text("{not json", encoding="utf-8")

        cleanup.purge_expired_memory_entries()

        # 读不了命中记录这一轮不按年龄淘汰，到期的照删。
        assert [entry["id"] for entry in _entries(path)] == ["stale"]

    def test_the_hit_stamps_of_removed_entries_go_with_them(self, tmp_path: Path, cleanup) -> None:
        namespace = conversation_namespace(_CONV_KEY)
        _write_book(tmp_path, namespace, "plans", [
            {"id": "over", "content": "c", "source": "auto", "expires_at": _PAST},
            {"id": "stale", "content": "c", "source": "auto", "updated_at": "2020-01-01T00:00:00+00:00"},
            {"id": "soon", "content": "c", "source": "auto", "updated_at": self._fresh(), "expires_at": _FUTURE},
        ])
        sidecar = tmp_path / "data" / "memory" / ".hit_cache.json"
        sidecar.write_text(json.dumps({
            f"{namespace}/over": self._fresh(),
            f"{namespace}/soon": self._fresh(),
        }), encoding="utf-8")

        cleanup.purge_expired_memory_entries()

        assert set(json.loads(sidecar.read_text(encoding="utf-8"))) == {f"{namespace}/soon"}

    def test_a_dry_run_removes_nothing(self, tmp_path: Path, cleanup, monkeypatch) -> None:
        monkeypatch.setattr(cleanup, "DRY_RUN", True)
        namespace = conversation_namespace(_CONV_KEY)
        path = _write_book(tmp_path, namespace, "plans", [
            {"id": "over", "content": "c", "source": "auto", "expires_at": _PAST},
        ])
        sidecar = tmp_path / "data" / "memory" / ".hit_cache.json"
        sidecar.write_text(json.dumps({f"{namespace}/over": self._fresh()}), encoding="utf-8")
        before = sidecar.read_bytes()

        cleanup.purge_expired_memory_entries()

        assert [entry["id"] for entry in _entries(path)] == ["over"]
        assert sidecar.read_bytes() == before


class TestExtraction:
    def _messages(self) -> list[dict[str, Any]]:
        return [
            {"role": "user", "content": "明天考试", "created_at": "2026-10-30T13:15:00+00:00"},
            {"role": "assistant", "content": "加油", "created_at": "2026-10-30T13:20:00+00:00"},
            {"role": "user", "content": "考完了", "created_at": "2026-10-30T14:00:00+00:00"},
            {"role": "user", "content": "睡了", "created_at": "2026-10-30T16:30:00+00:00"},
        ]

    def test_each_stretch_of_the_conversation_is_dated(self) -> None:
        text = _format_transcript_for_extract(self._messages(), zone=_SHANGHAI)

        assert text.split("\n\n") == [
            "[2026-10-30 21:15 周五]",
            "User: 明天考试",
            "Assistant: 加油",
            "[2026-10-30 22:00 周五]",
            "User: 考完了",
            "[2026-10-31 00:30 周六]",
            "User: 睡了",
        ]

    def test_the_first_message_kept_after_truncation_is_dated(self) -> None:
        text = _format_transcript_for_extract(self._messages()[:2], max_chars=10, zone=_SHANGHAI)

        assert text.split("\n\n") == ["[2026-10-30 21:20 周五]", "Assistant: 加油"]

    def test_without_a_zone_nothing_is_added(self) -> None:
        text = _format_transcript_for_extract(self._messages())

        assert "[" not in text

    def test_the_extractor_is_told_the_current_time(self, tmp_path: Path) -> None:
        calls: list[dict[str, Any]] = []
        intent = {
            "session_id": "sess-1",
            "intent_id": "intent-1",
            "transcript": "User: 明天考试",
            "memory_namespace": conversation_namespace(_CONV_KEY),
        }

        MemoryExtractHandler().fire_persisted_intent(
            {"workspace_root": str(tmp_path), "create_task": lambda **kwargs: calls.append(kwargs) or SimpleNamespace(task_id="x")},
            intent,
        )

        instruction = calls[0]["input_data"]["instruction"]
        assert instruction.startswith("当前时间：")
        assert "（Asia/Shanghai）" in instruction.splitlines()[0]
        assert instruction.endswith("User: 明天考试")

    def test_the_extractor_prompt_asks_for_dates_and_expiry_and_not_for_one_off_research(self) -> None:
        prompt = (_ROOT / "engine/system_nodes/system.memory_extractor.yaml").read_text(encoding="utf-8")

        assert '"expires_at"' in prompt
        assert "absolute dates" in prompt
        assert "if in doubt, save it" not in prompt
        assert "happened to ask about, look up or research" in prompt


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.status = "completed"

    def create_task(self, **kwargs: Any) -> SimpleNamespace:
        task_id = f"task-{len(self.calls)}"
        self.calls.append({"task_id": task_id, **kwargs})
        return SimpleNamespace(task_id=task_id)

    def task_snapshots(self, task_ids: list[str]) -> dict[str, dict[str, Any]]:
        return {tid: {"status": self.status, "result_text": "[]"} for tid in task_ids}

    def by_node(self, node_id: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call.get("node_id") == node_id]


def _tick(handler: DreamHandler, root: Path, recorder: _Recorder, now: datetime = _NOW) -> None:
    handler.on_tick({
        "schedule_type": "dream",
        "workspace_root": root,
        "now": now,
        "now_key": now.strftime("%Y-%m-%d %H:%M"),
        "create_task": recorder.create_task,
        "task_snapshots": recorder.task_snapshots,
        "current_session_generation": lambda _sid: 1,
    })


def _auto(eid: str, *, created_days: int = 40, **fields: Any) -> dict[str, Any]:
    created = (_NOW - timedelta(days=created_days)).isoformat()
    return {"id": eid, "content": f"{eid} 的内容", "keywords": [eid], "source": "auto",
            "created_at": created, "updated_at": created, **fields}


class TestDreamReview:
    def test_only_unreviewed_auto_memories_without_an_expiry_are_put_up(self, tmp_path: Path) -> None:
        namespace = conversation_namespace(_CONV_KEY)
        _write_book(tmp_path, namespace, "misc", [
            _auto("plain"),
            _auto("pinned", constant=True),
            _auto("manual", source="manual"),
            _auto("off", enabled=False),
            _auto("dated", expires_at=_FUTURE),
            _auto("odd", expires_at="下周"),
            _auto("seen", **{REVIEWED_AT: (_NOW - timedelta(days=10)).isoformat()}),
            _auto("seen_long_ago", **{REVIEWED_AT: (_NOW - timedelta(days=200)).isoformat()}),
        ])

        ids = [item["id"] for item in DreamHandler()._review_candidates(tmp_path, namespace, now=_NOW)]

        # 写坏的到期时间照样送审，模型可以替它补一个能读的；审过很久的排在没审过的后面再审一遍。
        assert ids == ["plain", "odd", "seen_long_ago"]

    def test_the_oldest_go_first_and_each_night_is_capped(self, tmp_path: Path) -> None:
        namespace = conversation_namespace(_CONV_KEY)
        _write_book(tmp_path, namespace, "misc", [_auto(f"m{days}", created_days=days) for days in range(1, 31)])

        ids = [item["id"] for item in DreamHandler()._review_candidates(tmp_path, namespace, now=_NOW)]

        assert ids == [f"m{days}" for days in range(30, 10, -1)]

    def test_the_model_sees_the_whole_text_or_is_told_it_is_cut(self, tmp_path: Path) -> None:
        namespace = conversation_namespace(_CONV_KEY)
        _write_book(tmp_path, namespace, "misc", [
            {**_auto("short"), "content": "短的"},
            {**_auto("long"), "content": "长" * 5000},
        ])
        handler = DreamHandler()

        rows = {row["id"]: row for row in json.loads(handler._review_json(
            handler._review_candidates(tmp_path, namespace, now=_NOW),
        ))}

        assert rows["short"]["content"] == "短的" and "truncated" not in rows["short"]
        assert rows["long"]["truncated"] is True
        assert set(rows["short"]) == {"book", "id", "content", "created_at", "age_days"}

    def test_expired_entries_stay_out_of_the_topology(self, tmp_path: Path) -> None:
        namespace = conversation_namespace(_CONV_KEY)
        _write_book(tmp_path, namespace, "misc", [_auto("over", expires_at=_PAST), _auto("soon", expires_at=_FUTURE)])

        loaded = DreamHandler()._load_memory_topology_entries(tmp_path, namespace, now=_NOW)

        assert [(entry["id"], entry["expires_at"]) for entry in loaded] == [("soon", "2099-12-31T23:59:59+08:00")]

    def test_every_review_rule_points_at_a_block_that_exists(self) -> None:
        instruction = DreamHandler()._build_dream_instruction(
            run_id="r1", now=_NOW, signals=[], topology_json="{}",
            review_json='[{"id": "m1"}]', current_time=now_text(_SHANGHAI, _NOW),
        )

        assert "<review_candidates>\n[{\"id\": \"m1\"}]\n</review_candidates>" in instruction
        assert "当前时间：2026-10-31 12:00 周六（Asia/Shanghai）" in instruction
        assert "15. 时效和价值审查" in instruction
        assert "30 天" in instruction

    def test_the_signal_extractor_is_told_the_time_and_may_set_an_expiry(self) -> None:
        instruction = DreamHandler()._build_extractor_instruction(
            "User: 明天考试", book_list="(no books found)", current_time="2026-10-31 12:00 周六（Asia/Shanghai）",
        )

        assert "当前时间：2026-10-31 12:00 周六（Asia/Shanghai）" in instruction
        assert '"expires_at"' in instruction

    def _archive(self, root: Path) -> Path:
        _write_runtime(root, {"dream": {"enabled": True, "cron": "* * * * *", "node_id": "system.dream"}})
        memory_subjects.record_interaction(root, "UserA")
        return _write_book(root, subject_namespace("UserA"), "people", [_auto("video_tools")])

    def test_a_single_entry_archive_is_still_reviewed(self, tmp_path: Path) -> None:
        # 一条记忆谈不上去重，可它照样可能早就过时、或者根本不值得留。
        self._archive(tmp_path)
        recorder = _Recorder()
        handler = DreamHandler()

        _tick(handler, tmp_path, recorder)
        _tick(handler, tmp_path, recorder)

        tasks = recorder.by_node("system.dream_profile")
        assert len(tasks) == 1
        assert '"id": "video_tools"' in tasks[0]["input_data"]["instruction"]

    def test_a_completed_run_marks_what_it_reviewed(self, tmp_path: Path) -> None:
        path = self._archive(tmp_path)
        recorder = _Recorder()
        handler = DreamHandler()

        for _ in range(3):
            _tick(handler, tmp_path, recorder)

        entry = _entries(path)[0]
        assert entry[REVIEWED_AT] == _NOW.isoformat()
        assert entry["updated_at"] == _auto("video_tools")["updated_at"]
        # 审过了，下一晚不再送审。
        assert DreamHandler()._review_candidates(tmp_path, subject_namespace("UserA"), now=_NOW) == []

    def test_a_failed_run_marks_nothing(self, tmp_path: Path) -> None:
        path = self._archive(tmp_path)
        recorder = _Recorder()
        handler = DreamHandler()
        _tick(handler, tmp_path, recorder)
        _tick(handler, tmp_path, recorder)

        recorder.status = "failed"
        _tick(handler, tmp_path, recorder)

        assert REVIEWED_AT not in _entries(path)[0]

    def test_an_entry_rewritten_during_the_run_is_reviewed_again(self, tmp_path: Path) -> None:
        path = self._archive(tmp_path)
        recorder = _Recorder()
        handler = DreamHandler()
        _tick(handler, tmp_path, recorder)
        _tick(handler, tmp_path, recorder)

        rewritten = {**_entries(path)[0], "content": "合并后的内容", "updated_at": _NOW.isoformat()}
        _write_book(tmp_path, subject_namespace("UserA"), "people", [rewritten])
        _tick(handler, tmp_path, recorder)

        assert REVIEWED_AT not in _entries(path)[0]
