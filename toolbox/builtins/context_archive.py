from __future__ import annotations

import asyncio
import json
from typing import Any

from engine.context_archive import ContextArchiveStore

from ..context import ToolContext


def _session(ctx: ToolContext) -> str:
    return str(getattr(ctx, "_context_archive_session_id", "") or ctx.session_id or "")


async def search_context_archive(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    result = await asyncio.to_thread(
        ContextArchiveStore(ctx.workspace_root).search, _session(ctx),
        query=args.get("query", ""), after=args.get("after", ""), before=args.get("before", ""),
        limit=args.get("limit", 20), offset=args.get("offset", 0),
    )
    return {"ok": True, "data": {"result": json.dumps(result, ensure_ascii=False)}}


async def read_context_archive(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    result = await asyncio.to_thread(
        ContextArchiveStore(ctx.workspace_root).read, _session(ctx), args.get("archive_id", ""),
        offset=args.get("offset", 0), limit=args.get("limit", 20), char_offset=args.get("char_offset", 0),
    )
    return {"ok": True, "data": {"result": json.dumps(result, ensure_ascii=False)}}


def register_tools(registry: Any) -> None:
    pagination = {
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
        "offset": {"type": "integer", "minimum": 0, "maximum": 1000000, "default": 0},
    }
    registry.register_builtin_tool(
        "search_context_archive",
        "Search the current conversation's original messages saved before context compression. "
        "Use keywords or original message times to recover omitted details. Expired archives are unavailable. "
        "Each match includes its archive_id and message offset for read_context_archive.",
        {"type": "object", "properties": {
            "query": {"type": "string", "maxLength": 200, "description": "Literal substring; empty lists recent archived messages."},
            "after": {"type": "string", "description": "Inclusive original message timestamp, ISO 8601; timezone defaults to UTC."},
            "before": {"type": "string", "description": "Inclusive original message timestamp, ISO 8601; timezone defaults to UTC."},
            **pagination,
        }, "additionalProperties": False},
        search_context_archive,
    )
    registry.register_builtin_tool(
        "read_context_archive",
        "Read original messages and tool records from an archive of the current conversation. "
        "Continue with next_offset and next_char_offset. Large records use consecutive data_json_chunk "
        "fragments of the complete original JSON; concatenate fragments to reconstruct the record.",
        {"type": "object", "properties": {
            "archive_id": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            **pagination,
            "char_offset": {"type": "integer", "minimum": 0, "default": 0},
        }, "required": ["archive_id"], "additionalProperties": False},
        read_context_archive,
    )
