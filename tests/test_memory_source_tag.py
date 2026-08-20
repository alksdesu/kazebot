"""save_memory / delete_memory 的 source 归属与写权限。

source 曾按节点名前缀判定，qq.orchestrator 写的全被打成 manual —— bot 自己记的
东西自己删不掉、dream 也清不掉，只能新增一条「上面那条错了」打补丁。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

from engine.builtin.knowledge_inject import (
    _conversation_memory_namespace,
    delete_memory,
    save_memory,
)
from toolbox.context import ToolContext

_CONV_KEY = "qq_group:bc3f12b0621298e191a49fa7"
_TOOL_NODES = ["system.memory_extractor", "system.dream", "qq.orchestrator",
               "bootstrap.shell_orchestrator", ""]


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


def _try_save(tmp_path: Path, node_id: str, **args: Any) -> dict[str, Any]:
    payload = {"id": "m1", "book": "habits", "content": "内容", "keywords": ["k"], **args}
    return asyncio.run(save_memory(payload, _ctx(tmp_path, node_id)))


def _save(tmp_path: Path, node_id: str, **args: Any) -> dict[str, Any]:
    result = _try_save(tmp_path, node_id, **args)
    # save_memory 会静默拒收非法 book / id，不断言就会把"没写盘"读成断言失败。
    assert result["ok"], result
    return result


def _delete(tmp_path: Path, node_id: str, mid: str = "m1") -> dict[str, Any]:
    return asyncio.run(delete_memory({"id": mid, "book": "habits"}, _ctx(tmp_path, node_id)))


def _book_path(tmp_path: Path, book: str = "habits") -> Path:
    return tmp_path / "data" / "memory" / _conversation_memory_namespace(_CONV_KEY) / f"{book}.yaml"


def _entries(tmp_path: Path, book: str = "habits") -> list[dict[str, Any]]:
    return yaml.safe_load(_book_path(tmp_path, book).read_text(encoding="utf-8"))["entries"]


def _seed(tmp_path: Path, **fields: Any) -> None:
    """直接落一条盘上的记忆，用来模拟控制台写入或历史遗留数据。"""
    path = _book_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"id": "m1", "content": "原文", "keywords": ["k"], **fields}
    path.write_text(yaml.safe_dump({"book": "habits", "entries": [entry]}), encoding="utf-8")


class TestProvenance:
    @pytest.mark.parametrize("node_id", _TOOL_NODES)
    def test_every_node_writes_auto(self, tmp_path: Path, node_id: str) -> None:
        # 工具只有模型调得到，人走的是控制台那条路。按节点名分家没有依据。
        _save(tmp_path, node_id)

        assert _entries(tmp_path)[0]["source"] == "auto"

    def test_source_is_never_left_empty(self, tmp_path: Path) -> None:
        # 空 source 会让 dream 的 Prune / Promote 两条规则一个条目都匹配不到。
        _save(tmp_path, "qq.orchestrator")

        assert _entries(tmp_path)[0]["source"]

    def test_the_entry_lands_in_the_conversation_namespace(self, tmp_path: Path) -> None:
        _save(tmp_path, "qq.orchestrator")

        assert not (tmp_path / "data" / "memory" / "habits.yaml").exists()
        assert len(_entries(tmp_path)) == 1


class TestToolCanRewriteItsOwn:
    """bot 记错了要能自己改回来，而不是新增一条「上面那条错了」。"""

    def test_it_rewrites_what_it_wrote(self, tmp_path: Path) -> None:
        _save(tmp_path, "qq.orchestrator")

        _save(tmp_path, "qq.orchestrator", content="改过的内容")

        assert _entries(tmp_path)[0]["content"] == "改过的内容"

    def test_it_deletes_what_it_wrote(self, tmp_path: Path) -> None:
        _save(tmp_path, "qq.orchestrator")

        assert _delete(tmp_path, "qq.orchestrator")["ok"]

    def test_dream_may_tidy_a_chat_node_entry(self, tmp_path: Path) -> None:
        # 两边写的都是 auto，dream 才够得着群里刚记下的东西。
        _save(tmp_path, "qq.orchestrator")

        assert _delete(tmp_path, "system.dream")["ok"]

    def test_fields_left_out_of_the_call_survive(self, tmp_path: Path) -> None:
        _save(tmp_path, "qq.orchestrator", keywords=["昵称", "称呼"])

        # 绕开 _try_save 的默认 payload：keywords 得真的不在 args 里才测得到继承。
        asyncio.run(save_memory(
            {"id": "m1", "book": "habits", "content": "只改正文"},
            _ctx(tmp_path, "qq.orchestrator"),
        ))

        assert _entries(tmp_path)[0]["keywords"] == ["昵称", "称呼"]

    def test_the_rewrite_stays_auto(self, tmp_path: Path) -> None:
        _save(tmp_path, "qq.orchestrator")
        _save(tmp_path, "system.dream", content="dream 整理过")

        assert _entries(tmp_path)[0]["source"] == "auto"


class TestManualIsReadOnly:
    """控制台写的就是最高权威：工具改不了，也删不了。"""

    def test_it_cannot_be_rewritten(self, tmp_path: Path) -> None:
        _seed(tmp_path, source="manual")

        assert not _try_save(tmp_path, "qq.orchestrator", content="覆盖掉")["ok"]

    def test_it_cannot_be_deleted(self, tmp_path: Path) -> None:
        _seed(tmp_path, source="manual")

        assert not _delete(tmp_path, "qq.orchestrator")["ok"]

    def test_it_cannot_be_disabled_either(self, tmp_path: Path) -> None:
        # enabled=false 的条目注入侧直接跳过，能关就等于能删，守卫不对称等于没有。
        _seed(tmp_path, source="manual")

        assert not _try_save(tmp_path, "qq.orchestrator", enabled=False)["ok"]

    def test_a_refused_write_leaves_the_entry_intact(self, tmp_path: Path) -> None:
        _seed(tmp_path, source="manual")
        _try_save(tmp_path, "qq.orchestrator", content="覆盖掉", keywords=[])

        entry = _entries(tmp_path)[0]
        assert entry["content"] == "原文"
        assert entry["keywords"] == ["k"]

    def test_a_legacy_entry_without_source_counts_as_manual(self, tmp_path: Path) -> None:
        # 来源不明的老数据保守当手工，跟 delete 侧一直以来的判据保持一致。
        _seed(tmp_path)

        assert not _try_save(tmp_path, "qq.orchestrator", content="新的")["ok"]
        assert not _delete(tmp_path, "qq.orchestrator")["ok"]

    def test_a_fresh_id_is_still_writable(self, tmp_path: Path) -> None:
        # 只读的是那一条，不是整本书。
        _seed(tmp_path, source="manual")

        assert _try_save(tmp_path, "qq.orchestrator", id="m2", content="另一条")["ok"]


class TestConstantCannotBeUnset:
    """常驻标记卸得掉，就能先解锁再 delete，绕开那边的 constant 守卫。"""

    def test_clearing_it_is_refused(self, tmp_path: Path) -> None:
        _save(tmp_path, "qq.orchestrator", constant=True)

        assert not _try_save(tmp_path, "qq.orchestrator", constant=False)["ok"]

    def test_the_flag_survives_the_refusal(self, tmp_path: Path) -> None:
        _save(tmp_path, "qq.orchestrator", constant=True)
        _try_save(tmp_path, "qq.orchestrator", constant=False)

        assert _entries(tmp_path)[0]["constant"] is True

    def test_a_constant_entry_is_still_editable_otherwise(self, tmp_path: Path) -> None:
        _save(tmp_path, "qq.orchestrator", constant=True)

        assert _try_save(tmp_path, "qq.orchestrator", content="改正文")["ok"]

    def test_setting_it_is_allowed(self, tmp_path: Path) -> None:
        _save(tmp_path, "qq.orchestrator")

        assert _try_save(tmp_path, "qq.orchestrator", constant=True)["ok"]

    def test_deleting_a_constant_entry_is_still_refused(self, tmp_path: Path) -> None:
        _save(tmp_path, "qq.orchestrator", constant=True)

        assert not _delete(tmp_path, "qq.orchestrator")["ok"]
