from __future__ import annotations

SPEC = {
    "name": "nai_cleanup",
    "description": "清理 data/attachments/novelai 下的 NovelAI 生成图。按过期天数和容量上限删除最旧文件。",
    "input_schema": {
        "type": "object",
        "properties": {
            "force": {"type": "boolean", "description": "忽略 cleanup_enabled，强制执行清理"},
        },
        "required": [],
    },
}

TIMEOUT_SEC = 60.0

if __name__ == "__main__":
    import json
    import sys
    from cleanup import cleanup_novelai_attachments

    args = json.loads(sys.stdin.read() or "{}")
    result = cleanup_novelai_attachments(force=bool(args.get("force", False)))
    result.setdefault("data", {})
    result["data"] = {
        "result": f"清理完成：删除 {result.get('deleted_count', 0)} 个文件，释放 {result.get('deleted_mb', 0)} MB；剩余 {result.get('remaining_count', 0)} 个文件 / {result.get('remaining_mb', 0)} MB",
        **result,
    }
    print(json.dumps(result, ensure_ascii=False))
