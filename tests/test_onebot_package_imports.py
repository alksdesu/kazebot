"""adapters.onebot 包和根目录下的兄弟模块必须能在没有 NoneBot 的进程里导入。

supervisor、迁移脚本和平台注册表都直接 import 这些模块；包一旦在导入时拉起 NoneBot，
它们只能退回按文件路径加载，每个调用方再各自维护一份绕路。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE = _ROOT / "adapters" / "onebot"
_PLUGIN = "onebot_plugin"


def _siblings() -> list[str]:
    return sorted(
        path.stem for path in _PACKAGE.glob("*.py")
        if path.stem not in {"__init__", _PLUGIN}
    )


def test_importing_every_sibling_never_loads_nonebot() -> None:
    modules = ["adapters.onebot", *(f"adapters.onebot.{name}" for name in _siblings())]
    probe = (
        "import importlib, sys\n"
        f"sys.path.insert(0, {str(_ROOT)!r})\n"
        f"for name in {modules!r}:\n"
        "    importlib.import_module(name)\n"
        "leaked = sorted(m for m in sys.modules if m == 'nonebot' or m.startswith('nonebot.'))\n"
        "print(','.join(leaked))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, encoding="utf-8", timeout=120,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == ""


def test_only_the_napcat_body_imports_nonebot() -> None:
    offenders = [
        name for name in _siblings()
        if any(
            line.lstrip().startswith(("import nonebot", "from nonebot"))
            for line in (_PACKAGE / f"{name}.py").read_text(encoding="utf-8").splitlines()
        )
    ]
    assert offenders == []
    body = "\n".join(path.read_text(encoding="utf-8") for path in (_PACKAGE / "napcat").glob("*.py"))
    assert "from nonebot" in body
    # 入口只负责把工作区放进 sys.path 再加载本体，matcher 注册在 napcat.handlers。
    assert "from .napcat import" in (_PACKAGE / f"{_PLUGIN}.py").read_text(encoding="utf-8")
