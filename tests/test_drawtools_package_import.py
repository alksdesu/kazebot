"""drawtools 的两种加载方式。

bot 和 supervisor 按包导入它，drawtools 脚本按自己的目录导入它。前者要一点进程状态
都不留 —— 往 sys.path 插一个目录，进程里所有的 common / cleanup 就全被它盖住了。
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from supervisor.admin_api import create_admin_router  # noqa: E402

from _onebot_harness import load_runtime  # noqa: E402

_DRAWTOOLS = _ROOT / "tools" / "drawtools"
# drawtools 目录里各有一份同名模块，顶层冒出来就说明有人把那个目录塞进了 sys.path。
_SHADOWED = ("common", "cleanup", "preset_manager", "config_io", "draw_context", "format_plan")


def _drawtools_modules() -> list[str]:
    return [name for name in sys.modules
            if name in _SHADOWED or name == "tools.drawtools" or name.startswith("tools.drawtools.")]


@pytest.fixture()
def fresh_imports() -> Iterator[None]:
    """先摘掉再还原：别的测试按脚本目录导入过 drawtools，留着就看不出这一次新增了什么。"""
    saved = {name: sys.modules[name] for name in _drawtools_modules()}
    for name in saved:
        del sys.modules[name]
    try:
        yield
    finally:
        for name in _drawtools_modules():
            del sys.modules[name]
        sys.modules.update(saved)


def _run_child(code: str, cwd: Path) -> Any:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    env.pop("PYTHONPATH", None)
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=str(cwd),
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=120,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


# 脚本以 python <脚本路径> 跑起来，sys.path[0] 就是 drawtools 自己那一级。
_STYLES = {
    "package": ("tools.drawtools.{name}", _ROOT),
    "script_dir": ("{name}", _DRAWTOOLS),
}


def _import_in_child(name: str, attr: str, style: str, expression: str) -> Any:
    module, cwd = _STYLES[style]
    code = (
        "import json\n"
        f"from {module.format(name=name)} import {attr}\n"
        f"print(json.dumps({expression}, ensure_ascii=False))\n"
    )
    return _run_child(code, cwd)


def test_loading_the_preset_manager_leaves_sys_path_and_top_level_modules_alone(
    fresh_imports: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = load_runtime(monkeypatch, tmp_path)
    before = list(sys.path)

    list_presets, switch_preset, preset_not_found = runtime.commands._load_drawtools_preset_manager()

    assert callable(list_presets) and callable(switch_preset)
    assert issubclass(preset_not_found, Exception)
    assert sys.path == before
    assert [name for name in _SHADOWED if name in sys.modules] == []
    # 真走了一次导入，不是撞上别的测试留下的缓存。
    assert "tools.drawtools.preset_manager" in sys.modules


def test_the_admin_cleanup_route_leaves_sys_path_and_top_level_modules_alone(
    fresh_imports: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    common = importlib.import_module("tools.drawtools.common")
    monkeypatch.setattr(common, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(common, "load_settings", lambda: {"storage": {"retention_days": 7, "max_total_mb": 2048}})
    endpoint = next(route.endpoint for route in create_admin_router(tmp_path).routes
                    if getattr(route, "path", "") == "/drawtools/cleanup")
    before = list(sys.path)

    result = endpoint()

    assert result["ok"] is True and result["deleted_count"] == 0
    # 清的是替身工作区，说明 cleanup 是这一刻才按包导入的，拿到了换过的 WORKSPACE_ROOT。
    assert (tmp_path / "data" / "attachments" / "novelai").is_dir()
    assert sys.path == before
    assert [name for name in _SHADOWED if name in sys.modules] == []


@pytest.mark.parametrize("style", list(_STYLES))
@pytest.mark.parametrize("name,attr", [
    ("preset_manager", "list_presets"),
    ("preset_manager", "switch_preset"),
    ("cleanup", "cleanup_novelai_attachments"),
    ("cleanup", "maybe_cleanup_novelai_attachments"),
])
def test_drawtools_entry_points_import_under_both_styles(name: str, attr: str, style: str) -> None:
    assert _import_in_child(name, attr, style, f"callable({attr})") is True


def test_the_preset_list_is_the_same_whichever_way_it_was_imported() -> None:
    listed = _import_in_child("preset_manager", "list_presets", "package", "list_presets()")

    assert listed, "预设列表为空的话两边相等证明不了什么"
    assert _import_in_child("preset_manager", "list_presets", "script_dir", "list_presets()") == listed
