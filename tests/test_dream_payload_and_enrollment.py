"""dream 提示词的约束必须有数据支持；建档门槛必须真的是门槛。

提示词写「对 created_at 超过 30 天的条目清理」，而 payload 里没有 created_at；写
「合并条目数 ≤ 5 的 book」，而 book 列表只有名字。这类约束不会报错，只会让模型
猜或者干脆跳过，而外部完全看不出整理没做。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine import memory_subjects
from engine.builtin.dream import (
    _MAX_MAINTENANCE_PER_KIND,
    DreamHandler,
    _days_since,
)
from engine.builtin.knowledge_inject import _conversation_memory_namespace
from tests._onebot_harness import load_runtime, set_live_config

_NOW = datetime(2026, 8, 17, 3, 0, tzinfo=timezone.utc)
_KEY = "qq_group:700001"


def _iso(days_ago: int) -> str:
    return (_NOW - timedelta(days=days_ago)).isoformat()


def _write(tmp_path: Path, book: str, entries: list[dict[str, Any]], namespace: str = "") -> None:
    ns = namespace or _conversation_memory_namespace(_KEY)
    book_dir = tmp_path / "data" / "memory" / ns
    book_dir.mkdir(parents=True, exist_ok=True)
    (book_dir / f"{book}.yaml").write_text(
        yaml.safe_dump({"book": book, "entries": entries}, allow_unicode=True), encoding="utf-8",
    )


class TestDaysSince:
    def test_it_measures_whole_days(self) -> None:
        assert _days_since(_iso(30), now=_NOW) == 30

    def test_a_blank_stamp_is_unknown(self) -> None:
        assert _days_since("", now=_NOW) is None

    def test_an_unparsable_stamp_is_unknown(self) -> None:
        assert _days_since("last tuesday", now=_NOW) is None

    def test_a_naive_stamp_is_read_as_utc(self) -> None:
        assert _days_since("2026-08-10T03:00:00", now=_NOW) == 7

    def test_a_future_stamp_clamps_to_zero(self) -> None:
        # 时钟回拨过的机器上写下的条目不该算成「负龄」再绕过所有阈值。
        assert _days_since(_iso(-5), now=_NOW) == 0


class TestTopologyFields:
    def test_entries_carry_what_the_constraints_ask_for(self, tmp_path: Path) -> None:
        namespace = _conversation_memory_namespace(_KEY)
        _write(tmp_path, "习惯", [
            {"id": "m1", "content": "内容一", "keywords": ["部署"], "created_at": _iso(40), "priority": 3},
            {"id": "m2", "content": "内容二", "keywords": ["部署"], "created_at": _iso(2), "priority": 0},
        ])

        payload = json.loads(DreamHandler()._build_keyword_topology_json(tmp_path, namespace))

        entry = payload["clusters"][0]["entries"][0]
        for field in ("created_at", "priority", "content_chars"):
            assert field in entry, f"约束按 {field} 判断，payload 里却没有"

    def test_the_loader_reads_them_off_disk(self, tmp_path: Path) -> None:
        namespace = _conversation_memory_namespace(_KEY)
        _write(tmp_path, "习惯", [
            {"id": "m1", "content": "x" * 300, "keywords": ["a"], "created_at": _iso(9), "priority": 5},
        ])

        loaded = DreamHandler()._load_memory_topology_entries(tmp_path, namespace)[0]

        assert loaded["created_at"] == _iso(9)
        assert loaded["priority"] == 5
        assert loaded["content_chars"] == 300


class TestMaintenanceCandidates:
    """topology 只输出 ≥2 成员的簇，孤立的过期条目永远不在里面。

    「30 天未命中就清理」这条约束因此常年没有操作对象。
    """

    def _entry(self, eid: str, **fields: Any) -> dict[str, Any]:
        base = {
            "book": "习惯", "id": eid, "content_preview": "预览", "content_chars": 50,
            "keywords": [], "keyword_set": set(), "constant": False, "source": "auto",
            "created_at": _iso(60), "priority": 0,
        }
        base.update(fields)
        return base

    def _pick(self, entries: list[dict[str, Any]], hits: dict[str, str]) -> dict[str, list[str]]:
        result = DreamHandler()._maintenance_candidates(entries, hits, now=_NOW)
        return {kind: [item["id"] for item in items] for kind, items in result.items()}

    def test_a_never_hit_old_auto_entry_is_a_prune_candidate(self) -> None:
        # 从未命中的条目在 hit_cache 里根本没有键，模型无从判断它「多久没命中」。
        picked = self._pick([self._entry("stale")], {})

        assert picked["prune"] == ["stale"]

    def test_a_recently_hit_entry_is_not_pruned(self) -> None:
        picked = self._pick([self._entry("fresh")], {"fresh": _iso(3)})

        assert picked["prune"] == []

    def test_a_young_entry_is_not_pruned_even_without_hits(self) -> None:
        picked = self._pick([self._entry("young", created_at=_iso(5))], {})

        assert picked["prune"] == []

    def test_a_manual_entry_is_never_a_prune_candidate(self) -> None:
        picked = self._pick([self._entry("manual", source="")], {})

        assert picked["prune"] == []

    def test_a_constant_entry_is_excluded_entirely(self) -> None:
        picked = self._pick([self._entry("always", constant=True)], {})

        assert picked == {"prune": [], "promote": [], "reactivate": []}

    def test_a_long_still_used_entry_is_a_promote_candidate(self) -> None:
        entry = self._entry("lesson", created_at=_iso(20), content_chars=500)

        picked = self._pick([entry], {"lesson": _iso(2)})

        assert picked["promote"] == ["lesson"]

    def test_a_short_entry_is_not_promoted(self) -> None:
        entry = self._entry("brief", created_at=_iso(20), content_chars=50)

        picked = self._pick([entry], {"brief": _iso(2)})

        assert picked["promote"] == []

    def test_a_prioritised_dormant_entry_is_a_reactivate_candidate(self) -> None:
        entry = self._entry("valuable", priority=5, created_at=_iso(60))

        picked = self._pick([entry], {"valuable": _iso(25)})

        assert picked["reactivate"] == ["valuable"]

    def test_reactivation_stops_where_pruning_begins(self) -> None:
        # 超过 30 天的归 prune，不该同时出现在两个清单里。
        entry = self._entry("gone", priority=5, created_at=_iso(60))

        picked = self._pick([entry], {"gone": _iso(35)})

        assert picked["reactivate"] == [] and picked["prune"] == ["gone"]

    def test_a_zero_priority_dormant_entry_is_not_reactivated(self) -> None:
        entry = self._entry("meh", priority=0, created_at=_iso(60))

        picked = self._pick([entry], {"meh": _iso(25)})

        assert picked["reactivate"] == []

    def test_each_list_is_capped(self) -> None:
        entries = [self._entry(f"m{i}") for i in range(_MAX_MAINTENANCE_PER_KIND + 15)]

        picked = self._pick(entries, {})

        assert len(picked["prune"]) == _MAX_MAINTENANCE_PER_KIND

    def test_the_stalest_entries_survive_the_cap(self) -> None:
        entries = [
            self._entry(f"m{i}", created_at=_iso(60)) for i in range(_MAX_MAINTENANCE_PER_KIND)
        ] + [self._entry("never_hit")]
        hits = {f"m{i}": _iso(31) for i in range(_MAX_MAINTENANCE_PER_KIND)}

        picked = self._pick(entries, hits)

        assert "never_hit" in picked["prune"]


class TestInstructionReferencesRealData:
    def test_every_constraint_points_at_a_block_that_exists(self, tmp_path: Path) -> None:
        """约束引用的数据块必须真的在 instruction 里。

        「hit_cache 中超过 30 天未命中」这类措辞看着可执行，实际那份 hit_cache 只装了
        最近有命中的条目 —— 判据和数据对不上，模型只能跳过。
        """
        instruction = DreamHandler()._build_dream_instruction(
            run_id="r1", now=_NOW, signals=[],
            topology_json="{}", hit_cache_json="{}",
            maintenance_json='{"prune": [], "promote": [], "reactivate": []}',
            skill_list="(none)", book_list="a(1)",
        )

        assert "<maintenance_candidates>" in instruction
        for referenced in ("maintenance_candidates.prune", "maintenance_candidates.promote",
                           "maintenance_candidates.reactivate"):
            assert referenced in instruction

    def test_the_hit_cache_block_explains_a_missing_key(self, tmp_path: Path) -> None:
        """hit_cache 只装最近 60 天内有命中的条目，从未命中的根本没有键。

        块头不说明这一点，模型看到「某 id 不在表里」无法断定它是从未命中还是被截断了。
        """
        instruction = DreamHandler()._build_dream_instruction(
            run_id="r1", now=_NOW, signals=[], topology_json="{}", hit_cache_json="{}",
        )

        header = instruction.split("<hit_cache>")[0].splitlines()[-1]
        assert "从未命中" in header

    def test_association_discovery_uses_data_that_exists(self) -> None:
        """hit_cache 只有 {entry_id: 时间戳}，没有 session 维度。

        「被同一类 session 命中」无法判断；改成比对最后命中时间的接近程度才可执行。
        """
        instruction = DreamHandler()._build_dream_instruction(
            run_id="r1", now=_NOW, signals=[], topology_json="{}",
        )

        assert "同一类 session" not in instruction
        assert "最后命中时间彼此相差" in instruction


class TestEnrollmentGate:
    @pytest.fixture()
    def runtime(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        return load_runtime(monkeypatch, tmp_path)

    def _event(self, runtime, user_id: int = 10001) -> SimpleNamespace:
        return SimpleNamespace(user_id=user_id, message_id=1, get_message=lambda: [])

    def test_a_direct_interaction_enrolls(self, runtime, tmp_path: Path) -> None:
        runtime._collect_memory_subjects(self._event(runtime), "你好", "qq_group:t", True)

        assert memory_subjects.is_enrolled(tmp_path, runtime._anonymize_user_id(10001))

    def test_just_talking_in_the_group_does_not_enroll(self, runtime, tmp_path: Path) -> None:
        """ONEBOT_GROUP_TRIGGER=all 下每条群消息都会被处理。

        「在群里说了句话」不是「主动找 bot」—— 照后者建档，群一活跃全员都有跨会话
        档案，注入预算被一堆无关的人占满。
        """
        runtime._collect_memory_subjects(self._event(runtime), "今天天气不错", "qq_group:t", False)

        assert not memory_subjects.is_enrolled(tmp_path, runtime._anonymize_user_id(10001))

    def test_an_already_enrolled_speaker_still_loads_their_memory(self, runtime, tmp_path: Path) -> None:
        # 不建档不等于不加载：他早先主动找过 bot，这轮仍该调出他的档案。
        alias = runtime._anonymize_user_id(10001)
        memory_subjects.record_interaction(tmp_path, alias)

        subjects = runtime._collect_memory_subjects(self._event(runtime), "随便说说", "qq_group:t", False)

        assert alias in subjects

    def test_the_trigger_check_is_separate_from_the_enrollment_check(self, runtime) -> None:
        """触发判定和建档判定必须各算一次。

        `all` / 关键词 / 随机插话下 bot 会回一堆不是在找它的消息，照触发结论建档，
        群一活跃全员都有跨会话档案，注入预算被无关的人占满。
        """
        import ast

        source = (_ROOT / "adapters/onebot/__init__.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        gate = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_is_direct_bot_interaction"
        )
        # 去掉 docstring 再看：说明文字本来就该提到「和触发判定分开」。
        statements = [n for n in gate.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
        body = "\n".join(ast.unparse(n) for n in statements)

        # 不许借道触发判定，也不许直接读 group_mode 这类全量开关。
        for borrowed in ("_group_should_trigger", "_group_trigger_decision", "GROUP_TRIGGER_ALL_MODES", "live.group_trigger"):
            assert borrowed not in body, f"建档判定不能依赖 {borrowed}"
        # 只认这三件事：被 @、回复 bot、带触发前缀。
        assert "_event_at_mentions_bot" in body
        assert "_event_replies_to_bot" in body
        assert "matched_prefix" in body

    def test_a_direct_turn_is_not_downgraded_by_queue_merging(self, runtime, monkeypatch) -> None:
        """排队期间追加的闲聊不能把「刚才那句 @ 了 bot」抹掉。

        合并后的文本包含两条，其中一条是主动找 bot 的，这一轮就该建档。
        """
        set_live_config(runtime, enable_queue=True)
        import asyncio

        def _item(direct: bool):
            return runtime.QueuedInbound(
                matcher=None, bot=None, event=None, channel="qq_group",
                real_conversation_key="qq_group:1", stable_conversation_key="conv_merge",
                text="正文", attachments=[], is_dm=False, platform_updates={},
                user_text="在吗", direct_interaction=direct,
            )

        first = _item(True)
        asyncio.run(runtime._enqueue_or_submit_inbound(first))
        asyncio.run(runtime._enqueue_or_submit_inbound(_item(False)))

        assert first.direct_interaction is True

    def test_the_prefix_check_runs_before_the_prefix_is_stripped(self, runtime) -> None:
        # 先剥前缀再判定，all 模式下所有人都会被算成主动找 bot。
        import ast

        tree = ast.parse((_ROOT / "adapters/onebot/__init__.py").read_text(encoding="utf-8"))
        handler = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_process_group_message"
        )
        lines = ast.unparse(handler).splitlines()
        direct_at = next(i for i, line in enumerate(lines) if "_is_direct_bot_interaction" in line)
        strip_at = next(i for i, line in enumerate(lines) if "_strip_trigger_prefix" in line)

        assert direct_at < strip_at
