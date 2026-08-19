"""python -m shell.tui 入口。"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path


def _enable_windows_vt100() -> None:
    """在 Windows 上启用虚拟终端处理（Textual 需要）。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        # STD_OUTPUT_HANDLE = -11
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Clonoth TUI")
    parser.add_argument(
        "--supervisor",
        default=os.getenv("CLONOTH_SUPERVISOR_URL", "http://127.0.0.1:8765"),
        help="Supervisor base URL",
    )
    parser.add_argument(
        "--conversation-key",
        default=os.getenv("CLONOTH_CONVERSATION_KEY") or None,
    )
    args = parser.parse_args()

    # 确保项目根目录在 sys.path 中
    project_root = str(Path(__file__).resolve().parents[2])
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    _enable_windows_vt100()

    try:
        from shell.tui.app import ClonothApp

        app = ClonothApp(
            supervisor_url=args.supervisor.rstrip("/"),
            conversation_key=args.conversation_key,
        )
        app.run()
    except Exception:
        traceback.print_exc()
        if sys.platform == "win32":
            print("\n[Clonoth TUI] 出错了，按 Enter 关闭窗口...", flush=True)
            try:
                input()
            except EOFError:
                pass
        sys.exit(1)


if __name__ == "__main__":
    main()
