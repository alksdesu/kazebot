"""每个入口启动时都要先把配置搬进 config/，搬不动就停住。

搬家没做完还往下走：ConfigStore 会在新位置建一份空配置，下次启动再也不会把旧配置搬过来，
而平台令牌读出来是空的 —— 表现是「升级之后 bot 连不上，配置看着还在」。
退出码 78 是 sysexits 的 EX_CONFIG，systemd 据此停住等人，不反复拉起刷日志。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import clonoth_runtime  # noqa: E402
import config_layout  # noqa: E402
import supervisor.main as supervisor_main  # noqa: E402
from workspace import CONFIG_ERROR_EXIT_CODE  # noqa: E402

BOT_ENTRY = _ROOT / "bot.py"


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "data").mkdir(parents=True)
    return root


@pytest.fixture
def no_legacy_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_layout, "_untracked_bundled_tools", lambda report: [])


@pytest.fixture
def pinned_workspace(ws: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """工作区变量走 monkeypatch 设一遍：被测代码会直接改 os.environ，不先记下来就带出用例。"""
    monkeypatch.setenv("CLONOTH_WORKSPACE", str(ws))
    monkeypatch.setenv("ONEBOT_WORKSPACE_ROOT", str(ws))
    monkeypatch.delenv("ONEBOT_WORKSPACE_ROOT")
    # 工作区里有 .env 就不会去读代码目录那份，免得把开发机的变量灌进本用例。
    (ws / ".env").write_text("", encoding="utf-8")
    return ws


# ---------------------------------------------------------------------------
#  supervisor
# ---------------------------------------------------------------------------

class _Socket:
    """替身 socket：bind 按 busy 决定成不成，不真占端口。"""

    busy = False

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def __enter__(self) -> "_Socket":
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False

    def setsockopt(self, *_args: Any) -> None:
        return None

    def bind(self, _address: Any) -> None:
        if type(self).busy:
            raise OSError("address already in use")


@pytest.fixture
def supervisor_boot(ws: Path, monkeypatch: pytest.MonkeyPatch):
    """把 supervisor 的 main() 停在启动早期：端口不真占，日志不动全局，工作区是临时目录。"""
    calls: list[Path] = []

    def record(root: Path) -> Any:
        calls.append(Path(root))
        raise RuntimeError("搬家失败")

    _Socket.busy = False
    monkeypatch.setattr(sys, "argv", ["supervisor"])
    monkeypatch.setattr(supervisor_main, "process_workspace", lambda: ws)
    monkeypatch.setattr(supervisor_main, "configure_supervisor_logging", lambda path, level: "info")
    monkeypatch.setattr(
        supervisor_main, "socket",
        SimpleNamespace(socket=_Socket, AF_INET=0, SOCK_STREAM=0, SOL_SOCKET=0, SO_REUSEADDR=0),
    )
    monkeypatch.setattr(config_layout, "ensure_layout", record)
    return calls


class TestTheSupervisorMigratesBeforeItServes:
    def test_a_failed_migration_stops_the_boot(self, supervisor_boot: list[Path]) -> None:
        with pytest.raises(SystemExit) as exited:
            supervisor_main.main()

        assert exited.value.code == CONFIG_ERROR_EXIT_CODE

    def test_it_migrates_the_workspace_it_settled_on(self, supervisor_boot: list[Path], ws: Path) -> None:
        with pytest.raises(SystemExit):
            supervisor_main.main()

        assert supervisor_boot == [ws]

    def test_a_busy_port_stops_it_before_anything_is_moved(self, supervisor_boot: list[Path]) -> None:
        # 端口被占时另一个实例正跑着，这时候动它的配置等于两个进程同时搬家。
        _Socket.busy = True

        with pytest.raises(SystemExit) as exited:
            supervisor_main.main()

        assert exited.value.code == supervisor_main.PORT_IN_USE_EXIT_CODE
        assert supervisor_boot == []


# ---------------------------------------------------------------------------
#  bot
# ---------------------------------------------------------------------------

# 装了 nonebot 也不能真跑，它会去监听端口；塞桩件让 bot.py 走完 import 就停在 main() 之前。
_STUBS = """
import importlib.util, json, sys, types

init_kwargs = []
nonebot = types.ModuleType("nonebot")
nonebot.init = lambda *a, **k: init_kwargs.append(k)
nonebot.run = lambda *a, **k: None
nonebot.get_driver = lambda: types.SimpleNamespace(register_adapter=lambda *a: None)
nonebot.load_plugin = lambda *a: None
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


def load(entry, repo):
    sys.path.insert(0, repo)
    spec = importlib.util.spec_from_file_location("bot_under_test", entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
"""

# 搬家抛异常时入口该退 78，而不是带着空令牌接着起。
_FAILING_LAYOUT = _STUBS + """
entry, repo, dumped = sys.argv[1], sys.argv[2], sys.argv[3]
module = load(entry, repo)

import config_layout


def boom(root):
    raise RuntimeError("搬家失败")


config_layout.ensure_layout = boom
code = 0
try:
    module._ensure_config_layout()
except SystemExit as exc:
    code = exc.code
open(dumped, "w", encoding="utf-8").write(json.dumps({"code": code}))
"""

# 真跑一遍 main()，记下平台校验和搬家的先后，以及交给 nonebot 的连接令牌。
_FULL_MAIN = _STUBS + """
entry, repo, dumped = sys.argv[1], sys.argv[2], sys.argv[3]
module = load(entry, repo)

import clonoth_runtime
clonoth_runtime.wait_supervisor = lambda *a, **k: None

seen = []
require, layout = module._require_qq_instance, module._ensure_config_layout


def traced(name, fn):
    def run():
        seen.append(name)
        return fn()
    return run


module._require_qq_instance = traced("platform", require)
module._ensure_config_layout = traced("layout", layout)
module.main()

open(dumped, "w", encoding="utf-8").write(json.dumps({"calls": seen, "init": init_kwargs}))
"""


def _run_entry(tmp_path: Path, script: str, *, config: str | None = None) -> dict[str, Any]:
    """把 bot.py 复制到临时目录后加载，工作区就是那个目录。"""
    home = tmp_path / "deploy"
    home.mkdir()
    (home / "bot.py").write_bytes(BOT_ENTRY.read_bytes())
    if config is not None:
        (home / "data").mkdir(exist_ok=True)
        (home / "data" / "config.yaml").write_text(config, encoding="utf-8")
    harness = tmp_path / "harness.py"
    harness.write_text(script, encoding="utf-8")
    dumped = tmp_path / "seen.json"

    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLONOTH_", "ONEBOT_"))}
    # 两端都钉死 UTF-8：中文 Windows 上按 locale 解码会整条流报 UnicodeDecodeError。
    env["PYTHONIOENCODING"] = "utf-8"
    done = subprocess.run(
        [sys.executable, str(harness), str(home / "bot.py"), str(_ROOT), str(dumped)],
        cwd=str(home), env=env, capture_output=True, text=True, encoding="utf-8", timeout=180,
    )
    assert done.returncode == 0, done.stderr or done.stdout
    return json.loads(dumped.read_text(encoding="utf-8"))


class TestTheBotMigratesBeforeItConnects:
    def test_a_failed_migration_stops_the_entry(self, tmp_path: Path) -> None:
        assert _run_entry(tmp_path, _FAILING_LAYOUT)["code"] == CONFIG_ERROR_EXIT_CODE

    def test_the_platform_is_checked_first_and_then_the_layout(self, tmp_path: Path) -> None:
        # 一个实例只跑一个平台，声明的不是 QQ 就不该去动它的配置。
        result = _run_entry(tmp_path, _FULL_MAIN)

        assert result["calls"] == ["platform", "layout"]

    def test_the_token_is_read_from_the_file_the_migration_just_wrote(self, tmp_path: Path) -> None:
        # 改造前令牌在 data/config.yaml 的 platforms 段里；搬家没做完这里读到的就是空的。
        result = _run_entry(
            tmp_path, _FULL_MAIN, config="platforms:\n  qq:\n    access_token: from-config\n",
        )

        assert result["init"] == [{"onebot_access_token": "from-config"}]
        home = tmp_path / "deploy"
        assert (home / "data" / ".config_layout.json").is_file()
        secrets = yaml.safe_load((home / "data" / "platform_secrets.yaml").read_text(encoding="utf-8"))
        assert secrets == {"qq": {"access_token": "from-config"}}
        assert (home / "config" / "models.yaml").is_file()


# ---------------------------------------------------------------------------
#  离线工具
# ---------------------------------------------------------------------------

class TestOfflineToolsMigrateToo:
    def test_the_workspace_is_migrated_before_anything_reads_it(
        self, pinned_workspace: Path, ws: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, no_legacy_tools: None,
    ) -> None:
        # 升级后服务还没起过就跑迁移脚本时，配置还在旧位置：先搬好再读，否则算出来的全是错的。
        code_root = tmp_path / "code"
        (code_root / config_layout.DEFAULT_NODES_DIR).mkdir(parents=True)
        monkeypatch.setattr(config_layout, "CODE_ROOT", code_root)
        (ws / "data" / "config.yaml").write_text(
            "version: 1\nplatforms:\n  qq:\n    access_token: t\n", encoding="utf-8",
        )

        root = clonoth_runtime.offline_workspace(ws)

        assert root == ws
        assert config_layout.models_path(ws).is_file()
        assert clonoth_runtime.load_platform_secrets(ws) == {"qq": {"access_token": "t"}}

    def test_a_workspace_already_migrated_is_left_as_it_is(
        self, pinned_workspace: Path, ws: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, no_legacy_tools: None,
    ) -> None:
        code_root = tmp_path / "code"
        (code_root / config_layout.DEFAULT_NODES_DIR).mkdir(parents=True)
        monkeypatch.setattr(config_layout, "CODE_ROOT", code_root)
        config_layout.ensure_layout(ws)
        marker = (ws / "data" / ".config_layout.json").read_bytes()

        clonoth_runtime.offline_workspace(ws)

        assert (ws / "data" / ".config_layout.json").read_bytes() == marker
