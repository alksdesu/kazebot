from __future__ import annotations

from typing import Any

from toolbox.feature_client import request


async def create_reminder(args: dict, ctx: Any) -> dict:
    try:
        data = await request(ctx, "POST", "/v1/reminders", body=args)
        return {"ok": True, "data": {"result": f"提醒 {data['id']} 已设定：{data['due_at']}。送达后可完成或延后。", "reminder": data}}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "data": {"result": str(exc)}}


async def list_reminders(args: dict, ctx: Any) -> dict:
    try:
        data = await request(ctx, "GET", "/v1/reminders")
        return {"ok": True, "data": {"result": "本人的提醒", **data}}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "data": {"result": str(exc)}}


async def update_reminder(args: dict, ctx: Any) -> dict:
    try:
        reminder_id = str(args.get("reminder_id") or "")
        action = str(args.get("action") or "")
        if action not in {"complete", "snooze", "cancel"}:
            raise ValueError("无效提醒操作")
        body = {key: value for key, value in args.items() if key not in {"reminder_id", "action"}}
        data = await request(ctx, "POST", f"/v1/reminders/{reminder_id}/{action}", body=body)
        return {"ok": True, "data": {"result": f"提醒 {reminder_id} 已更新", "reminder": data}}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "data": {"result": str(exc)}}


def register_tools(registry: Any) -> None:
    registry.register_builtin_tool("create_reminder", "为当前用户创建可完成或延后的提醒。due_at 使用 ISO 日期时间，timezone 指明用户当地时区。", {"type": "object", "properties": {"text": {"type": "string"}, "due_at": {"type": "string"}, "timezone": {"type": "string", "default": "Asia/Shanghai"}}, "required": ["text", "due_at"]}, create_reminder)
    registry.register_builtin_tool("list_reminders", "列出当前用户有权访问的提醒、到期时间、送达与完成状态。", {"type": "object", "properties": {}}, list_reminders)
    registry.register_builtin_tool("update_reminder", "按用户明确要求完成、延后或取消本人提醒。先查询最新 revision，不猜测提醒编号。", {"type": "object", "properties": {"reminder_id": {"type": "string"}, "action": {"type": "string", "enum": ["complete", "snooze", "cancel"]}, "expected_revision": {"type": "integer"}, "minutes": {"type": "integer", "minimum": 1}}, "required": ["reminder_id", "action", "expected_revision"]}, update_reminder)
