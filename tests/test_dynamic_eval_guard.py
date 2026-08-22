"""动态求值与 shell 执行的白名单守卫。

config/dynamic_context.yaml 曾用 eval(expr, {"__builtins__": __builtins__}, ctx) 求值
模板变量：完整 builtins、且 YAML 的 key 能覆盖 instruction / node_id / workspace_root。
那条机制已删除，这里把它挡在门外，同时把剩下两处 exec / shell=True 固定成有据可查的例外。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SKIP_DIRS = {".venv", "__pycache__", "_research", "tests", "node_modules", ".git"}

# 例外必须写清「为什么它不是洞」，而不是单纯登记文件名。
# 空集 = 全仓不允许 exec。要加就先在这里写清「为什么它不是洞」。
_ALLOWED_EXEC: set[str] = set()
_ALLOWED_SHELL_TRUE = {
    # schedule 的 type=script 就是「定时跑一条命令」。命令在 create_schedule 时已经过
    # request_guard(execute_command)，deny_patterns / sensitive_patterns 都已生效。
    "supervisor/scheduler.py",
}
_ALLOWED_YAML_LOAD = {
    # 传的 Loader 是 SafeLoader 子类，只从隐式 resolver 表里摘掉 YAML 1.1 的布尔别名
    # （on/off/yes/no），构造器一个没加，仍然构造不出任意 Python 对象。
    "adapters/onebot/yaml_loader.py",
}


def _python_files() -> list[Path]:
    files: list[Path] = []
    for path in _ROOT.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.relative_to(_ROOT).parts):
            continue
        files.append(path)
    return files


def _rel(path: Path) -> str:
    return path.relative_to(_ROOT).as_posix()


def _called_names(tree: ast.AST) -> list[tuple[str, int]]:
    """Collect (dotted_callee_name, lineno) for every call in a module."""
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            found.append((func.id, node.lineno))
        elif isinstance(func, ast.Attribute):
            parts: list[str] = [func.attr]
            inner = func.value
            while isinstance(inner, ast.Attribute):
                parts.append(inner.attr)
                inner = inner.value
            if isinstance(inner, ast.Name):
                parts.append(inner.id)
            found.append((".".join(reversed(parts)), node.lineno))
    return found


@pytest.fixture(scope="module")
def parsed() -> list[tuple[Path, ast.AST]]:
    out: list[tuple[Path, ast.AST]] = []
    for path in _python_files():
        try:
            out.append((path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))))
        except (SyntaxError, UnicodeDecodeError):
            continue
    return out


def test_nothing_calls_eval(parsed) -> None:
    hits = [
        f"{_rel(path)}:{lineno}"
        for path, tree in parsed
        for name, lineno in _called_names(tree)
        if name == "eval"
    ]

    assert hits == [], f"eval() 会把配置或用户输入变成可执行代码: {hits}"


def test_exec_stays_inside_the_known_exception(parsed) -> None:
    hits = [
        f"{_rel(path)}:{lineno}"
        for path, tree in parsed
        for name, lineno in _called_names(tree)
        if name == "exec" and _rel(path) not in _ALLOWED_EXEC
    ]

    assert hits == [], f"新增的 exec() 需要先在本文件登记理由: {hits}"


def test_no_unsafe_yaml_load(parsed) -> None:
    hits = [
        f"{_rel(path)}:{lineno}"
        for path, tree in parsed
        for name, lineno in _called_names(tree)
        if name in {"yaml.load", "_yaml.load"} and _rel(path) not in _ALLOWED_YAML_LOAD
    ]

    assert hits == [], f"yaml.load 会构造任意 Python 对象，用 safe_load: {hits}"


def test_no_pickle_loads_and_no_os_system(parsed) -> None:
    hits = [
        f"{name} at {_rel(path)}:{lineno}"
        for path, tree in parsed
        for name, lineno in _called_names(tree)
        if name in {"pickle.loads", "pickle.load", "os.system"}
    ]

    assert hits == [], f"反序列化/裸 shell 调用需要单独评审: {hits}"


def test_shell_true_stays_inside_the_known_exception(parsed) -> None:
    hits: list[str] = []
    for path, tree in parsed:
        if _rel(path) in _ALLOWED_SHELL_TRUE:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    hits.append(f"{_rel(path)}:{node.lineno}")

    assert hits == [], f"新增的 shell=True 需要先过 request_guard 并在本文件登记: {hits}"


def test_the_dynamic_context_module_is_gone() -> None:
    assert not (_ROOT / "engine" / "inference" / "dynamic_context.py").exists()

    with pytest.raises(ImportError):
        __import__("engine.inference.dynamic_context")


def test_no_module_still_references_dynamic_context(parsed) -> None:
    hits = [
        _rel(path) for path, _tree in parsed
        if "dynamic_context" in path.read_text(encoding="utf-8")
    ]

    assert hits == [], f"dynamic_context 已删除，残留引用会在运行时 ImportError: {hits}"
