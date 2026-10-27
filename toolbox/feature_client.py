from __future__ import annotations

import functools
import json
from typing import Any, Awaitable, Callable

import httpx

from .context import ToolContext
from clonoth_sdk.feature_paths import validate_feature_path

ToolFunc = Callable[[dict[str, Any], Any], Awaitable[dict[str, Any]]]

# FastAPI 请求校验的 loc 第一段是参数所在位置，不是字段名
_PARAMETER_LOCATIONS = frozenset({"body", "query", "path", "header", "cookie"})
_LISTED_PROBLEMS = 5
_DETAIL_JSON_CHARS = 500


class FeatureRequestError(httpx.HTTPStatusError):
    """supervisor 拒绝了这次请求；str() 就是交给模型的原因。"""


def refusal_reason(response: Any) -> str:
    status = response.status_code
    try:
        body = response.json()
    except ValueError:
        body = None
    detail = body.get("detail") if isinstance(body, dict) else None
    return _detail_text(detail, status) or f"supervisor 回了 HTTP {status}"


def _detail_text(detail: Any, status: int) -> str:
    if isinstance(detail, str):
        return detail.strip()
    if not detail:
        return ""
    if isinstance(detail, dict):
        message = detail.get("message")
        if isinstance(message, str) and message.strip():
            code = str(detail.get("code") or "").strip()
            return f"{message.strip()}（{code}，HTTP {status}）" if code else f"{message.strip()}（HTTP {status}）"
    elif isinstance(detail, list):
        problems = [problem for problem in map(_validation_problem, detail) if problem]
        if not problems:
            return ""
        listed = "；".join(problems[:_LISTED_PROBLEMS])
        if len(problems) > _LISTED_PROBLEMS:
            listed += f"；另有 {len(problems) - _LISTED_PROBLEMS} 处"
        return f"{listed}（HTTP {status}）"
    return f"{_clipped_json(detail)}（HTTP {status}）"


def _validation_problem(item: Any) -> str:
    if not isinstance(item, dict):
        return item.strip() if isinstance(item, str) else _clipped_json(item)
    loc = item.get("loc")
    fields = [str(part) for part in loc] if isinstance(loc, (list, tuple)) else []
    if fields and fields[0] in _PARAMETER_LOCATIONS:
        fields = fields[1:]
    message = str(item.get("msg") or item.get("type") or "").strip() or _clipped_json(item)
    return f"{'.'.join(fields)}：{message}" if fields else message


def _clipped_json(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= _DETAIL_JSON_CHARS else f"{text[:_DETAIL_JSON_CHARS]}…"


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
