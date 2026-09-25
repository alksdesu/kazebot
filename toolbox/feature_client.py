from __future__ import annotations

from typing import Any

from .context import ToolContext
from clonoth_sdk.feature_paths import validate_feature_path


async def request(
    ctx: ToolContext, method: str, path: str,
    body: dict[str, Any] | None = None, params: dict[str, Any] | None = None,
) -> Any:
    validate_feature_path(path)
    if not ctx.task_id:
        raise ValueError("feature tools require a task identity")
    response = await ctx.http.request(
        method, f"{ctx.supervisor_url.rstrip('/')}{path}", json=body, params=params,
        headers={"X-Clonoth-Task-Id": ctx.task_id},
    )
    response.raise_for_status()
    return response.json()
