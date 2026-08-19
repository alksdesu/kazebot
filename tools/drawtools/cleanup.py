from __future__ import annotations

"""Cleanup generated NovelAI attachment files.

Policy inspired by LittleWhiteBox gallery cache cleanup, adapted to filesystem
storage: delete expired files first, then trim oldest files until total size is
below the configured capacity limit.
"""

import time
from pathlib import Path
from typing import Any

from common import WORKSPACE_ROOT, load_settings

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def novelai_attachments_dir() -> Path:
    return WORKSPACE_ROOT / "data" / "attachments" / "novelai"


def _image_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return [p for p in root.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES]


def cleanup_novelai_attachments(settings: dict[str, Any] | None = None, *, force: bool = False) -> dict[str, Any]:
    settings = settings or load_settings()
    storage = settings.get("storage") if isinstance(settings.get("storage"), dict) else {}
    if not force and not bool(storage.get("cleanup_enabled", True)):
        return {"ok": True, "skipped": True, "reason": "cleanup disabled", "deleted_count": 0, "deleted_bytes": 0}

    root = novelai_attachments_dir()
    root.mkdir(parents=True, exist_ok=True)

    retention_days = float(storage.get("retention_days", 7) or 0)
    max_total_mb = float(storage.get("max_total_mb", 2048) or 0)
    now = time.time()
    cutoff = now - retention_days * 86400 if retention_days > 0 else None

    files = _image_files(root)
    deleted: list[dict[str, Any]] = []

    def delete_file(path: Path, reason: str) -> int:
        try:
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            deleted.append({"path": str(path.relative_to(WORKSPACE_ROOT).as_posix()), "size": size, "reason": reason})
            return size
        except Exception:
            return 0

    deleted_bytes = 0
    if cutoff is not None:
        for path in list(files):
            try:
                if path.stat().st_mtime < cutoff:
                    deleted_bytes += delete_file(path, "expired")
            except Exception:
                pass

    files = _image_files(root)
    total_size = 0
    sized_files: list[tuple[float, int, Path]] = []
    for path in files:
        try:
            stat = path.stat()
            total_size += stat.st_size
            sized_files.append((stat.st_mtime, stat.st_size, path))
        except Exception:
            pass

    max_bytes = int(max_total_mb * 1024 * 1024) if max_total_mb > 0 else 0
    if max_bytes > 0 and total_size > max_bytes:
        for _mtime, size, path in sorted(sized_files, key=lambda item: item[0]):
            if total_size <= max_bytes:
                break
            removed = delete_file(path, "capacity")
            if removed:
                total_size -= size
                deleted_bytes += removed

    remaining_files = _image_files(root)
    remaining_bytes = 0
    for path in remaining_files:
        try:
            remaining_bytes += path.stat().st_size
        except Exception:
            pass

    return {
        "ok": True,
        "skipped": False,
        "deleted_count": len(deleted),
        "deleted_bytes": deleted_bytes,
        "deleted_mb": round(deleted_bytes / 1024 / 1024, 3),
        "remaining_count": len(remaining_files),
        "remaining_bytes": remaining_bytes,
        "remaining_mb": round(remaining_bytes / 1024 / 1024, 3),
        "deleted": deleted[:200],
    }


def maybe_cleanup_novelai_attachments(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = settings or load_settings()
    storage = settings.get("storage") if isinstance(settings.get("storage"), dict) else {}
    if not bool(storage.get("cleanup_enabled", True)):
        return {"ok": True, "skipped": True, "reason": "cleanup disabled", "deleted_count": 0, "deleted_bytes": 0}
    interval = float(storage.get("cleanup_interval_sec", 3600) or 0)
    marker = WORKSPACE_ROOT / "data" / "temp" / "novelai" / "last_cleanup.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    if interval > 0 and marker.exists():
        try:
            last = float(marker.read_text(encoding="utf-8") or "0")
            if now - last < interval:
                return {"ok": True, "skipped": True, "reason": "interval", "deleted_count": 0, "deleted_bytes": 0}
        except Exception:
            pass
    result = cleanup_novelai_attachments(settings)
    try:
        marker.write_text(str(now), encoding="utf-8")
    except Exception:
        pass
    return result
