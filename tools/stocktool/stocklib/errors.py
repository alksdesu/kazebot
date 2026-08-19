from __future__ import annotations


class StockToolError(RuntimeError):
    """Base error for stocktool."""


class ResolveError(StockToolError):
    """Raised when a user symbol/name cannot be resolved."""


class DataSourceError(StockToolError):
    """Raised when all quote data sources fail."""
