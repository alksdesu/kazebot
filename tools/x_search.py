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
    'description': (
        '搜索 X/Twitter 上的帖子。调用时只需要填写 query 字段，例如：'
        '{"query": "今天 AI 领域的热门推文"}。'
        '需要更稳的自动搜索时优先使用 web_search。'
    ),
    'input_schema': {
        'properties': {
            'query': {'description': '搜索关键词或用户问题原文。必填。', 'type': 'string'}
        },
        'required': ['query'],
        'type': 'object'
    },
    'name': 'x_search'
}

TIMEOUT_SEC = 60.0


if __name__ == "__main__":
    import json, sys
    _input = json.loads((sys.stdin.read() or "{}").lstrip("\ufeff"))
    def output(result): print(json.dumps(result, ensure_ascii=False)); sys.exit(0)
    def fail(error, hint=''):
        # [AutoC 2026-05-31] Why: x_search failures should remain readable after
        # the prefers data.result. How: emit ok=false with data.result before
        # exiting non-zero. Purpose: preserve specific API errors in tool history.
        message = str(error)
        if hint:
            message = f"{message}\n修复建议：{hint}"
        print(json.dumps({"ok": False, "error": message, "data": {"result": f"ERROR: {message}"}}, ensure_ascii=False)); sys.exit(1)

    def extract_query(value):
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            return ' '.join(str(item).strip() for item in value if str(item).strip()).strip()
        if not isinstance(value, dict):
            return ''
        for key in ['query', 'q', 'keyword', 'keywords', 'search', 'search_query', 'searchQuery', 'term', 'terms', 'text', 'content', 'prompt', 'question', 'input', 'message']:
            if key in value:
                extracted = extract_query(value.get(key))
                if extracted:
                    return extracted
        for key in ['args', 'arguments', 'params', 'parameters', 'data', 'payload', 'request']:
            if key in value:
                extracted = extract_query(value.get(key))
                if extracted:
                    return extracted
        return ''

    args = _input if isinstance(_input, dict) else {}
    import urllib.request
    import json
    import os

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
    
    query = extract_query(_input)
    if not query:
        fail(
            '缺少搜索关键词。',
            '请重新调用 x_search，格式固定为：{"query": "要搜索的内容"}。如果已有用户问题，请把用户问题原文放入 query。'
        )
    model = args.get('model', 'grok-4.3')
    try:
        max_tokens = int(args.get('max_tokens', 8000))
    except Exception:
        max_tokens = 8000
    max_tokens = max(1000, min(16000, max_tokens))
    api_key = str(args.get('api_key') or os.environ.get('XAI_API_KEY') or os.environ.get('OPENAI_API_KEY') or '').strip()
    base_url = str(args.get('base_url') or os.environ.get('XAI_BASE_URL') or '').strip().rstrip('/')

    if not api_key or not base_url:
        dotenv = load_dotenv()
        if not api_key:
            api_key = str(dotenv.get('XAI_API_KEY') or dotenv.get('OPENAI_API_KEY') or '').strip()
        if not base_url:
            base_url = str(dotenv.get('XAI_BASE_URL') or '').strip().rstrip('/')
    
    if not api_key:
        fail('No API key available. Set XAI_API_KEY or OPENAI_API_KEY in the environment or .env, or pass api_key.')
    if not base_url:
        base_url = 'https://api.x.ai/v1'
    
    payload = json.dumps({
        'model': model,
        'input': query,
        'tools': [{'type': 'x_search'}],
        'max_output_tokens': max_tokens
    }).encode('utf-8')
    
    req = urllib.request.Request(
        f'{base_url}/responses',
        data=payload,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        },
        method='POST'
    )
    
    try:
        with urllib.request.urlopen(req, timeout=55) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        fail(f'API request failed: {e}')
    
    final_text = ''
    annotations = []
    reasoning_summary = ''
    
    for item in data.get('output', []):
        if item.get('type') == 'message':
            for content in item.get('content', []):
                if content.get('type') == 'output_text':
                    final_text = content.get('text', '')
                    for ann in content.get('annotations', []):
                        if ann.get('type') == 'url_citation':
                            annotations.append(ann.get('url', ''))
        elif item.get('type') == 'reasoning':
            for s in item.get('summary', []):
                if s.get('text'):
                    reasoning_summary += s['text'] + '\n'
    
    usage = data.get('usage', {})
    
    usage_info = {
        'input_tokens': usage.get('input_tokens', 0),
        'output_tokens': usage.get('output_tokens', 0),
        'total_tokens': usage.get('total_tokens', 0),
        'x_search_calls': usage.get('server_side_tool_usage_details', {}).get('x_search_calls', 0),
        'cost_usd': usage.get('cost_in_usd_ticks', 0) / 1_000_000_000
    }
    # [AutoC 2026-05-31] Why: x_search used to return primary text at the top
    # level, but all tools now expose human-readable output as data.result. How:
    # keep text, citations, model, and usage under data, with result matching the
    # final answer text. Purpose: preserve structured search metadata while making
    # engine rendering uniform.
    result = {
        'ok': True,
        'data': {
            'result': final_text,
            'text': final_text,
            'citations': list(set(annotations)),
            'model': data.get('model', model),
            'usage': usage_info
        }
    }
    
    output(result)
