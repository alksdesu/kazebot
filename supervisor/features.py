from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import logging

from . import (
    async_tools, community, execution, file_claims, materials_api, operations, processes, reminders,
    sticker_tagging, worktrees,
)
from .sticker_api import close_stores

logger = logging.getLogger(__name__)


def register_features(app, state, process_manager, config_store, features_config) -> None:
    state.feature_process_manager = process_manager
    state.feature_config_store = config_store
    state.execution = execution.ExecutionService(state)
    state.reminders = reminders.ReminderService(state)
    state.processes = processes.ProcessService(state)
    state.worktrees = worktrees.service_of(state)
    state.sticker_tagging = sticker_tagging.StickerTaggingService(state.workspace_root, features_config)
    host = getattr(state, "plugin_host", None)
    if host is not None:
        for name in ("execution", "reminders", "processes"):
            setattr(state, name, host.register_base(name, getattr(state, name), managed=True))
        for name, service in (("state", state), ("config_store", config_store), ("process_manager", process_manager),
                              ("eventlog", state.eventlog), ("hooks", state.hook_registry)):
            host.register_base(name, service)
    for module in (
        execution, reminders, community, materials_api, operations, file_claims, processes, async_tools, worktrees,
    ):
        app.include_router(module.create_router(state))


@asynccontextmanager
async def feature_lifespan(app):
    state = app.state.state
    host = getattr(state, "plugin_host", None)
    # 不经 register_features 搭起来的状态（测试替身之类）里没有 processes
    services = () if host is not None else tuple(
        service
        for service in (state.execution, state.reminders, getattr(state, "processes", None))
        if service is not None
    )
    # 隔离副本不归插件宿主管：清理时要停常驻进程，排在别的服务都起来之后，关的时候最先关
    worktree_service = getattr(state, "worktrees", None)
    started = []
    worktrees_started = False
    accounts = None
    try:
        for service in services:
            service.start()
            started.append(service)
        state.sticker_tagging.start()
        if host is not None:
            await host.start()
        if worktree_service is not None:
            await asyncio.to_thread(worktree_service.start)
            worktrees_started = True
        accounts = getattr(app.state, "platform_accounts", None)
        if accounts is not None:
            accounts.start()
        yield
    finally:
        if accounts is not None:
            await accounts.stop()
        if worktrees_started:
            try:
                await asyncio.to_thread(worktree_service.close)
            except Exception:
                logger.exception("Worktree service shutdown failed")
        if host is not None:
            await host.stop()
        try:
            await state.sticker_tagging.stop()
        except Exception:
            logger.exception("Sticker tagging shutdown failed")
        # The tagger is the long-lived holder of library handles here; close them once it has stopped.
        close_stores()
        for service in reversed(started):
            try:
                await asyncio.to_thread(service.close)
            except Exception:
                logger.exception("Feature service shutdown failed")
        materials_runtime = getattr(state, "materials_runtime", None)
        if materials_runtime is not None:
            await materials_runtime.close()
