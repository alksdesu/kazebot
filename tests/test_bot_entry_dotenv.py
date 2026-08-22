"""bot.py 的 .env 加载。这一步没做对不会报错，只会让白名单静默变空、群里 @Bot 毫无反应。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BOT_ENTRY = REPO_ROOT / "bot.py"

# 装了 nonebot 也不能真跑，它会去监听端口；塞桩件让 bot.py 走完 import 就停在 main() 之前。
_HARNESS = textwrap.dedent(
    """
    import importlib.util, json, os, sys, types

    entry, dumped = sys.argv[1], sys.argv[2]

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

    keys = ["CLONOTH_ALLOWED_GROUPS", "CLONOTH_ADMIN_QQ_USERS", "ONEBOT_ACCESS_TOKEN"]
    open(dumped, "w", encoding="utf-8").write(
        json.dumps({k: os.environ.get(k) for k in keys})
    )
    """
)


def _run(
    tmp_path: Path,
    lines: list[str] | None,
    *,
    cwd: Path,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str | None]:
    """把 bot.py 复制到临时目录后加载，读出它灌进 os.environ 的东西。"""
    home = tmp_path / "deploy"
    home.mkdir(exist_ok=True)
    (home / "bot.py").write_bytes(BOT_ENTRY.read_bytes())
    if lines is not None:
        (home / ".env").write_text(os.linesep.join(lines), encoding="utf-8")

    harness = tmp_path / "harness.py"
    harness.write_text(_HARNESS, encoding="utf-8")
    dumped = tmp_path / "seen.json"

    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLONOTH_", "ONEBOT_"))}
    env.update(extra_env or {})
    done = subprocess.run(
        [sys.executable, str(harness), str(home / "bot.py"), str(dumped)],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(dumped.read_text(encoding="utf-8"))


def test_env_file_reaches_os_environ(tmp_path: Path) -> None:
    seen = _run(
        tmp_path,
        ["CLONOTH_ALLOWED_GROUPS=987654321", "CLONOTH_ADMIN_QQ_USERS=111222333"],
        cwd=tmp_path / "deploy",
    )
    assert seen["CLONOTH_ALLOWED_GROUPS"] == "987654321"
    assert seen["CLONOTH_ADMIN_QQ_USERS"] == "111222333"


def test_env_file_found_by_file_location_not_cwd(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    seen = _run(tmp_path, ["CLONOTH_ADMIN_QQ_USERS=111222333"], cwd=elsewhere)
    assert seen["CLONOTH_ADMIN_QQ_USERS"] == "111222333"


def test_cwd_env_file_wins_over_the_one_beside_the_entry(tmp_path: Path) -> None:
    # 多开时几个号共用一份代码，端口与工作区只能靠各自 cwd 下那份 .env 区分。
    here = tmp_path / "instance"
    here.mkdir()
    (here / ".env").write_text("CLONOTH_ADMIN_QQ_USERS=444555666", encoding="utf-8")
    seen = _run(tmp_path, ["CLONOTH_ADMIN_QQ_USERS=111222333"], cwd=here)
    assert seen["CLONOTH_ADMIN_QQ_USERS"] == "444555666"


def test_real_environment_wins_over_env_file(tmp_path: Path) -> None:
    # systemd Environment= 和 docker -e 是部署时的显式覆盖，.env 不能把它盖回去。
    seen = _run(
        tmp_path,
        ["ONEBOT_ACCESS_TOKEN=from-file"],
        cwd=tmp_path / "deploy",
        extra_env={"ONEBOT_ACCESS_TOKEN": "from-environment"},
    )
    assert seen["ONEBOT_ACCESS_TOKEN"] == "from-environment"


def test_missing_env_file_is_not_fatal(tmp_path: Path) -> None:
    seen = _run(tmp_path, None, cwd=tmp_path / "deploy")
    assert seen["CLONOTH_ALLOWED_GROUPS"] is None


_MAIN_HARNESS = textwrap.dedent(
    """
    import importlib.util, json, sys, types

    entry, repo, dumped = sys.argv[1], sys.argv[2], sys.argv[3]
    sys.path.insert(0, repo)

    seen = []

    nonebot = types.ModuleType("nonebot")
    nonebot.init = lambda *a, **k: seen.append("init")
    nonebot.run = lambda *a, **k: seen.append("run")
    nonebot.get_driver = lambda: types.SimpleNamespace(
        register_adapter=lambda *a: seen.append("adapter")
    )
    nonebot.load_plugin = lambda name: seen.append("plugin:" + name)
    log = types.ModuleType("nonebot.log")
    log.LoguruHandler = type("LoguruHandler", (), {})
    adapters = types.ModuleType("nonebot.adapters")
    onebot = types.ModuleType("nonebot.adapters.onebot")
    v11 = types.ModuleType("nonebot.adapters.onebot.v11")
    v11.Adapter = object
    sys.modules.update({
        "nonebot": nonebot,
        "nonebot.log": log,
        "nonebot.adapters": adapters,
        "nonebot.adapters.onebot": onebot,
        "nonebot.adapters.onebot.v11": v11,
    })

    import clonoth_runtime
    clonoth_runtime.wait_supervisor = lambda *a, **k: seen.append("waited") or True

    spec = importlib.util.spec_from_file_location("bot_under_test", entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()

    open(dumped, "w", encoding="utf-8").write(json.dumps(seen))
    """
)


def test_main_runs_end_to_end_with_nonebot_stubbed(tmp_path: Path) -> None:
    """把 main() 真跑一遍。

    只做静态检查挡不住这一类：曾经在 init() 之前 import adapters.onebot.config，
    而导入子模块会先执行包的 __init__.py —— 那是插件，第一行 get_driver() 就抛
    NoneBot has not been initialized，进程直接起不来，而源码里看不出任何异常。
    """
    home = tmp_path / "deploy"
    home.mkdir()
    (home / "bot.py").write_bytes(BOT_ENTRY.read_bytes())
    harness = tmp_path / "main_harness.py"
    harness.write_text(_MAIN_HARNESS, encoding="utf-8")
    dumped = tmp_path / "seen.json"

    done = subprocess.run(
        [sys.executable, str(harness), str(home / "bot.py"), str(REPO_ROOT), str(dumped)],
        cwd=str(home), env=os.environ.copy(), capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, done.stderr

    seen = json.loads(dumped.read_text(encoding="utf-8"))
    # 等待必须发生在 init 之前：init 之后再等，NoneBot 已经开始收消息了。
    assert seen.index("waited") < seen.index("init")
    # 适配器插件只能由 load_plugin 加载，且必须在 init 之后。
    assert seen.index("init") < seen.index("plugin:adapters.onebot")
    assert seen[-1] == "run"


def test_dotenv_runs_before_the_plugin_is_imported() -> None:
    # adapters.onebot 在 import 期就读 os.environ，晚一步加载等于没加载。
    source = BOT_ENTRY.read_text(encoding="utf-8")
    assert source.index("load_dotenv(") < source.index("import nonebot")
    assert source.index("load_dotenv(") < source.index("load_plugin")


@pytest.mark.parametrize("marker", ["from dotenv import load_dotenv", "_REPO_ROOT"])
def test_entry_keeps_its_load_shape(marker: str) -> None:
    assert marker in BOT_ENTRY.read_text(encoding="utf-8")
