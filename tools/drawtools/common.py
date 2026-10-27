from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from typing import Any

import yaml

DRAWTOOLS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = DRAWTOOLS_DIR.parents[1]
if str(_REPO_ROOT) not in sys.path:  # 工具以子进程跑，sys.path 里只有自己那一级
    sys.path.insert(0, str(_REPO_ROOT))
import config_layout  # noqa: E402
from tools._channel import YamlDocument, dotenv_values, read_yaml_document  # noqa: E402
from workspace import resolve_workspace_root  # noqa: E402

WORKSPACE_ROOT = resolve_workspace_root(_REPO_ROOT)
# 运行时配置归实例，放在它的 config/drawtools/；仓库这边只发同名的 .example 模板。
SETTINGS_PATH = config_layout.drawtools_file(WORKSPACE_ROOT, "settings.yaml")
SETTINGS_EXAMPLE_PATH = config_layout.drawtools_example("settings.yaml")
CHARACTER_TAGS_PATH = config_layout.drawtools_file(WORKSPACE_ROOT, "character_tags.yaml")
CHARACTER_TAGS_EXAMPLE_PATH = config_layout.drawtools_example("character_tags.yaml")

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
        "path": f"{config_layout.DRAWTOOLS_DIR}/character_tags.yaml",
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


class DrawtoolsConfigUnreadable(ValueError):
    """config/drawtools 下的配置读不了，又没有上一份可沿用。消息只带位置和问题，不带原文。"""


def _config_path(own: Path, example: Path) -> Path:
    return own if own.exists() else example


def _read_config_document(own: Path, example: Path) -> YamlDocument:
    """实例那份，没有就读仓库的模板。常驻进程里写坏了沿用上一次读成的那份。"""
    path = _config_path(own, example)
    return read_yaml_document(path, label=config_layout.logical_path(WORKSPACE_ROOT, path))


def _settings_document() -> YamlDocument:
    """settings.yaml。params 不是映射也算读不了：出图预设补不进去。"""
    document = _read_config_document(SETTINGS_PATH, SETTINGS_EXAMPLE_PATH)
    params = document.data.get("params")
    if params is None or isinstance(params, dict):
        return document
    label = config_layout.logical_path(WORKSPACE_ROOT, _config_path(SETTINGS_PATH, SETTINGS_EXAMPLE_PATH))
    return YamlDocument({}, problem=f"{label} 读不了：params 不是映射（key: value）")


def _with_defaults(raw: dict[str, Any]) -> dict[str, Any]:
    # 默认值整份复制一遍：deep_merge 会把没写的那几段原样挂上来，调用方一改就改到默认值本身
    settings = deep_merge(copy.deepcopy(DEFAULT_SETTINGS), raw)
    presets = settings.setdefault("params", {}).setdefault("presets", [])
    if not isinstance(presets, list) or not presets:
        settings["params"]["presets"] = [copy.deepcopy(DEFAULT_PRESET)]
    return settings


def load_settings() -> dict[str, Any]:
    """settings.yaml 补上默认值。读不了又没有上一份可沿用时抛 DrawtoolsConfigUnreadable，不拿默认值顶上。"""
    document = _settings_document()
    if document.problem:
        raise DrawtoolsConfigUnreadable(document.problem)
    return _with_defaults(document.data)


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
    # 读改写全程持锁：控制台和 QQ 命令同时改，后写的会把先写的那笔整份盖掉。
    with config_layout.locked(SETTINGS_PATH):
        document = _settings_document()
        unreadable = document.problem or document.stale
        if unreadable:
            # 整份写回去的是默认值或上一份，手写到一半的那份就被盖掉了
            raise DrawtoolsConfigUnreadable(f"{unreadable}。先在控制台的原文编辑器里修好再切换")
        settings = _with_defaults(document.data)
        target = require_preset(settings, preset_ref)
        target_id = str(target.get("id") or "").strip()
        if not target_id:
            raise ValueError("preset has no id")
        settings.setdefault("params", {})["selected_preset_id"] = target_id
        # 用户运行期选择写入实例的 settings.yaml；仓库只维护 settings.example.yaml，升级不会覆盖用户配置。
        config_layout.write_locked(
            SETTINGS_PATH,
            yaml.safe_dump(settings, allow_unicode=True, sort_keys=False),
            mode=config_layout.private_mode_for(WORKSPACE_ROOT, SETTINGS_PATH),
        )
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
        # 工具子进程的环境里 *_API_KEY 被剥掉了，本实例 .env 和共享密钥文件得自己读；共享组里别的实例配上的这边也用得上。
        return dotenv_values(WORKSPACE_ROOT).get(env_name, "")
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
    """角色库里的角色。读不了又没有上一份可沿用时抛 DrawtoolsConfigUnreadable。"""
    document = _read_config_document(CHARACTER_TAGS_PATH, CHARACTER_TAGS_EXAMPLE_PATH)
    if document.problem:
        raise DrawtoolsConfigUnreadable(document.problem)
    chars = document.data.get("characters")
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
