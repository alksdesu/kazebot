"""子会话拆除必须删完一整组运行期文件。

拆除路径有六条，删 data/transcripts/ 的只有启动时的 reconcile 那一条 —— 于是每个
dispatch 出去的子节点都在磁盘上留一份转写，进程内没有任何清理者。不报错、不影响功能，
只是无声增长，直到有人去看那个目录。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from supervisor.eventlog import EventLog
from supervisor.policy import PolicyEngine
from supervisor.state import SupervisorState


def _state(workspace: Path) -> SupervisorState:
    eventlog = EventLog(workspace / "data" / "events.jsonl", run_id="run-teardown")
    return SupervisorState(
        workspace_root=workspace,
        eventlog=eventlog,
        policy=PolicyEngine(workspace_root=workspace),
    )


def _seed(workspace: Path, session_id: str) -> tuple[Path, Path]:
    conv = workspace / "data" / "conversations" / f"{session_id}.jsonl"
    transcript = workspace / "data" / "transcripts" / f"{session_id}.jsonl"
    for path in (conv, transcript):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"role": "user", "content": "x"}\n', encoding="utf-8")
    return conv, transcript


class TestPurgeHelper:
    def test_it_removes_both_artifacts(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        conv, transcript = _seed(tmp_path, "child_abc")

        state.purge_session_files("child_abc")

        assert not conv.exists()
        assert not transcript.exists()

    def test_a_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        _state(tmp_path).purge_session_files("never_existed")

    def test_a_blank_session_id_is_ignored(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        conv, transcript = _seed(tmp_path, "child_abc")

        state.purge_session_files("")
        state.purge_session_files("   ")

        assert conv.exists() and transcript.exists()

    def test_it_does_not_touch_a_sibling_session(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        _seed(tmp_path, "child_a")
        keep_conv, keep_transcript = _seed(tmp_path, "child_b")

        state.purge_session_files("child_a")

        assert keep_conv.exists() and keep_transcript.exists()

    def test_the_file_group_is_declared_in_one_place(self, tmp_path: Path) -> None:
        """新增一类运行期产物时，只能有一处需要改。"""
        state = _state(tmp_path)

        names = [p.parent.name for p in state.session_runtime_files("s1")]

        assert names == ["conversations", "transcripts"]

    def test_a_purged_session_reads_back_empty(self, tmp_path: Path) -> None:
        from engine.conversation_store import ConversationStore, Message

        state = _state(tmp_path)
        store = ConversationStore(tmp_path / "data" / "conversations")
        store.append_batch("child_abc", [Message(id="m1", role="user", content="x")])
        assert store.load("child_abc")

        state.purge_session_files("child_abc")

        assert store.load("child_abc") == []

    def test_the_conversation_file_goes_through_the_store(self) -> None:
        """走 ConversationStore.delete 而不是裸 unlink。

        当前 supervisor 每次用完就丢 store 实例，两种写法行为上没差别 —— 这条是结构
        约束：谁将来把 store 缓存起来，裸 unlink 就会留下一份读得到已删会话的缓存。
        """
        fn = next(
            node for node in ast.walk(ast.parse((_ROOT / "supervisor/session.py").read_text(encoding="utf-8")))
            if isinstance(node, ast.FunctionDef) and node.name == "purge_session_files"
        )
        body = ast.unparse(fn)

        assert "ConversationStore" in body and ".delete(" in body


class TestEveryTeardownPathPurgesBoth:
    def test_expiring_a_child_session_removes_its_transcript(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        parent = state.get_or_create_session(channel="test", conversation_key="test:expire")
        child, _ = state.get_or_create_child_session(parent, "child.node", "case", "fresh")
        conv, transcript = _seed(tmp_path, child)

        state._expire_child_session(child)

        assert not conv.exists()
        assert not transcript.exists()

    def test_resetting_a_conversation_removes_child_transcripts(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        parent = state.get_or_create_session(channel="test", conversation_key="test:reset")
        child, _ = state.get_or_create_child_session(parent, "child.node", "case", "fresh")
        parent_files = _seed(tmp_path, parent)
        child_files = _seed(tmp_path, child)

        assert state.reset_conversation(conversation_key="test:reset")["ok"]

        for path in (*parent_files, *child_files):
            assert not path.exists(), f"{path} 没被清掉"

    def test_a_startup_reconcile_removes_both(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        parent = state.get_or_create_session(channel="test", conversation_key="test:stale")
        child, _ = state.get_or_create_child_session(parent, "child.node", "case", "fresh")
        conv, transcript = _seed(tmp_path, child)
        # child 的 last_active_at 推到 TTL 之外，重启协调就会把它当过期清掉。
        with state._lock:
            state._session_store.set_entry_field(child, "last_active_at", "2020-01-01T00:00:00+00:00")

        state._reconcile_after_restart()

        assert not conv.exists()
        assert not transcript.exists()

    def test_a_dispatch_session_cleanup_removes_both(self, tmp_path: Path) -> None:
        from datetime import datetime, timezone

        from supervisor.types import Task, TaskKind

        state = _state(tmp_path)
        dispatch_sid = state.get_or_create_session(
            channel="internal", conversation_key="agent:bootstrap.executor:qq_group:1:uuid-x",
        )
        conv, transcript = _seed(tmp_path, dispatch_sid)
        now = datetime.now(timezone.utc)
        task = Task(
            task_id="t1", session_id=dispatch_sid, session_generation=1,
            kind=TaskKind.node, node_id="bootstrap.executor",
            input={"parent_session_id": dispatch_sid},
            created_at=now, updated_at=now,
        )

        with state._lock:
            state._cleanup_dispatch_session_locked(task)

        assert not conv.exists()
        assert not transcript.exists()


class TestNoTeardownPathHandRollsPaths:
    """每条拆除路径都必须走同一个 helper，否则漏一处就是又一个泄漏点。"""

    _TEARDOWN_FUNCTIONS = {
        ("supervisor/session.py", "_cleanup_branch_locked"),
        ("supervisor/session.py", "reset_conversation"),
        ("supervisor/state.py", "_expire_child_session"),
        ("supervisor/state.py", "_reconcile_after_restart"),
        ("supervisor/task_router.py", "_execute_post_cleanup"),
        ("supervisor/task_router.py", "_cleanup_dispatch_session_locked"),
    }

    def _function(self, rel: str, name: str) -> ast.AST | None:
        tree = ast.parse((_ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return node
        return None

    def test_the_teardown_functions_still_exist(self) -> None:
        missing = [
            f"{rel}:{name}" for rel, name in sorted(self._TEARDOWN_FUNCTIONS)
            if self._function(rel, name) is None
        ]
        assert missing == [], f"拆除入口改名了，这个清单要跟着更新: {missing}"

    def test_none_of_them_builds_a_session_file_path_by_hand(self) -> None:
        offenders: list[str] = []
        for rel, name in sorted(self._TEARDOWN_FUNCTIONS):
            fn = self._function(rel, name)
            if fn is None:
                continue
            body = ast.unparse(fn)
            for literal in ("'conversations'", '"conversations"', "'transcripts'", '"transcripts"'):
                if literal in body:
                    offenders.append(f"{rel}:{name} 自己拼了 {literal}")
        assert offenders == [], offenders

    def test_each_of_them_reaches_the_shared_helper(self) -> None:
        offenders: list[str] = []
        for rel, name in sorted(self._TEARDOWN_FUNCTIONS):
            fn = self._function(rel, name)
            if fn is None:
                continue
            body = ast.unparse(fn)
            # reset_conversation 通过 _cleanup_branch_locked 间接清理 branch，
            # 但它自己也要清主 session 和普通 child。
            if "purge_session_files" not in body:
                offenders.append(f"{rel}:{name}")
        assert offenders == [], f"这些拆除路径没有走共享 helper: {offenders}"
