"""全局功能设置 config/features.yaml 的原文读写、生效状态和控制台 schema。

全局那层不读环境变量，supervisor 自己算出的值就是本实例 bot 落到全局时用的那一份；只认全局的键全局没写时，
平台用的是自己的老位置或 env 兜底，另行列出。bot 换没换上当前这份文件，看它公布的 defaults 指纹。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from platforms.descriptor import PlatformDescriptor
from platforms.live import live_status, read_live_state, same_file
from platforms.registry import current_platform, features_module, get_platform

from . import config_files
from .admin_api import verify_admin_token
from .config_files import RawConfig
from .platform_api import config_file_name, published_states


def features_file(workspace_root: Path) -> Path:
    return features_module().features_config_path(workspace_root)


def read_features_raw(workspace_root: Path) -> dict[str, Any]:
    return config_files.read_raw(features_file(workspace_root))


def write_features_raw(workspace_root: Path, content: str) -> dict[str, Any]:
    path = features_file(workspace_root)
    document = config_files.parse_mapping(content)
    unknown = config_files.unknown_paths(document, (key.path for key in features_module().FEATURE_KEYS))
    if unknown:
        raise HTTPException(status_code=400, detail=_unknown_detail(workspace_root, unknown))
    config_files.write_raw(path, content)
    return {"ok": True, "warnings": []}


def _unknown_detail(workspace_root: Path, unknown: list[str]) -> str:
    """拒收的路径里是本实例平台专属项的，指明该写进平台的配置文件。"""
    owners: dict[str, list[str]] = {}
    platform = current_platform()
    published = read_live_state(platform, workspace_root).get("paths")
    if isinstance(published, dict):
        for path in sorted(set(unknown) & set(published.values())):
            owners.setdefault(path, []).append(platform.display_name)
    parts: list[str] = []
    stray = [path for path in unknown if path not in owners]
    if stray:
        parts.append(f"无人消费的配置项：{stray}")
    if owners:
        placed = "，".join(f"{path}（{'、'.join(names)}）" for path, names in owners.items())
        parts.append(f"只能写在平台配置里：{placed}")
    return "；".join(parts)


def platform_adoption(
    workspace_root: Path, platform: PlatformDescriptor, path: Path, current: dict[str, Any],
) -> dict[str, Any]:
    """这个平台的 bot 是否已经换上当前这份全局文件。"""
    entry: dict[str, Any] = {"id": platform.id, "display_name": platform.display_name}
    state = read_live_state(platform, workspace_root)
    if not state:
        return {**entry, "published": False, "applied": False, "reason": "bot 进程尚未公布生效配置"}
    status = live_status(state)
    entry.update(published=True, age_sec=status.age_sec, stale=status.stale)
    defaults = state.get("defaults")
    if not isinstance(defaults, dict):
        return {**entry, "applied": False, "reason": "这个 bot 的版本还不读全局文件，更新并重启之后才会生效"}
    seen = defaults.get("config_path")
    if not same_file(seen, path):
        return {**entry, "applied": False, "reason": f"bot 读的是另一份全局文件：{seen}"}
    # 和平台那边一样比 loaded：文件写坏时 bot 看到的指纹等于当前文件，用的却还是上一份。
    return {**entry, "applied": defaults.get("loaded") == current}


def _fallback_payload(workspace_root: Path, fallback: Any) -> dict[str, Any]:
    platform = get_platform(fallback.platform)
    return {
        "platform": platform.id,
        "display_name": platform.display_name,
        "source": fallback.source,
        # 老位置在哪个文件；env 兜底给空串。
        "file": "" if fallback.from_env else config_file_name(workspace_root, platform),
        "value": fallback.value,
    }


def _central_payload(switch: Any) -> dict[str, Any]:
    return {"enabled": switch.enabled, "set_globally": switch.set_globally}


def features_state(workspace_root: Path, config: Any) -> dict[str, Any]:
    path = features_file(workspace_root)
    state = config.state_payload()
    current = state["file"]
    platforms = [platform_adoption(workspace_root, current_platform(), path, current)]
    running = [entry for entry in platforms if entry["published"] and not entry["stale"]]
    features = features_module()
    published = published_states(workspace_root)
    return {
        # 生效值由 supervisor 当场算，不存在「还没公布」；外形和平台的 state 端点一致，控制台共用一套草稿逻辑。
        "published": True,
        # 文件本身要解析得了，bot 在跑的话也得换上它；它没在跑时没有要等的。
        "applied": state["loaded"] == current and all(entry["applied"] for entry in running),
        "file": current,
        "state": state,
        "platforms": platforms,
        # 全局没写的只认全局的键：页面上显示的是默认值，平台实际用着自己的兜底。
        "fallbacks": {
            name: [_fallback_payload(workspace_root, fallback) for fallback in found]
            for name, found in features.global_fallbacks(config, published).items()
        },
        # 统一跑的功能实际开不开：全局没写时按 bot 眼下用着的兜底算。
        "central": {
            name: _central_payload(features.central_switch(name, config, published))
            for name in sorted(features.CENTRAL_SWITCHES)
        },
    }


def create_feature_config_router(workspace_root: Path, config: Any) -> APIRouter:
    """config 是全局那层的实例，由 create_app 建好传进来。"""
    router = APIRouter(dependencies=[Depends(verify_admin_token)])

    @router.get("/config/raw")
    def get_raw() -> dict[str, Any]:
        return read_features_raw(workspace_root)

    @router.put("/config/raw")
    def put_raw(payload: RawConfig) -> dict[str, Any]:
        result = write_features_raw(workspace_root, payload.content)
        # 同一个 mtime 刻度里写进等长内容时指纹不变，缓存得手动作废。
        config.invalidate()
        return result

    @router.get("/config/state")
    def get_state() -> dict[str, Any]:
        return features_state(workspace_root, config)

    @router.get("/config/schema")
    def get_schema() -> dict[str, Any]:
        return features_module().schema()

    return router
