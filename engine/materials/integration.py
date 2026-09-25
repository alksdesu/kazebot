from __future__ import annotations

import logging
import hashlib
import json
from typing import Any

logger = logging.getLogger(__name__)

IMAGE_TOOLS = frozenset({"gpt_image_2", "gemini_image", "nai_generate", "nai_generate_from_plan"})
GENERATION_FIELDS = frozenset({"prompt", "negative_prompt", "seed", "width", "height", "size", "quality", "model", "sampler", "scheduler", "steps", "scale", "n"})


async def begin_image_generation(ctx: Any, name: str, arguments: dict) -> dict | None:
    if name not in IMAGE_TOOLS or not getattr(ctx, "task_id", ""):
        return None
    fingerprint = hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    previous = getattr(ctx, "_material_image_operation", None)
    call_id = getattr(ctx, "tool_call_id", "")
    if isinstance(previous, dict) and previous.get("tool_name") == name and previous.get("tool_call_id") == call_id and previous.get("fingerprint") == fingerprint:
        return previous
    response = await ctx.http.post(
        f"{ctx.supervisor_url.rstrip('/')}/v1/materials/image-operations",
        headers={"X-Clonoth-Task-Id": ctx.task_id},
        json={"tool_name": name, "tool_call_id": getattr(ctx, "tool_call_id", ""),
              "image_paths": arguments.get("image_paths") or [], "artifact_id": arguments.get("material_artifact_id"),
              "base_version": arguments.get("material_base_version"), "expected_version": arguments.get("material_expected_version"),
              "parameters": {key: arguments[key] for key in GENERATION_FIELDS if key in arguments},
              "instruction": str(arguments.get("prompt") or "")[:4000]}, timeout=20,
    )
    response.raise_for_status()
    operation = {**response.json(), "tool_name": name, "tool_call_id": call_id, "fingerprint": fingerprint}
    ctx._material_image_operation = operation
    return operation


async def persist_generated_images(ctx: Any, name: str, arguments: dict, result: Any, *, operation: dict | None = None) -> Any:
    if name not in IMAGE_TOOLS or not isinstance(result, dict) or result.get("ok") is False:
        return result
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    attachments = data.get("attachments") or result.get("attachments") or []
    images = [item for item in attachments if isinstance(item, dict) and item.get("type") == "image" and item.get("path")]
    if not images or not getattr(ctx, "task_id", ""):
        return result
    try:
        operation = operation or getattr(ctx, "_material_image_operation", None)
        if not operation:
            raise RuntimeError("No image operation was reserved before generation")
        response = await ctx.http.post(
            f"{ctx.supervisor_url.rstrip('/')}/v1/materials/image-operations/{operation['id']}/complete",
            headers={"X-Clonoth-Task-Id": ctx.task_id},
            json={"attachments": images, "result_metadata": {key: data[key] for key in GENERATION_FIELDS if key in data}},
            timeout=120,
        )
        response.raise_for_status()
        recorded = response.json()
        data["material_versions"] = recorded["versions"]
        data["materials_status"] = "archived"
        for image, version in zip(images, recorded["versions"]):
            image["artifact_version_id"] = version["id"]
            image["artifact_id"] = version["artifact_id"]
            image["material_source_id"] = version["metadata"]["source_id"]
    except Exception as exc:
        logger.warning("Generated image archive failed for task %s: %s", ctx.task_id, type(exc).__name__)
        data["materials_status"] = "archive_failed"
        data["materials_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        data["result"] = str(data.get("result") or "图片已实际生成。") + "\n版本存档失败；图片结果保留。不要再次付费生图，请重试存档。"
    result["data"] = data
    return result
