"""自带搜索的渠道分派。

四家的搜索接口从路径到引用字段全不一样，认错家发出去的请求不是 404 就是被当成普通提问
答一遍 —— 后者更糟，它会拿模型的记忆冒充搜索结果。所以这里用一个本地假上游把四条路
各跑一遍，看请求真打到了哪、带了什么头、引用有没有捞出来。
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(_ROOT / "tools"))

MODEL = "m1"

CONFIG = "\n".join([
    "version: 1",
    "provider: {provider}",
    "{provider}:",
    "  base_url: {base_url}",
    "  api_key: sk-test",
    "  model: " + MODEL,
    "",
])


def _workspace(tmp_path: Path, config: str) -> Path:
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "config.yaml").write_text(config, encoding="utf-8")
    return tmp_path


def _run(cwd: Path) -> dict:
    """黑盒跑真脚本。整段逻辑都在 __main__ 里，只能这么测。"""
    done = subprocess.run(
        [sys.executable, str(_ROOT / "tools" / "native_search.py")],
        input=json.dumps({"query": "今天天气"}),
        capture_output=True, text=True, cwd=str(cwd), timeout=120,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


# ── 本地假上游 ──

class _Upstream:
    def __init__(self, payload: dict) -> None:
        self.seen: list[dict] = []
        seen = self.seen

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                seen.append({
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": json.loads(self.rfile.read(length) or b"{}"),
                })
                blob = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)

            def log_message(self, *_args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = "http://127.0.0.1:%d" % self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture()
def upstream():
    made: list[_Upstream] = []

    def make(payload: dict) -> _Upstream:
        made.append(_Upstream(payload))
        return made[-1]

    yield make
    for item in made:
        item.close()


# 四家各自的「答了一句、给了一条来源」长什么样。
ANSWER = "晴。"
SOURCE = "https://example.com/weather"

REPLIES = {
    "openai": {"choices": [{"message": {
        "content": ANSWER,
        "annotations": [{"type": "url_citation", "url_citation": {"url": SOURCE}}],
    }}]},
    "deepseek": {"choices": [{"message": {
        "content": ANSWER,
        "annotations": [{"type": "url_citation", "url_citation": {"url": SOURCE}}],
    }}]},
    "openai-responses": {"output": [
        {"type": "web_search_call", "action": {"type": "search", "query": "天气"}},
        {"type": "message", "content": [{
            "type": "output_text",
            "text": ANSWER,
            "annotations": [{
                "type": "url_citation", "url": SOURCE, "title": "天气",
                "start_index": 0, "end_index": 2,
            }],
        }]},
    ]},
    "anthropic": {"content": [
        {"type": "text", "text": ANSWER},
        {"type": "web_search_tool_result", "content": [{"url": SOURCE}]},
    ]},
    "gemini": {"candidates": [{
        "content": {"parts": [{"text": ANSWER}]},
        "groundingMetadata": {"groundingChunks": [{"web": {"uri": SOURCE}}]},
    }]},
}

EXPECTED_PATH = {
    "openai": "/v1/chat/completions",
    "deepseek": "/v1/chat/completions",
    "openai-responses": "/v1/responses",
    "anthropic": "/v1/messages",
    "gemini": "/v1beta/models/%s:generateContent" % MODEL,
}

ALL_PROVIDERS = sorted(REPLIES)


def _search(tmp_path: Path, upstream, provider: str):
    server = upstream(REPLIES[provider])
    config = CONFIG.format(provider=provider, base_url=server.base_url)
    return _run(_workspace(tmp_path, config)), server


class TestEveryProviderReachesItsOwnEndpoint:
    @pytest.mark.parametrize("provider", ALL_PROVIDERS)
    def test_the_request_lands_on_the_right_path(
        self, tmp_path: Path, upstream, provider: str,
    ) -> None:
        _out, server = _search(tmp_path, upstream, provider)

        assert len(server.seen) == 1
        assert server.seen[0]["path"] == EXPECTED_PATH[provider]

    @pytest.mark.parametrize("provider", ALL_PROVIDERS)
    def test_the_citation_is_pulled_out(self, tmp_path: Path, upstream, provider: str) -> None:
        out, _server = _search(tmp_path, upstream, provider)

        assert out["ok"] is True, out
        assert out["data"]["citations"] == [SOURCE]
        assert ANSWER in out["data"]["result"]

    @pytest.mark.parametrize("provider", ALL_PROVIDERS)
    def test_the_key_travels_in_a_header_never_in_the_url(
        self, tmp_path: Path, upstream, provider: str,
    ) -> None:
        # URL 会被代理日志、浏览器历史和 Referer 原样记下来。
        _out, server = _search(tmp_path, upstream, provider)

        sent = server.seen[0]
        assert "sk-test" not in sent["path"]
        assert "key=" not in sent["path"]
        assert "sk-test" in json.dumps(dict(sent["headers"]))

    def test_a_relay_gets_bearer_even_for_anthropic(
        self, tmp_path: Path, upstream,
    ) -> None:
        # 官方域名才收 x-api-key，中转一律 Bearer —— 认错了是 401。
        _out, server = _search(tmp_path, upstream, "anthropic")

        assert server.seen[0]["headers"]["authorization"] == "Bearer sk-test"
        assert server.seen[0]["headers"]["anthropic-version"]

    def test_the_search_tool_is_actually_requested(self, tmp_path: Path, upstream) -> None:
        # 不带工具就是普通提问，模型会拿记忆答一遍还答得挺像。
        _out, server = _search(tmp_path, upstream, "gemini")

        assert server.seen[0]["body"]["tools"] == [{"google_search": {}}]

    def test_the_responses_api_asks_for_web_search(self, tmp_path: Path, upstream) -> None:
        _out, server = _search(tmp_path, upstream, "openai-responses")

        body = server.seen[0]["body"]
        assert body["tools"] == [{"type": "web_search"}]
        # 这家收的是 input，不是 messages。
        assert "messages" not in body and body["input"]


class TestAnswersWithoutSourcesAreRefused:
    @pytest.mark.parametrize("provider", ALL_PROVIDERS)
    def test_no_citation_means_the_model_never_searched(
        self, tmp_path: Path, upstream, provider: str,
    ) -> None:
        # 凭记忆编的答案比搜索失败更危险，按失败上报好让 web_search 退回 Exa。
        stripped = json.loads(json.dumps(REPLIES[provider])
                              .replace(SOURCE, "").replace("url_citation", "x"))
        server = upstream(stripped)
        out = _run(_workspace(tmp_path, CONFIG.format(
            provider=provider, base_url=server.base_url,
        )))

        assert out["ok"] is False
        assert "联网" in out["error"]


class TestDispatchRefusesBeforeSpending:
    """这几种都在发请求之前就该拒掉，所以不需要上游。"""

    def test_an_unknown_provider_is_named_not_guessed(self, tmp_path: Path) -> None:
        # 猜成 OpenAI 发出去就是一次白花的计费请求。
        config = CONFIG.format(provider="openai", base_url="https://relay.example/v1")
        out = _run(_workspace(tmp_path, config + "\n".join([
            "system_models:", "  native_search:", "    provider: some-relay-brand", "",
        ])))

        assert out["ok"] is False
        assert "some-relay-brand" in out["error"]

    def test_a_missing_key_is_reported(self, tmp_path: Path) -> None:
        config = CONFIG.format(
            provider="openai", base_url="https://relay.example/v1",
        ).replace("api_key: sk-test", "api_key: ''")
        out = _run(_workspace(tmp_path, config))

        assert out["ok"] is False
        assert "api_key" in out["error"]


class TestTheWireComesFromTheProvider:
    @pytest.mark.parametrize(("provider", "wire"), [
        ("openai", "openai"),
        ("deepseek", "openai"),
        ("openai-responses", "openai-responses"),
        ("anthropic", "anthropic"),
        ("gemini", "gemini"),
    ])
    def test_known_providers_map_to_a_wire(self, provider: str, wire: str) -> None:
        import _wire as w

        assert w.wire_for(provider) == wire

    def test_every_known_wire_has_a_search_engine(self) -> None:
        # 这份清单和上面的参数化用例是同一件事的两头，漏一家这里先炸。
        import _wire as w

        assert set(w.PROVIDER_WIRES) == set(ALL_PROVIDERS)
