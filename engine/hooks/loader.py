from __future__ import annotations

"""Legacy hook registration backed by the workspace plugin runtime."""

import logging
from pathlib import Path
from types import ModuleType

from .registry import HookRegistry

logger = logging.getLogger(__name__)


def _default_plugin_meta(py_file: Path) -> dict:
    """Build default metadata for a plugin file."""
    # Why: existing plugins predate PLUGIN_META and must keep loading unchanged.
    # How: derive a stable name from the file stem and fill every documented
    # metadata field. Purpose: make list_plugins() complete for legacy plugins.
    return {
        "name": py_file.stem,
        "version": "unknown",
        "description": "",
        "author": "",
        "hooks": [],
    }


def _normalize_plugin_meta(py_file: Path, raw_meta: object) -> dict:
    """Merge optional PLUGIN_META fields with loader-owned defaults."""
    # Why: plugin authors may omit PLUGIN_META or only provide some fields.
    # How: copy defaults first, then overlay a declared dict and repair required
    # display fields when they are empty. Purpose: keep name and version present
    # while preserving any extra metadata keys a plugin chooses to publish.
    meta = _default_plugin_meta(py_file)
    if isinstance(raw_meta, dict):
        meta.update(raw_meta)
    elif raw_meta is not None:
        logger.warning("Plugin %s has non-dict PLUGIN_META, using defaults", py_file.name)
    if not meta.get("name"):
        meta["name"] = py_file.stem
    if not meta.get("version"):
        meta["version"] = "unknown"
    if meta.get("description") is None:
        meta["description"] = ""
    if meta.get("author") is None:
        meta["author"] = ""
    if meta.get("hooks") is None:
        meta["hooks"] = []
    return meta


def register_plugin_module(hook_registry: HookRegistry, module: ModuleType, py_file: Path) -> None:
    raw_meta = getattr(module, "PLUGIN_META", None)
    meta = _normalize_plugin_meta(py_file, raw_meta)
    if isinstance(raw_meta, dict) and raw_meta.get("handler_class") and raw_meta.get("hook_points"):
        instance = getattr(module, str(raw_meta["handler_class"]).strip())()
        if "post_work_idempotent" in raw_meta:
            setattr(instance, "post_work_idempotent", bool(raw_meta["post_work_idempotent"]))
        priority = raw_meta.get("priority", getattr(instance, "priority", None))
        for item in raw_meta["hook_points"]:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise ValueError("Invalid plugin hook point")
            point, method = (str(part).strip() for part in item)
            hook_registry.register(point, getattr(instance, method), priority=priority)
    else:
        register = getattr(module, "register", None)
        if not callable(register):
            raise TypeError("Plugin must declare setup, register, or handler_class/hook_points")
        register(hook_registry)
    hook_registry.register_plugin_meta(meta)


def load_external_plugins(hook_registry: HookRegistry, plugins_dir: Path) -> int:
    from plugin_runtime import get_manager

    manager = get_manager(Path(plugins_dir).parent)
    manager.refresh()
    hook_registry.bind_plugins(manager)
    return len(manager._versions)
