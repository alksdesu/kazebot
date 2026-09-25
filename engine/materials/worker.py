from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> None:
    from .building import build_file
    from .parsing import parse_document
    from .rendering import capabilities, recalculate_workbook, render_preview
    from .storage import MaterialError

    try:
        from .limits import limit_worker
        limit_worker()
        data = json.loads(sys.stdin.read(8 * 1024 * 1024))
        operation = data["operation"]
        if operation == "capabilities":
            result = capabilities()
        elif operation == "parse":
            result = parse_document(Path(data["path"]))
        elif operation == "preview":
            result = render_preview(Path(data["path"]), Path(data["directory"]))
        elif operation == "build":
            directory = Path(data["directory"])
            artifact = build_file(data["format"], data["spec"], directory)
            if data["format"] == "xlsx":
                artifact = recalculate_workbook(artifact, directory / "calculated")
            result = {"path": str(artifact), "preview": render_preview(artifact, directory / "preview")}
        else:
            raise MaterialError("未知资料处理操作。")
        response = {"ok": True, "data": result}
    except MaterialError as exc:
        response = {"ok": False, "error": str(exc), "code": exc.code, "status": exc.status}
    except ImportError:
        response = {"ok": False, "error": "缺少资料处理依赖，请安装 requirements-materials.txt。", "code": "dependency_unavailable", "status": 503}
    except Exception as exc:
        response = {"ok": False, "error": f"资料处理失败：{type(exc).__name__}: {str(exc)[:300]}", "code": "processing_failed", "status": 422}
    sys.stdout.write(json.dumps(response, ensure_ascii=False))


if __name__ == "__main__":
    main()
