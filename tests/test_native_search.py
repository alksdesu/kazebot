"""自带搜索的渠道分派。

三家的搜索接口长得完全不一样，认错家发出去的请求不是 404 就是被当成普通提问答一遍 ——
后者更糟：它会拿模型的记忆冒充搜索结果。所以分派错了必须在发请求之前就拦住。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(_ROOT / "tools"))

CONFIG = "\n".join([
    "version: 1",
    "provider: {provider}",
    "openai:",
    "  base_url: https://relay.example/v1",
    "  api_key: sk-main",
    "  model: gpt-5.4",
    "anthropic:",
    "  base_url: https://api.anthropic.com",
    "  api_key: sk-ant",
    "  model: claude-4",
    "openai-responses:",
    "  base_url: https://api.openai.com/v1",
    "  api_key: sk-resp",
    "  model: gpt-5.4",
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


class TestDispatchRefusesBeforeSpending:
    """这几种都在发请求之前就该拒掉，所以不需要网络。"""

    def test_an_unknown_provider_is_named_not_guessed(self, tmp_path: Path) -> None:
        # 猜成 OpenAI 发出去就是一次白花的计费请求。
        out = _run(_workspace(tmp_path, CONFIG.format(provider="openai") + "\n".join([
            "system_models:", "  native_search:", "    provider: some-relay-brand", "",
        ])))

        assert out["ok"] is False
        assert "some-relay-brand" in out["error"]

    def test_a_wire_without_a_search_engine_says_which_one(self, tmp_path: Path) -> None:
        # Responses API 的搜索是另一套 body，还没接 —— 说清楚是格式问题，不是渠道坏了。
        out = _run(_workspace(tmp_path, CONFIG.format(provider="openai-responses")))

        assert out["ok"] is False
        assert "openai-responses" in out["error"]

    def test_a_missing_key_is_reported(self, tmp_path: Path) -> None:
        config = CONFIG.format(provider="openai").replace("api_key: sk-main", "api_key: ''")
        out = _run(_workspace(tmp_path, config))

        assert out["ok"] is False
        assert "api_key" in out["error"]


class TestTheWireIsPickedFromTheProvider:
    """分派本身不发请求也能验：wire 决定端点和鉴权头。"""

    @pytest.mark.parametrize(("provider", "wire"), [
        ("openai", "openai"),
        ("deepseek", "openai"),
        ("anthropic", "anthropic"),
        ("gemini", "gemini"),
    ])
    def test_known_providers_map_to_a_search_engine(self, provider: str, wire: str) -> None:
        import _wire as vw

        assert vw.wire_for(provider) == wire

    def test_the_key_never_lands_in_the_url(self) -> None:
        # 这家的官方文档给的是 ?key=，但 URL 会被代理日志和浏览器历史原样记下来。
        import _wire as vw

        url = vw.endpoint("gemini", "https://generativelanguage.googleapis.com", "m")
        assert "sk-" not in url and "key=" not in url
        assert vw.headers("gemini", "https://generativelanguage.googleapis.com", "sk-x")[
            "x-goog-api-key"
        ] == "sk-x"
