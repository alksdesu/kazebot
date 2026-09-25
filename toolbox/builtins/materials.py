from __future__ import annotations

import json
from urllib.parse import quote

from ..feature_client import request


def _id(args, key):
    value = str(args.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    return quote(value, safe="")


def _result(data, *, attachments=None):
    response = {"ok": True, "data": {"result": json.dumps(data, ensure_ascii=False), "value": data}}
    if attachments:
        response["data"]["attachments"] = attachments
        response["attachments"] = attachments
    return response


async def document(args, ctx):
    action = args.get("action", "list")
    if action == "list":
        data = await request(ctx, "GET", "/v1/materials/sources")
    elif action == "register":
        data = await request(ctx, "POST", "/v1/materials/sources/register", body={"path": args.get("path"), "name": args.get("name")})
    elif action == "search":
        data = await request(ctx, "GET", f"/v1/materials/sources/{_id(args, 'source_id')}/spans", params={"q": args.get("query", ""), "page": args.get("page", 0), "limit": 20})
    elif action == "cite":
        data = await request(ctx, "POST", f"/v1/materials/sources/{_id(args, 'source_id')}/cite", body={"span_id": args.get("span_id"), "quote": args.get("quote")})
    else:
        raise ValueError("unsupported document action")
    return _result(data)


async def chat_search(args, ctx):
    if args.get("message_ref"):
        data = await request(ctx, "GET", f"/v1/materials/journal/messages/{_id(args, 'message_ref')}")
    else:
        data = await request(ctx, "GET", "/v1/materials/journal/search", params={
            "q": args.get("query", ""), "since": args.get("since", 0), "until": args.get("until", 0),
            "sender": args.get("sender", ""), "limit": 15,
        })
    return _result(data)


async def structure(args, ctx):
    action = args.get("action", "draft")
    if action == "draft":
        data = await request(ctx, "POST", "/v1/materials/extractions", body={key: args.get(key) for key in ("source_id", "kind", "fields")})
    elif action == "correct":
        data = await request(ctx, "PATCH", f"/v1/materials/extractions/{_id(args, 'extraction_id')}", body={"expected_revision": args.get("expected_revision"), "values": args.get("values", {})})
    elif action == "list":
        data = await request(ctx, "GET", "/v1/materials/extractions")
    else:
        raise ValueError("unsupported structure action")
    return _result(data)


async def image_versions(args, ctx):
    action = args.get("action", "list")
    if action == "list":
        data = await request(ctx, "GET", "/v1/materials/artifacts")
    elif action == "get":
        data = await request(ctx, "GET", f"/v1/materials/artifacts/{_id(args, 'artifact_id')}")
    elif action == "register":
        data = await request(ctx, "POST", "/v1/materials/artifacts/images", body=args)
    elif action == "reference":
        data = await request(ctx, "GET", f"/v1/materials/versions/{_id(args, 'version_id')}/reference")
    elif action == "restore":
        data = await request(ctx, "POST", f"/v1/materials/artifacts/{_id(args, 'artifact_id')}/restore", body={"version_id": args.get("version_id"), "expected_version": args.get("expected_version")})
    else:
        raise ValueError("unsupported image version action")
    return _result(data)


async def generate(args, ctx):
    action = args.get("action", "generate")
    attachments = None
    if action == "generate":
        data = await request(ctx, "POST", "/v1/materials/artifacts/generate", body=args)
    elif action == "status":
        data = await request(ctx, "GET", f"/v1/materials/jobs/{_id(args, 'job_id')}")
    elif action == "preview":
        data = await request(ctx, "GET", f"/v1/materials/versions/{_id(args, 'version_id')}/preview")
        attachments = data.pop("attachments", [])
        data["notice"] = "仅发送预览图。用户确认后才能发送成品，不能替用户确认。"
    elif action == "cancel":
        data = await request(ctx, "POST", f"/v1/materials/jobs/{_id(args, 'job_id')}/cancel")
    elif action == "retry":
        data = await request(ctx, "POST", f"/v1/materials/jobs/{_id(args, 'job_id')}/retry")
    else:
        raise ValueError("unsupported generation action")
    return _result(data, attachments=attachments)


def register_tools(registry):
    common = {key: {"type": "string"} for key in ("source_id", "artifact_id", "version_id", "expected_version", "job_id")}
    definitions = [
        ("materials_document", "读取本会话已上传文档并检索真实页/段落位置。先search，再cite核验直接引文；不得编页码。register只能注册本任务实际收到的附件。", {
            **common, "action": {"type": "string", "enum": ["list", "register", "search", "cite"]},
            "path": {"type": "string"}, "name": {"type": "string"}, "query": {"type": "string"},
            "page": {"type": "integer"}, "span_id": {"type": "string"}, "quote": {"type": "string"},
        }, document),
        ("materials_chat_search", "检索当前授权会话保留的原消息和真实时间，返回message_id供精确回查。未记录/已过期必须直说，不从摘要编原话。", {
            "query": {"type": "string"}, "message_ref": {"type": "string"}, "sender": {"type": "string"},
            "since": {"type": "number", "description": "起始Unix时间"}, "until": {"type": "number", "description": "结束Unix时间"},
        }, chat_search),
        ("materials_structure", "仅在实际看见source_id对应截图后，把表格/待办/错误原文整理成可校对字段。看不清标uncertain，bbox为0到1。结果是待校对草稿，不表示用户已确认。", {
            **common, "action": {"type": "string", "enum": ["draft", "list", "correct"]},
            "kind": {"type": "string", "enum": ["table", "todos", "error"]},
            "fields": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string"}, "label": {"type": "string"}, "value": {"type": "string"},
                "uncertain": {"type": "boolean"}, "bbox": {"type": "array", "items": {"type": "number"}},
            }, "required": ["label", "value"]}}, "extraction_id": {"type": "string"},
            "expected_revision": {"type": "integer"}, "values": {"type": "object"},
        }, structure),
        ("materials_image_versions", "管理图像版本。reference取得已授权旧版本的实际图片路径，交给真实生图工具image_paths使用；register登记真实生成图片source；restore恢复既有字节不重新生成。", {
            **common, "action": {"type": "string", "enum": ["list", "get", "register", "reference", "restore"]},
            "base_version": {"type": "string"}, "instruction": {"type": "string"},
        }, image_versions),
        ("materials_generate", "生成真正PDF/DOCX/XLSX/PPTX并渲染预览，返回job_id后用status查询。preview仅发送预览图；成品需用户确认。文档spec={title,sections:[{heading,paragraphs,table}]}; xlsx={title,sheets:[{name,rows}]}; pptx={title,slides:[{title,bullets,notes}]}。", {
            **common, "action": {"type": "string", "enum": ["generate", "status", "preview", "cancel", "retry"]},
            "format": {"type": "string", "enum": ["pdf", "docx", "xlsx", "pptx"]}, "spec": {"type": "object"},
        }, generate),
    ]
    for name, description, properties, function in definitions:
        registry.register_builtin_tool(name, description, {"type": "object", "properties": properties, "additionalProperties": False}, function)
