"""Dream 判断命中新旧用的是这一轮的时钟，不是机器上的当前时间。

渲染 <hit_cache> 的那一步要是自己读 datetime.now()，同一个 handler 的其他方法却都接受
now=：用例只能把「现在」写死，日历一过 60 天窗口就整批失败；手动回放某一轮时也对不上。
"""
from __future__ import annotations

import ast
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.builtin.dream import DreamHandler  # noqa: E402
from engine.memory_namespace import conversation_namespace  # noqa: E402

# 早于真实日历：按机器时钟算，这里的命中全都早就出了 60 天窗口。
_RUN_AT = datetime(2025, 3, 1, 3, 0, tzinfo=timezone.utc)
_GROUP_KEY = "qq_group:bc3f12b0621298e191a49fa7"


def _write_cache(tmp_path: Path, payload: dict[str, str]) -> None:
    path = tmp_path / "data" / "memory" / ".hit_cache.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _hit_block(tmp_path: Path, namespace: str, entry_ids: set[str], **clock: datetime) -> dict[str, str]:
    handler = DreamHandler()
    return json.loads(handler._hit_cache_json(handler._hit_stamps(tmp_path, namespace, entry_ids), **clock))


class TestInjectedClock:
    def test_a_hit_inside_the_window_of_the_injected_clock_is_kept(self, tmp_path: Path) -> None:
        stamp = (_RUN_AT - timedelta(days=59)).isoformat()
        _write_cache(tmp_path, {"conv_x/mine": stamp})

        loaded = _hit_block(tmp_path, "conv_x", {"mine"}, now=_RUN_AT)

        assert loaded == {"mine": stamp}

    def test_a_hit_outside_the_window_of_the_injected_clock_is_dropped(self, tmp_path: Path) -> None:
        # 按机器时钟它是「未来」的命中，只有真的用了注入的时钟才会被筛掉。
        later = datetime.now(timezone.utc) + timedelta(days=400)
        _write_cache(tmp_path, {"conv_x/mine": (later - timedelta(days=61)).isoformat()})

        loaded = _hit_block(tmp_path, "conv_x", {"mine"}, now=later)

        assert loaded == {}

    def test_without_a_clock_it_still_uses_the_current_time(self, tmp_path: Path) -> None:
        stamp = datetime.now(timezone.utc).isoformat()
        _write_cache(tmp_path, {"conv_x/mine": stamp})

        loaded = _hit_block(tmp_path, "conv_x", {"mine"})

        assert loaded == {"mine": stamp}


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create_task(self, **kwargs: Any) -> SimpleNamespace:
        task_id = f"task-{len(self.calls)}"
        self.calls.append({"task_id": task_id, **kwargs})
        return SimpleNamespace(task_id=task_id)

    def task_snapshots(self, task_ids: list[str]) -> dict[str, dict[str, Any]]:
        return {tid: {"status": "completed", "result_text": "[]"} for tid in task_ids}


class TestTheTickClockReachesTheInstruction:
    def test_the_organizer_sees_hits_relative_to_the_tick(self, tmp_path: Path) -> None:
        (tmp_path / "config").mkdir(parents=True)
        (tmp_path / "config" / "runtime.yaml").write_text(
            yaml.safe_dump({"memory": {"dream": {"enabled": True, "cron": "* * * * *", "node_id": "system.dream"}}}),
            encoding="utf-8",
        )
        (tmp_path / "data").mkdir(exist_ok=True)
        (tmp_path / "data" / "sessions.json").write_text(json.dumps({
            "sess-a": {
                "session_id": "sess-a", "conversation_key": _GROUP_KEY, "channel": "qq",
                "updated_at": _RUN_AT.isoformat(),
            },
        }), encoding="utf-8")
        conv = tmp_path / "data" / "conversations" / "sess-a.jsonl"
        conv.parent.mkdir(parents=True)
        conv.write_text(json.dumps({"role": "user", "content": "你好"}) + "\n", encoding="utf-8")
        namespace = conversation_namespace(_GROUP_KEY)
        book = tmp_path / "data" / "memory" / namespace / "habits.yaml"
        book.parent.mkdir(parents=True)
        book.write_text(yaml.safe_dump({"book": "habits", "entries": [
            {"id": "m1", "content": "x", "keywords": ["a"], "source": "auto"},
        ]}), encoding="utf-8")
        stamp = (_RUN_AT - timedelta(days=3)).isoformat()
        _write_cache(tmp_path, {f"{namespace}/m1": stamp})
        recorder = _Recorder()
        ctx = {
            "schedule_type": "dream",
            "workspace_root": tmp_path,
            "now": _RUN_AT,
            "now_key": _RUN_AT.strftime("%Y-%m-%d %H:%M"),
            "create_task": recorder.create_task,
            "task_snapshots": recorder.task_snapshots,
            "current_session_generation": lambda _sid: 1,
        }
        handler = DreamHandler()

        handler.on_tick(ctx)
        handler.on_tick(ctx)

        finals = [call for call in recorder.calls if call.get("node_id") == "system.dream"]
        assert len(finals) == 1
        hit_block = finals[0]["input_data"]["instruction"].split("<hit_cache>")[1].split("</hit_cache>")[0]
        assert json.loads(hit_block) == {"m1": stamp}


class TestNoHiddenClockReads:
    def test_every_handler_method_that_reads_the_clock_accepts_now(self) -> None:
        """入口 on_tick 定下这一轮的时间，往下的方法都该收 now=，而不是自己再看一次表。"""
        tree = ast.parse((_ROOT / "engine" / "builtin" / "dream.py").read_text(encoding="utf-8"))
        handler = next(
            node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == "DreamHandler"
        )
        offenders: list[str] = []
        for method in handler.body:
            if not isinstance(method, ast.FunctionDef) or method.name == "on_tick":
                continue
            reads_clock = any(
                isinstance(call, ast.Call) and ast.unparse(call.func) == "datetime.now"
                for call in ast.walk(method)
            )
            params = {arg.arg for arg in (*method.args.args, *method.args.kwonlyargs)}
            if reads_clock and "now" not in params:
                offenders.append(method.name)

        assert offenders == []
