"""手动触发一次 dream：/整理记忆 走的是 cron 那条 on_schedule_tick 路径。

手动这一路要跳过 enabled / cron / 同分钟去重，但不能跳过在飞那轮的互斥；而且每条
退出路径都得回报 —— 发了指令什么都等不到，比整理失败本身更难查。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from engine.builtin.dream import DreamHandler
from engine.builtin.knowledge_inject import _conversation_memory_namespace

_NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
_KEY = "qq_group:bc3f12b0621298e191a49fa7"
_SESSION = "sess-1"
_NOTIFY = "notify-session"


def _write_runtime(tmp_path: Path, *, enabled: bool = True, cron: str = "* * * * *") -> None:
    cfg = {"memory": {"dream": {"enabled": enabled, "cron": cron, "node_id": "system.dream"}}}
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "runtime.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _write_session(tmp_path: Path) -> None:
    path = tmp_path / "data" / "sessions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({_SESSION: {
        "session_id": _SESSION, "conversation_key": _KEY,
        "channel": "qq", "updated_at": _NOW.isoformat(),
    }}), encoding="utf-8")
    conv = tmp_path / "data" / "conversations" / f"{_SESSION}.jsonl"
    conv.parent.mkdir(parents=True, exist_ok=True)
    conv.write_text(json.dumps({"role": "user", "content": "聊了点什么"}) + "\n", encoding="utf-8")


def _write_memory(tmp_path: Path) -> None:
    """两条说同一件事的记忆：没有可整理的内容时 dream 直接收尾，跑不到整理阶段。"""
    book_dir = tmp_path / "data" / "memory" / _conversation_memory_namespace(_KEY)
    book_dir.mkdir(parents=True, exist_ok=True)
    (book_dir / "people.yaml").write_text(yaml.safe_dump({
        "book": "people",
        "entries": [
            {"id": "m1", "content": "甲是会长", "keywords": ["甲", "会长"], "source": "auto"},
            {"id": "m2", "content": "甲担任会长", "keywords": ["甲", "会长"], "source": "auto"},
        ],
    }, allow_unicode=True), encoding="utf-8")


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.results: dict[str, str] = {}
        self.status = "completed"
        self.outbound: list[dict[str, str]] = []

    def create_task(self, **kwargs: Any) -> SimpleNamespace:
        task_id = f"task-{len(self.calls)}"
        self.calls.append({"task_id": task_id, **kwargs})
        return SimpleNamespace(task_id=task_id)

    def task_snapshots(self, task_ids: list[str]) -> dict[str, dict[str, Any]]:
        return {tid: {"status": self.status, "result_text": self.results.get(tid, "[]")}
                for tid in task_ids}

    def post_outbound(self, *, session_id: str, text: str) -> bool:
        self.outbound.append({"session_id": session_id, "text": text})
        return True

    @property
    def texts(self) -> list[str]:
        return [item["text"] for item in self.outbound]


def _ctx(tmp_path: Path, rec: _Recorder, *, manual: bool = False, notify: str = "",
         now: datetime = _NOW, outcome: dict[str, Any] | None = None) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "schedule_type": "dream",
        "workspace_root": tmp_path,
        "now": now,
        "now_key": now.strftime("%Y-%m-%d %H:%M"),
        "create_task": rec.create_task,
        "task_snapshots": rec.task_snapshots,
        "post_outbound": rec.post_outbound,
        "current_session_generation": lambda _sid: 1,
    }
    if manual:
        ctx["manual"] = True
        ctx["notify_session_id"] = notify
        ctx["outcome"] = outcome if outcome is not None else {}
    return ctx


@pytest.fixture()
def env(tmp_path: Path) -> Path:
    _write_runtime(tmp_path)
    _write_session(tmp_path)
    _write_memory(tmp_path)
    return tmp_path


class TestManualBypassesTheGates:
    def test_it_runs_even_when_dream_is_disabled(self, tmp_path: Path) -> None:
        _write_runtime(tmp_path, enabled=False)
        _write_session(tmp_path)
        outcome: dict[str, Any] = {}

        DreamHandler().on_tick(_ctx(tmp_path, _Recorder(), manual=True, outcome=outcome))

        assert outcome["status"] == "started"

    def test_it_runs_off_the_cron_minute(self, tmp_path: Path) -> None:
        # cron 定在凌晨 3 点，测试时钟是中午 12 点。
        _write_runtime(tmp_path, cron="0 3 * * *")
        _write_session(tmp_path)
        outcome: dict[str, Any] = {}

        DreamHandler().on_tick(_ctx(tmp_path, _Recorder(), manual=True, outcome=outcome))

        assert outcome["status"] == "started"

    def test_it_leaves_the_cron_dedupe_cursor_alone(self, env: Path) -> None:
        # 顶掉游标等于手动跑一次就吃掉当天的自动整理。
        handler = DreamHandler()

        handler.on_tick(_ctx(env, _Recorder(), manual=True))

        assert handler._last_dream_fired == ""

    def test_the_cron_path_still_honours_disabled(self, tmp_path: Path) -> None:
        _write_runtime(tmp_path, enabled=False)
        _write_session(tmp_path)
        handler, rec = DreamHandler(), _Recorder()

        handler.on_tick(_ctx(tmp_path, rec))

        assert handler._dream_pending is None
        assert rec.calls == []


class TestMutualExclusion:
    def test_a_second_trigger_reports_busy(self, env: Path) -> None:
        handler, rec = DreamHandler(), _Recorder()
        handler.on_tick(_ctx(env, rec, manual=True))
        outcome: dict[str, Any] = {}

        handler.on_tick(_ctx(env, rec, manual=True, outcome=outcome))

        assert outcome["status"] == "busy"

    def test_the_running_pipeline_is_not_restarted(self, env: Path) -> None:
        handler, rec = DreamHandler(), _Recorder()
        handler.on_tick(_ctx(env, rec, manual=True))
        before = dict(handler._dream_pending or {})

        handler.on_tick(_ctx(env, rec, manual=True))

        assert (handler._dream_pending or {}).get("now_key") == before.get("now_key")

    def test_a_dead_task_channel_is_reported(self, env: Path) -> None:
        ctx = _ctx(env, _Recorder(), manual=True)
        ctx.pop("create_task")
        outcome = ctx["outcome"]

        DreamHandler().on_tick(ctx)

        assert outcome["status"] == "unavailable"


class TestNotifyOnEveryExit:
    def _drive(self, env: Path, rec: _Recorder, handler: DreamHandler, rounds: int = 4) -> None:
        """推到流水线收尾为止。多推一轮就会启动新的一轮，测的东西就变了。"""
        for _ in range(rounds):
            handler.on_tick(_ctx(env, rec, manual=True, notify=_NOTIFY))
            if handler._dream_pending is None:
                return

    def test_success_carries_the_summary_back(self, env: Path) -> None:
        handler, rec = DreamHandler(), _Recorder()
        handler.on_tick(_ctx(env, rec, manual=True, notify=_NOTIFY))
        handler.on_tick(_ctx(env, rec, manual=True, notify=_NOTIFY))
        rec.results = {call["task_id"]: "合并了 3 条重复" for call in rec.calls}
        handler.on_tick(_ctx(env, rec, manual=True, notify=_NOTIFY))

        assert any("记忆整理完成" in text for text in rec.texts)
        assert any("合并了 3 条重复" in text for text in rec.texts)

    def test_it_lands_on_the_session_that_asked(self, env: Path) -> None:
        handler, rec = DreamHandler(), _Recorder()
        self._drive(env, rec, handler)

        assert rec.outbound
        assert all(item["session_id"] == _NOTIFY for item in rec.outbound)

    def test_a_cron_run_notifies_nobody(self, env: Path) -> None:
        handler, rec = DreamHandler(), _Recorder()
        for _ in range(3):
            handler.on_tick(_ctx(env, rec))

        assert rec.outbound == []

    def test_nothing_to_organize_still_reports(self, tmp_path: Path) -> None:
        # 没有会话就建不出 extractor，轮询时也建不出整理任务 —— 手动触发最常撞上的结局。
        _write_runtime(tmp_path)
        handler, rec = DreamHandler(), _Recorder()
        handler.on_tick(_ctx(tmp_path, rec, manual=True, notify=_NOTIFY))
        handler.on_tick(_ctx(tmp_path, rec, manual=True, notify=_NOTIFY))

        assert any("没有需要整理" in text for text in rec.texts)

    def test_a_failed_organizer_reports_failure(self, env: Path) -> None:
        handler, rec = DreamHandler(), _Recorder()
        handler.on_tick(_ctx(env, rec, manual=True, notify=_NOTIFY))
        handler.on_tick(_ctx(env, rec, manual=True, notify=_NOTIFY))
        rec.status = "failed"
        handler.on_tick(_ctx(env, rec, manual=True, notify=_NOTIFY))

        assert any("没能完成" in text for text in rec.texts)

    def test_a_stuck_extractor_reports_timeout(self, env: Path) -> None:
        handler, rec = DreamHandler(), _Recorder()
        handler.on_tick(_ctx(env, rec, manual=True, notify=_NOTIFY))
        rec.status = "running"

        handler.on_tick(_ctx(env, rec, manual=True, notify=_NOTIFY, now=_NOW + timedelta(hours=2)))

        assert any("超时" in text for text in rec.texts)

    def test_a_missing_snapshot_channel_reports_too(self, env: Path) -> None:
        handler, rec = DreamHandler(), _Recorder()
        handler.on_tick(_ctx(env, rec, manual=True, notify=_NOTIFY))
        ctx = _ctx(env, rec, manual=True, notify=_NOTIFY)
        ctx.pop("task_snapshots")

        handler.on_tick(ctx)

        assert any("查不到" in text for text in rec.texts)

    def test_the_pipeline_is_released_after_notifying(self, env: Path) -> None:
        handler, rec = DreamHandler(), _Recorder()
        self._drive(env, rec, handler)

        assert handler._dream_pending is None
