"""save_memory 的 source 归属标记回归测试。

新建条目从不写 source，于是 dream 的 Prune（只清 source=auto）和 Promote
永远匹配不到条目，而"手工记忆禁删"那条保护也无从判断谁是手工的。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

from engine.builtin.knowledge_inject import _conversation_memory_namespace, save_memory
from toolbox.context import ToolContext

_CONV_KEY = "qq_group:bc3f12b0621298e191a49fa7"


def _ctx(tmp_path: Path, node_id: str) -> ToolContext:
    return ToolContext(
        supervisor_url="http://localhost:0",
        session_id="sess-1",
        run_id="run-1",
        worker_id="worker-1",
        workspace_root=tmp_path,
        http=None,  # type: ignore[arg-type]
        registry=None,
        conversation_key=_CONV_KEY,
        node_id=node_id,
    )


def _save(tmp_path: Path, node_id: str, **args: Any) -> dict[str, Any]:
    payload = {"id": "m1", "book": "habits", "content": "内容", "keywords": ["k"], **args}
    result = asyncio.run(save_memory(payload, _ctx(tmp_path, node_id)))
    # save_memory 会静默拒收非法 book / id，不断言就会把"没写盘"读成断言失败。
    assert result["ok"], result
    return result


def _entries(tmp_path: Path, book: str = "habits") -> list[dict[str, Any]]:
    path = tmp_path / "data" / "memory" / _conversation_memory_namespace(_CONV_KEY) / f"{book}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))["entries"]


@pytest.mark.parametrize("node_id", ["system.memory_extractor", "system.dream"])
def test_system_nodes_write_auto(tmp_path: Path, node_id: str) -> None:
    _save(tmp_path, node_id)

    assert _entries(tmp_path)[0]["source"] == "auto"


@pytest.mark.parametrize("node_id", ["qq.orchestrator", "bootstrap.shell_orchestrator", "save_memory", ""])
def test_everything_else_writes_manual(tmp_path: Path, node_id: str) -> None:
    _save(tmp_path, node_id)

    assert _entries(tmp_path)[0]["source"] == "manual"


def test_source_is_never_left_empty(tmp_path: Path) -> None:
    # 空 source 会让 dream 的 Prune / Promote 两条规则一个条目都匹配不到。
    _save(tmp_path, "qq.orchestrator")

    assert _entries(tmp_path)[0]["source"]


def test_updating_keeps_the_original_provenance(tmp_path: Path) -> None:
    # 手工记忆被系统节点改写一次就不该变成 auto，否则下一轮 dream 可以删它。
    _save(tmp_path, "qq.orchestrator")

    _save(tmp_path, "system.dream", content="改过的内容")

    entry = _entries(tmp_path)[0]
    assert entry["source"] == "manual"
    assert entry["content"] == "改过的内容"


def test_legacy_entries_without_source_get_one_on_next_write(tmp_path: Path) -> None:
    book_dir = tmp_path / "data" / "memory" / _conversation_memory_namespace(_CONV_KEY)
    book_dir.mkdir(parents=True, exist_ok=True)
    (book_dir / "habits.yaml").write_text(
        yaml.safe_dump({"book": "habits", "entries": [{"id": "m1", "content": "旧的", "keywords": []}]}),
        encoding="utf-8",
    )

    _save(tmp_path, "qq.orchestrator", content="新的")

    assert _entries(tmp_path)[0]["source"] == "manual"


def test_the_entry_lands_in_the_conversation_namespace(tmp_path: Path) -> None:
    _save(tmp_path, "qq.orchestrator")

    assert not (tmp_path / "data" / "memory" / "habits.yaml").exists()
    assert len(_entries(tmp_path)) == 1
