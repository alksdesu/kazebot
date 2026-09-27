"""另起一个进程拿住文件锁，测跨进程互斥用：进程内的替身测不出文件锁有没有生效。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
# 拿住锁后报一声，等父进程在 stdin 上回一行再放。
_SCRIPT = "\n".join([
    "import sys",
    "sys.path.insert(0, sys.argv[1])",
    "from file_lock import exclusive_lock",
    "with exclusive_lock(sys.argv[2]):",
    "    print('held', flush=True)",
    "    sys.stdin.readline()",
])
WAIT_SEC = 30.0


class LockHolder:
    def __init__(self, path: Path) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-c", _SCRIPT, str(_ROOT), str(path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        assert self.process.stdout is not None
        assert self.process.stdout.readline().strip() == "held"

    def release(self) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write("\n")
        self.process.stdin.flush()
        self.process.wait(timeout=WAIT_SEC)

    def kill(self) -> None:
        self.process.kill()
        self.process.wait(timeout=WAIT_SEC)

    def close(self) -> None:
        if self.process.poll() is None:
            self.kill()
