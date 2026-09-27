"""runtime.yaml 的声明与代码读取必须对得上。

声明了却没有任何读取方的键最有迷惑性：`min_messages: 4` 读起来像「少于 4 条消息就不
提取」的安全阀，实际什么也不做，调参的人会一直调一个不存在的旋钮。反向的漏声明同样坑：
被读的 idle_delay_sec 不在配置文件里，想改正常路径的延迟就无从下手。退役键则要确保
既不在配置里复活，也不在代码和文档里留下还能用的错觉。
"""
from __future__ import annotations

import ast
import io
import logging
import os
import sys
import tokenize
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import clonoth_runtime  # noqa: E402
from clonoth_runtime import (  # noqa: E402
    DEFAULT_RUNTIME_CONFIG,
    RETIRED_RUNTIME_KEYS,
    load_runtime_config,
    retired_runtime_keys,
)
from providers import registry as provider_registry  # noqa: E402

# .claude/worktrees 下是整仓副本，扫进来会把副本里的代码也算一遍。
_SKIP_DIRS = {".venv", "__pycache__", "_research", "tests", "node_modules", ".git", ".claude"}

# 这两张表把每个键名都写成了字面量，算进语料等于让死键自证存在。
_SELF_REFERENTIAL_TABLES = {"DEFAULT_RUNTIME_CONFIG", "RETIRED_RUNTIME_KEYS"}

# 文件格式标记，不是可调项。
_SCHEMA_KEYS = {"version"}

_GETTERS = {"get_str", "get_int", "get_float", "get_bool"}

_RETIRED_DOC_HEADING = "### 已退役的键"


def _code_files() -> list[Path]:
    out: list[Path] = []
    for path in _ROOT.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.relative_to(_ROOT).parts):
            continue
        out.append(path)
    return out


def _strip_comments(text: str) -> str:
    """注释里提一嘴某个键不等于有代码读它，按行截掉注释；行号保持不变。"""
    lines = text.splitlines(keepends=True)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return text
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        row, col = token.start
        line = lines[row - 1]
        lines[row - 1] = line[:col] + line[len(line.rstrip("\r\n")):]
    return "".join(lines)


def _blank_assignments(text: str, names: set[str]) -> str:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text
    lines = text.splitlines(keepends=True)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id in names for t in targets):
            continue
        for index in range(node.lineno - 1, (node.end_lineno or node.lineno)):
            lines[index] = "\n"
    return "".join(lines)


def _quoted(token: str) -> tuple[str, str]:
    return (f'"{token}"', f"'{token}'")


def _leaf_paths(node: dict, prefix: str = "") -> list[str]:
    out: list[str] = []
    for key, value in (node or {}).items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.extend(_leaf_paths(value, dotted))
        else:
            out.append(dotted)
    return out


def _dotted_value(cfg: Any, dotted: str) -> tuple[Any, bool]:
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None, False
        node = node[part]
    return node, True


def _has_dotted(cfg: Any, dotted: str) -> bool:
    return _dotted_value(cfg, dotted)[1]


def _provider_option_leaf(dotted: str) -> tuple[str, str] | None:
    """providers.<格式族>.options.<参数名> 拆成 (格式族, 参数名)，不是这个形状返回 None。"""
    parts = dotted.split(".")
    if len(parts) == 4 and parts[0] == "providers" and parts[2] == "options":
        return parts[1], parts[3]
    return None


def _reader_files(dotted: str, sources: dict[str, str]) -> list[str]:
    """能算作读取方的文件。

    整条 dotted key 出现在某个文件里最硬；此外 cfg.get("engine", {}).get("signals") 这类
    逐段取字典的读法同样普遍，判据是叶子名和它上面每一段都以字面量出现在同一个文件里。
    """
    full = _quoted(dotted)
    segments = dotted.split(".")
    leaf = _quoted(segments[-1])
    parents = [_quoted(seg) for seg in segments[:-1]]

    hits: list[str] = []
    for rel, text in sources.items():
        if any(token in text for token in full):
            hits.append(rel)
            continue
        if not any(token in text for token in leaf):
            continue
        if all(any(token in text for token in parent) for parent in parents):
            hits.append(rel)
    return hits


@pytest.fixture(scope="module")
def config() -> dict:
    return yaml.safe_load((_ROOT / "config" / "runtime.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def sources() -> dict[str, str]:
    out: dict[str, str] = {}
    for path in _code_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = path.relative_to(_ROOT).as_posix()
        text = _strip_comments(text)
        if rel == "clonoth_runtime.py":
            text = _blank_assignments(text, _SELF_REFERENTIAL_TABLES)
        out[rel] = text
    return out


def test_every_declared_key_has_a_reader(config: dict, sources: dict[str, str]) -> None:
    dead: list[str] = []
    for dotted in _leaf_paths(config):
        if dotted in _SCHEMA_KEYS or _provider_option_leaf(dotted) is not None:
            continue
        if not _reader_files(dotted, sources):
            dead.append(dotted)

    assert dead == [], f"声明了但没有任何读取方: {dead}"


def test_declared_provider_options_exist_in_the_provider_catalog(config: dict) -> None:
    """provider 的可调参数名由 provider 类的 OPTIONS 声明，配置里写别的名字会被静默丢掉。"""
    catalog = provider_registry.options_catalog()
    unknown: list[str] = []
    for dotted in _leaf_paths(config):
        parsed = _provider_option_leaf(dotted)
        if parsed is None:
            continue
        family, option = parsed
        declared = {str(spec.get("key") or "") for spec in catalog.get(family, [])}
        if family not in catalog or option not in declared:
            unknown.append(dotted)

    assert unknown == [], f"provider 不认这些参数名: {unknown}"


def test_keys_read_by_code_are_declared(config: dict) -> None:
    """get_str/get_int/get_float/get_bool 按 dotted path 读的键必须能在配置文件里找到。"""
    missing: dict[str, str] = {}
    for path in _code_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or len(node.args) < 2:
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in _GETTERS:
                continue
            key = node.args[1]
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                continue
            if "." not in key.value or _has_dotted(config, key.value):
                continue
            missing.setdefault(key.value, f"{path.relative_to(_ROOT).as_posix()}:{node.lineno}")

    assert missing == {}, f"代码读了但 runtime.yaml 没声明: {missing}"


def _literal_default(path: Path, dotted: str) -> Any:
    """源码里 get_*(cfg, dotted, <字面量>) 的那个兜底值。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 3:
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        key = node.args[1]
        if name not in _GETTERS or not isinstance(key, ast.Constant) or key.value != dotted:
            continue
        if isinstance(node.args[2], ast.Constant):
            return node.args[2].value
    raise AssertionError(f"{path.name} 里没有读 {dotted} 的字面量兜底值")


@pytest.mark.parametrize("module_path, dotted", [
    ("toolbox/builtins/system.py", "meta.git.diff_max_chars"),
    ("toolbox/builtins/execute_command.py", "meta.execute_command.default_timeout_sec"),
    ("toolbox/builtins/execute_command.py", "meta.execute_command.max_output_chars"),
    ("toolbox/builtins/search_in_files.py", "meta.search.max_file_size_bytes"),
])
def test_call_site_fallbacks_match_the_shipped_default(module_path: str, dotted: str) -> None:
    """兜底值永远轮不到生效（读出来的 cfg 一定并过默认表），但对不上会让看调用点的人读到假数。"""
    fallback = _literal_default(_ROOT / module_path, dotted)
    declared, found = _dotted_value(DEFAULT_RUNTIME_CONFIG, dotted)

    assert found, f"{dotted} 不在 DEFAULT_RUNTIME_CONFIG 里"
    assert fallback == declared


@pytest.mark.parametrize("dotted", sorted(RETIRED_RUNTIME_KEYS))
def test_retired_keys_stay_retired(dotted: str, config: dict, sources: dict[str, str]) -> None:
    """退役键不许在配置里复活，也不许在代码里留下还有人读的样子。"""
    assert not _has_dotted(config, dotted), f"{dotted} 又被声明进 config/runtime.yaml 了"
    assert not _has_dotted(DEFAULT_RUNTIME_CONFIG, dotted), f"{dotted} 又回到 DEFAULT_RUNTIME_CONFIG 了"

    readers = [
        rel for rel, text in sources.items()
        if any(token in text for token in _quoted(dotted))
    ]
    assert readers == [], f"{dotted} 仍以字面量出现在 {readers}"


def test_the_docs_mention_retired_keys_only_in_the_retired_section() -> None:
    path = _ROOT / "docs" / "configuration.md"
    if not path.exists():
        pytest.skip("docs/ 不进公开仓库，本地没有这份说明")

    lines = path.read_text(encoding="utf-8").splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == _RETIRED_DOC_HEADING), None)
    assert start is not None, f"docs/configuration.md 缺少「{_RETIRED_DOC_HEADING}」一节"
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )

    outside = [(i + 1, line) for i, line in enumerate(lines) if not start <= i < end]
    leaked: list[str] = []
    for dotted in RETIRED_RUNTIME_KEYS:
        for lineno, line in outside:
            if dotted in line:
                leaked.append(f"{dotted} @ line {lineno}")
    assert leaked == [], f"退役键出现在「{_RETIRED_DOC_HEADING}」之外: {leaked}"


@pytest.fixture()
def fresh_runtime_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clonoth_runtime, "_CACHE", {})
    monkeypatch.setattr(clonoth_runtime, "_WARNED_RETIRED", set())


def _write_runtime_yaml(workspace: Path, body: str) -> Path:
    path = workspace / "config" / "runtime.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _retired_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "clonoth_runtime" and record.levelno >= logging.WARNING
    ]


def test_retired_keys_warn_once_per_file_and_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, fresh_runtime_cache: None,
) -> None:
    workspace = tmp_path / "ws"
    path = _write_runtime_yaml(
        workspace,
        "engine:\n  history_limit: 80\ntools:\n  command:\n    default_timeout_sec: 60\n",
    )
    caplog.set_level(logging.WARNING)

    load_runtime_config(workspace)
    first = _retired_warnings(caplog)
    assert len(first) == 2, first
    assert any("engine.history_limit" in message for message in first)
    assert any("tools.command" in message for message in first)
    assert all(str(path) in message for message in first)

    caplog.clear()
    load_runtime_config(workspace)
    assert _retired_warnings(caplog) == []

    # 缓存按 mtime 失效，重新读文件也不该把同一句话再说一遍
    stat = path.stat()
    os.utime(path, (stat.st_atime + 10, stat.st_mtime + 10))
    caplog.clear()
    load_runtime_config(workspace)
    assert _retired_warnings(caplog) == []


def test_retired_key_warning_is_per_workspace(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, fresh_runtime_cache: None,
) -> None:
    body = "engine:\n  history_limit: 80\n"
    first_ws = tmp_path / "one"
    second_ws = tmp_path / "two"
    _write_runtime_yaml(first_ws, body)
    _write_runtime_yaml(second_ws, body)
    caplog.set_level(logging.WARNING)

    load_runtime_config(first_ws)
    assert len(_retired_warnings(caplog)) == 1

    caplog.clear()
    load_runtime_config(second_ws)
    assert len(_retired_warnings(caplog)) == 1


def test_a_clean_runtime_yaml_warns_about_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, fresh_runtime_cache: None,
) -> None:
    _write_runtime_yaml(tmp_path, "engine:\n  max_steps: 8\ntools:\n  script:\n    default_timeout_sec: 60\n")
    caplog.set_level(logging.WARNING)

    load_runtime_config(tmp_path)

    assert _retired_warnings(caplog) == []


def test_retired_runtime_keys_reports_present_keys_in_table_order() -> None:
    cfg = {
        "tools": {"command": {"default_timeout_sec": 60}},
        "memory": {"dream": {"min_sessions": 3}},
        "engine": {"history_limit": 80, "max_steps": 8},
    }

    assert retired_runtime_keys(cfg) == [
        "engine.history_limit",
        "memory.dream.min_sessions",
        "tools.command",
    ]


def test_retired_subtree_key_matches_any_child() -> None:
    cfg = {"engine": {"supervisor": {"health_timeout_sec": 2.0}}}

    assert retired_runtime_keys(cfg) == ["engine.supervisor"]


def test_shipped_runtime_yaml_declares_no_retired_keys() -> None:
    cfg = yaml.safe_load((_ROOT / "config" / "runtime.yaml").read_text(encoding="utf-8"))

    assert retired_runtime_keys(cfg) == []
