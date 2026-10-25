"""bot.py 只灌本实例工作区自己的 .env：env 指定了工作区就不再回落代码目录或 cwd 那份。

多开时代码目录那份 .env 是主实例的，借过来这个实例就挂到主实例的端口、拿主实例的令牌。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BOT_ENTRY = REPO_ROOT / "bot.py"
_KEY = "CLONOTH_ADMIN_QQ_USERS"
_OTHER = "CLONOTH_ALLOWED_GROUPS"

# 装了 nonebot 也不能真跑，它会去监听端口；塞桩件让 bot.py 走完 import 就停在 main() 之前。
_HARNESS = textwrap.dedent(
    """
    import importlib.util, json, os, sys, types

    entry, repo, dumped = sys.argv[1], sys.argv[2], sys.argv[3]
    sys.path.insert(0, repo)

    nonebot = types.ModuleType("nonebot")
    nonebot.init = nonebot.run = lambda *a, **k: None
    nonebot.get_driver = lambda: types.SimpleNamespace(register_adapter=lambda *a: None)
    nonebot.load_plugin = lambda *a: None
    adapters = types.ModuleType("nonebot.adapters")
    onebot = types.ModuleType("nonebot.adapters.onebot")
    v11 = types.ModuleType("nonebot.adapters.onebot.v11")
    v11.Adapter = object
    sys.modules.update({
        "nonebot": nonebot,
        "nonebot.adapters": adapters,
        "nonebot.adapters.onebot": onebot,
        "nonebot.adapters.onebot.v11": v11,
    })

    spec = importlib.util.spec_from_file_location("bot_under_test", entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    keys = ["CLONOTH_ADMIN_QQ_USERS", "CLONOTH_ALLOWED_GROUPS", "CLONOTH_WORKSPACE"]
    open(dumped, "w", encoding="utf-8").write(json.dumps({k: os.environ.get(k) for k in keys}))
    """
)


def _dir(parent: Path, name: str, env: str | None = None) -> Path:
    root = parent / name
    root.mkdir(parents=True, exist_ok=True)
    if env is not None:
        (root / ".env").write_text(env, encoding="utf-8")
    return root


def _run(tmp_path: Path, *, cwd: Path, code_env: str | None = None,
         extra_env: dict[str, str] | None = None) -> tuple[dict[str, Any], str]:
    """把 bot.py 复制到 tmp_path/code（它的「代码目录」）后加载，返回灌进环境的值和 stderr。"""
    code = _dir(tmp_path, "code", code_env)
    (code / "bot.py").write_bytes(BOT_ENTRY.read_bytes())
    harness = tmp_path / "harness.py"
    harness.write_text(_HARNESS, encoding="utf-8")
    dumped = tmp_path / "seen.json"

    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLONOTH_", "ONEBOT_"))}
    env.update(extra_env or {})
    env["PYTHONIOENCODING"] = "utf-8"
    done = subprocess.run(
        [sys.executable, str(harness), str(code / "bot.py"), str(REPO_ROOT), str(dumped)],
        cwd=str(cwd), env=env, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(dumped.read_text(encoding="utf-8")), done.stderr


def test_a_named_workspace_without_env_never_borrows_the_code_directory_one(tmp_path: Path) -> None:
    instance = _dir(tmp_path, "instance")

    seen, stderr = _run(tmp_path, cwd=instance, code_env=f"{_KEY}=main-instance\n",
                        extra_env={"CLONOTH_WORKSPACE": str(instance)})

    assert seen[_KEY] is None
    assert "下没有 .env" in stderr


def test_a_named_workspace_ignores_the_env_file_in_the_cwd(tmp_path: Path) -> None:
    # 在代码目录里起别的实例：cwd 那份是主实例的。
    instance = _dir(tmp_path, "instance", f"{_OTHER}=from-instance\n")
    here = _dir(tmp_path, "here", f"{_KEY}=from-cwd\n")

    seen, _ = _run(tmp_path, cwd=here, extra_env={"CLONOTH_WORKSPACE": str(instance)})

    assert seen[_KEY] is None
    assert seen[_OTHER] == "from-instance"
    assert Path(seen["CLONOTH_WORKSPACE"]) == instance.resolve()


def test_the_named_workspace_env_still_loses_to_the_process_environment(tmp_path: Path) -> None:
    instance = _dir(tmp_path, "instance", f"{_KEY}=from-file\n")

    seen, _ = _run(tmp_path, cwd=instance, extra_env={"CLONOTH_WORKSPACE": str(instance), _KEY: "from-systemd"})

    assert seen[_KEY] == "from-systemd"


def test_the_cwd_env_file_can_name_the_workspace(tmp_path: Path) -> None:
    workspace = _dir(tmp_path, "workspace", f"{_KEY}=from-workspace\n{_OTHER}=only-in-workspace\n")
    here = _dir(tmp_path, "here", f"CLONOTH_WORKSPACE={workspace.as_posix()}\n{_KEY}=from-cwd\n")

    seen, _ = _run(tmp_path, cwd=here)

    assert seen[_KEY] == "from-cwd"
    assert seen[_OTHER] == "only-in-workspace"
    assert Path(seen["CLONOTH_WORKSPACE"]) == workspace.resolve()


def test_the_code_directory_env_file_can_name_the_workspace(tmp_path: Path) -> None:
    workspace = _dir(tmp_path, "workspace", f"{_OTHER}=from-workspace\n")
    elsewhere = _dir(tmp_path, "elsewhere")

    seen, _ = _run(tmp_path, cwd=elsewhere, code_env=f"CLONOTH_WORKSPACE={workspace.as_posix()}\n")

    assert seen[_OTHER] == "from-workspace"
    assert Path(seen["CLONOTH_WORKSPACE"]) == workspace.resolve()


def test_the_entry_loads_through_the_shared_runtime_helper() -> None:
    assert "clonoth_runtime.load_workspace_env(" in BOT_ENTRY.read_text(encoding="utf-8")
