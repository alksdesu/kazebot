"""supervisor 启动时认哪份 .env。

不带路径的 load_dotenv() 从调用它的文件往上找，多开时找到的是代码目录那份，也就是主实例的；
配上 override=True，这个实例的端口、工作区和密钥就被换成主实例的。灌 .env 只能指名道姓地给路径。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_MAIN = _ROOT / "supervisor" / "main.py"
# .claude/worktrees 下是整仓副本，扫进来会把副本里的代码也算一遍。
_SKIP_DIRS = {".venv", "__pycache__", "_research", "tests", "node_modules", ".git", ".claude"}
# load_dotenv 会直接写 os.environ，所以谁灌、灌哪份必须一眼看得见。
# 只读不写的 dotenv_values(path) 不在此列，tools/ 下几个同名的纯文本解析函数也不是它。
_MAY_LOAD_DOTENV = {
    # 两个入口：bot.py 灌 cwd 或自己旁边那份，clonoth_runtime 灌工作区那份，都带路径。
    "bot.py",
    "clonoth_runtime.py",
}


def _python_files(*roots: Path) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        for path in (root.rglob("*.py") if root.is_dir() else [root]):
            if any(part in _SKIP_DIRS for part in path.relative_to(_ROOT).parts):
                continue
            files.append(path)
    return sorted(files)


def _rel(path: Path) -> str:
    return path.relative_to(_ROOT).as_posix()


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _load_dotenv_imports(tree: ast.AST) -> list[int]:
    """引进 python-dotenv 那个 load_dotenv 的行号；`import dotenv` 也算，它照样调得到。"""
    found: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "dotenv":
            found.extend(node.lineno for alias in node.names if alias.name in {"load_dotenv", "*"})
        elif isinstance(node, ast.Import):
            found.extend(node.lineno for alias in node.names if alias.name.split(".")[0] == "dotenv")
    return found


def _load_dotenv_calls(tree: ast.AST) -> list[ast.Call]:
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "load_dotenv":
            out.append(node)
    return out


def _calls_named(tree: ast.AST, dotted: str) -> list[int]:
    wanted = dotted.split(".")[-1]
    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == wanted:
            found.append(node.lineno)
    return found


@pytest.fixture(scope="module")
def parsed() -> list[tuple[Path, ast.Module]]:
    out: list[tuple[Path, ast.Module]] = []
    for path in _python_files(_ROOT):
        try:
            out.append((path, _parse(path)))
        except (SyntaxError, UnicodeDecodeError):
            continue
    return out


@pytest.fixture(scope="module")
def main_func() -> ast.FunctionDef:
    for node in _parse(_MAIN).body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return node
    raise AssertionError("supervisor/main.py 里没有 main()")


class TestNobodyLoadsEnvBehindTheWorkspace:
    def test_the_long_running_processes_do_not_load_env_files(self) -> None:
        # supervisor / engine 拿到的工作区由 clonoth_runtime 定，自己再灌一遍就是另一份文件。
        hits: list[str] = []
        for path in _python_files(_ROOT / "supervisor", _ROOT / "engine"):
            tree = _parse(path)
            hits.extend(f"{_rel(path)}:{lineno}" for lineno in _load_dotenv_imports(tree))
            hits.extend(f"{_rel(path)}:{call.lineno}" for call in _load_dotenv_calls(tree))

        assert hits == [], f"灌 .env 走 clonoth_runtime.load_workspace_env，别自己调 load_dotenv: {hits}"

    def test_only_the_two_entry_points_may_load_env_files(self, parsed) -> None:
        hits = [
            f"{_rel(path)}:{lineno}"
            for path, tree in parsed
            if _rel(path) not in _MAY_LOAD_DOTENV
            for lineno in _load_dotenv_imports(tree)
        ]

        assert hits == [], f"新增的 load_dotenv 入口要先在本文件登记理由: {hits}"

    def test_every_load_names_the_file_it_loads(self, parsed) -> None:
        # 不给路径就让 python-dotenv 自己往上找，找到的是代码目录那份——多开时是主实例的。
        hits = [
            f"{_rel(path)}:{call.lineno}"
            for path, tree in parsed
            if _load_dotenv_imports(tree)
            for call in _load_dotenv_calls(tree)
            if not call.args and not any(kw.arg == "dotenv_path" for kw in call.keywords)
        ]

        assert hits == [], f"load_dotenv() 必须指名 .env 路径: {hits}"


class TestSupervisorMainSettlesTheWorkspaceFirst:
    def test_it_loads_the_env_before_reading_the_argument_defaults(self, main_func: ast.FunctionDef) -> None:
        # --port 的默认值就是 os.getenv("CLONOTH_PORT")：晚一步灌，这个实例就开在别人的端口上。
        settled = _calls_named(main_func, "process_workspace")
        parsers = _calls_named(main_func, "argparse.ArgumentParser")

        assert settled, "main() 没有调用 process_workspace()"
        assert parsers, "main() 没有建 ArgumentParser，这条守卫失效了"
        assert min(settled) < min(parsers)

    def test_it_uses_the_workspace_that_call_settled(self, main_func: ast.FunctionDef) -> None:
        bound = [
            target.id
            for node in ast.walk(main_func)
            if isinstance(node, ast.Assign) and _calls_named(node.value, "process_workspace")
            for target in node.targets
            if isinstance(target, ast.Name)
        ]

        assert len(bound) == 1, f"process_workspace() 的返回值要接住，不能只图它灌 .env: {bound}"
        used = [
            node.lineno
            for node in ast.walk(main_func)
            if isinstance(node, ast.Name) and node.id == bound[0] and isinstance(node.ctx, ast.Load)
        ]
        assert used, f"{bound[0]} 接了却没人用"

    def test_it_does_not_work_the_workspace_root_out_a_second_time(self) -> None:
        # 两处各算一遍就会各算各的：灌的是这份 .env，data/ 却落在另一个目录。
        hits = _calls_named(_parse(_MAIN), "resolve_workspace_root")

        assert hits == [], f"工作区根以 process_workspace() 的返回值为准: {hits}"
