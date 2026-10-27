from __future__ import annotations

import functools
from typing import Any, Awaitable, Callable

import httpx

from .context import ToolContext
from clonoth_sdk.feature_paths import validate_feature_path
from clonoth_sdk.refusals import refusal_reason

ToolFunc = Callable[[dict[str, Any], Any], Awaitable[dict[str, Any]]]


class FeatureRequestError(httpx.HTTPStatusError):
    """supervisor 拒绝了这次请求；str() 就是交给模型的原因。"""


async def request(
    ctx: ToolContext, method: str, path: str,
    body: dict[str, Any] | None = None, params: dict[str, Any] | None = None,
    *, timeout: Any = httpx.USE_CLIENT_DEFAULT,
) -> Any:
    validate_feature_path(path)
    if not ctx.task_id:
        raise ValueError("feature tools require a task identity")
    response = await ctx.http.request(
        method, f"{ctx.supervisor_url.rstrip('/')}{path}", json=body, params=params,
        headers={"X-Clonoth-Task-Id": ctx.task_id}, timeout=timeout,
    )
    if not response.is_success:
        raise FeatureRequestError(refusal_reason(response), request=response.request, response=response)
    return response.json()


def feature_tool(func: ToolFunc) -> ToolFunc:
    # 被拒是模型能照着改的结果，不该冒到 registry 变成带堆栈的通用失败
    @functools.wraps(func)
    async def run(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        try:
            return await func(args, ctx)
        except FeatureRequestError as exc:
            reason = str(exc)
            return {"ok": False, "error": reason, "data": {"result": f"ERROR: {reason}"}}

    return run
