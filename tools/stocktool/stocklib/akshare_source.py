from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

from .models import Quote, ResolvedSymbol

CN_TZ = timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        text = str(value).strip().replace(",", "")
        if text in {"", "-", "--", "nan", "None"}:
            return None
        return float(text)
    except Exception:
        return None


def row_get(row: Any, names: list[str]) -> Any:
    for name in names:
        try:
            value = row.get(name)
            if value is not None:
                return value
        except Exception:
            pass
    return None


def _base_quote(resolved: ResolvedSymbol, source: str, raw: dict[str, Any]) -> Quote:
    price = safe_float(row_get(raw, ["最新价", "现价", "最新", "Price", "price"]))
    pre_close = safe_float(row_get(raw, ["昨收", "昨收价", "PreClose", "pre_close"]))
    change = safe_float(row_get(raw, ["涨跌额", "涨跌", "change", "PriceChange"]))
    pct = safe_float(row_get(raw, ["涨跌幅", "涨幅", "pct_chg", "change_percent", "PriceChangePercent"]))
    if change is None and price is not None and pre_close:
        change = price - pre_close
    if pct is None and change is not None and pre_close:
        pct = change / pre_close * 100
    return Quote(
        input=resolved.input,
        symbol=resolved.symbol,
        name=str(row_get(raw, ["名称", "name", "Name"]) or resolved.name or resolved.symbol),
        asset_type=resolved.asset_type,
        market=resolved.market,
        exchange=resolved.exchange,
        currency=resolved.currency,
        price=price,
        change=change,
        pct_chg=pct,
        open=safe_float(row_get(raw, ["今开", "开盘", "开盘价", "Open", "open"])),
        high=safe_float(row_get(raw, ["最高", "最高价", "High", "high"])),
        low=safe_float(row_get(raw, ["最低", "最低价", "Low", "low"])),
        pre_close=pre_close,
        volume=safe_float(row_get(raw, ["成交量", "Volume", "volume"])),
        amount=safe_float(row_get(raw, ["成交额", "Amount", "amount"])),
        timestamp=now_iso(),
        source=source,
        source_chain=[source],
        raw=raw,
    )


def quote_cn(resolved: ResolvedSymbol) -> Quote:
    import akshare as ak  # type: ignore
    df = ak.stock_zh_a_spot_em()
    code = resolved.query_symbol
    match = df[df["代码"].astype(str).str.zfill(6) == code]
    if match.empty:
        raise RuntimeError(f"AkShare A股未找到代码 {code}")
    return _base_quote(resolved, "akshare.stock_zh_a_spot_em", match.iloc[0].to_dict())


def quote_etf(resolved: ResolvedSymbol) -> Quote:
    import akshare as ak  # type: ignore
    df = ak.fund_etf_spot_em()
    code = resolved.query_symbol
    match = df[df["代码"].astype(str).str.zfill(6) == code]
    if match.empty:
        raise RuntimeError(f"AkShare ETF未找到代码 {code}")
    return _base_quote(resolved, "akshare.fund_etf_spot_em", match.iloc[0].to_dict())


def quote_index(resolved: ResolvedSymbol) -> Quote:
    import akshare as ak  # type: ignore
    df = ak.stock_zh_index_spot_em()
    code = resolved.query_symbol[-6:] if resolved.query_symbol.startswith(("sh", "sz")) else resolved.query_symbol
    match = df[df["代码"].astype(str).str.zfill(6) == code]
    if match.empty:
        raise RuntimeError(f"AkShare 指数未找到代码 {code}")
    return _base_quote(resolved, "akshare.stock_zh_index_spot_em", match.iloc[0].to_dict())


def quote_hk(resolved: ResolvedSymbol) -> Quote:
    import akshare as ak  # type: ignore
    df = ak.stock_hk_spot_em()
    code5 = resolved.query_symbol.zfill(5)
    code_no_zero = str(int(code5)) if code5.isdigit() else code5
    codes = df["代码"].astype(str)
    match = df[(codes.str.zfill(5) == code5) | (codes == code_no_zero)]
    if match.empty:
        raise RuntimeError(f"AkShare 港股未找到代码 {code5}")
    return _base_quote(resolved, "akshare.stock_hk_spot_em", match.iloc[0].to_dict())


def quote_us(resolved: ResolvedSymbol) -> Quote:
    import akshare as ak  # type: ignore
    df = ak.stock_us_spot_em()
    ticker = resolved.query_symbol.upper()
    codes = df["代码"].astype(str).str.upper()
    match = df[(codes == ticker) | (codes.str.endswith(f".{ticker}"))]
    if match.empty:
        raise RuntimeError(f"AkShare 美股未找到代码 {ticker}")
    return _base_quote(resolved, "akshare.stock_us_spot_em", match.iloc[0].to_dict())
