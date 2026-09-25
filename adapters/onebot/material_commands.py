from __future__ import annotations

import datetime as dt
import re
from typing import Any
from urllib.parse import quote

COMMANDS = {"资料"}
CONTROL_COMMANDS: set[str] = set()


async def record_message(
    client: Any, actor: dict, *, message_id: str, text: str, timestamp: float,
    sender_name: str = "", segments: Any = None, attachments: Any = None,
    direction: str = "inbound", reply_ref: str = "",
) -> dict:
    return await client.request_feature("POST", "/v1/materials/journal/messages", actor={**actor, "message_id": str(message_id)}, body={
        "message_id": str(message_id), "text": text, "timestamp": timestamp,
        "sender_name": sender_name, "attachments": attachments or [], "direction": direction,
        "reply_ref": str(reply_ref or ""),
    })


async def register_attachment(client: Any, actor: dict, attachment: dict, *, message_id: str = "", source_time: float | None = None) -> dict:
    return await client.request_feature("POST", "/v1/materials/sources/register", actor=actor, body={
        "path": attachment.get("path", ""), "name": attachment.get("name"),
        "message_id": str(message_id), "source_time": source_time,
    })


async def handle(client: Any, actor: dict, text: str, reply_ref: str = "") -> dict | None:
    matched = re.match(r"^/?资料(?:\s+(.*))?$", text.strip(), re.S)
    if not matched:
        return None
    command = (matched.group(1) or "").strip()

    async def request(method, path, body=None, params=None):
        return await client.request_feature(method, "/v1/materials" + path, body=body, params=params, actor=actor)

    def result(message, attachments=None):
        return {"text": message, "attachments": attachments or []}

    parts = command.split(maxsplit=2)
    action = parts[0] if parts else "帮助"
    try:
        if action in {"帮助", "help"}:
            return result("资料命令：\n/资料 列表\n/资料 查 <资料ID> <关键词>\n/资料 查聊天 <原话关键词>\n/资料 记录 开|关（管理员）\n/资料 任务 <任务ID>\n/资料 预览 <版本ID>\n/资料 确认 <版本ID>\n/资料 发送 <版本ID>\n/资料 版本 <作品ID>\n/资料 比较 <版本ID1> <版本ID2>\n/资料 恢复 <作品ID> <版本ID>\n/资料 校对 <提取ID> <字段ID>=<文字>\n/资料 校对完成 <提取ID>\n也可以直接发文件或图片，再说明要读取、整理或生成什么。")
        if action == "列表":
            sources = await request("GET", "/sources")
            return result("当前会话资料：\n" + "\n".join(f"{row['name']} [{row['id']}] · {row['status']}" for row in sources[:20]) if sources else "当前会话还没有已登记的资料。")
        if action == "记录":
            if len(parts) < 2 or parts[1] not in {"开", "关"}:
                return result("用法：/资料 记录 开 或 /资料 记录 关")
            config = await request("PUT", "/settings", {"journal_enabled": parts[1] == "开"})
            return result(f"当前会话原消息记录已{'开启' if config['journal_enabled'] else '关闭'}。保留 {config['journal_retention_days']} 天；不会补造过去的聊天记录。")
        if action == "查聊天":
            query = command[len(action):].strip()
            found = await request("GET", "/journal/search", params={"q": query, "limit": 8})
            if not found.get("enabled"):
                return result(found.get("message", "当前会话未开启记录。"))
            lines = []
            for row in found["results"]:
                timestamp = dt.datetime.fromtimestamp(row["timestamp"], dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S UTC+8")
                lines.append(f"{timestamp} {row['sender_name']} [消息 {row['message_id']}]\n{row['text']}")
            return result("\n\n".join(lines) if lines else "授权且仍在保留期内的原消息中，没有找到匹配内容。")
        if action == "查":
            if len(parts) < 2:
                return result("用法：/资料 查 <资料ID> <关键词>")
            found = await request("GET", f"/sources/{quote(parts[1], safe='')}/spans", params={"q": parts[2] if len(parts) > 2 else "", "limit": 5})
            lines = [f"{row['citation']} [{row['id']}]\n{row['text']}" for row in found["results"]]
            return result("\n\n".join(lines) if lines else "没有找到依据。若这是扫描文档，需要先识别相应页面。")
        if action == "重试" and len(parts) > 1:
            job = await request("POST", f"/jobs/{quote(parts[1], safe='')}/retry")
            return result(f"已安排本地生成重试，任务 {job['id']}。不会自动发送成品。")
        if action == "任务" and len(parts) > 1:
            job = await request("GET", f"/jobs/{quote(parts[1], safe='')}")
            if job["status"] == "preview_ready":
                return result(f"已生成并渲染预览。用 /资料 预览 {job['version_id']} 查看；确认后才能发送成品。")
            return result(f"任务状态：{job['status']}" + (f"\n{job['error']}" if job.get("error") else ""))
        if action in {"预览", "确认", "发送"} and len(parts) > 1:
            version_id = quote(parts[1], safe="")
            if action == "发送":
                sent = await request("POST", f"/versions/{version_id}/send")
                return result(sent["message"])
            preview = await request("GET", f"/versions/{version_id}/preview")
            if action == "预览":
                return result(f"版本 {parts[1]}，共 {preview['version']['page_count']} 页。请查看全部预览；确认无误后发送 /资料 确认 {parts[1]}。", preview["attachments"])
            await request("POST", f"/versions/{version_id}/approve", {"sha256": preview["version"]["sha256"], "preview_inspected": True})
            return result(f"已确认这个确定版本。发送成品：/资料 发送 {parts[1]}")
        if action == "版本" and len(parts) > 1:
            artifact = await request("GET", f"/artifacts/{quote(parts[1], safe='')}")
            return result(artifact["name"] + "\n" + "\n".join(f"v{row['number']} {row['id']} · {row['status']}" for row in artifact["versions"]))
        if action == "比较" and len(parts) == 3:
            left = await request("GET", f"/versions/{quote(parts[1], safe='')}/preview")
            right = await request("GET", f"/versions/{quote(parts[2], safe='')}/preview")
            return result(f"依次为版本 {parts[1]} 与 {parts[2]} 的预览。", left["attachments"] + right["attachments"])
        if action == "恢复" and len(parts) == 3:
            artifact = await request("GET", f"/artifacts/{quote(parts[1], safe='')}")
            restored = await request("POST", f"/artifacts/{quote(parts[1], safe='')}/restore", {"version_id": parts[2], "expected_version": artifact["current_version"]})
            return result(f"已从旧文件恢复为新版本 {restored['id']}，未重新生成。请先预览再确认发送。")
        if action in {"校对", "校对完成"} and len(parts) > 1:
            records = await request("GET", "/extractions")
            record = next((row for row in records if row["id"] == parts[1]), None)
            if record is None:
                return result("当前会话未找到该截图提取结果。")
            body = {"expected_revision": record["revision"], "values": {}, "confirm": action == "校对完成"}
            if action == "校对":
                if len(parts) != 3 or "=" not in parts[2]:
                    return result("用法：/资料 校对 <提取ID> <字段ID>=<正确文字>")
                field, corrected = parts[2].split("=", 1)
                body["values"] = {field.strip(): corrected}
            revised = await request("PATCH", f"/extractions/{quote(parts[1], safe='')}", body)
            return result(f"校对结果已保存，修订 {revised['revision']}，状态 {revised['status']}。")
        return result("命令参数不完整。发送 /资料 查看用法。")
    except Exception as exc:
        return result(f"资料操作未完成：{str(exc)[:350]}")
