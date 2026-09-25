from __future__ import annotations

from toolbox.feature_client import request


async def community(args, ctx):
    operation = str(args.get("operation") or "list")
    if operation == "list":
        return await request(ctx, "GET", "/v1/community/activities")
    if operation == "guide":
        return await request(ctx, "GET", "/v1/community/state")
    if operation == "create":
        return await request(ctx, "POST", "/v1/community/activities", body=args.get("activity") or {})
    if operation in {"join", "leave", "vote", "confirm", "view", "close", "cancel", "remind"}:
        identity = str(args.get("activity_id") or "")
        if not identity or not identity.isalnum():
            raise ValueError("请先确定活动编号，不能猜测")
        return await request(ctx, "POST", f"/v1/community/activities/{identity}/{operation}", body={"choices": args.get("choices", [])})
    if operation == "quiet":
        return await request(ctx, "POST", "/v1/community/quiet", body={"mode": args.get("mode", "listen"), "duration_sec": args.get("duration_sec", 1800)})
    raise ValueError("不支持的群协作操作")


def register_tools(registry):
    spec = {
        "name": "community", "description": "管理当前群的投票、报名、确认、名单、群指引和临时安静。权限和群范围由平台强制验证；活动不明确时先list，不要猜编号。",
        "parameters": {"type": "object", "properties": {
            "operation": {"type": "string", "enum": ["list", "guide", "create", "join", "leave", "vote", "confirm", "view", "close", "cancel", "remind", "quiet"]},
            "activity_id": {"type": "string"}, "choices": {"type": "array", "items": {"type": "integer"}, "description": "投票选项下标，从0开始"},
            "activity": {"type": "object", "properties": {"kind": {"enum": ["poll", "event"]}, "title": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}, "capacity": {"type": "integer"}, "deadline": {"type": "number"}, "reminder_at": {"type": "number"}}, "description": "活动截止和提醒时间使用Unix秒；未明确时间时不要编造"},
            "mode": {"enum": ["listen", "silent", "off"]}, "duration_sec": {"type": "number"},
        }, "required": ["operation"]}, "func": community,
    }
    registry.register_builtin_tool(spec["name"], spec["description"], spec["parameters"], community)
