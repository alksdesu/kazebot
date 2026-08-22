"""Clonoth launcher.

默认启动 Supervisor，并自动拉起统一引擎进程与本地 CLI。

重启机制：
    supervisor 以退出码 75 退出表示"请重启"，
    本脚本检测到后自动重新启动。

用法：
    python main.py

也可以单独启动：
    python -m supervisor.main
    python -m engine
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

RESTART_EXIT_CODE = 75


def _child_env() -> dict[str, str]:
    """让子进程找得到本仓库。

    多实例的 cwd 是各自的工作区而非代码目录，而 -m 只把 cwd 加进 sys.path。
    """
    env = os.environ.copy()
    root = str(Path(__file__).resolve().parent)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (root, env.get("PYTHONPATH", "")) if p)
    return env


def main() -> None:
    env = _child_env()
    while True:
        result = subprocess.call([sys.executable, "-m", "supervisor.main", *sys.argv[1:]], env=env)
        if result == RESTART_EXIT_CODE:
            print(f"[launcher] supervisor exited with code {RESTART_EXIT_CODE}, restarting in 1s...", flush=True)
            time.sleep(1)  # 等待端口释放
            continue
        sys.exit(result)


if __name__ == "__main__":
    main()
