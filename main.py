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

import subprocess
import sys
import time

RESTART_EXIT_CODE = 75


def main() -> None:
    while True:
        result = subprocess.call([sys.executable, "-m", "supervisor.main", *sys.argv[1:]])
        if result == RESTART_EXIT_CODE:
            print(f"[launcher] supervisor exited with code {RESTART_EXIT_CODE}, restarting in 1s...", flush=True)
            time.sleep(1)  # 等待端口释放
            continue
        sys.exit(result)


if __name__ == "__main__":
    main()
