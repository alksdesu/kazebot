"""隔离子任务交回的副本说明写成文字：改了哪些文件、diff、补丁在哪、怎么合回或放弃；算不出改动、没有改动、
被取消按放弃处理、本实例不能合回时各有说法，收到结果的节点没有合回工具时不叫它调用。
单个委派、同步批次的恢复消息都把它带上，没有副本时一字不加。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.inference.resume_builder import _build_resume_messages  # noqa: E402
from engine.worktree_report import (  # noqa: E402
    MAX_LISTED_FILES,
    MERGE_TOOL,
    file_line,
    worktree_brief,
    worktree_lines,
)

BASE = "0123456789abcdef0123456789abcdef01234567"
REPO = "E:/work/repo"
PATH = "E:/work/repo-worktrees/repo-1a2b3c4d/task-1"
PATCH = f"{PATH}.patch"
HANDLE = "task-1@agent:a.node:qq_group:1"
DIFF = "diff --git a/app/main.py b/app/main.py\n-    return 1\n+    return 2\n"
HEAD = f"隔离副本 task-1（原仓库 {REPO}，分支 clonoth/task-1，基于提交 0123456789ab）"


def _view(**fields: Any) -> dict[str, Any]:
    view: dict[str, Any] = {
        "id": "task-1", "state": "finished", "repo": REPO, "branch": "clonoth/task-1", "base": BASE,
        "path": PATH, "patch": PATCH, "handle": HANDLE,
        "files": [
            {"path": "app/main.py", "status": "M", "added": 1, "deleted": 1},
            {"path": "app/old.py", "status": "D", "added": 0, "deleted": 1},
            {"path": "assets/logo.bin", "status": "A", "binary": True},
        ],
        "file_count": 3, "added": 1, "deleted": 2, "patch_bytes": 512, "digest": "f" * 64,
        "diff": DIFF, "diff_truncated": False, "merge_allowed": True,
    }
    view.update(fields)
    return view


class TestFileLine:
    def test_status_path_and_line_counts(self) -> None:
        assert file_line({"path": "app/main.py", "status": "M", "added": 1, "deleted": 1}) == "  M app/main.py（修改，+1 -1）"
        assert file_line({"path": "app/old.py", "status": "D", "added": 0, "deleted": 3}) == "  D app/old.py（删除，+0 -3）"
        assert file_line({"path": "logo.bin", "status": "A", "binary": True}) == "  A logo.bin（新增，二进制）"

    def test_missing_pieces_are_described_plainly(self) -> None:
        assert file_line({"path": "run.sh", "status": "T"}) == "  T run.sh（改类型）"
        assert file_line({"path": "x.txt", "status": "X", "added": 2}) == "  X x.txt（X，+2 -0）"
        assert file_line({"path": "x.txt"}) == "  ? x.txt（改动）"
        assert file_line({"path": "x.txt", "added": True, "deleted": "many"}) == "  ? x.txt（改动）"

    def test_anything_without_a_path_writes_nothing(self) -> None:
        for item in ("app/main.py", None, {}, {"status": "M"}, {"path": "  "}):
            assert file_line(item) == "", item


class TestWorktreeLines:
    def test_a_finished_worktree_lists_files_diff_patch_and_how_to_merge(self) -> None:
        lines = worktree_lines(_view())

        assert lines[0] == f"{HEAD}，副本目录 {PATH}。"
        assert lines[1] == "相对基准提交改了 3 个文件（+1 -2，含未提交和未跟踪的文件）："
        assert lines[2:5] == [
            "  M app/main.py（修改，+1 -1）",
            "  D app/old.py（删除，+0 -1）",
            "  A assets/logo.bin（新增，二进制）",
        ]
        assert lines[5:8] == ["diff：", DIFF.rstrip("\n"), f"完整补丁：{PATCH}"]
        assert lines[-1].startswith(f"审过之后要合回原仓库就调用 {MERGE_TOOL}，handle 填 {HANDLE}；")
        assert "abandon=true" in lines[-1] and len(lines) == 9

    def test_a_cut_diff_points_at_the_full_patch(self) -> None:
        lines = worktree_lines(_view(diff_truncated=True))

        assert f"（diff 太长，上面只列了开头；完整补丁在 {PATCH}）" in lines
        assert f"完整补丁：{PATCH}" not in lines

    def test_without_an_inline_diff_the_patch_is_still_named(self) -> None:
        lines = worktree_lines(_view(diff=""))

        assert "diff：" not in lines and f"完整补丁：{PATCH}" in lines

    def test_files_beyond_the_list_are_counted(self) -> None:
        files = [{"path": f"f{index}.txt", "status": "A", "added": 1, "deleted": 0} for index in range(MAX_LISTED_FILES + 5)]

        lines = worktree_lines(_view(files=files, file_count=100, added=100, deleted=0))

        assert sum(1 for line in lines if line.startswith("  A f")) == MAX_LISTED_FILES
        assert f"  …还有 {100 - MAX_LISTED_FILES} 个文件没有列出，见完整补丁。" in lines

    def test_no_changes_means_nothing_to_merge(self) -> None:
        lines = worktree_lines(_view(files=[], file_count=0, added=0, deleted=0, diff=""))

        assert lines == [
            f"{HEAD}，副本目录 {PATH}。",
            "副本里没有改动，不需要合回。",
            f"不要了就调用 {MERGE_TOOL}，handle 填 {HANDLE}，带上 abandon=true 放弃。",
        ]

    def test_an_outcome_that_could_not_be_computed_says_the_merge_recomputes(self) -> None:
        lines = worktree_lines(_view(error="git diff 失败"))

        assert lines == [
            f"{HEAD}：收尾时没算出改动（git diff 失败）。",
            f"副本还在 {PATH}；调用 {MERGE_TOOL} 时会重新算一遍。",
        ]

    def test_a_cancelled_child_is_reported_as_abandoned(self) -> None:
        lines = worktree_lines(_view(state="abandoned", note="子任务被取消"))

        assert lines == [
            f"{HEAD}：已按放弃处理（子任务被取消），会被清理。",
            f"清理前副本里的改动会存成补丁：{PATCH}（没有改动就不留）",
        ]

    def test_an_instance_that_may_not_merge_offers_only_the_patch_and_abandon(self) -> None:
        lines = worktree_lines(_view(merge_allowed=False))

        assert lines[-1] == (
            f"本实例不能合回这个仓库，补丁在 {PATCH}；不要了就调用 {MERGE_TOOL}，handle 填 {HANDLE}，带上 abandon=true 放弃。"
        )

    def test_the_worktree_id_stands_in_for_a_missing_handle(self) -> None:
        view = _view()
        view.pop("handle")

        assert "handle 填 task-1；" in worktree_lines(view)[-1]

    def test_anything_without_a_worktree_id_writes_nothing(self) -> None:
        for view in (None, "task-1", {}, {"id": "  "}, {"files": []}):
            assert worktree_lines(view) == [], view


class TestWithoutTheMergeTool:
    def test_a_finished_worktree_is_handed_up_with_its_handle(self) -> None:
        lines = worktree_lines(_view(), can_merge=False)

        assert lines[:-1] == worktree_lines(_view())[:-1]
        assert lines[-1] == (
            f"这个节点不能调用 {MERGE_TOOL}：审过之后把 handle {HANDLE} 连同你的结论交给上级或用户，由他们合回或放弃；"
            "不要自己把改动手抄进原仓库。副本到了保留期会被清理。"
        )

    def test_no_changes_leave_nothing_to_do(self) -> None:
        lines = worktree_lines(_view(files=[], file_count=0, added=0, deleted=0, diff=""), can_merge=False)

        assert lines == [f"{HEAD}，副本目录 {PATH}。", "副本里没有改动，不需要合回。", "副本到了保留期会被清理。"]

    def test_an_outcome_that_could_not_be_computed_says_who_merges(self) -> None:
        lines = worktree_lines(_view(error="git diff 失败"), can_merge=False)

        assert lines[-1] == f"副本还在 {PATH}；上级或用户调用 {MERGE_TOOL} 时会重新算一遍。"

    def test_an_instance_that_may_not_merge_leaves_only_the_patch(self) -> None:
        lines = worktree_lines(_view(merge_allowed=False), can_merge=False)

        assert lines[-1] == (
            f"本实例不能合回这个仓库，补丁在 {PATCH}；这个节点也不能调用 {MERGE_TOOL}，副本到了保留期会被清理。"
        )

    def test_an_abandoned_worktree_reads_the_same(self) -> None:
        view = _view(state="abandoned", note="子任务被取消")

        assert worktree_lines(view, can_merge=False) == worktree_lines(view)

    @pytest.mark.parametrize("fields", [
        pytest.param({}, id="finished"),
        pytest.param({"files": [], "file_count": 0, "added": 0, "deleted": 0, "diff": ""}, id="no-changes"),
        pytest.param({"error": "git diff 失败"}, id="not-computed"),
        pytest.param({"merge_allowed": False}, id="instance-may-not-merge"),
    ])
    def test_nothing_tells_the_node_to_call_the_tool_itself(self, fields: dict[str, Any]) -> None:
        text = "\n".join(worktree_lines(_view(**fields), can_merge=False))

        assert "abandon=true" not in text
        assert f"就调用 {MERGE_TOOL}" not in text and "就同样调用它" not in text


class TestBrief:
    def test_the_brief_keeps_the_facts_and_drops_diff_and_file_list(self) -> None:
        assert worktree_brief(_view()) == {
            "id": "task-1", "state": "finished", "repo": REPO, "branch": "clonoth/task-1", "base": BASE,
            "path": PATH, "patch": PATCH, "file_count": 3, "added": 1, "deleted": 2,
        }
        assert worktree_brief(_view(error="git diff 失败", file_count=0))["error"] == "git diff 失败"

    def test_no_worktree_id_means_no_brief(self) -> None:
        assert worktree_brief(None) == {} and worktree_brief({"id": ""}) == {} and worktree_brief("task-1") == {}


class TestResumeMessages:
    def test_a_single_child_result_carries_its_worktree_after_the_handle(self) -> None:
        [message] = _build_resume_messages({
            "type": "child_result", "from_node": "a.node", "result": {"text": "done"}, "handle": HANDLE,
            "worktree": _view(),
        })

        content = message["content"]
        assert content.startswith("下游节点 a.node 已完成。\n结果：\ndone\n")
        assert content.index(f"句柄：{HANDLE}") < content.index(HEAD)
        assert "  M app/main.py（修改，+1 -1）" in content
        assert content.endswith("合回前副本一直留着，到了保留期会被清理。")
        assert message["_meta"]["resume_type"] == "child_result"

    def test_a_failed_child_still_hands_back_its_worktree(self) -> None:
        [message] = _build_resume_messages({
            "type": "child_failed", "from_node": "a.node", "error": "ran out of steps", "worktree": _view(),
        })

        lines = message["content"].split("\n")
        assert lines[0] == "下游节点 a.node 执行失败：ran out of steps"
        assert lines[1] == f"{HEAD}，副本目录 {PATH}。"
        assert message["_meta"]["resume_type"] == "child_failed"

    def test_ask_cancel_and_preempt_carry_it_too(self) -> None:
        for resume_type in ("child_ask", "child_cancelled", "child_preempted"):
            [message] = _build_resume_messages({
                "type": resume_type, "from_node": "a.node", "result": {"text": "which file?"}, "worktree": _view(),
            })
            assert HEAD in message["content"], resume_type

    def test_without_a_worktree_nothing_is_added(self) -> None:
        [failed] = _build_resume_messages({"type": "child_failed", "from_node": "a.node", "error": "x"})
        [cancelled] = _build_resume_messages({"type": "child_cancelled", "from_node": "a.node"})

        assert failed["content"] == "下游节点 a.node 执行失败：x"
        assert cancelled["content"] == "下游节点 a.node 已被取消。"

    def test_a_batch_entry_carries_its_worktree(self) -> None:
        [message] = _build_resume_messages({"type": "batch_results", "entries": [{
            "kind": "node", "node_id": "a.node", "status": "finish", "text": "done", "call_id": "w1",
            "handle": HANDLE, "child_task_id": "task-1", "worktree": _view(),
        }]})

        content = message["content"]
        assert content.startswith("子节点 a.node（调用 w1）已完成。\n结果：\ndone\n")
        assert content.index(f"句柄：{HANDLE}") < content.index(HEAD)
        assert message["_meta"]["handle"] == HANDLE and message["_meta"]["child_task_id"] == "task-1"

    def test_a_batch_entry_without_a_worktree_is_unchanged(self) -> None:
        [message] = _build_resume_messages({"type": "batch_results", "entries": [{
            "kind": "node", "node_id": "a.node", "status": "fail", "error": "boom", "call_id": "w1",
        }]})

        assert message["content"] == "子节点 a.node（调用 w1）执行失败：boom\n它没有给出结果，不要臆测它的输出。"

    @pytest.mark.parametrize("resume_type", ["child_result", "child_ask", "child_failed", "child_cancelled", "child_preempted"])
    def test_a_single_result_follows_whether_the_node_can_merge(self, resume_type: str) -> None:
        resume = {"type": resume_type, "from_node": "a.node", "result": {"text": "done"}, "worktree": _view()}

        [handed_up] = _build_resume_messages(resume, can_merge=False)
        [merged] = _build_resume_messages(resume)

        assert f"这个节点不能调用 {MERGE_TOOL}：审过之后把 handle {HANDLE}" in handed_up["content"]
        assert "abandon=true" not in handed_up["content"]
        assert f"审过之后要合回原仓库就调用 {MERGE_TOOL}" in merged["content"]

    def test_a_batch_entry_follows_it_too(self) -> None:
        resume = {"type": "batch_results", "entries": [{
            "kind": "node", "node_id": "a.node", "status": "finish", "text": "done", "call_id": "w1",
            "handle": HANDLE, "worktree": _view(),
        }]}

        [handed_up] = _build_resume_messages(resume, can_merge=False)
        [merged] = _build_resume_messages(resume)

        assert f"这个节点不能调用 {MERGE_TOOL}" in handed_up["content"] and "abandon=true" not in handed_up["content"]
        assert "abandon=true" in merged["content"]
