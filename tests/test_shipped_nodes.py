"""仓库自带节点的分工：工程活归编程助手，并进去的节点的旧 id 落到它身上；入口列表只列 QQ 入口、网页入口和编程助手。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
for _path in (_ROOT, _TESTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from _node_prompts import assembled_prompt  # noqa: E402
from engine.node import canonical_node_id, entry_node_problem, load_node, node_file  # noqa: E402
from engine.runner import _discover_switchable_nodes  # noqa: E402
from shell.tui.services.node_scanner import find_root_nodes, scan_nodes  # noqa: E402

_CODER = "bootstrap.coder"
_ENTRIES = ("qq.orchestrator", "bootstrap.shell_orchestrator")
# web_search 自己会退到这三个搜索后端；生图要走绘图节点的规划模型和收尾检查
_DIRECT_ONLY_VIA_OTHERS = frozenset({
    "native_search", "exa_search", "x_search",
    "gpt_image_2", "gemini_image", "draw_context", "nai_generate", "nai_generate_from_plan", "nai_cleanup",
})
_FILE_AND_COMMAND_TOOLS = frozenset({
    "read_file", "write_file", "apply_diff", "list_dir", "search_in_files",
    "execute_command", "remote_exec", "manage_secret",
})


def _entry(workspace: Path, node_id: str):
    node = load_node(workspace, node_id)
    assert node is not None, node_id
    return node


class TestMergedNodes:
    @pytest.mark.parametrize("old_id", ["bootstrap.executor", "bootstrap.cmd_reviewer"])
    def test_the_old_ids_reach_the_coder(self, tmp_path: Path, old_id: str) -> None:
        assert canonical_node_id(tmp_path, old_id) == _CODER
        assert _entry(tmp_path, old_id).id == _CODER

    @pytest.mark.parametrize("old_id", ["chat.web_search", "qq.web_search"])
    def test_the_narrow_search_entry_is_gone_instead_of_pointing_at_a_wider_node(self, tmp_path: Path, old_id: str) -> None:
        # 还拿它当入口的部署报「节点未找到」，不会悄悄换成工具更多的节点
        assert node_file(tmp_path, old_id) is None


class TestEntries:
    @pytest.mark.parametrize("node_id", _ENTRIES)
    def test_engineering_work_goes_to_the_coder_and_drawing_to_the_draw_nodes(self, tmp_path: Path, node_id: str) -> None:
        assert _entry(tmp_path, node_id).delegate_targets == [_CODER, "draw.novelai_planner", "draw.image_gen"]
        assert _CODER in assembled_prompt(node_id)

    def test_only_the_web_and_terminal_entry_may_merge_the_coders_worktree(self, tmp_path: Path) -> None:
        qq, shell = (_entry(tmp_path, node_id) for node_id in _ENTRIES)

        assert "merge_worktree" in qq.tool_access.deny
        assert "merge_worktree" not in shell.tool_access.deny

    @pytest.mark.parametrize("node_id", _ENTRIES)
    def test_entries_search_and_draw_through_one_way_each(self, tmp_path: Path, node_id: str) -> None:
        allow = set(_entry(tmp_path, node_id).tool_access.allow)

        assert allow.isdisjoint(_DIRECT_ONLY_VIA_OTHERS)
        assert "web_search" in allow

    def test_the_two_entries_grant_the_same_tools_except_forwarding(self, tmp_path: Path) -> None:
        qq, shell = (set(_entry(tmp_path, node_id).tool_access.allow) for node_id in _ENTRIES)

        assert qq - shell == {"qq_forward"} and shell <= qq

    def test_the_narrow_example_has_no_file_or_command_tools_and_no_coder(self) -> None:
        data = yaml.safe_load((_ROOT / "defaults" / "nodes" / "qq.orchestrator.example.yaml").read_text(encoding="utf-8"))

        assert set(data["tool_access"]["allow"]).isdisjoint(_FILE_AND_COMMAND_TOOLS)
        assert data["delegate_targets"] == ["draw.novelai_planner", "draw.image_gen"]


class TestEntryList:
    def test_the_entry_list_is_the_two_entries_and_the_coder(self, tmp_path: Path) -> None:
        expected = [_CODER, "bootstrap.shell_orchestrator", "qq.orchestrator"]

        assert [row["id"] for row in _discover_switchable_nodes(tmp_path, "")] == expected
        assert [node.id for node in find_root_nodes(scan_nodes(tmp_path))] == expected

    def test_the_vision_node_is_left_out_but_still_takes_routed_conversations(self, tmp_path: Path) -> None:
        vision = _entry(tmp_path, "chat.vision")

        assert vision.selectable is False and entry_node_problem(vision) == ""
