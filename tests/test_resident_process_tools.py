"""常驻进程的四个工具：起进程按执行命令的规则审批、审批卡标明常驻，节点跑不了命令就不许起；读、停、列都转给 supervisor。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

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
from engine.builtin.doom_loop import _RepeatableTools  # noqa: E402
from engine.inference.ai_step import _is_parallel_safe  # noqa: E402
from engine.inference.pseudo_tools import tool_allowed  # noqa: E402
from engine.node import load_node  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from toolbox.builtins import processes as tools  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402
from toolbox.registry import ToolRegistry  # noqa: E402

PROCESS_TOOLS = ("start_process", "read_process_output", "stop_process", "list_processes")


class _Response:
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Supervisor:
    """/v1/processes 的替身：按 (方法, 路径) 依次给应答，最后一个一直重复；记下每次请求。"""

    def __init__(self, routes: dict[tuple[str, str], list[_Response]] | None = None, *, down: bool = False) -> None:
        self.routes = {key: list(value) for key, value in (routes or {}).items()}
        self.down = down
        self.calls: list[dict[str, Any]] = []

    async def request(
        self, method: str, url: str, *, json: Any = None, params: Any = None, timeout: Any = None,
    ) -> _Response:
        path = url.removeprefix("http://sup")
        self.calls.append({"method": method, "path": path, "json": json, "params": params, "timeout": timeout})
        if self.down:
            raise httpx.ConnectError("connection refused")
        answers = self.routes.get((method, path))
        if not answers:
            raise AssertionError(f"unexpected {method} {path}")
        return answers.pop(0) if len(answers) > 1 else answers[0]


class _Ctx(ToolContext):
    def __init__(
        self,
        root: Path,
        http: _Supervisor,
        *,
        node_id: str = "bootstrap.coder",
        task_id: str = "task-1",
        verdict: str = "auto",
        approval: str = "allowed",
        cancelled: list[bool] | None = None,
    ) -> None:
        super().__init__(
            supervisor_url="http://sup", session_id="branch-1", run_id="run-1", worker_id="worker-1",
            workspace_root=root, http=http, registry=None, task_id=task_id, parent_session_id="sess-1",
            node_id=node_id,
        )
        self.verdict = verdict
        self.approval = approval
        self.cancel_answers = list(cancelled or [])
        self.ops: list[tuple[str, dict[str, Any]]] = []

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.ops.append((op, dict(parameters)))
        return {"safety_level": self.verdict, "approval_id": "ap-1", "reason": "commands need a human"}

    async def wait_for_approval(self, approval_id: str, poll_interval: float | None = None) -> dict[str, Any]:
        return {"status": self.approval}

    async def check_cancelled(self) -> bool:
        return self.cancel_answers.pop(0) if self.cancel_answers else False


def _view(**overrides: Any) -> dict[str, Any]:
    view = {
        "id": "p_abc", "name": "dev", "status": "running", "pid": 4321, "uptime_sec": 3.0, "cwd": "/work",
        "max_runtime_sec": 14400.0, "ready": None, "ready_line": None, "returncode": None, "stop_reason": "",
        "stop_reason_text": "", "log_error": "", "command": "npm run dev", "last_line": 2,
    }
    view.update(overrides)
    return view


def _ok(body: Any) -> list[_Response]:
    return [_Response(200, body)]


_TAIL = ("GET", "/v1/processes/p_abc/output")
_START = ("POST", "/v1/processes")
_STATUS = ("GET", "/v1/processes/p_abc")


def _run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


class TestStart:
    def test_it_asks_like_a_command_and_the_card_says_resident(self, tmp_path: Path) -> None:
        http = _Supervisor({_START: _ok({"process": _view()}), _TAIL: _ok({"text": "   1 | booting"})})
        ctx = _Ctx(tmp_path, http)

        result = _run(tools.start_process({"command": "  npm run dev  ", "name": "dev"}, ctx))

        assert ctx.ops == [("execute_command", {
            "command": "npm run dev", "tool_name": "start_process", "reason": tools.RESIDENT_REASON, "resident": True,
        })]
        assert tools.RESIDENT_REASON.startswith("常驻进程")
        assert http.calls[0]["json"] == {
            "task_id": "task-1", "command": "npm run dev", "name": "dev", "ready_pattern": "", "max_runtime_sec": None,
        }
        assert http.calls[1]["params"] == {"task_id": "task-1", "tail": 20}
        assert result["ok"] is True and result["data"]["ready_timed_out"] is False
        assert result["data"]["process"]["id"] == "p_abc"
        text = result["data"]["result"]
        assert text.startswith("已起常驻进程：p_abc（dev）运行中，已运行 3 秒，pid 4321。工作目录 /work，最长运行 4 小时 0 分。")
        assert "最近的输出：\n   1 | booting" in text and "read_process_output" in text

    def test_the_policy_judges_it_by_the_command_alone(self, tmp_path: Path) -> None:
        policy = PolicyEngine(workspace_root=tmp_path)
        extra = {"tool_name": "start_process", "reason": tools.RESIDENT_REASON, "resident": True}

        for command in ("ls -la", "rm -rf /", "pip install requests"):
            plain = policy.evaluate(op="execute_command", parameters={"command": command})
            resident = policy.evaluate(op="execute_command", parameters={"command": command, **extra})
            assert (resident.safety_level, resident.reason, getattr(resident, "sensitive", False)) == (
                plain.safety_level, plain.reason, getattr(plain, "sensitive", False),
            )

    @pytest.mark.parametrize("node_id", ["chat.vision", "no.such.node"])
    def test_a_node_that_cannot_run_commands_cannot_start_one(self, tmp_path: Path, node_id: str) -> None:
        http = _Supervisor()
        ctx = _Ctx(tmp_path, http, node_id=node_id)

        result = _run(tools.start_process({"command": "npm run dev"}, ctx))

        assert result["ok"] is False and "不能执行命令" in result["error"]
        assert ctx.ops == [] and http.calls == []

    def test_an_empty_command_is_refused_up_front(self, tmp_path: Path) -> None:
        ctx = _Ctx(tmp_path, _Supervisor())

        result = _run(tools.start_process({"command": "   "}, ctx))

        assert result == {"ok": False, "error": "command 不能为空", "data": {"result": "ERROR: command 不能为空"}}
        assert ctx.ops == []

    @pytest.mark.parametrize("verdict, approval, error", [
        ("deny", "allowed", "commands need a human"),
        ("approval_required", "denied", "user denied approval"),
    ])
    def test_a_refused_request_starts_nothing(self, tmp_path: Path, verdict: str, approval: str, error: str) -> None:
        http = _Supervisor()

        result = _run(tools.start_process({"command": "npm run dev"}, _Ctx(tmp_path, http, verdict=verdict, approval=approval)))

        assert result["ok"] is False and result["error"] == error and "cancelled" not in result
        assert http.calls == []

    def test_a_cancelled_task_starts_nothing(self, tmp_path: Path) -> None:
        http = _Supervisor()

        result = _run(tools.start_process({"command": "npm run dev"}, _Ctx(tmp_path, http, cancelled=[True])))

        assert result["ok"] is False and result["cancelled"] is True and result["data"]["cancelled"] is True
        assert http.calls == []

    def test_supervisor_refusals_come_back_as_tool_failures(self, tmp_path: Path) -> None:
        http = _Supervisor({_START: [_Response(409, {"detail": "这个会话已经有 4 个常驻进程（上限 4）"})]})

        result = _run(tools.start_process({"command": "npm run dev"}, _Ctx(tmp_path, http)))

        assert result["ok"] is False and result["error"] == "这个会话已经有 4 个常驻进程（上限 4）"

    def test_an_unreadable_error_names_the_status(self, tmp_path: Path) -> None:
        http = _Supervisor({_START: [_Response(503, ValueError("not json"))]})

        result = _run(tools.start_process({"command": "npm run dev"}, _Ctx(tmp_path, http)))

        assert result["error"] == "supervisor 回了 HTTP 503"

    def test_an_unreachable_supervisor_is_a_failure(self, tmp_path: Path) -> None:
        result = _run(tools.start_process({"command": "npm run dev"}, _Ctx(tmp_path, _Supervisor(down=True))))

        assert result["ok"] is False and result["error"].startswith("连不上 supervisor：ConnectError")

    def test_without_a_task_there_is_nothing_to_own_the_process(self, tmp_path: Path) -> None:
        http = _Supervisor()

        result = _run(tools.start_process({"command": "npm run dev"}, _Ctx(tmp_path, http, task_id="")))

        assert result["error"] == "没有任务身份，不能管理常驻进程" and http.calls == []

    def test_the_runtime_limit_is_passed_on_as_a_number(self, tmp_path: Path) -> None:
        http = _Supervisor({_START: _ok({"process": _view()}), _TAIL: _ok({"text": ""})})

        _run(tools.start_process({"command": "x", "max_runtime_sec": "600"}, _Ctx(tmp_path, http)))
        _run(tools.start_process({"command": "x", "max_runtime_sec": float("inf")}, _Ctx(tmp_path, http)))

        assert [call["json"]["max_runtime_sec"] for call in http.calls if call["method"] == "POST"] == [600.0, None]


class TestReadyPattern:
    @pytest.fixture(autouse=True)
    def _fast_polling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(tools, "_READY_POLL_SEC", 0.01)

    def test_it_waits_until_the_line_shows_up(self, tmp_path: Path) -> None:
        http = _Supervisor({
            _START: _ok({"process": _view(ready=False, ready_pattern="Listening")}),
            _STATUS: [_Response(200, {"process": _view(ready=False)}), _Response(200, {"process": _view(ready=True, ready_line=7)})],
            _TAIL: _ok({"text": "   7 | Listening on 8080"}),
        })

        result = _run(tools.start_process({"command": "npm run dev", "ready_pattern": "Listening"}, _Ctx(tmp_path, http)))

        assert result["data"]["process"]["ready"] is True and result["data"]["ready_timed_out"] is False
        assert "已就绪（第 7 行出现 ready_pattern）" in result["data"]["result"]
        assert http.calls[0]["json"]["ready_pattern"] == "Listening"
        assert [call["params"] for call in http.calls if call["path"] == _STATUS[1]] == [{"task_id": "task-1"}] * 2

    def test_a_pattern_that_never_shows_up_times_out_and_the_process_stays(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(tools, "_ready_timeout", lambda ctx, raw: 0.05)
        http = _Supervisor({
            _START: _ok({"process": _view(ready=False)}),
            _STATUS: _ok({"process": _view(ready=False)}),
            _TAIL: _ok({"text": "   1 | still compiling"}),
        })

        result = _run(tools.start_process({"command": "npm run dev", "ready_pattern": "Listening"}, _Ctx(tmp_path, http)))

        assert result["ok"] is True and result["data"]["ready_timed_out"] is True
        assert "还没出现 ready_pattern，进程还在跑" in result["data"]["result"]
        assert not any(call["path"].endswith("/stop") for call in http.calls)

    def test_the_wait_ends_when_the_process_exits(self, tmp_path: Path) -> None:
        http = _Supervisor({
            _START: _ok({"process": _view(ready=False)}),
            _STATUS: _ok({"process": _view(status="exited", returncode=1, ready=False)}),
            _TAIL: _ok({"text": "   1 | error: port in use"}),
        })

        result = _run(tools.start_process({"command": "npm run dev", "ready_pattern": "Listening"}, _Ctx(tmp_path, http)))

        assert result["data"]["ready_timed_out"] is False
        assert "p_abc（dev）已退出，returncode=1" in result["data"]["result"]
        assert "还没出现 ready_pattern" not in result["data"]["result"]

    def test_the_wait_ends_when_the_task_is_cancelled(self, tmp_path: Path) -> None:
        http = _Supervisor({
            _START: _ok({"process": _view(ready=False)}),
            _STATUS: _ok({"process": _view(ready=False)}),
            _TAIL: _ok({"text": ""}),
        })
        # 审批前后各问一次都没取消，等就绪的时候取消了
        ctx = _Ctx(tmp_path, http, cancelled=[False, False, True])

        result = _run(tools.start_process({"command": "npm run dev", "ready_pattern": "Listening"}, ctx))

        assert result["ok"] is True and result["data"]["ready_timed_out"] is True
        assert len([call for call in http.calls if call["path"] == _STATUS[1]]) == 1

    def test_an_already_ready_process_is_not_polled(self, tmp_path: Path) -> None:
        http = _Supervisor({_START: _ok({"process": _view(ready=True, ready_line=1)}), _TAIL: _ok({"text": ""})})

        _run(tools.start_process({"command": "npm run dev", "ready_pattern": "x"}, _Ctx(tmp_path, http)))

        assert [call["method"] + " " + call["path"] for call in http.calls] == [
            "POST /v1/processes", "GET /v1/processes/p_abc/output",
        ]

    @pytest.mark.parametrize("raw, expected", [
        (None, 60.0), (True, 60.0), ("abc", 60.0), (float("nan"), 60.0), (0, 60.0), (-1, 60.0),
        (0.5, 1.0), (30, 30.0), ("45", 45.0), (10_000, 600.0),
    ])
    def test_the_ready_timeout_is_normalised(self, tmp_path: Path, raw: Any, expected: float) -> None:
        assert tools._ready_timeout(_Ctx(tmp_path, _Supervisor()), raw) == expected

    def test_the_instance_sets_the_default_ready_timeout(self, tmp_path: Path) -> None:
        path = config_layout.runtime_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump({"processes": {"ready_timeout_sec": 5}}), encoding="utf-8")

        assert tools._ready_timeout(_Ctx(tmp_path, _Supervisor()), None) == 5.0


class TestReadStopList:
    def test_reading_forwards_the_paging_arguments(self, tmp_path: Path) -> None:
        page = {"process": _view(), "text": "第 11–12 行（共 12 行）", "next_since": 12, "more": False, "last_line": 12}
        http = _Supervisor({_TAIL: _ok(page)})

        result = _run(tools.read_process_output(
            {"id": "p_abc", "since": "10", "max_lines": 50, "search": "ERR", "tail": 0}, _Ctx(tmp_path, http),
        ))

        assert http.calls[0]["params"] == {"task_id": "task-1", "since": 10, "max_lines": 50, "search": "ERR"}
        assert result["data"]["result"] == "p_abc（dev）运行中，已运行 3 秒，pid 4321\n第 11–12 行（共 12 行）"
        assert (result["data"]["next_since"], result["data"]["more"], result["data"]["last_line"]) == (12, False, 12)

    def test_unusable_paging_arguments_are_dropped(self, tmp_path: Path) -> None:
        http = _Supervisor({_TAIL: _ok({"process": _view(), "text": ""})})

        _run(tools.read_process_output(
            {"id": "p_abc", "tail": "abc", "since": -3, "max_lines": True, "search": ""}, _Ctx(tmp_path, http),
        ))
        _run(tools.read_process_output({"id": "p_abc", "tail": 2.9}, _Ctx(tmp_path, http)))

        assert [call["params"] for call in http.calls] == [{"task_id": "task-1"}, {"task_id": "task-1", "tail": 2}]

    @pytest.mark.parametrize("tool", [tools.read_process_output, tools.stop_process])
    def test_an_id_is_required(self, tmp_path: Path, tool: Any) -> None:
        http = _Supervisor()

        result = _run(tool({"id": "  "}, _Ctx(tmp_path, http)))

        assert result["error"] == "id 不能为空" and http.calls == []

    def test_stopping_reports_whether_anything_was_running(self, tmp_path: Path) -> None:
        ended = _view(status="stopped", returncode=-15, stop_reason="stop", stop_reason_text="被 stop_process 停掉")
        http = _Supervisor({("POST", "/v1/processes/p_abc/stop"): [
            _Response(200, {"process": ended, "stopped": True}),
            _Response(200, {"process": ended, "stopped": False}),
        ]})
        ctx = _Ctx(tmp_path, http)

        first = _run(tools.stop_process({"id": "p_abc"}, ctx))
        second = _run(tools.stop_process({"id": "p_abc"}, ctx))

        assert first["data"]["result"] == "已停掉。p_abc（dev）已停止，returncode=-15，被 stop_process 停掉，共运行 3 秒"
        assert second["data"]["result"].startswith("没有要停的：它已经结束了。")
        assert http.calls[0]["json"] == {"task_id": "task-1"} and http.calls[0]["timeout"] == tools._STOP_TIMEOUT_SEC

    def test_listing_this_conversations_processes(self, tmp_path: Path) -> None:
        listed = {"processes": [_view(), "junk", _view(id="p_def", name="watch", status="exited", returncode=0)]}
        http = _Supervisor({("GET", "/v1/processes"): [_Response(200, {"processes": []}), _Response(200, listed)]})
        ctx = _Ctx(tmp_path, http)

        empty = _run(tools.list_processes({}, ctx))
        full = _run(tools.list_processes({}, ctx))

        assert empty["data"] == {"result": "这个会话没有常驻进程。", "processes": []}
        rows = full["data"]["result"].splitlines()
        assert rows[0] == "这个会话的常驻进程："
        assert rows[1] == "- p_abc（dev）运行中，已运行 3 秒，pid 4321；命令：npm run dev；输出到第 2 行"
        assert rows[2].startswith("- p_def（watch）已退出，returncode=0") and len(full["data"]["processes"]) == 2
        assert http.calls[0]["params"] == {"task_id": "task-1"}


class TestStatusLine:
    @pytest.mark.parametrize("overrides, expected", [
        ({}, "p_abc（dev）运行中，已运行 3 秒，pid 4321"),
        ({"uptime_sec": 125}, "p_abc（dev）运行中，已运行 2 分 5 秒，pid 4321"),
        ({"status": "exited", "returncode": 0, "uptime_sec": 3725}, "p_abc（dev）已退出，returncode=0，共运行 1 小时 2 分"),
        ({"ready": False}, "p_abc（dev）运行中，已运行 3 秒，pid 4321，还没等到 ready_pattern"),
        ({"ready": True, "ready_line": 9}, "p_abc（dev）运行中，已运行 3 秒，pid 4321，已就绪（第 9 行出现 ready_pattern）"),
        ({"log_error": "disk full"}, "p_abc（dev）运行中，已运行 3 秒，pid 4321，输出写不进日志：disk full"),
        ({"uptime_sec": None, "pid": 0}, "p_abc（dev）运行中，已运行 ?"),
        (
            {"status": "stopped", "stop_reason": "idle", "stop_reason_text": "", "returncode": None},
            "p_abc（dev）已停止，idle，共运行 3 秒",
        ),
    ])
    def test_rendering(self, overrides: dict[str, Any], expected: str) -> None:
        assert tools.status_line(_view(**overrides)) == expected


@pytest.fixture(scope="module")
def registry(tmp_path_factory: pytest.TempPathFactory) -> ToolRegistry:
    root = tmp_path_factory.mktemp("registry")
    return ToolRegistry(workspace_root=root, tools_dir=root / "tools")


class TestRegistration:
    @pytest.mark.parametrize("name", PROCESS_TOOLS)
    def test_the_names_are_reserved(self, registry: ToolRegistry, name: str) -> None:
        assert registry.reserved_name_reason(name) == "taken by a builtin tool"

    def test_reading_and_listing_may_repeat_and_run_side_by_side(self, registry: ToolRegistry) -> None:
        repeatable = _RepeatableTools(registry)

        for name in ("read_process_output", "list_processes"):
            spec = registry.get_spec(name)
            assert spec["repeatable"] is True and spec["parallel_safe"] is True
            assert name in repeatable and _is_parallel_safe(name, {}, spec) is True
        for name in ("start_process", "stop_process"):
            spec = registry.get_spec(name)
            assert "repeatable" not in spec and name not in repeatable
            assert _is_parallel_safe(name, {}, spec) is False

    def test_the_required_arguments(self, registry: ToolRegistry) -> None:
        required = {name: registry.get_spec(name)["input_schema"]["required"] for name in PROCESS_TOOLS}

        assert required == {
            "start_process": ["command"], "read_process_output": ["id"], "stop_process": ["id"], "list_processes": [],
        }

    def test_only_the_coder_ships_with_them(self, tmp_path: Path) -> None:
        coder = load_node(tmp_path, "bootstrap.coder")
        others = [load_node(tmp_path, node_id) for node_id in (
            "bootstrap.code_reviewer", "qq.orchestrator", "bootstrap.shell_orchestrator",
        )]

        assert coder is not None and None not in others
        assert all(tool_allowed(coder, name) for name in PROCESS_TOOLS)
        assert not any(tool_allowed(node, name) for node in others for name in PROCESS_TOOLS)


def test_the_qq_approval_card_says_resident(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from _onebot_harness import load_runtime

    runtime = load_runtime(monkeypatch, tmp_path)
    details = {"command": "npm run dev", "tool_name": "start_process", "reason": tools.RESIDENT_REASON, "resident": True}

    card = runtime.approval._approval_summary("ap-1", "start_process", details)

    assert "操作: start_process" in card
    assert "command: npm run dev" in card and f"reason: {tools.RESIDENT_REASON}" in card
