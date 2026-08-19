"""按 cron 在子进程里拉起 data_cleanup。

engine/data_cleanup.py 只有 `__main__` 入口和一个 systemd timer，Windows 部署上从来
没跑过 —— 记忆 14 天、附件 24 小时、node_contexts 48 小时这些保留策略一条都没生效。
这里补上进程内调度。用 subprocess 而不是直接调 main()：清理要遍历整个 data 目录，
放进 scheduler 的串行 tick 里会把 dream 和所有用户定时任务一起阻塞掉。
"""
from __future__ import annotations

import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clonoth_runtime import get_bool, get_str, load_runtime_config
from engine.cron import cron_match

log = logging.getLogger(__name__)

PLUGIN_META = {
    "handler_class": "DataCleanupScheduleHandler",
    "hook_points": [
        ("on_schedule_tick", "on_tick"),
    ],
    "priority": 90,
}

_SCHEDULE_TYPE = "data_cleanup"
_SCRIPT = Path(__file__).resolve().parent.parent / "data_cleanup.py"


class DataCleanupScheduleHandler:
    """cron 到点就 spawn 一次 data_cleanup，绝不等待它结束。"""

    name = "data_cleanup_schedule"

    def __init__(self) -> None:
        self._last_fired: str = ""
        self._running: subprocess.Popen | None = None

    def on_tick(self, ctx: dict[str, Any]) -> None:
        if str(ctx.get("schedule_type") or "").strip() != _SCHEDULE_TYPE:
            return
        # poll() 是唯一的 reap 动作。放在 cron 判断之后，退出的子进程要挂到下次命中才回收，
        # 而 cron 每小时才命中一次；清理一旦被停用，最后那个僵尸就再也没人收。
        if self._running is not None and self._running.poll() is not None:
            self._running = None
        workspace_root = ctx.get("workspace_root")
        if workspace_root is None:
            return
        workspace_root = Path(workspace_root)

        now_value = ctx.get("now")
        now = now_value if isinstance(now_value, datetime) else datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now_key = str(ctx.get("now_key") or now.strftime("%Y-%m-%d %H:%M"))
        if self._last_fired == now_key:
            return

        runtime_cfg = load_runtime_config(workspace_root)
        if not get_bool(runtime_cfg, "maintenance.data_cleanup.enabled", True):
            return
        cron_expr = get_str(runtime_cfg, "maintenance.data_cleanup.cron", "17 * * * *").strip()
        if not cron_expr:
            return

        if not cron_match(cron_expr, now):
            return
        self._last_fired = now_key

        # 上一轮还在跑就跳过：清理是幂等的，但两个进程同时遍历同一批目录只会互相
        # 撞上「文件已被对方删掉」的竞态。
        if self._running is not None:
            log.info("[data_cleanup] previous run still active, skipping this tick")
            return

        dry_run = get_bool(runtime_cfg, "maintenance.data_cleanup.dry_run", True)
        args = [sys.executable, str(_SCRIPT)]
        if dry_run:
            args.append("--dry-run")
        try:
            self._running = subprocess.Popen(
                args,
                cwd=str(workspace_root),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            log.warning("[data_cleanup] failed to spawn cleanup process", exc_info=True)
            return
        log.info(
            "[data_cleanup] spawned pid=%s dry_run=%s (log: data/logs/cleanup.log)",
            self._running.pid, dry_run,
        )
