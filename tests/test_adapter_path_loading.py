"""测试按包名 import 适配器模块，不按文件路径再加载一份。

按路径加载会在 sys.modules 里多出一份同源模块，类各算各的，isinstance 和 monkeypatch 都会落空。
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator, NamedTuple

import pytest

_TESTS = Path(__file__).resolve().parent
# harness 要在 NoneBot 桩下拼出 napcat 本体，按路径加载正是它的用途。
_HARNESS = "_onebot_harness.py"
# config.py 在导入时读环境变量，默认工作区只能清掉环境重新执行一遍才测得到。
_ALLOWED = frozenset({("test_qq_env_docs.py", "adapters/onebot/config.py")})
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_NESTED_SCOPES = (*_FUNCTIONS, ast.Lambda, ast.ClassDef)
_PATH_WRAPPERS = frozenset({"Path", "PurePath", "str"})
_UNKNOWN = ""


class _PathParameter(NamedTuple):
    position: int
    keyword: str


_PATH_LOADERS = {
    "spec_from_file_location": _PathParameter(1, "location"),
    "SourceFileLoader": _PathParameter(1, "path"),
    "run_path": _PathParameter(0, "path_name"),
}


def _callee(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def _location(call: ast.Call) -> ast.expr | None:
    parameter = _PATH_LOADERS.get(_callee(call))
    if parameter is None:
        return None
    for keyword in call.keywords:
        if keyword.arg == parameter.keyword:
            return keyword.value
    return call.args[parameter.position] if parameter.position < len(call.args) else None


def _join(left: str, right: str) -> str:
    return "/".join(part for part in (left, right) if part)


def _is_name(node: ast.AST, identifier: str) -> bool:
    return isinstance(node, ast.Name) and node.id == identifier


def _own_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    pending = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        yield node
        if not isinstance(node, _NESTED_SCOPES):
            pending.extend(ast.iter_child_nodes(node))


def _assignments(scope: ast.AST, identifier: str) -> list[ast.expr]:
    values: list[ast.expr] = []
    for node in _own_nodes(scope):
        if isinstance(node, ast.Assign) and any(_is_name(target, identifier) for target in node.targets):
            values.append(node.value)
        elif isinstance(node, ast.AnnAssign) and _is_name(node.target, identifier) and node.value is not None:
            values.append(node.value)
        elif isinstance(node, (ast.For, ast.AsyncFor)) and _is_name(node.target, identifier):
            if isinstance(node.iter, (ast.Tuple, ast.List, ast.Set)):
                values.extend(node.iter.elts)
    return values


class _PathValues:
    """一个测试文件里某个表达式可能取到的路径。

    变量沿赋值往回找，形参再看默认值和每个调用处传进来的实参；来源不止一个时取全部组合，宁可多报不漏报。
    """

    def __init__(self, tree: ast.Module) -> None:
        self._tree = tree
        self._parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        self._calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]

    def of(self, node: ast.AST, seen: frozenset[str] = frozenset()) -> set[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.JoinedStr):
            return self._formatted(node, seen)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return {_join(left, right) for left in self.of(node.left, seen) for right in self.of(node.right, seen)}
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return {left + right for left in self.of(node.left, seen) for right in self.of(node.right, seen)}
        if isinstance(node, ast.Call) and _callee(node) in _PATH_WRAPPERS:
            return self._joined(node.args, seen)
        if isinstance(node, ast.Name) and node.id not in seen:
            bound = seen | {node.id}
            return set().union(*(self.of(value, bound) for value in self._bindings(node))) or {_UNKNOWN}
        return {_UNKNOWN}

    def _formatted(self, node: ast.JoinedStr, seen: frozenset[str]) -> set[str]:
        results = {""}
        for part in node.values:
            if isinstance(part, ast.Constant):
                options = {str(part.value)}
            else:
                options = (self.of(part.value, seen) - {_UNKNOWN}) or {"*"}
            results = {prefix + option for prefix in results for option in options}
        return results

    def _joined(self, parts: list[ast.expr], seen: frozenset[str]) -> set[str]:
        results = {""}
        for part in parts:
            results = {_join(prefix, value) for prefix in results for value in self.of(part, seen)}
        return results

    def _bindings(self, name: ast.Name) -> list[ast.expr]:
        scope = self._scope(name)
        values = [*_assignments(scope, name.id), *self._arguments(scope, name.id)]
        if not values and scope is not self._tree:
            values = _assignments(self._tree, name.id)
        return values

    def _scope(self, node: ast.AST) -> ast.AST:
        while node in self._parents and not isinstance(node, _FUNCTIONS):
            node = self._parents[node]
        return node

    def _arguments(self, scope: ast.AST, identifier: str) -> list[ast.expr]:
        if not isinstance(scope, _FUNCTIONS):
            return []
        signature = scope.args
        positional = [argument.arg for argument in (*signature.posonlyargs, *signature.args)]
        keyword_only = [argument.arg for argument in signature.kwonlyargs]
        if identifier not in positional and identifier not in keyword_only:
            return []
        defaults = {
            **dict(zip(positional[len(positional) - len(signature.defaults):], signature.defaults)),
            **{name: value for name, value in zip(keyword_only, signature.kw_defaults) if value is not None},
        }
        values = [defaults[identifier]] if identifier in defaults else []
        position = positional.index(identifier) if identifier in positional else -1
        bound_receiver = positional[:1] in (["self"], ["cls"])
        for call in self._calls:
            if _callee(call) != scope.name:
                continue
            values.extend(keyword.value for keyword in call.keywords if keyword.arg == identifier)
            index = position - (1 if bound_receiver and isinstance(call.func, ast.Attribute) else 0)
            if 0 <= index < len(call.args):
                values.append(call.args[index])
        return values


def _adapter_target(path: str) -> str:
    parts = [part for part in path.replace("\\", "/").split("/") if part not in {"", "."}]
    return "/".join(parts[parts.index("adapters"):]) if "adapters" in parts else ""


def _adapter_loads(source: str) -> list[tuple[int, str]]:
    """源码里按路径加载 adapters 下模块的地方：(行号, 从 adapters 起的相对路径)。"""
    tree = ast.parse(source)
    loads = [
        (node, location) for node in ast.walk(tree)
        if isinstance(node, ast.Call) and (location := _location(node)) is not None
    ]
    if not loads:
        return []
    values = _PathValues(tree)
    return sorted(
        (call.lineno, target)
        for call, location in loads
        for target in {_adapter_target(path) for path in values.of(location)} - {""}
    )


@pytest.fixture(scope="module")
def path_loads() -> list[tuple[str, int, str]]:
    """每个测试文件里按路径加载 adapters 模块的地方：(文件名, 行号, 目标)。"""
    return [
        (path.name, line, target)
        for path in sorted(_TESTS.glob("*.py")) if path.name != _HARNESS
        for line, target in _adapter_loads(path.read_text(encoding="utf-8"))
    ]


def test_tests_import_adapter_modules_by_package_name(path_loads: list[tuple[str, int, str]]) -> None:
    offenders = [f"{name}:{line} {target}" for name, line, target in path_loads if (name, target) not in _ALLOWED]
    assert offenders == [], "改成 from adapters... import，别按文件路径再加载一份"


def test_every_exception_is_still_in_use(path_loads: list[tuple[str, int, str]]) -> None:
    assert _ALLOWED <= {(name, target) for name, _, target in path_loads}, "例外名单里有已经不按路径加载的条目，删掉它"


@pytest.mark.parametrize("source, expected", [
    pytest.param(
        'ROOT = Path(__file__).resolve().parents[1]\n'
        'PATH = ROOT / "adapters" / "onebot" / "send_contract.py"\n'
        'spec = importlib.util.spec_from_file_location("copy", PATH)\n',
        [(3, "adapters/onebot/send_contract.py")],
        id="module_level_variable",
    ),
    pytest.param(
        'def load(name, relpath):\n'
        '    return importlib.util.spec_from_file_location(name, ROOT / relpath)\n'
        'load("hash", "adapters/_shared/conversation_hash.py")\n'
        'load("migrate", "deploy/migrate_qq_conversation_hash.py")\n',
        [(2, "adapters/_shared/conversation_hash.py")],
        id="helper_parameter",
    ),
    pytest.param(
        'def load(name, package="onebot"):\n'
        '    return spec_from_file_location(f"t_{name}", ROOT / "adapters" / package / f"{name}.py")\n'
        'load("send_contract")\n'
        'load("reminder_commands", package="_shared")\n',
        [
            (2, "adapters/_shared/reminder_commands.py"),
            (2, "adapters/_shared/send_contract.py"),
            (2, "adapters/onebot/reminder_commands.py"),
            (2, "adapters/onebot/send_contract.py"),
        ],
        id="defaults_and_keywords",
    ),
    pytest.param(
        'class Loader:\n'
        '    def load(self, relpath):\n'
        '        return SourceFileLoader("copy", str(ROOT / relpath))\n'
        'Loader().load("adapters/onebot/emoji_handler.py")\n',
        [(3, "adapters/onebot/emoji_handler.py")],
        id="method_helper",
    ),
    pytest.param(
        'spec_from_file_location("x", location=str(ROOT) + "/adapters/onebot/config.py")\n',
        [(1, "adapters/onebot/config.py")],
        id="keyword_and_concatenation",
    ),
    pytest.param(
        'spec_from_file_location("channel", ROOT / "tools" / "_channel.py")\n'
        'from adapters.onebot import emoji_handler\n',
        [],
        id="other_directories_and_package_imports",
    ),
])
def test_the_scan_follows_how_tests_build_paths(source: str, expected: list[tuple[int, str]]) -> None:
    assert _adapter_loads(source) == expected
