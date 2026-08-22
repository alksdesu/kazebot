"""控制台的账号清单。

多开时每个号是一套独立进程，各自挂在自己的 URL 前缀下，靠这份共享文件互相看见。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

_ENV_FILE = "CLONOTH_INSTANCES_FILE"
_ENV_PREFIX = "CLONOTH_URL_PREFIX"


def url_prefix() -> str:
    """本实例挂在哪个 URL 前缀下。单实例为空串，路由与挂载前逐字相同。"""
    raw = str(os.environ.get(_ENV_PREFIX) or "").strip().strip("/")
    return f"/{raw}" if raw else ""


def instances_file(workspace_root: Path) -> Path:
    """清单位置。多实例要都指向同一份，否则各自只看得见自己。"""
    raw = str(os.environ.get(_ENV_FILE) or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(workspace_root) / "config" / "instances.yaml"


def _clean(entry: Any) -> dict[str, str] | None:
    if not isinstance(entry, dict):
        return None
    uin = str(entry.get("uin") or "").strip()
    if not uin:
        return None
    path = str(entry.get("path") or "").strip().strip("/")
    return {
        "uin": uin,
        "label": str(entry.get("label") or "").strip() or uin,
        "path": f"/{path}" if path else "",
    }


def load_instances(workspace_root: Path) -> list[dict[str, str]]:
    """读共享清单。文件不存在就是单实例部署，空表让前端不必渲染切换器。"""
    try:
        raw = yaml.safe_load(instances_file(workspace_root).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    rows = raw.get("instances") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    seen: set[str] = set()
    cleaned: list[dict[str, str]] = []
    for entry in rows:
        row = _clean(entry)
        # 同一个号写两条会让切换器多出一个永远点不到的重复项。
        if row and row["uin"] not in seen:
            seen.add(row["uin"])
            cleaned.append(row)
    return cleaned
