"""记忆预算裁剪与写入安全。

四条都是「不报错但功能没了」：发起人的档案被别的记忆挤掉、常驻记忆吃光预算让召回
整个失效、两个 worker 同时写把彼此的条目覆盖掉、dispatch 子节点每轮换一个 namespace。
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine import memory_subjects
from engine.builtin.knowledge_inject import (
    _apply_global_budget,
    _apply_memory_budget,
    _conversation_memory_namespace,
    _load_book,
    _save_book,
    _tool_memory_namespaces,
    delete_memory,
    memory_owner_conversation_key,
    save_memory,
)
from toolbox.context import ToolContext

_CONV_KEY = "qq_group:900001"


def _ctx(tmp_path: Path, conversation_key: str = _CONV_KEY, **extra: Any) -> ToolContext:
    ctx = ToolContext(
        supervisor_url="http://localhost:0",
        session_id="sess-1",
        run_id="run-1",
        worker_id="worker-1",
        workspace_root=tmp_path,
        http=None,  # type: ignore[arg-type]
        registry=None,
        conversation_key=conversation_key,
        node_id="qq.orchestrator",
    )
    for key, value in extra.items():
        setattr(ctx, key, value)
    return ctx


def _buckets(active: list[dict[str, Any]], constant: list[dict[str, Any]] | None = None):
    return {"memory_constant": list(constant or []), "memory_active": list(active)}


def _entry(eid: str, chars: int, **fields: Any) -> dict[str, Any]:
    return {"id": eid, "content": "x" * chars, **fields}


class TestSubjectOrderInBudget:
    def test_the_sender_profile_outranks_a_bystanders(self) -> None:
        """collect_subjects 把发起人排最前，理由就是「预算裁剪时他的档案最该留下」。

        排序不看这个次序，那句承诺就是空的 —— 谁留下来只取决于字典序。
        """
        buckets = _buckets([
            _entry("bystander", 150, subject="UserZ"),
            _entry("sender", 150, subject="UserA"),
        ])

        _apply_memory_budget(buckets, 200, ["UserA", "UserZ"])

        assert [e["id"] for e in buckets["memory_active"]] == ["sender"]

    def test_the_order_actually_follows_the_hint(self) -> None:
        buckets = _buckets([
            _entry("first", 150, subject="UserZ"),
            _entry("second", 150, subject="UserA"),
        ])

        _apply_memory_budget(buckets, 200, ["UserZ", "UserA"])

        assert [e["id"] for e in buckets["memory_active"]] == ["first"]

    def test_an_explicit_priority_still_wins_over_subject_order(self) -> None:
        # priority 是人为声明的重要性，不能被「本轮谁在说话」盖掉。
        buckets = _buckets([
            _entry("important", 150, priority=9, subject="UserZ"),
            _entry("sender", 150, priority=0, subject="UserA"),
        ])

        _apply_memory_budget(buckets, 200, ["UserA", "UserZ"])

        assert [e["id"] for e in buckets["memory_active"]] == ["important"]

    def test_freshness_still_breaks_ties_within_one_subject(self) -> None:
        buckets = _buckets([
            _entry("old", 150, subject="UserA", updated_at="2026-01-01T00:00:00+00:00"),
            _entry("new", 150, subject="UserA", updated_at="2026-08-01T00:00:00+00:00"),
        ])

        _apply_memory_budget(buckets, 200, ["UserA"])

        assert [e["id"] for e in buckets["memory_active"]] == ["new"]

    def test_entries_without_a_subject_are_not_penalised_into_oblivion(self) -> None:
        # 会话自己的记忆没有 subject；只有一个人在场时它们仍该正常参与。
        buckets = _buckets([_entry("plain", 150), _entry("also_plain", 30)])

        _apply_memory_budget(buckets, 200, ["UserA"])

        assert len(buckets["memory_active"]) == 2

    def test_no_hint_keeps_the_old_priority_ordering(self) -> None:
        buckets = _buckets([_entry("low", 150, priority=1), _entry("high", 150, priority=9)])

        _apply_memory_budget(buckets, 200, None)

        assert [e["id"] for e in buckets["memory_active"]] == ["high"]


class TestConstantFloor:
    def test_constant_entries_cannot_starve_recall_completely(self) -> None:
        """一条超长的常驻记忆不能让「@ 某人加载他的档案」整个失效。

        原来 used 从 constant 总量起算，constant 一超预算，第一条召回条目就 break，
        所有档案全部消失，而外部只看到 bot 不记得人。
        """
        buckets = _buckets(
            [_entry("profile", 100, subject="UserA")],
            [_entry("always", 5000)],
        )

        _apply_memory_budget(buckets, 1000, ["UserA"])

        assert [e["id"] for e in buckets["memory_active"]] == ["profile"]
        assert [e["id"] for e in buckets["memory_constant"]] == ["always"]

    def test_the_floor_is_a_floor_not_a_free_pass(self) -> None:
        # 保底额度是 25%，不是无限。
        buckets = _buckets(
            [_entry("huge", 400, subject="UserA")],
            [_entry("always", 5000)],
        )

        _apply_memory_budget(buckets, 1000, ["UserA"])

        assert buckets["memory_active"] == []

    def test_the_floor_does_not_shrink_a_healthy_budget(self) -> None:
        # constant 很小时，剩余预算照常是 max - constant，不受保底影响。
        buckets = _buckets([_entry("a", 700), _entry("b", 400)], [_entry("c", 100)])

        _apply_memory_budget(buckets, 1000, None)

        assert [e["id"] for e in buckets["memory_active"]] == ["a"]

    def test_an_over_budget_constant_set_is_logged_as_a_warning(self, caplog) -> None:
        # 这是配置问题而不是运行时波动，INFO 会被淹掉。
        buckets = _buckets([_entry("x", 10)], [_entry("always", 5000)])

        with caplog.at_level(logging.WARNING):
            _apply_memory_budget(buckets, 1000, None)

        assert any("constant entries alone" in r.getMessage() for r in caplog.records)

    def test_a_normal_budget_logs_no_warning(self, caplog) -> None:
        buckets = _buckets([_entry("x", 10)], [_entry("always", 10)])

        with caplog.at_level(logging.WARNING):
            _apply_memory_budget(buckets, 1000, None)

        assert not [r for r in caplog.records if "constant entries alone" in r.getMessage()]


class TestGlobalBudgetAgrees:
    """两条预算路径不能对「谁该留」给出不同答案。"""

    def _global(self, **kw):
        buckets = {
            "skill_constant": [], "skill_active": [], "skill_index": [],
            "memory_constant": [], "memory_active": [],
        }
        buckets.update(kw)
        return buckets

    def test_it_also_honours_the_subject_order(self) -> None:
        buckets = self._global(memory_active=[
            _entry("bystander", 150, subject="UserZ"),
            _entry("sender", 150, subject="UserA"),
        ])

        _apply_global_budget(buckets, 200, ["UserA", "UserZ"])

        assert [e["id"] for e in buckets["memory_active"]] == ["sender"]

    def test_constant_still_outranks_active_at_equal_priority(self) -> None:
        buckets = self._global(
            memory_constant=[_entry("always", 150)],
            memory_active=[_entry("recalled", 150, subject="UserA")],
        )

        _apply_global_budget(buckets, 200, ["UserA"])

        assert [e["id"] for e in buckets["memory_constant"]] == ["always"]
        assert buckets["memory_active"] == []

    def test_priority_remains_the_top_level_key(self) -> None:
        buckets = self._global(
            memory_constant=[_entry("always", 150, priority=0)],
            memory_active=[_entry("urgent", 150, priority=9)],
        )

        _apply_global_budget(buckets, 200, None)

        assert [e["id"] for e in buckets["memory_active"]] == ["urgent"]
        assert buckets["memory_constant"] == []


class TestMemoryOwner:
    def test_a_real_conversation_owns_its_own_memory(self) -> None:
        assert memory_owner_conversation_key("qq_group:1", "", "") == "qq_group:1"

    def test_a_fresh_dispatch_child_defers_to_the_root(self) -> None:
        """fresh/fork 的 conv_key 每次带一个新 uuid。

        按运行期 key 派生 namespace，子节点每轮读一个空目录、写进一个用一次就废弃的
        目录，那些目录还会无人清理地堆下去。
        """
        own = "agent:bootstrap.executor:qq_group:1:0f9c1e2a-dead-beef-cafe-000000000001"

        assert memory_owner_conversation_key(own, "qq_group:1", "qq_group:1") == "qq_group:1"

    def test_two_fresh_dispatches_land_in_the_same_namespace(self) -> None:
        first = memory_owner_conversation_key("agent:x:qq_group:1:uuid-aaa", "qq_group:1")
        second = memory_owner_conversation_key("agent:x:qq_group:1:uuid-bbb", "qq_group:1")

        assert _conversation_memory_namespace(first) == _conversation_memory_namespace(second)

    def test_it_falls_back_to_the_parent_key(self) -> None:
        assert memory_owner_conversation_key("agent:x:qq_group:1:u", "", "qq_group:1") == "qq_group:1"

    def test_a_child_without_any_root_keeps_its_own_key(self) -> None:
        # 没有可用的归属就用自己的，宁可隔离也不能崩。
        assert memory_owner_conversation_key("agent:x:internal", "", "") == "agent:x:internal"

    def test_the_write_side_resolves_the_same_namespace_as_injection(self, tmp_path: Path) -> None:
        """写入侧算错就是「写得进、读不到」，比读不到更难查。"""
        from engine.builtin.knowledge_inject import _context_memory_namespace

        task_context = {
            "conversation_key": "agent:bootstrap.executor:qq_group:1:uuid-xyz",
            "route_conversation_key": "qq_group:1",
        }
        ctx = _ctx(
            tmp_path,
            conversation_key=task_context["conversation_key"],
            _route_conversation_key="qq_group:1",
        )

        assert _tool_memory_namespaces(ctx)[0] == _context_memory_namespace(None, task_context)

    def test_an_explicit_memory_book_still_wins(self, tmp_path: Path) -> None:
        ctx = _ctx(
            tmp_path,
            conversation_key="agent:x:qq_group:1:u",
            _route_conversation_key="qq_group:1",
            _node_extra={"memory_book": "persona_bob"},
        )

        assert _tool_memory_namespaces(ctx)[0] == "persona_bob"


class TestSubjectOrderReachesTheBudget:
    """端到端：subject 次序要真的从 task_context 走到裁剪那一步。

    每层单测各自绿、中间少传一个参数，就是「按人加载」静默退化成随机保留 ——
    这类断线是这条链上最容易发生的故障。
    """

    def _archive(self, tmp_path: Path, alias: str, content: str) -> None:
        memory_subjects.record_interaction(tmp_path, alias)
        book_dir = tmp_path / "data" / "memory" / f"user_{alias}"
        book_dir.mkdir(parents=True, exist_ok=True)
        (book_dir / "profile.yaml").write_text(
            yaml.safe_dump(
                {"book": "profile", "entries": [{"id": f"{alias}_m1", "content": content}]},
                allow_unicode=True,
            ),
            encoding="utf-8",
        )

    def _build(self, tmp_path: Path, subjects: list[str], budget: int) -> str:
        from engine.builtin.knowledge_inject import build_knowledge_context
        from engine.node import Node

        _ss, _sd, _static, dynamic = build_knowledge_context(
            tmp_path,
            Node(id="qq.orchestrator", type="ai"),
            "他电话多少",
            [],
            {"memory": {"max_budget_chars": budget}},
            task_context={"conversation_key": _CONV_KEY, "memory_hints": {"subjects": subjects}},
        )
        return "\n".join(str(m.get("content") or "") for m in dynamic)

    def test_the_sender_survives_a_budget_that_fits_only_one(self, tmp_path: Path) -> None:
        self._archive(tmp_path, "UserA", "甲" * 200)
        self._archive(tmp_path, "UserZ", "乙" * 200)

        rendered = self._build(tmp_path, ["UserA", "UserZ"], 260)

        assert "甲" * 200 in rendered
        assert "乙" * 200 not in rendered

    def test_reversing_the_hint_reverses_who_survives(self, tmp_path: Path) -> None:
        self._archive(tmp_path, "UserA", "甲" * 200)
        self._archive(tmp_path, "UserZ", "乙" * 200)

        rendered = self._build(tmp_path, ["UserZ", "UserA"], 260)

        assert "乙" * 200 in rendered
        assert "甲" * 200 not in rendered

    def test_a_generous_budget_keeps_both(self, tmp_path: Path) -> None:
        self._archive(tmp_path, "UserA", "甲" * 200)
        self._archive(tmp_path, "UserZ", "乙" * 200)

        rendered = self._build(tmp_path, ["UserA", "UserZ"], 30000)

        assert "甲" * 200 in rendered and "乙" * 200 in rendered


class TestToolContextWiring:
    def test_ai_step_hands_the_routed_key_to_tools(self) -> None:
        """ToolContext 上这两个属性是写入侧算 namespace 的唯一来源。

        赋成空字符串，dispatch 子节点就又开始写进用一次就废弃的目录，而注入侧
        已经改去读发起会话的 namespace —— 变成写得进、读不到。
        """
        import ast

        tree = ast.parse((_ROOT / "engine/inference/ai_step.py").read_text(encoding="utf-8"))
        wired: dict[str, str] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Attribute):
                continue
            if target.attr not in ("_route_conversation_key", "_parent_conversation_key"):
                continue
            wired[target.attr] = ast.unparse(node.value)

        assert set(wired) == {"_route_conversation_key", "_parent_conversation_key"}
        for attr, source in wired.items():
            key = attr.lstrip("_")
            assert f'"{key}"' in source or f"'{key}'" in source, f"{attr} 不是从 task_context.{key} 取的"


def _save(ctx: ToolContext, **args: Any) -> dict[str, Any]:
    payload = {"id": "m1", "book": "profile", "content": "内容", "keywords": ["k"], **args}
    return asyncio.run(save_memory(payload, ctx))


class TestWriteSafety:
    def _book(self, tmp_path: Path, namespace: str = "") -> Path:
        ns = namespace or _conversation_memory_namespace(_CONV_KEY)
        return tmp_path / "data" / "memory" / ns / "profile.yaml"

    def test_concurrent_saves_do_not_lose_entries(self, tmp_path: Path) -> None:
        """两个 engine worker 各跑一次记忆提取就能撞上。

        都读到同一份 entries、各自加一条，后写的把前一条整段覆盖掉。
        """
        ctx = _ctx(tmp_path)
        errors: list[BaseException] = []

        def _write(index: int) -> None:
            try:
                assert _save(ctx, id=f"m{index}", content=f"第{index}条")["ok"]
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_write, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        ids = {e["id"] for e in _load_book(self._book(tmp_path))["entries"]}
        assert ids == {f"m{i}" for i in range(6)}

    def test_the_read_modify_write_span_is_inside_one_lock(self, tmp_path: Path, monkeypatch) -> None:
        """读完再等锁毫无意义：拿到锁时手里的 entries 已经是别人改过之前的快照。"""
        import engine.builtin.knowledge_inject as module

        trace: list[str] = []
        real_load, real_save = module._load_book, module._save_book

        class _TracingLock:
            def __enter__(self):
                trace.append("lock")
                return self

            def __exit__(self, *args):
                trace.append("unlock")
                return False

        monkeypatch.setattr(module, "_memory_write_lock", lambda _ws: _TracingLock())
        monkeypatch.setattr(module, "_load_book", lambda p: (trace.append("read"), real_load(p))[1])
        monkeypatch.setattr(module, "_save_book", lambda p, d: (trace.append("write"), real_save(p, d))[1])

        assert _save(_ctx(tmp_path))["ok"]

        assert trace[0] == "lock" and trace[-1] == "unlock"
        assert "read" in trace and "write" in trace

    def test_deletes_are_serialised_too(self, tmp_path: Path, monkeypatch) -> None:
        import engine.builtin.knowledge_inject as module

        ctx = _ctx(tmp_path)
        assert _save(ctx)["ok"]
        entered: list[str] = []

        class _TracingLock:
            def __enter__(self):
                entered.append("lock")
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr(module, "_memory_write_lock", lambda _ws: _TracingLock())

        asyncio.run(delete_memory({"id": "m1", "book": "profile"}, ctx))

        assert entered == ["lock"]

    def test_a_failed_write_leaves_the_old_book_intact(self, tmp_path: Path, monkeypatch) -> None:
        """write_text 写一半崩掉留下的半截 YAML，会被 _load_book 静默当成空本。

        整本记忆无声消失，而调用方看到的只是一次工具报错。
        """
        path = self._book(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        good = {"book": "profile", "entries": [{"id": "keep", "content": "原有内容"}]}
        _save_book(path, good)

        real_write = Path.write_text

        def _boom(self: Path, *args: Any, **kwargs: Any):
            if self.name.endswith(".tmp"):
                real_write(self, *args, **kwargs)
                raise OSError("disk full")
            return real_write(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", _boom)
        with pytest.raises(OSError):
            _save_book(path, {"book": "profile", "entries": [{"id": "new", "content": "新"}]})

        monkeypatch.undo()
        assert yaml.safe_load(path.read_text(encoding="utf-8")) == good

    def test_no_temp_files_are_left_behind(self, tmp_path: Path) -> None:
        path = self._book(tmp_path)
        _save_book(path, {"book": "profile", "entries": []})

        assert list(path.parent.glob("*.tmp")) == []

    def test_a_subject_archive_write_is_also_atomic(self, tmp_path: Path) -> None:
        memory_subjects.record_interaction(tmp_path, "UserA")
        ctx = _ctx(tmp_path)

        assert _save(ctx, subject="UserA")["ok"]

        archive = tmp_path / "data" / "memory" / "user_UserA"
        assert list(archive.glob("*.tmp")) == []
        assert _load_book(archive / "profile.yaml")["entries"][0]["subject"] == "UserA"
