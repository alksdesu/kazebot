"""supervisor 拒绝请求时应答里给的原因：engine、工具和 QQ 适配器按同一种写法读。"""
from __future__ import annotations

import json
from typing import Any

# FastAPI 请求校验的 loc 第一段是参数所在位置，不是字段名
_PARAMETER_LOCATIONS = frozenset({"body", "query", "path", "header", "cookie"})
_LISTED_PROBLEMS = 5
_DETAIL_JSON_CHARS = 500


def refusal_reason(response: Any) -> str:
    """交给模型的原因：detail 读得出就用它，读不出就说 supervisor 回了哪个状态码。"""
    return refusal_detail(response) or f"supervisor 回了 HTTP {response.status_code}"


def refusal_detail(response: Any) -> str:
    """应答 detail 里写的原因；没有 detail、应答体不是 JSON 或读不出时给空串。"""
    try:
        body = response.json()
    except Exception:
        # httpx 读不出 JSON 抛 ValueError，测试替身和别的客户端抛什么都有
        return ""
    detail = body.get("detail") if isinstance(body, dict) else None
    return _detail_text(detail, getattr(response, "status_code", None))


def error_detail(error: BaseException) -> str:
    """异常带着的 supervisor 应答里写的原因；不是被 supervisor 拒绝引起的，或应答里读不出原因时给空串。"""
    response = getattr(error, "response", None)
    return refusal_detail(response) if response is not None else ""


def _detail_text(detail: Any, status: Any) -> str:
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
