from __future__ import annotations

"""Clonoth external tool: stocktool_quote.

The engine parses SPEC via AST at registration time. At invocation this file runs
as a subprocess: JSON arguments in stdin, JSON response in stdout.
"""

SPEC = {
    "name": "stocktool_quote",
    "description": "查询单只或多只股票、指数、ETF 的实时/近实时行情。支持 A股、港股、美股、A股ETF、常用A股指数；工具只拉取数据，不提供投资建议。",
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "单个股票/指数/ETF 代码或名称。例如 600519、贵州茅台、00700.HK、腾讯控股、AAPL、苹果、sh、沪深300。",
            },
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "description": "多个股票/指数/ETF 代码或名称。和 symbol 二选一；如果同时提供，以 symbols 为准。",
            },
            "market": {
                "type": "string",
                "description": "市场提示：auto/a/cn/hk/us/index/etf。默认 auto。",
                "default": "auto",
            },
            "refresh": {
                "type": "boolean",
                "description": "是否绕过缓存强制刷新。默认 false。",
                "default": False,
            },
            "include_raw": {
                "type": "boolean",
                "description": "是否返回更完整的原始字段。默认 false。",
                "default": False,
            },
            "max_items": {
                "type": "integer",
                "description": "批量查询上限，默认 10，最大 30。",
                "default": 10,
            },
        },
        "required": [],
    },
}

TIMEOUT_SEC = 60


def _run(args: dict) -> dict:
    from stocklib.config import DEFAULT_MAX_ITEMS, HARD_MAX_ITEMS, DISCLAIMER, QUOTE_TTL_SEC
    from stocklib.formatter import format_result
    from stocklib.resolver import resolve_many
    from stocklib.sources import get_quote

    raw_symbols = args.get("symbols")
    if isinstance(raw_symbols, list) and raw_symbols:
        inputs = [str(x).strip() for x in raw_symbols if str(x).strip()]
    else:
        symbol = str(args.get("symbol", "")).strip()
        inputs = [symbol] if symbol else []

    if not inputs:
        return {
            "ok": False,
            "error": "no symbol specified",
            "data": {"result": "ERROR: 请提供 symbol 或 symbols。", "quotes": [], "failures": [], "warnings": [], "success_count": 0, "fail_count": 0},
        }

    try:
        max_items = int(args.get("max_items") or DEFAULT_MAX_ITEMS)
    except Exception:
        max_items = DEFAULT_MAX_ITEMS
    max_items = max(1, min(HARD_MAX_ITEMS, max_items))
    if len(inputs) > max_items:
        inputs = inputs[:max_items]
        truncated_warning = f"输入数量超过上限，已截断为前 {max_items} 个。"
    else:
        truncated_warning = ""

    market = str(args.get("market") or "auto")
    refresh = bool(args.get("refresh", False))
    include_raw = bool(args.get("include_raw", False))

    resolved, failures = resolve_many(inputs, market_hint=market)
    quotes = []
    warnings = [DISCLAIMER] if DISCLAIMER else []
    if truncated_warning:
        warnings.insert(0, truncated_warning)

    for item in resolved:
        try:
            quote = get_quote(item, refresh=refresh, include_raw=include_raw)
            quotes.append(quote.to_dict(include_raw=include_raw))
        except Exception as exc:
            failures.append({"input": item.input, "symbol": item.symbol, "error": str(exc)})

    result_text = format_result(quotes, failures, warnings)
    ok = bool(quotes)
    data = {
        "result": result_text if ok else f"ERROR: {result_text}",
        "quotes": quotes,
        "failures": failures,
        "warnings": warnings,
        "success_count": len(quotes),
        "fail_count": len(failures),
        "cache": {"enabled": True, "ttl_sec": QUOTE_TTL_SEC},
    }
    response = {"ok": ok, "data": data}
    if not ok:
        response["error"] = "所有标的查询失败"
    return response


if __name__ == "__main__":
    import json
    import sys
    import traceback

    def output(result):
        print(json.dumps(result, ensure_ascii=False, default=str))
        sys.exit(0 if result.get("ok", True) else 1)

    try:
        raw_input = (sys.stdin.read() or "{}").lstrip("\ufeff")
        args = json.loads(raw_input)
        output(_run(args if isinstance(args, dict) else {}))
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "error": str(exc),
            "data": {
                "result": f"ERROR: stocktool_quote failed: {exc}",
                "traceback": traceback.format_exc(limit=5),
            },
        }, ensure_ascii=False))
        sys.exit(1)
