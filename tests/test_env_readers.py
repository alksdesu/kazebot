"""直接从进程环境读的变量名，在 platforms/protection 里都得有着落。

manage_secret 和建共享组按 env_scope 判一个名字该写哪：直接读进程环境、又没登记的名字会被判成共享，
写进 config/secrets.env 谁也读不到，报的却是成功。新加一处 os.environ.get 不跟着登记，这里就会红。
"""
from __future__ import annotations

import ast
import importlib.machinery
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adapters.onebot import process_env as qq_process_env  # noqa: E402
from platforms import protection  # noqa: E402
from platforms.builtin import QQ_NAPCAT  # noqa: E402
from platforms.protection import ENV_SHARED, env_scope  # noqa: E402
from toolbox.redaction import SENSITIVE_KEY  # noqa: E402

# .claude/worktrees 下是整仓副本，扫进来会把副本里的代码也算一遍。
_SKIP_DIRS = {".venv", "venv", "__pycache__", "_research", "tests", "node_modules", ".git", ".claude"}
# 仓库根下这几个不是仓库的代码：config/ 和 data/ 归实例，examples/ 是拷进 config/plugins 才加载的模板。
_SKIP_TOP = {"config", "data", "examples"}
# 先查进程环境、再查 dotenv 两层（本实例 .env 盖在共享密钥文件上）的读法，这几个文件里直接读到的名字共享文件服务得了。
_LAYERED_TOOLS = ("tools/exa_search.py", "tools/web_search.py", "tools/x_search.py", "tools/drawtools/common.py")
_LAYERED = {"tools/_channel.py", *_LAYERED_TOOLS}
_READ_METHODS = {"get", "pop", "setdefault"}


def _is_os_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute) and node.attr == "environ"
        and isinstance(node.value, ast.Name) and node.value.id == "os"
    )


def _direct_reads(node: ast.AST) -> list[ast.AST]:
    """os.environ.get(x)、os.getenv(x)、os.environ[x]、x in os.environ 里那个 x。"""
    if isinstance(node, ast.Call) and node.args:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _READ_METHODS and _is_os_environ(func.value):
            return [node.args[0]]
        if (isinstance(func, ast.Attribute) and func.attr == "getenv"
                and isinstance(func.value, ast.Name) and func.value.id == "os"):
            return [node.args[0]]
    if isinstance(node, ast.Subscript) and _is_os_environ(node.value) and isinstance(node.ctx, ast.Load):
        return [node.slice]
    if (isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn))
            and _is_os_environ(node.comparators[0])):
        return [node.left]
    return []


@dataclass
class _Function:
    node: ast.FunctionDef | ast.AsyncFunctionDef
    is_method: bool
    # 哪几个位置参数会被拿去读进程环境，"*" 是变长参数。
    positions: set[int | str] = field(default_factory=set)

    @property
    def parameter_names(self) -> list[str]:
        arguments = self.node.args
        return [arg.arg for arg in arguments.posonlyargs + arguments.args]


@dataclass
class _Module:
    rel: str
    tree: ast.Module
    constants: dict[str, str]
    functions: dict[str, list[_Function]]
    imported: dict[str, tuple[Path, str]]
    module_aliases: dict[str, Path]


def _resolve(importer: Path, module: str | None, level: int) -> Path | None:
    """import 语句指的仓库文件。工具以子进程跑，裸写的 `from _channel import` 指的是同目录那份。"""
    parts = module.split(".") if module else []
    if level:
        base = importer.parent
        for _ in range(level - 1):
            base = base.parent
        bases = [base]
    else:
        bases = [_ROOT, importer.parent]
    for base in bases:
        candidate = base.joinpath(*parts)
        if candidate.with_suffix(".py").is_file():
            return candidate.with_suffix(".py")
        if (candidate / "__init__.py").is_file():
            return candidate / "__init__.py"
    return None


def _functions(tree: ast.Module) -> dict[str, list[_Function]]:
    found: dict[str, list[_Function]] = {}

    def visit(node: ast.AST, in_class: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                static = any(isinstance(d, ast.Name) and d.id == "staticmethod" for d in child.decorator_list)
                found.setdefault(child.name, []).append(_Function(child, in_class and not static))
                visit(child, False)
            elif isinstance(child, ast.ClassDef):
                visit(child, True)
            else:
                visit(child, in_class)

    visit(tree, False)
    return found


def _module(path: Path) -> _Module | None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return None
    constants = {
        statement.targets[0].id: statement.value.value
        for statement in tree.body
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
        and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str)
    }
    imported: dict[str, tuple[Path, str]] = {}
    aliases: dict[str, Path] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            target = _resolve(path, node.module, node.level)
            for alias in node.names:
                dotted = f"{node.module}.{alias.name}" if node.module else alias.name
                submodule = _resolve(path, dotted, node.level)
                if submodule is not None:
                    aliases[alias.asname or alias.name] = submodule
                elif target is not None:
                    imported[alias.asname or alias.name] = (target, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                target = _resolve(path, alias.name, 0)
                if target is not None and (alias.asname or "." not in alias.name):
                    aliases[alias.asname or alias.name] = target
    return _Module(path.relative_to(_ROOT).as_posix(), tree, constants, _functions(tree), imported, aliases)


def _callees(modules: dict[Path, _Module], module: _Module, call: ast.Call) -> list[tuple[_Function, int]]:
    """这一处调用可能落到的函数，连同实参比形参少的那几位（实例方法少一个 self）。"""
    func = call.func
    if isinstance(func, ast.Name):
        own = [fn for fn in module.functions.get(func.id, []) if not fn.is_method]
        if own:
            return [(fn, 0) for fn in own]
        if func.id in module.imported:
            target, name = module.imported[func.id]
            other = modules.get(target)
            return [(fn, 0) for fn in other.functions.get(name, []) if not fn.is_method] if other else []
        return []
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name) and func.value.id in module.module_aliases:
            other = modules.get(module.module_aliases[func.value.id])
            return [(fn, 0) for fn in other.functions.get(func.attr, []) if not fn.is_method] if other else []
        # 实例的类型推不出来，只认同一个文件里定义的方法
        return [(fn, 1) for fn in module.functions.get(func.attr, []) if fn.is_method]
    return []


def _passed(call: ast.Call, callee: _Function, offset: int) -> list[ast.AST]:
    """调用时塞进 callee 那几个读环境变量的形参的实参，按位置传的和按名字传的都算。"""
    args: list[ast.AST] = []
    for position in callee.positions:
        if position == "*":
            args.extend(call.args)
        elif isinstance(position, int) and position - offset >= 0:
            args.extend(call.args[position - offset:position - offset + 1])
    names = callee.parameter_names
    for keyword in call.keywords:
        if keyword.arg in names and names.index(keyword.arg) in callee.positions:
            args.append(keyword.value)
    return args


def _parameters(fn: _Function) -> dict[str, int | str]:
    """形参名到位置，遍历形参的循环变量算同一个位置。"""
    names: dict[str, int | str] = {name: index for index, name in enumerate(fn.parameter_names)}
    arguments = fn.node.args
    if arguments.vararg:
        names[arguments.vararg.arg] = "*"
    for node in ast.walk(fn.node):
        if (isinstance(node, ast.For) and isinstance(node.iter, ast.Name) and node.iter.id in names
                and isinstance(node.target, ast.Name)):
            names[node.target.id] = names[node.iter.id]
    return names


def _find_wrappers(modules: dict[Path, _Module]) -> None:
    """把形参拿去读进程环境的函数标出来，包了一层的也算，算到不再变为止。"""
    changed = True
    while changed:
        changed = False
        for module in modules.values():
            for functions in module.functions.values():
                for fn in functions:
                    parameters = _parameters(fn)
                    for node in ast.walk(fn.node):
                        reached = _direct_reads(node)
                        if isinstance(node, ast.Call):
                            for callee, offset in _callees(modules, module, node):
                                reached.extend(_passed(node, callee, offset))
                        for arg in reached:
                            if isinstance(arg, ast.Name) and arg.id in parameters:
                                position = parameters[arg.id]
                                if position not in fn.positions:
                                    fn.positions.add(position)
                                    changed = True


def _literal(module: _Module, node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return module.constants.get(node.id, "")
    return ""


@dataclass
class _Scan:
    modules: dict[Path, _Module]
    # 变量名 → 直接从进程环境读它的文件
    reads: dict[str, set[str]]
    # 源码里出现过的所有字符串常量
    strings: set[str]

    def wrapper(self, rel: str, name: str) -> set[int | str]:
        module = next(module for module in self.modules.values() if module.rel == rel)
        return set().union(*(fn.positions for fn in module.functions.get(name, [])))


@pytest.fixture(scope="module")
def scan() -> _Scan:
    modules: dict[Path, _Module] = {}
    for path in sorted(_ROOT.rglob("*.py")):
        parts = path.relative_to(_ROOT).parts
        if parts[0] in _SKIP_TOP or any(part in _SKIP_DIRS for part in parts):
            continue
        module = _module(path)
        if module is not None:
            modules[path] = module
    _find_wrappers(modules)

    reads: dict[str, set[str]] = {}
    strings: set[str] = set()
    for module in modules.values():
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                strings.add(node.value)
            reached = _direct_reads(node)
            if isinstance(node, ast.Call):
                for callee, offset in _callees(modules, module, node):
                    reached.extend(_passed(node, callee, offset))
            for arg in reached:
                name = _literal(module, arg)
                if name:
                    reads.setdefault(name, set()).add(module.rel)
    return _Scan(modules, reads, strings)


class TestTheScannerSeesTheKnownReaders:
    """扫描器自己退化了，下面的判定就会因为什么都没扫到而放行。"""

    @pytest.mark.parametrize("rel,name,positions", [
        ("adapters/_shared/env.py", "env_int", {0}),
        ("adapters/_shared/env.py", "env_float", {0}),
        ("adapters/_shared/env.py", "env_first", {"*"}),
        ("clonoth_runtime.py", "env_bool", {0}),
        ("supervisor/logging_setup.py", "_env_count", {0}),
        # 实例方法，位置算上了 self
        ("tools/_channel.py", "env", {1}),
        # 和 adapters/_shared/env.py 那个同名，读的是第二个参数
        ("tools/web_search.py", "env_float", {1}),
    ])
    def test_a_wrapper_is_recognised(self, scan: _Scan, rel: str, name: str, positions: set[int | str]) -> None:
        assert scan.wrapper(rel, name) == positions

    @pytest.mark.parametrize("name,rel", [
        ("CLONOTH_LOG_LEVEL", "supervisor/main.py"),
        ("ONEBOT_IMAGE_DOWNLOAD_TIMEOUT", "adapters/onebot/config.py"),
        ("CLONOTH_ENTRY_NODE", "adapters/onebot/config.py"),
        ("CLONOTH_SUPERVISOR_LOG_MAX_BYTES", "supervisor/logging_setup.py"),
        ("CLONOTH_ACCESS_LOG", "supervisor/main.py"),
        ("GEMINI_API_KEY", "tools/_channel.py"),
    ])
    def test_a_known_read_is_found(self, scan: _Scan, name: str, rel: str) -> None:
        assert rel in scan.reads.get(name, set())


class TestEveryProcessReadIsClassified:
    def test_nothing_read_straight_from_the_process_counts_as_shared(self, scan: _Scan) -> None:
        unclassified = sorted(
            f"{name}（{'、'.join(sorted(files - _LAYERED))}）"
            for name, files in scan.reads.items()
            if files - _LAYERED and env_scope(name).kind == ENV_SHARED
        )

        assert unclassified == [], (
            "这些名字直接从进程环境读，共享密钥文件服务不了：登记进 platforms/protection.py 的名单"
            "或平台的 process_env 钩子，值是路径、端口、令牌的归实例身份"
        )

    def test_the_shared_reads_only_happen_where_dotenv_is_consulted_too(self, scan: _Scan) -> None:
        readers = {
            file
            for name, files in scan.reads.items()
            if env_scope(name).kind == ENV_SHARED
            for file in files
        }

        assert readers <= _LAYERED

    @pytest.mark.parametrize("rel", _LAYERED_TOOLS)
    def test_a_layered_tool_reads_both_dotenv_layers(self, scan: _Scan, rel: str) -> None:
        module = scan.modules[_ROOT / rel]
        imported = {
            alias.name
            for node in ast.walk(module.tree)
            if isinstance(node, ast.ImportFrom) and node.module in {"_channel", "tools._channel"}
            for alias in node.names
        }

        assert "dotenv_values" in imported

    def test_the_channel_view_falls_through_to_the_shared_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from tools import _channel

        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "secrets.env").write_text("GEMINI_API_KEY=k-group\n", encoding="utf-8")

        assert _channel._ConfigView(root=tmp_path).env("GEMINI_API_KEY") == "k-group"


class TestEveryListedNameIsStillReal:
    """名单里留着没人读的名字，manage_secret 就会拒掉其实能共享的同名配置。"""

    @pytest.mark.parametrize("name", sorted(protection._CORE_PROCESS_SETTINGS | qq_process_env.STARTUP_ENV))
    def test_a_listed_setting_is_read_from_the_process(self, scan: _Scan, name: str) -> None:
        assert name in scan.reads

    @pytest.mark.parametrize("name", sorted(protection._CORE_INJECTED_ENV))
    def test_an_injected_name_is_still_used(self, scan: _Scan, name: str) -> None:
        assert name in scan.strings


class TestTheQqHook:
    def test_the_live_fallbacks_match_the_live_keys(self) -> None:
        from adapters.onebot.live_config import LIVE_KEYS

        assert qq_process_env.LIVE_ENV == {env for key in LIVE_KEYS for env in key.env}

    def test_judging_a_name_does_not_load_the_qq_adapter_config(self) -> None:
        # QQ 的 config.py 导入时就读平台密钥和 .env，supervisor、engine 里判一个名字不该把它拉进来
        code = (
            "import sys\n"
            "from platforms.protection import env_scope\n"
            "scope = env_scope('ONEBOT_GROUP_TRIGGER')\n"
            "print(scope.kind, scope.platform, 'adapters.onebot.config' in sys.modules)\n"
        )
        result = subprocess.run([sys.executable, "-c", code], cwd=_ROOT, capture_output=True, text=True)

        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == ["process", "qq", "False"]


def _nonebot_config_names() -> set[str]:
    """装着的 NoneBot 和 OneBot V11 适配器认的配置项，按环境变量的写法大写，别名也算。"""
    spec = importlib.machinery.PathFinder.find_spec("nonebot")
    if spec is None or not spec.submodule_search_locations:
        pytest.skip("没装 NoneBot")
    root = Path(next(iter(spec.submodule_search_locations)))
    names: set[str] = set()
    for rel, classes in (("config.py", {"Env", "Config"}), ("adapters/onebot/v11/config.py", {"Config"})):
        path = root / rel
        if not path.is_file():
            pytest.skip(f"没装 {rel} 所在的包")
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if not (isinstance(node, ast.ClassDef) and node.name in classes):
                continue
            for statement in node.body:
                if not (isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)):
                    continue
                if statement.target.id.startswith("_"):
                    continue
                names.add(statement.target.id.upper())
                if isinstance(statement.value, ast.Call):
                    names.update(
                        str(keyword.value.value).upper()
                        for keyword in statement.value.keywords
                        if keyword.arg == "alias" and isinstance(keyword.value, ast.Constant)
                    )
    return names


class TestTheBotHostSettings:
    def test_nothing_the_bot_host_reads_counts_as_shared(self) -> None:
        # NoneBot 只读进程环境和自己目录下的 .env，config/secrets.env 不在其中。
        shared = sorted(name for name in _nonebot_config_names() if env_scope(name).kind == ENV_SHARED)

        assert shared == []

    def test_the_host_list_only_names_real_settings(self) -> None:
        assert qq_process_env.HOST_ENV <= _nonebot_config_names()

    def test_the_declared_host_secrets_are_real_settings(self) -> None:
        assert set(QQ_NAPCAT.host_secret_env) <= _nonebot_config_names()

    def test_every_secret_the_bot_host_reads_is_stripped_from_tools(self) -> None:
        # NoneBot 自己从环境读的令牌、签名密钥：平台描述漏一个，工具子进程里 echo 一下就出来了。
        secrets = {name for name in _nonebot_config_names() if SENSITIVE_KEY.search(name)}

        assert secrets
        assert secrets <= protection.secret_env_names([QQ_NAPCAT])
