from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:  # 工具以子进程跑，sys.path 里只有自己那一级
    sys.path.insert(0, str(_REPO_ROOT))
from workspace import resolve_workspace_root  # noqa: E402

CACHE_ROOT = resolve_workspace_root(_REPO_ROOT) / "data" / "stocktool" / "cache"


def _safe_name(value: str) -> str:
    text = str(value or "").strip().replace("/", "_").replace("\\", "_")
    text = re.sub(r"[^0-9A-Za-z_.\-\u4e00-\u9fff]+", "_", text)
    return text[:160] or "empty"


def cache_path(namespace: str, key: str) -> Path:
    return CACHE_ROOT / _safe_name(namespace) / f"{_safe_name(key)}.json"


def get(namespace: str, key: str) -> Any | None:
    path = cache_path(namespace, key)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        created_at = float(payload.get("created_at", 0))
        ttl_sec = float(payload.get("ttl_sec", 0))
        if ttl_sec <= 0 or time.time() - created_at > ttl_sec:
            return None
        return payload.get("value")
    except Exception:
        return None


def set(namespace: str, key: str, value: Any, ttl_sec: int | float) -> None:
    path = cache_path(namespace, key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"created_at": time.time(), "ttl_sec": float(ttl_sec), "value": value}
        path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    except Exception:
        # Cache must never make the market data tool fail.
        return
