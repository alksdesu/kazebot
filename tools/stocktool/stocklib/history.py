from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Any

import requests

from .akshare_source import safe_float
from .models import ResolvedSymbol

CN_TZ = timezone(timedelta(hours=8))
_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_TENCENT_PERIOD = {"day": "day", "week": "week", "month": "month"}
_US_SUFFIXES = (".OQ", ".N", "")


def _cn_tencent_code(resolved: ResolvedSymbol) -> str:
    code = resolved.query_symbol[-6:] if resolved.query_symbol.startswith(("sh", "sz")) else resolved.query_symbol
    if resolved.exchange == "SH":
        prefix = "sh"
    elif resolved.exchange == "SZ":
        prefix = "sz"
    else:
        prefix = "sh" if code.startswith(("5", "6", "9")) else "sz"
    return f"{prefix}{code}"


def _tencent_candidates(resolved: ResolvedSymbol) -> list:
    if resolved.market == "港股":
        return ["hk" + resolved.query_symbol.zfill(5)]
    if resolved.market == "美股":
        ticker = resolved.query_symbol.upper()
        return ["us" + ticker + suffix for suffix in _US_SUFFIXES]
    return [_cn_tencent_code(resolved)]



def _fq_tag(adjust: str) -> str:
    if adjust == "qfq":
        return "qfq"
    if adjust == "hfq":
        return "hfq"
    return ""


def _extract_series(node: dict, period: str) -> list:
    for key in (f"qfq{period}", f"hfq{period}", period):
        rows = node.get(key)
        if isinstance(rows, list) and rows:
            return rows
    return []


def _fetch_one(sym: str, period: str, count: int, fq: str, timeout: int) -> list:
    param = f"{sym},{period},,,{count},{fq}"
    resp = requests.get(_KLINE_URL, params={"param": param}, timeout=timeout,
                        headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    payload = json.loads(resp.text)
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    node = data.get(sym)
    if not isinstance(node, dict):
        return []
    return _extract_series(node, period)



def fetch_history_tencent(resolved: ResolvedSymbol, *, period: str = "day", limit: int = 20,
                          start: str = None, end: str = None,
                          adjust: str = "qfq", timeout: int = 10):
    period = _TENCENT_PERIOD.get(period, "day")
    fq = _fq_tag(adjust)
    # When an explicit date range is given, Tencent still returns the last N bars
    # (it ignores start/end), so pull a wide window and filter locally.
    if start or end:
        count = 320
    else:
        count = max(int(limit or 20), 20)
    count = max(1, min(count, 320))
    rows = []
    for sym in _tencent_candidates(resolved):
        try:
            rows = _fetch_one(sym, period, count, fq, timeout)
        except Exception:
            rows = []
        if rows:
            break
    if not rows:
        raise RuntimeError("腾讯K线无数据：" + resolved.symbol)
    out = []
    for row in rows:
        if len(row) < 6:
            continue
        out.append({
            "date": str(row[0]),
            "open": safe_float(row[1]),
            "close": safe_float(row[2]),
            "high": safe_float(row[3]),
            "low": safe_float(row[4]),
            "volume": safe_float(row[5]),
        })
    def _dnorm(d): return str(d).replace("-", "")
    if start:
        out = [r for r in out if _dnorm(r["date"]) >= start]
    if end:
        out = [r for r in out if _dnorm(r["date"]) <= end]
    # Limit only caps results when no explicit range was requested.
    if not start and not end and limit and len(out) > int(limit):
        out = out[-int(limit):]
    return out



def fetch_history_akshare_cn(resolved: ResolvedSymbol, *, period: str = "day", limit: int = 20,
                             start: str = None, end: str = None, adjust: str = "qfq"):
    import akshare as ak
    ak_period = {"day": "daily", "week": "weekly", "month": "monthly"}.get(period, "daily")
    s = (start or "19900101").replace("-", "")
    e = (end or datetime.now(CN_TZ).strftime("%Y%m%d")).replace("-", "")
    df = ak.stock_zh_a_hist(symbol=resolved.query_symbol[-6:], period=ak_period,
                            start_date=s, end_date=e, adjust=adjust or "")
    out = []
    for _, row in df.iterrows():
        out.append({
            "date": str(row.get("日期")),
            "open": safe_float(row.get("开盘")),
            "close": safe_float(row.get("收盘")),
            "high": safe_float(row.get("最高")),
            "low": safe_float(row.get("最低")),
            "volume": safe_float(row.get("成交量")),
            "amount": safe_float(row.get("成交额")),
            "pct_chg": safe_float(row.get("涨跌幯")),
        })
    if limit and len(out) > int(limit):
        out = out[-int(limit):]
    return out


def get_history(resolved: ResolvedSymbol, *, period: str = "day", limit: int = 20,
                start: str = None, end: str = None, adjust: str = "qfq"):
    errors = []
    try:
        rows = fetch_history_tencent(resolved, period=period, limit=limit,
                                 start=start, end=end, adjust=adjust)
        return rows, "tencent.ifzq.gtimg.cn", errors
    except Exception as exc:
        errors.append("tencent.ifzq.gtimg.cn: " + type(exc).__name__ + ": " + str(exc))
    if resolved.market == "A股" or resolved.asset_type in ("etf", "index"):
        try:
            rows = fetch_history_akshare_cn(resolved, period=period, limit=limit,
                                         start=start, end=end, adjust=adjust)
            return rows, "akshare.stock_zh_a_hist", errors
        except Exception as exc:
            errors.append("akshare.stock_zh_a_hist: " + type(exc).__name__ + ": " + str(exc))
    raise RuntimeError("所有历史行情数据源均失败：" + "；".join(errors))
