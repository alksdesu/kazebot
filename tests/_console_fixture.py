"""控制台前端测试用的 schema 快照：前端测试按它画页面，test_console_schema 盯着它和后端声明一致。

改了布局或控件之后跑 `python tests/_console_fixture.py` 重新生成。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "adapters" / "web" / "frontend" / "src" / "__tests__" / "fixtures" / "consoleSchemas.json"


def current() -> dict[str, Any]:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from adapters._shared import features
    from platforms.builtin import BUILTIN_PLATFORMS
    from platforms.descriptor import HOOK_CONSOLE_SCHEMA
    from platforms.registry import platform_hook

    # 只取随仓库发布的平台：别的测试临时登记的假平台不该混进前端快照。
    return {
        "features": features.schema(),
        "platforms": {
            platform.id: platform_hook(platform, HOOK_CONSOLE_SCHEMA).schema()
            for platform in BUILTIN_PLATFORMS
            if HOOK_CONSOLE_SCHEMA in platform.hooks
        },
    }


def render(document: dict[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


if __name__ == "__main__":
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(render(current()), encoding="utf-8", newline="\n")
    print(f"wrote {FIXTURE.relative_to(ROOT).as_posix()}")
