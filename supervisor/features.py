from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import logging

from . import community, materials_api

logger = logging.getLogger(__name__)


def register_features(app, state, process_manager, config_store) -> None:
    state.feature_process_manager = process_manager
    state.feature_config_store = config_store
    for module in (community, materials_api):
        app.include_router(module.create_router(state))


@asynccontextmanager
async def feature_lifespan(app):
    state = app.state.state
    services = ()
    started = []
    try:
        for service in services:
            service.start()
            started.append(service)
        yield
    finally:
        for service in reversed(started):
            try:
                await asyncio.to_thread(service.close)
            except Exception:
                logger.exception("Feature service shutdown failed")
        materials_runtime = getattr(state, "materials_runtime", None)
        if materials_runtime is not None:
            await materials_runtime.close()
