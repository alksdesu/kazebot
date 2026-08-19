"""压缩的保留量、软硬阈值与参数传播回归测试。

保留量原先按「最近 N 个 task 段」算，段数和 token 量没有对应关系；阈值只发给 engine
侧，supervisor 收缩时回落硬编码 256000，把 threshold_tokens 调低就会变成
「engine 反复触发、supervisor 压不够」的死循环。
"""
from __future__ import annotations

import ast
from typing import Any

from engine.compact import (
    apply_compact_summary,
    estimate_message_tokens,
    segments_within_token_budget,
    should_compact,
)
from engine.inference.ai_step import (
    _resolve_compact_hard_threshold,
    _resolve_compact_keep_recent_tokens,
)
from supervisor.task_router import TaskRouterMixin


def _msg(role: str, chars: int, task_id: str) -> dict[str, Any]:
    return {"role": role, "content": "x" * chars, "_meta": {"source_task_id": task_id}}


def _segmented(spec: list[tuple[str, int]]) -> list[dict[str, Any]]:
    """spec 是 [(task_id, 每段字符数)]，每段两条消息。"""
    out: list[dict[str, Any]] = []
    for task_id, chars in spec:
        out.append(_msg("user", chars // 2, task_id))
        out.append(_msg("assistant", chars // 2, task_id))
    return out


class TestTokenEstimation:
    def test_string_content_uses_the_same_divisor_as_should_compact(self) -> None:
        assert estimate_message_tokens({"content": "x" * 300}) == 100

    def test_list_content_counts_only_text_parts(self) -> None:
        message = {"content": [{"text": "x" * 300}, {"image_url": {"url": "data:..."}}]}

        assert estimate_message_tokens(message) == 100

    def test_object_content_is_read_through_attributes(self) -> None:
        class _Stored:
            content = "x" * 600

        assert estimate_message_tokens(_Stored()) == 200

    def test_missing_content_is_zero(self) -> None:
        assert estimate_message_tokens({}) == 0


class TestSegmentsWithinTokenBudget:
    def test_counts_trailing_segments_until_the_budget_is_full(self) -> None:
        # 每段 300 字符 = 100 token，预算 250 装得下两段。
        segments = [[_msg("user", 300, f"t{i}")] for i in range(5)]

        assert segments_within_token_budget(segments, 250) == 2

    def test_keeps_at_least_the_active_segment(self) -> None:
        # 当前活跃 task 不能被压掉，哪怕它自己就超预算。
        segments = [[_msg("user", 30000, "t0")], [_msg("user", 30000, "t1")]]

        assert segments_within_token_budget(segments, 100) == 1

    def test_zero_budget_means_the_caller_keeps_its_own_rule(self) -> None:
        segments = [[_msg("user", 300, "t0")]]

        assert segments_within_token_budget(segments, 0) == 0

    def test_no_segments_is_zero(self) -> None:
        assert segments_within_token_budget([], 30000) == 0

    def test_a_budget_covering_everything_keeps_everything(self) -> None:
        segments = [[_msg("user", 300, f"t{i}")] for i in range(4)]

        assert segments_within_token_budget(segments, 10_000) == 4


class TestApplyCompactSummary:
    def test_token_budget_overrides_the_segment_count(self) -> None:
        # keep_recent=6 会保留全部 5 段（不压缩），token 预算只留得下 2 段。
        messages = _segmented([(f"t{i}", 600) for i in range(5)])

        result = apply_compact_summary(
            messages, "S" * 300, keep_recent=6, keep_recent_tokens=450,
        )

        kept_ids = {m["_meta"]["source_task_id"] for m in result if m["_meta"]["source_task_id"] != "compact_summary"}
        assert kept_ids == {"t3", "t4"}

    def test_zero_token_budget_falls_back_to_segment_counting(self) -> None:
        messages = _segmented([(f"t{i}", 600) for i in range(5)])

        result = apply_compact_summary(messages, "S" * 300, keep_recent=2, keep_recent_tokens=0)

        kept_ids = {m["_meta"]["source_task_id"] for m in result if m["_meta"]["source_task_id"] != "compact_summary"}
        assert kept_ids == {"t3", "t4"}

    def test_the_summary_lands_as_one_message_with_the_compressed_ids(self) -> None:
        messages = _segmented([(f"t{i}", 600) for i in range(5)])

        result = apply_compact_summary(messages, "S" * 300, keep_recent=6, keep_recent_tokens=450)

        summaries = [m for m in result if m["_meta"]["source_task_id"] == "compact_summary"]
        assert len(summaries) == 1
        assert set(summaries[0]["_meta"]["compressed_task_ids"]) == {"t0", "t1", "t2"}

    def test_cuts_on_segment_boundaries_so_tool_pairs_stay_together(self) -> None:
        # 一段里的 assistant 与 tool_result 必须同去同留，否则供应商会拒。
        messages = [
            _msg("user", 600, "t0"),
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}], "_meta": {"source_task_id": "t1"}},
            {"role": "tool", "content": "r" * 600, "tool_call_id": "c1", "_meta": {"source_task_id": "t1"}},
        ]

        result = apply_compact_summary(messages, "S" * 300, keep_recent=6, keep_recent_tokens=100)

        tool_calls = [m for m in result if m.get("tool_calls")]
        tool_results = [m for m in result if m.get("tool_call_id")]
        assert len(tool_calls) == len(tool_results)


class _Router(TaskRouterMixin):
    """只装配 _apply_compact_via_conv_store_locked 真正用到的那几样东西。"""

    def __init__(self, workspace_root: Any, usage: dict[str, dict[str, Any]] | None = None) -> None:
        self.workspace_root = workspace_root
        self.synced: list[tuple[str, int]] = []
        # 真实的 SupervisorState 由 SessionMixin 建这张表；空表即「还没收到 usage 上报」。
        self._session_context_usage: dict[str, dict[str, Any]] = usage or {}

    def _sync_compact_to_branches(self, target_session_id: str, summary_msg: Any, to_keep: list[Any]) -> None:
        self.synced.append((target_session_id, len(to_keep)))


class TestSupervisorApplyOnDisk:
    """真正决定磁盘上留什么的是这段；它按段留还是按 token 留，从外部看不出来。"""

    SESSION = "sess-compact"

    def _store(self, tmp_path: Any, spec: list[tuple[str, int]]):
        from engine.conversation_store import ConversationStore, Message

        store = ConversationStore(tmp_path / "data" / "conversations")
        messages = [
            Message(
                id=f"m{i}", role="user", content="x" * chars,
                source_task_id=task_id, meta={"source_task_id": task_id},
            )
            for i, (task_id, chars) in enumerate(spec)
        ]
        store.append_batch(self.SESSION, messages)
        return store

    def test_the_token_budget_decides_what_stays_on_disk(self, tmp_path: Any) -> None:
        # 每条 600 字符 = 200 token，预算 450 只装得下最后两条。
        store = self._store(tmp_path, [(f"t{i}", 600) for i in range(5)])

        result = _Router(tmp_path)._apply_compact_via_conv_store_locked(
            self.SESSION, "S" * 300, keep_recent=6, threshold_tokens=256000, keep_recent_tokens=450,
        )

        assert result["kept_segments"] == 2
        kept = [m for m in store.load(self.SESSION) if m.source_task_id != "compact_summary"]
        assert [m.source_task_id for m in kept] == ["t3", "t4"]

    def test_keep_recent_segments_still_works_when_no_token_budget_is_set(self, tmp_path: Any) -> None:
        store = self._store(tmp_path, [(f"t{i}", 600) for i in range(5)])

        result = _Router(tmp_path)._apply_compact_via_conv_store_locked(
            self.SESSION, "S" * 300, keep_recent=2, threshold_tokens=256000, keep_recent_tokens=0,
        )

        assert result["kept_segments"] == 2
        kept = [m for m in store.load(self.SESSION) if m.source_task_id != "compact_summary"]
        assert [m.source_task_id for m in kept] == ["t3", "t4"]

    def test_exactly_one_summary_row_replaces_the_compressed_span(self, tmp_path: Any) -> None:
        store = self._store(tmp_path, [(f"t{i}", 600) for i in range(5)])

        _Router(tmp_path)._apply_compact_via_conv_store_locked(
            self.SESSION, "S" * 300, keep_recent=6, threshold_tokens=256000, keep_recent_tokens=450,
        )

        rows = store.load(self.SESSION)
        summaries = [m for m in rows if m.source_task_id == "compact_summary"]
        assert len(summaries) == 1
        assert set(summaries[0].meta["compressed_task_ids"]) == {"t0", "t1", "t2"}

    def test_a_budget_covering_everything_leaves_the_file_untouched(self, tmp_path: Any) -> None:
        store = self._store(tmp_path, [(f"t{i}", 600) for i in range(3)])

        result = _Router(tmp_path)._apply_compact_via_conv_store_locked(
            self.SESSION, "S" * 300, keep_recent=6, threshold_tokens=256000, keep_recent_tokens=100_000,
        )

        assert result["before"] == result["after"] == 3
        assert len(store.load(self.SESSION)) == 3

    def test_branch_sync_is_told_the_messages_that_survived(self, tmp_path: Any) -> None:
        # 分支前缀按存活消息条数对齐；数错了合并回主历史时会重复或丢消息。
        self._store(tmp_path, [(f"t{i}", 600) for i in range(5)])
        router = _Router(tmp_path)

        router._apply_compact_via_conv_store_locked(
            self.SESSION, "S" * 300, keep_recent=6, threshold_tokens=256000, keep_recent_tokens=450,
        )

        assert router.synced == [(self.SESSION, 2)]


class TestHardThreshold:
    def test_defaults_to_one_and_a_quarter_of_the_soft_threshold(self) -> None:
        cfg = {"engine": {"compact": {"threshold_tokens": 200_000}}}

        assert _resolve_compact_hard_threshold(cfg) == 250_000

    def test_an_explicit_value_wins(self) -> None:
        cfg = {"engine": {"compact": {"threshold_tokens": 200_000, "hard_threshold_tokens": 400_000}}}

        assert _resolve_compact_hard_threshold(cfg) == 400_000

    def test_never_lands_below_the_soft_threshold(self) -> None:
        # 硬阈值低于软阈值会让每次触发都变成同步压缩，静默压缩就白做了。
        cfg = {"engine": {"compact": {"threshold_tokens": 200_000, "hard_threshold_tokens": 50_000}}}

        assert _resolve_compact_hard_threshold(cfg) == 200_000

    def test_soft_and_hard_bracket_the_background_window(self) -> None:
        cfg = {"engine": {"compact": {"threshold_tokens": 100_000}}}
        hard = _resolve_compact_hard_threshold(cfg)
        messages: list[dict[str, Any]] = []

        # 110k token 落在软硬之间 → 后台压缩，用户无感
        assert should_compact(messages, 100_000, 110_000)
        assert not should_compact(messages, hard, 110_000)
        # 130k 顶到硬阈值 → 必须同步压
        assert should_compact(messages, hard, 130_000)


class TestKeepRecentTokensCeiling:
    def test_the_configured_value_passes_through_when_it_fits(self) -> None:
        cfg = {"engine": {"compact": {"threshold_tokens": 256_000, "keep_recent_tokens": 30_000}}}

        assert _resolve_compact_keep_recent_tokens(cfg) == 30_000

    def test_a_value_above_eighty_percent_of_the_threshold_is_capped(self) -> None:
        # 保留量压不到阈值之下，就会每轮重新触发、重新压出同样的结果。
        cfg = {"engine": {"compact": {"threshold_tokens": 100_000, "keep_recent_tokens": 300_000}}}

        assert _resolve_compact_keep_recent_tokens(cfg) == 80_000

    def test_zero_stays_zero_so_the_segment_rule_keeps_working(self) -> None:
        cfg = {"engine": {"compact": {"threshold_tokens": 256_000, "keep_recent_tokens": 0}}}

        assert _resolve_compact_keep_recent_tokens(cfg) == 0

    def test_the_capped_value_really_compresses_below_the_threshold(self, tmp_path: Any) -> None:
        cfg = {"engine": {"compact": {"threshold_tokens": 1_000, "keep_recent_tokens": 5_000}}}
        keep = _resolve_compact_keep_recent_tokens(cfg)

        # 每段 600 字符 = 200 token；预算 800 留 4 段 = 800 token，低于阈值 1000。
        segments = [[_msg("user", 600, f"t{i}")] for i in range(10)]
        kept = segments_within_token_budget(segments, keep)

        assert kept * 200 < 1_000


class TestSizingPropagation:
    def test_all_three_sizing_fields_survive_the_hop(self) -> None:
        source = {
            "_compact_keep_recent": 4,
            "_compact_keep_recent_tokens": 30_000,
            "_compact_threshold_tokens": 100_000,
        }

        assert TaskRouterMixin._compact_sizing(source) == {
            "keep_recent": 4,
            "keep_recent_tokens": 30_000,
            "threshold_tokens": 100_000,
        }

    def test_a_configured_threshold_is_not_replaced_by_the_hardcoded_fallback(self) -> None:
        # 这是死循环的根因：engine 按 100k 触发，supervisor 按 256000 收缩，永远压不够。
        sizing = TaskRouterMixin._compact_sizing({"_compact_threshold_tokens": 100_000})

        assert sizing["threshold_tokens"] == 100_000

    def test_a_missing_threshold_still_falls_back(self) -> None:
        assert TaskRouterMixin._compact_sizing({})["threshold_tokens"] == 256_000

    def test_the_sizing_keys_match_what_the_apply_functions_accept(self) -> None:
        # 参数名对不上就会在压缩落盘那一刻抛 TypeError，而那条路径没有测试兜着。
        import inspect

        sizing = TaskRouterMixin._compact_sizing({})
        for func in (apply_compact_summary, TaskRouterMixin._apply_compact_via_conv_store_locked):
            params = inspect.signature(func).parameters
            for key in sizing:
                assert key in params, f"{func.__name__} 缺少参数 {key}"


def _dict_keys(node: ast.Dict) -> set[str]:
    return {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}


class TestEngineDispatchCarriesSizing:
    """尺寸参数少发一个，supervisor 就回落默认值，与 engine 的触发阈值脱钩。"""

    @staticmethod
    def _compact_module() -> ast.Module:
        return ast.parse(open("engine/builtin/compact.py", encoding="utf-8").read())

    def test_the_dispatch_input_itself_carries_all_three(self) -> None:
        dispatch_inputs = [
            keys for node in ast.walk(self._compact_module())
            if isinstance(node, ast.Dict) and "_compact_dispatch" in (keys := _dict_keys(node))
        ]

        assert dispatch_inputs, "找不到 dispatch_input 字面量"
        for keys in dispatch_inputs:
            assert {"_compact_keep_recent", "_compact_keep_recent_tokens", "_compact_threshold_tokens"} <= keys

    def test_the_background_request_body_carries_all_three(self) -> None:
        # 用 caller_node_id 认请求体：compact_start 事件和同步 dispatch_input 都带
        # target_session_id + instruction，只有 HTTP 请求体带调用方节点。
        bodies = [
            keys for node in ast.walk(self._compact_module())
            if isinstance(node, ast.Dict) and "caller_node_id" in (keys := _dict_keys(node))
        ]

        assert bodies, "找不到后台压缩请求体"
        for keys in bodies:
            assert {"keep_recent", "keep_recent_tokens", "threshold_tokens"} <= keys

    def test_the_background_request_returns_no_action(self) -> None:
        # 返回 TaskAction 就意味着当轮让位挂起，静默压缩的前提是它什么都不返回。
        found = [
            node for node in ast.walk(self._compact_module())
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_request_background_compact"
        ]

        assert len(found) == 1
        assert isinstance(found[0].returns, ast.Constant) and found[0].returns.value is None

    def test_the_endpoint_the_engine_calls_exists_in_the_supervisor(self) -> None:
        engine_src = open("engine/builtin/compact.py", encoding="utf-8").read()
        api_src = open("supervisor/api.py", encoding="utf-8").read()

        assert "/v1/tasks/compact-async" in engine_src
        assert '"/v1/tasks/compact-async"' in api_src
