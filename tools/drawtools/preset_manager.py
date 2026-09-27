"""Drawtools preset management utility for QQ command routing."""
from __future__ import annotations

from typing import Any

# The QQ adapter imports this as tools.drawtools.preset_manager; scripts and tests load it from its own directory.
if __package__:
    from .common import (
        PresetNotFoundError,
        is_selected_preset,
        iter_presets,
        load_settings,
        set_selected_preset,
    )
else:
    from common import (
        PresetNotFoundError,
        is_selected_preset,
        iter_presets,
        load_settings,
        set_selected_preset,
    )

__all__ = ["PresetNotFoundError", "list_presets", "switch_preset"]


def list_presets() -> list[dict[str, Any]]:
    settings = load_settings()
    result = []
    for preset in iter_presets(settings):
        item = {
            "id": str(preset.get("id") or ""),
            "name": str(preset.get("name") or ""),
            "selected": is_selected_preset(settings, preset),
            "model": str((preset.get("params") or {}).get("model") or ""),
            "scale": (preset.get("params") or {}).get("scale"),
            "steps": (preset.get("params") or {}).get("steps"),
        }
        result.append(item)
    return result


def switch_preset(preset_ref: str) -> dict[str, Any]:
    return set_selected_preset(preset_ref)
