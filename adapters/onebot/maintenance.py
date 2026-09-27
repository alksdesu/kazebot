"""QQ 这一侧的定期维护：适配器的引用附件索引，和 NTQQ/NapCat 自己攒的图片视频缓存（要 root，归宿主清理）。"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from adapters._shared.env import env_first
from adapters.onebot.qq_cache import purge_qq_internal_cache
from platforms.maintenance import SCOPE_HOST, MaintenanceContext, MaintenanceTask

_log = logging.getLogger(__name__)

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
        # 缓存在 NapCat 容器里、归 root，实例自己的清理删不动。
        MaintenanceTask("qq_cache", purge_qq_internal_cache, scope=SCOPE_HOST),
    )
