"""分支 transcript 并回父会话：父文件上一次停在半行时，并进去的记录从新的一行开始，不和那半行粘在一起。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from supervisor.task_router import BranchFinalizePlan, TaskRouterMixin  # noqa: E402


def _transcripts(ws: Path) -> Path:
    path = ws / "data" / "transcripts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _torn_parent(ws: Path) -> Path:
    parent = _transcripts(ws) / "P.jsonl"
    parent.write_bytes(json.dumps({"task_id": "before", "session_id": "P"}).encode("utf-8") + b'\n{"task_id": "torn')
    return parent


def _branch(ws: Path, session_id: str) -> None:
    path = _transcripts(ws) / f"{session_id}.jsonl"
    path.write_text(json.dumps({"task_id": "t", "session_id": session_id}) + "\n", encoding="utf-8")


def _readable(path: Path) -> list[tuple[Any, Any]]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        records.append((record["task_id"], record["session_id"]))
    return records


class TestATornParentTranscript:
    def test_finalize_starts_the_merged_records_on_a_fresh_line(self, tmp_path: Path) -> None:
        parent = _torn_parent(tmp_path)
        _branch(tmp_path, "branch_1")
        router = SimpleNamespace(
            workspace_root=tmp_path, tasks={},
            _session_store=SimpleNamespace(get_entry=lambda _session_id: None),
            purge_session_files=lambda _session_id: None,
        )
        plan = BranchFinalizePlan(
            identity="i", task_id="t", parent_session_id="P", branch_session_id="branch_1",
            base_count=0, merge=True, cleanup_session_ids=(),
        )

        TaskRouterMixin._execute_branch_finalize_io_body(router, plan)

        assert _readable(parent) == [("before", "P"), ("t", "P")]

    def test_the_completion_merge_starts_on_a_fresh_line(self, tmp_path: Path) -> None:
        parent = _torn_parent(tmp_path)
        _branch(tmp_path, "branch_2")

        merged = TaskRouterMixin._merge_branch_transcript_locked(SimpleNamespace(workspace_root=tmp_path), "P", "branch_2")

        assert merged == 1
        assert _readable(parent) == [("before", "P"), ("t", "P")]
        assert not (_transcripts(tmp_path) / "branch_2.jsonl").exists()
