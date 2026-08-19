from __future__ import annotations

"""
Fault-tolerant web search wrapper for Clonoth.

The engine parses SPEC via AST at registration time.
At invocation this file runs as a subprocess:
  - Input: tool arguments as JSON on stdin
  - Output: result as JSON on stdout
"""

SPEC = {
    "name": "web_search",
    "description": (
        "极简联网搜索工具。用于网页搜索、最新资料检索、新闻查询，以及 X/Twitter 帖子搜索。"
        "只需要填写 query 字段，例如：{\"query\": \"今天 AI 新闻\"}。"
        "不要调用 native_search / exa_search / x_search；"
        "本工具会先用主模型自带的联网能力，不可用时自动退回 Exa 或 X。"
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
            "source": {
                "type": "string",
                "default": "auto",
                "enum": ["auto", "native", "exa", "x", "both"],
                "description": "可选搜索来源。默认 auto（主模型自带能力优先）。一般不要填写；需要 X/Twitter 时可填 x。",
            },
        },
        "required": ["query"],
    },
}

TIMEOUT_SEC = 70.0


if __name__ == "__main__":
    import json
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path
    from typing import Any

    TOOL_DIR = Path(__file__).resolve().parent

    def output(result: dict[str, Any]) -> None:
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0)

    def fail(error: str, *, hint: str = "") -> None:
        message = str(error)
        if hint:
            message = f"{message}\n修复建议：{hint}"
        print(json.dumps({"ok": False, "error": message, "data": {"result": f"ERROR: {message}"}}, ensure_ascii=False))
        sys.exit(1)

    def load_dotenv() -> dict[str, str]:
        env_path = Path(os.getcwd()) / ".env"
        values: dict[str, str] = {}
        if not env_path.is_file():
            return values
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("'\"")
        except Exception:
            return {}
        return values

    def has_config_key(dotenv: dict[str, str], *names: str) -> bool:
        for name in names:
            if str(os.environ.get(name) or dotenv.get(name) or "").strip():
                return True
        return False

    def extract_text(value: Any) -> str:
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
                extracted = extract_text(value.get(key))
                if extracted:
                    return extracted

        nested_keys = ["args", "arguments", "params", "parameters", "data", "payload", "request"]
        for key in nested_keys:
            if key in value:
                extracted = extract_text(value.get(key))
                if extracted:
                    return extracted

        return ""

    def clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except Exception:
            parsed = default
        return max(minimum, min(maximum, parsed))

    def clamp_float(value: Any, default: float, minimum: float, maximum: float) -> float:
        try:
            parsed = float(value)
        except Exception:
            parsed = default
        return max(minimum, min(maximum, parsed))

    def extract_int_alias(args: dict[str, Any], keys: list[str], default: int, minimum: int, maximum: int) -> int:
        for key in keys:
            if key not in args:
                continue
            return clamp_int(args.get(key), default, minimum, maximum)
        return default

    def extract_float_alias(args: dict[str, Any], keys: list[str], default: float, minimum: float, maximum: float) -> float:
        for key in keys:
            if key not in args:
                continue
            return clamp_float(args.get(key), default, minimum, maximum)
        return default

    def env_float(dotenv: dict[str, str], names: list[str], default: float, minimum: float, maximum: float) -> float:
        for name in names:
            raw = os.environ.get(name) or dotenv.get(name)
            if str(raw or "").strip():
                return clamp_float(raw, default, minimum, maximum)
        return default

    def normalize_source(args: dict[str, Any], query: str, dotenv: dict[str, str]) -> str:
        raw = ""
        for key in ["source", "provider", "search_provider", "searchProvider", "engine", "type"]:
            if key in args:
                raw = str(args.get(key) or "").strip().lower()
                break
        if not raw:
            raw = str(os.environ.get("WEB_SEARCH_PROVIDER") or dotenv.get("WEB_SEARCH_PROVIDER") or os.environ.get("WEB_SEARCH_SOURCE") or dotenv.get("WEB_SEARCH_SOURCE") or "auto").strip().lower()

        aliases = {
            "web": "exa",
            "general": "exa",
            "google": "exa",
            "exa_search": "exa",
            "native_search": "native",
            "model": "native",
            "twitter": "x",
            "tweet": "x",
            "tweets": "x",
            "x_search": "x",
            "x/twitter": "x",
            "all": "both",
            "multi": "both",
        }
        source = aliases.get(raw, raw)
        if source not in {"auto", "native", "exa", "x", "both"}:
            source = "auto"

        if source != "auto":
            return source

        # 点名要 X 的问题直接走 X：主模型的联网搜索覆盖不到站内帖子。
        lower_query = query.lower()
        wants_x = any(token in lower_query for token in ["twitter", "tweet", "tweets", "x 上", "x上", "推特", "推文"])
        if wants_x and has_config_key(dotenv, "XAI_API_KEY", "OPENAI_API_KEY"):
            return "x"
        return "auto"

    def child_error(tool_name: str, error: str) -> dict[str, Any]:
        return {"ok": False, "error": error, "data": {"result": f"ERROR: {error}"}}

    def parse_child_result(tool_name: str, returncode: int, stdout: str, stderr: str) -> dict[str, Any]:
        stdout = (stdout or "").strip()
        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

        err = (stderr or "").strip()
        text = stdout or err or f"{tool_name} exited with code {returncode}"
        ok = returncode == 0
        return {"ok": ok, "error": "" if ok else text, "data": {"result": text}}

    def start_child(tool_name: str, child_args: dict[str, Any]) -> tuple[subprocess.Popen[str] | None, dict[str, Any] | None]:
        script = TOOL_DIR / f"{tool_name}.py"
        if not script.is_file():
            return None, child_error(tool_name, f"tool script not found: {tool_name}")
        try:
            proc = subprocess.Popen(
                [sys.executable, str(script)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                cwd=os.getcwd(),
            )
            if proc.stdin is not None:
                proc.stdin.write(json.dumps(child_args, ensure_ascii=False))
                proc.stdin.close()
            return proc, None
        except Exception as exc:
            return None, child_error(tool_name, f"{tool_name} execution failed: {exc}")

    def collect_child(tool_name: str, proc: subprocess.Popen[str]) -> dict[str, Any]:
        try:
            stdout = proc.stdout.read() if proc.stdout is not None else ""
            stderr = proc.stderr.read() if proc.stderr is not None else ""
        except Exception as exc:
            return child_error(tool_name, f"{tool_name} output read failed: {exc}")
        return parse_child_result(tool_name, int(proc.returncode or 0), stdout, stderr)

    def kill_child(tool_name: str, proc: subprocess.Popen[str], reason: str) -> dict[str, Any]:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
        return child_error(tool_name, reason)

    def run_children(calls: list[tuple[str, dict[str, Any]]], *, total_timeout_sec: float, child_timeout_sec: float) -> dict[str, dict[str, Any]]:
        """Run one or more configured providers with an overall timeout.

        Multiple providers are started together so source=both (or any auto/config
        resolving to multiple providers) waits roughly for the slowest provider,
        not the sum of all provider latencies.
        """
        results: dict[str, dict[str, Any]] = {}
        running: dict[str, tuple[subprocess.Popen[str], float]] = {}
        started = time.monotonic()
        deadline = started + max(1.0, total_timeout_sec)

        for tool_name, child_args in calls:
            proc, immediate_result = start_child(tool_name, child_args)
            if immediate_result is not None:
                results[tool_name] = immediate_result
            elif proc is not None:
                running[tool_name] = (proc, time.monotonic())

        while running:
            now = time.monotonic()
            for tool_name, (proc, child_started) in list(running.items()):
                if proc.poll() is not None:
                    results[tool_name] = collect_child(tool_name, proc)
                    running.pop(tool_name, None)
                    continue
                if now - child_started >= child_timeout_sec:
                    results[tool_name] = kill_child(tool_name, proc, f"{tool_name} timeout after {child_timeout_sec:.0f}s")
                    running.pop(tool_name, None)

            if not running:
                break
            now = time.monotonic()
            if now >= deadline:
                for tool_name, (proc, _) in list(running.items()):
                    results[tool_name] = kill_child(tool_name, proc, f"{tool_name} cancelled by web_search total timeout after {total_timeout_sec:.0f}s")
                    running.pop(tool_name, None)
                break
            time.sleep(min(0.05, max(0.0, deadline - now)))

        return results

    raw_input = json.loads((sys.stdin.read() or "{}").lstrip("\ufeff"))
    args = raw_input if isinstance(raw_input, dict) else {}
    query = extract_text(raw_input)
    if not query:
        fail(
            "缺少搜索关键词。",
            hint="请重新调用 web_search，格式固定为：{\"query\": \"要搜索的内容\"}。如果已有用户问题，请把用户问题原文放入 query。",
        )

    num_results = extract_int_alias(
        args,
        ["num_results", "numResults", "max_results", "maxResults", "limit", "top_k", "topK", "count"],
        5,
        1,
        10,
    )
    dotenv = load_dotenv()
    source = normalize_source(args, query, dotenv)
    total_timeout_sec = extract_float_alias(
        args,
        ["total_timeout_sec", "totalTimeoutSec", "timeout_sec", "timeout", "deadline_sec"],
        env_float(dotenv, ["WEB_SEARCH_TOTAL_TIMEOUT_SEC", "WEB_SEARCH_TIMEOUT_SEC"], 65.0, 5.0, float(TIMEOUT_SEC)),
        5.0,
        float(TIMEOUT_SEC),
    )
    child_timeout_sec = extract_float_alias(
        args,
        ["child_timeout_sec", "childTimeoutSec", "provider_timeout_sec", "providerTimeoutSec"],
        env_float(dotenv, ["WEB_SEARCH_CHILD_TIMEOUT_SEC", "WEB_SEARCH_PROVIDER_TIMEOUT_SEC"], 60.0, 5.0, total_timeout_sec),
        5.0,
        total_timeout_sec,
    )

    labels = {
        "native_search": "主模型联网搜索",
        "exa_search": "Exa 网页搜索",
        "x_search": "X/Twitter 搜索",
    }

    def keyed_calls() -> list[tuple[str, dict[str, Any]]]:
        planned: list[tuple[str, dict[str, Any]]] = []
        if source in {"auto", "exa", "both"}:
            planned.append(("exa_search", {"query": query, "num_results": num_results}))
        if source in {"x", "both"}:
            planned.append(("x_search", {"query": query, "max_tokens": 8000}))
        return planned or [("exa_search", {"query": query, "num_results": num_results})]

    # 主模型自带的联网能力不额外收费也不用配密钥，所以 auto 先问它，不行再花钱。
    # 第二项是每阶段的时间上限：原生这一路必须留够预算给后面的降级。
    stages: list[tuple[list[tuple[str, dict[str, Any]]], float]] = []
    if source in {"auto", "native"}:
        stages.append(([("native_search", {"query": query})], 35.0))
    if source != "native":
        stages.append((keyed_calls(), total_timeout_sec))

    child_results: dict[str, dict[str, Any]] = {}
    result_sections: list[str] = [f"联网搜索结果：{query}"]
    failed_sections: list[str] = []
    citations: list[str] = []
    any_ok = False
    deadline = time.monotonic() + total_timeout_sec

    for calls, stage_cap in stages:
        remaining = deadline - time.monotonic()
        if remaining <= 1.0:
            break
        stage_total = min(remaining, stage_cap)
        stage_results = run_children(
            calls,
            total_timeout_sec=stage_total,
            child_timeout_sec=min(child_timeout_sec, stage_total),
        )
        child_results.update(stage_results)

        stage_sections: list[str] = []
        stage_ok = False
        for tool_name, _child_args in calls:
            result = stage_results.get(tool_name) or child_error(tool_name, f"{tool_name} did not return a result")
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            result_text = str(data.get("result") or result.get("error") or "").strip()
            label = labels.get(tool_name, tool_name)
            if result.get("ok") is True:
                stage_ok = True
                stage_sections.extend(["", f"## {label}", result_text or "搜索成功，但没有返回可读文本。"])
                for url in data.get("citations") or []:
                    url_text = str(url or "").strip()
                    if url_text and url_text not in citations:
                        citations.append(url_text)
            else:
                stage_sections.extend(["", f"## {label}失败", result_text or str(result.get("error") or "未知错误")])

        if stage_ok:
            any_ok = True
            result_sections.extend(stage_sections)
            break
        # 降级过程留到全部落空时再讲，成功时不必拿它打扰模型。
        failed_sections.extend(stage_sections)

    if not any_ok:
        result_sections.extend(failed_sections)

    final_text = "\n".join(result_sections).strip()
    if not any_ok:
        final_text += (
            "\n\n修复建议：主模型没有可用的联网搜索能力，且没有配置 Exa/X 密钥。"
            "可以用 manage_secret 设置 EXA_API_KEY，"
            "或把 data/config.yaml 的 system_models.native_search 指向一个支持联网搜索的模型。"
        )
        output({
            "ok": False,
            "error": "all configured search providers failed",
            "data": {
                "result": final_text,
                "query": query,
                "source": source,
                "citations": citations,
                "providers": child_results,
                "total_timeout_sec": total_timeout_sec,
                "child_timeout_sec": child_timeout_sec,
            },
        })

    output({
        "ok": True,
        "data": {
            "result": final_text,
            "query": query,
            "source": source,
            "citations": citations,
            "providers": child_results,
            "total_timeout_sec": total_timeout_sec,
            "child_timeout_sec": child_timeout_sec,
        },
    })
