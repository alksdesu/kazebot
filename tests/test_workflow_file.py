"""config/workflows/<id>.yaml 读不了时要说出来，不当成没有这个 workflow。

文件不在才是没有；在却读不了、解析不了、写的不是一份 workflow，抛 WorkflowUnreadable，原因只有位置和问题。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config_layout  # noqa: E402
from engine.graph import WorkflowUnreadable, allowed_outcomes, load_workflow, next_node  # noqa: E402

WORKFLOW = """\
kind: workflow
name: 审稿
entry_node: writer
nodes:
  writer:
    on:
      done: reviewer
  reviewer:
    on:
      rejected: writer
      default: ""
    handoffs:
      escalate: owner
"""


def _write(root: Path, wid: str, content: str | bytes) -> Path:
    path = config_layout.config_dir(root) / "workflows" / f"{wid}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def test_a_workflow_file_is_read(tmp_path: Path) -> None:
    _write(tmp_path, "review", WORKFLOW)

    workflow = load_workflow(tmp_path, "review")

    assert workflow is not None
    assert (workflow.id, workflow.name, workflow.entry_node) == ("review", "审稿", "writer")
    assert next_node(workflow, "writer", "done") == "reviewer"
    assert next_node(workflow, "reviewer", "rejected") == "writer"
    assert allowed_outcomes(workflow, "reviewer") == ["rejected", "escalate"]


@pytest.mark.parametrize("workflow_id", ["", "   "])
def test_no_id_means_no_workflow(tmp_path: Path, workflow_id: str) -> None:
    assert load_workflow(tmp_path, workflow_id) is None


def test_a_missing_file_means_no_workflow(tmp_path: Path) -> None:
    assert load_workflow(tmp_path, "review") is None


@pytest.mark.parametrize("workflow_id", ["../policy", "a/b", "a\\b", ".hidden", "-x", "x" * 65])
def test_an_id_that_is_not_one_name_is_refused(tmp_path: Path, workflow_id: str) -> None:
    # id 直接拼成文件名：带路径的 id 会读到 workflows 目录外面的文件。
    _write(tmp_path, "policy", WORKFLOW)

    with pytest.raises(ValueError, match="workflow id") as caught:
        load_workflow(tmp_path, workflow_id)

    assert not isinstance(caught.value, WorkflowUnreadable)


@pytest.mark.parametrize("content,reason", [
    pytest.param("kind: workflow\nnodes: [writer\n", "YAML 读不出来：第 3 行第 1 列", id="yaml-error"),
    pytest.param("", "文件是空的", id="empty"),
    pytest.param("- writer\n- reviewer\n", "顶层不是 key: value 的映射", id="list"),
    pytest.param("kind: node\nnodes: {}\n", "kind 写的不是 workflow", id="wrong-kind"),
    pytest.param("kind: workflow\nentry_node: writer\n", "nodes 不是 key: value 的映射", id="no-nodes"),
    pytest.param("kind: workflow\nnodes: [writer]\n", "nodes 不是 key: value 的映射", id="nodes-list"),
    pytest.param(b"kind: workflow\n# \xff\n", "读不了：不是 UTF-8 编码", id="not-utf8"),
])
def test_a_file_that_is_there_but_unusable_is_reported(
    tmp_path: Path, content: str | bytes, reason: str,
) -> None:
    _write(tmp_path, "review", content)

    with pytest.raises(WorkflowUnreadable) as caught:
        load_workflow(tmp_path, "review")

    assert caught.value.rel_path == "config/workflows/review.yaml"
    assert reason in caught.value.reason
    assert str(caught.value).startswith("config/workflows/review.yaml 用不了：")


def test_the_reason_names_the_place_but_not_what_was_written(tmp_path: Path) -> None:
    _write(tmp_path, "review", "kind: workflow\nnodes:\n  writer: {on: [sk-live-0123456789abcdef\n")

    with pytest.raises(WorkflowUnreadable) as caught:
        load_workflow(tmp_path, "review")

    assert "第 " in caught.value.reason
    assert "sk-live-0123456789abcdef" not in str(caught.value)
