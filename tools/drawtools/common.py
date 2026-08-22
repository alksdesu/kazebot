from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import yaml

DRAWTOOLS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = DRAWTOOLS_DIR.parents[1]
if str(_REPO_ROOT) not in sys.path:  # 工具以子进程跑，sys.path 里只有自己那一级
    sys.path.insert(0, str(_REPO_ROOT))
from workspace import resolve_workspace_root  # noqa: E402

WORKSPACE_ROOT = resolve_workspace_root(_REPO_ROOT)
SETTINGS_PATH = DRAWTOOLS_DIR / "settings.yaml"
SETTINGS_EXAMPLE_PATH = DRAWTOOLS_DIR / "settings.example.yaml"
CHARACTER_TAGS_PATH = DRAWTOOLS_DIR / "character_tags.yaml"
CHARACTER_TAGS_EXAMPLE_PATH = DRAWTOOLS_DIR / "character_tags.example.yaml"

DEFAULT_SETTINGS: dict[str, Any] = {
    "api": {
        "base_url": "https://image.novelai.net",
        "api_key_env": "NOVELAI_API_KEY",
        "api_key": "",
        "timeout_sec": 130,
    },
    "generation": {
        "request_delay_sec": 0,
        "retry_wait_sec": 3,
        "retry_max_attempts": 5,
        "lock_timeout_sec": 3600,
    },
    "storage": {
        "cleanup_enabled": True,
        "retention_days": 7,
        "max_total_mb": 2048,
        "cleanup_interval_sec": 3600,
    },
    "params": {
        "selected_preset_id": "default-v45-full",
        "size_options": {
            "横图": {"width": 1216, "height": 832},
            "竖图": {"width": 832, "height": 1216},
            "方图": {"width": 1024, "height": 1024},
        },
        "presets": [],
    },
    "character_tags": {
        "path": "tools/drawtools/character_tags.yaml",
        "auto_search_unknown": True,
    },
}

DEFAULT_PRESET: dict[str, Any] = {
    "id": "default-v45-full",
    "name": "默认 (V4.5 Full)",
    "positive_prefix": "best quality, amazing quality, very aesthetic, absurdres",
    "negative_prefix": "lowres, bad anatomy, bad hands, missing fingers, extra digits, fewer digits, cropped, worst quality, low quality, normal quality, jpeg artifacts, signature, watermark, username, blurry",
    "max_images": 4,
    "max_characters_per_image": 4,
    "params": {
        "model": "nai-diffusion-4-5-full",
        "sampler": "k_euler_ancestral",
        "scheduler": "karras",
        "steps": 28,
        "scale": 6,
        "seed": -1,
        "qualityToggle": True,
        "autoSmea": False,
        "ucPreset": 0,
        "cfg_rescale": 0,
        "variety_boost": False,
        "sm": False,
        "sm_dyn": False,
        "decrisper": False,
    },
}


def deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = deep_merge(merged.get(key), value)
        return merged
    if override is None:
        return base
    return override


def load_yaml_file(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return default if data is None else data
    except Exception:
        return default


def load_settings() -> dict[str, Any]:
    raw = load_yaml_file(SETTINGS_PATH if SETTINGS_PATH.exists() else SETTINGS_EXAMPLE_PATH, {})
    settings = deep_merge(DEFAULT_SETTINGS, raw if isinstance(raw, dict) else {})
    presets = settings.setdefault("params", {}).setdefault("presets", [])
    if not isinstance(presets, list) or not presets:
        settings["params"]["presets"] = [dict(DEFAULT_PRESET)]
    return settings


def iter_presets(settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    settings = settings or load_settings()
    params_cfg = settings.get("params") if isinstance(settings.get("params"), dict) else {}
    presets = params_cfg.get("presets") if isinstance(params_cfg.get("presets"), list) else []
    return [deep_merge(DEFAULT_PRESET, p) for p in presets if isinstance(p, dict)] or [dict(DEFAULT_PRESET)]


class PresetNotFoundError(ValueError):
    # 继承 ValueError 让既有 except ValueError 的调用方不用改。
    def __init__(self, preset_ref: str, available: list[str]) -> None:
        self.preset_ref = str(preset_ref or "")
        self.available = list(available)
        super().__init__(f"没找到画师串预设“{self.preset_ref}”")


def normalized_preset_tokens(preset: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for value in (preset.get("id"), preset.get("name")):
        token = str(value or "").strip().lower()
        if token:
            tokens.add(token)
    aliases = preset.get("aliases") if isinstance(preset.get("aliases"), list) else []
    for alias in aliases:
        token = str(alias).strip().lower()
        if token:
            tokens.add(token)
    return tokens


def find_preset(settings: dict[str, Any] | None = None, preset_ref: str = "") -> dict[str, Any] | None:
    ref = str(preset_ref or "").strip().lower()
    if not ref:
        return None
    for preset in iter_presets(settings):
        if ref in normalized_preset_tokens(preset):
            return preset
    return None


def default_preset_id(settings: dict[str, Any] | None = None) -> str:
    settings = settings or load_settings()
    params_cfg = settings.get("params") if isinstance(settings.get("params"), dict) else {}
    return str(params_cfg.get("selected_preset_id") or "").strip()


def is_selected_preset(settings: dict[str, Any] | None, preset: dict[str, Any]) -> bool:
    return default_preset_id(settings).lower() == str(preset.get("id") or "").strip().lower()


def preset_choice_labels(settings: dict[str, Any] | None = None) -> list[str]:
    labels: list[str] = []
    for preset in iter_presets(settings):
        name = str(preset.get("name") or "").strip()
        pid = str(preset.get("id") or "").strip()
        labels.append(f"{name}（id: {pid}）")
    return labels


def require_preset(settings: dict[str, Any] | None, preset_ref: str) -> dict[str, Any]:
    preset = find_preset(settings, preset_ref)
    if preset is not None:
        return preset
    raise PresetNotFoundError(preset_ref, preset_choice_labels(settings))


def selected_preset(settings: dict[str, Any] | None = None, preset_ref: str = "") -> dict[str, Any]:
    settings = settings or load_settings()
    return (
        find_preset(settings, preset_ref)
        or find_preset(settings, default_preset_id(settings))
        or iter_presets(settings)[0]
    )


def set_selected_preset(preset_ref: str) -> dict[str, Any]:
    settings = load_settings()
    target = require_preset(settings, preset_ref)
    target_id = str(target.get("id") or "").strip()
    if not target_id:
        raise ValueError("preset has no id")
    settings.setdefault("params", {})["selected_preset_id"] = target_id
    # 用户运行期选择写入 settings.yaml；仓库只应维护 settings.example.yaml，避免升级覆盖用户配置。
    SETTINGS_PATH.write_text(yaml.safe_dump(settings, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return target


def resolve_api_key(settings: dict[str, Any]) -> str:
    # settings.yaml 优先：用户在 settings.yaml 里显式写了 api_key 就以它为准，
    # 仅当为空时才回退到环境变量（api_key_env 指定的变量名，默认 NOVELAI_API_KEY）。
    # 避免部署环境里遗留的同名环境变量静默覆盖用户配置。
    api_cfg = settings.get("api") if isinstance(settings.get("api"), dict) else {}
    key = str(api_cfg.get("api_key") or "").strip()
    if key:
        return key
    env_name = str(api_cfg.get("api_key_env") or "NOVELAI_API_KEY").strip()
    if env_name:
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    return ""


def resolve_base_url(settings: dict[str, Any]) -> str:
    api_cfg = settings.get("api") if isinstance(settings.get("api"), dict) else {}
    return str(api_cfg.get("base_url") or "https://image.novelai.net").strip().rstrip("/")


def size_to_dimensions(size_label: str, settings: dict[str, Any] | None = None) -> tuple[int, int, str]:
    settings = settings or load_settings()
    label = str(size_label or "").strip()
    if label not in {"横图", "竖图", "方图"}:
        label = "竖图"
    params_cfg = settings.get("params") if isinstance(settings.get("params"), dict) else {}
    size_options = params_cfg.get("size_options") if isinstance(params_cfg.get("size_options"), dict) else {}
    opt = size_options.get(label) if isinstance(size_options.get(label), dict) else {}
    width = int(opt.get("width") or (1216 if label == "横图" else 832 if label == "竖图" else 1024))
    height = int(opt.get("height") or (832 if label == "横图" else 1216 if label == "竖图" else 1024))
    return width, height, label


def load_character_tags() -> list[dict[str, Any]]:
    data = load_yaml_file(CHARACTER_TAGS_PATH if CHARACTER_TAGS_PATH.exists() else CHARACTER_TAGS_EXAMPLE_PATH, {})
    chars = data.get("characters") if isinstance(data, dict) else []
    return [c for c in chars if isinstance(c, dict)] if isinstance(chars, list) else []


def comma_join(*parts: Any) -> str:
    out: list[str] = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, list):
            text = ", ".join(str(x).strip() for x in part if str(x).strip())
        else:
            text = str(part).strip()
        text = text.replace("，", ",").strip(" ,")
        if text:
            out.append(text)
    return ", ".join(out)
