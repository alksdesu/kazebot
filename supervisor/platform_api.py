"""平台清单与各平台的热载配置、控制台 schema、试听端点。

supervisor 只管文件；「实际生效值」由各平台的 bot 进程写进 live 状态文件公布。
supervisor 不自己算生效值 —— bot 是独立进程，env 可能和这里不一样，算出来的会是另一份配置。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping

from fastapi import APIRouter, Body, Depends, HTTPException

from platforms.descriptor import HOOK_CONSOLE_SCHEMA, HOOK_TRIGGER_POLICY, STATE_LIVE, PlatformDescriptor
from platforms.registry import (
    all_platforms,
    features_module,
    get_platform,
    platform_config_path,
    platform_hook,
    platform_state_path,
)

from . import config_files, platform_trigger
from .admin_api import verify_admin_token
from .config_files import RawConfig

# bot 至少每 30 秒重写一次 live 状态，留三倍余量再判定失联。
_STALE_AFTER_SEC = 90.0
_DRY_RUN_LIMIT = 200


def resolve_platform(platform_id: str) -> PlatformDescriptor:
    try:
        return get_platform(platform_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"未知平台：{platform_id}") from None


def config_file(workspace_root: Path, platform: PlatformDescriptor) -> Path:
    path = platform_config_path(platform, workspace_root)
    if path is None:
        raise HTTPException(status_code=404, detail=f"{platform.display_name} 没有热载配置")
    return path


def live_state(workspace_root: Path, platform: PlatformDescriptor) -> dict[str, Any]:
    try:
        data = json.loads(platform_state_path(platform, STATE_LIVE, workspace_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def published_states(workspace_root: Path) -> dict[str, dict[str, Any]]:
    """各平台 bot 最近一次公布的生效快照，按平台 id。失联的也算：写在配置和 env 里的设置不随 bot 停下而消失。"""
    return {
        platform.id: state
        for platform in all_platforms()
        if (state := live_state(workspace_root, platform))
    }


def publication_age(state: Mapping[str, Any]) -> tuple[float, bool]:
    """live 状态公布了多久，以及是否久到该当 bot 失联。"""
    age_sec = max(0.0, time.time() - float(state.get("published_at") or 0.0))
    return round(age_sec, 3), age_sec > _STALE_AFTER_SEC


def read_config_raw(workspace_root: Path, platform: PlatformDescriptor) -> dict[str, Any]:
    return config_files.read_raw(config_file(workspace_root, platform))


def _current_document(path: Path) -> dict[str, Any]:
    try:
        return config_files.parse_mapping(config_files.read_raw(path)["content"])
    except (HTTPException, OSError, UnicodeDecodeError):
        # 读不了或写坏了就当它是空的：新内容里的每一处都算新写。
        return {}


def _global_only_writes(path: Path, document: dict[str, Any]) -> list[str]:
    """新写或改了值的只认全局的键。升级前就有、原样留着的老写法照收：全局没写时 bot 还靠它兜底。"""
    current = _current_document(path)
    return sorted(
        key_path
        for key_path in features_module().GLOBAL_ONLY_PATHS
        if (value := config_files.value_at(document, key_path)) is not None
        and value != config_files.value_at(current, key_path)
    )


def _shown_path(path: Path, workspace_root: Path) -> str:
    try:
        return path.relative_to(workspace_root).as_posix()
    except ValueError:
        return str(path)


def write_config_raw(workspace_root: Path, platform: PlatformDescriptor, content: str) -> dict[str, Any]:
    path = config_file(workspace_root, platform)
    document = config_files.parse_mapping(content)
    misplaced = _global_only_writes(path, document)
    if misplaced:
        features = _shown_path(features_module().features_config_path(workspace_root), workspace_root)
        raise HTTPException(status_code=400, detail=f"只能写在全局设置 {features} 里：{misplaced}")
    warnings: list[str] = []
    known = live_state(workspace_root, platform).get("paths")
    if isinstance(known, dict) and known:
        unknown = config_files.unknown_paths(document, known.values())
        if unknown:
            raise HTTPException(status_code=400, detail=f"无人消费的配置项：{unknown}")
    else:
        warnings.append("bot 进程未公布配置结构，本次只校验了 YAML 语法")
    config_files.write_raw(path, content)
    return {"ok": True, "warnings": warnings}


def config_state(workspace_root: Path, platform: PlatformDescriptor) -> dict[str, Any]:
    state = live_state(workspace_root, platform)
    fingerprint = config_files.fingerprint(config_file(workspace_root, platform))
    if not state:
        return {
            "published": False,
            "reason": "bot 进程尚未公布生效配置（未启动，或还没跑完第一轮对齐）",
            "file": fingerprint,
        }
    age_sec, stale = publication_age(state)
    return {
        "published": True,
        "age_sec": age_sec,
        "stale": stale,
        # 「已生效」的判据是 bot 跑着的那份值确实来自当前这个文件版本。
        # 不能拿 state["file"] 比 —— 那是 bot 看到的文件指纹，文件写坏时它照样等于
        # 当前文件，而 bot 用的还是上一份配置。
        "applied": state.get("loaded") == fingerprint,
        "file": fingerprint,
        "state": state,
    }


def config_schema(platform: PlatformDescriptor) -> dict[str, Any]:
    """控制台的分区布局和控件说明，取自平台代码里的声明，bot 没起来也给得出。"""
    try:
        layout = platform_hook(platform, HOOK_CONSOLE_SCHEMA)
    except LookupError:
        raise HTTPException(status_code=404, detail=f"{platform.display_name} 没有控制台配置界面") from None
    return layout.schema()


def trigger_dry_run(workspace_root: Path, platform: PlatformDescriptor, payload: dict[str, Any]) -> dict[str, Any]:
    """把一批模拟消息过一遍真实判定。

    config 省略时用 bot 公布的生效值 —— 「试听当前配置」不该要求调用方自己拼一份，
    拼出来的那份和 bot 跑着的不一样就等于试听骗人。
    """
    if HOOK_TRIGGER_POLICY not in platform.hooks:
        raise HTTPException(status_code=404, detail=f"{platform.display_name} 没有触发判定")
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="messages 必须是数组")
    if len(messages) > _DRY_RUN_LIMIT:
        raise HTTPException(status_code=400, detail=f"一次最多 {_DRY_RUN_LIMIT} 条")

    values = payload.get("config")
    if not isinstance(values, dict):
        values = live_state(workspace_root, platform).get("values")
    if not isinstance(values, dict) or not values:
        raise HTTPException(status_code=409, detail="bot 进程尚未公布生效配置，请显式传 config 后再试听")
    try:
        return platform_trigger.dry_run(
            platform, messages, values,
            apply_cooldown=payload.get("cooldown", True) is not False,
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        # 缺键/类型不对最可能来自调用方自拼的 config，这是 400 不是 500。
        raise HTTPException(status_code=400, detail=f"配置不完整或类型不对：{exc}") from exc


def create_platform_router(workspace_root: Path) -> APIRouter:
    router = APIRouter(dependencies=[Depends(verify_admin_token)])

    @router.get("")
    def list_platforms() -> dict[str, Any]:
        return {"platforms": [platform.public_dict() for platform in all_platforms()]}

    @router.get("/{platform_id}")
    def get_one(platform_id: str) -> dict[str, Any]:
        return resolve_platform(platform_id).public_dict()

    @router.get("/{platform_id}/config/raw")
    def get_raw(platform_id: str) -> dict[str, Any]:
        return read_config_raw(workspace_root, resolve_platform(platform_id))

    @router.put("/{platform_id}/config/raw")
    def put_raw(platform_id: str, payload: RawConfig) -> dict[str, Any]:
        return write_config_raw(workspace_root, resolve_platform(platform_id), payload.content)

    @router.get("/{platform_id}/config/state")
    def get_state(platform_id: str) -> dict[str, Any]:
        return config_state(workspace_root, resolve_platform(platform_id))

    @router.get("/{platform_id}/config/schema")
    def get_schema(platform_id: str) -> dict[str, Any]:
        return config_schema(resolve_platform(platform_id))

    @router.post("/{platform_id}/trigger/dry-run")
    def dry_run(platform_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return trigger_dry_run(workspace_root, resolve_platform(platform_id), payload)

    return router
