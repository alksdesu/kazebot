"""bot 进程里的每个标准 logger 都要接到 loguru。

NoneBot 只配置 loguru；没接上的 logger 只剩 lastResort 兜底，INFO 整条消失，
WARNING 以上也不带时间和来源，排障时对不上别的日志。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_BOT = _ROOT / "bot.py"
# 这几棵树只在 bot 进程里跑；engine、supervisor 的模块各有自己进程的日志配置。
_BOT_PROCESS_TREES = ("adapters/onebot", "adapters/_shared")


def _bridged() -> tuple[str, ...]:
    tree = ast.parse(_BOT.read_text(encoding="utf-8"))
    bridge = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_bridge_stdlib_logging"
    )
    loop = next(node for node in ast.walk(bridge) if isinstance(node, ast.For))
    assert isinstance(loop.iter, ast.Tuple), "桥接名单要写成字面量元组，这条检查才读得到"
    return tuple(element.value for element in loop.iter.elts if isinstance(element, ast.Constant))


def _module_name(path: Path) -> str:
    relative = path.relative_to(_ROOT).with_suffix("")
    parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
    return ".".join(parts)


def _is_get_logger(call: ast.Call) -> bool:
    func = call.func
    return (isinstance(func, ast.Attribute) and func.attr == "getLogger") or (
        isinstance(func, ast.Name) and func.id == "getLogger"
    )


def _logger_names(path: Path) -> list[str]:
    names = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Call) and _is_get_logger(node) and node.args):
            continue
        argument = node.args[0]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            names.append(argument.value)
        elif isinstance(argument, ast.Name) and argument.id == "__name__":
            names.append(_module_name(path))
        else:
            # 拼出来的名字静态看不出落在哪棵树下，原样报出来让人确认。
            names.append(ast.unparse(argument))
    return names


def _bot_process_modules() -> list[Path]:
    return sorted(
        path
        for tree in _BOT_PROCESS_TREES
        for path in (_ROOT / tree).rglob("*.py")
        if "__pycache__" not in path.parts
    )


def test_the_plugin_logger_is_bridged() -> None:
    assert "nonebot.plugin.clonoth_agent" in _bridged()


@pytest.mark.parametrize(
    "path", _bot_process_modules(), ids=lambda path: path.relative_to(_ROOT).as_posix(),
)
def test_every_logger_in_the_bot_process_reaches_loguru(path: Path) -> None:
    bridged = _bridged()
    stray = [
        name for name in _logger_names(path)
        if not any(name == root or name.startswith(f"{root}.") for root in bridged)
    ]
    assert stray == []
