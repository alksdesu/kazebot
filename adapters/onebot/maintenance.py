"""QQ 这一侧的定期维护：清 NTQQ/NapCat 自己攒的图片视频缓存，和适配器的引用附件索引。"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from adapters._shared.env import env_first
from platforms.maintenance import MaintenanceContext, MaintenanceTask

_log = logging.getLogger(__name__)

# QQ Electron 运行时可能在独立 mount namespace 中使用 /app/.config/QQ；systemd timer 在宿主
# namespace 运行时需要通过 /proc/<qq-pid>/root/... 访问这些路径。
#
# NTQQ 常见缓存还包括账号目录下的 Image/Video，例如：
#   /app/.config/QQ/<qq号>/Image
#   /app/.config/QQ/<qq号>/Video
# 这些目录里的文件不一定都有标准扩展名，所以清理逻辑除扩展名外，
# 也会按父目录名 image/video/cache/tmp/download 等识别旧缓存文件。
QQ_CACHE_MAX_AGE = float(os.getenv("CLONOTH_QQ_CACHE_MAX_AGE_SECONDS", str(7 * 24 * 3600)))
QQ_CACHE_ROOTS_RAW = os.getenv("CLONOTH_QQ_CACHE_ROOTS", "")
QQ_CACHE_MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".m4v",
}
QQ_CACHE_DIR_KEYWORDS = {
    "cache", "cache_data", "code cache", "gpucache", "blob_storage",
    "tmp", "temp", "download", "downloads", "thumb", "thumbnail", "thumbnails",
    # Explicitly cover NTQQ account media cache directories: <qq号>/Image and <qq号>/Video.
    "image", "images", "pic", "pics", "video", "videos", "emoji", "face", "record",
}

# 适配器自己的缓存（引用附件索引这类）指向已落盘的附件；附件本体由核心的 attachments 清理，这里按文件龄保守清，与附件同龄期。
ADAPTER_CACHE_MAX_AGE = float(os.getenv("CLONOTH_ONEBOT_CACHE_MAX_AGE_SECONDS", str(24 * 3600)))
# 适配器往 data/cache 写的都以 onebot_ 开头，和平台描述 owned_files 里的 data/cache/onebot_* 一致；以后多一份也照样清得到。
ADAPTER_CACHE_PATTERN = "onebot_*"


def _adapter_cache_dir(workspace_root: Path) -> Path:
    return Path(workspace_root) / "data" / "cache"


def reply_attachment_cache_file(workspace_root: Path) -> Path:
    """引用消息附件索引的位置。适配器写、定期维护清，两边都从这里取；清理进程因此不用导入 config 去读密钥文件。"""
    return Path(env_first(
        "ONEBOT_REPLY_ATTACHMENT_CACHE_FILE",
        default=str(_adapter_cache_dir(workspace_root) / "onebot_reply_attachments.json"),
    ))


def _split_configured_roots(raw: str) -> list[Path]:
    roots: list[Path] = []
    for item in str(raw or "").replace("\n", ",").split(","):
        value = item.strip()
        if value:
            roots.append(Path(value))
    return roots


def _read_proc_strings(path: Path) -> list[str]:
    try:
        data = path.read_bytes()
    except Exception:
        return []
    return [x.decode("utf-8", "replace") for x in data.split(b"\0") if x]


def _append_unique_path(paths: list[Path], path: Path) -> None:
    key = str(path)
    if not key or key == ".":
        return
    if all(str(x) != key for x in paths):
        paths.append(path)


def _discover_qq_cache_roots() -> list[Path]:
    """Return conservative candidate roots for NTQQ/NapCat cache cleanup.

    On the Debian server QQ is launched from /opt/QQ/qq but runs with HOME=/app and
    Chromium child processes use --user-data-dir=/app/.config/QQ.  When /app lives
    only in QQ's mount namespace, the host-side timer can still access it through
    /proc/<pid>/root/app/....
    """
    roots: list[Path] = []
    for p in _split_configured_roots(QQ_CACHE_ROOTS_RAW):
        _append_unique_path(roots, p)

    for p in (Path("/app/.config/QQ"), Path("/app/napcat"), Path("/root/.config/QQ")):
        _append_unique_path(roots, p)

    proc = Path("/proc")
    if not proc.exists():
        return roots

    for pid_dir in proc.iterdir():
        if not pid_dir.name.isdigit():
            continue
        cmdline = _read_proc_strings(pid_dir / "cmdline")
        environ = _read_proc_strings(pid_dir / "environ")
        haystack = "\n".join(cmdline + environ)
        if "/opt/QQ/qq" not in haystack and "--user-data-dir=" not in haystack and "napcat" not in haystack.lower():
            continue

        proc_root = pid_dir / "root"
        for index, arg in enumerate(cmdline):
            user_data_dir = ""
            if arg.startswith("--user-data-dir="):
                user_data_dir = arg.split("=", 1)[1]
            elif arg == "--user-data-dir" and index + 1 < len(cmdline):
                user_data_dir = cmdline[index + 1]
            if user_data_dir.startswith("/"):
                _append_unique_path(roots, proc_root / user_data_dir.lstrip("/"))

        home = ""
        for item in environ:
            if item.startswith("HOME="):
                home = item.split("=", 1)[1].strip()
                break
        if home.startswith("/"):
            _append_unique_path(roots, proc_root / home.lstrip("/") / ".config" / "QQ")
            _append_unique_path(roots, proc_root / home.lstrip("/") / "napcat")

    return roots


def _is_safe_qq_cache_root(root: Path) -> bool:
    text = str(root)
    if not text or text in {"/", "/app", "/root", "/proc"}:
        return False
    lowered = text.lower()
    return "qq" in lowered or "napcat" in lowered


def _is_cache_like_relative_path(rel: Path) -> bool:
    for part in rel.parts[:-1]:
        lowered = part.lower()
        compact = lowered.replace(" ", "")
        if any(keyword in lowered or keyword.replace(" ", "") in compact for keyword in QQ_CACHE_DIR_KEYWORDS):
            return True
    return False


def purge_qq_internal_cache(context: MaintenanceContext) -> None:
    """Clean stale NTQQ/NapCat image/video/cache files.

    This deliberately does not wipe whole QQ data directories. It only removes old
    media files or files inside cache-like directories, including NTQQ account
    media caches such as <qq号>/Image and <qq号>/Video. This avoids deleting login
    state, account databases, and other durable QQ configuration.
    """
    if QQ_CACHE_MAX_AGE <= 0:
        _log.info("[qq_cache] disabled")
        return

    cutoff = time.time() - QQ_CACHE_MAX_AGE
    deleted = 0
    deleted_bytes = 0
    scanned_roots = 0
    roots = _discover_qq_cache_roots()

    for root in roots:
        try:
            if not _is_safe_qq_cache_root(root) or not root.exists() or not root.is_dir():
                continue
            scanned_roots += 1
            for p in root.rglob("*"):
                try:
                    if not p.is_file():
                        continue
                    st = p.stat()
                    if st.st_mtime >= cutoff:
                        continue
                    rel = p.relative_to(root)
                    is_media = p.suffix.lower() in QQ_CACHE_MEDIA_EXTENSIONS
                    is_cache_file = _is_cache_like_relative_path(rel)
                    if not (is_media or is_cache_file):
                        continue
                    size = st.st_size
                    context.remove_file(p)
                    deleted += 1
                    deleted_bytes += size
                except Exception:
                    continue

            # Remove empty cache/media subdirectories, but never the root itself.
            for dirpath, _, _ in os.walk(str(root), topdown=False):
                dp = Path(dirpath)
                if dp == root:
                    continue
                try:
                    rel = dp.relative_to(root)
                    if not _is_cache_like_relative_path(rel / "placeholder"):
                        continue
                    next(dp.iterdir())
                except StopIteration:
                    try:
                        context.remove_empty_dir(dp)
                    except Exception:
                        pass
                except Exception:
                    pass
        except Exception as exc:
            _log.info("[qq_cache] skipped %s: %s", root, exc)

    _log.info(
        "[qq_cache] scanned_roots=%d deleted=%d freed=%dKB max_age_hours=%.1f",
        scanned_roots,
        deleted,
        deleted_bytes // 1024,
        QQ_CACHE_MAX_AGE / 3600,
    )


def _adapter_cache_files(context: MaintenanceContext) -> list[Path]:
    index = reply_attachment_cache_file(context.workspace_root)
    # 写到一半崩掉会留下 .tmp；索引用 env 改过位置时不在 data/cache 下，单独算上。
    found = {index, index.with_suffix(index.suffix + ".tmp")}
    try:
        found.update(_adapter_cache_dir(context.workspace_root).glob(ADAPTER_CACHE_PATTERN))
    except OSError:
        pass
    return sorted(found)


def purge_adapter_cache(context: MaintenanceContext) -> None:
    cutoff = time.time() - ADAPTER_CACHE_MAX_AGE
    deleted = 0
    for candidate in _adapter_cache_files(context):
        try:
            if candidate.is_file() and candidate.stat().st_mtime < cutoff:
                context.remove_file(candidate)
                deleted += 1
        except OSError:
            continue
    _log.info("[onebot_cache] deleted %d files", deleted)


def maintenance_tasks() -> tuple[MaintenanceTask, ...]:
    return (
        MaintenanceTask("onebot_cache", purge_adapter_cache),
        MaintenanceTask("qq_cache", purge_qq_internal_cache),
    )
