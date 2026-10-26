"""脚本工具读开关：tools/_channel 里那份 parse_bool 和 clonoth_runtime 的读出同一个结论，各个脚本按自带工具的启动方式都拿得到它。"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import clonoth_runtime  # noqa: E402
from tools import _channel  # noqa: E402

_TOOLS = _ROOT / "tools"

_SAMPLES: list[Any] = [
    True, False, None, 0, 1, 2, -1, 0.0, 1.0, 1.5, "", " ", "1", "0", "2",
    "true", "TRUE", " True ", "false", "False", "yes", "YES", "no", "No", "y", "n", "on", "ON", "off", "Off",
    "enable", "disabled", "maybe", "是", [], {}, ["true"],
]


class TestTheToolsCopy:
    @pytest.mark.parametrize("default", [None, True, False])
    def test_it_reads_every_sample_like_the_runtime(self, default: bool | None) -> None:
        for raw in _SAMPLES:
            assert _channel.parse_bool(raw, default) == clonoth_runtime.parse_bool(raw, default), raw

    def test_it_knows_the_same_words(self) -> None:
        assert _channel._TRUE_WORDS == clonoth_runtime._TRUE_WORDS
        assert _channel._FALSE_WORDS == clonoth_runtime._FALSE_WORDS


# 脚本工具里装着模型参数和手写配置的变量名；接口回包这类本来就是真假值的不在其列
_SWITCH_SOURCES = frozenset({"args", "arguments", "params", "settings", "storage", "spec", "preset", "config", "cfg"})


def _bool_of_a_get(tree: ast.AST) -> list[int]:
    return [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "bool"
        and len(node.args) == 1 and isinstance(node.args[0], ast.Call)
        and isinstance(node.args[0].func, ast.Attribute) and node.args[0].func.attr == "get"
        and isinstance(node.args[0].func.value, ast.Name) and node.args[0].func.value.id in _SWITCH_SOURCES
    ]


def test_no_script_tool_reads_a_switch_with_bool() -> None:
    # bool(args.get(...)) 会把模型或手写配置里的 "false" 当成开
    found = {
        str(path.relative_to(_ROOT)): lines
        for path in sorted(_TOOLS.rglob("*.py"))
        if (lines := _bool_of_a_get(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))))
    }

    assert found == {}


def _run_script(script: Path, workspace: Path, args: dict[str, Any]) -> tuple[int, dict[str, Any], str]:
    """照注册表起自带工具的样子跑：解释器直接跑脚本，工作目录是工作区，不补 PYTHONPATH。"""
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env["CLONOTH_WORKSPACE"] = str(workspace)
    proc = subprocess.run(
        [sys.executable, str(script)], input=json.dumps(args).encode("utf-8"), capture_output=True,
        cwd=str(workspace), env=env, timeout=120,
    )
    # 和注册表一样按 UTF-8 宽松解码：Windows 上子进程按本地编码输出，中文会花，但键和真假值都是 ASCII
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    lines = [line for line in stdout.splitlines() if line.strip()]
    return proc.returncode, (json.loads(lines[-1]) if lines else {}), stderr


def _cleanup_settings(workspace: Path, cleanup_enabled: str) -> None:
    path = workspace / "config" / "drawtools" / "settings.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"storage": {"cleanup_enabled": cleanup_enabled}}), encoding="utf-8")


class TestRunningTheScripts:
    @pytest.mark.parametrize("cleanup_enabled", ["false", "no", "off"])
    def test_nai_cleanup_honours_a_quoted_off_switch_and_a_quoted_false_force(self, tmp_path: Path, cleanup_enabled: str) -> None:
        _cleanup_settings(tmp_path, cleanup_enabled)

        code, result, stderr = _run_script(_TOOLS / "drawtools" / "nai_cleanup.py", tmp_path, {"force": "false"})

        assert code == 0, stderr
        assert result.get("skipped") is True and result.get("reason") == "cleanup disabled"

    def test_nai_cleanup_runs_when_forced_with_a_quoted_true(self, tmp_path: Path) -> None:
        _cleanup_settings(tmp_path, "false")

        code, result, stderr = _run_script(_TOOLS / "drawtools" / "nai_cleanup.py", tmp_path, {"force": "yes"})

        assert code == 0, stderr
        assert result.get("skipped") is not True
        assert result.get("deleted_count") == 0

    @pytest.mark.parametrize("script,args", [
        pytest.param("read_web.py", {"url": "http://127.0.0.1/", "raw": "false"}, id="read_web"),
        pytest.param("exa_search.py", {"use_autoprompt": "false"}, id="exa_search"),
        pytest.param("stocktool/quote.py", {"refresh": "false"}, id="stocktool"),
    ])
    def test_a_script_gets_parse_bool_before_it_turns_the_input_down(self, tmp_path: Path, script: str, args: dict[str, Any]) -> None:
        # 这几个只走不联网就退出的那条路：拿不到 parse_bool 的话进程会先死在 import 上
        _code, result, stderr = _run_script(_TOOLS / script, tmp_path, args)

        assert "ModuleNotFoundError" not in stderr and "ImportError" not in stderr, stderr
        assert result.get("ok") is False
