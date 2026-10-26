"""request_restart 重启前只做一次 git 提交，main.py 和 supervisor 里都没有回退逻辑。

发给用户的那句话要说清这个提交拿来手动回退，不能承诺自动回滚。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from toolbox.builtins import system as system_tool  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402

_NEW_HEAD = "0123456789abcdef0123456789abcdef01234567"
_OLD_HEAD = "fedcba9876543210fedcba9876543210fedcba98"
_COMMITTED = {"ok": True, "committed": True, "before_head": _OLD_HEAD, "after_head": _NEW_HEAD}
_CLEAN = {"ok": True, "committed": False, "before_head": _OLD_HEAD, "after_head": _OLD_HEAD}
_NOT_A_REPO = {"ok": False, "error": "not a git repo"}
_COMMIT_FAILED = {"ok": False, "error": "git commit failed", "before_head": _OLD_HEAD, "after_head": _OLD_HEAD}


def _restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, commit_result: Any, *, reason: str = "装了新工具",
) -> tuple[dict[str, Any], list[str]]:
    """跑一趟 request_restart，返回工具结果和发给用户的中间回复。真跑 git 会在仓库里落下 checkpoint 提交。"""
    monkeypatch.setattr(system_tool, "_git_snapshot", lambda _ctx: {"git_ok": False})
    monkeypatch.setattr(system_tool, "_git_commit_all", lambda **_kwargs: commit_result)
    replies: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/ops/request":
            return httpx.Response(200, json={"safety_level": "allow"})
        if path.endswith("/cancelled"):
            return httpx.Response(200, json={"cancelled": False})
        if path == "/v1/sessions/sess-1/events":
            event = json.loads(request.content)
            if event.get("type") == "intermediate_reply":
                replies.append(event["payload"]["text"])
            return httpx.Response(200, json={"ok": True})
        if path == "/v1/admin/restart":
            return httpx.Response(200, json={"scheduled": True, "target": "engine"})
        return httpx.Response(200, json={})

    async def run() -> dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://sv") as http:
            ctx = ToolContext(
                supervisor_url="http://sv",
                session_id="sess-1",
                run_id="run-1",
                worker_id="worker-1",
                workspace_root=tmp_path,
                http=http,
                registry=None,
            )
            return await system_tool.request_restart({"target": "engine", "reason": reason}, ctx)

    return asyncio.run(run()), replies


class TestTheReplyToTheUser:
    @pytest.mark.parametrize("commit_result", [
        pytest.param(_COMMITTED, id="提交了改动"),
        pytest.param(_CLEAN, id="没有改动"),
        pytest.param(_NOT_A_REPO, id="不是 git 仓库"),
        pytest.param(_COMMIT_FAILED, id="提交失败"),
    ])
    def test_it_never_promises_an_automatic_rollback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, commit_result: dict[str, Any],
    ) -> None:
        result, replies = _restart(tmp_path, monkeypatch, commit_result)

        [reply] = replies
        assert result["ok"] is True and result["data"]["scheduled"] is True
        assert reply.startswith("已请求重启：engine。\n原因：装了新工具\n（")
        assert "自动回滚" not in reply

    def test_a_new_checkpoint_is_named_as_the_commit_to_revert_to(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _result, [reply] = _restart(tmp_path, monkeypatch, _COMMITTED)

        assert f"checkpoint {_NEW_HEAD[:12]}" in reply
        assert "手动回退到这个提交" in reply
        assert _OLD_HEAD[:12] not in reply

    def test_a_clean_tree_names_the_current_commit(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _result, [reply] = _restart(tmp_path, monkeypatch, _CLEAN)

        assert f"当前提交 {_OLD_HEAD[:12]}" in reply
        assert "手动回退到这个提交" in reply

    @pytest.mark.parametrize("commit_result, reason", [
        pytest.param(_NOT_A_REPO, "not a git repo", id="不是 git 仓库"),
        pytest.param(_COMMIT_FAILED, "git commit failed", id="提交失败"),
    ])
    def test_a_missing_checkpoint_offers_nothing_to_revert_to(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, commit_result: dict[str, Any], reason: str,
    ) -> None:
        _result, [reply] = _restart(tmp_path, monkeypatch, commit_result)

        assert f"没能留下 checkpoint 提交：{reason}" in reply
        assert "手动回退到这个提交" not in reply

    def test_without_a_reason_the_note_follows_the_first_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _result, [reply] = _restart(tmp_path, monkeypatch, _CLEAN, reason="  ")

        assert reply == f"已请求重启：engine。\n{system_tool._checkpoint_note(_CLEAN)}"


class TestCheckpointNote:
    def test_a_result_without_any_head_leaves_the_hash_out(self) -> None:
        note = system_tool._checkpoint_note({"ok": True, "committed": False})

        assert "当前提交；" in note

    def test_a_commit_without_the_new_head_falls_back_to_the_old_one(self) -> None:
        note = system_tool._checkpoint_note({"ok": True, "committed": True, "before_head": _OLD_HEAD})

        assert f"checkpoint {_OLD_HEAD[:12]}；" in note

    @pytest.mark.parametrize("commit_result", [None, "committed", ["ok"], {}])
    def test_a_malformed_result_reads_as_no_checkpoint(self, commit_result: Any) -> None:
        note = system_tool._checkpoint_note(commit_result)

        assert note.startswith("（重启前没能留下 checkpoint 提交：原因不明。")
        assert "自动回滚" not in note
