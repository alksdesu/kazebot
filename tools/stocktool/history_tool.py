from __future__ import annotations

"""Clonoth external tool: stocktool_history.

Fetches historical OHLC kline data for A/HK/US stocks, A-share etf/index.
Runs as a subprocess: JSON arguments in stdin, JSON response in stdout.
"""

SPEC = {
    "name": "stocktool_history",
    "description": "查询股票/指数/ETF 的历史行情（日/周/月 K 线）。支持 A股/渧股/美股/A股ETF/A股指数；可传日期区间 start/end 或取最近 limit 根；工具只拉数据，不提供投资建议。",
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "股票／指数／ETF 代码或名称。例如 600519、贵州茅台、00700.HK、腾讯控股、AAPL、苹果、沪深300。",
            },
            "period": {
                "type": "string",
                "description": "K 线周期：day/week/month。默认 day。",
                "default": "day",
            },
            "start": {
                "type": "string",
                "description": "起始日期，YYYY-MM-DD 或 YYYYMMDD，可选。",
            },
            "end": {
                "type": "string",
                "description": "结束日期，YYYY-MM-DD 或 YYYYMMDD，可选。",
            },
            "limit": {
                "type": "integer",
                "description": "返回最近的多少根（未给定 start/end 时㉈使用）。默认 20，最大 250。",
                "default": 20,
            },
            "adjust": {
                "type": "string",
                "description": "复权：qfq（前复权）/hfq（后复权）/空字符串（不复权）。默认 qfq。",
                "default": "qfq",
            },
        },
        "required": ["symbol"],
    },
}

TIMEOUT_SEC = 60



def _format_rows(name, symbol, market, period, rows, source):
    pmap = {"day": "日K", "week": "周K", "month": "月K"}
    lines = ["📊 " + str(name) + "（" + str(symbol) + "，" + str(market) + "） " + pmap.get(period, "日K") + " 历史行情"]
    lines.append("")
    for r in rows:
        chg = ""
        if r.get("pct_chg") is not None:
            chg = "｜涨跌" + str(r.get("pct_chg")) + "%"
        lines.append(
            str(r.get("date")) + "：开" + str(r.get("open")) + "～收" + str(r.get("close"))
            + "ｘ高" + str(r.get("high")) + "｜低" + str(r.get("low")) + chg
        )
    lines.append("")
    lines.append("数据源：" + str(source) + "，共 " + str(len(rows)) + " 条。仅供参考，不构成投资建议。")
    return "\n".join(lines).strip()



def _run(args):
    from stocklib.resolver import resolve_symbol
    from stocklib.history import get_history

    symbol = str(args.get("symbol", "")).strip()
    if not symbol:
        return {"ok": False, "error": "no symbol", "data": {"result": "ERROR: 请提供 symbol。"}}
    period = str(args.get("period") or "day").lower()
    if period not in ("day", "week", "month"):
        period = "day"
    adjust = str(args.get("adjust") or "qfq").lower()
    if adjust not in ("qfq", "hfq", "", "none"):
        adjust = "qfq"
    if adjust == "none":
        adjust = ""
    try:
        limit = int(args.get("limit") or 20)
    except Exception:
        limit = 20
    limit = max(1, min(limit, 250))
    start = str(args.get("start") or "").replace("-", "").strip() or None
    end = str(args.get("end") or "").replace("-", "").strip() or None

    try:
        resolved = resolve_symbol(symbol)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "data": {"result": "ERROR: 无法识别标的：" + symbol}}

    try:
        rows, source, errors = get_history(resolved, period=period, limit=limit, start=start, end=end, adjust=adjust)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "data": {"result": "ERROR: " + str(exc)}}

    text = _format_rows(resolved.name or resolved.symbol, resolved.symbol, resolved.market, period, rows, source)
    return {
        "ok": True,
        "data": {
            "result": text,
            "symbol": resolved.symbol,
            "name": resolved.name,
            "market": resolved.market,
            "period": period,
            "adjust": adjust or "none",
            "rows": rows,
            "count": len(rows),
            "source": source,
        },
    }



if __name__ == "__main__":
    import json
    import os
    import sys
    import traceback

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    def output(result):
        print(json.dumps(result, ensure_ascii=False, default=str))
        sys.exit(0 if result.get("ok", True) else 1)

    try:
        raw_input = (sys.stdin.read() or "{}").lstrip("\ufeff")
        args = json.loads(raw_input)
        output(_run(args if isinstance(args, dict) else {}))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc), "data": {"result": "ERROR: stocktool_history failed: " + str(exc), "traceback": traceback.format_exc(limit=5)}}, ensure_ascii=False))
        sys.exit(1)
