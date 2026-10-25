"""工具子进程的密钥不出现在 URL 和命令行参数里：?key= 会被中转站的访问日志和各种报错原样记下来，
命令行参数同机任何用户 ps 一下就能看到。"""
from __future__ import annotations

import base64
import importlib.util
import json
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from toolbox._common import safe_subprocess_env  # noqa: E402

_KEY = "gemini-secret-key"
_IMAGE = base64.b64encode(b"\x89PNG\r\n\x1a\nnot-really-a-png").decode("ascii")


def _load_nai_generate() -> Any:
    # 脚本主体在 __main__ 块里，导入只拿到顶层的 curl_request，不会真去请求。
    spec = importlib.util.spec_from_file_location("_prov_nai_generate", _ROOT / "tools" / "drawtools" / "nai_generate.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def relay() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    seen: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server 的方法名
            payload = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            seen.append({"path": self.path, "headers": dict(self.headers.items()), "body": payload})
            body = json.dumps({"candidates": [{"content": {"parts": [
                {"text": "done"},
                {"inlineData": {"mimeType": "image/png", "data": _IMAGE}},
            ]}}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _tool_env() -> dict[str, str]:
    # 开发机上的代理和同名渠道变量不能替测试里的 .env 做主。
    env = {
        key: value for key, value in safe_subprocess_env().items()
        if not key.upper().endswith("_PROXY")
        and not key.upper().startswith(("CLONOTH_IMAGE_GEMINI_", "GEMINI_", "OPENAI_"))
    }
    env["NO_PROXY"] = "*"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def test_the_key_goes_in_a_header_and_never_into_the_url(
    tmp_path: Path, relay: tuple[str, list[dict[str, Any]]],
) -> None:
    base_url, seen = relay
    (tmp_path / ".env").write_text(
        f"CLONOTH_IMAGE_GEMINI_API_KEY={_KEY}\nCLONOTH_IMAGE_GEMINI_BASE_URL={base_url}\n", encoding="utf-8",
    )

    done = subprocess.run(
        [sys.executable, str(_ROOT / "tools" / "gemini_image.py")],
        input=json.dumps({"prompt": "a cat", "model": "img-model"}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        cwd=tmp_path,
        env=_tool_env(),
    )

    result = json.loads(done.stdout)
    assert result["ok"] is True, done.stdout + done.stderr
    assert len(seen) == 1
    assert seen[0]["path"] == "/v1beta/models/img-model:generateContent"
    assert _KEY not in seen[0]["path"]
    # 中转站一律 Bearer，和 provider、看图工具同一套；官方域名的 x-goog-api-key 由 test_wire 钉住。
    assert seen[0]["headers"].get("Authorization") == f"Bearer {_KEY}"
    assert (tmp_path / result["data"]["attachments"][0]["path"]).is_file()


def test_the_novelai_key_is_fed_to_curl_through_stdin() -> None:
    argv, stdin = _load_nai_generate().curl_request(
        "https://image.novelai.net/ai/generate-image", "nai-secret",
        timeout_sec=30, payload_path="payload.json", output_path="out.zip",
    )

    assert not any("nai-secret" in arg for arg in argv)
    assert argv[argv.index("-H") + 1] == "@-"
    assert "Authorization: Bearer nai-secret\n" in stdin
    assert "Content-Type: application/json\n" in stdin


@pytest.mark.skipif(shutil.which("curl") is None, reason="本机没有 curl")
def test_curl_really_sends_the_headers_it_reads_from_stdin(
    tmp_path: Path, relay: tuple[str, list[dict[str, Any]]],
) -> None:
    # -H @- 要 curl 7.55 起才认：拿本机 curl 真发一次，确认密钥和请求体都到了服务端。
    base_url, seen = relay
    payload = tmp_path / "payload.json"
    payload.write_text('{"input": "x"}', encoding="utf-8")
    argv, stdin = _load_nai_generate().curl_request(
        f"{base_url}/ai/generate-image", "nai-secret",
        timeout_sec=30, payload_path=str(payload), output_path=str(tmp_path / "out.bin"),
    )

    done = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=60, env=_tool_env())

    assert done.stdout.strip() == "200"
    assert seen[0]["path"] == "/ai/generate-image"
    assert seen[0]["headers"].get("Authorization") == "Bearer nai-secret"
    assert seen[0]["body"] == b'{"input": "x"}'
