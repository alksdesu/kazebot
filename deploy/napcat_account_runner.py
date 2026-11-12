"""NapCat 账号动作执行器：以 root 跑，由 napcat-account 路径单元在请求文件出现时触发。

supervisor 带 NoNewPrivileges，改不了 root 的 webui.json 也重启不了容器；它把请求写成文件，
这里校验令牌后执行固定的几个动作。装到 /usr/local/lib/kazebot/，只用标准库。
"""
from __future__ import annotations

import contextlib
import hmac
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

MAIN_WORKSPACE = Path("/opt/kazebot")
DATA_ROOT = Path("/opt/kazebot-data")
NAPCAT_HOME = "/opt/napcat"
CONTAINER = "napcat"

# 和 provision_instance.sh 收得一样紧：名字会拼进路径和容器名。
INSTANCE_RE = re.compile(r"^([a-z][a-z0-9_]{1,31}|[1-9][0-9]{4,10})$")
RESERVED_INSTANCES = frozenset({"stickers", "shared"})
UIN_RE = re.compile(r"^[0-9]{5,12}$")

REQUEST_NAME = "napcat-account.request"
RESULT_NAME = "napcat-account.result"
TOKEN_NAME = ".admin_token"
MAX_INPUT_BYTES = 4096
MESSAGE_LIMIT = 200
RESTART_TIMEOUT_SEC = 120


@dataclass(frozen=True)
class Target:
    """一个实例的请求目录、它那份 webui.json 和它的容器。"""

    data_dir: Path
    webui_config: Path
    container: str

    @property
    def request(self) -> Path:
        return self.data_dir / REQUEST_NAME

    @property
    def result(self) -> Path:
        return self.data_dir / RESULT_NAME

    @property
    def token(self) -> Path:
        return self.data_dir / TOKEN_NAME


def target_for(instance: str) -> Target:
    """不带实例名是主实例。三样东西全由名字推出，不读 kazebot 可写的清单。"""
    if not instance:
        return Target(MAIN_WORKSPACE / "data", Path(NAPCAT_HOME) / "config" / "webui.json", CONTAINER)
    if not INSTANCE_RE.fullmatch(instance) or instance in RESERVED_INSTANCES:
        raise ValueError("实例名不合法")
    return Target(
        DATA_ROOT / instance / "data",
        Path(f"{NAPCAT_HOME}-{instance}") / "config" / "webui.json",
        f"{CONTAINER}-{instance}",
    )


def read_regular(path: Path) -> str | None:
    """只读普通文件。目录归 kazebot，符号链接指向哪里由写它的人决定，root 不跟着走。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return None
        return handle.read(MAX_INPUT_BYTES).decode("utf-8", "replace")


def write_result(target: Target, nonce: str, ok: bool, message: str) -> None:
    payload = json.dumps({"nonce": nonce, "ok": ok, "message": message}, ensure_ascii=False)
    # 先写临时文件再 rename：直接写结果文件会跟着 kazebot 摆在那里的符号链接走。
    fd, tmp = tempfile.mkstemp(dir=target.data_dir, prefix="." + RESULT_NAME + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.chmod(tmp, 0o644)
        os.replace(tmp, target.result)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def set_auto_login(config: Path, uin: str) -> None:
    data = json.loads(config.read_text(encoding="utf-8"))
    data["autoLoginAccount"] = uin
    config.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def apply(target: Target, request: dict[str, object]) -> tuple[bool, str]:
    expected = (read_regular(target.token) or "").strip()
    given = str(request.get("token") or "")
    if not expected or not hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8")):
        return False, "令牌校验失败"

    action = str(request.get("action") or "")
    try:
        if action == "set-auto":
            uin = str(request.get("uin") or "")
            if not UIN_RE.fullmatch(uin):
                return False, "QQ 号格式不对"
            set_auto_login(target.webui_config, uin)
        elif action == "clear-auto":
            set_auto_login(target.webui_config, "")
        elif action == "restart":
            subprocess.run(
                ["docker", "restart", target.container],
                check=True, capture_output=True, timeout=RESTART_TIMEOUT_SEC,
            )
        else:
            return False, "未知动作: " + action[:MESSAGE_LIMIT]
    except subprocess.CalledProcessError as exc:
        return False, (exc.stderr or b"").decode("utf-8", "replace")[:MESSAGE_LIMIT] or "命令失败"
    except Exception as exc:
        return False, str(exc)[:MESSAGE_LIMIT]
    return True, "ok"


def main(argv: list[str]) -> int:
    try:
        target = target_for(argv[1] if len(argv) > 1 else "")
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    # 工作区和它的 data/ 都归 kazebot，换成链接就能把 root 的读写引到别处。
    if os.path.islink(target.data_dir) or os.path.islink(target.data_dir.parent):
        print(f"{target.data_dir} 经过符号链接，不处理", file=sys.stderr)
        return 1

    raw = read_regular(target.request)
    # 读没读懂都先删：PathExists 见文件还在会立刻再触发一次。
    with contextlib.suppress(OSError):
        target.request.unlink()
    if raw is None:
        return 0

    try:
        request = json.loads(raw)
    except ValueError:
        request = None
    if not isinstance(request, dict):
        write_result(target, "", False, "请求不是合法 JSON")
        return 0

    ok, message = apply(target, request)
    write_result(target, str(request.get("nonce") or ""), ok, message)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
