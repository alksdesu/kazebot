from __future__ import annotations

"""
External tool (Clonoth).

Searches with the live-search capability the main model already has, so
searching costs no extra subscription. Falls back via web_search when the
model turns out to have none.
"""

SPEC = {
    "name": "native_search",
    "description": (
        "用主模型自带的联网搜索能力检索，不需要额外的搜索密钥。"
        "一般不要直接调用：请用 web_search，它会先试本工具，不可用时自动退回 Exa。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词或用户问题原文。必填。",
            },
        },
        "required": ["query"],
    },
}

# 比 web_search 给本阶段的 35s 上限小：自己先超时能给出明确原因，而不是被父进程 kill。
TIMEOUT_SEC = 30.0


if __name__ == "__main__":
    import json
    import sys
    import urllib.error
    import urllib.request
    from pathlib import Path
    from typing import Any

    TOOL_DIR = Path(__file__).resolve().parent
    if str(TOOL_DIR) not in sys.path:
        sys.path.insert(0, str(TOOL_DIR))
    from _channel import Channel
    from _wire import (
        PROVIDER_WIRES, WIRE_ANTHROPIC, WIRE_GEMINI, WIRE_OPENAI,
        default_base_url, endpoint, headers, normalize_base_url, wire_for,
    )

    PROMPT = (
        "请联网搜索并回答下面的问题。必须实际使用你的联网搜索能力获取最新信息，"
        "不要仅凭记忆作答，并在回答中保留来源链接。\n\n问题：{query}"
    )

    def output(result: dict[str, Any]) -> None:
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0)

    def fail(error: str) -> None:
        print(json.dumps(
            {"ok": False, "error": error, "data": {"result": "ERROR: " + error}},
            ensure_ascii=False,
        ))
        sys.exit(1)

    def post(url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", **headers},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            # 上游不认识搜索工具时就是这里 4xx，把原文带回去，好判断是不支持还是配错了。
            fail("上游返回 HTTP " + str(exc.code) + "：" + (detail or exc.reason or ""))
        except Exception as exc:
            fail(type(exc).__name__ + ": " + str(exc)[:200])
        return {}

    def search_openai(model: str, key: str, base: str, query: str) -> tuple[str, list[str]]:
        data = post(
            endpoint(WIRE_OPENAI, base, model),
            {
                "model": model,
                "messages": [{"role": "user", "content": PROMPT.format(query=query)}],
                "tools": [{"type": "web_search"}],
            },
            headers(WIRE_OPENAI, base, key),
        )
        message = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
        text = str(message.get("content") or "")
        urls: list[str] = []
        for note in message.get("annotations") or []:
            citation = note.get("url_citation") if isinstance(note, dict) else None
            url = str((citation or {}).get("url") or "").strip()
            if url:
                urls.append(url)
        return text, urls

    def search_gemini(model: str, key: str, base: str, query: str) -> tuple[str, list[str]]:
        data = post(
            endpoint(WIRE_GEMINI, base, model),
            {
                "contents": [{"role": "user", "parts": [{"text": PROMPT.format(query=query)}]}],
                "tools": [{"google_search": {}}],
            },
            headers(WIRE_GEMINI, base, key),
        )
        candidate = (data.get("candidates") or [{}])[0] or {}
        parts = ((candidate.get("content") or {}).get("parts")) or []
        text = "".join(str(part.get("text") or "") for part in parts if isinstance(part, dict))
        urls = []
        for chunk in (candidate.get("groundingMetadata") or {}).get("groundingChunks") or []:
            url = str(((chunk or {}).get("web") or {}).get("uri") or "").strip()
            if url:
                urls.append(url)
        return text, urls

    def search_anthropic(model: str, key: str, base: str, query: str) -> tuple[str, list[str]]:
        data = post(
            endpoint(WIRE_ANTHROPIC, base, model),
            {
                "model": model,
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": PROMPT.format(query=query)}],
                "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
            },
            headers(WIRE_ANTHROPIC, base, key),
        )
        chunks: list[str] = []
        urls: list[str] = []
        for block in data.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                chunks.append(str(block.get("text") or ""))
            elif block.get("type") == "web_search_tool_result":
                for item in block.get("content") or []:
                    url = str((item or {}).get("url") or "").strip()
                    if url:
                        urls.append(url)
        return "".join(chunks), urls

    ENGINES = {
        WIRE_GEMINI: search_gemini,
        WIRE_ANTHROPIC: search_anthropic,
        WIRE_OPENAI: search_openai,
    }

    raw_input = json.loads((sys.stdin.read() or "{}").lstrip("﻿"))
    args = raw_input if isinstance(raw_input, dict) else {}
    query = str(args.get("query") or "").strip()
    if not query:
        fail("缺少搜索关键词。格式固定为：{\"query\": \"要搜索的内容\"}")

    channel = Channel("native_search")
    main = channel.main()
    provider = (channel.own("provider", "PROVIDER") or main.provider or "openai").strip().lower()
    model = channel.pick("model", "MODEL", main.model)
    api_key = channel.pick("api_key", "API_KEY", main.api_key)
    # 搜索请求发出去就计费，认不出的渠道宁可直说，不按 OpenAI 猜着发一次。
    if provider not in PROVIDER_WIRES:
        fail("不认识渠道 " + provider + "，没法判断它的搜索接口该怎么调。")
    wire = wire_for(provider)
    base_url = normalize_base_url(
        wire, channel.pick("base_url", "BASE_URL", main.base_url) or default_base_url(wire),
    )

    if not model or not api_key:
        fail("主模型渠道没有配好 model 或 api_key，无法使用自带搜索。")

    engine = ENGINES.get(wire)
    if engine is None:
        fail("渠道 " + provider + " 的搜索走 " + wire + " 格式，本工具还没接。")
    answer, citations = engine(model, api_key, base_url, query)

    unique: list[str] = []
    for url in citations:
        if url not in unique:
            unique.append(url)

    # 没有结构化引用就说明模型压根没联网，只是凭记忆答的。这种答案比搜索失败更危险，
    # 所以按失败上报，让 web_search 去退回 Exa。
    if not unique:
        fail(
            "主模型（" + provider + "/" + model + "）没有返回任何来源引用，"
            "说明它没有可用的联网搜索能力。"
        )

    body = (answer or "").strip() or "模型返回了引用但没有正文。"
    lines = [body, "", "来源："]
    lines.extend("- " + url for url in unique)

    output({
        "ok": True,
        "data": {
            "result": "\n".join(lines),
            "query": query,
            "citations": unique,
            "provider": provider,
            "model": model,
        },
    })
