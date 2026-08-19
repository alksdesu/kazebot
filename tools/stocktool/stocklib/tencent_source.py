from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

import requests

from .akshare_source import safe_float
from .models import Quote, ResolvedSymbol

CN_TZ = timezone(timedelta(hours=8))


def _prefix(resolved: ResolvedSymbol) -> str:
    if resolved.exchange == "SH":
        return "sh"
    if resolved.exchange == "SZ":
        return "sz"
    code = resolved.query_symbol[-6:]
    return "sh" if code.startswith(("5", "6", "9")) else "sz"


def _parse_tencent_quote(resolved: ResolvedSymbol, query: str, *, timeout: int = 8) -> Quote:
    resp = requests.get(f"https://qt.gtimg.cn/q={query}", headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = "gbk"
    raw_text = resp.text.strip()
    if "~" not in raw_text:
        raise RuntimeError(f"腾讯财经未返回有效行情：{query}")
    body = raw_text.split('="', 1)[-1].rsplit('"', 1)[0]
    parts = body.split("~")
    if len(parts) < 10:
        raise RuntimeError(f"腾讯财经行情字段不足：{query}")

    def part(index: int) -> Any:
        return parts[index] if 0 <= index < len(parts) else None

    price = safe_float(part(3))
    pre_close = safe_float(part(4))
    change = safe_float(part(31))
    pct = safe_float(part(32))
    if change is None and price is not None and pre_close:
        change = price - pre_close
    if pct is None and change is not None and pre_close:
        pct = change / pre_close * 100

    raw = {"query": query, "parts": parts, "raw_text": raw_text[:1000]}
    currency = resolved.currency
    if resolved.market == "美股":
        currency = str(part(35) or resolved.currency or "USD")
    elif resolved.market == "港股":
        # Tencent HK quote payload contains many market metrics near the tail;
        # fixed HK listings should display HKD instead of accidentally reading
        # a metric column as currency.
        currency = "HKD"

    return Quote(
        input=resolved.input,
        symbol=resolved.symbol,
        name=str(part(1) or resolved.name or resolved.symbol),
        asset_type=resolved.asset_type,
        market=resolved.market,
        exchange=resolved.exchange,
        currency=currency,
        price=price,
        change=change,
        pct_chg=pct,
        open=safe_float(part(5)),
        high=safe_float(part(33)) or safe_float(part(41)),
        low=safe_float(part(34)) or safe_float(part(42)),
        pre_close=pre_close,
        volume=safe_float(part(6)),
        amount=safe_float(part(37)),
        timestamp=datetime.now(CN_TZ).isoformat(timespec="seconds"),
        source="tencent.qt.gtimg.cn",
        source_chain=["tencent.qt.gtimg.cn"],
        raw=raw,
    )


def quote(resolved: ResolvedSymbol, *, timeout: int = 8) -> Quote:
    code = resolved.query_symbol[-6:] if resolved.query_symbol.startswith(("sh", "sz")) else resolved.query_symbol
    query = f"{_prefix(resolved)}{code}"
    return _parse_tencent_quote(resolved, query, timeout=timeout)


def quote_hk(resolved: ResolvedSymbol, *, timeout: int = 8) -> Quote:
    code = resolved.query_symbol.zfill(5)
    return _parse_tencent_quote(resolved, f"hk{code}", timeout=timeout)


def quote_us(resolved: ResolvedSymbol, *, timeout: int = 8) -> Quote:
    ticker = resolved.query_symbol.upper()
    return _parse_tencent_quote(resolved, f"us{ticker}", timeout=timeout)
