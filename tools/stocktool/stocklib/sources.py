from __future__ import annotations

from typing import Callable

from . import cache
from .config import FAILURE_TTL_SEC, QUOTE_TTL_SEC
from .errors import DataSourceError
from .models import Quote, ResolvedSymbol


def _ttl(resolved: ResolvedSymbol) -> int:
    if resolved.asset_type == "etf":
        return QUOTE_TTL_SEC["etf"]
    if resolved.asset_type == "index":
        return QUOTE_TTL_SEC["index"]
    if resolved.market == "港股":
        return QUOTE_TTL_SEC["hk"]
    if resolved.market == "美股":
        return QUOTE_TTL_SEC["us"]
    return QUOTE_TTL_SEC["cn"]


def _cache_key(resolved: ResolvedSymbol) -> str:
    return f"{resolved.market}_{resolved.asset_type}_{resolved.symbol}"


def _quote_from_cache(resolved: ResolvedSymbol, include_raw: bool) -> Quote | None:
    value = cache.get("quote", _cache_key(resolved))
    if not isinstance(value, dict):
        return None
    try:
        q = Quote(**value)
        q.cached = True
        if not include_raw:
            q.raw = None
        return q
    except Exception:
        return None


def _store_quote(resolved: ResolvedSymbol, quote: Quote) -> None:
    data = quote.to_dict(include_raw=True)
    data["cached"] = False
    cache.set("quote", _cache_key(resolved), data, _ttl(resolved))


def _try_chain(resolved: ResolvedSymbol, funcs: list[tuple[str, Callable[[ResolvedSymbol], Quote]]]) -> Quote:
    errors: list[str] = []
    attempted: list[str] = []
    for label, func in funcs:
        attempted.append(label)
        try:
            quote = func(resolved)
            quote.source_chain = attempted.copy()
            quote.warnings.extend(resolved.warnings)
            if errors:
                quote.warnings.append("部分数据源失败，已使用兜底源：" + "；".join(errors[-2:]))
            return quote
        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
            continue
    cache.set("quote_failure", _cache_key(resolved), {"errors": errors}, FAILURE_TTL_SEC)
    raise DataSourceError("所有行情数据源均失败：" + "；".join(errors))


def get_quote(resolved: ResolvedSymbol, *, refresh: bool = False, include_raw: bool = False) -> Quote:
    if not refresh:
        cached = _quote_from_cache(resolved, include_raw)
        if cached is not None:
            return cached

    from . import akshare_source, tencent_source, yfinance_source

    if resolved.asset_type == "etf":
        funcs = [
            ("akshare.fund_etf_spot_em", akshare_source.quote_etf),
            ("tencent.qt.gtimg.cn", tencent_source.quote),
        ]
    elif resolved.asset_type == "index":
        funcs = [
            ("akshare.stock_zh_index_spot_em", akshare_source.quote_index),
            ("tencent.qt.gtimg.cn", tencent_source.quote),
        ]
    elif resolved.market == "港股":
        # Why: in some deployments AkShare HK and Yahoo/yfinance are slow or blocked,
        # while Tencent's lightweight quote endpoint is fast and cookie-free. How:
        # prefer Tencent for stability, keeping AkShare and yfinance as additional
        # fallbacks. Purpose: make QQ quote queries return within tool timeouts.
        funcs = [
            ("tencent.qt.gtimg.cn", tencent_source.quote_hk),
            ("akshare.stock_hk_spot_em", akshare_source.quote_hk),
            ("yfinance", yfinance_source.quote),
        ]
    elif resolved.market == "美股":
        # See HK note above. Tencent US quote endpoint avoids slow full-market
        # AkShare pulls and Yahoo 403 failures in restricted network environments.
        funcs = [
            ("tencent.qt.gtimg.cn", tencent_source.quote_us),
            ("akshare.stock_us_spot_em", akshare_source.quote_us),
            ("yfinance", yfinance_source.quote),
        ]
    else:
        # See HK/US note above. AkShare full-market snapshot endpoints are
        # frequently rate-limited or drop connections on some servers, forcing
        # a slow fail-then-fallback each call. Prefer the lightweight Tencent
        # quote, keeping AkShare as a fallback for extra coverage.
        funcs = [
            ("tencent.qt.gtimg.cn", tencent_source.quote),
            ("akshare.stock_zh_a_spot_em", akshare_source.quote_cn),
        ]

    quote = _try_chain(resolved, funcs)
    _store_quote(resolved, quote)
    if not include_raw:
        quote.raw = None
    return quote
