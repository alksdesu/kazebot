from __future__ import annotations

import re
from urllib.parse import unquote


def validate_feature_path(path: str, *, operations: bool = False) -> None:
    domains = {"execution", "reminders", "materials", "community"}
    if operations:
        domains.add("operations")
    parts = path.split("/")
    try:
        segments = [unquote(part, errors="strict") for part in parts[3:]]
    except UnicodeDecodeError as error:
        raise ValueError("unsupported feature endpoint") from error
    if (
        len(parts) < 3 or parts[:2] != ["", "v1"] or parts[2] not in domains
        or any(not re.fullmatch(r"[A-Za-z0-9_.:-]+", part) or part in {".", ".."} for part in segments)
    ):
        raise ValueError("unsupported feature endpoint")
