"""spec 带 requires_edit_mode 的工具只给 edit_mode 相同的节点：列表过滤、单名校验和推理循环里的授权是同一套规则。"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _engine_loop_fixture import NodeRun, ScriptedRegistry, call, echo, turn  # noqa: E402
from engine.inference.pseudo_tools import _filter_tool_specs, edit_mode_matches, tool_allowed  # noqa: E402
from engine.node import Node, ToolAccess  # noqa: E402

HASHLINE = {"name": "edit_lines", "description": "edit by anchors", "requires_edit_mode": "hashline"}
PLAIN = {"name": "read_file", "description": "read a file"}


def _node(edit_mode: Any = None, access: ToolAccess | None = None) -> Node:
    extra = {} if edit_mode is None else {"edit_mode": edit_mode}
    return Node(id="node-1", type="ai", tool_access=access or ToolAccess(mode="all"), extra=extra)


def _names(node: Node, specs: list[dict[str, Any]]) -> list[str]:
    return [spec["name"] for spec in _filter_tool_specs(node, specs)]


class TestListFilter:
    @pytest.mark.parametrize("edit_mode", [None, "", "diff", "hash"])
    def test_the_tool_is_hidden_unless_the_mode_matches(self, edit_mode: Any) -> None:
        assert _names(_node(edit_mode), [HASHLINE, PLAIN]) == ["read_file"]

    @pytest.mark.parametrize("edit_mode", ["hashline", " HashLine ", "HASHLINE"])
    def test_a_matching_mode_shows_it(self, edit_mode: str) -> None:
        assert _names(_node(edit_mode), [HASHLINE, PLAIN]) == ["edit_lines", "read_file"]

    @pytest.mark.parametrize("required", [None, "", "   "])
    def test_an_empty_requirement_means_no_restriction(self, required: Any) -> None:
        spec = {**HASHLINE, "requires_edit_mode": required}

        assert _names(_node(), [spec]) == ["edit_lines"]

    def test_tool_access_still_applies_on_top(self) -> None:
        only_read = ToolAccess(mode="allowlist", allow=["read_file"])
        both = ToolAccess(mode="allowlist", allow=["read_file", "edit_lines"])
        denied = ToolAccess(mode="all", deny=["edit_lines"])

        assert _names(_node("hashline", only_read), [HASHLINE, PLAIN]) == ["read_file"]
        assert _names(_node("hashline", both), [HASHLINE, PLAIN]) == ["edit_lines", "read_file"]
        assert _names(_node("hashline", denied), [HASHLINE, PLAIN]) == ["read_file"]
        assert _names(_node("hashline", ToolAccess(mode="none")), [HASHLINE, PLAIN]) == []


class TestSingleNameCheck:
    @pytest.mark.parametrize("edit_mode", [None, "diff", "hashline"])
    @pytest.mark.parametrize("access", [ToolAccess(mode="all"), ToolAccess(mode="allowlist", allow=["edit_lines"])])
    def test_it_agrees_with_the_list_filter(self, edit_mode: Any, access: ToolAccess) -> None:
        node = _node(edit_mode, access)
        listed = set(_names(node, [HASHLINE, PLAIN]))

        for spec in (HASHLINE, PLAIN):
            assert tool_allowed(node, spec["name"], spec) is (spec["name"] in listed)

    def test_without_a_spec_only_tool_access_counts(self) -> None:
        assert tool_allowed(_node(), "edit_lines") is True
        assert tool_allowed(_node(access=ToolAccess(mode="none")), "edit_lines") is False

    def test_a_node_without_usable_extra_does_not_get_gated_tools(self) -> None:
        assert edit_mode_matches(SimpleNamespace(extra=None), HASHLINE) is False
        assert edit_mode_matches(SimpleNamespace(extra=None), PLAIN) is True
        assert edit_mode_matches(_node("hashline"), None) is True


def _registry() -> ScriptedRegistry:
    return ScriptedRegistry(
        tools={"edit_lines": echo("edited"), "probe": echo("probed")},
        specs={"edit_lines": {"requires_edit_mode": "hashline"}},
    )


def _run(monkeypatch: pytest.MonkeyPatch, workspace: Path, extra: dict[str, Any]) -> NodeRun:
    node = Node(
        id="node-1", type="ai", prompt="Work.", tool_mode="native",
        tool_access=ToolAccess(mode="all"), extra=extra,
    )
    run = NodeRun(monkeypatch, workspace, registry=_registry(), node=node, runtime_cfg={"engine": {"max_steps": 4}})
    run.run(turn(call("edit_lines", {}, call_id="e")), turn(call("finish", {"text": "ok"}, call_id="f")))
    return run


class TestInTheLoop:
    def test_a_node_without_hashline_neither_sees_nor_runs_the_tool(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run = _run(monkeypatch, tmp_path, {})

        assert "edit_lines" not in run.tool_names(0) and "probe" in run.tool_names(0)
        assert run.registry.executed_names() == []
        refusal = [message for message in run.requests[1]["messages"] if message.get("tool_call_id") == "e"]
        assert "not in this node's allowed tool list" in refusal[0]["content"]

    def test_a_hashline_node_sees_and_runs_it(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        run = _run(monkeypatch, tmp_path, {"edit_mode": "hashline"})

        assert "edit_lines" in run.tool_names(0)
        assert run.registry.executed_names() == ["edit_lines"]
