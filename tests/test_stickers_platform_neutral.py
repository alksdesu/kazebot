"""表情包库不认识任何聊天平台：supervisor 和各平台适配器共用这一份，平台的写法只能留在各自的适配器里。"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE = _ROOT / "stickers"
_MODULES = sorted(_PACKAGE.glob("*.py"))
_PLATFORM_WORDS = ("qq", "napcat", "onebot", "nonebot", "[cq:")


def _imported_roots(tree: ast.Module) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("path", _MODULES, ids=[path.name for path in _MODULES])
def test_the_library_imports_no_adapter(path: Path) -> None:
    roots = _imported_roots(ast.parse(path.read_text(encoding="utf-8")))
    assert sorted(roots & {"adapters", "nonebot"}) == []


@pytest.mark.parametrize("path", _MODULES, ids=[path.name for path in _MODULES])
def test_the_library_never_names_a_platform(path: Path) -> None:
    lowered = path.read_text(encoding="utf-8").lower()
    assert [word for word in _PLATFORM_WORDS if word in lowered] == []
