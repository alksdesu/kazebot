"""主入口提示词拆成共享规则和平台片段：共享那份换个平台照样成立，平台写法只在各自的片段里。"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _node_prompts import assembled_prompt  # noqa: E402

_NODES = _ROOT / "defaults" / "nodes"
_SHARED = "_orchestrator.md"
_QQ = "_platform_qq.md"
_ENTRIES = ("qq.orchestrator", "qq.orchestrator.example")
_PSEUDO_TOOLS = frozenset({"finish", "ask", "dispatch_node", "switch_node"})
# 共享规则教模型去调的工具，两个入口的白名单都得有。只被提到的（read_image）不算。
_TAUGHT_TOOLS = frozenset({
    "web_search", "read_web", "stocktool_quote", "stocktool_history",
    "create_schedule", "list_schedules", "delete_schedule", "request_restart", "cancel_active_tasks",
    "save_memory", "list_memories", "delete_memory",
    "draft_execution_plan", "create_reminder", "community",
    "materials_document", "materials_chat_search", "materials_structure", "materials_generate",
    "materials_image_versions",
})


def _fragment(name: str) -> str:
    return (_NODES / name).read_text(encoding="utf-8")


def _allowlist(node_id: str) -> set[str]:
    data = yaml.safe_load((_NODES / f"{node_id}.yaml").read_text(encoding="utf-8")) or {}
    return set((data.get("tool_access") or {}).get("allow") or ())


class TestSharedRules:
    @pytest.mark.parametrize("word", ["qq", "napcat", "onebot", "[at:", "[cq:", "群号"])
    def test_no_platform_syntax_leaks_into_the_shared_rules(self, word: str) -> None:
        assert word not in _fragment(_SHARED).lower()

    def test_emoji_and_reactions_are_read_off_the_platform_guide_not_its_titles(self) -> None:
        shared = _fragment(_SHARED)

        # 段落标题各平台不同，认输出说明的包裹标记才换得了平台。
        assert "[OUTPUT_GUIDE:CONSTANT]" in shared and "[OUTPUT_GUIDE:ACTIVE]" in shared
        assert re.findall(r"【[^】]*(?:表情|表态)[^】]*】", shared) == []

    def test_every_ability_the_rules_list_is_a_tool_they_teach(self) -> None:
        abilities = _fragment(_SHARED).split("## 可用能力", 1)[1].split("\n## ", 1)[0]
        listed = {
            name.strip() for group in re.findall(r"\*\*([a-z_ /]+)\*\*", abilities) for name in group.split("/")
        }

        # 新加一条能力却没进 _TAUGHT_TOOLS，示例入口的白名单就没人盯了。
        assert sorted(listed - _PSEUDO_TOOLS - _TAUGHT_TOOLS) == []

    def test_every_taught_tool_is_still_in_the_rules(self) -> None:
        shared = _fragment(_SHARED)

        assert sorted(tool for tool in _TAUGHT_TOOLS if tool not in shared) == []


class TestQQFragment:
    @pytest.mark.parametrize("marker", ["qq_forward", "[at:", "[at:all]", "[表情包:", "[QQ表情:"])
    def test_the_qq_only_syntax_lives_here(self, marker: str) -> None:
        assert marker in _fragment(_QQ)

    def test_forwarding_is_taught_once(self) -> None:
        qq = _fragment(_QQ)

        assert qq.count("**qq_forward**") == 1
        assert qq.count("使用 qq_forward") == 1


class TestEntryNodes:
    @pytest.mark.parametrize("node_id", _ENTRIES)
    def test_both_entries_assemble_the_shared_rules_then_the_qq_fragment(self, node_id: str) -> None:
        prompt = assembled_prompt(node_id)

        assert "{{include:" not in prompt
        assert _fragment(_SHARED).strip() in prompt and _fragment(_QQ).strip() in prompt
        assert prompt.index(_fragment(_SHARED).strip()) < prompt.index(_fragment(_QQ).strip())

    @pytest.mark.parametrize("node_id", _ENTRIES)
    def test_every_tool_the_shared_rules_teach_is_granted(self, node_id: str) -> None:
        assert sorted(_TAUGHT_TOOLS - _allowlist(node_id)) == []

    def test_nodes_that_include_the_qq_fragment_are_granted_qq_forward(self) -> None:
        directive = f"{{{{include:{_QQ}}}}}"
        including = [path.stem for path in sorted(_NODES.glob("*.yaml")) if directive in path.read_text(encoding="utf-8")]

        assert set(_ENTRIES) <= set(including)
        assert [node_id for node_id in including if "qq_forward" not in _allowlist(node_id)] == []
