from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class ResolvedSymbol:
    input: str
    symbol: str
    query_symbol: str
    name: str | None
    asset_type: str
    market: str
    exchange: str | None
    currency: str | None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Quote:
    input: str
    symbol: str
    name: str | None
    asset_type: str
    market: str
    exchange: str | None
    currency: str | None
    price: float | None = None
    change: float | None = None
    pct_chg: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    pre_close: float | None = None
    volume: float | None = None
    amount: float | None = None
    timestamp: str | None = None
    source: str = ""
    source_chain: list[str] = field(default_factory=list)
    cached: bool = False
    warnings: list[str] = field(default_factory=list)
    raw: dict[str, Any] | None = None

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        data = asdict(self)
        if not include_raw:
            data.pop("raw", None)
        return data
