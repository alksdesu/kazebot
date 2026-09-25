from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from clonoth_sdk.command_text import parse_duration_seconds

COMMANDS = frozenset({"提醒", "提醒列表", "提醒完成", "提醒延后", "提醒取消"})
CONTROL_COMMANDS = frozenset({"提醒列表", "提醒取消"})


def _reply_action(text: str) -> dict | None:
    text = text.strip().rstrip("。！!~～ ")
    if text in {"已完成", "完成了", "完成", "做好了"}:
        return {"action": "complete"}
    if text in {"取消提醒", "取消", "不用提醒了", "不提醒了"}:
        return {"action": "cancel"}
    match = re.fullmatch(r"(?:延后|推迟|稍后|再等)(.+?)(?:后)?", text)
    if match is None:
        match = re.fullmatch(r"(.+?)后(?:再?提醒我?)?", text)
    if match is None:
        return None
    seconds = parse_duration_seconds(match[1])
    if seconds is None:
        return None
    if seconds % 60 or not 60 <= seconds <= 525600 * 60:
        return {"error": "请用 1 分钟到一年之间的整分钟时长，例如“十分钟后”。"}
    return {"action": "snooze", "minutes": int(seconds / 60)}


def matches(text: str, reply_ref: str = "") -> bool:
    return bool(reply_ref and _reply_action(text))


def is_control(text: str, reply_ref: str = "") -> bool:
    return matches(text, reply_ref)


async def matches_reference(client: Any, actor: dict, reply_ref: str) -> bool:
    result = await client.request_feature("GET", "/v1/reminders/reply-context", params={"message_id": reply_ref}, actor=actor)
    return result.get("matched") is True


def render(item: dict) -> str:
    status = {"open": "未完成", "completed": "已完成", "cancelled": "已取消"}.get(item["status"], item["status"])
    local_time = datetime.fromisoformat(item["due_at"]).astimezone(ZoneInfo(item["timezone"])).strftime("%Y-%m-%d %H:%M")
    delivery = {"scheduled": "等待到期", "sending": "准备发送", "queued": "等待送达", "delivered": "已送达", "failed": "发送失败", "outcome_unknown": "送达结果待核对"}.get(item["delivery_status"], item["delivery_status"])
    return f"提醒 {item['id']}：{item['text']}\n时间：{local_time}（{item['timezone']}）\n状态：{status}，投递：{delivery}\n引用实际到期提醒消息，可回复“已完成”“十分钟后”或“取消提醒”。\n/提醒完成 {item['id']}\n/提醒延后 {item['id']} 10分钟"


async def handle(client: Any, actor: dict, text: str, reply_ref: str = "") -> dict | None:
    natural = _reply_action(text)
    if natural is not None:
        if not reply_ref:
            return {"text": "请引用要处理的提醒消息，避免误改其他提醒。"}
        if "error" in natural:
            return {"text": natural["error"]}
        try:
            item = await client.request_feature("POST", "/v1/reminders/reply-actions", body={"message_id": reply_ref, **natural}, actor=actor)
            return {"text": render(item)}
        except Exception as exc:
            return {"text": f"提醒操作未完成：{_error_detail(exc)}"}
    parts = text.strip().lstrip("/").split(maxsplit=1)
    if not parts or parts[0] not in COMMANDS:
        return None
    command, rest = parts[0], parts[1] if len(parts) > 1 else ""
    try:
        if command == "提醒列表":
            response = await client.request_feature("GET", "/v1/reminders", actor=actor)
            return {"text": "\n\n".join(render(item) for item in response["reminders"][:30]) or "暂无提醒。"}
        if command == "提醒":
            match = re.fullmatch(r"(\d+)(分钟|小时|天)后\s+(.+)", rest, flags=re.S)
            if not match:
                return {"text": "用法：/提醒 10分钟后 喝水；也可以直接告诉我明确日期和时间。"}
            minutes = int(match[1]) * {"分钟": 1, "小时": 60, "天": 1440}[match[2]]
            due = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
            item = await client.request_feature("POST", "/v1/reminders", body={"text": match[3], "due_at": due, "timezone": "Asia/Shanghai"}, actor=actor)
            return {"text": render(item)}
        tokens = rest.split(maxsplit=1)
        reminder_id = tokens[0] if tokens else reply_ref
        if not re.fullmatch(r"R[0-9a-f]{12}", reminder_id):
            return {"text": "请指定提醒编号，避免误改其他提醒。"}
        item = await client.request_feature("GET", f"/v1/reminders/{reminder_id}", actor=actor)
        action = {"提醒完成": "complete", "提醒取消": "cancel", "提醒延后": "snooze"}[command]
        body = {"expected_revision": item["revision"]}
        if action == "snooze":
            delay = re.fullmatch(r"(\d+)(分钟|小时|天)", tokens[1] if len(tokens) > 1 else "")
            if not delay:
                return {"text": f"用法：/提醒延后 {reminder_id} 10分钟"}
            body["minutes"] = int(delay[1]) * {"分钟": 1, "小时": 60, "天": 1440}[delay[2]]
        item = await client.request_feature("POST", f"/v1/reminders/{reminder_id}/{action}", body=body, actor=actor)
        return {"text": render(item)}
    except Exception as exc:
        return {"text": f"提醒操作未完成：{_error_detail(exc)}"}


def _error_detail(error: Exception) -> str:
    response = getattr(error, "response", None)
    if response is not None:
        try:
            detail = response.json().get("detail")
            if isinstance(detail, str):
                return detail
        except (ValueError, AttributeError):
            pass
    return str(error)
