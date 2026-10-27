"""委派时要求隔离：节点 YAML 写了 isolation: worktree 的目标才多 isolation、repo 两个参数，有这样的目标才给合回工具；
两个参数随 wait 批次项和异步 dispatch_origin 交给 supervisor，写错的、只给 repo 的、已在副本里又要求的当场拒绝。
子任务交回的副本说明按调用方工具表里有没有合回工具来写怎么合回。"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import config_layout  # noqa: E402
from _engine_loop_fixture import (  # noqa: E402
    NodeRun,
    ScriptedRegistry,
    answered_once,
    call,
    echo,
    make_loop_state,
    make_rctx,
    results_for,
    turn,
    use_hooks,
)
from _worktree_fixture import (  # noqa: E402
    Env,
    env,  # noqa: F401
    item,
    requires_git,
)
from engine.dispatch_batch import (  # noqa: E402
    ISOLATION_KEY,
    ISOLATION_WORKTREE,
    REPO_KEY,
    dispatch_isolation,
    dispatch_repo,
)
from engine.inference.ai_step import _handle_tool_calls  # noqa: E402
from engine.inference.pseudo_handlers import prepare_wait_dispatches  # noqa: E402
from engine import runner  # noqa: E402
from engine.inference.pseudo_tools import (  # noqa: E402
    _dispatch_delegate_spec,
    _dispatch_delegate_specs,
    _filter_tool_specs,
    isolatable_node,
    isolating_targets,
    merge_tool_offered,
    plan_delegation,
    tool_allowed,
)
from engine.isolation_notice import isolation_parts  # noqa: E402
from engine.node import Node, ToolAccess, load_node  # noqa: E402
from engine.project_root import INBOUND_CHILD_WORKTREE_KEY, ISOLATION_CONTEXT_KEY  # noqa: E402
from engine.protocol import ACTION_DISPATCH, TaskAction  # noqa: E402
from engine.runner import _collect_node_info, _discover_switchable_nodes  # noqa: E402
from engine.worktree_report import MERGE_TOOL  # noqa: E402
from shell.tui.services.node_scanner import find_root_nodes, scan_nodes  # noqa: E402
from toolbox.registry import ToolRegistry  # noqa: E402

CODER = "coder"
SCOUT = "scout"
REPO = "E:/repos/lib"
HANDLE = "0f1e2d3c-4b5a-4978-8796-a5b4c3d2e1f0@child_9c2f"
KEPT_AGENT = {
    "ok": True, "session_id": "child_9c2f", "task_id": "0f1e2d3c-4b5a-4978-8796-a5b4c3d2e1f0",
    "kind": "agent", "node_id": CODER, "context_mode": "fresh", "conversation_key": "agent:coder:qq_group:1:abc",
}
IN_WORKTREE = {ISOLATION_CONTEXT_KEY: {"mode": "worktree", "worktree": "wt-1", "path": "/tmp/wt-1"}}
NESTED_REFUSAL = "没有派出：你已经在隔离副本 wt-1 里，子任务会沿用这个副本，不能再要求隔离；去掉 isolation 和 repo 再派。"


def _info(target: str, isolatable: Any = None) -> dict[str, Any]:
    """runner 交给推理循环的下游节点信息；isolatable 不给就是加这个字段以前的样子。"""
    row: dict[str, Any] = {"id": target, "name": target.title(), "description": f"{target} 节点"}
    if isolatable is not None:
        row["isolatable"] = isolatable
    return row


DOWNSTREAM = [_info(CODER, True), _info(SCOUT, False)]


def _node(*, access: ToolAccess | None = None, targets: tuple[str, ...] = (CODER, SCOUT)) -> Node:
    return Node(
        id="lead", type="ai", prompt="Work carefully.", tool_mode="native",
        tool_access=access or ToolAccess(mode="allowlist", allow=["probe"]),
        delegate_targets=list(targets),
    )


def _registry() -> ScriptedRegistry:
    return ScriptedRegistry(tools={"probe": echo(), MERGE_TOOL: echo("merged")})


def _finish() -> Any:
    return turn(call("finish", {"text": "ok"}, call_id="f"))


def _offered(run: NodeRun, name: str, request: int = 0) -> dict[str, Any]:
    for tool in run.requests[request]["tools"]:
        function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        if function.get("name") == name:
            return function
    raise AssertionError(f"{name} is not offered")


def _parameters(run: NodeRun, target: str) -> dict[str, Any]:
    return _offered(run, f"dispatch_to_{target}")["parameters"]["properties"]


def _workspace_dir(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    return path


class TestToolSpecs:
    @pytest.mark.parametrize("allow_wait", [True, False], ids=["wait-and-async", "async-only"])
    def test_an_isolatable_target_takes_isolation_and_repo(self, allow_wait: bool) -> None:
        function = _dispatch_delegate_spec(CODER, _info(CODER, True), allow_wait=allow_wait, isolation=True)["function"]

        properties = function["parameters"]["properties"]
        assert properties[ISOLATION_KEY]["type"] == "string"
        assert properties[ISOLATION_KEY]["enum"] == [ISOLATION_WORKTREE]
        assert properties[REPO_KEY]["type"] == "string"
        for phrase in ("git worktree", "HEAD", "continue_from", MERGE_TOOL, "只读、查询、生图这类任务不要用"):
            assert phrase in properties[ISOLATION_KEY]["description"], phrase
        assert "extra_roots" in properties[REPO_KEY]["description"]
        assert function["parameters"]["required"] == ["instruction"]
        assert f"满意就调用 {MERGE_TOOL} 合回" in function["description"]
        assert "abandon=true" in function["description"] and "不要绕开合回" in function["description"]

    @pytest.mark.parametrize("allow_wait", [True, False], ids=["wait-and-async", "async-only"])
    def test_apart_from_them_the_tool_is_byte_for_byte_the_same(self, allow_wait: bool) -> None:
        plain = _dispatch_delegate_spec(CODER, _info(CODER, True), allow_wait=allow_wait)
        isolated = copy.deepcopy(_dispatch_delegate_spec(CODER, _info(CODER, True), allow_wait=allow_wait, isolation=True))

        function = isolated["function"]
        del function["parameters"]["properties"][ISOLATION_KEY]
        del function["parameters"]["properties"][REPO_KEY]
        review = f"带 {ISOLATION_KEY}={ISOLATION_WORKTREE} 派出的"
        function["description"] = "\n".join(
            line for line in function["description"].split("\n") if not line.startswith(review)
        )

        assert json.dumps(isolated, ensure_ascii=False) == json.dumps(plain, ensure_ascii=False)

    def test_only_the_targets_offered_isolation_change(self) -> None:
        before = _dispatch_delegate_specs([CODER, SCOUT], DOWNSTREAM)
        after = _dispatch_delegate_specs([CODER, SCOUT], DOWNSTREAM, isolating=[CODER])

        assert after[1] == before[1]
        assert ISOLATION_KEY not in before[0]["function"]["parameters"]["properties"]
        assert {ISOLATION_KEY, REPO_KEY} <= set(after[0]["function"]["parameters"]["properties"])

    def test_a_node_without_the_merge_tool_is_told_to_hand_the_diff_on(self) -> None:
        function = _dispatch_delegate_spec(CODER, _info(CODER, True), isolation=True, can_merge=False)["function"]

        assert f"这个节点不能调用 {MERGE_TOOL}" in function["description"]
        assert f"这个节点不能调用 {MERGE_TOOL}" in function["parameters"]["properties"][ISOLATION_KEY]["description"]
        assert "abandon=true" not in function["description"]


class TestIsolatableTargets:
    @pytest.mark.parametrize("extra, expected", [
        pytest.param({ISOLATION_KEY: ISOLATION_WORKTREE}, True, id="worktree"),
        pytest.param({ISOLATION_KEY: "  WorkTree "}, True, id="case-and-spaces"),
        pytest.param({}, False, id="not-written"),
        pytest.param({ISOLATION_KEY: None}, False, id="left-empty"),
    ])
    def test_only_an_explicit_worktree_counts(self, extra: dict[str, Any], expected: bool) -> None:
        node = Node(id="target", type="ai", tool_access=ToolAccess(mode="all"), extra=dict(extra))

        assert isolatable_node(node) is expected

    @pytest.mark.parametrize("value", ["copy", "none", True, 1, [ISOLATION_WORKTREE]])
    def test_any_other_value_is_ignored_and_logged(self, value: Any, caplog: pytest.LogCaptureFixture) -> None:
        node = Node(id="target", type="ai", extra={ISOLATION_KEY: value})

        with caplog.at_level(logging.WARNING, logger="engine.inference.pseudo_tools"):
            assert isolatable_node(node) is False

        assert "node target has an invalid isolation" in caplog.text

    def test_tools_that_change_the_project_do_not_make_a_node_isolatable(self) -> None:
        node = Node(
            id="target", type="ai", tool_access=ToolAccess(mode="allowlist", allow=["write_file", "execute_command"]),
        )

        assert isolatable_node(node) is False

    def test_among_the_shipped_nodes_only_the_coder_opts_in(self, tmp_path: Path) -> None:
        # 空工作区里加载：拿到的是仓库默认那份，不会被本机 config/nodes 里的同名节点顶掉
        loaded = [load_node(tmp_path, path.stem) for path in sorted(config_layout.default_nodes_dir().glob("*.yaml"))]
        nodes = [node for node in loaded if node is not None]

        assert {"bootstrap.coder", "bootstrap.code_reviewer", "qq.orchestrator"} <= {node.id for node in nodes}
        assert [node.id for node in nodes if isolatable_node(node)] == ["bootstrap.coder"]


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # 仓库自带的 defaults/nodes 排在实例目录后面，不挡住就会混进这个假工作区的节点表
    monkeypatch.setattr(config_layout, "CODE_ROOT", tmp_path)
    (tmp_path / "config" / "nodes").mkdir(parents=True)
    (tmp_path / "engine" / "system_nodes").mkdir(parents=True)
    return tmp_path


def _write_node(workspace: Path, node_id: str, **fields: Any) -> Path:
    path = workspace / "config" / "nodes" / f"{node_id}.yaml"
    path.write_text(
        yaml.safe_dump({"id": node_id, "type": "ai", **fields}, sort_keys=False, allow_unicode=True), encoding="utf-8",
    )
    return path


def _touch_later(path: Path) -> None:
    stamp = os.stat(path).st_mtime_ns + 1_000_000_000
    os.utime(path, ns=(stamp, stamp))


def _bare_row(node_id: str) -> dict[str, Any]:
    return {"id": node_id, "name": node_id, "description": node_id, "isolatable": False}


class TestNodeInfo:
    def test_every_row_says_whether_the_target_can_be_isolated(self, workspace: Path) -> None:
        _write_node(
            workspace, "t.edit", name="改代码", description="会改文件", isolation=ISOLATION_WORKTREE,
            tool_access={"mode": "allowlist", "allow": ["read_file", "apply_diff"]},
        )
        _write_node(workspace, "t.all", tool_access={"mode": "all"})
        _write_node(workspace, "t.typo", isolation="copy", tool_access={"mode": "all"})

        rows = _collect_node_info(workspace, ["t.edit", "t.all", "t.typo", "t.missing"])

        assert rows[0] == {"id": "t.edit", "name": "改代码", "description": "会改文件", "isolatable": True}
        assert [row["isolatable"] for row in rows[1:]] == [False, False, False]
        assert rows[-1] == _bare_row("t.missing")

    def test_an_edit_to_the_node_file_is_picked_up(self, workspace: Path) -> None:
        path = _write_node(workspace, "t.flip", tool_access={"mode": "all"})
        [first] = _collect_node_info(workspace, ["t.flip"])
        assert first["isolatable"] is False
        assert _collect_node_info(workspace, ["t.flip"])[0] is first

        _write_node(workspace, "t.flip", isolation=ISOLATION_WORKTREE, tool_access={"mode": "all"})
        _touch_later(path)

        [second] = _collect_node_info(workspace, ["t.flip"])
        assert second["isolatable"] is True

    @pytest.mark.parametrize("fields", [
        pytest.param({"kind": "workflow"}, id="not-a-node"),
        pytest.param({"type": "router"}, id="neither-ai-nor-tool"),
    ])
    def test_a_target_the_engine_cannot_run_keeps_the_bare_row(self, workspace: Path, fields: dict[str, Any]) -> None:
        _write_node(workspace, "t.other", name="别的", isolation=ISOLATION_WORKTREE, **fields)

        assert _collect_node_info(workspace, ["t.other"]) == [_bare_row("t.other")]

    def test_a_file_that_does_not_parse_keeps_the_bare_row(self, workspace: Path) -> None:
        (workspace / "config" / "nodes" / "t.broken.yaml").write_text("id: [unclosed\n", encoding="utf-8")

        assert _collect_node_info(workspace, ["t.broken"]) == [_bare_row("t.broken")]


class TestSelfDelegation:
    def test_a_node_that_delegates_to_itself_is_still_an_entry(self, workspace: Path) -> None:
        _write_node(workspace, "w.coder", delegate_targets=["w.reviewer", "w.coder"])
        _write_node(workspace, "w.reviewer")

        assert {row["id"] for row in _discover_switchable_nodes(workspace, "someone.else")} == {"w.coder"}
        assert [node.id for node in find_root_nodes(scan_nodes(workspace))] == ["w.coder"]

    def test_delegated_by_another_node_it_is_not_one(self, workspace: Path) -> None:
        _write_node(workspace, "w.boss", delegate_targets=["w.coder"])
        _write_node(workspace, "w.coder", delegate_targets=["w.coder"])

        assert {row["id"] for row in _discover_switchable_nodes(workspace, "someone.else")} == {"w.boss"}
        assert [node.id for node in find_root_nodes(scan_nodes(workspace))] == ["w.boss"]


class TestMergeTool:
    def test_tool_access_alone_never_grants_it(self) -> None:
        everything = Node(id="n", type="ai", tool_access=ToolAccess(mode="all"))
        listed = Node(id="n", type="ai", tool_access=ToolAccess(mode="allowlist", allow=[MERGE_TOOL, "probe"]))

        for node in (everything, listed):
            assert tool_allowed(node, MERGE_TOOL) is False
            assert [spec["name"] for spec in _filter_tool_specs(node, [{"name": MERGE_TOOL}, {"name": "probe"}])] == ["probe"]

    def test_it_follows_isolatable_targets_unless_denied(self) -> None:
        denied = _node(access=ToolAccess(mode="allowlist", allow=["probe"], deny=[MERGE_TOOL]))

        assert merge_tool_offered(_node(), [CODER]) is True
        assert merge_tool_offered(_node(), []) is False
        assert merge_tool_offered(denied, [CODER]) is False

    def test_targets_are_picked_from_the_node_info(self) -> None:
        rows = [*DOWNSTREAM, _info("legacy"), _info("loose", "true")]
        targets = [CODER, SCOUT, "legacy", "loose", "ghost", CODER]

        assert isolating_targets(targets, rows, in_isolation=False) == [CODER]
        assert isolating_targets(targets, rows, in_isolation=True) == []
        assert isolating_targets(targets, None, in_isolation=False) == []


class TestWholeNode:
    def test_an_isolatable_target_brings_merge_and_the_two_parameters(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run = NodeRun(monkeypatch, tmp_path, registry=_registry(), node=_node())

        run.run(_finish(), downstream_info=DOWNSTREAM)

        assert MERGE_TOOL in run.tool_names(0)
        assert {ISOLATION_KEY, REPO_KEY} <= set(_parameters(run, CODER))
        assert ISOLATION_KEY not in _parameters(run, SCOUT) and REPO_KEY not in _parameters(run, SCOUT)

    def test_without_one_the_tool_list_is_what_it_was(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        before = NodeRun(monkeypatch, _workspace_dir(tmp_path, "before"), registry=_registry(), node=_node())
        before.run(_finish(), downstream_info=[_info(CODER), _info(SCOUT)])
        after = NodeRun(monkeypatch, _workspace_dir(tmp_path, "after"), registry=_registry(), node=_node())
        after.run(_finish(), downstream_info=[_info(CODER, False), _info(SCOUT, False)])

        assert after.requests[0]["tools"] == before.requests[0]["tools"]
        assert MERGE_TOOL not in after.tool_names(0)

    def test_mode_all_alone_does_not_bring_merge(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        run = NodeRun(
            monkeypatch, tmp_path, registry=_registry(), node=_node(access=ToolAccess(mode="all"), targets=(SCOUT,)),
        )

        run.run(_finish(), downstream_info=DOWNSTREAM)

        names = run.tool_names(0)
        assert "probe" in names and MERGE_TOOL not in names

    def test_an_explicit_deny_is_honoured(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        node = _node(access=ToolAccess(mode="allowlist", allow=["probe"], deny=[MERGE_TOOL]))
        run = NodeRun(monkeypatch, tmp_path, registry=_registry(), node=node)

        run.run(_finish(), downstream_info=DOWNSTREAM)

        assert MERGE_TOOL not in run.tool_names(0)
        assert ISOLATION_KEY in _parameters(run, CODER)
        assert f"这个节点不能调用 {MERGE_TOOL}" in _offered(run, f"dispatch_to_{CODER}")["description"]

    def test_a_task_in_a_worktree_gets_neither(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        inside = NodeRun(
            monkeypatch, _workspace_dir(tmp_path, "inside"), registry=_registry(), node=_node(), task_context=IN_WORKTREE,
        )
        inside.run(_finish(), downstream_info=DOWNSTREAM)
        outside = NodeRun(monkeypatch, _workspace_dir(tmp_path, "outside"), registry=_registry(), node=_node())
        outside.run(_finish(), downstream_info=[_info(CODER), _info(SCOUT)])

        assert MERGE_TOOL not in inside.tool_names(0)
        assert _offered(inside, f"dispatch_to_{CODER}") == _offered(outside, f"dispatch_to_{CODER}")

    def test_a_system_task_gets_the_parameters_on_its_async_only_tools(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run = NodeRun(
            monkeypatch, tmp_path, registry=_registry(), node=_node(), task_context={"is_system_task": True},
        )

        run.run(_finish(), downstream_info=DOWNSTREAM)

        properties = _parameters(run, CODER)
        assert "wait" not in properties and ISOLATION_KEY in properties

    def test_merge_runs_when_offered_and_is_refused_otherwise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def merging() -> Any:
            return turn(call(MERGE_TOOL, {"handle": "wt-1"}, call_id="m1"))

        offered = NodeRun(monkeypatch, _workspace_dir(tmp_path, "offered"), registry=_registry(), node=_node())
        offered.run(merging(), _finish(), downstream_info=DOWNSTREAM)
        refused = NodeRun(
            monkeypatch, _workspace_dir(tmp_path, "refused"), registry=_registry(), node=_node(targets=(SCOUT,)),
        )
        refused.run(merging(), _finish(), downstream_info=DOWNSTREAM)

        assert offered.registry.executed_names() == [MERGE_TOOL]
        assert refused.registry.executed_names() == []
        [answer] = results_for(refused.requests[1]["messages"], "m1")
        assert "not in this node's allowed tool list" in str(answer["content"])


class TestTheShippedCoder:
    @staticmethod
    def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **options: Any) -> NodeRun:
        coder = load_node(tmp_path, "bootstrap.coder")
        assert coder is not None
        registry = ScriptedRegistry(specs={name: {} for name in [*coder.tool_access.allow, MERGE_TOOL]})
        downstream = _collect_node_info(tmp_path, list(coder.delegate_targets))
        # 只读的审查就地看改动，不给隔离；子 coder 给
        assert [(row["id"], row["isolatable"]) for row in downstream] == [
            ("bootstrap.code_reviewer", False), ("bootstrap.coder", True),
        ]
        run = NodeRun(monkeypatch, tmp_path, registry=registry, node=coder, **options)
        run.run(_finish(), downstream_info=downstream)
        return run

    def test_it_can_hand_parts_to_isolated_sub_coders_and_merge_them(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run = self._run(tmp_path, monkeypatch)

        assert MERGE_TOOL in run.tool_names(0)
        own = _offered(run, "dispatch_to_bootstrap_coder")["parameters"]["properties"]
        reviewer = _offered(run, "dispatch_to_bootstrap_code_reviewer")["parameters"]["properties"]
        assert ISOLATION_KEY in own and ISOLATION_KEY not in reviewer
        # 派给子 coder 不能沿用会话，派给审查照旧
        assert own["context_mode"]["enum"] == ["fresh", "fork"] and "context_key" not in own
        assert reviewer["context_mode"]["enum"] == ["fresh", "fork", "accumulate"] and "context_key" in reviewer

    def test_inside_a_worktree_it_cannot_split_further(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        run = self._run(tmp_path, monkeypatch, task_context=IN_WORKTREE)

        assert MERGE_TOOL not in run.tool_names(0)
        assert ISOLATION_KEY not in _offered(run, "dispatch_to_bootstrap_coder")["parameters"]["properties"]


def _dispatching(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    task_context: dict[str, Any] | None = None,
    isolating: tuple[str, ...] = (CODER,),
) -> Any:
    """推理循环建好后的状态：两个委派目标里只有 coder 的工具带了隔离参数。"""
    use_hooks(monkeypatch)
    ls = make_loop_state(
        workspace, ScriptedRegistry(tools={"probe": echo()}), rctx=make_rctx(workspace, task_context=task_context),
    )
    ls.allowed_dispatch_targets = {CODER, SCOUT}
    ls.isolating_targets = set(isolating)
    return ls


def _run(ls: Any, *tool_calls: Any) -> Any:
    return asyncio.run(_handle_tool_calls(ls, turn(*tool_calls), 1, final_step=False))


def _wait(target: str = CODER, *, call_id: str = "w1", **extra: Any) -> Any:
    return call(f"dispatch_to_{target}", {"instruction": "改解析器", "wait": True, **extra}, call_id=call_id)


def _async(target: str = CODER, *, call_id: str = "a1", **extra: Any) -> Any:
    return call(f"dispatch_to_{target}", {"instruction": "改解析器", **extra}, call_id=call_id)


def _payload(ls: Any, call_id: str) -> dict[str, Any]:
    [message] = results_for(ls.messages, call_id)
    return json.loads(message["content"])


def _response(body: Any, status: int = 200) -> SimpleNamespace:
    return SimpleNamespace(status_code=status, text=json.dumps(body, ensure_ascii=False), json=lambda: body)


def _supervisor(continuation: dict[str, Any] | None = None) -> SimpleNamespace:
    """顶替 rctx.http：GET 是 continue_from 核对，POST 是异步委派的入站。"""
    accepted = _response({"session_id": "agent-session-1", "task_id": "child-1", "accepted": True})
    return SimpleNamespace(get=AsyncMock(return_value=_response(continuation or {})), post=AsyncMock(return_value=accepted))


def _refusal_of_wait(ls: Any, dispatch: Any) -> str:
    plan = asyncio.run(prepare_wait_dispatches(ls, [dispatch]))
    assert plan.items == []
    [(_refused, text)] = plan.rejected
    return json.loads(text)["error"]


def _refusal_of_async(ls: Any, dispatch: Any) -> str:
    ls.rctx.http = _supervisor()
    _run(ls, dispatch)
    assert ls.rctx.http.post.await_count == 0
    payload = _payload(ls, dispatch.id)
    assert payload["success"] is False
    return payload["error"]


_BAD_ARGUMENTS = [
    pytest.param(
        {ISOLATION_KEY: "copy"},
        '没有派出：isolation 只能填 worktree（让目标节点在自己的 git worktree 里做），不要隔离就不填；收到的是 "copy"。',
        id="unknown-value",
    ),
    pytest.param(
        {ISOLATION_KEY: True},
        "没有派出：isolation 只能填 worktree（让目标节点在自己的 git worktree 里做），不要隔离就不填；收到的是 true。",
        id="boolean",
    ),
    pytest.param(
        {ISOLATION_KEY: "none"},
        '没有派出：isolation 只能填 worktree（让目标节点在自己的 git worktree 里做），不要隔离就不填；收到的是 "none"。',
        id="not-silently-read-as-no-isolation",
    ),
    pytest.param(
        {REPO_KEY: REPO},
        "没有派出：repo 只在 isolation=worktree 时有效；要隔离就同时填 isolation: worktree，不隔离就去掉 repo。",
        id="repo-alone",
    ),
    pytest.param(
        {ISOLATION_KEY: ISOLATION_WORKTREE, REPO_KEY: ["E:/a"]},
        "没有派出：repo 要填仓库的绝对路径。",
        id="repo-not-a-string",
    ),
]


class TestHandling:
    def test_a_wait_dispatch_carries_isolation_in_its_batch_item(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls = _dispatching(tmp_path, monkeypatch)

        action = _run(ls, _wait(isolation=ISOLATION_WORKTREE))

        assert action is not None and action.action == ACTION_DISPATCH
        [batch_item] = action.dispatch_batch
        assert batch_item == {
            "kind": "node", "target": CODER, "instruction": "改解析器", "context_mode": "fresh",
            ISOLATION_KEY: ISOLATION_WORKTREE, "call_id": "w1",
        }
        assert dispatch_isolation(batch_item) == ISOLATION_WORKTREE and dispatch_repo(batch_item) == ""
        payload = _payload(ls, "w1")
        assert payload["success"] is True and payload["status"] == "waiting"
        assert payload["message"].startswith(f"已派出给 {CODER}（在隔离副本里执行），正在等待结果")
        assert payload[ISOLATION_KEY] == ISOLATION_WORKTREE
        assert answered_once(ls.messages)

    def test_the_value_is_normalised_and_the_repo_trimmed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls = _dispatching(tmp_path, monkeypatch)

        action = _run(ls, _wait(isolation="  WorkTree ", repo=f"  {REPO}  "))

        [batch_item] = action.dispatch_batch
        assert batch_item[ISOLATION_KEY] == ISOLATION_WORKTREE and batch_item[REPO_KEY] == REPO
        assert dispatch_repo(batch_item) == REPO
        assert f"（在隔离副本里执行，仓库 {REPO}）" in _payload(ls, "w1")["message"]

    def test_an_async_dispatch_carries_isolation_in_its_dispatch_origin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls = _dispatching(tmp_path, monkeypatch)
        ls.rctx.http = _supervisor()

        _run(ls, _async(isolation=ISOLATION_WORKTREE, repo=REPO))

        origin = ls.rctx.http.post.await_args.kwargs["json"]["dispatch_origin"]
        assert origin[ISOLATION_KEY] == ISOLATION_WORKTREE and origin[REPO_KEY] == REPO
        assert dispatch_isolation(origin) == ISOLATION_WORKTREE and dispatch_repo(origin) == REPO
        payload = _payload(ls, "a1")
        assert payload["success"] is True and payload["handle"] == "child-1@agent-session-1"
        assert payload["message"].startswith(f"已异步委派给 {CODER}（在隔离副本里执行，仓库 {REPO}），结果稍后")
        assert payload[ISOLATION_KEY] == ISOLATION_WORKTREE and payload[REPO_KEY] == REPO

    @pytest.mark.parametrize("extra", [
        pytest.param({}, id="absent"),
        pytest.param({ISOLATION_KEY: ""}, id="blank"),
        pytest.param({ISOLATION_KEY: None, REPO_KEY: "   "}, id="null-and-blank-repo"),
    ])
    def test_without_isolation_nothing_is_added(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: dict[str, Any],
    ) -> None:
        waiting = _dispatching(tmp_path, monkeypatch)
        action = _run(waiting, _wait(**extra))
        sending = _dispatching(tmp_path, monkeypatch)
        sending.rctx.http = _supervisor()
        _run(sending, _async(**extra))

        assert action.dispatch_batch == [{
            "kind": "node", "target": CODER, "instruction": "改解析器", "context_mode": "fresh", "call_id": "w1",
        }]
        origin = sending.rctx.http.post.await_args.kwargs["json"]["dispatch_origin"]
        assert ISOLATION_KEY not in origin and REPO_KEY not in origin
        for payload in (_payload(waiting, "w1"), _payload(sending, "a1")):
            assert payload["success"] is True and ISOLATION_KEY not in payload
            assert "隔离" not in payload["message"]

    @pytest.mark.parametrize("arguments, reason", _BAD_ARGUMENTS)
    def test_a_bad_wait_dispatch_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any], reason: str,
    ) -> None:
        ls = _dispatching(tmp_path, monkeypatch)

        assert _refusal_of_wait(ls, _wait(**arguments)) == reason

    @pytest.mark.parametrize("arguments, reason", _BAD_ARGUMENTS)
    def test_a_bad_async_dispatch_is_refused_before_anything_is_sent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any], reason: str,
    ) -> None:
        ls = _dispatching(tmp_path, monkeypatch)

        assert _refusal_of_async(ls, _async(**arguments)) == reason

    def test_a_refused_item_does_not_hold_back_the_rest_of_the_batch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls = _dispatching(tmp_path, monkeypatch)

        action = _run(ls, _wait(call_id="w1", isolation="copy"), _wait(SCOUT, call_id="w2"))

        assert [entry["call_id"] for entry in action.dispatch_batch] == ["w2"]
        assert _payload(ls, "w1")["success"] is False
        assert answered_once(ls.messages)

    def test_a_target_that_does_not_opt_in_is_not_isolated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls = _dispatching(tmp_path, monkeypatch)
        reason = f"没有派出：{SCOUT} 的节点配置没有写 isolation: worktree，委派它不能要求隔离；去掉 isolation 和 repo 再派。"

        assert _refusal_of_wait(ls, _wait(SCOUT, isolation=ISOLATION_WORKTREE)) == reason
        assert _refusal_of_async(ls, _async(SCOUT, isolation=ISOLATION_WORKTREE)) == reason


class TestNested:
    @pytest.mark.parametrize("arguments", [
        pytest.param({ISOLATION_KEY: ISOLATION_WORKTREE}, id="isolation"),
        pytest.param({ISOLATION_KEY: ISOLATION_WORKTREE, REPO_KEY: REPO}, id="isolation-and-repo"),
        pytest.param({REPO_KEY: REPO}, id="repo-alone"),
        pytest.param({ISOLATION_KEY: "copy"}, id="unknown-value"),
    ])
    def test_a_task_in_a_worktree_cannot_ask_for_another(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any],
    ) -> None:
        # 推理循环不会给它这两个参数；照着旧历史硬塞进来的，也按沿用当前副本拒绝
        ls = _dispatching(tmp_path, monkeypatch, task_context=IN_WORKTREE)

        assert _refusal_of_wait(ls, _wait(**arguments)) == NESTED_REFUSAL
        assert _refusal_of_async(ls, _async(**arguments)) == NESTED_REFUSAL

    def test_a_plain_dispatch_from_a_worktree_still_goes_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls = _dispatching(tmp_path, monkeypatch, task_context=IN_WORKTREE, isolating=())

        action = _run(ls, _wait())

        [batch_item] = action.dispatch_batch
        assert ISOLATION_KEY not in batch_item and REPO_KEY not in batch_item


SELF = "node-1"


def _self_dispatching(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """委派清单里列着自己：make_loop_state 给的节点就是 SELF。"""
    ls = _dispatching(workspace, monkeypatch)
    assert ls.node.id == SELF
    ls.allowed_dispatch_targets.add(SELF)
    return ls


class TestSelfDispatch:
    def test_the_tool_to_oneself_offers_only_modes_with_their_own_session(self) -> None:
        own, other = _dispatch_delegate_specs([SELF, SCOUT], [_info(SELF), _info(SCOUT)], caller_id=SELF)

        own_properties = own["function"]["parameters"]["properties"]
        assert own_properties["context_mode"]["enum"] == ["fresh", "fork"]
        assert "context_key" not in own_properties
        assert "context_mode 不再生效" in own_properties["continue_from"]["description"]
        assert "目标就是你自己这个节点" in own["function"]["description"]
        assert other == _dispatch_delegate_specs([SCOUT], [_info(SCOUT)])[0]
        assert _dispatch_delegate_specs([SELF], [_info(SELF)]) == _dispatch_delegate_specs([SELF], [_info(SELF)], caller_id="lead")

    def test_without_a_mode_it_is_fresh_even_for_a_persistent_node(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_node(workspace, SELF, persistent=True)
        _write_node(workspace, SCOUT, persistent=True)
        ls = _self_dispatching(workspace, monkeypatch)

        action = _run(ls, _wait(SELF, call_id="w1"), _wait(SCOUT, call_id="w2"))

        assert [(entry["target"], entry["context_mode"]) for entry in action.dispatch_batch] == [
            (SELF, "fresh"), (SCOUT, "accumulate"),
        ]

    @pytest.mark.parametrize("mode", ["fresh", "fork"])
    def test_a_mode_with_its_own_session_goes_out(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
    ) -> None:
        ls = _self_dispatching(workspace, monkeypatch)

        [entry] = _run(ls, _wait(SELF, context_mode=mode)).dispatch_batch

        assert entry["context_mode"] == mode

    @pytest.mark.parametrize("mode", ["accumulate", "Fresh", "reuse"])
    def test_a_mode_that_would_share_a_session_is_refused(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
    ) -> None:
        # 认不出的取值 supervisor 按 accumulate 沿用子会话，一样拦下
        ls = _self_dispatching(workspace, monkeypatch)
        reason = (
            f"没有派出：委派给自己时 context_mode 只能是 fresh 或 fork，收到的是 {json.dumps(mode)}；"
            "沿用会话的话，同时派出的几份、以及它们再派出的，都会落进同一个会话。"
            "要接着之前派出的那一份，用 continue_from 带上它的句柄。"
        )

        assert _refusal_of_wait(ls, _wait(SELF, context_mode=mode)) == reason
        assert _refusal_of_async(ls, _async(SELF, context_mode=mode)) == reason

    def test_continue_from_still_picks_up_an_earlier_one(self, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls = _self_dispatching(workspace, monkeypatch)
        ls.rctx.http = _supervisor({**KEPT_AGENT, "node_id": SELF})

        [entry] = _run(ls, _wait(SELF, continue_from=HANDLE, context_mode="accumulate")).dispatch_batch

        assert entry["continue_from"] == HANDLE and entry["context_mode"] == "fresh"

    def test_another_target_keeps_accumulate(self, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls = _self_dispatching(workspace, monkeypatch)

        [entry] = _run(ls, _wait(SCOUT, context_mode="accumulate")).dispatch_batch

        assert entry["context_mode"] == "accumulate"


class TestContinueFrom:
    def test_a_wait_continuation_passes_the_fields_on_for_the_supervisor_to_settle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 接着一个隔离过的子 agent 时 supervisor 沿用它的副本，repo 指向别的仓库时另建；engine 不另加拒绝
        ls = _dispatching(tmp_path, monkeypatch)
        ls.rctx.http = _supervisor(KEPT_AGENT)

        action = _run(ls, _wait(continue_from=HANDLE, isolation=ISOLATION_WORKTREE, repo=REPO))

        [batch_item] = action.dispatch_batch
        assert batch_item["continue_from"] == HANDLE
        assert batch_item[ISOLATION_KEY] == ISOLATION_WORKTREE and batch_item[REPO_KEY] == REPO

    def test_an_async_continuation_carries_them_too(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ls = _dispatching(tmp_path, monkeypatch)
        ls.rctx.http = _supervisor(KEPT_AGENT)

        _run(ls, _async(continue_from=HANDLE, isolation=ISOLATION_WORKTREE))

        sent = ls.rctx.http.post.await_args.kwargs["json"]
        assert sent["conversation_key"] == KEPT_AGENT["conversation_key"]
        assert sent["dispatch_origin"]["continue_from"] == HANDLE
        assert sent["dispatch_origin"][ISOLATION_KEY] == ISOLATION_WORKTREE
        assert REPO_KEY not in sent["dispatch_origin"]


@requires_git
class TestThroughTheSupervisor:
    def test_the_engine_item_puts_the_child_in_a_worktree_and_tells_it_so(
        self, env: Env, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ls = _dispatching(env.ws, monkeypatch)
        [engine_item] = _run(ls, _wait(isolation=ISOLATION_WORKTREE)).dispatch_batch
        caller = env.caller(env.session())

        [child] = env.wait_batch(caller, engine_item)
        env.wait()

        info = child.input["task_context"][ISOLATION_CONTEXT_KEY]
        assert env.entry(child)["state"] == "ready" and info["worktree"] == child.task_id
        [block] = isolation_parts(info, workspace_root=env.ws)
        assert f"- 副本里的项目根：{info['path']}" in block
        assert f"分支 {info['branch']}" in block and f"基于提交 {info['base'][:12]}" in block
        assert f"- 原仓库：{info['repo']}" in block and info["base"] not in block

        [grandchild] = env.wait_batch(child, item("b.node"))
        [inherited] = isolation_parts(grandchild.input["task_context"][ISOLATION_CONTEXT_KEY], workspace_root=env.ws)
        assert f"这个副本属于派你来的任务（登记号 {child.task_id}）" in inherited

    def test_a_child_in_a_worktree_is_refused_in_the_supervisor_s_words(
        self, env: Env, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        child = env.isolated_child(env.caller(env.session()))
        ls = _dispatching(env.ws, monkeypatch, task_context=child.input["task_context"])

        engine_reason = _refusal_of_wait(ls, _wait(isolation=ISOLATION_WORKTREE))
        [nested] = env.wait_batch(child, item("b.node", isolation=ISOLATION_WORKTREE))
        env.wait()

        shared = f"隔离副本 {child.task_id} 里，子任务会沿用这个副本，不能再要求隔离；去掉 isolation 和 repo 再派"
        assert shared in engine_reason and shared in nested.result["error"]


# 一份做完的隔离副本，随子任务的结果交回调用方
WORKTREE_VIEW = {
    "id": "wt-1", "state": "finished", "repo": "E:/repos/app", "branch": "clonoth/wt-1", "base": "0" * 40,
    "path": "E:/repos/app-worktrees/wt-1", "patch": "E:/repos/app-worktrees/wt-1.patch", "handle": "wt-1@agent-session-1",
    "files": [{"path": "app.py", "status": "M", "added": 1, "deleted": 1}], "file_count": 1, "added": 1, "deleted": 1,
    "diff": "", "merge_allowed": True,
}
DENIES_MERGE = ToolAccess(mode="allowlist", allow=["probe"], deny=[MERGE_TOOL])
HANDED_UP = f"这个节点不能调用 {MERGE_TOOL}：审过之后把 handle wt-1@agent-session-1"
MERGE_HINT = f"审过之后要合回原仓库就调用 {MERGE_TOOL}，handle 填 wt-1@agent-session-1"


class TestPlanDelegation:
    def test_targets_isolation_and_the_merge_tool_are_settled_together(self, tmp_path: Path) -> None:
        plan = plan_delegation(_node(), tmp_path, DOWNSTREAM, [{"name": "probe"}, {"name": MERGE_TOOL}], in_isolation=False)

        assert plan.targets == (CODER, SCOUT) and plan.isolating == (CODER,)
        assert plan.merge_spec == {"name": MERGE_TOOL} and plan.can_merge is True

    @pytest.mark.parametrize("node, specs, in_isolation", [
        pytest.param(_node(access=DENIES_MERGE), [{"name": MERGE_TOOL}], False, id="denied"),
        pytest.param(_node(), [{"name": "probe"}], False, id="not-registered"),
        pytest.param(_node(), [{"name": MERGE_TOOL}], True, id="inside-a-worktree"),
        pytest.param(_node(targets=(SCOUT,)), [{"name": MERGE_TOOL}], False, id="no-isolatable-target"),
    ])
    def test_without_the_tool_there_is_no_merging(
        self, tmp_path: Path, node: Node, specs: list[dict[str, Any]], in_isolation: bool,
    ) -> None:
        plan = plan_delegation(node, tmp_path, DOWNSTREAM, specs, in_isolation=in_isolation)

        assert plan.merge_spec is None and plan.can_merge is False

    def test_a_draw_node_that_cannot_draw_is_left_out(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from engine.builtin import image_gen_gating

        monkeypatch.setattr(image_gen_gating, "disabled_draw_nodes", lambda root: {CODER})

        plan = plan_delegation(_node(), tmp_path, DOWNSTREAM, [{"name": MERGE_TOOL}], in_isolation=False)

        assert plan.targets == (SCOUT,) and plan.isolating == () and plan.can_merge is False


class TestMergeHintInResults:
    @staticmethod
    def _wait_result() -> dict[str, Any]:
        return {"type": "batch_results", "entries": [{
            "kind": "node", "node_id": CODER, "status": "finish", "text": "改好了", "call_id": "w1",
            "handle": WORKTREE_VIEW["handle"], "worktree": dict(WORKTREE_VIEW),
        }]}

    @pytest.mark.parametrize("access, expected", [
        pytest.param(DENIES_MERGE, HANDED_UP, id="denied"),
        pytest.param(None, MERGE_HINT, id="offered"),
    ])
    def test_a_wait_result_follows_the_tool_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, access: ToolAccess | None, expected: str,
    ) -> None:
        run = NodeRun(monkeypatch, tmp_path, registry=_registry(), node=_node(access=access))

        run.run(_finish(), instruction="", resume_data=self._wait_result(), downstream_info=DOWNSTREAM)

        shown = "\n".join(
            str(message.get("content") or "") for message in run.requests[0]["messages"]
            if "隔离副本 wt-1" in str(message.get("content") or "")
        )
        assert expected in shown
        assert (MERGE_TOOL in run.tool_names(0)) is (access is None)

    @pytest.mark.parametrize("deny, expected", [
        pytest.param([MERGE_TOOL], HANDED_UP, id="denied"),
        pytest.param([], MERGE_HINT, id="offered"),
    ])
    def test_the_async_result_prefix_follows_the_tool_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deny: list[str], expected: str,
    ) -> None:
        nodes = tmp_path / "config" / "nodes"
        nodes.mkdir(parents=True)
        (nodes / f"{CODER}.yaml").write_text(
            yaml.safe_dump({"id": CODER, "type": "ai", ISOLATION_KEY: ISOLATION_WORKTREE, "tool_access": "all"}),
            encoding="utf-8",
        )
        lead = Node(
            id="lead", type="ai", prompt="Work.", tool_mode="native", delegate_targets=[CODER],
            tool_access=ToolAccess(mode="allowlist", allow=["probe"], deny=deny),
        )
        load_real = runner.load_node
        monkeypatch.setattr(runner, "load_node", lambda root, node_id: lead if node_id == "lead" else load_real(root, node_id))
        monkeypatch.setattr(runner, "_fetch_session_provider_override", AsyncMock(return_value=None))
        monkeypatch.setattr(runner, "_create_provider_from_registry", lambda *args, **kwargs: SimpleNamespace())
        monkeypatch.setattr(runner, "install_event_bridge", lambda *args, **kwargs: None)
        run_ai_node = AsyncMock(return_value=TaskAction(action="finish", node_id="lead", result={"text": "done"}))
        monkeypatch.setattr(runner, "run_ai_node", run_ai_node)
        task = {"task_id": "task", "node_id": "lead", "input": {
            "instruction": "改好了", "task_context": {},
            "inbound_message_type": "dispatch_result", "inbound_child_node_id": CODER, "inbound_child_status": "finish",
            INBOUND_CHILD_WORKTREE_KEY: dict(WORKTREE_VIEW),
        }}

        async def run_task() -> None:
            transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
            async with httpx.AsyncClient(transport=transport) as http:
                await runner._run_node_task(
                    http=http, llm_http=http, sup_url="http://local", ws_root=tmp_path,
                    registry=ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools"),
                    task=task, worker_id="worker", session_id="session", session_generation=1,
                    api_key="test", base_url="http://model.invalid", default_model="test",
                )

        asyncio.run(run_task())

        instruction = run_ai_node.await_args.kwargs["instruction"]
        assert instruction.startswith(f"[Async task completed] Node {CODER} finished.")
        assert expected in instruction
