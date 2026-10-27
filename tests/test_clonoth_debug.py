from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_active_tasks_prefers_authoritative_api_and_uses_configured_url(tmp_path: Path) -> None:
    seen: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            seen["path"] = self.path
            seen["authorization"] = self.headers.get("Authorization", "")
            body = json.dumps([{
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
            }]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = {
            **dict(__import__("os").environ),
            "CLONOTH_SUPERVISOR_URL": f"http://127.0.0.1:{server.server_port}/i/miaomiao",
            "CLONOTH_ADMIN_TOKEN": "debug-token",
        }
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "clonoth_debug.py")],
            input=json.dumps({"action": "active_tasks"}),
            text=True,
            capture_output=True,
            cwd=tmp_path,
            env=env,
            check=False,
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["data"]["payload"]["source"] == "admin_api"
    assert payload["data"]["payload"]["tasks"][0]["task_id"] == "task-full-id"[:12]
    assert seen == {
        "path": "/i/miaomiao/v1/admin/tasks/active",
        "authorization": "Bearer debug-token",
    }


@pytest.mark.parametrize("path", [
    # 地址不带端口时这一段会被拼成端口，管理令牌跟着发到同机别的服务上
    ":{port}/v1/admin/tasks/active",
    "@127.0.0.1:{port}/v1/admin/tasks/active",
    ["/v1/admin/tasks/active"],
])
def test_api_refuses_a_path_that_could_leave_the_supervisor(tmp_path: Path, path: object) -> None:
    seen: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            seen.append((self.path, self.headers.get("Authorization", "")))
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    if isinstance(path, str):
        path = path.format(port=server.server_port)
    try:
        env = {
            **os.environ,
            "CLONOTH_SUPERVISOR_URL": "http://127.0.0.1",
            "CLONOTH_ADMIN_TOKEN": "debug-token",
            "PYTHONIOENCODING": "utf-8",
        }
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "clonoth_debug.py")],
            input=json.dumps({"action": "api", "path": path}),
            text=True,
            encoding="utf-8",
            capture_output=True,
            cwd=tmp_path,
            env=env,
            check=False,
            timeout=60,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result.returncode == 1, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "path 必须是以 / 开头" in payload["error"]
    assert seen == []
