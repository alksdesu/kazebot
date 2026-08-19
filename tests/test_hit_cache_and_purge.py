"""命中缓存的落盘与 14 天扫除的判龄基准。

命中缓存原来是「进程内持整份 + 全量覆盖写」，两个 engine worker 互相抹掉对方的记录；
atexit 只 import 没 register，worker 活不到 10 分钟就一条都不落盘。而扫除的判龄基准
排在最前的 last_hit_at 从来没有任何代码写进 YAML，于是基准恒为 updated_at ——
一条天天被注入的记忆和一条从没命中过的，第 14 天一起被删。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import engine.memory_hit_cache as hits  # noqa: E402

_NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean_module_state():
    """每个用例都从干净的进程内 pending 开始。"""
    hits._hit_pending.clear()
    hits._hit_cache_last_flush = 0.0
    yield
    hits._hit_pending.clear()


def _cache_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "memory" / ".hit_cache.json"


def _write_cache(tmp_path: Path, payload) -> None:
    p = _cache_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload), encoding="utf-8")


def _read_cache(tmp_path: Path) -> dict:
    return json.loads(_cache_path(tmp_path).read_text(encoding="utf-8"))


def _entry(eid: str, namespace: str = "conv_abc") -> dict:
    return {"id": eid, "namespace": namespace}


class TestHitKeyScoping:
    def test_a_key_is_scoped_by_namespace(self) -> None:
        assert hits.hit_cache_key("conv_abc", "m1") == "conv_abc/m1"

    def test_an_empty_namespace_yields_the_bare_id(self) -> None:
        assert hits.hit_cache_key("", "m1") == "m1"

    def test_an_empty_id_yields_nothing(self) -> None:
        assert hits.hit_cache_key("conv_abc", "") == ""

    def test_lookup_prefers_the_scoped_key(self) -> None:
        cache = {"m1": "2020-01-01T00:00:00+00:00", "conv_abc/m1": "2026-01-01T00:00:00+00:00"}

        assert hits.hit_timestamp(cache, "conv_abc", "m1") == "2026-01-01T00:00:00+00:00"

    def test_lookup_falls_back_to_the_legacy_flat_key(self) -> None:
        assert hits.hit_timestamp({"m1": "2020-01-01T00:00:00+00:00"}, "conv_abc", "m1")

    def test_two_namespaces_do_not_share_a_slot(self) -> None:
        cache = {"conv_a/user_zhangsan": "2026-01-01T00:00:00+00:00"}

        assert hits.hit_timestamp(cache, "conv_b", "user_zhangsan") == ""


class TestReadHitCache:
    def test_a_missing_file_reads_as_empty(self, tmp_path: Path) -> None:
        assert hits.read_hit_cache(tmp_path) == {}

    def test_a_non_object_payload_reads_as_empty(self, tmp_path: Path) -> None:
        """`[]` 能解析成功，随后每次 .get 都抛 TypeError，一路打掉整个 prompt 构建。"""
        _write_cache(tmp_path, [])

        assert hits.read_hit_cache(tmp_path) == {}

    def test_broken_json_reads_as_empty(self, tmp_path: Path) -> None:
        p = _cache_path(tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json", encoding="utf-8")

        assert hits.read_hit_cache(tmp_path) == {}


class TestFlushMerges:
    def test_a_flush_keeps_another_worker_s_entries(self, tmp_path: Path) -> None:
        """两个 worker 各持一份内存再整体覆盖写 = 后写的赢，对方这一轮的命中全丢。"""
        _write_cache(tmp_path, {"conv_abc/other_worker": _NOW.isoformat()})
        hits.record_hits(tmp_path, [_entry("mine")])

        hits.flush_hit_cache(tmp_path)

        merged = _read_cache(tmp_path)
        assert "conv_abc/other_worker" in merged
        assert "conv_abc/mine" in merged

    def test_a_newer_timestamp_wins_on_merge(self, tmp_path: Path) -> None:
        older = (_NOW - timedelta(days=5)).isoformat()
        _write_cache(tmp_path, {"conv_abc/m1": older})
        hits.record_hits(tmp_path, [_entry("m1")])

        hits.flush_hit_cache(tmp_path)

        assert _read_cache(tmp_path)["conv_abc/m1"] > older

    def test_an_older_pending_stamp_does_not_overwrite_a_newer_disk_stamp(self, tmp_path: Path) -> None:
        future = (_NOW + timedelta(days=365)).isoformat()
        _write_cache(tmp_path, {"conv_abc/m1": future})
        hits.record_hits(tmp_path, [_entry("m1")])

        hits.flush_hit_cache(tmp_path)

        assert _read_cache(tmp_path)["conv_abc/m1"] == future

    def test_pending_is_drained_after_a_successful_flush(self, tmp_path: Path) -> None:
        hits.record_hits(tmp_path, [_entry("m1")])

        hits.flush_hit_cache(tmp_path)

        assert hits._hit_pending == {}

    def test_a_failed_flush_keeps_the_hits_for_next_time(self, tmp_path: Path, monkeypatch) -> None:
        hits.record_hits(tmp_path, [_entry("m1")])

        def _boom(*_a, **_kw):
            raise OSError("disk full")

        monkeypatch.setattr(hits,"read_hit_cache", _boom)
        hits.flush_hit_cache(tmp_path)

        assert "conv_abc/m1" in hits._hit_pending

    def test_flushing_nothing_does_not_create_a_file(self, tmp_path: Path) -> None:
        hits.flush_hit_cache(tmp_path)

        assert not _cache_path(tmp_path).exists()

    def test_no_temp_file_is_left_behind(self, tmp_path: Path) -> None:
        hits.record_hits(tmp_path, [_entry("m1")])

        hits.flush_hit_cache(tmp_path)

        leftovers = list((tmp_path / "data" / "memory").glob(".hit_cache.json.*"))
        assert [p for p in leftovers if p.suffix == ".tmp"] == []


class TestRecordHits:
    def test_recording_registers_an_atexit_flush(self, tmp_path: Path, monkeypatch) -> None:
        """文件头注释承诺的「or at shutdown」原来并不存在：atexit 只 import 没 register。"""
        registered: list = []
        monkeypatch.setattr(hits.atexit, "register", lambda fn, *a: registered.append((fn, a)))
        monkeypatch.setattr(hits,"_hit_atexit_registered", False)

        hits.record_hits(tmp_path, [_entry("m1")])

        assert registered
        assert registered[0][0] is hits.flush_hit_cache
        assert registered[0][1] == (tmp_path,)

    def test_recording_does_not_touch_the_disk(self, tmp_path: Path) -> None:
        # 这个函数在每次 LLM 调用的热路径上；前一版在这里读写 660KB YAML 吃掉 45% CPU。
        hits.record_hits(tmp_path, [_entry("m1")])

        assert not _cache_path(tmp_path).exists()
        assert hits._hit_pending

    def test_a_broken_entry_does_not_break_prompt_building(self, tmp_path: Path) -> None:
        # 命中统计是旁路能力，异常绝不该冒泡打掉四个注入键。
        hits.record_hits(tmp_path, ["not a dict"])  # type: ignore[list-item]

    def test_entries_without_an_id_are_skipped(self, tmp_path: Path) -> None:
        hits.record_hits(tmp_path, [{"id": "", "namespace": "conv_abc"}])

        assert hits._hit_pending == {}


class TestPurgeBasis:
    def _book(self, tmp_path: Path, namespace: str, entries: list[dict]) -> Path:
        d = tmp_path / "data" / "memory" / namespace
        d.mkdir(parents=True, exist_ok=True)
        p = d / "people.yaml"
        p.write_text(yaml.safe_dump({"book": "people", "entries": entries}, allow_unicode=True), encoding="utf-8")
        return p

    def _purge(self, tmp_path: Path, monkeypatch):
        import engine.data_cleanup as dc

        # DATA_DIR 是模块级硬编码的仓库路径，不改它就会跑在真实 data/ 上。
        monkeypatch.setattr(dc, "DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(dc, "MEMORY_ENTRY_MAX_AGE", 14 * 24 * 3600)
        dc.purge_expired_memory_entries()
        return dc

    def _entries_left(self, path: Path) -> list[str]:
        if not path.exists():
            return []
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return [str(e.get("id")) for e in (data.get("entries") or [])]

    def _old(self, days: int = 30) -> str:
        return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    def test_a_recently_hit_entry_survives(self, tmp_path: Path, monkeypatch) -> None:
        book = self._book(tmp_path, "conv_abc", [
            {"id": "hot", "content": "x", "keywords": ["k"], "source": "auto", "updated_at": self._old()},
        ])
        _write_cache(tmp_path, {"conv_abc/hot": datetime.now(timezone.utc).isoformat()})

        self._purge(tmp_path, monkeypatch)

        assert self._entries_left(book) == ["hot"]

    def test_a_never_hit_stale_auto_entry_is_removed(self, tmp_path: Path, monkeypatch) -> None:
        book = self._book(tmp_path, "conv_abc", [
            {"id": "cold", "content": "x", "keywords": ["k"], "source": "auto", "updated_at": self._old()},
            {"id": "keep", "content": "x", "keywords": ["k"], "source": "auto", "updated_at": self._old(1)},
        ])
        _write_cache(tmp_path, {})

        self._purge(tmp_path, monkeypatch)

        assert self._entries_left(book) == ["keep"]

    def test_a_manual_entry_is_never_aged_out(self, tmp_path: Path, monkeypatch) -> None:
        book = self._book(tmp_path, "conv_abc", [
            {"id": "manual", "content": "x", "keywords": ["k"], "source": "manual", "updated_at": self._old()},
        ])

        self._purge(tmp_path, monkeypatch)

        assert self._entries_left(book) == ["manual"]

    def test_an_entry_without_a_source_is_treated_as_manual(self, tmp_path: Path, monkeypatch) -> None:
        # save_memory 早期不写 source；把空值判成 auto 就会删掉本该只有人能删的条目。
        book = self._book(tmp_path, "conv_abc", [
            {"id": "legacy", "content": "x", "keywords": ["k"], "updated_at": self._old()},
        ])

        self._purge(tmp_path, monkeypatch)

        assert self._entries_left(book) == ["legacy"]

    def test_a_cross_conversation_archive_is_skipped_entirely(self, tmp_path: Path, monkeypatch) -> None:
        """人物画像的价值就在长期积累，不参与年龄淘汰，只能手工删。"""
        book = self._book(tmp_path, "user_UserA", [
            {"id": "birthday", "content": "x", "subject": "UserA", "source": "auto", "updated_at": self._old(400)},
        ])

        self._purge(tmp_path, monkeypatch)

        assert self._entries_left(book) == ["birthday"]

    def test_a_constant_entry_still_survives(self, tmp_path: Path, monkeypatch) -> None:
        book = self._book(tmp_path, "conv_abc", [
            {"id": "always", "content": "x", "constant": True, "source": "auto", "updated_at": self._old()},
        ])

        self._purge(tmp_path, monkeypatch)

        assert self._entries_left(book) == ["always"]

    def test_a_legacy_flat_hit_key_also_protects(self, tmp_path: Path, monkeypatch) -> None:
        book = self._book(tmp_path, "conv_abc", [
            {"id": "hot", "content": "x", "keywords": ["k"], "source": "auto", "updated_at": self._old()},
        ])
        _write_cache(tmp_path, {"hot": datetime.now(timezone.utc).isoformat()})

        self._purge(tmp_path, monkeypatch)

        assert self._entries_left(book) == ["hot"]

    def test_another_namespaces_hit_does_not_protect(self, tmp_path: Path, monkeypatch) -> None:
        book = self._book(tmp_path, "conv_abc", [
            {"id": "cold", "content": "x", "keywords": ["k"], "source": "auto", "updated_at": self._old()},
        ])
        _write_cache(tmp_path, {"conv_other/cold": datetime.now(timezone.utc).isoformat()})

        self._purge(tmp_path, monkeypatch)

        assert self._entries_left(book) == []


class TestNamespacePrune:
    def test_pruning_one_namespace_keeps_the_others(self, tmp_path: Path) -> None:
        _write_cache(tmp_path, {"conv_a/m1": _NOW.isoformat(), "conv_b/m1": _NOW.isoformat()})

        removed = hits.prune_namespace_hits(tmp_path, "conv_a")

        assert removed == 1
        assert _read_cache(tmp_path) == {"conv_b/m1": _NOW.isoformat()}

    def test_pruning_keeps_a_legacy_flat_key(self, tmp_path: Path) -> None:
        # 扁平 key 无法归属某个会话，删它会让别的会话的同 id 记忆变成「从没命中」。
        _write_cache(tmp_path, {"m1": _NOW.isoformat()})

        hits.prune_namespace_hits(tmp_path, "conv_a")

        assert _read_cache(tmp_path) == {"m1": _NOW.isoformat()}

    def test_pruning_does_not_match_a_prefix_lookalike(self, tmp_path: Path) -> None:
        # conv_a 不能吃掉 conv_ab：必须按 namespace + "/" 前缀匹配。
        _write_cache(tmp_path, {"conv_ab/m1": _NOW.isoformat()})

        hits.prune_namespace_hits(tmp_path, "conv_a")

        assert _read_cache(tmp_path) == {"conv_ab/m1": _NOW.isoformat()}

    def test_pruning_drops_the_matching_pending_hits(self, tmp_path: Path) -> None:
        hits.record_hits(tmp_path, [_entry("m1", "conv_a")])

        hits.prune_namespace_hits(tmp_path, "conv_a")

        assert not any(k.startswith("conv_a/") for k in hits._hit_pending)

    def test_pruning_a_missing_cache_creates_nothing(self, tmp_path: Path) -> None:
        removed = hits.prune_namespace_hits(tmp_path, "conv_a")

        assert removed == 0
        assert not _cache_path(tmp_path).exists()

    def test_an_empty_namespace_prunes_nothing(self, tmp_path: Path) -> None:
        _write_cache(tmp_path, {"m1": _NOW.isoformat()})

        assert hits.prune_namespace_hits(tmp_path, "") == 0
        assert _read_cache(tmp_path) == {"m1": _NOW.isoformat()}


class TestOrphanPrune:
    def test_a_hit_key_for_a_deleted_namespace_is_swept(self, tmp_path: Path) -> None:
        _write_cache(tmp_path, {"conv_gone/m1": _NOW.isoformat(), "conv_live/m1": _NOW.isoformat()})

        removed = hits.prune_orphan_namespace_hits(tmp_path, {"conv_live"})

        assert removed == 1
        assert _read_cache(tmp_path) == {"conv_live/m1": _NOW.isoformat()}

    def test_a_legacy_flat_key_is_never_swept(self, tmp_path: Path) -> None:
        _write_cache(tmp_path, {"m1": _NOW.isoformat(), "conv_gone/x": _NOW.isoformat()})

        hits.prune_orphan_namespace_hits(tmp_path, {"conv_live"})

        assert _read_cache(tmp_path) == {"m1": _NOW.isoformat()}

    def test_an_empty_known_set_sweeps_nothing(self, tmp_path: Path) -> None:
        # 拿不到目录列表时空集合不能被当成「所有 namespace 都没了」。
        _write_cache(tmp_path, {"conv_gone/m1": _NOW.isoformat()})

        assert hits.prune_orphan_namespace_hits(tmp_path, set()) == 0
        assert _read_cache(tmp_path) == {"conv_gone/m1": _NOW.isoformat()}

    def test_the_cleanup_run_sweeps_orphan_hit_keys(self, tmp_path: Path, monkeypatch) -> None:
        import engine.data_cleanup as dc

        live_dir = tmp_path / "data" / "memory" / "conv_live"
        live_dir.mkdir(parents=True, exist_ok=True)
        (live_dir / "people.yaml").write_text(
            yaml.safe_dump(
                {"book": "people", "entries": [{"id": "x", "content": "c", "source": "manual"}]},
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        _write_cache(tmp_path, {"conv_live/x": _NOW.isoformat(), "conv_gone/y": _NOW.isoformat()})

        monkeypatch.setattr(dc, "DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(dc, "MEMORY_ENTRY_MAX_AGE", 14 * 24 * 3600)
        dc.purge_expired_memory_entries()

        assert _read_cache(tmp_path) == {"conv_live/x": _NOW.isoformat()}
