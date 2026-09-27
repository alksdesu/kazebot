"""NTQQ/NapCat 自己攒的图片视频缓存。

只用标准库、不 import 仓库里别的模块：宿主清理把这个文件装进 /usr/local/lib/kazebot，由 root 用系统解释器单独跑。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Protocol

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

# 部署脚本把各实例 NapCat 容器的 NTQQ 数据挂在这下面。
_NAPCAT_HOST_PARENT = Path("/opt")
_PROC_ROOT = Path("/proc")


class CacheRemover(Protocol):
    """删文件的一方：数据清理传的是它的上下文，宿主清理自己删。"""

    def remove_file(self, path: Path) -> None: ...

    def remove_empty_dir(self, path: Path) -> None: ...


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


def _deployed_ntqq_dirs() -> list[Path]:
    """部署脚本挂到宿主上的 NTQQ 数据目录：序号 0 在 /opt/napcat/qq，其余实例在 /opt/napcat-<实例名>/qq。

    容器停着时 /proc 里没有它的进程，只能按这里认。
    """
    try:
        homes = sorted(_NAPCAT_HOST_PARENT.glob("napcat-*"))
    except OSError:
        homes = []
    # 实例名里没有点；带点的是删实例时改名归档的目录，留给人去看。
    return [_NAPCAT_HOST_PARENT / "napcat" / "qq", *(home / "qq" for home in homes if "." not in home.name)]


def _running_as_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _owned_by_root(pid_dir: Path) -> bool:
    """真实和有效 uid 都是 0。看 status 而不看目录属主：进程关掉 dumpable 之后，/proc/<pid> 也显示成 root 的。"""
    try:
        status = (pid_dir / "status").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in status.splitlines():
        if line.startswith("Uid:"):
            return line.split()[1:3] == ["0", "0"]
    return False


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

    for p in (Path("/app/.config/QQ"), Path("/app/napcat"), Path("/root/.config/QQ"), *_deployed_ntqq_dirs()):
        _append_unique_path(roots, p)

    proc = _PROC_ROOT
    if not proc.exists():
        return roots

    # 以 root 跑时只跟 root 的进程：别的用户随手起一个命令行带 napcat 的进程，就能把 root 引到自己指定的目录去删文件。
    only_root_processes = _running_as_root()
    for pid_dir in proc.iterdir():
        if not pid_dir.name.isdigit():
            continue
        if only_root_processes and not _owned_by_root(pid_dir):
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


def purge_qq_internal_cache(context: CacheRemover) -> None:
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
    seen: set[tuple[int, int]] = set()

    for root in roots:
        try:
            if not _is_safe_qq_cache_root(root) or not root.exists() or not root.is_dir():
                continue
            # 同一个目录常有两条路：宿主上的挂载点，和经 /proc/<pid>/root 看到的容器里那条，扫一遍就够。
            root_stat = root.stat()
            if (root_stat.st_dev, root_stat.st_ino) in seen:
                continue
            seen.add((root_stat.st_dev, root_stat.st_ino))
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


class _HostRemover:
    """宿主清理没有工作区，也就没有数据清理那份上下文：演练和删除在这里自己管。"""

    def __init__(self, *, dry_run: bool) -> None:
        self._dry_run = dry_run

    def remove_file(self, path: Path) -> None:
        if self._dry_run:
            _log.info("[dry-run] would delete file %s", path)
            return
        path.unlink()

    def remove_empty_dir(self, path: Path) -> None:
        if self._dry_run:
            _log.info("[dry-run] would remove empty dir %s", path)
            return
        path.rmdir()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清本机各 NapCat 容器里 NTQQ 攒下的旧缓存，宿主清理的定时任务跑的就是它。")
    parser.add_argument("--dry-run", action="store_true", help="只列出会删什么，不动文件")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    purge_qq_internal_cache(_HostRemover(dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
