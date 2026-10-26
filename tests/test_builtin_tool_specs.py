"""内置工具的说明和实现对得上：glob 已登记、经注册表调得通；说明里点名的参数 schema 里都有，实现也真去读；
说明里的数字跟着常量走。
"""
from __future__ import annotations

import asyncio
import importlib
import re
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from toolbox import builtins as builtin_tools  # noqa: E402
from toolbox._common import OUTPUT_HEAD_SHARE  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402
from toolbox.ignore_rules import DEFAULT_IGNORED_DIRS, MAX_WALK_FILES  # noqa: E402
from toolbox.registry import ToolRegistry  # noqa: E402

# 包的 __init__ 把这几个名字换成了函数，模块得从 sys.modules 里拿
exec_module = importlib.import_module("toolbox.builtins.execute_command")
glob_module = importlib.import_module("toolbox.builtins.glob_files")
sif = importlib.import_module("toolbox.builtins.search_in_files")

_TOOLS = ("list_dir", "execute_command", "search_in_files", "glob")

_MENTIONED = {
    "list_dir": ("paths", "recursive", "include_ignored"),
    "execute_command": ("command", "timeout_sec", "max_output_chars"),
    "search_in_files": (
        "query", "path", "mode", "pattern", "isRegex", "includeIgnored", "maxResults", "replace", "maxFiles",
    ),
    "glob": ("pattern", "path", "max_results"),
}

# 说明里提到、schema 里不另列的写法，实现照样认
_ALIASES = {"search_in_files": {"include_ignored"}}

_CALLS = {
    "list_dir": {"path": "."},
    "execute_command": {"command": "echo hi"},
    "search_in_files": {"query": "needle"},
    "glob": {"pattern": "*.py"},
}


class _Ctx(ToolContext):
    """离线 ToolContext：每次送审都给同一个结论，记下送审的 op 和参数。"""

    def __init__(self, workspace_root: Path, *, decision: str = "auto") -> None:
        super().__init__(
            supervisor_url="http://unused",
            session_id="sess-specs",
            run_id="run-1",
            worker_id="worker-1",
            workspace_root=workspace_root,
            http=None,  # type: ignore[arg-type]
            registry=None,
            task_id="task-1",
        )
        self.decision = decision
        self.requested: list[tuple[str, dict[str, Any]]] = []

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.requested.append((op, dict(parameters)))
        return {"safety_level": self.decision, "approval_id": "ap-1", "reason": f"{op} by the test"}

    async def evaluate_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return {"safety_level": self.decision}

    async def wait_for_approval(self, approval_id: str, poll_interval: float | None = None) -> dict[str, Any]:
        return {"status": "denied"}

    async def check_cancelled(self) -> bool:
        return False


class _RecordingArgs(dict):
    """记下实现按哪些名字取过参数。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.seen: set[str] = set()

    def get(self, key: Any, default: Any = None) -> Any:
        self.seen.add(key)
        return super().get(key, default)

    def __getitem__(self, key: Any) -> Any:
        self.seen.add(key)
        return super().__getitem__(key)

    def __contains__(self, key: Any) -> bool:
        self.seen.add(key)
        return super().__contains__(key)


@pytest.fixture(scope="module")
def registry(tmp_path_factory: pytest.TempPathFactory) -> ToolRegistry:
    root = tmp_path_factory.mktemp("builtin-specs")
    return ToolRegistry(workspace_root=root, tools_dir=root / "tools", load_external=False)


def _spec(registry: ToolRegistry, name: str) -> dict[str, Any]:
    spec = registry.get_spec(name)
    assert spec is not None, name
    return spec


def _properties(spec: dict[str, Any]) -> dict[str, Any]:
    return spec["input_schema"]["properties"]


class TestTheGlobEntry:
    def test_glob_is_a_registered_builtin(self, registry: ToolRegistry) -> None:
        spec = _spec(registry, "glob")

        assert spec["input_schema"]["required"] == ["pattern"]
        assert set(_properties(spec)) == {"pattern", "path", "max_results"}
        assert registry._builtin_funcs["glob"] is builtin_tools.glob_files
        assert registry.reserved_name_reason("glob") == "taken by a builtin tool"

    def test_glob_runs_through_the_registry(self, tmp_path: Path) -> None:
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "a.py").write_bytes(b"x")
        registry = ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools", load_external=False)
        ctx = _Ctx(tmp_path)

        result = asyncio.run(registry.execute(name="glob", arguments={"pattern": "*.py"}, ctx=ctx))

        assert result["ok"] is True, result
        assert result["data"]["files"] == ["pkg/a.py"]
        assert ctx.requested == [("read_file", {"path": ".", "glob": "*.py"})]

    def test_the_descriptions_divide_the_work(self, registry: ToolRegistry) -> None:
        glob_text = _spec(registry, "glob")["description"]

        assert "search_in_files" in glob_text and "list_dir" in glob_text
        assert "use glob" in _spec(registry, "list_dir")["description"]


class TestParameters:
    @pytest.mark.parametrize("name", _TOOLS)
    def test_every_parameter_the_description_names_is_in_the_schema(self, registry: ToolRegistry, name: str) -> None:
        spec = _spec(registry, name)

        for parameter in _MENTIONED[name]:
            assert parameter in spec["description"], parameter
            assert parameter in _properties(spec), parameter

    @pytest.mark.parametrize("name", _TOOLS)
    def test_every_assignment_in_the_description_names_a_parameter(self, registry: ToolRegistry, name: str) -> None:
        spec = _spec(registry, name)
        # 引号里的是结果格式（'returncode=N'）和示例，不是参数
        unquoted = re.sub(r"'[^']*'", "", spec["description"])

        assigned = set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)=", unquoted))

        assert assigned <= set(_properties(spec)) | _ALIASES.get(name, set())

    @pytest.mark.parametrize("name", _TOOLS)
    def test_every_required_parameter_is_declared(self, registry: ToolRegistry, name: str) -> None:
        schema = _spec(registry, name)["input_schema"]

        assert set(schema.get("required") or []) <= set(schema["properties"])

    @pytest.mark.parametrize("name", _TOOLS)
    def test_the_implementation_reads_every_parameter_it_declares(
        self, registry: ToolRegistry, tmp_path: Path, name: str,
    ) -> None:
        args = _RecordingArgs(_CALLS[name])

        asyncio.run(registry._builtin_funcs[name](args, _Ctx(tmp_path, decision="deny")))

        expected = set(_properties(_spec(registry, name))) | _ALIASES.get(name, set())
        assert expected <= args.seen, expected - args.seen


class TestDescriptionsFollowTheCode:
    def test_list_dir_names_every_directory_it_leaves_closed(self, registry: ToolRegistry) -> None:
        description = _spec(registry, "list_dir")["description"]

        assert [name for name in sorted(DEFAULT_IGNORED_DIRS) if name not in description] == []

    def test_execute_command_states_its_capture_and_its_split(self, registry: ToolRegistry) -> None:
        spec = _spec(registry, "execute_command")
        head = round(OUTPUT_HEAD_SHARE * 100)

        assert (
            f"first {exec_module.CAPTURE_HEAD_BYTES // 1024}KB and last {exec_module.CAPTURE_TAIL_BYTES // 1024}KB"
            in spec["description"]
        )
        assert f"about {head}% from the start and {100 - head}% from the end" in spec["description"]
        budget = _properties(spec)["max_output_chars"]["description"]
        assert f"({exec_module.MIN_OUTPUT_CHARS}-{exec_module.MAX_OUTPUT_CHARS})" in budget

    def test_glob_states_its_limits(self, registry: ToolRegistry) -> None:
        description = _spec(registry, "glob")["description"]

        assert f"default {glob_module.DEFAULT_MAX_RESULTS}, at most {glob_module.MAX_RESULTS_LIMIT}" in description
        assert f"walking {MAX_WALK_FILES} files" in description

    def test_search_in_files_states_its_defaults_and_rules(self, registry: ToolRegistry) -> None:
        description = _spec(registry, "search_in_files")["description"]

        assert f"maxResults matches (default {sif._DEFAULT_MAX_RESULTS})" in description
        assert f"about {sif._MAX_TOTAL_RESULT_CHARS} characters" in description
        assert f"maxFiles files (default {sif._DEFAULT_MAX_FILES})" in description
        assert f"{sif.REPLACE_SWITCH}: false" in description
        assert "data/edit_journal/" in description
