"""项目说明自动加载：工具碰到的目录往上找 AGENTS.md / CLAUDE.md，过 read_file 策略，下一步推理前以动态消息注入。"""
from __future__ import annotations

import asyncio
import os
import sys
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _engine_loop_fixture import NodeRun, ScriptedRegistry, answered_once, call, echo, failed, stored, turn  # noqa: E402
from engine.builtin import project_instructions  # noqa: E402
from engine.builtin.project_instructions import (  # noqa: E402
    FILE_CHAR_LIMIT,
    LOADED_EVENT,
    MESSAGE_KEY,
    ProjectInstructions,
    discover,
    read_instruction,
    touched_paths,
)
from engine.node import Node, ToolAccess  # noqa: E402
from engine.protocol import ACTION_FINISH  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402

HEADER = "[PROJECT_INSTRUCTIONS]"


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch) -> None:
    # 状态按任务 id 存在进程里，测试脚手架的任务 id 都一样
    monkeypatch.setattr(project_instructions, "_TASK_STATES", OrderedDict())


class Verdicts:
    """顶替 ToolContext.evaluate_op：按路径给 read_file 的结论（没写的一律 auto），可以给一串依次用；每次问都记下。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, by_path: dict[str, Any] | None = None) -> None:
        self.by_path = dict(by_path or {})
        self.asked: list[tuple[str, str]] = []
        self.contexts: list[ToolContext] = []
        verdicts = self

        async def evaluate_op(ctx: ToolContext, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
            verdicts.asked.append((op, parameters["path"]))
            verdicts.contexts.append(ctx)
            outcome = verdicts.by_path.get(parameters["path"], "auto")
            if isinstance(outcome, list):
                outcome = outcome.pop(0) if len(outcome) > 1 else outcome[0]
            if isinstance(outcome, Exception):
                raise outcome
            return {"safety_level": outcome, "reason": "decided by the test"}

        monkeypatch.setattr(ToolContext, "evaluate_op", evaluate_op)

    @property
    def paths(self) -> list[str]:
        return [path for _op, path in self.asked]


def seed(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def read(path: str, call_id: str) -> Any:
    return call("read_file", {"files": [{"path": path}]}, call_id=call_id)


def finish(text: str = "done") -> Any:
    return turn(call("finish", {"text": text}, call_id="f"))


def node_run(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    *,
    tools: dict[str, Any] | None = None,
    task_context: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    more: dict[str, list[Any]] | None = None,
    handler: ProjectInstructions | None = None,
) -> NodeRun:
    handler = handler or ProjectInstructions()
    handlers: dict[str, list[Any]] = {"after_tool_call": [handler.collect], "before_step": [handler.inject]}
    for point, items in (more or {}).items():
        handlers.setdefault(point, []).extend(items)
    node = Node(
        id="node-1", type="ai", prompt="Work carefully.", tool_mode="native",
        tool_access=ToolAccess(mode="all"), extra=dict(extra or {}),
    )
    registry = ScriptedRegistry(tools=tools or {"read_file": echo("file text"), "probe": echo("probe output")})
    return NodeRun(
        monkeypatch, workspace, registry=registry, node=node,
        runtime_cfg={"engine": {"max_steps": 8}}, task_context=task_context, handlers=handlers,
    )


def injected(run: NodeRun, request: int) -> list[dict[str, Any]]:
    return [message for message in run.requests[request]["messages"] if message.get(MESSAGE_KEY)]


def loaded_events(run: NodeRun) -> list[dict[str, Any]]:
    return run.rctx.events.of(LOADED_EVENT)  # type: ignore[attr-defined]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    seed(tmp_path / "AGENTS.md", "root rules")
    seed(tmp_path / "pkg" / "AGENTS.md", "pkg rules")
    return tmp_path


class TestLoading:
    def test_an_auto_file_is_injected_before_the_next_step(self, project: Path, monkeypatch: Any) -> None:
        verdicts = Verdicts(monkeypatch)
        run = node_run(monkeypatch, project)

        action = run.run(turn(read("pkg/mod.py", "r1")), finish())

        assert action.action == ACTION_FINISH
        assert injected(run, 0) == []
        [message] = injected(run, 1)
        assert message is run.requests[1]["messages"][-1]
        assert message["role"] == "user" and message["_dynamic"] is True
        content = message["content"]
        assert content.startswith(HEADER) and content.endswith("[/PROJECT_INSTRUCTIONS]")
        assert "── AGENTS.md（工作区根目录下项目的约定）──\nroot rules" in content
        assert "── pkg/AGENTS.md（pkg/ 目录下项目的约定）──\npkg rules" in content
        assert content.index("root rules") < content.index("pkg rules")
        assert len(message[MESSAGE_KEY]) == 2
        assert verdicts.asked == [("read_file", "AGENTS.md"), ("read_file", "pkg/AGENTS.md")]
        assert answered_once(run.requests[1]["messages"])

    def test_the_policy_question_carries_the_task_identity(self, project: Path, monkeypatch: Any) -> None:
        verdicts = Verdicts(monkeypatch)
        run = node_run(monkeypatch, project)

        run.run(turn(read("pkg/mod.py", "r1")), finish())

        assert {(ctx.task_id, ctx.node_id) for ctx in verdicts.contexts} == {("task-1", "node-1")}

    def test_each_file_is_loaded_once_per_task(self, project: Path, monkeypatch: Any) -> None:
        verdicts = Verdicts(monkeypatch)
        run = node_run(monkeypatch, project)

        run.run(turn(read("pkg/mod.py", "r1")), turn(read("pkg/other.py", "r2")), turn(read("AGENTS.md", "r3")), finish())

        assert verdicts.paths == ["AGENTS.md", "pkg/AGENTS.md"]
        assert [len(injected(run, index)) for index in range(4)] == [0, 1, 1, 1]
        assert len(loaded_events(run)) == 1

    def test_the_event_lists_what_was_loaded(self, project: Path, monkeypatch: Any) -> None:
        Verdicts(monkeypatch, {"pkg/AGENTS.md": "approval_required"})
        run = node_run(monkeypatch, project)

        run.run(turn(read("pkg/mod.py", "r1")), finish())

        [event] = loaded_events(run)
        assert event["files"] == [
            {"path": "AGENTS.md", "status": "loaded", "chars": len("root rules")},
            {"path": "pkg/AGENTS.md", "status": "hinted", "chars": 0},
        ]
        assert event["task_id"] == "task-1" and event["node_id"] == "node-1" and event["step"] == 1

    def test_nothing_is_persisted(self, project: Path, monkeypatch: Any) -> None:
        Verdicts(monkeypatch)
        run = node_run(monkeypatch, project)

        run.run(turn(read("pkg/mod.py", "r1")), finish())

        assert not any("root rules" in str(message.content) or HEADER in str(message.content) for message in stored(project))

    def test_a_file_created_later_is_found_on_a_later_touch(self, tmp_path: Path, monkeypatch: Any) -> None:
        Verdicts(monkeypatch)

        async def create_rules(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            seed(tmp_path / "pkg" / "CLAUDE.md", "late rules")
            return {"ok": True, "data": {"result": "created"}}

        run = node_run(monkeypatch, tmp_path, tools={"read_file": echo("x"), "probe": create_rules})

        run.run(
            turn(read("pkg/a.py", "r1")),
            turn(call("probe", {}, call_id="p1")),
            turn(read("pkg/b.py", "r2")),
            finish(),
        )

        assert [len(injected(run, index)) for index in range(4)] == [0, 0, 0, 1]
        assert "late rules" in injected(run, 3)[0]["content"]


class TestPolicy:
    def test_approval_required_only_mentions_the_file(self, project: Path, monkeypatch: Any) -> None:
        Verdicts(monkeypatch, {"pkg/AGENTS.md": "approval_required"})
        run = node_run(monkeypatch, project)

        run.run(turn(read("pkg/mod.py", "r1")), finish())

        content = injected(run, 1)[0]["content"]
        assert "发现 pkg/AGENTS.md，需要时用 read_file 读取。" in content
        assert "pkg rules" not in content
        assert "root rules" in content

    def test_a_denied_file_is_not_mentioned(self, project: Path, monkeypatch: Any) -> None:
        verdicts = Verdicts(monkeypatch, {"AGENTS.md": "deny", "pkg/AGENTS.md": "deny"})
        run = node_run(monkeypatch, project)

        run.run(turn(read("pkg/mod.py", "r1")), turn(read("pkg/other.py", "r2")), finish())

        assert all(injected(run, index) == [] for index in range(3))
        assert loaded_events(run) == []
        assert verdicts.paths == ["AGENTS.md", "pkg/AGENTS.md"]

    def test_an_unknown_answer_counts_as_deny(self, project: Path, monkeypatch: Any) -> None:
        Verdicts(monkeypatch, {"AGENTS.md": "maybe", "pkg/AGENTS.md": "maybe"})
        run = node_run(monkeypatch, project)

        run.run(turn(read("pkg/mod.py", "r1")), finish())

        assert injected(run, 1) == []

    def test_a_failed_policy_check_is_asked_again_on_the_next_touch(self, project: Path, monkeypatch: Any) -> None:
        verdicts = Verdicts(monkeypatch, {"AGENTS.md": [RuntimeError("supervisor down"), "auto"], "pkg/AGENTS.md": "deny"})
        run = node_run(monkeypatch, project)

        run.run(turn(read("pkg/mod.py", "r1")), turn(read("pkg/other.py", "r2")), finish())

        assert injected(run, 1) == []
        assert "root rules" in injected(run, 2)[0]["content"]
        assert verdicts.paths == ["AGENTS.md", "pkg/AGENTS.md", "AGENTS.md"]


class TestLimits:
    def test_files_are_cut_and_the_task_total_is_capped(self, tmp_path: Path, monkeypatch: Any) -> None:
        seed(tmp_path / "AGENTS.md", "a" * (FILE_CHAR_LIMIT + 1000))
        seed(tmp_path / "pkg" / "AGENTS.md", "b" * FILE_CHAR_LIMIT)
        seed(tmp_path / "pkg" / "sub" / "CLAUDE.md", "c rules")
        Verdicts(monkeypatch)
        run = node_run(monkeypatch, tmp_path)

        run.run(turn(read("pkg/sub/x.py", "r1")), finish())

        content = injected(run, 1)[0]["content"]
        assert "a" * FILE_CHAR_LIMIT in content and "a" * (FILE_CHAR_LIMIT + 1) not in content
        assert f"…（这份说明超过 {FILE_CHAR_LIMIT} 字，后面的没有加载；需要时用 read_file 读 AGENTS.md）" in content
        assert "b" * FILE_CHAR_LIMIT in content
        assert "发现 pkg/sub/CLAUDE.md（项目说明已加载到上限，这份没有自动加载），需要时用 read_file 读取。" in content
        assert "c rules" not in content
        assert [item["status"] for item in loaded_events(run)[0]["files"]] == ["truncated", "loaded", "over_budget"]

    def test_the_remaining_budget_cuts_the_next_file(self, tmp_path: Path, monkeypatch: Any) -> None:
        seed(tmp_path / "AGENTS.md", "a" * FILE_CHAR_LIMIT)
        seed(tmp_path / "pkg" / "AGENTS.md", "b" * 3000)
        seed(tmp_path / "pkg" / "sub" / "AGENTS.md", "c" * 7000)
        Verdicts(monkeypatch)
        run = node_run(monkeypatch, tmp_path)

        run.run(turn(read("pkg/sub/x.py", "r1")), finish())

        content = injected(run, 1)[0]["content"]
        assert "c" * 5000 in content and "c" * 5001 not in content
        assert "后面的没有加载；需要时用 read_file 读 pkg/sub/AGENTS.md" in content

    def test_reading_strips_a_bom_and_normalizes_newlines(self, tmp_path: Path) -> None:
        path = tmp_path / "AGENTS.md"
        path.write_bytes(b"\xef\xbb\xbfline one\r\nline two\r\n")

        assert read_instruction(path, 100) == ("line one\nline two", False)

    def test_an_empty_or_unreadable_file_is_skipped(self, tmp_path: Path) -> None:
        empty = seed(tmp_path / "AGENTS.md", "  \n\n")
        folder = tmp_path / "folder"
        folder.mkdir()

        assert read_instruction(empty, 100) is None
        assert read_instruction(folder, 100) is None
        assert read_instruction(tmp_path / "missing.md", 100) is None


class TestWhereToLook:
    def test_the_walk_stops_at_the_workspace_root(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        seed(tmp_path / "AGENTS.md", "outside the workspace")
        seed(workspace / "AGENTS.md", "root")
        seed(workspace / "a" / "b" / "CLAUDE.md", "deep")

        direct, candidates = discover(workspace, [("a/b/c/file.py", True)], set())

        assert direct == []
        assert [(item.display, item.directory) for item in candidates] == [("AGENTS.md", ""), ("a/b/CLAUDE.md", "a/b")]

    def test_both_names_in_one_directory_are_found(self, tmp_path: Path) -> None:
        seed(tmp_path / "AGENTS.md", "agents")
        seed(tmp_path / "CLAUDE.md", "claude")

        _direct, candidates = discover(tmp_path, [(".", False)], set())

        assert [item.display for item in candidates] == ["AGENTS.md", "CLAUDE.md"]

    def test_a_listed_directory_counts_itself(self, project: Path) -> None:
        _direct, candidates = discover(project, [("pkg", False)], set())

        assert [item.display for item in candidates] == ["AGENTS.md", "pkg/AGENTS.md"]

    def test_policy_extra_roots_are_walked_up_to_their_root(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        extra = tmp_path / "code"
        seed(workspace / "config" / "policy.yaml", yaml.safe_dump({"version": 1, "extra_roots": [str(extra)]}))
        seed(tmp_path / "AGENTS.md", "above the extra root")
        seed(extra / "AGENTS.md", "code root")
        seed(extra / "lib" / "CLAUDE.md", "lib")

        _direct, candidates = discover(workspace, [(str(extra / "lib" / "mod.py"), True)], set())

        expected = [(extra / "AGENTS.md").resolve().as_posix(), (extra / "lib" / "CLAUDE.md").resolve().as_posix()]
        assert [item.display for item in candidates] == expected

    def test_an_untrusted_outside_path_finds_nothing(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        seed(tmp_path / "elsewhere" / "AGENTS.md", "not ours")

        assert discover(workspace, [(str(tmp_path / "elsewhere" / "x.py"), True)], set()) == ([], [])
        assert discover(workspace, [("../elsewhere/x.py", True)], set()) == ([], [])

    def test_a_file_the_model_read_itself_is_not_loaded_again(self, project: Path) -> None:
        direct, candidates = discover(project, [("pkg/AGENTS.md", True)], set())

        assert direct == [os.path.normcase(str((project / "pkg" / "AGENTS.md").resolve()))]
        assert [item.display for item in candidates] == ["AGENTS.md"]

    def test_listing_an_instruction_file_by_name_does_not_count_as_reading_it(self, project: Path) -> None:
        direct, _candidates = discover(project, [("pkg/AGENTS.md", False)], set())

        assert direct == []

    def test_files_already_decided_are_skipped(self, project: Path) -> None:
        handled = {os.path.normcase(str((project / "AGENTS.md").resolve()))}

        _direct, candidates = discover(project, [("pkg/x.py", True)], handled)

        assert [item.display for item in candidates] == ["pkg/AGENTS.md"]

    def test_a_link_to_the_same_file_is_loaded_once(self, tmp_path: Path) -> None:
        seed(tmp_path / "AGENTS.md", "rules")
        try:
            os.symlink(tmp_path / "AGENTS.md", tmp_path / "CLAUDE.md")
        except (OSError, NotImplementedError):
            pytest.skip("this system cannot create symlinks")

        _direct, candidates = discover(tmp_path, [("x.py", True)], set())

        assert [item.display for item in candidates] == ["AGENTS.md"]

    def test_the_model_reading_the_file_directly_skips_it_in_the_loop(self, project: Path, monkeypatch: Any) -> None:
        verdicts = Verdicts(monkeypatch)
        run = node_run(monkeypatch, project)

        run.run(turn(read("pkg/AGENTS.md", "r1")), finish())

        assert verdicts.paths == ["AGENTS.md"]
        assert "pkg rules" not in injected(run, 1)[0]["content"]


class TestTouchedPaths:
    def test_read_file_paths_point_at_files(self) -> None:
        ok = {"ok": True, "data": {"result": "text"}}

        assert touched_paths("read_file", {"files": [{"path": " a.py "}, {"path": "b/c.py"}, "d.py"]}, ok) == [
            ("a.py", True), ("b/c.py", True), ("d.py", True),
        ]
        assert touched_paths("read_file", {"path": "legacy.py"}, ok) == [("legacy.py", True)]

    def test_a_partly_failed_read_keeps_the_files_that_were_read(self) -> None:
        result = {
            "ok": False, "error": "1 个文件读取失败",
            "data": {"result": "...", "results": [
                {"path": "a.py", "success": True},
                {"path": "b.py", "success": False, "error": "denied"},
                {"path": "c.py", "success": True, "omitted": True},
            ]},
        }

        assert touched_paths("read_file", {"files": [{"path": "a.py"}, {"path": "b.py"}, {"path": "c.py"}]}, result) == [
            ("a.py", True),
        ]

    def test_failed_calls_touch_nothing(self) -> None:
        assert touched_paths("write_file", {"path": "x.py"}, failed("denied")) == []
        assert touched_paths("glob", {"pattern": "*.py"}, failed("denied")) == []
        assert touched_paths("read_file", {"path": "x.py"}, failed("denied")) == []

    def test_edits_point_at_files(self) -> None:
        ok = {"ok": True, "data": {"result": "written"}}

        for name in ("write_file", "apply_diff", "edit_lines"):
            assert touched_paths(name, {"path": "src/x.py"}, ok) == [("src/x.py", True)]

    def test_listings_and_searches_point_at_directories(self) -> None:
        ok = {"ok": True, "data": {"result": "listing"}}

        assert touched_paths("list_dir", {"paths": ["a", "b", "a"]}, ok) == [("a", False), ("b", False)]
        assert touched_paths("list_dir", {}, ok) == [(".", False)]
        assert touched_paths("search_in_files", {"query": "x"}, ok) == [(".", False)]
        assert touched_paths("search_in_files", {"query": "x", "path": "src", "pattern": "*.py"}, ok) == [("src", False)]
        assert touched_paths("glob", {"pattern": "engine/builtin/**/*.py"}, ok) == [
            (".", False), ("./engine/builtin", False),
        ]
        assert touched_paths("glob", {"pattern": "**/*.py", "path": "src"}, ok) == [("src", False)]

    def test_a_search_pattern_names_a_workspace_path_and_a_glob_pattern_one_under_path(self) -> None:
        ok = {"ok": True, "data": {"result": "listing"}}

        # search_in_files 的 pattern 和工作区相对路径比，glob 的相对 path
        assert touched_paths(
            "search_in_files", {"query": "x", "path": "engine", "pattern": "engine/builtin/*.py"}, ok,
        ) == [("engine", False), ("engine/builtin", False)]
        assert touched_paths("glob", {"pattern": "builtin/*.py", "path": "engine"}, ok) == [
            ("engine", False), ("engine/builtin", False),
        ]

    def test_other_tools_touch_nothing(self) -> None:
        assert touched_paths("execute_command", {"command": "cat AGENTS.md"}, {"ok": True}) == []
        assert touched_paths("read_tool_output", {"ref": "x"}, {"ok": True}) == []


class TestWhenItIsOff:
    def test_system_tasks_load_nothing(self, project: Path, monkeypatch: Any) -> None:
        verdicts = Verdicts(monkeypatch)
        run = node_run(monkeypatch, project, task_context={"is_system_task": True})

        run.run(turn(read("pkg/mod.py", "r1")), finish())

        assert verdicts.asked == []
        assert all(injected(run, index) == [] for index in range(2))

    @pytest.mark.parametrize("switch", [False, "off", "false", "no"])
    def test_a_node_can_turn_it_off(self, project: Path, monkeypatch: Any, switch: Any) -> None:
        verdicts = Verdicts(monkeypatch)
        run = node_run(monkeypatch, project, extra={"project_instructions": switch})

        run.run(turn(read("pkg/mod.py", "r1")), finish())

        assert verdicts.asked == []
        assert injected(run, 1) == []

    def test_a_failed_tool_call_is_not_collected(self, project: Path, monkeypatch: Any) -> None:
        verdicts = Verdicts(monkeypatch)

        async def broken(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            return failed("cannot read")

        run = node_run(monkeypatch, project, tools={"read_file": broken})

        run.run(turn(read("pkg/mod.py", "r1")), finish())

        assert verdicts.asked == []
        assert injected(run, 1) == []


class TestKeepingItVisible:
    def test_instructions_dropped_with_the_dynamic_context_come_back(self, project: Path, monkeypatch: Any) -> None:
        class DropDynamic:
            """抢占注入会这样清掉全部动态消息。"""

            name = "drop_dynamic"
            priority = 60

            async def handle(self, ctx: Any) -> None:
                if ctx.step == 2:
                    loop_state = ctx.extra["loop_state"]
                    loop_state.messages[:] = [message for message in loop_state.messages if not message.get("_dynamic")]

        verdicts = Verdicts(monkeypatch)
        run = node_run(monkeypatch, project, more={"before_step": [DropDynamic()]})

        run.run(turn(read("pkg/mod.py", "r1")), turn(call("probe", {}, call_id="p1")), finish())

        [again] = injected(run, 2)
        assert again is run.requests[2]["messages"][-1]
        assert "root rules" in again["content"] and "pkg rules" in again["content"]
        assert verdicts.paths == ["AGENTS.md", "pkg/AGENTS.md"]
        assert len(loaded_events(run)) == 1

    def test_a_resumed_task_gets_them_back_on_its_first_step(self, project: Path, monkeypatch: Any) -> None:
        verdicts = Verdicts(monkeypatch)
        node_run(monkeypatch, project).run(turn(read("pkg/mod.py", "r1")), finish())

        resumed = node_run(monkeypatch, project)
        resumed.run(finish())

        [message] = injected(resumed, 0)
        assert "root rules" in message["content"] and "pkg rules" in message["content"]
        assert verdicts.paths == ["AGENTS.md", "pkg/AGENTS.md"]

    def test_a_finished_task_forgets_them(self, project: Path, monkeypatch: Any) -> None:
        Verdicts(monkeypatch)
        handler = ProjectInstructions()
        node_run(monkeypatch, project, handler=handler, more={"on_task_end": [handler.forget]}).run(
            turn(read("pkg/mod.py", "r1")), finish(),
        )

        resumed = node_run(monkeypatch, project)
        resumed.run(finish())

        assert injected(resumed, 0) == []

    def test_forget_without_a_task_id_is_harmless(self) -> None:
        asyncio.run(ProjectInstructions().forget(SimpleNamespace(rctx=SimpleNamespace(task_id=""))))
