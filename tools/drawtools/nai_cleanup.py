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
    from common import DrawtoolsConfigUnreadable
    # cleanup 经 common 把代码根放进 sys.path，工具目录不在里面，_channel 只能按包名找
    from tools._channel import SwitchValueError, switch_arg

    def fail(error: str) -> None:
        print(json.dumps({"ok": False, "error": error, "data": {"result": f"ERROR: {error}"}}, ensure_ascii=False))
        sys.exit(1)

    args = json.loads(sys.stdin.read() or "{}")
    try:
        force = switch_arg(args, "force", False)
    except SwitchValueError as exc:
        fail(str(exc))
    try:
        result = cleanup_novelai_attachments(force=force)
    except DrawtoolsConfigUnreadable as exc:
        # 保留期和容量上限都在 settings.yaml 里，读不了就不删
        fail(str(exc))
    result.setdefault("data", {})
    result["data"] = {
        "result": f"清理完成：删除 {result.get('deleted_count', 0)} 个文件，释放 {result.get('deleted_mb', 0)} MB；剩余 {result.get('remaining_count', 0)} 个文件 / {result.get('remaining_mb', 0)} MB",
        **result,
    }
    print(json.dumps(result, ensure_ascii=False))
