from __future__ import annotations

import re
from typing import Any

from . import cache
from .config import RESOLVER_TTL_SEC
from .errors import ResolveError
from .models import ResolvedSymbol

INDEX_ALIASES = {
    "sh": "000001.SH",
    "上证": "000001.SH",
    "上证指数": "000001.SH",
    "沪指": "000001.SH",
    "sz": "399001.SZ",
    "深证": "399001.SZ",
    "深证成指": "399001.SZ",
    "深成指": "399001.SZ",
    "cyb": "399006.SZ",
    "创业板": "399006.SZ",
    "创业板指": "399006.SZ",
    "hs300": "000300.SH",
    "沪深300": "000300.SH",
    "zz500": "000905.SH",
    "中证500": "000905.SH",
}

INDEX_NAMES = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
}

US_NAME_ALIASES = {
    "苹果": "AAPL.US",
    "苹果公司": "AAPL.US",
    "特斯拉": "TSLA.US",
    "英伟达": "NVDA.US",
    "辉达": "NVDA.US",
    "微软": "MSFT.US",
    "谷歌": "GOOGL.US",
    "alphabet": "GOOGL.US",
    "亚马逊": "AMZN.US",
    "meta": "META.US",
    "脸书": "META.US",
    "奈飞": "NFLX.US",
    "英特尔": "INTC.US",
    "amd": "AMD.US",
}

US_DISPLAY_NAMES = {
    "AAPL.US": "苹果",
    "TSLA.US": "特斯拉",
    "NVDA.US": "英伟达",
    "MSFT.US": "微软",
    "GOOGL.US": "谷歌",
    "AMZN.US": "亚马逊",
    "META.US": "Meta",
    "NFLX.US": "奈飞",
    "INTC.US": "英特尔",
    "AMD.US": "AMD",
}

HK_NAME_ALIASES = {
    "腾讯": "00700.HK",
    "腾讯控股": "00700.HK",
    "阿里": "09988.HK",
    "阿里巴巴": "09988.HK",
    "美团": "03690.HK",
    "小米": "01810.HK",
    "小米集团": "01810.HK",
    "比亚迪股份": "01211.HK",
}

CN_NAME_ALIASES = {
    "贵州茅台": "600519.SH",
    "茅台": "600519.SH",
    "平安银行": "000001.SZ",
    "宁德时代": "300750.SZ",
    "招商银行": "600036.SH",
    "比亚迪": "002594.SZ",
    "五粮液": "000858.SZ",
    "中国平安": "601318.SH",
    "东方财富": "300059.SZ",
    "中芯国际": "688981.SH",
    "沪深300etf": "510300.SH",
    "沪深300ETF": "510300.SH",
}

SH_ETF_PREFIXES = ("510", "511", "512", "513", "515", "516", "517", "518", "560", "561", "562", "563", "588")
SZ_ETF_PREFIXES = ("159", "150", "160", "161", "162", "163", "164", "165")


def _clean(value: str) -> str:
    return str(value or "").strip()


def _upper_no_space(value: str) -> str:
    return re.sub(r"\s+", "", _clean(value)).upper()


def _is_etf_code(code: str) -> bool:
    return code.startswith(SH_ETF_PREFIXES + SZ_ETF_PREFIXES)


def _exchange_for_cn(code: str) -> str:
    if code.startswith(("6", "9", "5")):
        return "SH"
    return "SZ"


def _exchange_for_etf(code: str) -> str:
    if code.startswith(SH_ETF_PREFIXES):
        return "SH"
    return "SZ"


def _resolved(symbol: str, query_symbol: str, name: str | None, asset_type: str, market: str, exchange: str | None, currency: str | None, raw_input: str, warnings: list[str] | None = None) -> ResolvedSymbol:
    return ResolvedSymbol(
        input=raw_input,
        symbol=symbol,
        query_symbol=query_symbol,
        name=name,
        asset_type=asset_type,
        market=market,
        exchange=exchange,
        currency=currency,
        warnings=warnings or [],
    )


def _load_akshare_name_map(kind: str) -> list[dict[str, str]]:
    cached = cache.get("resolver", kind)
    if isinstance(cached, list):
        return cached
    rows: list[dict[str, str]] = []
    try:
        import akshare as ak  # type: ignore
        if kind == "cn_names":
            try:
                df = ak.stock_info_a_code_name()
            except Exception:
                df = ak.stock_zh_a_spot_em()
            code_col = "code" if "code" in df.columns else "代码"
            name_col = "name" if "name" in df.columns else "名称"
            for _, row in df.iterrows():
                code = str(row.get(code_col, "")).strip().zfill(6)
                name = str(row.get(name_col, "")).strip()
                if code and name:
                    ex = _exchange_for_etf(code) if _is_etf_code(code) else _exchange_for_cn(code)
                    rows.append({"name": name, "symbol": f"{code}.{ex}"})
        elif kind == "etf_names":
            df = ak.fund_etf_spot_em()
            for _, row in df.iterrows():
                code = str(row.get("代码", "")).strip().zfill(6)
                name = str(row.get("名称", "")).strip()
                if code and name:
                    rows.append({"name": name, "symbol": f"{code}.{_exchange_for_etf(code)}"})
        elif kind == "hk_names":
            df = ak.stock_hk_spot_em()
            for _, row in df.iterrows():
                code = str(row.get("代码", "")).strip()
                name = str(row.get("名称", "")).strip()
                if code and name:
                    rows.append({"name": name, "symbol": f"{code.zfill(5)}.HK"})
    except Exception:
        rows = []
    if rows:
        cache.set("resolver", kind, rows, RESOLVER_TTL_SEC)
    return rows


def _resolve_by_name(raw: str) -> ResolvedSymbol | None:
    key = _clean(raw)
    key_no_space = re.sub(r"\s+", "", key)
    lower_key = key_no_space.lower()

    if key in CN_NAME_ALIASES or lower_key in {k.lower(): v for k, v in CN_NAME_ALIASES.items()}:
        symbol = CN_NAME_ALIASES.get(key) or {k.lower(): v for k, v in CN_NAME_ALIASES.items()}[lower_key]
        return resolve_symbol(symbol, raw_input=raw, forced_name=key)
    if key in HK_NAME_ALIASES:
        return resolve_symbol(HK_NAME_ALIASES[key], raw_input=raw, forced_name=key)
    if lower_key in US_NAME_ALIASES:
        return resolve_symbol(US_NAME_ALIASES[lower_key], raw_input=raw, forced_name=key)

    candidates: list[dict[str, str]] = []
    candidates.extend(_load_akshare_name_map("cn_names"))
    candidates.extend(_load_akshare_name_map("etf_names"))
    candidates.extend(_load_akshare_name_map("hk_names"))

    exact = [item for item in candidates if item.get("name") == key]
    if exact:
        return resolve_symbol(exact[0]["symbol"], raw_input=raw, forced_name=exact[0].get("name"))

    fuzzy = [item for item in candidates if key and (key in item.get("name", "") or item.get("name", "") in key)]
    if fuzzy:
        return resolve_symbol(fuzzy[0]["symbol"], raw_input=raw, forced_name=fuzzy[0].get("name"))
    return None


def resolve_symbol(raw: str, *, market_hint: str = "auto", raw_input: str | None = None, forced_name: str | None = None) -> ResolvedSymbol:
    original = raw_input if raw_input is not None else raw
    value = _clean(raw)
    if not value:
        raise ResolveError("empty symbol")
    compact_upper = _upper_no_space(value)
    hint = _clean(market_hint).lower() or "auto"

    if value in INDEX_ALIASES or compact_upper.lower() in INDEX_ALIASES:
        symbol = INDEX_ALIASES.get(value) or INDEX_ALIASES[compact_upper.lower()]
        code, ex = symbol.split(".")
        prefix = "sh" if ex == "SH" else "sz"
        return _resolved(symbol, f"{prefix}{code}", INDEX_NAMES.get(symbol), "index", "指数", ex, "CNY", original)

    if compact_upper in INDEX_NAMES:
        code, ex = compact_upper.split(".")
        prefix = "sh" if ex == "SH" else "sz"
        return _resolved(compact_upper, f"{prefix}{code}", INDEX_NAMES.get(compact_upper), "index", "指数", ex, "CNY", original)

    hk_match = re.match(r"^(?:HK)?(\d{1,5})(?:\.HK)?$", compact_upper)
    if hk_match and (hint == "hk" or ".HK" in compact_upper or compact_upper.startswith("HK")):
        code = hk_match.group(1).zfill(5)
        return _resolved(f"{code}.HK", code, forced_name, "stock", "港股", "HK", "HKD", original)

    if re.match(r"^\d{6}(?:\.(SH|SZ))?$", compact_upper):
        code = compact_upper.split(".")[0]
        ex = compact_upper.split(".")[1] if "." in compact_upper else (_exchange_for_etf(code) if _is_etf_code(code) else _exchange_for_cn(code))
        asset_type = "etf" if _is_etf_code(code) or hint == "etf" else "stock"
        market = "ETF" if asset_type == "etf" else "A股"
        return _resolved(f"{code}.{ex}", code, forced_name, asset_type, market, ex, "CNY", original)

    us_value = compact_upper[:-3] if compact_upper.endswith(".US") else compact_upper
    if re.match(r"^[A-Z][A-Z0-9.\-]{0,9}$", us_value) and (hint == "us" or compact_upper.endswith(".US") or re.match(r"^[A-Z][A-Z0-9.\-]{0,9}$", compact_upper)):
        ticker = us_value
        symbol = f"{ticker}.US"
        return _resolved(symbol, ticker, forced_name or US_DISPLAY_NAMES.get(symbol), "stock", "美股", "US", "USD", original)

    named = _resolve_by_name(value)
    if named is not None:
        return named

    raise ResolveError(f"无法识别股票/指数/ETF：{original}")


def resolve_many(inputs: list[str], *, market_hint: str = "auto") -> tuple[list[ResolvedSymbol], list[dict[str, Any]]]:
    resolved: list[ResolvedSymbol] = []
    failures: list[dict[str, Any]] = []
    for item in inputs:
        try:
            resolved.append(resolve_symbol(item, market_hint=market_hint))
        except Exception as exc:
            failures.append({"input": item, "error": str(exc)})
    return resolved, failures
