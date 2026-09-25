from __future__ import annotations

import re

from clonoth_sdk.command_text import canonicalize_quiet_command


HELP = (
    "群协作：/投票创建 标题 | 选项1 | 选项2；/投票 编号 1；"
    "/活动创建 标题 | 名额；/活动列表；/报名 编号；/退出活动 编号；/确认 编号；"
    "/名单 编号；/活动提醒 编号；/结束活动 编号；/取消活动 编号。\n"
    "群指引：/群规；/群资料；/常见问题。\n"
    "对话：/旁听 30分钟（仍接明确请求）；/安静 30分钟（仅控制命令）；/恢复；/安静状态。"
)


def activity_text(data: dict) -> str:
    header = f"{data['title']}（{data['id']}，{data['status']}）"
    if data["kind"] == "poll":
        return header + "\n" + "\n".join(f"{index + 1}. {option}：{data['counts'][index]} 票" for index, option in enumerate(data["options"])) + f"\n投票：/投票 {data['id']} 选项编号"
    labels = {"joined": "待确认", "confirmed": "已确认", "waiting": "候补", "left": "已退出"}
    members = [f"{item['name']}（{labels[item['status']]}）" for item in data["participants"] if item["status"] != "left"]
    return header + f"\n名额 {data['capacity']}，余 {data['remaining']}。" + ("\n" + "、".join(members) if members else "尚无人报名。") + f"\n/报名 {data['id']} · /确认 {data['id']} · /退出活动 {data['id']}"


def command(service, actor, body: dict) -> dict | None:
    text = canonicalize_quiet_command(str(body.get("text") or "")).strip().replace("／", "/", 1)
    parts = text.split(maxsplit=1)
    name = parts[0] if parts else ""
    tail = parts[1].strip() if len(parts) > 1 else ""
    result = None
    if name == "/群协作帮助":
        result = HELP
    elif name in {"/群规", "/群资料", "/常见问题"}:
        key = {"/群规": "rules", "/群资料": "resources", "/常见问题": "faq"}[name]
        result = service.state(actor)["guide"].get(key) or "本群还没有发布这部分指引，请联系群管理员。"
    elif name in {"/旁听", "/安静", "/恢复", "/安静状态"}:
        if name == "/安静状态":
            state = service.state(actor)
        else:
            match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(秒|分钟|分|小时|时|天)?", tail or "30分钟")
            if name != "/恢复" and not match:
                raise ValueError("请提供明确时长，例如 /旁听 30分钟")
            duration = float(match[1]) * {"秒": 1, "分钟": 60, "分": 60, "小时": 3600, "时": 3600, "天": 86400, None: 60}[match[2]] if match else 0
            state = service.set_quiet(actor, "off" if name == "/恢复" else "listen" if name == "/旁听" else "silent", duration)
        quiet = state["quiet"]
        if not quiet:
            result = "本群已恢复正常回应。"
        else:
            import datetime
            until = datetime.datetime.fromtimestamp(quiet["expires_at"], datetime.timezone(datetime.timedelta(hours=8))).strftime("%m-%d %H:%M:%S")
            mode = "旁听，只回应明确 @、引用和命令" if quiet["mode"] == "listen" else "完全安静，普通对话和通知暂停；恢复、状态和取消等控制命令仍可用"
            result = f"本群进入{mode}，到 {until}（北京时间）自动恢复。"
    elif name in {"/活动列表", "/投票列表"}:
        items = service.activities(actor)
        result = "\n".join(f"{item['id']} · {item['title']} · {item['status']}" for item in items) or "本群还没有活动或投票。"
    elif name in {"/投票创建", "/活动创建"}:
        values = [value.strip() for value in tail.split("|")]
        if name == "/投票创建":
            data = service.create_activity(actor, {"kind": "poll", "title": values[0], "options": values[1:]})
        else:
            if len(values) < 2 or not values[1].isdigit():
                raise ValueError("用法：/活动创建 周六聚餐 | 8")
            data = service.create_activity(actor, {"kind": "event", "title": values[0], "capacity": int(values[1])})
        result = activity_text(data)
    elif name in {"/报名", "/退出活动", "/确认", "/名单", "/投票", "/活动提醒", "/结束活动", "/取消活动"}:
        arguments = tail.split()
        if not arguments:
            raise ValueError("请带上活动编号，可先发送 /活动列表")
        action = {"/报名": "join", "/退出活动": "leave", "/确认": "confirm", "/名单": "view", "/投票": "vote", "/活动提醒": "remind", "/结束活动": "close", "/取消活动": "cancel"}[name]
        choices = [int(value) - 1 for value in re.split(r"[,，、\s]+", " ".join(arguments[1:])) if value] if action == "vote" else []
        data = service.activity_action(actor, arguments[0], action, {"choices": choices, "display_name": body.get("display_name")})
        result = "已安排提醒仍未确认的参与者。\n" if action == "remind" else ""
        result += activity_text(data)
    return None if result is None else {"text": result, "attachments": [], "control": name in {"/旁听", "/安静", "/恢复", "/安静状态"}}
