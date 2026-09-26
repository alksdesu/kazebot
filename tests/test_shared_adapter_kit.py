"""adapters/_shared 是各平台共用的一层：不认识任何平台，也不在模块级存状态。

它按绝对路径导入，整个进程只有一份；存了状态，两个平台连接、两次测试加载就会串到一起。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from _module_state_guards import module_level_mutables, rebound_globals

_ROOT = Path(__file__).resolve().parents[1]
_KIT = _ROOT / "adapters" / "_shared"
_MODULES = sorted(_KIT.glob("*.py"))
# 共享层不认识任何平台：平台库和具体适配器都不许碰，engine、platforms、SDK 这些底层工具照常用。
_PLATFORM_LIBRARIES = frozenset({"nonebot"})
_PLATFORM_WORDS = ("qq", "napcat", "onebot", "nonebot", "[cq:")


def _module_names() -> list[str]:
    return [
        "adapters._shared" if path.stem == "__init__" else f"adapters._shared.{path.stem}"
        for path in _MODULES
    ]


def _is_foreign(name: str) -> bool:
    parts = name.split(".")
    if parts[0] in _PLATFORM_LIBRARIES:
        return True
    return parts[0] == "adapters" and (len(parts) == 1 or parts[1] != "_shared")


def _foreign_imports(tree: ast.Module) -> list[str]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names if _is_foreign(alias.name))
        elif isinstance(node, ast.ImportFrom):
            # 两级以上的相对导入已经爬出 _shared，落到具体适配器那一层了。
            if node.level >= 2:
                found.append("." * node.level + (node.module or ""))
            elif node.level == 0:
                found.extend(
                    name for name in (f"{node.module}.{alias.name}" for alias in node.names)
                    if _is_foreign(name)
                )
    return found


def test_the_kit_is_a_regular_package() -> None:
    assert (_KIT / "__init__.py").is_file()
    assert len(_MODULES) > 1


@pytest.mark.parametrize("path", _MODULES, ids=[path.name for path in _MODULES])
def test_the_kit_imports_no_platform(path: Path) -> None:
    assert _foreign_imports(ast.parse(path.read_text(encoding="utf-8"))) == []


@pytest.mark.parametrize("path", _MODULES, ids=[path.name for path in _MODULES])
def test_the_kit_never_names_a_platform(path: Path) -> None:
    lowered = path.read_text(encoding="utf-8").lower()
    assert [word for word in _PLATFORM_WORDS if word in lowered] == []


@pytest.mark.parametrize("path", _MODULES, ids=[path.name for path in _MODULES])
def test_the_kit_keeps_no_state_at_module_level(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert rebound_globals(tree) == []
    assert module_level_mutables(tree) == []


def test_importing_the_kit_pulls_in_no_platform() -> None:
    # 静态扫描只看得到直接 import；经注册表等间接拉进来的只能真导一遍才看得见。
    probe = (
        "import importlib, sys\n"
        "sys.modules['nonebot'] = None\n"
        f"sys.path.insert(0, {str(_ROOT)!r})\n"
        f"for name in {_module_names()!r}:\n"
        "    importlib.import_module(name)\n"
        f"roots = {sorted(_PLATFORM_LIBRARIES)!r}\n"
        "leaked = sorted(\n"
        "    name for name, module in sys.modules.items()\n"
        "    if module is not None and (\n"
        "        name.split('.')[0] in roots\n"
        "        or (name.startswith('adapters.') and not name.startswith('adapters._shared'))\n"
        "    )\n"
        ")\n"
        "print(','.join(leaked))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, encoding="utf-8", timeout=120,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == ""
