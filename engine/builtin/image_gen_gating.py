"""生图渠道门控（口径②：只认显式配置的 model）。

[2026-07-19] 目的：用户不一定配置 gpt/gemini 生图渠道。为彻底杜绝“未配置却被
误调用 / 调用后 404”的情况，这里提供统一的“渠道是否启用”判定，并被引擎在构建
工具列表 / 委派目标时使用，做到：
  - 未显式配置该渠道 model → 对应生图工具从模型可用工具列表里彻底移除；
  - 两个渠道都未配置 → draw.image_gen 不参与委派（orchestrator 看不到它）。

某渠道“可用”当且仅当**显式配置了该渠道的 model**：
  - config.yaml 的 system_models.<slot>.model 非空，或
  - 环境变量 CLONOTH_IMAGE_<X>_MODEL 非空。
不再因为回退到主渠道 OPENAI_* / GEMINI_* 就算可用，避免“假可用”。

与 tools/gpt_image_2.py、tools/gemini_image.py、plugins/image_gen_inject.py 的
判定口径保持一致，是唯一事实来源。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from clonoth_runtime import resolve_env_ref

# slot -> (config 块名, 环境变量 model 名, 工具名)
_SLOTS = {
    "gpt": ("image_gpt", "CLONOTH_IMAGE_GPT_MODEL", "gpt_image_2"),
    "gemini": ("image_gemini", "CLONOTH_IMAGE_GEMINI_MODEL", "gemini_image"),
}

# 受门控的生图工具名集合
IMAGE_GEN_TOOLS = frozenset(t for (_c, _e, t) in _SLOTS.values())
# 受门控的委派目标节点
IMAGE_GEN_NODE_ID = "draw.image_gen"


def _load_dotenv(workspace_root: Path) -> dict[str, str]:
    env_path = workspace_root / ".env"
    kv: dict[str, str] = {}
    if env_path.exists():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip().strip("'\"")
        except Exception:
            pass
    return kv


def _load_system_models(workspace_root: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception:
        return {}
    cfg_path = workspace_root / "data" / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    sm = data.get("system_models") if isinstance(data, dict) else None
    return sm if isinstance(sm, dict) else {}


def _resolve_ref(v: str, env: dict[str, str]) -> str:
    # 进程环境查不到再退到 .env：本函数只判断"配没配"，此时引擎可能还没加载 .env。
    return resolve_env_ref((v or "").strip(), env)


def _slot_model_configured(slot_cfg_name: str, env_model_key: str, workspace_root: Path) -> bool:
    """口径②：该渠道是否显式配置了 model。"""
    dotenv = _load_dotenv(workspace_root)
    sm = _load_system_models(workspace_root)
    blk = sm.get(slot_cfg_name) if isinstance(sm.get(slot_cfg_name), dict) else {}
    # config.yaml 显式 model（支持 ${VAR} 展开后仍非空）
    cfg_model = _resolve_ref(str((blk or {}).get("model") or ""), dotenv)
    if cfg_model.strip():
        return True
    # 环境变量 model
    env_model = (os.environ.get(env_model_key, "") or dotenv.get(env_model_key, "")).strip()
    return bool(env_model)


def image_channel_availability(workspace_root: Path | str) -> dict[str, bool]:
    """返回 {'gpt': bool, 'gemini': bool}，供插件与引擎共用。"""
    root = Path(workspace_root)
    result: dict[str, bool] = {}
    for key, (cfg_name, env_model_key, _tool) in _SLOTS.items():
        result[key] = _slot_model_configured(cfg_name, env_model_key, root)
    return result


def disabled_image_tools(workspace_root: Path | str) -> set[str]:
    """返回应从可用工具列表中移除的生图工具名集合（未显式配 model 的）。"""
    avail = image_channel_availability(workspace_root)
    disabled: set[str] = set()
    for key, (_cfg, _env, tool) in _SLOTS.items():
        if not avail.get(key):
            disabled.add(tool)
    return disabled


def any_image_channel_enabled(workspace_root: Path | str) -> bool:
    """任一生图渠道显式配置了 model 则为 True。"""
    return any(image_channel_availability(workspace_root).values())
