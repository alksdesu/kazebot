"""压缩链上四处会静默毁数据或空转的接线。

这四条都不会报错，只会让上下文越压越大或者把历史换成别人的摘要：
手动压缩的目标会话、跨进程熔断、跨进程缓存、中文 token 估算。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.compact import (
    DEFAULT_CHARS_PER_TOKEN,
    observed_chars_per_token,
    segments_within_token_budget,
    should_compact,
    total_content_chars,
)
from engine.conversation_store import ConversationStore, Message
from engine.inference.loop_state import compact_target_session_id
from supervisor.compact_breaker import CompactBreaker


def _ls(**rctx: Any) -> SimpleNamespace:
    base = {"child_session_id": "", "parent_session_id": "", "session_id": ""}
    base.update(rctx)
    return SimpleNamespace(rctx=SimpleNamespace(**base))


class TestCompactTarget:
    def test_a_child_session_compacts_its_own_history(self) -> None:
        ls = _ls(child_session_id="child-1", parent_session_id="parent-1", session_id="branch-1")

        assert compact_target_session_id(ls) == "child-1"

    def test_an_entry_branch_compacts_the_parent(self) -> None:
        # branch 马上会被删，改写它等于什么都没压。
        ls = _ls(parent_session_id="parent-1", session_id="branch-1")

        assert compact_target_session_id(ls) == "parent-1"

    def test_a_plain_session_compacts_itself(self) -> None:
        assert compact_target_session_id(_ls(session_id="sess-1")) == "sess-1"

    def test_every_compact_entry_point_asks_the_same_question(self) -> None:
        """自动压缩、手动 compact_context、L2 snip 必须用同一个答案。

        手动路径原来不发 target_session_id，supervisor 就回落到 parent_session_id ——
        子节点手动压缩时 instruction 是子节点自己的对话，摘要却整段替换父会话的历史。
        """
        import ast

        sources = {
            "engine/builtin/compact.py": None,
            "engine/inference/ai_step.py": None,
            "engine/inference/pseudo_handlers.py": None,
        }
        for rel in sources:
            tree = ast.parse((_ROOT / rel).read_text(encoding="utf-8"))
            calls = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "compact_target_session_id"
            ]
            assert calls, f"{rel} 没有调用共享的 compact_target_session_id"

        # 旧的重复实现不能再回来
        for rel in ("engine/builtin/compact.py", "engine/inference/ai_step.py"):
            text = (_ROOT / rel).read_text(encoding="utf-8")
            assert "def _compact_target_session_id" not in text, f"{rel} 又抄了一份"

    def test_the_manual_compact_dispatch_carries_a_target(self) -> None:
        import ast

        tree = ast.parse((_ROOT / "engine/inference/pseudo_handlers.py").read_text(encoding="utf-8"))
        handler = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_handle_pseudo_compact"
        )
        keys = {
            key.value for node in ast.walk(handler) if isinstance(node, ast.Dict)
            for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        assert "target_session_id" in keys


class TestBreaker:
    def test_it_stays_closed_below_the_threshold(self) -> None:
        breaker = CompactBreaker(max_failures=3)
        breaker.record_failure("s")
        breaker.record_failure("s")

        assert not breaker.is_open("s")

    def test_it_opens_at_the_threshold(self) -> None:
        breaker = CompactBreaker(max_failures=3)
        for _ in range(3):
            breaker.record_failure("s")

        assert breaker.is_open("s")

    def test_a_success_closes_it(self) -> None:
        breaker = CompactBreaker(max_failures=3)
        for _ in range(3):
            breaker.record_failure("s")

        breaker.record_success("s")

        assert not breaker.is_open("s")
        assert breaker.failure_count("s") == 0

    def test_the_cooldown_expires_with_a_clean_slate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 冷却结束还挂着旧计数的话，下一次失败会立刻再熔断，等于永久关闭。
        import supervisor.compact_breaker as module

        clock = [1000.0]
        monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
        breaker = CompactBreaker(max_failures=3, cooldown_seconds=600.0)
        for _ in range(3):
            breaker.record_failure("s")

        clock[0] += 601.0

        assert not breaker.is_open("s")
        assert breaker.failure_count("s") == 0

    def test_sessions_do_not_accumulate_without_bound(self) -> None:
        # 清理点有十来处，靠外部通知必漏；自己带上限就没有泄漏面。
        breaker = CompactBreaker(max_tracked=4)
        for i in range(20):
            breaker.record_failure(f"s{i}")

        assert breaker.failure_count("s0") == 0
        assert breaker.failure_count("s19") == 1

    def test_a_blank_session_id_is_ignored(self) -> None:
        breaker = CompactBreaker(max_failures=1)
        breaker.record_failure("")

        assert not breaker.is_open("")

    def test_the_engine_no_longer_carries_its_own_breaker(self) -> None:
        """熔断状态必须只有一份。

        engine 是多个独立 worker 进程，各自的 module dict 互相看不见，而压缩失败几乎
        全在 supervisor 侧记账 —— engine 侧那份计数器恒为 0，判它等于没判。
        """
        import engine.compact as engine_compact

        for gone in ("record_compact_failure", "record_compact_success", "is_compact_circuit_open"):
            assert not hasattr(engine_compact, gone), f"engine.compact 又有了 {gone}"

        for rel in ("engine/builtin/compact.py", "engine/inference/ai_step.py"):
            text = (_ROOT / rel).read_text(encoding="utf-8")
            assert "is_compact_circuit_open" not in text, f"{rel} 还在读进程内熔断器"


class TestStaleCache:
    def test_another_writer_invalidates_the_cache(self, tmp_path: Path) -> None:
        """supervisor 压缩后 engine 必须看到新内容。

        看不到就会拿压缩前的快照去 replace_all，把摘要整段覆盖回去 —— compactor 那次
        调用白花，历史回到压缩前，下一轮又超阈值。
        """
        data_dir = tmp_path / "conversations"
        engine_side = ConversationStore(data_dir)
        supervisor_side = ConversationStore(data_dir)

        engine_side.append_batch("s", [Message(id=f"m{i}", role="user", content="x") for i in range(5)])
        assert len(engine_side.load("s")) == 5

        supervisor_side.replace_all("s", [Message(id="sum", role="user", content="摘要")])

        assert [m.id for m in engine_side.load("s")] == ["sum"]

    def test_a_deletion_by_another_writer_is_seen(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "conversations"
        reader = ConversationStore(data_dir)
        writer = ConversationStore(data_dir)
        writer.append_batch("s", [Message(id="m0", role="user", content="x")])
        assert len(reader.load("s")) == 1

        writer.delete("s")

        assert reader.load("s") == []

    def test_a_single_writer_still_serves_from_cache(self, tmp_path: Path) -> None:
        # append 后指纹要跟着更新，否则每写一条消息都要整文件重读一遍，长会话上是 O(n²)。
        store = ConversationStore(tmp_path / "conversations")
        store.load("s")
        cached = store._cache["s"]

        store.append("s", Message(id="m0", role="user", content="x"))

        assert store.load("s") is cached
        assert [m.id for m in cached] == ["m0"]

    def test_stamps_do_not_outlive_their_cache_entry(self, tmp_path: Path) -> None:
        store = ConversationStore(tmp_path / "conversations")
        store.append_batch("s", [Message(id="m0", role="user", content="x")])
        store.load("s")

        store.invalidate_cache("s")

        assert "s" not in store._cache
        assert "s" not in store._cache_stamps


class TestCharsPerToken:
    def test_no_real_data_falls_back_to_the_default(self) -> None:
        assert observed_chars_per_token(0, None) == DEFAULT_CHARS_PER_TOKEN
        assert observed_chars_per_token(3000, 0) == DEFAULT_CHARS_PER_TOKEN
        assert observed_chars_per_token(0, 1000) == DEFAULT_CHARS_PER_TOKEN

    def test_chinese_calibrates_well_below_three(self) -> None:
        # 3000 个汉字实测 ~2500 token，硬编码 3 会认为只有 1000。
        assert observed_chars_per_token(3000, 2500) == pytest.approx(1.2)

    def test_english_stays_near_the_default(self) -> None:
        assert observed_chars_per_token(4000, 1000) == pytest.approx(4.0)

    def test_the_ratio_can_never_drop_below_one(self) -> None:
        # 图片 token 算进 prompt_tokens 但字符里没有；字符数不可能少于 token 数。
        assert observed_chars_per_token(1000, 100_000) == 1.0

    def test_the_ratio_is_capped(self) -> None:
        # 缓存命中让上报值异常小的话比例会暴涨，跟着它裁剪等于不裁剪。
        assert observed_chars_per_token(1_000_000, 10) == 6.0

    def test_the_budget_shrinks_when_the_ratio_is_calibrated(self) -> None:
        """同一个 keep_recent_tokens，中文比例下能装的段数必须变少。

        这就是「保留最近 30k token」实际留下 60k+ 的成因。
        """
        segments = [[{"content": "汉" * 600}] for _ in range(6)]

        assert segments_within_token_budget(segments, 600, DEFAULT_CHARS_PER_TOKEN) == 3
        assert segments_within_token_budget(segments, 600, 1.2) == 1

    def test_should_compact_uses_the_same_ratio(self) -> None:
        messages = [{"role": "user", "content": "汉" * 1200}]

        assert not should_compact(messages, 500, chars_per_token=DEFAULT_CHARS_PER_TOKEN)
        assert should_compact(messages, 500, chars_per_token=1.2)

    def test_a_real_prompt_token_count_still_wins(self) -> None:
        # 有真实值就不该再估算。
        assert should_compact([{"content": "x"}], 100, last_prompt_tokens=101)
        assert not should_compact([{"content": "x" * 10_000}], 100, last_prompt_tokens=50)

    def test_multimodal_content_counts_only_its_text(self) -> None:
        messages = [{"content": [
            {"type": "text", "text": "abcde"},
            {"type": "image_url", "image_url": {"url": "data:..." + "A" * 9999}},
        ]}]

        assert total_content_chars(messages) == 5

    def test_message_objects_are_counted_like_dicts(self) -> None:
        assert total_content_chars([Message(id="m", role="user", content="abc")]) == 3


class TestSupervisorCalibration:
    """supervisor 侧的裁剪必须真的用上 engine 报来的比例。"""

    SESSION = "sess-cal"

    def _router(self, tmp_path: Path, usage: dict[str, Any] | None):
        from supervisor.task_router import TaskRouterMixin

        class _Router(TaskRouterMixin):
            def __init__(self) -> None:
                self.workspace_root = tmp_path
                self._session_context_usage = {TestSupervisorCalibration.SESSION: usage} if usage else {}

            def _sync_compact_to_branches(self, *args: Any) -> None:
                return None

        return _Router()

    def _seed(self, tmp_path: Path, segments: int = 5, chars: int = 600) -> ConversationStore:
        store = ConversationStore(tmp_path / "data" / "conversations")
        store.append_batch(self.SESSION, [
            Message(
                id=f"m{i}", role="user", content="汉" * chars,
                source_task_id=f"t{i}", meta={"source_task_id": f"t{i}"},
            )
            for i in range(segments)
        ])
        return store

    def test_without_usage_it_uses_the_default_ratio(self, tmp_path: Path) -> None:
        self._seed(tmp_path)

        result = self._router(tmp_path, None)._apply_compact_via_conv_store_locked(
            self.SESSION, "S" * 300, keep_recent=6, threshold_tokens=256000, keep_recent_tokens=450,
        )

        assert result["kept_segments"] == 2

    def test_a_reported_chinese_ratio_keeps_fewer_segments(self, tmp_path: Path) -> None:
        # 600 汉字实际 500 token，预算 450 一段都装不下 → 触发 max(kept, 1) 下限。
        self._seed(tmp_path)

        result = self._router(tmp_path, {
            "prompt_tokens": 2500, "prompt_chars": 3000,
        })._apply_compact_via_conv_store_locked(
            self.SESSION, "S" * 300, keep_recent=6, threshold_tokens=256000, keep_recent_tokens=450,
        )

        assert result["kept_segments"] == 1

    def test_prompt_chars_is_preferred_over_the_store_history(self, tmp_path: Path) -> None:
        """store 里没有 system 段，用它当分子会把比例算小。

        engine 上报的 prompt_chars 和 prompt_tokens 是同一批消息，才是准的。
        """
        self._seed(tmp_path)
        router = self._router(tmp_path, {"prompt_tokens": 1000, "prompt_chars": 4000})

        ratio = router._session_chars_per_token(self.SESSION, [])

        assert ratio == pytest.approx(4.0)

    def test_it_falls_back_to_the_store_history_without_prompt_chars(self, tmp_path: Path) -> None:
        self._seed(tmp_path)
        router = self._router(tmp_path, {"prompt_tokens": 1000})
        messages = [Message(id="m", role="user", content="x" * 2000)]

        assert router._session_chars_per_token(self.SESSION, messages) == pytest.approx(2.0)

    def test_the_engine_reports_the_chars_behind_its_token_count(self) -> None:
        import ast

        tree = ast.parse((_ROOT / "engine/inference/llm_call.py").read_text(encoding="utf-8"))
        keys = {
            key.value for node in ast.walk(tree) if isinstance(node, ast.Dict)
            for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        assert "prompt_chars" in keys, "engine 不报字符数，supervisor 就无法校准比例"


