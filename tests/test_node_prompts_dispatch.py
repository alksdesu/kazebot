"""节点提示词讲的委派方式和真实工具一致：每个目标一个 dispatch_to_<目标> 工具，要结果时设 wait；不再提已删掉的 dispatch_node 和「enum 里的 target」。"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _node_prompts import assembled_prompt  # noqa: E402
from engine.inference.pseudo_tools import _ask_spec, _dispatch_tool_name, _finish_spec  # noqa: E402

_NODES = _ROOT / "defaults" / "nodes"
# 用户自己在改的共享片段，不归这里管
_EXCLUDED = frozenset({"_shared.md"})
_DELEGATING = ("bootstrap.executor", "bootstrap.shell_orchestrator", "qq.orchestrator", "qq.orchestrator.example")


def _prompt_files() -> list[Path]:
    return sorted(
        path for path in _NODES.iterdir()
        if path.suffix in {".md", ".yaml"} and path.name not in _EXCLUDED
    )


@pytest.mark.parametrize("path", _prompt_files(), ids=lambda path: path.name)
def test_no_prompt_teaches_the_removed_dispatch_node_tool(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    assert re.search(r"\bdispatch_node\b", text) is None
    assert re.search(r"target\s*enum|enum\s*中|enum\s*为准", text) is None


def test_the_scan_covers_the_prompts_that_used_to_teach_it() -> None:
    names = {path.name for path in _prompt_files()}

    assert {"bootstrap.executor.yaml", "bootstrap.shell_orchestrator.yaml", "_orchestrator.md"} <= names
    assert names.isdisjoint(_EXCLUDED)


def test_the_executor_sends_commands_to_the_reviewer_and_waits() -> None:
    prompt = assembled_prompt("bootstrap.executor")
    reviewer_tool = _dispatch_tool_name("bootstrap.cmd_reviewer")

    assert reviewer_tool == "dispatch_to_bootstrap_cmd_reviewer"
    assert reviewer_tool in prompt
    assert "wait: true" in prompt


@pytest.mark.parametrize("node_id", _DELEGATING)
def test_every_dispatch_tool_a_prompt_names_is_one_the_node_really_gets(node_id: str) -> None:
    data = yaml.safe_load((_NODES / f"{node_id}.yaml").read_text(encoding="utf-8")) or {}
    offered = {_dispatch_tool_name(target) for target in data.get("delegate_targets") or []}

    named = set(re.findall(r"dispatch_to_[A-Za-z0-9_]+", assembled_prompt(node_id)))

    assert named, f"{node_id} 的提示词没有讲委派工具叫什么"
    assert named <= offered


def test_the_shared_orchestrator_rules_teach_when_to_wait() -> None:
    rules = (_NODES / "_orchestrator.md").read_text(encoding="utf-8")
    delegation = rules.split("## 委派规则", 1)[1].split("\n## ", 1)[0]

    assert "wait: true" in delegation
    assert "[Async task completed]" in delegation


def test_agents_md_documents_wait() -> None:
    path = _ROOT / "AGENTS.md"
    if not path.exists():
        pytest.skip("AGENTS.md 是本地文件，这份检出里没有")
    section = path.read_text(encoding="utf-8").split("### Dispatch Modes", 1)[1].split("\n## ", 1)[0]

    assert "dispatch_to_<target>" in section
    assert "wait: true" in section and "wait: false" in section


def test_finish_and_ask_describe_the_same_round_rule_the_same_way() -> None:
    finish = _finish_spec()["function"]["description"]
    ask = _ask_spec()["function"]["description"]

    assert "those other tools still run but finish is REJECTED" in finish
    assert "those other tools still run but ask is ignored" in ask
    assert "ALL calls will be REJECTED" not in ask
