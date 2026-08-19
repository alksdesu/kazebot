"""Dream 记忆整理的 namespace 回归测试。

记忆按会话落在 data/memory/conv_<hash>/ 下，而 dream 曾用非递归 glob 扫顶层，
book 列表恒为空、topology 恒为 0 条，六项整理动作全是空转；final task 又固定跑在
system:dream 上，save_memory/delete_memory 根本够不着目标会话的目录。
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

_NOW = datetime(2026, 8, 16, 19, 0, tzinfo=timezone.utc)
_GROUP_KEY = "qq_group:bc3f12b0621298e191a49fa7"
_PRIVATE_KEY = "qq_private:7d1a0c95f4be2280b3c6de11"


def _write_runtime(tmp_path: Path, *, enabled: bool = True) -> None:
    cfg = {"memory": {"dream": {"enabled": enabled, "cron": "* * * * *", "node_id": "system.dream"}}}
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "runtime.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _write_session(tmp_path: Path, session_id: str, conversation_key: str, *, channel: str = "qq") -> None:
    path = tmp_path / "data" / "sessions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    rows[session_id] = {
        "session_id": session_id,
        "conversation_key": conversation_key,
        "channel": channel,
        "updated_at": _NOW.isoformat(),
    }
    path.write_text(json.dumps(rows), encoding="utf-8")

    conv = tmp_path / "data" / "conversations" / f"{session_id}.jsonl"
    conv.parent.mkdir(parents=True, exist_ok=True)
    conv.write_text(
        json.dumps({"role": "user", "content": f"来自 {session_id} 的对话"}) + "\n"
        + json.dumps({"role": "assistant", "content": "收到"}) + "\n",
        encoding="utf-8",
    )


def _write_memory(tmp_path: Path, namespace: str, book: str, entries: list[dict[str, Any]]) -> Path:
    book_dir = tmp_path / "data" / "memory" / namespace
    book_dir.mkdir(parents=True, exist_ok=True)
    path = book_dir / f"{book}.yaml"
    path.write_text(yaml.safe_dump({"book": book, "entries": entries}, allow_unicode=True), encoding="utf-8")
    return path


def _entry(eid: str, keywords: list[str], *, content: str = "内容") -> dict[str, Any]:
    return {"id": eid, "content": content, "keywords": keywords, "source": "auto"}


class _Recorder:
    """收集 create_task 调用并按需回放任务快照。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.results: dict[str, str] = {}
        self.status: str = "completed"

    def create_task(self, **kwargs: Any) -> SimpleNamespace:
        task_id = f"task-{len(self.calls)}"
        self.calls.append({"task_id": task_id, **kwargs})
        return SimpleNamespace(task_id=task_id)

    def task_snapshots(self, task_ids: list[str]) -> dict[str, dict[str, Any]]:
        return {
            tid: {"status": self.status, "result_text": self.results.get(tid, "[]")}
            for tid in task_ids
        }

    def by_node(self, node_id: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call.get("node_id") == node_id]


def _ctx(tmp_path: Path, recorder: _Recorder, *, now: datetime = _NOW) -> dict[str, Any]:
    return {
        "schedule_type": "dream",
        "workspace_root": tmp_path,
        "now": now,
        "now_key": now.strftime("%Y-%m-%d %H:%M"),
        "create_task": recorder.create_task,
        "task_snapshots": recorder.task_snapshots,
        "current_session_generation": lambda _sid: 1,
    }


class TestNamespaceScan:
    def test_book_list_reads_the_conversation_subdirectory(self, tmp_path: Path) -> None:
        namespace = _conversation_memory_namespace(_GROUP_KEY)
        _write_memory(tmp_path, namespace, "习惯", [_entry("m1", ["a"])])
        _write_memory(tmp_path, namespace, "术语", [_entry("m2", ["b"])])

        assert DreamHandler()._build_book_list(tmp_path, namespace) == "习惯(1), 术语(1)"

    def test_book_list_carries_entry_counts(self, tmp_path: Path) -> None:
        # 提示词让模型合并「条目数 ≤ 5 的碎片 book」，只给名字它只能猜，
        # 或者去全量 list_memories —— 那正是约束 1 明令禁止的。
        namespace = _conversation_memory_namespace(_GROUP_KEY)
        _write_memory(tmp_path, namespace, "大本", [_entry(f"m{i}", ["a"]) for i in range(7)])
        _write_memory(tmp_path, namespace, "碎片", [_entry("z1", ["b"])])

        assert DreamHandler()._build_book_list(tmp_path, namespace) == "大本(7), 碎片(1)"

    def test_book_list_does_not_leak_other_conversations(self, tmp_path: Path) -> None:
        _write_memory(tmp_path, _conversation_memory_namespace(_GROUP_KEY), "群本", [_entry("m1", ["a"])])
        _write_memory(tmp_path, _conversation_memory_namespace(_PRIVATE_KEY), "私聊本", [_entry("m2", ["b"])])

        assert DreamHandler()._build_book_list(tmp_path, _conversation_memory_namespace(_GROUP_KEY)) == "群本(1)"

    def test_topology_clusters_entries_inside_one_namespace(self, tmp_path: Path) -> None:
        namespace = _conversation_memory_namespace(_GROUP_KEY)
        _write_memory(tmp_path, namespace, "习惯", [
            _entry("m1", ["部署", "生产"]),
            _entry("m2", ["部署", "生产"]),
        ])

        payload = json.loads(DreamHandler()._build_keyword_topology_json(tmp_path, namespace))

        assert payload["total_entries"] == 2
        assert payload["total_clusters"] == 1

    def test_topology_is_empty_for_a_namespace_without_memory(self, tmp_path: Path) -> None:
        _write_memory(tmp_path, _conversation_memory_namespace(_GROUP_KEY), "群本", [_entry("m1", ["a"])])

        payload = json.loads(
            DreamHandler()._build_keyword_topology_json(tmp_path, _conversation_memory_namespace(_PRIVATE_KEY))
        )

        assert payload["total_entries"] == 0

    def _write_cache(self, tmp_path: Path, payload: dict) -> None:
        cache = tmp_path / "data" / "memory" / ".hit_cache.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(payload), encoding="utf-8")

    def test_hit_cache_keeps_only_ids_this_namespace_can_touch(self, tmp_path: Path) -> None:
        self._write_cache(tmp_path, {"mine": _NOW.isoformat(), "theirs": _NOW.isoformat()})

        loaded = json.loads(DreamHandler()._load_hit_cache_json(tmp_path, "conv_x", {"mine"}))

        assert loaded == {"mine": _NOW.isoformat()}

    def test_hit_cache_is_empty_without_entries(self, tmp_path: Path) -> None:
        self._write_cache(tmp_path, {"whatever": _NOW.isoformat()})

        assert DreamHandler()._load_hit_cache_json(tmp_path, "conv_x", set()) == "{}"

    def test_a_legacy_flat_key_is_still_found(self, tmp_path: Path) -> None:
        """升级前写的记录不带 namespace；只认新 key 会让全部历史命中一夜变成「从未命中」。"""
        self._write_cache(tmp_path, {"mine": _NOW.isoformat()})

        loaded = json.loads(DreamHandler()._load_hit_cache_json(tmp_path, "conv_abc", {"mine"}))

        assert loaded == {"mine": _NOW.isoformat()}

    def test_a_namespaced_key_is_found(self, tmp_path: Path) -> None:
        self._write_cache(tmp_path, {"conv_abc/mine": _NOW.isoformat()})

        loaded = json.loads(DreamHandler()._load_hit_cache_json(tmp_path, "conv_abc", {"mine"}))

        assert loaded == {"mine": _NOW.isoformat()}

    def test_the_same_id_in_another_namespace_does_not_leak(self, tmp_path: Path) -> None:
        # 提取器的 id 规则鼓励用稳定别名，两个会话都有 user_zhangsan 是常态。
        self._write_cache(tmp_path, {"conv_other/mine": _NOW.isoformat()})

        loaded = json.loads(DreamHandler()._load_hit_cache_json(tmp_path, "conv_abc", {"mine"}))

        assert loaded == {}

    def test_a_namespaced_key_wins_over_the_legacy_one(self, tmp_path: Path) -> None:
        older = (_NOW - timedelta(days=10)).isoformat()
        self._write_cache(tmp_path, {"mine": older, "conv_abc/mine": _NOW.isoformat()})

        loaded = json.loads(DreamHandler()._load_hit_cache_json(tmp_path, "conv_abc", {"mine"}))

        assert loaded == {"mine": _NOW.isoformat()}

    def test_a_non_object_cache_does_not_raise(self, tmp_path: Path) -> None:
        self._write_cache(tmp_path, [])  # type: ignore[arg-type]

        assert DreamHandler()._load_hit_cache_json(tmp_path, "conv_abc", {"mine"}) == "{}"


class TestNamespaceResolution:
    def test_namespace_of_an_existing_namespace_is_itself(self) -> None:
        namespace = _conversation_memory_namespace(_GROUP_KEY)

        assert _conversation_memory_namespace(namespace) == namespace

    def test_real_keys_still_hash(self) -> None:
        assert _conversation_memory_namespace(_GROUP_KEY).startswith("conv_")
        assert _conversation_memory_namespace(_GROUP_KEY) != _GROUP_KEY

    def test_lookalike_strings_are_not_treated_as_namespaces(self) -> None:
        for value in ["conv_" + "z" * 24, "conv_abc", "conv_" + "a" * 25, "channel:conv_" + "a" * 24]:
            assert _conversation_memory_namespace(value) != value

    def test_sessions_map_to_the_namespace_their_tools_write_into(self, tmp_path: Path) -> None:
        _write_session(tmp_path, "sess-a", _GROUP_KEY)
        _write_session(tmp_path, "sess-b", _PRIVATE_KEY)

        mapping = DreamHandler()._session_memory_targets(tmp_path, ["sess-a", "sess-b", "sess-missing"])

        assert mapping == {
            "sess-a": (_GROUP_KEY, _conversation_memory_namespace(_GROUP_KEY)),
            "sess-b": (_PRIVATE_KEY, _conversation_memory_namespace(_PRIVATE_KEY)),
        }

    def test_dream_own_sessions_are_excluded_from_the_next_run(self, tmp_path: Path) -> None:
        # Dream 的整理任务以 namespace 作为 conversation_key；不排掉就会被当成
        # 活跃会话，下一轮对上一轮的整理记录再提取一次。
        _write_session(tmp_path, "sess-a", _GROUP_KEY)
        _write_session(tmp_path, "sess-dream", _conversation_memory_namespace(_GROUP_KEY), channel="system")

        assert DreamHandler()._active_session_ids_from_registry(tmp_path) == {"sess-a"}

    def test_system_and_internal_sessions_stay_excluded(self, tmp_path: Path) -> None:
        _write_session(tmp_path, "sess-a", _GROUP_KEY)
        _write_session(tmp_path, "sess-sys", "system:dream", channel="system")
        _write_session(tmp_path, "sess-int", "cli:default", channel="internal")

        assert DreamHandler()._active_session_ids_from_registry(tmp_path) == {"sess-a"}


class TestPipeline:
    def _run_to_final(self, tmp_path: Path, recorder: _Recorder) -> DreamHandler:
        handler = DreamHandler()
        handler.on_tick(_ctx(tmp_path, recorder))
        handler.on_tick(_ctx(tmp_path, recorder))
        return handler

    def test_each_conversation_gets_its_own_organizer_task(self, tmp_path: Path) -> None:
        _write_runtime(tmp_path)
        _write_session(tmp_path, "sess-a", _GROUP_KEY)
        _write_session(tmp_path, "sess-b", _PRIVATE_KEY)
        recorder = _Recorder()
        recorder.results = {
            "task-0": json.dumps([{"id": "s1", "book": "习惯", "content": "群里的事", "keywords": ["x"]}]),
            "task-1": json.dumps([{"id": "s2", "book": "习惯", "content": "私聊的事", "keywords": ["y"]}]),
        }

        self._run_to_final(tmp_path, recorder)

        finals = recorder.by_node("system.dream")
        assert {call["conversation_key"] for call in finals} == {
            _conversation_memory_namespace(_GROUP_KEY),
            _conversation_memory_namespace(_PRIVATE_KEY),
        }

    def test_the_organizer_writes_into_the_directory_it_was_given(self, tmp_path: Path) -> None:
        # save_memory 是拿 ToolContext.conversation_key 再算一次 namespace 的，
        # 这条断言就是那次换算的闭环。
        _write_runtime(tmp_path)
        _write_session(tmp_path, "sess-a", _GROUP_KEY)
        recorder = _Recorder()
        recorder.results = {"task-0": json.dumps([{"id": "s1", "book": "习惯", "content": "x", "keywords": ["k"]}])}

        self._run_to_final(tmp_path, recorder)

        conv_key = recorder.by_node("system.dream")[0]["conversation_key"]
        assert _conversation_memory_namespace(conv_key) == _conversation_memory_namespace(_GROUP_KEY)

    def test_the_organizer_key_never_looks_like_a_qq_session(self, tmp_path: Path) -> None:
        # qq_ 前缀会命中 supervisor 的 QQ 非管理员硬限制，data/ 直接写不进去。
        _write_runtime(tmp_path)
        _write_session(tmp_path, "sess-a", _GROUP_KEY)
        recorder = _Recorder()
        recorder.results = {"task-0": json.dumps([{"id": "s1", "book": "习惯", "content": "x", "keywords": ["k"]}])}

        self._run_to_final(tmp_path, recorder)

        call = recorder.by_node("system.dream")[0]
        assert not call["conversation_key"].startswith("qq_")
        assert call["channel"] == "system"

    def test_the_instruction_carries_that_conversation_own_books(self, tmp_path: Path) -> None:
        _write_runtime(tmp_path)
        _write_session(tmp_path, "sess-a", _GROUP_KEY)
        _write_memory(tmp_path, _conversation_memory_namespace(_GROUP_KEY), "群本", [_entry("m1", ["a"])])
        _write_memory(tmp_path, _conversation_memory_namespace(_PRIVATE_KEY), "私聊本", [_entry("m2", ["b"])])
        recorder = _Recorder()
        recorder.results = {"task-0": json.dumps([{"id": "s1", "book": "群本", "content": "x", "keywords": ["k"]}])}

        self._run_to_final(tmp_path, recorder)

        instruction = recorder.by_node("system.dream")[0]["input_data"]["instruction"]
        assert "群本" in instruction
        assert "私聊本" not in instruction

    def test_extractors_are_told_the_books_of_their_own_conversation(self, tmp_path: Path) -> None:
        _write_runtime(tmp_path)
        _write_session(tmp_path, "sess-a", _GROUP_KEY)
        _write_memory(tmp_path, _conversation_memory_namespace(_GROUP_KEY), "群本", [_entry("m1", ["a"])])
        _write_memory(tmp_path, _conversation_memory_namespace(_PRIVATE_KEY), "私聊本", [_entry("m2", ["b"])])
        recorder = _Recorder()

        DreamHandler().on_tick(_ctx(tmp_path, recorder))

        instruction = recorder.by_node("system.memory_extractor")[0]["input_data"]["instruction"]
        assert "群本" in instruction
        assert "私聊本" not in instruction

    def test_extractors_carry_their_conversation_key(self, tmp_path: Path) -> None:
        # 提取器仍持有 save_memory 权限；缺 conversation_key 时越权写会落到根目录。
        _write_runtime(tmp_path)
        _write_session(tmp_path, "sess-a", _GROUP_KEY)
        recorder = _Recorder()

        DreamHandler().on_tick(_ctx(tmp_path, recorder))

        task_context = recorder.by_node("system.memory_extractor")[0]["input_data"]["task_context"]
        assert task_context["conversation_key"] == _GROUP_KEY
        assert _conversation_memory_namespace(task_context["conversation_key"]) == _conversation_memory_namespace(
            _GROUP_KEY
        )

    def test_nothing_to_organize_creates_no_task(self, tmp_path: Path) -> None:
        # 空 signals + 空记忆时曾照样建 final task，每晚凌晨 3 点白烧一次模型调用。
        _write_runtime(tmp_path)
        _write_session(tmp_path, "sess-a", _GROUP_KEY)
        recorder = _Recorder()

        handler = self._run_to_final(tmp_path, recorder)

        assert recorder.by_node("system.dream") == []
        assert handler._dream_pending is None

    def test_existing_memory_alone_still_earns_an_organizer_pass(self, tmp_path: Path) -> None:
        # 没有新信号也要跑：过期清理、碎片 book 合并都只看已有条目。
        _write_runtime(tmp_path)
        _write_session(tmp_path, "sess-a", _GROUP_KEY)
        _write_memory(tmp_path, _conversation_memory_namespace(_GROUP_KEY), "群本", [_entry("m1", ["a"])])
        recorder = _Recorder()

        self._run_to_final(tmp_path, recorder)

        assert len(recorder.by_node("system.dream")) == 1

    def test_lock_is_written_only_after_every_organizer_finishes(self, tmp_path: Path) -> None:
        _write_runtime(tmp_path)
        _write_session(tmp_path, "sess-a", _GROUP_KEY)
        _write_session(tmp_path, "sess-b", _PRIVATE_KEY)
        recorder = _Recorder()
        recorder.results = {
            "task-0": json.dumps([{"id": "s1", "book": "b", "content": "x", "keywords": ["k"]}]),
            "task-1": json.dumps([{"id": "s2", "book": "b", "content": "y", "keywords": ["k"]}]),
        }
        handler = self._run_to_final(tmp_path, recorder)
        lock = tmp_path / "data" / "memory" / ".dream-lock"
        assert not lock.exists()

        handler.on_tick(_ctx(tmp_path, recorder))

        assert lock.exists()
        assert handler._dream_pending is None

    def test_a_stuck_organizer_recovers_after_the_deadline(self, tmp_path: Path) -> None:
        """整理任务挂在非终态时必须有期限。

        15 分钟那道超时只覆盖提取阶段；organizer 一旦进入 pending 就再没有期限，
        而 suspended 不在终态集合里 —— 一个等审批的整理任务会把 dream 冻结到
        supervisor 重启（pending 只在内存里）。
        """
        _write_runtime(tmp_path)
        _write_session(tmp_path, "sess-a", _GROUP_KEY)
        recorder = _Recorder()
        recorder.results = {"task-0": json.dumps([{"id": "s1", "book": "b", "content": "x", "keywords": ["k"]}])}
        handler = self._run_to_final(tmp_path, recorder)
        recorder.status = "suspended"

        handler.on_tick(_ctx(tmp_path, recorder, now=_NOW + timedelta(hours=3)))

        assert handler._dream_pending is None

    def test_a_stuck_organizer_is_left_alone_before_the_deadline(self, tmp_path: Path) -> None:
        _write_runtime(tmp_path)
        _write_session(tmp_path, "sess-a", _GROUP_KEY)
        recorder = _Recorder()
        recorder.results = {"task-0": json.dumps([{"id": "s1", "book": "b", "content": "x", "keywords": ["k"]}])}
        handler = self._run_to_final(tmp_path, recorder)
        recorder.status = "running"

        handler.on_tick(_ctx(tmp_path, recorder, now=_NOW + timedelta(minutes=5)))

        assert handler._dream_pending is not None

    def test_a_failed_lock_write_does_not_freeze_dream(self, tmp_path: Path, monkeypatch) -> None:
        # 锁文件全仓没有任何读取方，丢一次远比把整个特性冻结到重启轻。
        _write_runtime(tmp_path)
        _write_session(tmp_path, "sess-a", _GROUP_KEY)
        recorder = _Recorder()
        recorder.results = {"task-0": json.dumps([{"id": "s1", "book": "b", "content": "x", "keywords": ["k"]}])}
        handler = self._run_to_final(tmp_path, recorder)
        monkeypatch.setattr(DreamHandler, "_write_dream_lock", lambda *a, **kw: False)

        handler.on_tick(_ctx(tmp_path, recorder))

        assert handler._dream_pending is None

    def test_a_disabled_dream_still_drains_an_in_flight_run(self, tmp_path: Path) -> None:
        """pending 那道门原来排在 enabled 之前，一个挂住的任务会连 enabled=false 都读不到。"""
        _write_runtime(tmp_path)
        _write_session(tmp_path, "sess-a", _GROUP_KEY)
        recorder = _Recorder()
        recorder.results = {"task-0": json.dumps([{"id": "s1", "book": "b", "content": "x", "keywords": ["k"]}])}
        handler = self._run_to_final(tmp_path, recorder)
        _write_runtime(tmp_path, enabled=False)

        handler.on_tick(_ctx(tmp_path, recorder))

        assert handler._dream_pending is None
        assert (tmp_path / "data" / "memory" / ".dream-lock").exists()

    def test_a_disabled_dream_does_not_start_a_new_run(self, tmp_path: Path) -> None:
        _write_runtime(tmp_path, enabled=False)
        _write_session(tmp_path, "sess-a", _GROUP_KEY)
        recorder = _Recorder()

        DreamHandler().on_tick(_ctx(tmp_path, recorder))

        assert recorder.calls == []

    def test_a_failed_organizer_leaves_the_lock_alone(self, tmp_path: Path) -> None:
        _write_runtime(tmp_path)
        _write_session(tmp_path, "sess-a", _GROUP_KEY)
        recorder = _Recorder()
        recorder.results = {"task-0": json.dumps([{"id": "s1", "book": "b", "content": "x", "keywords": ["k"]}])}
        handler = self._run_to_final(tmp_path, recorder)
        recorder.status = "failed"

        handler.on_tick(_ctx(tmp_path, recorder))

        assert not (tmp_path / "data" / "memory" / ".dream-lock").exists()
        assert handler._dream_pending is None

    def test_disabled_dream_does_nothing(self, tmp_path: Path) -> None:
        _write_runtime(tmp_path, enabled=False)
        _write_session(tmp_path, "sess-a", _GROUP_KEY)
        recorder = _Recorder()

        DreamHandler().on_tick(_ctx(tmp_path, recorder))

        assert recorder.calls == []


class TestClearGroupMemoryCommand:
    """/清除群记忆 必须删掉 engine 真正写入的那个目录。"""

    @pytest.fixture()
    def runtime(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        from tests._onebot_harness import load_runtime

        return load_runtime(monkeypatch, tmp_path)

    def test_namespace_matches_what_the_engine_writes(self, runtime) -> None:
        # engine 收到的是 _stable_conversation_key 换过的 key，不是真实群号。
        group_id = 123456789
        stable_key = runtime._stable_conversation_key(f"qq_group:{group_id}")

        assert runtime._conversation_memory_namespace_for_group(group_id) == _conversation_memory_namespace(stable_key)

    def test_namespace_is_not_the_raw_group_digest(self, runtime) -> None:
        assert runtime._conversation_memory_namespace_for_group(123456789) != _conversation_memory_namespace(
            "qq_group:123456789"
        )

    def test_clearing_removes_the_directory_the_engine_wrote(self, runtime, tmp_path: Path) -> None:
        group_id = 123456789
        namespace = _conversation_memory_namespace(runtime._stable_conversation_key(f"qq_group:{group_id}"))
        _write_memory(tmp_path, namespace, "群本", [_entry("m1", ["a"])])

        existed, count = runtime._clear_group_memory_namespace(group_id)

        assert (existed, count) == (True, 1)
        assert not (tmp_path / "data" / "memory" / namespace).exists()

    def test_clearing_one_group_keeps_the_others(self, runtime, tmp_path: Path) -> None:
        keep = _conversation_memory_namespace(runtime._stable_conversation_key("qq_group:999"))
        _write_memory(tmp_path, keep, "别的群", [_entry("m1", ["a"])])
        _write_memory(
            tmp_path,
            _conversation_memory_namespace(runtime._stable_conversation_key("qq_group:123456789")),
            "本群",
            [_entry("m2", ["b"])],
        )

        runtime._clear_group_memory_namespace(123456789)

        assert (tmp_path / "data" / "memory" / keep / "别的群.yaml").exists()

    def test_missing_namespace_reports_nothing_cleared(self, runtime) -> None:
        assert runtime._clear_group_memory_namespace(555) == (False, 0)

    def test_computing_the_namespace_does_not_mint_an_alias(self, runtime) -> None:
        # _stable_conversation_key 会顺带分配匿名别名；清记忆不该有这个副作用。
        before = dict(runtime._anon_groups)

        runtime._conversation_memory_namespace_for_group(20260816)

        assert runtime._anon_groups == before

    def test_clearing_one_group_keeps_another_groups_hit_stamps(self, runtime, tmp_path: Path) -> None:
        # 清一个群不能整份删 .hit_cache.json，否则其他群的「最近命中」全被抹平。
        ns_a = runtime._conversation_memory_namespace_for_group(123456789)
        ns_b = runtime._conversation_memory_namespace_for_group(999)
        _write_memory(tmp_path, ns_a, "本群", [_entry("m1", ["a"])])
        _write_memory(tmp_path, ns_b, "别的群", [_entry("m1", ["b"])])
        cache = tmp_path / "data" / "memory" / ".hit_cache.json"
        cache.write_text(
            json.dumps({f"{ns_a}/m1": _NOW.isoformat(), f"{ns_b}/m1": _NOW.isoformat()}),
            encoding="utf-8",
        )

        runtime._clear_group_memory_namespace(123456789)

        left = json.loads(cache.read_text(encoding="utf-8"))
        assert left == {f"{ns_b}/m1": _NOW.isoformat()}
        assert cache.exists()

    def test_clearing_a_group_keeps_the_legacy_flat_hit_keys(self, runtime, tmp_path: Path) -> None:
        ns_a = runtime._conversation_memory_namespace_for_group(123456789)
        _write_memory(tmp_path, ns_a, "本群", [_entry("m1", ["a"])])
        cache = tmp_path / "data" / "memory" / ".hit_cache.json"
        cache.write_text(json.dumps({"m1": _NOW.isoformat()}), encoding="utf-8")

        runtime._clear_group_memory_namespace(123456789)

        assert json.loads(cache.read_text(encoding="utf-8")) == {"m1": _NOW.isoformat()}
