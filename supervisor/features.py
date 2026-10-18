from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import logging

from . import community, execution, materials_api, operations, reminders, sticker_tagging
from .sticker_api import close_stores

logger = logging.getLogger(__name__)


def register_features(app, state, process_manager, config_store, features_config) -> None:
    state.feature_process_manager = process_manager
    state.feature_config_store = config_store
    state.execution = execution.ExecutionService(state)
    state.reminders = reminders.ReminderService(state)
    state.sticker_tagging = sticker_tagging.StickerTaggingService(state.workspace_root, features_config)
    host = getattr(state, "plugin_host", None)
    if host is not None:
        for name in ("execution", "reminders"):
            setattr(state, name, host.register_base(name, getattr(state, name), managed=True))
        for name, service in (("state", state), ("config_store", config_store), ("process_manager", process_manager),
                              ("eventlog", state.eventlog), ("hooks", state.hook_registry)):
            host.register_base(name, service)
    for module in (execution, reminders, community, materials_api, operations):
        app.include_router(module.create_router(state))


@asynccontextmanager
async def feature_lifespan(app):
    state = app.state.state
    host = getattr(state, "plugin_host", None)
    services = () if host is not None else (state.execution, state.reminders)
    started = []
    try:
        for service in services:
            service.start()
            started.append(service)
        state.sticker_tagging.start()
        if host is not None:
            await host.start()
        yield
    finally:
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
