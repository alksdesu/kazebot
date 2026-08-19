from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

from .akshare_source import safe_float
from .models import Quote, ResolvedSymbol

CN_TZ = timezone(timedelta(hours=8))


def _yahoo_ticker(resolved: ResolvedSymbol) -> str:
    if resolved.market == "港股":
        code = resolved.query_symbol.zfill(5)
        # Yahoo commonly uses 4-digit HK tickers such as 0700.HK, 9988.HK.
        return f"{code[-4:]}.HK"
    if resolved.market == "美股":
        return resolved.query_symbol.upper()
    return resolved.query_symbol


def _fast_get(fast: Any, key: str) -> Any:
    try:
        return fast[key]
    except Exception:
        try:
            return getattr(fast, key)
        except Exception:
            return None


def quote(resolved: ResolvedSymbol, *, timeout: int = 15) -> Quote:
    import yfinance as yf  # type: ignore

    ticker = _yahoo_ticker(resolved)
    stock = yf.Ticker(ticker)
    fast = getattr(stock, "fast_info", {})
    info = {}
    try:
        info = stock.info or {}
    except Exception:
        info = {}

    price = safe_float(_fast_get(fast, "last_price") or _fast_get(fast, "lastPrice") or info.get("currentPrice") or info.get("regularMarketPrice"))
    pre_close = safe_float(_fast_get(fast, "previous_close") or info.get("previousClose") or info.get("regularMarketPreviousClose"))
    open_price = safe_float(_fast_get(fast, "open") or info.get("regularMarketOpen"))
    high = safe_float(_fast_get(fast, "day_high") or info.get("dayHigh") or info.get("regularMarketDayHigh"))
    low = safe_float(_fast_get(fast, "day_low") or info.get("dayLow") or info.get("regularMarketDayLow"))
    volume = safe_float(_fast_get(fast, "last_volume") or info.get("volume") or info.get("regularMarketVolume"))
    currency = str(_fast_get(fast, "currency") or info.get("currency") or resolved.currency or "") or resolved.currency
    name = info.get("shortName") or info.get("longName") or resolved.name or resolved.symbol
    change = price - pre_close if price is not None and pre_close else None
    pct = change / pre_close * 100 if change is not None and pre_close else None

    if price is None:
        raise RuntimeError(f"yfinance 未返回有效价格：{ticker}")

    return Quote(
        input=resolved.input,
        symbol=resolved.symbol,
        name=str(name),
        asset_type=resolved.asset_type,
        market=resolved.market,
        exchange=resolved.exchange,
        currency=currency,
        price=price,
        change=change,
        pct_chg=pct,
        open=open_price,
        high=high,
        low=low,
        pre_close=pre_close,
        volume=volume,
        amount=None,
        timestamp=datetime.now(CN_TZ).isoformat(timespec="seconds"),
        source="yfinance",
        source_chain=["yfinance"],
        raw={"ticker": ticker, "info_subset": {k: info.get(k) for k in ["shortName", "longName", "currency", "currentPrice", "regularMarketPrice", "previousClose"]}},
    )
