"""自动记忆提取写进的目录，必须就是注入侧下一轮会读的那个。

持久化子节点跑在 agent:<节点>:<根会话> 会话里，提取器却按这个运行期键派生 namespace：
写进的是一个谁都不读的孤儿目录，提示词里展示的现有 book 列表也来自那里。
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.builtin.knowledge_inject import build_knowledge_context, save_memory  # noqa: E402
from engine.builtin.memory_extract import MemoryExtractHandler  # noqa: E402
from engine.memory_namespace import NAMESPACE_KEY_PREFIX, conversation_namespace  # noqa: E402
from engine.node import Node, load_node  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402

_ROOT_KEY = "qq_group:900001"
_CHILD_KEY = f"agent:smith:{_ROOT_KEY}"
_SESSION = "sess-child"
_TASK_ID = "task-child-1"


def _child_task_context() -> dict[str, Any]:
    return {
        "conversation_key": _CHILD_KEY,
        "route_conversation_key": _ROOT_KEY,
        "parent_conversation_key": _ROOT_KEY,
        "dispatch_context_mode": "accumulate",
    }


def _seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **node_fields: Any) -> None:
    config = tmp_path / "config"
    (config / "nodes").mkdir(parents=True, exist_ok=True)
    (config / "runtime.yaml").write_text(
        yaml.safe_dump({"memory": {"auto_extract": {"enabled": True, "node_id": "system.memory_extractor"}}}),
        encoding="utf-8",
    )
    (config / "nodes" / "smith.yaml").write_text(
        yaml.safe_dump({"id": "smith", "type": "ai", "persistent": True, **node_fields}, allow_unicode=True),
        encoding="utf-8",
    )
    messages = [
        SimpleNamespace(source_task_id=_TASK_ID, role="user", content="记一下：部署走蓝绿", message_type="", name=""),
        SimpleNamespace(source_task_id=_TASK_ID, role="assistant", content="好的", message_type="", name=""),
    ]

    class _Store:
        def __init__(self, _root: Path) -> None:
            pass

        def load(self, _session_id: str) -> list[SimpleNamespace]:
            return messages

    monkeypatch.setattr("engine.conversation_store.ConversationStore", _Store)


def _write_book(tmp_path: Path, namespace: str, book: str) -> None:
    path = tmp_path / "data" / "memory" / namespace / f"{book}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"book": book, "entries": [{"id": f"{book}_1", "content": "x", "keywords": ["k"]}]}),
        encoding="utf-8",
    )


def _schedule(tmp_path: Path, task_context: dict[str, Any] | None = None) -> tuple[MemoryExtractHandler, list[dict]]:
    handler = MemoryExtractHandler()
    intents: list[dict[str, Any]] = []
    task = SimpleNamespace(
        task_id=_TASK_ID,
        node_id="smith",
        kind="node",
        result={"action": "finish"},
        input={"task_context": task_context if task_context is not None else _child_task_context()},
        source_inbound_seq=7,
        caller_task_id=None,
        session_id=_SESSION,
        session_generation=1,
    )
    handler.on_task_complete({
        "workspace_root": str(tmp_path),
        "task": task,
        "session_id": _SESSION,
        "persist_memory_extract_intent": intents.append,
    })
    return handler, intents


def _fire(handler: MemoryExtractHandler, tmp_path: Path, intent: dict[str, Any]) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []

    def _create_task(**kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(task_id="extractor-1")

    handler.fire_persisted_intent({"workspace_root": str(tmp_path), "create_task": _create_task}, intent)
    assert len(calls) == 1
    return calls[0]["input_data"]


def _extractor_ctx(tmp_path: Path, task_context: dict[str, Any]) -> ToolContext:
    """照 loop_state.build_tool_context 的取法给提取器配一份 ToolContext。"""
    ctx = ToolContext(
        supervisor_url="http://localhost:0",
        session_id=_SESSION,
        run_id="extractor-1",
        worker_id="worker-1",
        workspace_root=tmp_path,
        http=None,  # type: ignore[arg-type]
        registry=None,
        conversation_key=str(task_context.get("conversation_key", "")).strip(),
        node_id="system.memory_extractor",
    )
    object.__setattr__(ctx, "_node_extra", {})
    object.__setattr__(ctx, "_memory_subjects", [])
    object.__setattr__(ctx, "_route_conversation_key", str(task_context.get("route_conversation_key") or ""))
    object.__setattr__(ctx, "_parent_conversation_key", str(task_context.get("parent_conversation_key") or ""))
    return ctx


def _injected_text(tmp_path: Path, node: Node) -> str:
    _ss, _sd, memory_static, memory_dynamic = build_knowledge_context(
        tmp_path, node, "部署", [], {}, task_context=_child_task_context(),
    )
    return "\n".join(str(message.get("content") or "") for message in memory_static + memory_dynamic)


class TestPersistentChildWithoutMemoryBook:
    def test_the_intent_targets_the_root_conversation(self, tmp_path: Path, monkeypatch) -> None:
        _seed(tmp_path, monkeypatch)

        _handler, intents = _schedule(tmp_path)

        assert intents[0]["memory_namespace"] == conversation_namespace(_ROOT_KEY)
        assert intents[0]["memory_namespace"] != conversation_namespace(_CHILD_KEY)

    def test_the_extractor_is_shown_the_books_of_that_namespace(self, tmp_path: Path, monkeypatch) -> None:
        _seed(tmp_path, monkeypatch)
        _write_book(tmp_path, conversation_namespace(_ROOT_KEY), "deploy")
        _write_book(tmp_path, conversation_namespace(_CHILD_KEY), "orphan")
        handler, intents = _schedule(tmp_path)

        instruction = _fire(handler, tmp_path, intents[0])["instruction"]

        assert "deploy" in instruction
        assert "orphan" not in instruction

    def test_what_the_extractor_saves_is_what_the_child_reads_next_turn(self, tmp_path: Path, monkeypatch) -> None:
        _seed(tmp_path, monkeypatch)
        handler, intents = _schedule(tmp_path)
        task_context = _fire(handler, tmp_path, intents[0])["task_context"]

        result = asyncio.run(save_memory(
            {"id": "deploy_rule", "book": "rules", "content": "部署走蓝绿发布", "constant": True},
            _extractor_ctx(tmp_path, task_context),
        ))

        assert result["ok"] is True
        assert (tmp_path / "data" / "memory" / conversation_namespace(_ROOT_KEY) / "rules.yaml").exists()
        assert not (tmp_path / "data" / "memory" / conversation_namespace(_CHILD_KEY)).exists()
        assert "部署走蓝绿发布" in _injected_text(tmp_path, Node(id="smith", type="ai"))


class TestPersistentChildWithMemoryBook:
    def test_the_intent_targets_the_node_memory_book(self, tmp_path: Path, monkeypatch) -> None:
        _seed(tmp_path, monkeypatch, memory_book="smith_notes")

        _handler, intents = _schedule(tmp_path)

        assert intents[0]["memory_namespace"] == "smith_notes"

    def test_the_extractor_key_names_that_directory(self, tmp_path: Path, monkeypatch) -> None:
        _seed(tmp_path, monkeypatch, memory_book="smith_notes")
        _write_book(tmp_path, "smith_notes", "research")
        _write_book(tmp_path, conversation_namespace(_ROOT_KEY), "group_book")
        handler, intents = _schedule(tmp_path)

        input_data = _fire(handler, tmp_path, intents[0])

        assert input_data["task_context"]["conversation_key"] == NAMESPACE_KEY_PREFIX + "smith_notes"
        assert conversation_namespace(input_data["task_context"]["conversation_key"]) == "smith_notes"
        assert "research" in input_data["instruction"]
        assert "group_book" not in input_data["instruction"]

    def test_what_the_extractor_saves_is_what_the_child_reads_next_turn(self, tmp_path: Path, monkeypatch) -> None:
        _seed(tmp_path, monkeypatch, memory_book="smith_notes")
        handler, intents = _schedule(tmp_path)
        task_context = _fire(handler, tmp_path, intents[0])["task_context"]

        result = asyncio.run(save_memory(
            {"id": "paper", "book": "research", "content": "那篇论文的结论是负面的", "constant": True},
            _extractor_ctx(tmp_path, task_context),
        ))

        assert result["ok"] is True
        assert (tmp_path / "data" / "memory" / "smith_notes" / "research.yaml").exists()
        node = load_node(tmp_path, "smith")
        assert node is not None
        assert "那篇论文的结论是负面的" in _injected_text(tmp_path, node)

    def test_an_invalid_memory_book_extracts_nothing_and_piles_up_nothing(
        self, tmp_path: Path, monkeypatch, caplog,
    ) -> None:
        _seed(tmp_path, monkeypatch, memory_book="../escape")

        with caplog.at_level(logging.WARNING, logger="engine.builtin.memory_extract"):
            handler, intents = _schedule(tmp_path)

        assert intents == []
        assert handler._memory_extract_pending_task_ids.get(_SESSION) is None
        assert any("memory_book" in record.getMessage() for record in caplog.records)


class TestRootConversation:
    def test_a_root_conversation_keeps_its_own_namespace(self, tmp_path: Path, monkeypatch) -> None:
        _seed(tmp_path, monkeypatch)
        handler, intents = _schedule(tmp_path, {"conversation_key": _ROOT_KEY})

        input_data = _fire(handler, tmp_path, intents[0])

        assert intents[0]["memory_namespace"] == conversation_namespace(_ROOT_KEY)
        assert conversation_namespace(input_data["task_context"]["conversation_key"]) == conversation_namespace(_ROOT_KEY)

    def test_an_intent_persisted_without_a_namespace_still_fires(self, tmp_path: Path) -> None:
        # 重启前落盘的意图只有会话键；按它推出的目录和原来一致，不能因为缺字段就丢掉。
        intent = {
            "intent_id": "memory_extract:legacy",
            "session_id": "sess-legacy",
            "session_generation": 1,
            "transcript": "User: remember this",
            "task_count": 1,
            "pending_task_ids": ["t1"],
            "extractor_node": "system.memory_extractor",
            "kind": "node",
            "conversation_key": _ROOT_KEY,
        }

        input_data = _fire(MemoryExtractHandler(), tmp_path, intent)

        assert conversation_namespace(input_data["task_context"]["conversation_key"]) == conversation_namespace(_ROOT_KEY)

    def test_a_tampered_intent_is_dropped_instead_of_retried_forever(self, tmp_path: Path) -> None:
        completed: list[str] = []
        created: list[dict[str, Any]] = []
        intent = {
            "intent_id": "memory_extract:bad",
            "session_id": "sess-bad",
            "session_generation": 1,
            "transcript": "User: remember this",
            "memory_namespace": "../escape",
        }

        MemoryExtractHandler().fire_persisted_intent(
            {
                "workspace_root": str(tmp_path),
                "create_task": lambda **kwargs: created.append(kwargs),
                "complete_memory_extract_intent": completed.append,
            },
            intent,
        )

        assert created == []
        assert completed == ["memory_extract:bad"]
