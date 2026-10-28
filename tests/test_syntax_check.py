"""改完之后的检查：进程内按扩展名查语法；edit_checks.commands 里的外部命令和 execute_command 走同一套审批。

检查只报结论，不回滚；报出来的错误只有位置和原因，不带出错那行的原文。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config_layout  # noqa: E402
from toolbox import syntax_check  # noqa: E402
from toolbox.context import ToolContext  # noqa: E402
from toolbox.syntax_check import (  # noqa: E402
    CheckCommand,
    check_syntax,
    configured_commands,
    glob_matches,
    quote_path,
    run_edit_checks,
    run_external_checks,
)


class _Ctx(ToolContext):
    def __init__(self, workspace_root: Path) -> None:
        super().__init__(
            supervisor_url="http://unused",
            session_id="sess-1",
            run_id="run-1",
            worker_id="worker-1",
            workspace_root=workspace_root,
            http=None,  # type: ignore[arg-type]
            registry=None,
        )

    async def request_op(self, op: str, parameters: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("request_guard is mocked in these tests")

    async def check_cancelled(self) -> bool:
        return False


class _Guard:
    """顶替 request_guard：记下送审的参数，按预设放行或拒绝。"""

    def __init__(self, *, deny: bool = False, cancel: bool = False) -> None:
        self.deny = deny
        self.cancel = cancel
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, ctx: Any, op: str, parameters: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        self.calls.append((op, dict(parameters)))
        if self.cancel:
            return {}, {"ok": False, "error": "task cancelled", "cancelled": True}
        if self.deny:
            return {}, {"ok": False, "error": "command requires approval and was denied"}
        return {}, None


def _python_command(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    return tmp_path


def _use_commands(monkeypatch: pytest.MonkeyPatch, commands: list[dict[str, Any]]) -> None:
    monkeypatch.setattr(syntax_check, "load_runtime_config", lambda _root: {"edit_checks": {"commands": commands}})


class TestPython:
    def test_valid_code_passes(self) -> None:
        report = check_syntax(Path("a.py"), "def f():\n    return 1\n")

        assert report is not None and report.ok
        assert report.describe() == "语法检查通过（Python）"

    def test_a_syntax_error_reports_line_and_message(self) -> None:
        report = check_syntax(Path("a.py"), "x = 1\ndef f(:\n    pass\n")

        assert report is not None and not report.ok
        assert report.line == 2
        assert report.describe().startswith("语法检查未通过（Python）：第 2 行")

    def test_errors_only_compile_finds_are_reported(self) -> None:
        # ast.parse 放过的 return 不在函数里，compile 才报
        report = check_syntax(Path("a.py"), "return 1\n")

        assert report is not None and not report.ok
        assert report.line == 1

    def test_an_invalid_escape_is_a_warning_not_an_error(self) -> None:
        report = check_syntax(Path("a.py"), 'pattern = "\\d+"\n')

        assert report is not None and report.ok
        assert report.warnings
        assert "有警告" in report.describe()

    def test_a_leading_bom_and_crlf_are_fine(self) -> None:
        report = check_syntax(Path("a.py"), "\ufeffx = 1\r\ny = 2\r\n")

        assert report is not None and report.ok


class TestJson:
    def test_valid_json_passes(self) -> None:
        report = check_syntax(Path("data.json"), '{"a": [1, 2]}\n')

        assert report is not None and report.ok

    def test_a_trailing_comma_fails_with_its_position(self) -> None:
        report = check_syntax(Path("data.json"), '{\n  "a": 1,\n}\n')

        assert report is not None and not report.ok
        # 3.11 报到右括号那一行，较新的版本报到逗号那一行
        assert report.line in (2, 3)
        assert report.column is not None

    @pytest.mark.parametrize("name", ["tsconfig.json", "tsconfig.base.json", "jsconfig.json", ".vscode/settings.json"])
    def test_jsonc_files_are_not_checked_as_strict_json(self, name: str) -> None:
        assert check_syntax(Path(name), '{\n  // comment\n  "a": 1,\n}\n') is None


class TestYaml:
    def test_valid_yaml_passes(self) -> None:
        report = check_syntax(Path("conf.yaml"), "a: 1\nb: [x, y]\n")

        assert report is not None and report.ok

    def test_several_documents_are_fine(self) -> None:
        report = check_syntax(Path("conf.yml"), "a: 1\n---\nb: 2\n")

        assert report is not None and report.ok

    def test_a_broken_flow_list_fails_with_its_position(self) -> None:
        report = check_syntax(Path("conf.yaml"), "a: 1\nb: [1, 2\nc: 3\n")

        assert report is not None and not report.ok
        assert report.line is not None

    def test_the_message_does_not_quote_the_file(self) -> None:
        # 改的可能是不让读的文件：报错只给位置和原因，不把出错那一行原文带出去
        report = check_syntax(Path("conf.yaml"), "api_token_value_123: [unclosed\n")

        assert report is not None and not report.ok
        assert "api_token_value_123" not in report.describe()

    def test_the_context_keeps_its_own_position(self) -> None:
        report = check_syntax(Path("conf.yaml"), "a: 1\nb: [1, 2\nc: 3\n")

        assert report is not None and not report.ok
        assert (report.line, report.column) == (3, 2)
        assert report.message == "expected ',' or ']', but got ':'（while parsing a flow sequence，第 2 行第 4 列）"

    @pytest.mark.parametrize(
        "content,column,kind",
        [
            ("when: 2024-13-45\n", 7, "timestamp"),
            ("port: !!int sk-SECRET-port\n", 7, "int"),
            ("enabled: !!bool sk-SECRET-flag\n", 10, "bool"),
            ("ratio: !!float sk-SECRET-ratio\n", 8, "float"),
        ],
    )
    def test_a_value_that_cannot_be_built_fails_instead_of_raising(self, content: str, column: int, kind: str) -> None:
        # 检查跑在写盘之后：PyYAML 构造值失败抛的是 ValueError 一类，冒出去就成了一次没写上的写入
        report = check_syntax(Path("conf.yaml"), content)

        assert report is not None and not report.ok
        assert (report.line, report.column) == (1, column)
        assert report.message == f"值不是合法的 {kind}"
        assert "sk-SECRET" not in report.describe()

    @pytest.mark.parametrize("content", ["password: *sk-SECRET-alias\n", "key: !sk-SECRET-tag value\n"])
    def test_an_alias_or_a_tag_named_in_the_problem_is_not_quoted(self, content: str) -> None:
        report = check_syntax(Path("conf.yaml"), content)

        assert report is not None and not report.ok
        assert report.line == 1
        assert "sk-SECRET" not in report.describe()

    def test_a_broken_later_document_reports_its_own_line(self) -> None:
        report = check_syntax(Path("conf.yaml"), "a: 1\n---\nb: !!int sk-SECRET-second\n")

        assert report is not None and not report.ok
        assert (report.line, report.column) == (3, 4)
        assert "sk-SECRET" not in report.describe()

    def test_deep_nesting_fails_without_a_position(self) -> None:
        report = check_syntax(Path("conf.yaml"), "[" * 5000 + "]" * 5000)

        assert report is not None and not report.ok
        assert report.line is None
        assert report.describe() == "语法检查未通过（YAML）：嵌套太深"


class TestToml:
    def test_valid_toml_passes(self) -> None:
        report = check_syntax(Path("pyproject.toml"), '[tool]\nname = "x"\n')

        assert report is not None and report.ok

    def test_a_missing_value_fails_with_line_and_column(self) -> None:
        report = check_syntax(Path("pyproject.toml"), "a = 1\nb = \n")

        assert report is not None and not report.ok
        assert report.line == 2
        assert report.column is not None
        assert "(at line" not in report.message


class TestSkillFrontmatter:
    _SKILL = Path("ws") / "config" / "skills" / "demo" / "SKILL.md"

    def test_a_good_frontmatter_passes(self) -> None:
        report = check_syntax(self._SKILL, "---\nname: demo\nkeywords: [a, b]\n---\n\n正文\n")

        assert report is not None and report.ok
        assert report.describe() == "语法检查通过（SKILL.md）"

    def test_a_skill_without_frontmatter_passes(self) -> None:
        report = check_syntax(self._SKILL, "只有正文。\n")

        assert report is not None and report.ok

    def test_a_yaml_error_reports_the_line_in_the_file(self) -> None:
        report = check_syntax(self._SKILL, "---\nname: demo\nnote: sk-SECRET-skill: x\n---\n正文\n")

        assert report is not None and not report.ok
        assert (report.line, report.column) == (3, 22)
        assert report.message == "frontmatter 的 YAML 读不出来：mapping values are not allowed here"
        assert "sk-SECRET" not in report.describe()

    @pytest.mark.parametrize("content,line,message", [
        pytest.param("---\nname: demo\n正文\n", 1, "frontmatter 没有收尾，后面没有单独一行的 ---", id="unclosed"),
        pytest.param("---\n- demo\n---\n正文\n", None, "frontmatter 顶层不是 key: value 的映射", id="list"),
    ])
    def test_a_frontmatter_that_cannot_be_used_fails(self, content: str, line: int | None, message: str) -> None:
        report = check_syntax(self._SKILL, content)

        assert report is not None and not report.ok
        assert (report.line, report.message) == (line, message)

    def test_a_leading_bom_and_crlf_are_fine(self) -> None:
        report = check_syntax(self._SKILL, "\ufeff---\r\nname: demo\r\n---\r\n正文\r\n")

        assert report is not None and report.ok

    @pytest.mark.parametrize("path", [
        Path("docs") / "SKILL.md",
        Path("ws") / "config" / "skills" / "demo" / "notes.md",
        Path("ws") / "skills" / "demo" / "SKILL.md",
    ])
    def test_only_skill_files_under_config_skills_are_checked(self, path: Path) -> None:
        assert check_syntax(path, "---\nbroken: [\n") is None


class TestWhatGetsChecked:
    @pytest.mark.parametrize("name", ["notes.md", "script.sh", "Makefile", "data.csv"])
    def test_other_files_have_no_checker(self, name: str) -> None:
        assert check_syntax(Path(name), "anything goes {[(\n") is None

    def test_a_huge_file_is_skipped_not_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(syntax_check, "_MAX_CHECK_CHARS", 10)

        report = check_syntax(Path("a.json"), '{"a": "0123456789"}')

        assert report is not None and report.ok and report.skipped
        assert report.describe().startswith("语法检查跳过")


class TestTheConfiguredCommands:
    def test_the_shipped_default_is_an_empty_list(self) -> None:
        shipped = yaml.safe_load(config_layout.default_runtime_path().read_text(encoding="utf-8"))

        assert shipped["edit_checks"] == {"commands": []}
        assert configured_commands(shipped) == []

    def test_broken_entries_are_skipped(self) -> None:
        commands = configured_commands({"edit_checks": {"commands": [
            "ruff check {path}",
            {"glob": "*.py"},
            {"command": "ruff check {path}"},
            {"glob": "*.py", "command": "ruff check {path}", "timeout_sec": 12},
        ]}})

        assert commands == [CheckCommand("*.py", "ruff check {path}", 12.0)]

    @pytest.mark.parametrize(
        "raw,expected",
        [(None, 30.0), ("abc", 30.0), (True, 30.0), (0, 1.0), (99999, 300.0), (float("nan"), 30.0), ("45", 45.0)],
    )
    def test_the_timeout_is_clamped(self, raw: Any, expected: float) -> None:
        commands = configured_commands({"edit_checks": {"commands": [{"glob": "*", "command": "true", "timeout_sec": raw}]}})

        assert commands[0].timeout_sec == expected

    def test_a_non_list_is_ignored(self) -> None:
        assert configured_commands({"edit_checks": {"commands": "ruff check {path}"}}) == []
        assert configured_commands({}) == []


class TestGlobsAndQuoting:
    @pytest.mark.parametrize(
        "path,pattern,expected",
        [
            ("toolbox/a.py", "*.py", True),
            ("a.py", "**/*.py", True),
            ("toolbox/builtins/a.py", "**/*.py", True),
            ("toolbox/a.py", "toolbox/*.py", True),
            ("engine/a.py", "toolbox/*.py", False),
            ("a.PY", "*.py", False),
            ("a.yaml", "*.py", False),
        ],
    )
    def test_glob_matches_like_the_policy_rules(self, path: str, pattern: str, expected: bool) -> None:
        assert glob_matches(path, pattern) is expected

    def test_posix_quoting_neutralises_shell_syntax(self) -> None:
        assert quote_path("src/a b;rm -rf x.py", windows=False) == "'src/a b;rm -rf x.py'"

    def test_windows_quoting_wraps_in_double_quotes(self) -> None:
        assert quote_path("src/a b&c.py", windows=True) == '"src/a b&c.py"'

    @pytest.mark.parametrize("path", ["src/100%.py", "src/a!b.py", 'src/a"b.py', "src/a\nb.py"])
    def test_windows_refuses_what_cmd_would_still_expand(self, path: str) -> None:
        assert quote_path(path, windows=True) is None

    def test_the_command_path_is_workspace_relative(self, workspace: Path) -> None:
        resolved = (workspace / "src" / "app.py").resolve()

        assert syntax_check.command_path(workspace, resolved) == "src/app.py"


class TestRunningExternalChecks:
    def test_each_command_goes_through_the_execute_command_approval(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        guard = _Guard(deny=True)
        monkeypatch.setattr(syntax_check, "request_guard", guard)
        _use_commands(monkeypatch, [{"glob": "**/*.py", "command": "ruff check {path}"}])

        async def no_process(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("a denied check must not start a process")

        monkeypatch.setattr(syntax_check.asyncio, "create_subprocess_shell", no_process)
        target = workspace / "src" / "app.py"
        target.write_text("x = 1\n", encoding="utf-8")

        results = asyncio.run(run_external_checks(_Ctx(workspace), target.resolve(), tool_name="apply_diff"))

        assert [op for op, _params in guard.calls] == ["execute_command"]
        params = guard.calls[0][1]
        assert params["command"] == f"ruff check {quote_path('src/app.py')}"
        assert params["tool_name"] == "apply_diff"
        assert [result.status for result in results] == ["denied"]
        assert "外部检查没有执行" in results[0].describe()

    def test_a_glob_that_does_not_match_asks_nothing(self, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        guard = _Guard()
        monkeypatch.setattr(syntax_check, "request_guard", guard)
        _use_commands(monkeypatch, [{"glob": "*.ts", "command": "tsc {path}"}])
        target = workspace / "src" / "app.py"
        target.write_text("x = 1\n", encoding="utf-8")

        results = asyncio.run(run_external_checks(_Ctx(workspace), target.resolve(), tool_name="write_file"))

        assert results == []
        assert guard.calls == []

    def test_a_cancelled_approval_stops_the_remaining_checks(self, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        guard = _Guard(cancel=True)
        monkeypatch.setattr(syntax_check, "request_guard", guard)
        _use_commands(monkeypatch, [
            {"glob": "*.py", "command": "first {path}"},
            {"glob": "*.py", "command": "second {path}"},
        ])
        target = workspace / "src" / "app.py"
        target.write_text("x = 1\n", encoding="utf-8")

        results = asyncio.run(run_external_checks(_Ctx(workspace), target.resolve(), tool_name="write_file"))

        assert [result.status for result in results] == ["cancelled"]
        assert len(guard.calls) == 1

    def test_an_approved_command_runs_with_the_quoted_path(self, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        guard = _Guard()
        monkeypatch.setattr(syntax_check, "request_guard", guard)
        code = "import sys; print('checked', sys.argv[1]); sys.exit(0 if sys.argv[1] == 'src/app.py' else 7)"
        _use_commands(monkeypatch, [{"glob": "*.py", "command": _python_command(code) + " {path}", "timeout_sec": 60}])
        target = workspace / "src" / "app.py"
        target.write_text("x = 1\n", encoding="utf-8")

        results = asyncio.run(run_external_checks(_Ctx(workspace), target.resolve(), tool_name="write_file"))

        assert [result.status for result in results] == ["passed"], results
        assert "checked src/app.py" in results[0].output
        assert results[0].describe().startswith("外部检查通过：")

    def test_a_failing_command_reports_its_exit_code_and_output(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        guard = _Guard()
        monkeypatch.setattr(syntax_check, "request_guard", guard)
        code = "import sys; print('lint says no'); sys.exit(3)"
        _use_commands(monkeypatch, [{"glob": "*.py", "command": _python_command(code), "timeout_sec": 60}])
        target = workspace / "src" / "app.py"
        target.write_text("x = 1\n", encoding="utf-8")

        results = asyncio.run(run_external_checks(_Ctx(workspace), target.resolve(), tool_name="write_file"))

        assert results[0].status == "failed"
        assert results[0].returncode == 3
        assert "lint says no" in results[0].describe()
        assert "退出码 3" in results[0].describe()

    def test_the_commands_come_from_the_instance_runtime_yaml(self, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        guard = _Guard(deny=True)
        monkeypatch.setattr(syntax_check, "request_guard", guard)
        runtime = config_layout.runtime_path(workspace)
        runtime.parent.mkdir(parents=True, exist_ok=True)
        runtime.write_text(
            yaml.safe_dump({"edit_checks": {"commands": [{"glob": "*.py", "command": "mypy {path}"}]}}),
            encoding="utf-8",
        )
        target = workspace / "src" / "app.py"
        target.write_text("x = 1\n", encoding="utf-8")

        asyncio.run(run_external_checks(_Ctx(workspace), target.resolve(), tool_name="write_file"))

        assert [params["command"] for _op, params in guard.calls] == [f"mypy {quote_path('src/app.py')}"]


class TestTheCombinedReport:
    def test_syntax_and_command_results_are_reported_together(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(syntax_check, "request_guard", _Guard(deny=True))
        _use_commands(monkeypatch, [{"glob": "*.json", "command": "jq . {path}"}])
        target = workspace / "src" / "data.json"
        target.write_text("{broken", encoding="utf-8")

        checks = asyncio.run(run_edit_checks(_Ctx(workspace), target.resolve(), "{broken", tool_name="write_file"))

        assert checks.failed
        lines = checks.lines()
        assert lines[0].startswith("语法检查未通过（JSON）")
        assert lines[1].startswith("外部检查没有执行")
        data = checks.to_data()
        assert data["syntaxCheck"]["ok"] is False
        assert data["externalChecks"][0]["status"] == "denied"
