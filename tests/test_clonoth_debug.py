"""clonoth_debug：拿着管理令牌查 supervisor 的调试工具。

令牌能读配置原文、对话、审批，模型和群友却都能看到它交回来的结果：只放白名单里的只读接口，
交出去的东西一律打码，调用本身按读 data/.admin_token 送审。
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _approval_grant_fixture import QQ_KEY, Clock, grant_for, make_state, qq_auth, start_task  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import OpRequestOut, SafetyLevel  # noqa: E402
from toolbox.redaction import HIDDEN  # noqa: E402
from toolbox.registry import _guard_parameters, _guard_problem, extract_tool_spec  # noqa: E402

SCRIPT = ROOT / "tools" / "clonoth_debug.py"


def _load_script_module() -> Any:
    spec = importlib.util.spec_from_file_location("_test_clonoth_debug", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


debug = _load_script_module()


class _Supervisor:
    """假的 supervisor：记下每个请求的路径和 Authorization，按路径回预设的 JSON，别的路径回 {}。"""

    def __init__(self, replies: dict[str, Any] | None = None) -> None:
        self.seen: list[tuple[str, str]] = []
        seen, known = self.seen, dict(replies or {})

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                seen.append((self.path, self.headers.get("Authorization", "")))
                body = json.dumps(known.get(self.path.split("?", 1)[0], {})).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> _Supervisor:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


def _run(
    cwd: Path, args: Any, *, url: str, script: Path = SCRIPT, isolated: bool = False,
) -> tuple[int, dict[str, Any]]:
    """按注册表的跑法起脚本：参数是 stdin 上的 JSON，结果是 stdout 上的一行 JSON。"""
    env = {
        **os.environ,
        "CLONOTH_SUPERVISOR_URL": url,
        "CLONOTH_ADMIN_TOKEN": "debug-token",
        "PYTHONIOENCODING": "utf-8",
    }
    # -I 连 PYTHONPATH 一起不认，PYTHONIOENCODING 也跟着失效，输出编码改用 -X utf8 定
    command = [sys.executable, *(["-I", "-X", "utf8"] if isolated else []), str(script)]
    result = subprocess.run(
        command,
        input=json.dumps(args, ensure_ascii=False),
        text=True,
        encoding="utf-8",
        capture_output=True,
        cwd=cwd,
        env=env,
        check=False,
        timeout=60,
    )
    assert result.stdout, result.stderr
    return result.returncode, json.loads(result.stdout)


def test_active_tasks_prefers_authoritative_api_and_uses_configured_url(tmp_path: Path) -> None:
    task = {
        "task_id": "task-full-id",
        "session_id": "session-1",
        "node_id": "coder",
        "status": "running",
        "kind": "node",
        "created_at": "2026-10-14T00:00:00+00:00",
        "updated_at": "2026-10-14T00:01:00+00:00",
        "caller_task_id": "parent-task",
        "cancel_requested": False,
        "current_phase": "tool",
        "current_detail": "reading",
    }
    with _Supervisor({"/i/miaomiao/v1/admin/tasks/active": [task]}) as server:
        returncode, payload = _run(tmp_path, {"action": "active_tasks"}, url=f"{server.url}/i/miaomiao")

    assert returncode == 0
    assert payload["data"]["payload"]["source"] == "admin_api"
    assert payload["data"]["payload"]["tasks"][0]["task_id"] == "task-full-id"[:12]
    assert server.seen == [("/i/miaomiao/v1/admin/tasks/active", "Bearer debug-token")]


@pytest.mark.parametrize("path", [
    # 地址不带端口时这一段会被拼成端口，管理令牌跟着发到同机别的服务上
    ":{port}/v1/admin/tasks/active",
    "@127.0.0.1:{port}/v1/admin/tasks/active",
    ["/v1/admin/tasks/active"],
])
def test_api_refuses_a_path_that_could_leave_the_supervisor(tmp_path: Path, path: object) -> None:
    with _Supervisor() as server:
        if isinstance(path, str):
            path = path.format(port=server.port)
        returncode, payload = _run(tmp_path, {"action": "api", "path": path}, url="http://127.0.0.1")

    assert returncode == 1
    assert payload["ok"] is False
    assert "path 必须是以 / 开头" in payload["error"]
    assert server.seen == []


_WHITELIST = (
    "/v1/health",
    "/v1/admin/state",
    "/v1/admin/tasks/active",
    "/v1/admin/runtime/status",
    "/v1/admin/runtime/logs",
    "/v1/admin/runtime/logs/{id}",
    "/v1/sessions/{id}/running_tasks",
    "/v1/sessions/{id}/active_node",
    "/v1/sessions/{id}/children",
    "/v1/sessions/{id}/cancelled",
    "/v1/sessions/{id}/context_window",
    "/v1/tasks/{id}/origin",
    "/v1/tasks/{id}/dispatches",
    "/v1/tasks/{id}/continuation",
    "/v1/tasks/{id}/family/resolve",
    "/v1/tasks/{id}/cancelled",
    "/v1/tasks/{id}/preempted",
    "/v1/admin/config/nodes",
    "/v1/admin/config/node-files",
    "/v1/admin/config/nodes/{id}/effective-tools",
    "/v1/admin/config/tools",
    "/v1/admin/config/all-tool-names",
)

# 管理令牌能打开的、不在白名单里的接口：配置原文、文件、对话内容、事件流、审批、授权、记忆、实例，以及会领走任务的那个。
_REFUSED = [
    "/v1/admin/config/nodes/coder/raw",
    "/v1/admin/config/node-files/coder.yaml/raw",
    "/v1/admin/config/tools/web_search/raw",
    "/v1/admin/config/mcp-clients",
    "/v1/admin/config/mcp-clients/raw",
    "/v1/admin/config/policy/raw",
    "/v1/admin/config/memory/overview",
    "/v1/admin/config/memory/bot/entries",
    "/v1/config",
    "/v1/config/openai/secret",
    "/v1/config/policy",
    "/v1/files/config/models.yaml",
    "/v1/admin/conversations",
    "/v1/admin/conversations/s-1/messages",
    "/v1/sessions",
    "/v1/sessions/s-1/messages",
    "/v1/sessions/s-1/history",
    "/v1/sessions/s-1/events",
    "/v1/events",
    "/v1/approvals/a-1",
    "/v1/approval-grants",
    "/v1/instances",
    "/v1/instance-directory",
    "/v1/share-groups",
    "/v1/tasks/next",
    "/v1/sessions/s-1",
    "/v1/admin/config/nodes/coder",
    "/v1/admin/runtime/logs/a/b",
    "/v1/admin/config/nodes/coder/effective-tools/extra",
    "/v1/sessions/.hidden/running_tasks",
    "/v1/sessions/-s/running_tasks",
    "/v1/admin/stаte",
]

_MALFORMED = [
    "/v1/admin/../config/raw",
    "/v1/sessions/s..1/running_tasks",
    "/v1//admin/state",
    "/v1/admin/state%2F..%2Fconfig",
    "/v1/admin/state?x=1",
    "/v1/admin/state#top",
    "/v1\\admin\\state",
    "/v1/admin/state ",
    "/v1/admin/\tstate",
    "/v1/admin/state\n",
    "/v1/admin/sta\x00te",
]


def _sample(template: str) -> str:
    return template.replace("{id}", "s-1.log" if template.endswith("logs/{id}") else "s-1")


class TestTheWhitelist:
    def test_the_whitelist_is_exactly_these_endpoints(self) -> None:
        # 多放一个就多一条拿管理令牌读东西的路：加进来要连这里一起改，等于再过一遍眼。
        assert debug.API_WHITELIST == _WHITELIST

    @pytest.mark.parametrize("template", _WHITELIST)
    def test_every_listed_endpoint_is_allowed(self, template: str) -> None:
        path = _sample(template)

        assert debug.normalized_api_path(path) == (path, "")

    def test_a_path_is_normalised_before_it_is_compared(self) -> None:
        assert debug.normalized_api_path("/v1/admin/./state/") == ("/v1/admin/state", "")

    @pytest.mark.parametrize("path", _REFUSED)
    def test_endpoints_outside_the_whitelist_are_refused(self, path: str) -> None:
        normalized, problem = debug.normalized_api_path(path)

        assert normalized == ""
        assert "不在 api 能查的白名单里" in problem

    @pytest.mark.parametrize("path", _MALFORMED)
    def test_a_path_that_could_change_on_the_server_side_is_refused_outright(self, path: str) -> None:
        # 服务端解码、切分之后是另一条路径的，规整之前就拒，不拿它去和白名单比。
        normalized, problem = debug.normalized_api_path(path)

        assert normalized == ""
        assert problem.startswith("path 里不能有")


class TestQueryParams:
    def test_scalars_become_query_strings(self) -> None:
        assert debug.query_params({"lines": 50, "follow": False, "name": "x", "ratio": 0.5}) == (
            {"lines": "50", "follow": "false", "name": "x", "ratio": "0.5"}, "",
        )

    def test_no_params_is_an_empty_query(self) -> None:
        assert debug.query_params(None) == ({}, "")

    @pytest.mark.parametrize(("params", "said"), [
        ("lines=50", "params 必须是 JSON 对象"),
        (["lines", 50], "params 必须是 JSON 对象"),
        ({"lines": [50]}, "params.lines 只能是字符串、数字或布尔"),
        ({"filter": {"a": 1}}, "params.filter 只能是字符串、数字或布尔"),
        ({"lines": None}, "params.lines 只能是字符串、数字或布尔"),
    ])
    def test_anything_else_is_refused(self, params: Any, said: str) -> None:
        assert debug.query_params(params) == ({}, said)


class TestTheApiAction:
    def test_a_listed_endpoint_is_requested_with_its_params(self, tmp_path: Path) -> None:
        with _Supervisor() as server:
            returncode, payload = _run(tmp_path, {
                "action": "api", "path": "/v1/admin/runtime/logs/supervisor.log", "params": {"lines": 50, "follow": False},
            }, url=server.url)

        assert returncode == 0, payload
        assert server.seen == [("/v1/admin/runtime/logs/supervisor.log?lines=50&follow=false", "Bearer debug-token")]

    def test_the_normalised_path_is_the_one_sent(self, tmp_path: Path) -> None:
        with _Supervisor() as server:
            returncode, _payload = _run(tmp_path, {"action": "api", "path": "/v1/admin/./state/"}, url=server.url)

        assert returncode == 0
        assert [path for path, _auth in server.seen] == ["/v1/admin/state"]

    @pytest.mark.parametrize("path", [
        "/v1/admin/config/nodes/coder/raw",
        "/v1/files/config/models.yaml",
        "/v1/tasks/next",
        "/v1/admin/../config/raw",
    ])
    def test_a_refused_path_never_reaches_the_supervisor(self, tmp_path: Path, path: str) -> None:
        with _Supervisor() as server:
            returncode, payload = _run(tmp_path, {"action": "api", "path": path}, url=server.url)

        assert returncode == 1
        assert payload["ok"] is False
        assert server.seen == []

    def test_bad_params_never_reach_the_supervisor(self, tmp_path: Path) -> None:
        with _Supervisor() as server:
            returncode, payload = _run(
                tmp_path, {"action": "api", "path": "/v1/admin/state", "params": {"x": [1]}}, url=server.url,
            )

        assert returncode == 1
        assert payload["error"] == "params.x 只能是字符串、数字或布尔"
        assert server.seen == []

    def test_a_write_method_is_refused(self, tmp_path: Path) -> None:
        with _Supervisor() as server:
            returncode, payload = _run(
                tmp_path, {"action": "api", "path": "/v1/admin/state", "method": "POST"}, url=server.url,
            )

        assert returncode == 1
        assert "只能发 GET" in payload["error"]
        assert server.seen == []


_LEAKY_STATE = {
    "pending_approvals": [{
        "approval_id": "a-1",
        "details": {
            "command": "curl -H 'Authorization: Bearer abcdef123456' https://bob:hunter2@relay.example/v1",
            "api_key": "relay-anything-at-all",
            # 接口回的是 JSON，纯数字的口令到这边还是数
            "env": {"DB_PASSWORD": 987654, "max_tokens": 4096},
        },
    }],
    "model": {"base_url": "https://user:hunter2@relay.example/v1", "api_key": "sk-ABCDEFGHIJKL1234"},
}
_LEAKS = ("abcdef123456", "hunter2", "relay-anything-at-all", "sk-ABCDEFGHIJKL1234", "987654")


class TestWhatItHandsOut:
    @pytest.mark.parametrize("args", [
        {"action": "api", "path": "/v1/admin/state"},
        {"action": "pending_approvals"},
    ])
    def test_secrets_in_what_the_supervisor_returns_are_masked(self, tmp_path: Path, args: dict[str, Any]) -> None:
        with _Supervisor({"/v1/admin/state": _LEAKY_STATE}) as server:
            returncode, payload = _run(tmp_path, args, url=server.url)

        assert returncode == 0, payload
        shown = json.dumps(payload, ensure_ascii=False)
        for leak in _LEAKS:
            assert leak not in shown
        assert HIDDEN in shown
        # 键和结构还在，审批人和模型看得出是哪一条；max_tokens 这种上限照给
        assert "a-1" in shown
        assert '"max_tokens": 4096' in shown

    @pytest.mark.skipif(shutil.which("tail") is None, reason="事件流靠 tail 读最后几行")
    def test_event_messages_are_masked_before_they_are_cut(self, tmp_path: Path) -> None:
        # 先截后打码的话，截在密钥中间剩下的那截短得认不出来，开头几位原样交出去。
        message = "x" * 70 + " sk-ABCDEFGHIJKLMNOP"
        events = tmp_path / "data" / "events.jsonl"
        events.parent.mkdir(parents=True)
        events.write_text(json.dumps({
            "type": "task_progress", "ts": "2026-10-28T00:00:00",
            "payload": {"task_id": "t-1", "node_id": "coder", "message": message},
        }) + "\n", encoding="utf-8")

        with _Supervisor() as server:
            returncode, payload = _run(tmp_path, {"action": "recent_events"}, url=server.url)

        assert returncode == 0, payload
        [event] = payload["data"]["payload"]["events"]
        assert "ABCDEF" not in event["message"]
        assert event["message"] == "x" * 70 + " ******MNO"

    def test_nothing_is_handed_out_when_the_masking_module_cannot_be_imported(self, tmp_path: Path) -> None:
        # 拷到仓库外面跑：导不到 toolbox.redaction 就拒绝交出结果，也不先去拿。
        copy = tmp_path / "elsewhere" / "tools" / "clonoth_debug.py"
        copy.parent.mkdir(parents=True)
        shutil.copy2(SCRIPT, copy)

        with _Supervisor({"/v1/health": {"status": "ok"}}) as server:
            returncode, payload = _run(tmp_path, {"action": "health"}, url=server.url, script=copy, isolated=True)

        assert returncode == 1
        assert payload["ok"] is False
        assert "导不进打码模块" in payload["error"]
        assert server.seen == []


class TestTheSpec:
    def test_the_description_says_it_needs_approval_and_what_api_may_ask(self) -> None:
        description = debug.SPEC["description"]

        assert "要审批，api 只能查白名单里的接口" in description
        for template in debug.API_WHITELIST:
            tail = template.split("{id}/")[-1] if "{id}/" in template else template.rsplit("/", 1)[-1]
            assert tail in description, template

    def test_the_guard_judges_the_admin_token_whatever_path_the_model_gives(self) -> None:
        # 脚本读的是 data/.admin_token，path 参数只是接口路径：按它判的话审批的对象就错了。
        spec, _timeout = extract_tool_spec(SCRIPT)
        assert spec is not None
        guard = spec["guard"]

        assert guard == {
            "op": "read_file", "fixed": {"path": "data/.admin_token"}, "params": {"action": "action", "api_path": "path"},
        }
        assert _guard_problem(guard) == ""
        assert _guard_parameters(guard, "clonoth_debug", {"action": "api", "path": "/v1/admin/state"}) == {
            "tool_name": "clonoth_debug", "action": "api", "api_path": "/v1/admin/state", "path": "data/.admin_token",
        }


@pytest.fixture()
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SupervisorState:
    clock = Clock()
    monkeypatch.setattr("supervisor.state._now", clock)
    return make_state(tmp_path)


def _debug_request(state: SupervisorState, *, auth: dict[str, Any], key: str, channel: str) -> OpRequestOut:
    """按 engine 的跑法送审一次 clonoth_debug 的调用：参数由 SPEC 里的 guard 拼出来。"""
    sid, task = start_task(state, auth=auth, key=key, channel=channel)
    spec, _timeout = extract_tool_spec(SCRIPT)
    assert spec is not None
    guard = spec["guard"]
    parameters = _guard_parameters(guard, "clonoth_debug", {"action": "api", "path": "/v1/admin/state"})
    return state.request_operation(session_id=sid, op=guard["op"], parameters=parameters, task_id=task.task_id)


class TestWhoMayRunIt:
    def test_a_group_member_is_refused(self, state: SupervisorState) -> None:
        out = _debug_request(
            state, auth=qq_auth("30003", is_admin=False), key="qq_group:abcdef", channel="qq_group",
        )

        assert out.safety_level == SafetyLevel.deny

    def test_a_qq_admin_is_asked_and_sees_what_is_read(self, state: SupervisorState) -> None:
        out = _debug_request(state, auth=qq_auth(), key=QQ_KEY, channel="qq_private")

        assert out.safety_level == SafetyLevel.approval_required
        details = state.approvals[out.approval_id].details
        assert details["path"] == "data/.admin_token"
        assert details["api_path"] == "/v1/admin/state"

    def test_a_console_admin_is_asked(self, state: SupervisorState) -> None:
        out = _debug_request(
            state, auth={"platform": "web", "user_id": "u1", "is_admin": True}, key="web:console-u1", channel="web",
        )

        assert out.safety_level == SafetyLevel.approval_required

    def test_an_admin_with_a_grant_is_let_through(self, state: SupervisorState) -> None:
        grant_for(state)

        out = _debug_request(state, auth=qq_auth(), key=QQ_KEY, channel="qq_private")

        assert out.safety_level == SafetyLevel.auto
        assert out.reason.startswith("auto-approved: approval grant")

    def test_a_scheduled_task_is_refused(self, state: SupervisorState) -> None:
        out = _debug_request(state, auth=qq_auth(), key="scheduler:nightly", channel="internal")

        assert out.safety_level == SafetyLevel.deny
