"""合回和放弃：预览列出要改的原仓库文件，合回把副本的改动套到原仓库上并记进改动日志、undo_edits 撤得掉；
原仓库这期间被改过就如实报告冲突、什么都不改；别的任务认领着就拒绝；多开时没被点名的实例只给补丁。
merge_worktree 工具按 write_file 的规则送审，审批卡列出改了哪些文件。"""
from __future__ import annotations

import asyncio
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _file_edits_fake import FakeFileEdits  # noqa: E402
from _worktree_fixture import (  # noqa: E402
    Env,
    close_service,
    entries,
    env,  # noqa: F401
    git,
    item,
    make_repo,
    make_state,
    requires_git,
    write,
    write_policy,
    write_runtime,
)
from clonoth_runtime import switch_refusal  # noqa: E402
from engine.worktree_report import worktree_lines  # noqa: E402
from supervisor.file_claims import _resolver_locked, ledger_of  # noqa: E402
from supervisor.worktrees.keys import WORKTREE_OUTCOME_KEY  # noqa: E402
from toolbox import edit_journal, file_state  # noqa: E402
from toolbox.builtins.undo_edits import undo_edits  # noqa: E402
from toolbox.builtins.worktrees import merge_worktree, register_tools  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402

pytestmark = requires_git

ORIGINAL_MAIN = "def main():\n    return 1\n"


def _finished_child(env: Env, *, edit: bool = True) -> tuple[Any, Any, dict[str, Any]]:
    """调用方派一个要求隔离的子任务，子任务在副本里改几个文件后收尾；返回 (调用方, 子任务, 交回的副本结果)。"""
    caller = env.caller(env.session())
    child = env.isolated_child(caller)
    if edit:
        path = env.worktree_of(child)
        write(path / "app" / "main.py", "def main():\n    return 2\n")
        (path / "app" / "old.py").unlink()
        write(path / "app" / "new.py", "NEW = 1\n")
    env.finish(child)
    [entry] = entries(caller)
    return caller, child, entry["worktree"]


class TestPreviewAndMerge:
    def test_the_preview_lists_the_files_to_change_in_the_original_repo(self, env: Env) -> None:
        caller, child, view = _finished_child(env)

        preview = env.service.preview(task_id=caller.task_id, handle=view["handle"])

        assert preview["ok"] is True and preview["worktree"] == child.task_id
        targets = {item["path"]: Path(item["target"]) for item in preview["files"]}
        assert set(targets) == {"app/main.py", "app/old.py", "app/new.py"}
        assert all(target.resolve() == (env.ws / name).resolve() for name, target in targets.items())
        assert preview["digest"] == view["digest"] and preview["file_count"] == 3
        assert (env.ws / "app" / "main.py").read_text(encoding="utf-8") == ORIGINAL_MAIN

    def test_previews_of_one_worktree_at_once_agree(self, env: Env) -> None:
        caller, _child, view = _finished_child(env)
        previews: list[dict[str, Any]] = []
        errors: list[BaseException] = []

        def preview() -> None:
            try:
                previews.append(env.service.preview(task_id=caller.task_id, handle=view["handle"]))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=preview) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)

        assert errors == [] and len(previews) == 4
        assert all(item["ok"] is True for item in previews), previews
        assert {item["digest"] for item in previews} == {view["digest"]}

    def test_merging_applies_the_changes_and_cleans_up_afterwards(self, env: Env) -> None:
        caller, child, view = _finished_child(env)
        preview = env.service.preview(task_id=caller.task_id, handle=view["handle"])

        merged = env.service.merge(task_id=caller.task_id, handle=view["handle"], digest=preview["digest"])

        assert merged["ok"] is True, merged
        assert sorted(merged["merged"]) == ["app/main.py", "app/new.py", "app/old.py"]
        assert merged["undo_task_id"] == f"merge-{child.task_id}" and merged["undoable"] is True
        assert (env.ws / "app" / "main.py").read_text(encoding="utf-8") == "def main():\n    return 2\n"
        assert (env.ws / "app" / "new.py").read_text(encoding="utf-8") == "NEW = 1\n"
        assert not (env.ws / "app" / "old.py").exists()
        entry = env.entry(child)
        assert entry["state"] == "merged" and entry["merged_by"] == caller.task_id
        # 合回只改工作区里的文件，不提交，也不动索引
        assert git(env.ws, "diff", "--cached", "--name-only").strip() == ""
        # 合回期间拿的认领合完就还回去，调用方之后还能自己改这些文件
        assert ledger_of(env.state).claims.held_by(caller.task_id) == []

        env.service.housekeeping()
        assert env.entry(child)["state"] == "removed"
        assert not env.worktree_of(child).exists()

    def test_the_merge_is_journaled_and_undo_edits_puts_everything_back(self, env: Env) -> None:
        caller, child, view = _finished_child(env)
        preview = env.service.preview(task_id=caller.task_id, handle=view["handle"])
        merged = env.service.merge(task_id=caller.task_id, handle=view["handle"], digest=preview["digest"])
        assert merged["ok"] is True, merged

        ctx = _UndoCtx(env.ws, task=caller.task_id, session=merged["journal_session"])
        listed = asyncio.run(undo_edits({"action": "list"}, ctx))
        text = listed["data"]["result"]
        assert f"merge-{child.task_id}" in text
        assert "app/old.py（merge_worktree 删除）" in text
        assert "app/new.py（merge_worktree 新建）" in text

        undone = asyncio.run(undo_edits({"task_id": f"merge-{child.task_id}"}, ctx))

        assert undone["ok"] is True, undone
        assert (env.ws / "app" / "main.py").read_text(encoding="utf-8") == ORIGINAL_MAIN
        assert (env.ws / "app" / "old.py").read_text(encoding="utf-8") == "OLD = True\n"
        assert not (env.ws / "app" / "new.py").exists()
        assert git(env.ws, "status", "--porcelain", "--", "app").strip() == ""

    def test_a_worktree_without_changes_merges_to_nothing(self, env: Env) -> None:
        caller, child, view = _finished_child(env, edit=False)
        preview = env.service.preview(task_id=caller.task_id, handle=view["handle"])
        assert preview["ok"] is True and preview["files"] == []

        merged = env.service.merge(task_id=caller.task_id, handle=view["handle"], digest=preview["digest"])

        assert merged["ok"] is True and merged["merged"] == []
        assert env.entry(child)["state"] == "merged"

    def test_an_external_repo_is_merged_into_itself(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLONOTH_INSTANCES_FILE", raising=False)
        libs = tmp_path / "libs"
        external = make_repo(libs / "shared-lib", {"lib.py": "VALUE = 1\n"})
        ws = make_repo(tmp_path / "ws")
        write_policy(ws, extra_roots=[libs.as_posix()])
        state = make_state(ws)
        try:
            environment = Env(tmp_path, ws, state)
            caller = environment.caller(environment.session())
            [child] = environment.wait_batch(caller, item(isolation="worktree", repo=str(external)))
            environment.wait()
            write(environment.worktree_of(child) / "lib.py", "VALUE = 2\n")
            environment.finish(child)
            view = entries(caller)[0]["worktree"]

            preview = environment.service.preview(task_id=caller.task_id, handle=view["handle"])
            [target] = preview["files"]
            assert Path(target["target"]).resolve() == (external / "lib.py").resolve()
            merged = environment.service.merge(task_id=caller.task_id, handle=view["handle"], digest=preview["digest"])

            assert merged["ok"] is True, merged
            assert (external / "lib.py").read_text(encoding="utf-8") == "VALUE = 2\n"
            assert not (ws / "lib.py").exists()
        finally:
            close_service(state)


class TestRefusals:
    def test_a_conflict_is_reported_and_nothing_changes(self, env: Env) -> None:
        caller, child, view = _finished_child(env)
        write(env.ws / "app" / "main.py", "def main():\n    return 3\n")
        preview = env.service.preview(task_id=caller.task_id, handle=view["handle"])

        merged = env.service.merge(task_id=caller.task_id, handle=view["handle"], digest=preview["digest"])

        assert merged["ok"] is False and merged["reason"] == "conflict"
        [conflict] = [item for item in merged["conflicts"] if item["path"] == "app/main.py"]
        assert isinstance(conflict.get("line"), int) and conflict["reason"]
        assert "什么都没有改" in merged["message"]
        assert (env.ws / "app" / "main.py").read_text(encoding="utf-8") == "def main():\n    return 3\n"
        assert (env.ws / "app" / "old.py").is_file() and not (env.ws / "app" / "new.py").exists()
        assert env.entry(child)["state"] == "finished"
        assert env.worktree_of(child).is_dir()

    def test_a_file_another_task_is_editing_blocks_the_merge(self, env: Env) -> None:
        caller, child, view = _finished_child(env)
        editor = env.caller(env.session(), node="test.editor")
        target = file_state.path_key((env.ws / "app" / "main.py").resolve())
        with env.state._lock:
            now = datetime.now(timezone.utc)
            claimed = ledger_of(env.state).begin(
                task_id=editor.task_id, session=editor.session_id, path=target, display="app/main.py",
                journal_session=editor.session_id, resolve=_resolver_locked(env.state, now), now=now.timestamp(),
            )
        assert claimed["ok"] is True

        preview = env.service.preview(task_id=caller.task_id, handle=view["handle"])
        merged = env.service.merge(task_id=caller.task_id, handle=view["handle"], digest=view["digest"])

        for refused in (preview, merged):
            assert refused["ok"] is False and refused["reason"] == "claimed"
            assert editor.task_id in refused["message"] and "test.editor" in refused["message"]
        assert (env.ws / "app" / "main.py").read_text(encoding="utf-8") == ORIGINAL_MAIN
        assert env.entry(child)["state"] == "finished"

    def test_a_worktree_that_changed_after_approval_is_not_merged(self, env: Env) -> None:
        caller, child, view = _finished_child(env)
        preview = env.service.preview(task_id=caller.task_id, handle=view["handle"])
        write(env.worktree_of(child) / "app" / "late.py", "LATE = 1\n")

        merged = env.service.merge(task_id=caller.task_id, handle=view["handle"], digest=preview["digest"])

        assert merged["ok"] is False and merged["reason"] == "changed"
        assert (env.ws / "app" / "main.py").read_text(encoding="utf-8") == ORIGINAL_MAIN
        assert env.entry(child)["state"] == "finished"

    def test_a_task_inside_a_worktree_cannot_merge(self, env: Env) -> None:
        caller = env.caller(env.session())
        child = env.isolated_child(caller)

        refused = env.service.preview(task_id=child.task_id, handle=child.task_id)

        assert refused["ok"] is False and "自己在隔离副本里" in refused["message"]

    def test_another_conversation_cannot_touch_the_worktree(self, env: Env) -> None:
        caller, child, view = _finished_child(env)
        stranger = env.caller(env.session("qq_group:9999"), key="qq_group:9999")

        for refused in (
            env.service.preview(task_id=stranger.task_id, handle=view["handle"]),
            env.service.abandon(task_id=stranger.task_id, handle=view["handle"]),
        ):
            assert refused["ok"] is False and "不是这个会话派出去的" in refused["message"]
        assert env.entry(child)["state"] == "finished"

    def test_a_worktree_still_in_use_is_not_merged(self, env: Env) -> None:
        sid = env.session()
        caller = env.caller(sid)
        child = env.isolated_child(caller)
        grandchild = env.async_child(sid, child, node="b.node")
        assert grandchild.input["_worktree"] == child.task_id
        env.finish(child)
        view = entries(caller)[0]["worktree"]
        assert env.entry(child)["state"] == "finished"

        refused = env.service.preview(task_id=caller.task_id, handle=view["handle"])

        assert refused["ok"] is False and grandchild.task_id in refused["message"]

    def test_an_unknown_handle_is_refused(self, env: Env) -> None:
        caller = env.caller(env.session())

        refused = env.service.preview(task_id=caller.task_id, handle="no-such-task@no-such-session")

        assert refused["ok"] is False and "找不到" in refused["message"]


class TestInstances:
    def test_only_the_named_instances_may_merge_the_workspace_repo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data_root = tmp_path / "data-root"
        instances = write(data_root / "instances.yaml", yaml.safe_dump({"instances": [
            {"id": "main", "path": "", "idx": 0},
            {"id": "alt", "path": "/i/alt", "idx": 1},
        ]}))
        monkeypatch.setenv("CLONOTH_INSTANCES_FILE", str(instances))
        monkeypatch.setenv("CLONOTH_URL_PREFIX", "/i/alt")
        ws = make_repo(tmp_path / "ws")
        write_runtime(ws, {"engine": {"worktree": {"merge_instances": ["main"]}}})
        state = make_state(ws)
        try:
            environment = Env(tmp_path, ws, state)
            caller, child, view = _finished_child(environment)
            assert Path(view["path"]).resolve().parents[1] == (data_root / "worktrees").resolve()
            assert view["merge_allowed"] is False
            assert any("本实例不能合回这个仓库" in line for line in worktree_lines(view))

            preview = environment.service.preview(task_id=caller.task_id, handle=view["handle"])
            merged = environment.service.merge(task_id=caller.task_id, handle=view["handle"], digest=view["digest"])

            for refused in (preview, merged):
                assert refused["ok"] is False and refused["reason"] == "instance_not_allowed"
                assert refused["message"] == f"本实例不能合回这个仓库，补丁在 {view['patch']}"
                assert Path(refused["patch"]).is_file()
            assert (ws / "app" / "main.py").read_text(encoding="utf-8") == ORIGINAL_MAIN
            assert environment.entry(child)["state"] == "finished"

            write_runtime(ws, {"engine": {"worktree": {"merge_instances": ["alt"]}}})
            preview = environment.service.preview(task_id=caller.task_id, handle=view["handle"])
            merged = environment.service.merge(task_id=caller.task_id, handle=view["handle"], digest=preview["digest"])
            assert merged["ok"] is True, merged
        finally:
            close_service(state)

    def test_a_roster_nobody_can_read_still_refuses_but_says_it_is_the_roster(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 只说本实例不能合回，人会去改 merge_instances；挡着的其实是读不懂的清单。
        data_root = tmp_path / "data-root"
        instances = write(data_root / "instances.yaml", yaml.safe_dump({"instances": [
            {"id": "main", "path": "", "idx": 0},
            {"id": "alt", "path": "/i/alt", "idx": 1},
        ]}))
        monkeypatch.setenv("CLONOTH_INSTANCES_FILE", str(instances))
        monkeypatch.setenv("CLONOTH_URL_PREFIX", "/i/alt")
        ws = make_repo(tmp_path / "ws")
        write_runtime(ws, {"engine": {"worktree": {"merge_instances": ["alt"]}}})
        state = make_state(ws)
        try:
            environment = Env(tmp_path, ws, state)
            caller = environment.caller(environment.session())
            child = environment.isolated_child(caller)
            # 建副本时清单还读得懂；之后写坏了，子任务照样收尾。
            write(instances, "instances: [ this is: not: valid\n")
            write(environment.worktree_of(child) / "app" / "main.py", "def main():\n    return 2\n")
            environment.finish(child)
            [entry] = entries(caller)
            view = entry["worktree"]
            refusal = f"查不了本实例能不能合回这个仓库：实例清单 {instances} 读不懂：第 1 行"

            assert view["merge_allowed"] is False
            assert view["merge_refusal"].startswith(refusal)
            assert any(line.startswith(refusal) for line in worktree_lines(view))
            preview = environment.service.preview(task_id=caller.task_id, handle=view["handle"])
            merged = environment.service.merge(task_id=caller.task_id, handle=view["handle"], digest=view["digest"])
            for refused in (preview, merged):
                assert refused["ok"] is False and refused["reason"] == "instance_not_allowed"
                assert refused["message"].startswith(refusal)
                assert refused["message"].endswith(f"，补丁在 {view['patch']}")
                assert "this is" not in refused["message"]
            assert (ws / "app" / "main.py").read_text(encoding="utf-8") == ORIGINAL_MAIN
        finally:
            close_service(state)

    def test_a_roster_that_breaks_before_the_child_starts_does_not_stop_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 登记里记哪个实例建的只供查看，读不懂清单不该把隔离子任务挡掉。
        data_root = tmp_path / "data-root"
        instances = write(data_root / "instances.yaml", "instances: [ this is: not: valid\n")
        monkeypatch.setenv("CLONOTH_INSTANCES_FILE", str(instances))
        monkeypatch.setenv("CLONOTH_URL_PREFIX", "/i/alt")
        ws = make_repo(tmp_path / "ws")
        state = make_state(ws)
        try:
            environment = Env(tmp_path, ws, state)
            caller = environment.caller(environment.session())
            child = environment.isolated_child(caller)

            assert environment.entry(child)["instance"] == ""
            assert environment.worktree_of(child).is_dir()
        finally:
            close_service(state)


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _ServiceHttp:
    """把 merge_worktree 发给 supervisor 的请求直接交给副本服务，和 /v1/worktrees 接口做的一样。"""

    def __init__(self, env: Env) -> None:
        self.env = env
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def request(self, method: str, url: str, *, json: dict[str, Any] | None = None, timeout: float = 0.0) -> Any:
        path = url.split("/v1/worktrees", 1)[1]
        body = dict(json or {})
        self.calls.append((path, body))
        service = self.env.service
        if path == "/preview":
            return _FakeResponse(service.preview(task_id=body["task_id"], handle=body["handle"]))
        if path == "/merge":
            return _FakeResponse(service.merge(task_id=body["task_id"], handle=body["handle"], digest=body["digest"]))
        if path == "/abandon":
            return _FakeResponse(service.abandon(task_id=body["task_id"], handle=body["handle"]))
        return _FakeResponse({"detail": "not found"}, 404)


class _ToolCtx:
    """merge_worktree 用到的那几样：任务号、supervisor 地址、HTTP 客户端、送审。"""

    def __init__(self, env: Env, task: Any, *, decision: str = "auto") -> None:
        self.task_id = task.task_id
        self.supervisor_url = "http://supervisor.test"
        self.http = _ServiceHttp(env)
        self.workspace_root = env.ws
        self.decision = decision
        self.requested: list[tuple[str, dict[str, Any]]] = []

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.requested.append((op, dict(parameters)))
        return {"safety_level": self.decision, "reason": f"{op} decided by the test"}

    async def check_cancelled(self) -> bool:
        return False

    async def wait_for_approval(self, approval_id: Any) -> dict[str, Any]:
        return {"status": "denied"}


class _UndoCtx(FakeFileEdits, ToolContext):
    """离线跑 undo_edits：认领交给测试里的 FakeSupervisor，送审一律放行。"""

    def __init__(self, workspace_root: Path, *, task: str, session: str) -> None:
        super().__init__(
            supervisor_url="http://unused", session_id=session, run_id="run-undo", worker_id="worker-undo",
            workspace_root=workspace_root, http=None, registry=None, task_id=task,  # type: ignore[arg-type]
        )

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return {"safety_level": "auto", "reason": "test"}

    async def evaluate_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return {"safety_level": "auto", "reason": "test"}

    async def check_cancelled(self) -> bool:
        return False


def _run_tool(ctx: _ToolCtx, **args: Any) -> dict[str, Any]:
    return asyncio.run(merge_worktree(args, ctx))


class TestTheMergeTool:
    def test_it_asks_for_write_approval_on_every_changed_file_then_merges(self, env: Env) -> None:
        caller, child, view = _finished_child(env)
        ctx = _ToolCtx(env, caller)

        result = _run_tool(ctx, handle=view["handle"])

        assert result["ok"] is True, result
        [(op, parameters)] = ctx.requested
        assert op == "write_file" and parameters["tool_name"] == "merge_worktree"
        assert {Path(target).resolve() for target in parameters["paths"]} == {
            (env.ws / name).resolve() for name in ("app/main.py", "app/old.py", "app/new.py")
        }
        assert parameters["path"] == parameters["paths"][0]
        assert parameters["worktree"] == child.task_id
        reason = parameters["reason"]
        assert child.task_id in reason and "3 个文件" in reason
        for line in ("M app/main.py", "D app/old.py", "A app/new.py"):
            assert line in reason
        text = result["data"]["result"]
        assert "已把隔离副本" in text and f"merge-{child.task_id}" in text and "undo_edits" in text
        assert result["data"]["undo_task_id"] == f"merge-{child.task_id}"
        assert (env.ws / "app" / "new.py").is_file()
        assert [path for path, _body in ctx.http.calls] == ["/preview", "/merge"]

    def test_a_denied_approval_merges_nothing(self, env: Env) -> None:
        caller, child, view = _finished_child(env)
        ctx = _ToolCtx(env, caller, decision="deny")

        result = _run_tool(ctx, handle=view["handle"])

        assert result["ok"] is False
        assert [path for path, _body in ctx.http.calls] == ["/preview"]
        assert (env.ws / "app" / "main.py").read_text(encoding="utf-8") == ORIGINAL_MAIN
        assert env.entry(child)["state"] == "finished"

    def test_abandon_needs_no_approval(self, env: Env) -> None:
        caller, child, view = _finished_child(env)
        ctx = _ToolCtx(env, caller)

        result = _run_tool(ctx, handle=view["handle"], abandon=True)

        assert result["ok"] is True and result["data"]["abandoned"] is True
        assert ctx.requested == []
        assert env.entry(child)["state"] == "abandoned"
        again = _run_tool(ctx, handle=view["handle"], abandon=True)
        assert again["ok"] is True and again["data"]["abandoned"] is False

    @pytest.mark.parametrize("raw", ["true", "YES", 1])
    def test_other_spellings_of_true_still_abandon(self, env: Env, raw: Any) -> None:
        caller, _child, view = _finished_child(env)
        ctx = _ToolCtx(env, caller)

        result = _run_tool(ctx, handle=view["handle"], abandon=raw)

        assert result["ok"] is True and result["data"]["abandoned"] is True
        assert [path for path, _body in ctx.http.calls] == ["/abandon"]

    @pytest.mark.parametrize("raw", [False, "false", 0, None, ""])
    def test_false_or_unset_merges(self, env: Env, raw: Any) -> None:
        caller, _child, view = _finished_child(env)
        ctx = _ToolCtx(env, caller)

        result = _run_tool(ctx, handle=view["handle"], abandon=raw)

        assert result["ok"] is True, result
        assert [path for path, _body in ctx.http.calls] == ["/preview", "/merge"]
        assert (env.ws / "app" / "new.py").is_file()

    @pytest.mark.parametrize("raw", ["discard", "放弃", 2, ["true"]])
    def test_an_unrecognised_abandon_is_refused_instead_of_merging(self, env: Env, raw: Any) -> None:
        caller, child, view = _finished_child(env)
        ctx = _ToolCtx(env, caller)

        result = _run_tool(ctx, handle=view["handle"], abandon=raw)

        assert result["ok"] is False
        assert result["error"] == switch_refusal("abandon", raw)
        assert ctx.http.calls == [] and ctx.requested == []
        assert env.entry(child)["state"] == "finished"
        assert (env.ws / "app" / "main.py").read_text(encoding="utf-8") == ORIGINAL_MAIN

    def test_a_conflict_lists_the_files_and_lines(self, env: Env) -> None:
        caller, child, view = _finished_child(env)
        write(env.ws / "app" / "main.py", "def main():\n    return 3\n")
        ctx = _ToolCtx(env, caller)

        result = _run_tool(ctx, handle=view["handle"])

        assert result["ok"] is False and result["data"]["reason"] == "conflict"
        text = result["data"]["result"]
        assert "冲突的位置" in text and "app/main.py 第 " in text and "abandon=true" in text
        assert env.entry(child)["state"] == "finished"

    def test_an_empty_handle_is_rejected_before_anything_runs(self, env: Env) -> None:
        caller = env.caller(env.session())
        ctx = _ToolCtx(env, caller)

        result = _run_tool(ctx, handle="  ")

        assert result["ok"] is False and ctx.http.calls == []

    def test_the_tool_is_registered_with_handle_required(self) -> None:
        registered: dict[str, Any] = {}
        registry = SimpleNamespace(
            register_builtin_tool=lambda name, description, schema, func, *extra: registered.update(
                {name: (description, schema, func)},
            ),
        )

        register_tools(registry)

        description, schema, func = registered["merge_worktree"]
        assert func is merge_worktree and schema["required"] == ["handle"]
        assert set(schema["properties"]) == {"handle", "abandon"}
        assert "undo_edits" in description and "write_file" in description


def test_the_outcome_is_not_written_into_slim_snapshots(env: Env) -> None:
    caller, child, view = _finished_child(env)

    payload = env.state._event_payload_for_task(child, event_type="task_completed")

    assert WORKTREE_OUTCOME_KEY not in payload["input"]
    assert payload["input"]["_worktree"] == child.task_id and payload["input"]["_worktree_owner"] is True


def test_the_journal_directory_cannot_be_a_merge_target(env: Env) -> None:
    assert edit_journal.inside_journal(env.ws, env.ws / file_state.JOURNAL_DIR / "x") is True
