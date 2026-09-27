"""ToolRegistry 建完之后 sys.path 必须原样。

工作区那份 tools/ 只有一个空 __init__.py，插进 sys.path[0] 就把代码目录的 tools/ 盖掉，
本进程后面 import tools._image 直接 ModuleNotFoundError。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Iterator

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from toolbox.registry import ToolRegistry  # noqa: E402

_REPO_TOOLS = _ROOT / "tools"


def _drop_tools_modules() -> None:
    for name in [n for n in sys.modules if n == "tools" or n.startswith("tools.")]:
        del sys.modules[name]
    importlib.invalidate_caches()


@pytest.fixture
def import_state() -> Iterator[None]:
    # 只有在 tools 还没进 sys.modules 时才测得到解析结果，拆完必须还原，否则别的测试拿到的是另一份 tools。
    saved_path = list(sys.path)
    saved_modules = {n: m for n, m in sys.modules.items() if n == "tools" or n.startswith("tools.")}
    try:
        yield
    finally:
        sys.path[:] = saved_path
        _drop_tools_modules()
        sys.modules.update(saved_modules)


def test_registry_leaves_sys_path_untouched(tmp_path: Path, import_state: None) -> None:
    warmup = tmp_path / "warmup"
    # 内置工具模块的首次 import 只发生在第一个 registry 上，先走完再取快照。
    ToolRegistry(workspace_root=warmup, tools_dir=warmup / "tools")
    before = list(sys.path)

    probe = tmp_path / "probe"
    ToolRegistry(workspace_root=probe, tools_dir=probe / "tools")

    assert str(probe) not in sys.path
    assert sys.path == before


def test_tools_package_still_resolves_to_repo(tmp_path: Path, import_state: None) -> None:
    ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools")
    _drop_tools_modules()

    assert Path(importlib.import_module("tools").__file__).resolve().parent == _REPO_TOOLS
    # _image 只在代码目录那份 tools/ 里，工作区盖上来时它是第一个消失的。
    assert Path(importlib.import_module("tools._image").__file__).resolve().parent == _REPO_TOOLS


def test_external_tools_load_without_sys_path(tmp_path: Path, import_state: None) -> None:
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "probe_tool.py").write_text(
        "SPEC = {'name': 'probe_tool', 'description': 'probe', "
        "'input_schema': {'type': 'object', 'properties': {}}}\n",
        encoding="utf-8",
    )
    before = list(sys.path)

    registry = ToolRegistry(workspace_root=tmp_path, tools_dir=tools_dir)

    assert registry.get_spec("probe_tool") is not None
    assert sys.path == before


def test_workspace_on_sys_path_shadows_repo_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, import_state: None,
) -> None:
    # 手动插回 reload 从前插的那一条，上面两条断言就不是空转：回归回来就是这个结果。
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "__init__.py").touch()
    monkeypatch.syspath_prepend(str(tmp_path))
    _drop_tools_modules()

    shadowed = Path(importlib.import_module("tools").__file__).resolve().parent
    assert shadowed == (tmp_path / "tools").resolve()
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("tools._image")
