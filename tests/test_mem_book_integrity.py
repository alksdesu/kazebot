"""记忆本读不出来时绝不能被当成空本写回，清理任务和 engine 共用一把锁、一份原子写。

YAML 打错一个冒号，_load_book 曾按空本返回，紧接着的 save_memory 整本写回，原有条目一条不剩；
每小时的清理任务读写不持锁、用 write_text 非原子覆盖，会冲掉 engine 刚存进去的那条。
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import engine.data_cleanup as cleanup  # noqa: E402
from engine import memory_admin  # noqa: E402
from engine.builtin.knowledge_inject import (  # noqa: E402
    MemoryBookUnreadable,
    _load_book,
    delete_memory,
    list_memories,
    load_memory_catalog,
    save_memory,
)
from engine.memory_hit_cache import hit_cache_path, prune_entry_hits, read_hit_cache  # noqa: E402
from engine.memory_namespace import conversation_namespace  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402

_CONV_KEY = "qq_group:bc3f12b0621298e191a49fa7"
_BROKEN = "book: people\nentries:\n- id: keep\n  content: 原有内容\n  keywords: [a\n"
# 坏的正是记着原话的那一行：报错里只该有位置和问题。
_PRIVATE = "hunter2"
_BROKEN_ON_A_PRIVATE_LINE = f"book: people\nentries:\n- id: keep\n  content: 她的密码是-{_PRIVATE}-secret: x\n"
_BROKEN_ON_A_PRIVATE_LINE_REASON = "第 4 行第 32 列：mapping values are not allowed here"


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        supervisor_url="http://localhost:0",
        session_id="sess-1",
        run_id="run-1",
        worker_id="worker-1",
        workspace_root=tmp_path,
        http=None,  # type: ignore[arg-type]
        registry=None,
        conversation_key=_CONV_KEY,
        node_id="system.memory_extractor",
    )


def _namespace_dir(tmp_path: Path) -> Path:
    return tmp_path / "data" / "memory" / conversation_namespace(_CONV_KEY)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _book(path: Path, entries: list[dict[str, Any]]) -> Path:
    return _write(path, yaml.safe_dump({"book": path.stem, "entries": entries}, allow_unicode=True))


class TestLoadBook:
    def test_a_missing_book_is_an_empty_one(self, tmp_path: Path) -> None:
        assert _load_book(tmp_path / "nope.yaml") == {"book": "nope", "entries": []}

    def test_an_empty_file_is_an_empty_book(self, tmp_path: Path) -> None:
        # 空文件里没有能丢的东西，拒写只会让这本永远写不进去。
        assert _load_book(_write(tmp_path / "blank.yaml", ""))["entries"] == []

    @pytest.mark.parametrize("text", [
        pytest.param(_BROKEN, id="YAML 语法错误"),
        pytest.param("- id: a\n  content: x\n", id="顶层是列表"),
        pytest.param("book: people\nentries: oops\n", id="entries 不是列表"),
        pytest.param("book: people\nentries:\n  a: 1\n", id="entries 是映射"),
    ])
    def test_a_book_that_cannot_be_read_raises(self, tmp_path: Path, text: str) -> None:
        with pytest.raises(MemoryBookUnreadable):
            _load_book(_write(tmp_path / "people.yaml", text))

    @pytest.mark.parametrize(("text", "reason"), [
        pytest.param(_BROKEN_ON_A_PRIVATE_LINE, _BROKEN_ON_A_PRIVATE_LINE_REASON, id="坏行"),
        # 值构造不出来原来抛的是消息里带着原值的 ValueError，原样拼进了原因
        pytest.param(
            f"book: people\nentries:\n- id: keep\n  priority: !!int 她的密码是-{_PRIVATE}-secret\n",
            "第 4 行第 13 列：值不是合法的 int",
            id="坏值",
        ),
    ])
    def test_the_reason_names_the_place_but_not_the_line(self, tmp_path: Path, text: str, reason: str) -> None:
        # 原因会交给模型、写进日志、回给控制台。
        with pytest.raises(MemoryBookUnreadable) as caught:
            _load_book(_write(tmp_path / "people.yaml", text))

        assert caught.value.reason == reason
        for link in (caught.value, caught.value.__cause__, caught.value.__context__):
            assert link is not None and _PRIVATE not in str(link)

    def test_missing_entries_keep_the_other_fields(self, tmp_path: Path) -> None:
        data = _load_book(_write(tmp_path / "people.yaml", "book: people\nnote: 手写\n"))

        assert data == {"book": "people", "note": "手写", "entries": []}


class TestToolsRefuseToOverwrite:
    def test_save_memory_leaves_an_unreadable_book_untouched(self, tmp_path: Path) -> None:
        path = _write(_namespace_dir(tmp_path) / "people.yaml", _BROKEN)

        result = asyncio.run(save_memory(
            {"id": "new", "book": "people", "content": "新的", "keywords": ["k"]}, _ctx(tmp_path),
        ))

        assert result["ok"] is False
        assert "unreadable" in result["error"]
        assert path.read_text(encoding="utf-8") == _BROKEN

    def test_another_book_in_the_same_namespace_is_still_writable(self, tmp_path: Path) -> None:
        _write(_namespace_dir(tmp_path) / "people.yaml", _BROKEN)

        result = asyncio.run(save_memory(
            {"id": "new", "book": "rules", "content": "新的", "keywords": ["k"]}, _ctx(tmp_path),
        ))

        assert result["ok"] is True

    def test_delete_memory_leaves_an_unreadable_book_untouched(self, tmp_path: Path) -> None:
        path = _write(_namespace_dir(tmp_path) / "people.yaml", _BROKEN)

        result = asyncio.run(delete_memory({"id": "keep", "book": "people"}, _ctx(tmp_path)))

        assert result["ok"] is False
        assert path.read_text(encoding="utf-8") == _BROKEN

    def test_list_memories_says_which_book_it_could_not_read(self, tmp_path: Path) -> None:
        _write(_namespace_dir(tmp_path) / "people.yaml", _BROKEN)
        _book(_namespace_dir(tmp_path) / "rules.yaml", [{"id": "r1", "content": "规则", "keywords": ["k"]}])

        result = asyncio.run(list_memories({}, _ctx(tmp_path)))

        assert result["ok"] is True
        assert [entry["id"] for entry in result["data"]["entries"]] == ["r1"]
        assert result["data"]["unreadable_books"] == ["people.yaml"]

    def test_the_refusal_handed_to_the_model_names_the_place_but_not_the_line(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        path = _write(_namespace_dir(tmp_path) / "people.yaml", _BROKEN_ON_A_PRIVATE_LINE)

        with caplog.at_level(logging.WARNING, logger="engine.builtin.knowledge_inject"):
            result = asyncio.run(save_memory(
                {"id": "new", "book": "people", "content": "新的", "keywords": ["k"]}, _ctx(tmp_path),
            ))

        assert result["ok"] is False
        assert _BROKEN_ON_A_PRIVATE_LINE_REASON in result["error"]
        assert _PRIVATE not in result["error"]
        assert _PRIVATE not in caplog.text
        assert path.read_text(encoding="utf-8") == _BROKEN_ON_A_PRIVATE_LINE

    def test_the_prompt_build_skips_it_naming_the_place_but_not_the_line(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        # 每轮构建 prompt 都会扫到它，日志里会一遍遍出现。
        _write(_namespace_dir(tmp_path) / "people.yaml", _BROKEN_ON_A_PRIVATE_LINE)

        with caplog.at_level(logging.WARNING, logger="engine.builtin.knowledge_inject"):
            catalog = load_memory_catalog(tmp_path, memory_book=conversation_namespace(_CONV_KEY), _use_cache=False)

        assert catalog == []
        assert f"memory book people.yaml skipped, unreadable: {_BROKEN_ON_A_PRIVATE_LINE_REASON}" in caplog.text
        assert _PRIVATE not in caplog.text


class TestConsoleRefusesToOverwrite:
    def _namespace(self, tmp_path: Path) -> str:
        return conversation_namespace(_CONV_KEY)

    def test_upsert_reports_it_and_leaves_the_file(self, tmp_path: Path) -> None:
        path = _write(_namespace_dir(tmp_path) / "default.yaml", _BROKEN)

        with pytest.raises(memory_admin.MemoryAdminError):
            memory_admin.upsert_entry(
                tmp_path, self._namespace(tmp_path), {"id": "a", "content": "x", "keywords": ["k"]},
            )

        assert path.read_text(encoding="utf-8") == _BROKEN

    def test_delete_reports_it_and_leaves_the_file(self, tmp_path: Path) -> None:
        path = _write(_namespace_dir(tmp_path) / "default.yaml", _BROKEN)

        with pytest.raises(memory_admin.MemoryAdminError):
            memory_admin.delete_entry(tmp_path, self._namespace(tmp_path), "default", "keep")

        assert path.read_text(encoding="utf-8") == _BROKEN

    def test_the_refusal_names_the_place_but_not_the_line(self, tmp_path: Path) -> None:
        # 控制台把原因原样显示出来；接口层再包一层往上抛，整条链都不能带原话。
        _write(_namespace_dir(tmp_path) / "default.yaml", _BROKEN_ON_A_PRIVATE_LINE)

        with pytest.raises(memory_admin.MemoryAdminError) as caught:
            memory_admin.upsert_entry(
                tmp_path, self._namespace(tmp_path), {"id": "a", "content": "x", "keywords": ["k"]},
            )

        assert str(caught.value) == (
            f"记忆本 default.yaml 读不出来（{_BROKEN_ON_A_PRIVATE_LINE_REASON}），先修好或删掉这个文件再改"
        )
        link: BaseException | None = caught.value
        while link is not None:
            assert _PRIVATE not in str(link)
            link = link.__cause__ or link.__context__

    def test_listing_shows_the_rest_and_names_the_book_it_could_not_read(self, tmp_path: Path) -> None:
        # 悄悄跳过的话，控制台的条数和本数少了一截，看着像那本里的记忆没了。
        _write(_namespace_dir(tmp_path) / "default.yaml", _BROKEN_ON_A_PRIVATE_LINE)
        _book(_namespace_dir(tmp_path) / "rules.yaml", [{"id": "r1", "content": "规则", "keywords": ["k"]}])

        listing = memory_admin.list_entries(tmp_path, self._namespace(tmp_path))

        assert [entry["id"] for entry in listing.entries] == ["r1"]
        assert listing.unreadable_books == [{"book": "default", "problem": _BROKEN_ON_A_PRIVATE_LINE_REASON}]

    def test_the_overview_counts_only_what_it_read_and_lists_the_rest_by_name(self, tmp_path: Path) -> None:
        _write(_namespace_dir(tmp_path) / "default.yaml", _BROKEN_ON_A_PRIVATE_LINE)
        _book(_namespace_dir(tmp_path) / "rules.yaml", [{"id": "r1", "content": "规则", "keywords": ["k"]}])

        [row] = memory_admin.list_namespaces(tmp_path)

        assert (row["entry_count"], row["books"], row["unreadable_books"]) == (1, ["rules"], ["default"])
        assert _PRIVATE not in str(row)

    def test_a_namespace_with_nothing_but_an_unreadable_book_is_still_listed(self, tmp_path: Path) -> None:
        # 只列条数为零的一行，人会以为这里本来就是空的，顺手清掉。
        _write(tmp_path / "data" / "memory" / "default.yaml", _BROKEN)

        [row] = memory_admin.list_namespaces(tmp_path)

        assert (row["namespace"], row["entry_count"], row["unreadable_books"]) == ("", 0, ["default"])

    def test_clearing_the_namespace_still_drops_it(self, tmp_path: Path) -> None:
        # 清空是人明确要删掉整个 namespace，读不出来的那本也在其中。
        path = _write(_namespace_dir(tmp_path) / "default.yaml", _BROKEN)

        assert memory_admin.clear_namespace(tmp_path, self._namespace(tmp_path)) == 0
        assert not path.exists()


class _TracingLock:
    def __init__(self, trace: list[str]) -> None:
        self._trace = trace

    def __enter__(self) -> "_TracingLock":
        self._trace.append("lock")
        return self

    def __exit__(self, *_exc: Any) -> bool:
        self._trace.append("unlock")
        return False


class TestCleanupWrites:
    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(cleanup, "DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(cleanup, "DRY_RUN", False)
        monkeypatch.setattr(cleanup, "MEMORY_ENTRY_MAX_AGE", 14 * 24 * 3600)

    def _old(self) -> str:
        return (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()

    def _fresh(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _mixed_book(self, tmp_path: Path) -> Path:
        return _book(_namespace_dir(tmp_path) / "people.yaml", [
            {"id": "stale", "content": "旧", "keywords": ["k"], "source": "auto", "updated_at": self._old()},
            {"id": "fresh", "content": "新", "keywords": ["k"], "source": "auto", "updated_at": self._fresh()},
        ])

    def test_an_unreadable_book_is_left_byte_for_byte(self, tmp_path: Path) -> None:
        path = _write(_namespace_dir(tmp_path) / "people.yaml", _BROKEN)

        cleanup.purge_expired_memory_entries()

        assert path.read_text(encoding="utf-8") == _BROKEN

    def test_the_read_modify_write_runs_inside_the_engine_lock(self, tmp_path: Path, monkeypatch) -> None:
        """读完到写回之间 engine 存进去的那条，会被清理任务的整本写回盖掉。"""
        self._mixed_book(tmp_path)
        trace: list[str] = []
        real_load, real_save = cleanup._load_book, cleanup._save_book
        monkeypatch.setattr(cleanup, "_memory_write_lock", lambda _ws: _TracingLock(trace))
        monkeypatch.setattr(cleanup, "_load_book", lambda path: (trace.append("read"), real_load(path))[1])
        monkeypatch.setattr(cleanup, "_save_book", lambda path, data: (trace.append("write"), real_save(path, data))[1])

        cleanup.purge_expired_memory_entries()

        read_at, write_at = trace.index("read"), trace.index("write")
        lock_at = max(index for index in range(read_at) if trace[index] == "lock")
        assert "unlock" not in trace[lock_at:write_at]
        assert "unlock" in trace[write_at:]

    def test_the_lock_is_the_one_the_engine_tools_take(self, tmp_path: Path) -> None:
        from engine.builtin import knowledge_inject

        assert cleanup._memory_write_lock is knowledge_inject._memory_write_lock

    def test_a_surviving_book_is_rewritten_through_the_shared_atomic_writer(self, tmp_path: Path, monkeypatch) -> None:
        path = self._mixed_book(tmp_path)
        written: list[Path] = []
        real_save = cleanup._save_book
        monkeypatch.setattr(cleanup, "_save_book", lambda p, data: (written.append(p), real_save(p, data))[1])

        cleanup.purge_expired_memory_entries()

        assert written == [path]
        assert [entry["id"] for entry in _load_book(path)["entries"]] == ["fresh"]
        assert list(path.parent.glob("*.tmp")) == []

    def test_the_module_has_no_second_book_writer(self) -> None:
        # 再长出一处 write_text，就又是一条不持锁、写一半就丢整本的路径。
        tree = ast.parse((_ROOT / "engine" / "data_cleanup.py").read_text(encoding="utf-8"))
        writers = [
            ast.unparse(node) for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("write_text", "write_bytes", "safe_dump", "dump")
        ]

        assert writers == []

    def test_a_dry_run_still_writes_nothing(self, tmp_path: Path, monkeypatch, caplog) -> None:
        path = self._mixed_book(tmp_path)
        before = path.read_bytes()
        monkeypatch.setattr(cleanup, "DRY_RUN", True)

        with caplog.at_level(logging.INFO):
            cleanup.purge_expired_memory_entries()

        assert path.read_bytes() == before
        assert any("would rewrite" in record.getMessage() for record in caplog.records)

    def test_internal_lock_directories_survive_the_empty_dir_sweep(self, tmp_path: Path) -> None:
        # 空目录清扫曾一路扫进 data/memory 下所有目录，别的进程正持有的锁目录也照删。
        lock_dir = tmp_path / "data" / "memory" / ".hit_cache.json.rotate.lock"
        lock_dir.mkdir(parents=True)
        empty_namespace = tmp_path / "data" / "memory" / "persona_bob"
        empty_namespace.mkdir()

        cleanup.purge_expired_memory_entries()

        assert lock_dir.is_dir()
        assert not empty_namespace.exists()

    def test_a_namespace_linked_outside_is_not_swept(self, tmp_path: Path) -> None:
        import os

        root = tmp_path / "data" / "memory"
        root.mkdir(parents=True)
        outside = tmp_path / "elsewhere"
        book = _book(outside / "people.yaml", [
            {"id": "stale", "content": "旧", "keywords": ["k"], "source": "auto", "updated_at": self._old()},
        ])
        try:
            os.symlink(outside, root / "escape", target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"cannot create a directory symlink here: {exc}")

        cleanup.purge_expired_memory_entries()

        assert book.exists()


class TestHitStampsOnToolDelete:
    def _seed_hits(self, tmp_path: Path, payload: dict[str, str]) -> None:
        path = hit_cache_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_delete_memory_drops_the_entry_stamp_like_the_console_does(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        assert asyncio.run(save_memory({"id": "m1", "book": "people", "content": "x", "keywords": ["k"]}, ctx))["ok"]
        namespace = conversation_namespace(_CONV_KEY)
        self._seed_hits(tmp_path, {f"{namespace}/m1": "2026-10-01T00:00:00+00:00", "m1": "2026-09-01T00:00:00+00:00"})

        assert asyncio.run(delete_memory({"id": "m1", "book": "people"}, ctx))["ok"]

        # 扁平 key 是升级前的共享记录，别的会话的同 id 记忆还靠它判龄。
        assert read_hit_cache(tmp_path) == {"m1": "2026-09-01T00:00:00+00:00"}

    def test_a_root_entry_never_drops_the_shared_flat_key(self, tmp_path: Path) -> None:
        self._seed_hits(tmp_path, {"m1": "2026-09-01T00:00:00+00:00"})

        assert prune_entry_hits(tmp_path, "", "m1") == 0
        assert read_hit_cache(tmp_path) == {"m1": "2026-09-01T00:00:00+00:00"}

    def test_an_unreadable_hit_cache_does_not_stop_delete_memory_and_is_left_as_it_is(self, tmp_path: Path) -> None:
        # 摘命中戳读不懂侧车就跳过：整份写回会把别的会话的命中一起抹掉。
        ctx = _ctx(tmp_path)
        assert asyncio.run(save_memory({"id": "m1", "book": "people", "content": "x", "keywords": ["k"]}, ctx))["ok"]
        path = hit_cache_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        broken = '{"conv_other/m1": '
        path.write_text(broken, encoding="utf-8")

        assert asyncio.run(delete_memory({"id": "m1", "book": "people"}, ctx))["ok"]

        assert not (_namespace_dir(tmp_path) / "people.yaml").exists()
        assert path.read_text(encoding="utf-8") == broken
