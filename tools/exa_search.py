from __future__ import annotations

"""
External tool (Clonoth).

The engine parses SPEC via AST at registration time.
At invocation this file runs as a subprocess:
  - Input: tool arguments as JSON on stdin
  - Output: result as JSON on stdout
  - Sensitive env vars are stripped
"""

SPEC = {
    "name": "exa_search",
    "description": (
        "Exa 网页搜索工具。用于通用网页搜索、资料检索、新闻查询和带来源引用的研究任务。"
        "调用时只需要填写 query 字段，例如：{\"query\": \"今天 AI 新闻\"}。"
        "不要填写 api_key、base_url 等内部字段；需要更稳的自动搜索时优先使用 web_search。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词或用户问题原文。必填。",
            },
            "num_results": {
                "type": "integer",
                "default": 5,
                "description": "返回结果数量，默认 5，最大 10。",
            },
        },
        "required": ["query"],
    },
}

TIMEOUT_SEC = 30.0


if __name__ == "__main__":
    import json
    import os
    import sys
    import urllib.error
    import urllib.request

    _input = json.loads((sys.stdin.read() or "{}").lstrip("\ufeff"))

    def output(result):
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0)

    def fail(error, hint=""):
        message = str(error)
        if hint:
            message = f"{message}\n修复建议：{hint}"
        print(json.dumps({"ok": False, "error": message, "data": {"result": f"ERROR: {message}"}}, ensure_ascii=False))
        sys.exit(1)

    def as_clean_string_list(value):
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    def clamp_int(value, default, minimum, maximum):
        try:
            parsed = int(value)
        except Exception:
            parsed = default
        return max(minimum, min(maximum, parsed))

    def extract_query(value):
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            return " ".join(str(item).strip() for item in value if str(item).strip()).strip()
        if not isinstance(value, dict):
            return ""

        query_keys = [
            "query", "q", "keyword", "keywords", "search", "search_query", "searchQuery",
            "term", "terms", "text", "content", "prompt", "question", "input", "message",
        ]
        for key in query_keys:
            if key in value:
                extracted = extract_query(value.get(key))
                if extracted:
                    return extracted

        for key in ["args", "arguments", "params", "parameters", "data", "payload", "request"]:
            if key in value:
                extracted = extract_query(value.get(key))
                if extracted:
                    return extracted
        return ""

    def extract_int_alias(args, keys, default, minimum, maximum):
        if not isinstance(args, dict):
            return default
        for key in keys:
            if key in args:
                return clamp_int(args.get(key), default, minimum, maximum)
        return default

    def load_dotenv():
        env_path = os.path.join(os.getcwd(), ".env")
        values = {}
        if not os.path.isfile(env_path):
            return values
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f.read().splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip().strip("'\"")
        except Exception:
            return {}
        return values

    args = _input if isinstance(_input, dict) else {}

    query = extract_query(_input)
    if not query:
        fail(
            "缺少搜索关键词。",
            "请重新调用 exa_search，格式固定为：{\"query\": \"要搜索的内容\"}。如果已有用户问题，请把用户问题原文放入 query。",
        )

    api_key = str(args.get("api_key") or os.environ.get("EXA_API_KEY") or "").strip()
    base_url = str(args.get("base_url") or os.environ.get("EXA_BASE_URL") or "").strip().rstrip("/")

    if not api_key or not base_url:
        dotenv = load_dotenv()
        if not api_key:
            api_key = str(dotenv.get("EXA_API_KEY") or "").strip()
        if not base_url:
            base_url = str(dotenv.get("EXA_BASE_URL") or "").strip().rstrip("/")

    if not api_key:
        fail("No Exa API key available. Set EXA_API_KEY in the environment or .env, or pass api_key.")
    if not base_url:
        base_url = "https://api.exa.ai"
    num_results = extract_int_alias(
        args,
        ["num_results", "numResults", "max_results", "maxResults", "limit", "top_k", "topK", "count"],
        5,
        1,
        20,
    )
    search_type = str(args.get("search_type") or "auto").strip().lower()
    if search_type not in {"auto", "neural", "keyword"}:
        search_type = "auto"
    text_max_characters = clamp_int(args.get("text_max_characters", 800), 800, 0, 3000)

    payload = {
        "query": query,
        "type": search_type,
        "numResults": num_results,
        "useAutoprompt": bool(args.get("use_autoprompt", True)),
    }

    include_domains = as_clean_string_list(args.get("include_domains"))
    exclude_domains = as_clean_string_list(args.get("exclude_domains"))
    if include_domains:
        payload["includeDomains"] = include_domains
    if exclude_domains:
        payload["excludeDomains"] = exclude_domains

    start_published_date = str(args.get("start_published_date") or "").strip()
    end_published_date = str(args.get("end_published_date") or "").strip()
    if start_published_date:
        payload["startPublishedDate"] = start_published_date
    if end_published_date:
        payload["endPublishedDate"] = end_published_date

    if text_max_characters > 0:
        payload["contents"] = {
            "text": {"maxCharacters": text_max_characters},
            "highlights": {"numSentences": 2, "highlightsPerUrl": 2},
        }

    req = urllib.request.Request(
        f"{base_url}/search",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            response_data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")[:2000]
        except Exception:
            body = ""
        fail(f"Exa API request failed: HTTP {e.code} {e.reason}. {body}")
    except Exception as e:
        fail(f"Exa API request failed: {e}")

    raw_results = response_data.get("results", [])
    if not isinstance(raw_results, list):
        raw_results = []

    formatted_results = []
    citations = []
    lines = [f"Exa 搜索结果：{query}"]

    for index, item in enumerate(raw_results, start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "Untitled").strip()
        url = str(item.get("url") or "").strip()
        published_date = str(item.get("publishedDate") or "").strip()
        author = str(item.get("author") or "").strip()
        text = str(item.get("text") or "").strip()
        highlights = item.get("highlights") if isinstance(item.get("highlights"), list) else []

        if url:
            citations.append(url)

        snippet = text
        if not snippet and highlights:
            snippet = "\n".join(str(h).strip() for h in highlights if str(h).strip())
        snippet = snippet[:1200].strip()

        lines.append("")
        lines.append(f"[{index}] {title}")
        if url:
            lines.append(f"URL: {url}")
        meta = "；".join(part for part in [f"发布时间: {published_date}" if published_date else "", f"作者: {author}" if author else ""] if part)
        if meta:
            lines.append(meta)
        if snippet:
            lines.append(f"摘要: {snippet}")

        formatted_results.append({
            "title": title,
            "url": url,
            "published_date": published_date,
            "author": author,
            "score": item.get("score"),
            "text": text,
            "highlights": highlights,
            "summary": item.get("summary"),
            "id": item.get("id"),
        })

    result_text = "\n".join(lines).strip()
    if not formatted_results:
        result_text = f"Exa 搜索没有返回结果：{query}"

    output({
        "ok": True,
        "data": {
            "result": result_text,
            "query": query,
            "citations": citations,
            "results": formatted_results,
            "autoprompt_string": response_data.get("autopromptString"),
            "request_id": response_data.get("requestId"),
        },
    })
