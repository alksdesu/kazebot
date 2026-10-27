from __future__ import annotations

"""
External tool (Clonoth).

The engine parses SPEC via AST at registration time.
At invocation this file runs as a subprocess:
  - Input: tool arguments as JSON on stdin
  - Output: result as JSON on stdout
  - Sensitive env vars are stripped
"""

import json
import posixpath
import re
import sys
from pathlib import Path

SPEC = {
    'name': 'clonoth_debug',
    'description': (
        'Clonoth 内部调试工具。查询任务状态、事件流、活跃任务、pending 审批等。避免每次手写 execute_command + grep。\n'
        '要审批，api 只能查白名单里的接口：工具读 data/.admin_token 和 data/events.jsonl，按 data/** 的读规则送审。'
        '结果里认得出的密钥都打过码。\n'
        '\n'
        'action 可选值：\n'
        '- api: 只读查询 supervisor API（只发 GET，可带 params），只能查下面白名单里的路径，'
        '{id} 填会话、任务、节点 ID 或日志文件名：\n'
        '  /v1/health、/v1/admin/state、/v1/admin/tasks/active、/v1/admin/runtime/status、'
        '/v1/admin/runtime/logs、/v1/admin/runtime/logs/{id}；\n'
        '  /v1/sessions/{id}/ 下的 running_tasks、active_node、children、cancelled、context_window；\n'
        '  /v1/tasks/{id}/ 下的 origin、dispatches、continuation、family/resolve、cancelled、preempted；\n'
        '  /v1/admin/config/ 下的 nodes、node-files、nodes/{id}/effective-tools、tools、all-tool-names。\n'
        '  配置原文、文件、对话内容、事件流、审批、授权、记忆、实例这些接口都不在白名单里，别试。\n'
        '- task_events: 查某个 task 的事件流（需要 task_id，支持前缀匹配）\n'
        '- active_tasks: 列出所有活跃任务（running/pending/suspended）\n'
        '- pending_approvals: 列出未决审批\n'
        '- recent_events: 最近 N 条事件（可选 node_id 过滤）\n'
        '- node_status: 查某个节点最近活动（需要 node_id）\n'
        '- health: 查 Clonoth supervisor 健康状态'
    ),
    'input_schema': {
        'properties': {
            'action': {
                'description': '要执行的查询操作',
                'enum': ['api', 'task_events', 'active_tasks', 'pending_approvals',
                         'recent_events', 'node_status', 'health'],
                'type': 'string',
            },
            'path': {
                'description': "api action 用：白名单里的 supervisor API 路径，如 '/v1/admin/state'、'/v1/sessions/{sid}/running_tasks'",
                'type': 'string',
            },
            'method': {
                'description': 'api action 用：只读工具，仅支持 GET',
                'enum': ['GET'],
                'type': 'string',
            },
            'params': {
                'description': 'api action 用：URL query 参数（JSON 对象）',
                'type': 'object',
            },
            'task_id': {
                'description': "task_events 用：task ID（支持前缀，如 '53cf8e69'）",
                'type': 'string',
            },
            'node_id': {
                'description': "node_status / recent_events 过滤用：节点 ID（如 'child_coder'）",
                'type': 'string',
            },
            'limit': {
                'default': 20,
                'description': '返回条数限制，默认 20',
                'type': 'integer',
            },
        },
        'required': ['action'],
        'type': 'object',
    },
    # 管理令牌和事件流都在 data/ 下，按读 data/.admin_token 送审；api_path 只是让审批人看见要查哪个接口。
    'guard': {'op': 'read_file', 'fixed': {'path': 'data/.admin_token'}, 'params': {'action': 'action', 'api_path': 'path'}},
}

TIMEOUT_SEC = 15.0

# api action 能查的接口，{id} 那一段填 ID。只放看任务、会话、派发、运行状态、健康和节点、工具列表的 GET：
# 配置原文、文件、对话内容、事件流、审批、授权、记忆、实例都不在里面，GET /v1/tasks/next 会把任务领走。
API_WHITELIST = (
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
# 会话 ID、任务 ID、节点 ID 和日志文件名都是这个样子；开头必须是字母或数字，. 和 .. 就占不了一段。
_ID_SEGMENT = "[A-Za-z0-9][A-Za-z0-9._-]*"
_API_PATTERNS = tuple(
    re.compile("/".join(_ID_SEGMENT if part == "{id}" else re.escape(part) for part in template.split("/")))
    for template in API_WHITELIST
)
# 规整之前就拒：..、// 能把路径拐出白名单，%、?、# 和反斜杠到服务端解码、切分之后又是另一条路径。
_FORBIDDEN_IN_PATH = ("..", "//", "%", "?", "#", "\\")


def normalized_api_path(path: object) -> tuple[str, str]:
    """(规整后的路径, 拒绝的原因)，原因不空时路径是空串。只放行白名单里的接口。"""
    if not isinstance(path, str) or not path.startswith("/"):
        # path 直接拼在 supervisor 地址后面：不以 / 开头的（@host/…、:端口/…）能把请求连同管理令牌送到别处
        shown = json.dumps(path, ensure_ascii=False)[:80]
        return "", f"path 必须是以 / 开头的 supervisor API 路径，收到 {shown}"
    if any(part in path for part in _FORBIDDEN_IN_PATH) or any(ch.isspace() or not ch.isprintable() for ch in path):
        return "", "path 里不能有 ..、//、%、?、#、反斜杠、空白和控制字符；查询参数写在 params 里"
    normalized = posixpath.normpath(path)
    if not any(pattern.fullmatch(normalized) for pattern in _API_PATTERNS):
        return "", f"{normalized} 不在 api 能查的白名单里，能查哪些见工具说明"
    return normalized, ""


def query_params(params: object) -> tuple[dict[str, str], str]:
    """api action 的 URL 查询参数，(参数, 拒绝的原因)。只收值是字符串、数字或布尔的对象。"""
    if params is None:
        return {}, ""
    if not isinstance(params, dict):
        return {}, "params 必须是 JSON 对象"
    query: dict[str, str] = {}
    for key, value in params.items():
        if isinstance(value, bool):
            query[str(key)] = "true" if value else "false"
        elif isinstance(value, (str, int, float)):
            query[str(key)] = str(value)
        else:
            return {}, f"params.{key} 只能是字符串、数字或布尔"
    return query, ""


def _redaction():
    """仓库根下的 toolbox.redaction：打码和日志、审批预览用同一套。

    工具子进程的 sys.path 里只有脚本自己那一级，导不到再把仓库根补进去。
    """
    try:
        from toolbox import redaction
    except ImportError:
        root = str(Path(__file__).resolve().parents[1])
        if root not in sys.path:
            sys.path.insert(0, root)
        from toolbox import redaction
    return redaction


def masked(payload: object) -> object:
    """交给模型之前打码：审批 details、事件和接口回的内容里认得出的密钥都换掉。"""
    return _redaction().mask_sensitive(payload)


def clipped(text: object, limit: int) -> str:
    """先打码再截断：截在密钥中间的话，剩下的半截就认不出来了。"""
    return _redaction().redact(str(text or ""))[:limit]


if __name__ == "__main__":
    _input = json.loads(sys.stdin.read())
    def _readable_result(result):
        # [AutoC 2026-05-31] Why: clonoth_debug has many action-specific payloads,
        # but the engine now reads data.result uniformly. How: derive a concise
        # action-aware summary while storing the original payload under data.payload.
        # Purpose: keep debug details structured without losing readable history.
        if isinstance(result, dict):
            if action == "api":
                return f"API result for {args.get('path', '')}"
            if action == "task_events":
                return f"{result.get('count', 0)} task events"
            if action == "active_tasks":
                return f"{result.get('active_count', 0)} active tasks"
            if action == "pending_approvals":
                return f"{result.get('pending_count', 0)} pending approvals"
            if action == "recent_events":
                return f"{result.get('total', 0)} recent events"
            if action == "node_status":
                return f"Node status: {result.get('node_id', '')}"
            if action == "health":
                return "Health checked"
        return json.dumps(result, ensure_ascii=False, default=str)
    def output(result):
        shown = masked(result)
        print(json.dumps({"ok": True, "data": {"result": _readable_result(shown), "payload": shown}}, ensure_ascii=False)); sys.exit(0)
    def fail(error): print(json.dumps({"ok": False, "error": str(error), "data": {"result": f"ERROR: {error}"}}, ensure_ascii=False)); sys.exit(1)
    try:
        _redaction()
    except ImportError as exc:
        fail(f"导不进打码模块 toolbox.redaction（{exc.name or exc}），没打码的结果不能交出去")
    args = _input
    import os, subprocess
    from urllib.request import Request, urlopen
    from urllib.parse import urlencode
    from urllib.error import HTTPError, URLError
    action = args.get("action", "")
    task_id = args.get("task_id", "")
    node_id = args.get("node_id", "")
    limit = args.get("limit", 20)
    EVENTS_FILE = "data/events.jsonl"
    CLONOTH_URL = (
        os.environ.get("CLONOTH_SUPERVISOR_URL")
        or os.environ.get("CLONOTH_BASE_URL")
        or os.environ.get("CLONOTH_URL")
        or "http://127.0.0.1:8765"
    ).rstrip("/")
    if CLONOTH_URL.endswith("/v1"):
        CLONOTH_URL = CLONOTH_URL[:-3].rstrip("/")
    ADMIN_TOKEN = os.environ.get("CLONOTH_ADMIN_TOKEN", "").strip()
    if not ADMIN_TOKEN:
        try:
            ADMIN_TOKEN = (Path("data/.admin_token").read_text(encoding="utf-8")).strip()
        except OSError:
            ADMIN_TOKEN = ""
    def api_call(path, method="GET", body=None, params=None):
        url = f"{CLONOTH_URL}{path}"
        if params:
            url += "?" + urlencode(params)
        data = json.dumps(body).encode() if body else None
        headers = {"Content-Type": "application/json"} if data else {}
        if ADMIN_TOKEN:
            headers["Authorization"] = f"Bearer {ADMIN_TOKEN}"
        req = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            try:
                err_body = json.loads(e.read().decode())
            except Exception:
                err_body = e.reason
            return {"error": f"HTTP {e.code}", "detail": err_body}
        except URLError as e:
            return {"error": f"Connection failed: {e.reason}"}
        except Exception as e:
            return {"error": str(e)}
    def read_events_tail(n=500):
        try:
            result = subprocess.run(["tail", f"-{n}", EVENTS_FILE],
                                    capture_output=True, text=True, timeout=10)
            lines = result.stdout.strip().split("\n")
            return [json.loads(l) for l in lines if l.strip()]
        except Exception:
            return []
    # ── api: 白名单里的只读接口 ──
    if action == "api":
        if not args.get("path", ""):
            fail("api action 需要 path 参数")
        method = str(args.get("method", "GET") or "GET").upper()
        # supervisor 在回环上对多数写端点不校验令牌，靠的是「同机进程可信」。
        # 这个工具让模型也成了同机进程，放开写方法等于把那条假设作废
        # —— POST /v1/inbound 能自带 platform_auth.is_admin，整套鉴权就绕过去了。
        if method != "GET":
            fail(f"api action 只能发 GET，收到 {method}；写操作请用对应的专用工具")
        api_path, problem = normalized_api_path(args.get("path"))
        if problem:
            fail(problem)
        query, problem = query_params(args.get("params"))
        if problem:
            fail(problem)
        output(api_call(api_path, method="GET", params=query))
    # ── task_events: 查事件流（events.jsonl） ──
    elif action == "task_events":
        if not task_id:
            fail("需要 task_id 参数")
        events = read_events_tail(1000)
        matched = []
        for ev in events:
            p = ev.get("payload", {})
            tid = p.get("task_id", "")
            if task_id in tid:
                matched.append({
                    "type": ev["type"],
                    "ts": ev.get("ts", "")[:19],
                    "node_id": p.get("node_id", ""),
                    "status": p.get("status", ""),
                    "message": clipped(p.get("message"), 100),
                })
        output({"task_id_prefix": task_id, "count": len(matched), "events": matched[-limit:]})
    # ── active_tasks: 优先读取权威 API，事件尾部只作降级 ──
    elif action == "active_tasks":
        api_active = api_call("/v1/admin/tasks/active")
        if isinstance(api_active, list):
            active = [{
                "task_id": str(item.get("task_id", ""))[:12],
                "session_id": item.get("session_id", ""),
                "node_id": item.get("node_id") or "?",
                "status": item.get("status", ""),
                "kind": item.get("kind", ""),
                "created": item.get("created_at", ""),
                "last_ts": item.get("updated_at", ""),
                "caller_task_id": str(item.get("caller_task_id") or "")[:12],
                "cancel_requested": bool(item.get("cancel_requested")),
                "current_phase": item.get("current_phase", ""),
                "current_detail": item.get("current_detail", ""),
            } for item in api_active]
            output({"active_count": len(active), "tasks": active, "source": "admin_api"})
        events = read_events_tail(2000)
        tasks = {}  # tid -> info
        for ev in events:
            p = ev.get("payload", {})
            tid = p.get("task_id", "")
            if not tid:
                continue
            etype = ev["type"]
            ts = ev.get("ts", "")[:19]
            nid = p.get("node_id", "")
            if etype == "task_created":
                tasks[tid] = {"node_id": nid, "status": "pending", "created": ts, "last_ts": ts}
            elif etype == "task_started":
                if tid in tasks:
                    tasks[tid].update(status="running", last_ts=ts)
                else:
                    tasks[tid] = {"node_id": nid, "status": "running", "created": ts, "last_ts": ts}
            elif etype == "task_completed":
                if tid in tasks:
                    tasks[tid].update(status=p.get("status", "completed"), last_ts=ts)
            elif etype == "task_cancelled":
                if tid in tasks:
                    tasks[tid].update(status="cancelled", last_ts=ts)
            elif etype == "task_suspended":
                if tid in tasks:
                    tasks[tid].update(status="suspended", last_ts=ts)
            elif etype == "task_resumed":
                if tid in tasks:
                    tasks[tid].update(status="pending", last_ts=ts)
        active = []
        for tid, info in tasks.items():
            if info["status"] in ("running", "pending", "suspended"):
                active.append({
                    "task_id": tid[:12],
                    "node_id": info.get("node_id", "?"),
                    "status": info["status"],
                    "created": info.get("created", ""),
                    "last_ts": info.get("last_ts", ""),
                })
        output({
            "active_count": len(active),
            "tasks": active,
            "source": "event_tail_fallback",
            "warning": "权威 active-task API 不可用，结果只覆盖最近 2000 条事件",
            "api_error": api_active.get("error", "unknown") if isinstance(api_active, dict) else "invalid response",
        })
    # ── pending_approvals: 从 API 查 ──
    elif action == "pending_approvals":
        data = api_call("/v1/admin/state")
        if isinstance(data, dict) and "pending_approvals" in data:
            pa = data["pending_approvals"]
            output({"pending_count": len(pa), "approvals": pa})
        else:
            # fallback to events
            events = read_events_tail(200)
            requested = {}
            decided = set()
            for ev in events:
                p = ev.get("payload", {})
                if ev["type"] == "approval_requested":
                    aid = p.get("approval_id", "")
                    det = p.get("details", {})
                    desc = (
                        det.get("tool_name", "") or clipped(det.get("command"), 80) or det.get("path", "")
                        or json.dumps(masked(det), ensure_ascii=False)[:80]
                    )
                    requested[aid] = {"ts": ev.get("ts", "")[:19], "desc": desc}
                elif ev["type"] == "approval_decided":
                    decided.add(p.get("approval_id", ""))
            pending = {k: v for k, v in requested.items() if k not in decided}
            result = [{"approval_id": k[:16], **v} for k, v in pending.items()]
            output({"pending_count": len(result), "approvals": result})
    # ── recent_events: 最近事件 ──
    elif action == "recent_events":
        events = read_events_tail(200)
        filtered = []
        for ev in events:
            p = ev.get("payload", {})
            nid = p.get("node_id", "")
            if node_id and node_id not in nid:
                continue
            filtered.append({
                "type": ev["type"],
                "ts": ev.get("ts", "")[:19],
                "node_id": nid,
                "task_id": p.get("task_id", "")[:12],
                "message": clipped(p.get("message"), 80),
                "status": p.get("status", ""),
            })
        output({"total": len(filtered), "events": filtered[-limit:]})
    # ── node_status: 节点状态 ──
    elif action == "node_status":
        if not node_id:
            fail("需要 node_id 参数")
        events = read_events_tail(500)
        node_events = []
        node_tasks = {}
        for ev in events:
            p = ev.get("payload", {})
            nid = p.get("node_id", "")
            tid = p.get("task_id", "")
            if node_id in nid:
                node_events.append({
                    "type": ev["type"],
                    "ts": ev.get("ts", "")[:19],
                    "task_id": tid[:12],
                    "message": clipped(p.get("message"), 100),
                    "status": p.get("status", ""),
                })
                if tid:
                    etype = ev["type"]
                    if etype in ("task_created", "task_started"):
                        node_tasks[tid] = {"status": p.get("status", "running"), "ts": ev.get("ts", "")[:19]}
                    elif etype in ("task_completed", "task_cancelled"):
                        node_tasks.pop(tid, None)
                    elif etype == "task_suspended":
                        if tid in node_tasks:
                            node_tasks[tid]["status"] = "suspended"
                    elif etype == "task_resumed":
                        if tid in node_tasks:
                            node_tasks[tid]["status"] = "pending"
        active = [{"task_id": tid[:12], "status": info["status"], "last_ts": info["ts"]}
                  for tid, info in node_tasks.items()
                  if info["status"] in ("running", "pending", "suspended")]
        last_event = node_events[-1] if node_events else None
        output({
            "node_id": node_id,
            "last_event": last_event,
            "recent_events": node_events[-limit:],
            "active_tasks": active,
        })
    # ── health ──
    elif action == "health":
        output(api_call("/v1/health"))
    else:
        fail(f"未知 action: {action}")
