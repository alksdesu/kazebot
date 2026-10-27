"""策略层认得隔离副本：副本里的路径按原仓库里同一位置判，相对路径、绝对路径、命令文本里的副本路径都一样；
别的任务的副本算外部；外部仓库的副本映射回那个仓库原来的路径。engine 侧判出的显示路径和 supervisor 一致，
不要求隔离时一切照旧。隔离任务起的子进程带着原仓库和它的虚拟环境的位置。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _worktree_fixture import (  # noqa: E402
    Env,
    env,  # noqa: F401
    item,
    requires_git,
    write,
    write_policy,
)
import config_layout  # noqa: E402
from clonoth_runtime import classify_path  # noqa: E402
from engine.project_root import (  # noqa: E402
    foreign_worktrees_root,
    isolation_error,
    task_project_origin,
    worktrees_root,
    worktrees_root_problem,
)
from supervisor.policy import PathScope, PolicyEngine, _default_policy_dict, map_worktree_paths  # noqa: E402
from supervisor.types import SafetyLevel, TaskKind  # noqa: E402
from toolbox._common import (  # noqa: E402
    ORIGIN_ROOT_ENV,
    ORIGIN_VENV_ENV,
    isolation_env,
    resolve_and_classify,
    safe_subprocess_env,
)

KEY = "ws-0123abcd"


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """工作区、默认位置的副本根，以及根下本任务的副本和另一个任务的副本；外部仓库和它的副本另放。"""
    for name in (config_layout.INSTANCES_FILE_ENV, "CLONOTH_URL_PREFIX"):
        monkeypatch.delenv(name, raising=False)
    ws = tmp_path / "ws"
    for rel in ("engine/x.py", "notes.md", "config/models.yaml"):
        write(ws / rel, "x\n")
    (ws / ".git").mkdir()
    root = tmp_path / "ws-worktrees"
    own = root / KEY / "task-own"
    other = root / KEY / "task-other"
    for base in (own, other):
        for rel in ("engine/x.py", "notes.md", "README.md"):
            write(base / rel, "x\n")
    libs = tmp_path / "libs"
    external = libs / "secret-lib"
    write(external / "lib.py", "x\n")
    write(external / "keys" / "id.txt", "x\n")
    external_wt = root / "secret-lib-89abcdef" / "task-ext"
    write(external_wt / "lib.py", "x\n")
    write(external_wt / "keys" / "id.txt", "x\n")
    return SimpleNamespace(
        tmp=tmp_path.resolve(), ws=ws.resolve(), root=root.resolve(), own=own.resolve(), other=other.resolve(),
        libs=libs.resolve(), external=external.resolve(), external_wt=external_wt.resolve(),
    )


def _own_scope(layout: SimpleNamespace) -> PathScope:
    return PathScope(
        project_root=layout.own, project_origin=layout.ws, foreign_root=layout.root, origin_repo=layout.ws,
        worktree="task-own", mappings=((layout.own, layout.ws), (layout.other, layout.ws)),
    )


def _external_scope(layout: SimpleNamespace) -> PathScope:
    return PathScope(
        project_root=layout.external_wt, project_origin=layout.external, foreign_root=layout.root,
        origin_repo=layout.external, worktree="task-ext", mappings=((layout.external_wt, layout.external),),
    )


def _policy(layout: SimpleNamespace, **overrides: Any) -> PolicyEngine:
    write_policy(layout.ws, **overrides)
    return PolicyEngine(workspace_root=layout.ws)


def _same(left: Any, right: Any) -> bool:
    return (left.safety_level, left.reason, left.sensitive) == (right.safety_level, right.reason, right.sensitive)


class TestClassifyPath:
    def test_a_relative_path_in_the_own_worktree_takes_the_origin_name(self, layout: SimpleNamespace) -> None:
        resolved, display, external = classify_path(
            layout.ws, [], "engine/x.py",
            project_root=layout.own, project_origin=layout.ws, foreign_root=layout.root,
        )

        assert resolved == layout.own / "engine" / "x.py"
        assert (display, external) == ("engine/x.py", False)

    def test_an_absolute_path_in_the_own_worktree_takes_the_origin_name(self, layout: SimpleNamespace) -> None:
        resolved, display, external = classify_path(
            layout.ws, [], str(layout.own / "notes.md"),
            project_root=layout.own, project_origin=layout.ws, foreign_root=layout.root,
        )

        assert resolved == layout.own / "notes.md" and (display, external) == ("notes.md", False)

    def test_the_worktree_root_itself_is_the_origin_root(self, layout: SimpleNamespace) -> None:
        _resolved, display, external = classify_path(
            layout.ws, [], ".", project_root=layout.own, project_origin=layout.ws, foreign_root=layout.root,
        )

        assert (display, external) == (".", False)

    def test_another_tasks_worktree_is_external_and_cannot_be_reached_relatively(self, layout: SimpleNamespace) -> None:
        target = layout.other / "README.md"

        for project_root, project_origin in ((layout.own, layout.ws), (None, None)):
            resolved, display, external = classify_path(
                layout.ws, [], str(target),
                project_root=project_root, project_origin=project_origin, foreign_root=layout.root,
            )
            assert resolved == target and display == target.as_posix() and external is True
        assert classify_path(
            layout.ws, [], f"../ws-worktrees/{KEY}/task-other/README.md", foreign_root=layout.root,
        ) == (None, "path escapes workspace root", False)

    def test_an_external_repo_worktree_maps_back_to_the_repo(self, layout: SimpleNamespace) -> None:
        resolved, display, external = classify_path(
            layout.ws, [layout.libs], "lib.py",
            project_root=layout.external_wt, project_origin=layout.external, foreign_root=layout.root,
        )

        assert resolved == layout.external_wt / "lib.py"
        assert display == (layout.external / "lib.py").as_posix() and external is False

        _resolved, display, external = classify_path(
            layout.ws, [], "lib.py",
            project_root=layout.external_wt, project_origin=layout.external, foreign_root=layout.root,
        )
        assert display == (layout.external / "lib.py").as_posix() and external is True

    def test_without_the_new_arguments_nothing_changes(self, layout: SimpleNamespace) -> None:
        for raw in ("engine/x.py", str(layout.ws / "notes.md"), str(layout.other / "README.md"), "../outside.txt"):
            assert classify_path(layout.ws, [], raw) == classify_path(layout.ws, [], raw, project_root=None)
        assert classify_path(layout.ws, [], "engine/x.py") == (layout.ws / "engine" / "x.py", "engine/x.py", False)

    def test_the_engine_resolves_like_the_supervisor_judges(self, layout: SimpleNamespace) -> None:
        assert foreign_worktrees_root(layout.ws) == layout.root

        resolved, external = resolve_and_classify(
            layout.ws, "engine/x.py", project_root=layout.own, project_origin=layout.ws,
        )
        assert resolved == layout.own / "engine" / "x.py" and external is False
        _resolved, external = resolve_and_classify(layout.ws, str(layout.other / "README.md"))
        assert external is True
        with pytest.raises(ValueError):
            resolve_and_classify(layout.ws, f"../ws-worktrees/{KEY}/task-other/README.md")


class TestPolicy:
    @pytest.mark.parametrize("rel", ["engine/x.py", "notes.md", "config/models.yaml", "data/worktrees.json"])
    def test_paths_in_the_own_worktree_are_judged_like_the_same_paths_in_the_repo(
        self, layout: SimpleNamespace, rel: str,
    ) -> None:
        policy = _policy(layout)
        scope = _own_scope(layout)

        for evaluate in (policy.evaluate_read_file, policy.evaluate_write_file):
            plain = evaluate(path=rel)
            assert _same(evaluate(path=rel, scope=scope), plain), (evaluate.__name__, rel)
            assert _same(evaluate(path=str(layout.own / rel), scope=scope), plain), (evaluate.__name__, rel)

    def test_the_worktree_registry_cannot_be_written(self, layout: SimpleNamespace) -> None:
        decision = _policy(layout).evaluate_write_file(path="data/worktrees.json")

        assert decision.safety_level == SafetyLevel.deny and "worktree registry" in decision.reason

    def test_the_bundled_example_policy_protects_the_registry_too(self) -> None:
        example = Path(__file__).resolve().parents[1] / "defaults" / "policy.example.yaml"
        import yaml

        rules = yaml.safe_load(example.read_text(encoding="utf-8"))["write_file"]["rules"]
        assert {"pattern": "data/worktrees.json", "decision": "deny",
                "reason": "the worktree registry is written by the supervisor only"} in rules
        assert {"pattern": "data/worktrees.json", "decision": "deny",
                "reason": "the worktree registry is written by the supervisor only"} in _default_policy_dict()["write_file"]["rules"]

    def test_another_tasks_worktree_needs_approval_like_any_outside_path(self, layout: SimpleNamespace) -> None:
        policy = _policy(layout)
        target = str(layout.other / "README.md")

        for scope in (_own_scope(layout), PathScope(foreign_root=layout.root)):
            decision = policy.evaluate_read_file(path=target, scope=scope)
            assert decision.safety_level == SafetyLevel.approval_required and decision.sensitive is True
        relative = f"../ws-worktrees/{KEY}/task-other/README.md"
        assert policy.evaluate_read_file(path=relative, scope=PathScope(foreign_root=layout.root)).safety_level == (
            SafetyLevel.deny
        )

    def test_writing_the_original_repo_from_a_worktree_needs_approval(self, layout: SimpleNamespace) -> None:
        policy = _policy(layout)
        scope = _own_scope(layout)
        target = str(layout.ws / "notes.md")

        isolated = policy.evaluate_write_file(path=target, scope=scope)
        assert isolated.safety_level == SafetyLevel.approval_required and isolated.sensitive is True
        assert "原仓库" in isolated.reason
        assert policy.evaluate_write_file(path=target).safety_level == SafetyLevel.auto
        assert policy.evaluate_read_file(path=target, scope=scope).safety_level == SafetyLevel.auto
        # 相对路径写的是副本里的那份，不算绕开副本
        assert policy.evaluate_write_file(path="notes.md", scope=scope).safety_level == SafetyLevel.auto

    def test_the_instance_config_and_data_follow_the_ordinary_rules(self, layout: SimpleNamespace) -> None:
        # 实例的 config/、data/ 被 gitignore，副本里没有这一份，写它们不算绕开副本
        policy = _policy(layout)
        scope = _own_scope(layout)

        for rel in ("data/attachments/report.txt", "config/runtime.yaml", "config/nodes/a.yaml", "data/worktrees.json"):
            target = str(layout.ws / rel)
            assert _same(policy.evaluate_write_file(path=target, scope=scope), policy.evaluate_write_file(path=target)), rel
        delivered = policy.evaluate_write_file(path=str(layout.ws / "data" / "attachments" / "report.txt"), scope=scope)
        assert delivered.safety_level == SafetyLevel.auto
        # 只是名字以 data 开头的仓库目录照样要确认
        assert "原仓库" in policy.evaluate_write_file(path=str(layout.ws / "database" / "x.py"), scope=scope).reason

    def test_an_unusable_worktree_denies_every_path_operation(self, layout: SimpleNamespace) -> None:
        policy = _policy(layout)
        scope = PathScope(worktree="task-gone", invalid="这个任务所在的隔离副本 task-gone 用不了")

        for op, parameters in (
            ("read_file", {"path": "notes.md"}),
            ("write_file", {"path": "notes.md"}),
            ("write_file", {"path": "notes.md", "paths": ["notes.md"]}),
            ("execute_command", {"command": "python -V"}),
        ):
            decision = policy.evaluate(op=op, parameters=parameters, scope=scope)
            assert decision.safety_level == SafetyLevel.deny and "task-gone" in decision.reason

    def test_a_worktree_path_in_a_command_is_judged_by_the_origin_path(self, layout: SimpleNamespace) -> None:
        keys = (layout.external / "keys").as_posix()
        policy = _policy(
            layout,
            extra_roots=[layout.libs.as_posix()],
            read_file={
                **_default_policy_dict()["read_file"],
                "rules": [
                    {"pattern": f"{keys}/**", "decision": "deny", "reason": "keys stay put"},
                    *_default_policy_dict()["read_file"]["rules"],
                ],
            },
        )
        command = f'cat "{layout.external_wt / "keys" / "id.txt"}"'

        assert policy.evaluate_execute_command(command=command).safety_level == SafetyLevel.approval_required
        mapped = policy.evaluate_execute_command(command=command, scope=_external_scope(layout))
        assert mapped.safety_level == SafetyLevel.deny and "keys" in mapped.reason
        # 没登记成本任务的副本时，副本路径照样映射：命令文本里的副本路径都按原仓库判
        foreign = policy.evaluate_execute_command(
            command=command, scope=PathScope(foreign_root=layout.root, mappings=_external_scope(layout).mappings),
        )
        assert foreign.safety_level == SafetyLevel.deny

    def test_the_external_repo_worktree_follows_the_rules_of_the_repo(self, layout: SimpleNamespace) -> None:
        keys = (layout.external / "keys").as_posix()
        policy = _policy(
            layout,
            extra_roots=[layout.libs.as_posix()],
            read_file={
                **_default_policy_dict()["read_file"],
                "rules": [
                    {"pattern": f"{keys}/**", "decision": "deny", "reason": "keys stay put"},
                    *_default_policy_dict()["read_file"]["rules"],
                ],
            },
        )
        scope = _external_scope(layout)

        assert policy.evaluate_read_file(path="keys/id.txt", scope=scope).safety_level == SafetyLevel.deny
        assert policy.evaluate_read_file(path="lib.py", scope=scope).safety_level == SafetyLevel.auto
        assert policy.logical_path("lib.py", scope) == (layout.external / "lib.py").as_posix()

    def test_a_multi_file_write_takes_the_strictest_file(self, layout: SimpleNamespace) -> None:
        policy = _policy(layout)
        notes, engine_file, registry = (
            str(layout.ws / "notes.md"), str(layout.ws / "engine" / "x.py"), str(layout.ws / "data" / "worktrees.json"),
        )

        assert policy.evaluate(op="write_file", parameters={"paths": [notes]}).safety_level == SafetyLevel.auto
        stricter = policy.evaluate(op="write_file", parameters={"path": notes, "paths": [notes, engine_file]})
        assert stricter.safety_level == SafetyLevel.approval_required and stricter.sensitive is True
        assert stricter.reason.startswith("engine/x.py: ")
        denied = policy.evaluate(op="write_file", parameters={"paths": [notes, engine_file, registry]})
        assert denied.safety_level == SafetyLevel.deny and denied.reason.startswith("data/worktrees.json: ")
        for broken in ([], "notes.md", ["notes.md", ""], ["notes.md", 3]):
            assert policy.evaluate(op="write_file", parameters={"paths": broken}).safety_level == SafetyLevel.deny

    def test_without_a_scope_the_decisions_are_the_old_ones(self, layout: SimpleNamespace) -> None:
        policy = _policy(layout)
        plain_scope = PathScope(foreign_root=layout.root)

        for op, parameters in (
            ("read_file", {"path": "config/models.yaml"}),
            ("write_file", {"path": "engine/x.py"}),
            ("write_file", {"path": str(layout.ws / "notes.md")}),
            ("execute_command", {"command": "cat config/models.yaml"}),
            ("execute_command", {"command": "python -V"}),
        ):
            assert _same(policy.evaluate(op=op, parameters=parameters, scope=plain_scope),
                         policy.evaluate(op=op, parameters=parameters))


class TestMapWorktreePaths:
    def test_the_worktree_part_and_what_follows_become_the_origin_path(self, layout: SimpleNamespace) -> None:
        mappings = ((layout.own, layout.ws),)
        command = f'python "{layout.own / "engine" / "x.py"}" --out {layout.own}'

        mapped = map_worktree_paths(command, mappings)

        assert mapped == f'python "{layout.ws.as_posix()}/engine/x.py" --out {layout.ws.as_posix()}'

    def test_msys_drive_spelling_and_neighbouring_names(self) -> None:
        mappings = ((Path("E:/work/wt/task-1"), Path("E:/work/repo")),)

        assert map_worktree_paths("ls /e/work/wt/task-1/src", mappings) == "ls E:/work/repo/src"
        assert map_worktree_paths("ls E:\\work\\wt\\task-1\\src\\a.py", mappings) == "ls E:/work/repo/src/a.py"
        assert map_worktree_paths("ls E:/work/wt/task-10/src", mappings) == "ls E:/work/wt/task-10/src"
        assert map_worktree_paths("echo nothing here", mappings) == "echo nothing here"


class TestEngineSide:
    def test_isolation_error_names_the_missing_piece(self, layout: SimpleNamespace) -> None:
        assert isolation_error({}) == ""
        assert isolation_error({"_project_root": str(layout.own)}) == ""
        assert "目录用不了" in isolation_error({"_worktree": "task-own"})
        assert "原仓库用不了" in isolation_error({
            "_worktree": "task-own", "_project_root": str(layout.own), "_project_origin": str(layout.tmp / "gone"),
        })
        assert isolation_error({
            "_worktree": "task-own", "_project_root": str(layout.own), "_project_origin": str(layout.ws),
        }) == ""

    def test_the_project_origin_is_only_read_for_isolated_tasks(self, layout: SimpleNamespace) -> None:
        assert task_project_origin({"_worktree": "task-own", "_project_origin": str(layout.ws)}) == layout.ws
        assert task_project_origin({"_project_origin": str(layout.ws)}) is None
        assert task_project_origin({"_worktree": "task-own", "_project_origin": "relative/path"}) is None

    def test_the_default_root_sits_beside_the_workspace_and_may_not_overlap_it(self, layout: SimpleNamespace) -> None:
        assert worktrees_root(layout.ws) == layout.root
        assert worktrees_root_problem(layout.root, layout.ws) == ""
        assert "里面" in worktrees_root_problem(layout.ws / "inner", layout.ws)
        assert "里面" in worktrees_root_problem(layout.tmp, layout.ws)

    def test_a_tool_task_whose_worktree_is_unusable_is_refused(self, layout: SimpleNamespace) -> None:
        from engine.runner import _run_tool_task

        result = asyncio.run(_run_tool_task(
            http=None, sup_url="http://unused", ws_root=layout.ws, registry=None,  # type: ignore[arg-type]
            task={"tool_name": "read_file", "input": {"_worktree": "task-gone", "arguments": {}}},
            worker_id="worker-1", session_id="s-1", session_generation=1, task_id="t-1",
        ))

        assert result["action"] == "fail" and "task-gone" in result["error"]

    def test_a_node_task_whose_worktree_is_unusable_is_refused(self, layout: SimpleNamespace) -> None:
        from engine.runner import _run_node_task

        result = asyncio.run(_run_node_task(
            http=None, llm_http=None, sup_url="http://unused", ws_root=layout.ws, registry=None,  # type: ignore[arg-type]
            task={"task_id": "t-2", "node_id": "bootstrap.coder", "input": {
                "_worktree": "task-gone", "_project_root": str(layout.own), "_project_origin": str(layout.tmp / "gone"),
            }},
            worker_id="worker-1", session_id="s-1", session_generation=1,
            api_key="", base_url="", default_model="",
        ))

        assert result["action"] == "fail" and "原仓库用不了" in result["error"]


class TestSubprocessEnv:
    def test_isolated_processes_learn_where_the_origin_and_its_venv_are(self, layout: SimpleNamespace) -> None:
        write(layout.ws / ".venv" / "pyvenv.cfg", "home = x\n")

        env = safe_subprocess_env(project_origin=layout.ws)

        assert env[ORIGIN_ROOT_ENV] == str(layout.ws)
        assert Path(env[ORIGIN_VENV_ENV]) == layout.ws / ".venv"

    def test_the_venv_is_looked_up_to_the_repo_root(self, layout: SimpleNamespace) -> None:
        write(layout.ws / "venv" / "pyvenv.cfg", "home = x\n")
        (layout.ws / "sub" / "deeper").mkdir(parents=True)

        env = isolation_env(layout.ws / "sub" / "deeper")

        assert Path(env[ORIGIN_VENV_ENV]) == layout.ws / "venv"
        assert Path(env[ORIGIN_ROOT_ENV]) == layout.ws / "sub" / "deeper"

    def test_no_venv_means_no_venv_variable(self, layout: SimpleNamespace) -> None:
        env = isolation_env(layout.ws)

        assert env == {ORIGIN_ROOT_ENV: str(layout.ws)}

    def test_ordinary_processes_never_see_the_variables(
        self, layout: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(ORIGIN_ROOT_ENV, "/somewhere/else")
        monkeypatch.setenv(ORIGIN_VENV_ENV, "/somewhere/else/.venv")

        env = safe_subprocess_env()

        assert ORIGIN_ROOT_ENV not in env and ORIGIN_VENV_ENV not in env
        assert isolation_env(None) == {} and isolation_env("") == {}


@requires_git
class TestSupervisorVerdicts:
    def _web_child(self, env: Env) -> Any:
        sid = env.session("web:wt", channel="web")
        caller = env.caller(sid, key="web:wt", channel="web", auth={})
        [child] = env.wait_batch(caller, item(isolation="worktree"))
        env.wait()
        return sid, caller, child

    def test_a_relative_path_is_judged_and_shown_like_the_repo_path(self, env: Env) -> None:
        sid, _caller, child = self._web_child(env)
        parameters = {"path": "engine/x.py", "tool_name": "write_file", "reason": "edit"}

        verdict = env.state.evaluate_operation(session_id=sid, op="write_file", parameters=parameters, task_id=child.task_id)
        plain = env.state.policy.evaluate(op="write_file", parameters=parameters)
        assert verdict.safety_level == plain.safety_level == SafetyLevel.approval_required

        requested = env.state.request_operation(
            session_id=sid, op="write_file", parameters=parameters, task_id=child.task_id,
        )
        details = env.state.approvals[requested.approval_id].details
        assert details["path"] == "engine/x.py" and "worktree_path" not in details
        assert details["isolated_copy"]["worktree"] == child.task_id
        assert Path(details["isolated_copy"]["path"]).resolve() == env.worktree_of(child).resolve()
        assert Path(details["isolated_copy"]["origin"]).resolve() == env.ws.resolve()
        assert details["reason"] == f"在隔离副本 {child.task_id} 里：edit"

    def test_an_absolute_worktree_path_is_shown_by_its_repo_name(self, env: Env) -> None:
        sid, _caller, child = self._web_child(env)
        raw = str(env.worktree_of(child) / "engine" / "x.py")

        requested = env.state.request_operation(
            session_id=sid, op="write_file", parameters={"path": raw, "tool_name": "write_file"}, task_id=child.task_id,
        )

        assert requested.safety_level == SafetyLevel.approval_required
        details = env.state.approvals[requested.approval_id].details
        assert details["path"] == "engine/x.py" and details["worktree_path"] == raw
        assert details["reason"].startswith(f"在隔离副本 {child.task_id} 里：")

    def test_a_command_in_a_worktree_is_marked_on_the_card(self, env: Env) -> None:
        sid, _caller, child = self._web_child(env)

        requested = env.state.request_operation(
            session_id=sid, op="execute_command", parameters={"command": "python -V", "tool_name": "execute_command"},
            task_id=child.task_id,
        )

        assert requested.safety_level == SafetyLevel.approval_required
        details = env.state.approvals[requested.approval_id].details
        assert details["command"] == "python -V" and details["isolated_copy"]["worktree"] == child.task_id

    def test_a_task_that_is_not_isolated_sends_the_same_details_as_before(self, env: Env) -> None:
        sid = env.session("web:plain", channel="web")
        caller = env.caller(sid, key="web:plain", channel="web", auth={})
        parameters = {"path": "engine/x.py", "tool_name": "write_file", "reason": "edit"}

        requested = env.state.request_operation(session_id=sid, op="write_file", parameters=parameters, task_id=caller.task_id)

        details = env.state.approvals[requested.approval_id].details
        assert details == parameters

    def test_a_merge_card_lists_the_files_by_their_repo_names(self, env: Env) -> None:
        sid = env.session("web:merge", channel="web")
        caller = env.caller(sid, key="web:merge", channel="web", auth={})
        targets = [str(env.ws / "app" / "new.py"), str(env.ws / "engine" / "x.py")]

        requested = env.state.request_operation(
            session_id=sid, op="write_file",
            parameters={"path": targets[0], "paths": targets, "tool_name": "merge_worktree", "reason": "merge"},
            task_id=caller.task_id,
        )

        assert requested.safety_level == SafetyLevel.approval_required
        assert requested.reason.startswith("engine/x.py: ")
        details = env.state.approvals[requested.approval_id].details
        assert details["paths"] == ["app/new.py", "engine/x.py"] and details["path"] == "app/new.py"
        assert "isolated_copy" not in details

    def test_a_task_whose_worktree_is_gone_is_denied(self, env: Env) -> None:
        sid, caller, child = self._web_child(env)
        env.finish(child)
        handle = env.state.tasks[caller.task_id].input["resume_data"]["entries"][0]["worktree"]["handle"]
        env.service.abandon(task_id=caller.task_id, handle=handle)
        grandchild_input = {key: child.input[key] for key in ("_project_root", "_project_origin", "_worktree")}
        with env.state._lock:
            straggler = env.state._create_task_locked(
                session_id=sid, session_generation=1, kind=TaskKind.node, node_id="late.node",
                input_data={**grandchild_input, "task_context": {"conversation_key": "web:wt", "channel": "web"}},
                continuation={}, source_inbound_seq=None,
            )

        verdict = env.state.evaluate_operation(
            session_id=sid, op="read_file", parameters={"path": "README.md"}, task_id=straggler.task_id,
        )

        assert verdict.safety_level == SafetyLevel.deny and child.task_id in verdict.reason
