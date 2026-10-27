"""委派时要求隔离的子任务在自己的 git worktree 里做：supervisor 建副本、收尾算 diff、合回或放弃、清理。"""
from __future__ import annotations

from .api import create_router
from .service import NO_ISOLATION, IsolationPlan, WorktreeService, service_of

__all__ = ["NO_ISOLATION", "IsolationPlan", "WorktreeService", "create_router", "service_of"]
