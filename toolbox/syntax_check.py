"""改完之后的检查：按扩展名在进程内查语法，再按 runtime 的 edit_checks.commands 跑外部检查命令。

检查不通过不回滚，结论附在工具结果末尾。外部命令和 execute_command 走同一套策略审批。"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import math
import os
import re
import shlex
import tomllib
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

import config_layout
from clonoth_runtime import load_runtime_config

from ._common import request_guard, run_process, safe_subprocess_env
from .text_files import BOM, normalize_newlines

logger = logging.getLogger(__name__)

_LANGUAGES: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".json": "json",
    ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml",
}
_LANGUAGE_LABELS: dict[str, str] = {"python": "Python", "json": "JSON", "yaml": "YAML", "toml": "TOML"}
# 这些 .json 其实是 JSONC，允许注释和尾逗号，按严格 JSON 查只会误报
_JSONC_NAMES = ("tsconfig*.json", "jsconfig*.json", "devcontainer.json", ".devcontainer.json", ".eslintrc.json")
_MAX_CHECK_CHARS = 4_000_000
_MAX_WARNINGS = 3
_TOML_POSITION = re.compile(r"\s*\(at line (\d+), column (\d+)\)\s*$")
_TOML_END = re.compile(r"\s*\(at end of document\)\s*$")

_DEFAULT_COMMAND_TIMEOUT_SEC = 30.0
_MAX_COMMAND_TIMEOUT_SEC = 300.0
_MIN_COMMAND_TIMEOUT_SEC = 1.0
_COMMAND_OUTPUT_CHARS = 4000
# UTF-8 一个字符最多 4 字节：每路输出留够截到 _COMMAND_OUTPUT_CHARS 的量，多出来的边读边扔
_BYTES_PER_CHAR = 4
_MAX_COMMANDS_PER_EDIT = 8
# cmd.exe 在双引号里照样展开 %VAR% 和 !VAR!，这几个字符没法安全转义
_UNQUOTABLE_ON_WINDOWS = frozenset('"%!')

_WARNED_CONFIG: set[str] = set()


@dataclass(frozen=True)
class SyntaxReport:
    """skipped 非空表示没查（文件太大）。message 只有出错原因和位置，不带出错那行的原文。"""

    language: str
    ok: bool
    line: int | None = None
    column: int | None = None
    message: str = ""
    skipped: str = ""
    warnings: tuple[str, ...] = ()

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"language": self.language, "ok": self.ok}
        if self.skipped:
            data["skipped"] = self.skipped
        if not self.ok:
            data.update({"line": self.line, "column": self.column, "message": self.message})
        if self.warnings:
            data["warnings"] = list(self.warnings)
        return data

    def describe(self) -> str:
        label = _LANGUAGE_LABELS.get(self.language, self.language)
        if self.skipped:
            return f"语法检查跳过（{label}）：{self.skipped}"
        if self.ok:
            text = f"语法检查通过（{label}）"
            return f"{text}，有警告：{'；'.join(self.warnings)}" if self.warnings else text
        return f"语法检查未通过（{label}）：{_position(self.line, self.column)}{self.message}"


def _position(line: int | None, column: int | None) -> str:
    if line is None:
        return ""
    return f"第 {line} 行第 {column} 列：" if column else f"第 {line} 行："


def _is_jsonc(path: Path) -> bool:
    return ".vscode" in path.parts or any(fnmatch.fnmatchcase(path.name, name) for name in _JSONC_NAMES)


def language_of(path: Path) -> str | None:
    language = _LANGUAGES.get(path.suffix.lower())
    if language == "json" and _is_jsonc(path):
        return None
    return language


def _check_python(path: Path, source: str) -> SyntaxReport:
    try:
        # SyntaxWarning（如字符串里无效的转义）记下来报给模型，不打到进程的 stderr
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            compile(source, str(path), "exec", dont_inherit=True)
    except SyntaxError as exc:
        return SyntaxReport("python", False, exc.lineno, exc.offset, exc.msg or type(exc).__name__)
    except (ValueError, RecursionError, MemoryError, OverflowError) as exc:
        return SyntaxReport("python", False, message=f"{type(exc).__name__}: {exc}")
    notes = tuple(
        f"{_position(item.lineno, None)}{item.message}"
        for item in caught
        if issubclass(item.category, (SyntaxWarning, DeprecationWarning))
    )[:_MAX_WARNINGS]
    return SyntaxReport("python", True, warnings=notes)


def _check_json(path: Path, source: str) -> SyntaxReport:
    try:
        json.loads(source)
    except json.JSONDecodeError as exc:
        return SyntaxReport("json", False, exc.lineno, exc.colno, exc.msg)
    except RecursionError:
        return SyntaxReport("json", False, message="嵌套太深")
    return SyntaxReport("json", True)


def _check_yaml(path: Path, source: str) -> SyntaxReport:
    try:
        # 多文档的 yaml（--- 分隔）用 safe_load 会报「expected a single document」
        for _document in yaml.safe_load_all(source):
            pass
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        message = "; ".join(part for part in (exc.context, exc.problem) if part) or type(exc).__name__
        if mark is None:
            return SyntaxReport("yaml", False, message=message)
        return SyntaxReport("yaml", False, mark.line + 1, mark.column + 1, message)
    except yaml.YAMLError as exc:
        return SyntaxReport("yaml", False, message=type(exc).__name__)
    except RecursionError:
        return SyntaxReport("yaml", False, message="嵌套太深")
    return SyntaxReport("yaml", True)


def _check_toml(path: Path, source: str) -> SyntaxReport:
    try:
        tomllib.loads(source)
    except tomllib.TOMLDecodeError as exc:
        message = str(exc)
        found = _TOML_POSITION.search(message)
        if found is not None:
            return SyntaxReport("toml", False, int(found.group(1)), int(found.group(2)), message[: found.start()])
        return SyntaxReport("toml", False, message=_TOML_END.sub("（在文件末尾）", message))
    except RecursionError:
        return SyntaxReport("toml", False, message="嵌套太深")
    return SyntaxReport("toml", True)


_CHECKERS = {"python": _check_python, "json": _check_json, "yaml": _check_yaml, "toml": _check_toml}


def check_syntax(path: Path, text: str) -> SyntaxReport | None:
    """按扩展名查 text 的语法；没有对应检查器的返回 None。"""
    language = language_of(path)
    if language is None:
        return None
    if len(text) > _MAX_CHECK_CHARS:
        return SyntaxReport(language, True, skipped=f"文件超过 {_MAX_CHECK_CHARS} 字符")
    source = normalize_newlines(text[len(BOM):] if text.startswith(BOM) else text)
    return _CHECKERS[language](path, source)


@dataclass(frozen=True)
class CheckCommand:
    glob: str
    command: str
    timeout_sec: float


@dataclass(frozen=True)
class CommandResult:
    """status：passed / failed / timeout / denied / cancelled / error。"""

    glob: str
    command: str
    status: str
    returncode: int | None = None
    output: str = ""
    truncated: bool = False
    reason: str = ""
    timeout_sec: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "passed"

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"glob": self.glob, "command": self.command, "status": self.status, "ok": self.ok}
        if self.returncode is not None:
            data["returncode"] = self.returncode
        if self.output:
            data["output"] = self.output
        if self.truncated:
            data["truncated"] = True
        if self.reason:
            data["reason"] = self.reason
        return data

    def describe(self) -> str:
        if self.status == "passed":
            return f"外部检查通过：{self.command}"
        if self.status == "denied":
            return f"外部检查没有执行：{self.command}（{self.reason}）"
        if self.status == "cancelled":
            return f"外部检查被取消：{self.command}"
        if self.status == "error":
            return f"外部检查没能运行：{self.command}（{self.reason}）"
        head = (
            f"外部检查超时：{self.command}（超过 {self.timeout_sec:g} 秒）"
            if self.status == "timeout"
            else f"外部检查未通过：{self.command}（退出码 {self.returncode}）"
        )
        body = self.output.rstrip()
        tail = "\n…（输出过长，已截断）" if self.truncated else ""
        return f"{head}\n{body}{tail}" if body else f"{head}{tail}"


def _warn_once(message: str) -> None:
    if message in _WARNED_CONFIG:
        return
    _WARNED_CONFIG.add(message)
    logger.warning("%s", message)


def _timeout_of(raw: Any) -> float:
    if isinstance(raw, bool):
        return _DEFAULT_COMMAND_TIMEOUT_SEC
    try:
        value = float(raw) if raw is not None else _DEFAULT_COMMAND_TIMEOUT_SEC
    except (TypeError, ValueError):
        value = _DEFAULT_COMMAND_TIMEOUT_SEC
    if math.isnan(value):
        value = _DEFAULT_COMMAND_TIMEOUT_SEC
    return min(_MAX_COMMAND_TIMEOUT_SEC, max(_MIN_COMMAND_TIMEOUT_SEC, value))


def configured_commands(runtime_cfg: dict[str, Any]) -> list[CheckCommand]:
    """runtime 的 edit_checks.commands；写坏的条目跳过并记一次日志。"""
    section = runtime_cfg.get("edit_checks")
    raw = section.get("commands") if isinstance(section, dict) else None
    if raw is None:
        return []
    if not isinstance(raw, list):
        _warn_once("runtime 的 edit_checks.commands 应当是列表，已忽略")
        return []
    commands: list[CheckCommand] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            _warn_once(f"runtime 的 edit_checks.commands[{index}] 应当是 {{glob, command, timeout_sec}} 映射，已跳过")
            continue
        glob = str(item.get("glob") or "").strip()
        command = str(item.get("command") or "").strip()
        if not glob or not command:
            _warn_once(f"runtime 的 edit_checks.commands[{index}] 缺 glob 或 command，已跳过")
            continue
        commands.append(CheckCommand(glob, command, _timeout_of(item.get("timeout_sec"))))
    return commands


def glob_matches(rel_path: str, pattern: str) -> bool:
    """和 policy 规则一样区分大小写；不带 / 的模式也比文件名，**/ 开头的也认工作区根下的文件。"""
    pattern = pattern.replace("\\", "/").strip()
    if fnmatch.fnmatchcase(rel_path, pattern):
        return True
    if "/" not in pattern:
        return fnmatch.fnmatchcase(rel_path.rsplit("/", 1)[-1], pattern)
    return pattern.startswith("**/") and fnmatch.fnmatchcase(rel_path, pattern[3:])


def command_path(workspace_root: Path, resolved: Path) -> str:
    """{path} 换成的路径：工作区里的写相对路径（命令在工作区根下跑），之外的写绝对路径；一律用 /。

    策略按命令文本里出现的路径判敏感路径，写成这个样子它才认得出 config/nodes/ 之类的前缀。
    """
    logical = config_layout.workspace_logical_path(workspace_root, resolved)
    return logical if logical is not None else resolved.as_posix()


def quote_path(path: str, *, windows: bool | None = None) -> str | None:
    """按当前平台的 shell 转义；Windows 上有 cmd.exe 没法安全转义的字符时返回 None。"""
    if windows if windows is not None else os.name == "nt":
        if any(char in _UNQUOTABLE_ON_WINDOWS or ord(char) < 32 for char in path):
            return None
        return f'"{path}"'
    return shlex.quote(path)


async def _run_command(ctx: Any, spec: CheckCommand, rendered: str, *, tool_name: str, display_path: str) -> CommandResult:
    _op, err = await request_guard(
        ctx,
        "execute_command",
        {"command": rendered, "tool_name": tool_name, "reason": f"{tool_name} 改完 {display_path} 后跑的检查命令"},
    )
    if err is not None:
        if err.get("cancelled"):
            return CommandResult(spec.glob, rendered, "cancelled", reason="task cancelled")
        return CommandResult(spec.glob, rendered, "denied", reason=str(err.get("error") or "denied"))
    keep = _COMMAND_OUTPUT_CHARS * _BYTES_PER_CHAR
    try:
        proc = await asyncio.create_subprocess_shell(
            rendered,
            cwd=str(ctx.workspace_root),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=safe_subprocess_env(),
            # 独立进程组：超时或取消时整棵进程树一起杀
            start_new_session=True,
        )
        outcome = await run_process(
            proc, timeout_sec=spec.timeout_sec, keep_stdout=keep, keep_stderr=keep, is_cancelled=ctx.check_cancelled,
        )
    except Exception as exc:
        return CommandResult(spec.glob, rendered, "error", reason=f"{type(exc).__name__}: {exc}")
    if outcome.cancelled:
        return CommandResult(spec.glob, rendered, "cancelled", reason="task cancelled")
    stdout = outcome.stdout.decode("utf-8", errors="replace")
    stderr = outcome.stderr.decode("utf-8", errors="replace")
    output = stdout + ("\n" + stderr if stderr else "")
    truncated = len(output) > _COMMAND_OUTPUT_CHARS or outcome.stdout_overflow or outcome.stderr_overflow
    output = output[:_COMMAND_OUTPUT_CHARS]
    if outcome.timed_out:
        return CommandResult(
            spec.glob, rendered, "timeout", output=output, truncated=truncated, timeout_sec=spec.timeout_sec,
        )
    returncode = int(outcome.returncode or 0)
    return CommandResult(
        spec.glob, rendered, "passed" if returncode == 0 else "failed", returncode, output, truncated,
    )


async def run_external_checks(ctx: Any, resolved: Path, *, tool_name: str) -> list[CommandResult]:
    """按 glob 依次跑命中的检查命令；任务被取消就不再往下跑。"""
    runtime_cfg = await asyncio.to_thread(load_runtime_config, ctx.workspace_root)
    commands = configured_commands(runtime_cfg)
    if not commands:
        return []
    rel_path = command_path(ctx.workspace_root, resolved)
    matched = [spec for spec in commands if glob_matches(rel_path, spec.glob)]
    quoted = quote_path(rel_path)
    results: list[CommandResult] = []
    for spec in matched[:_MAX_COMMANDS_PER_EDIT]:
        if "{path}" in spec.command and quoted is None:
            results.append(CommandResult(spec.glob, spec.command, "error", reason="路径里有命令行没法安全转义的字符"))
            continue
        # 只换 {path}：命令里别的花括号（awk 脚本、${VAR}）原样保留，不能走 str.format
        rendered = spec.command.replace("{path}", quoted or "")
        result = await _run_command(ctx, spec, rendered, tool_name=tool_name, display_path=rel_path)
        results.append(result)
        if result.status == "cancelled":
            break
    return results


@dataclass(frozen=True)
class EditChecks:
    syntax: SyntaxReport | None
    commands: tuple[CommandResult, ...] = ()

    @property
    def failed(self) -> bool:
        syntax_failed = self.syntax is not None and not self.syntax.ok
        return syntax_failed or any(not result.ok for result in self.commands)

    def lines(self) -> list[str]:
        lines = [self.syntax.describe()] if self.syntax is not None else []
        lines.extend(result.describe() for result in self.commands)
        return lines

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.syntax is not None:
            data["syntaxCheck"] = self.syntax.to_data()
        if self.commands:
            data["externalChecks"] = [result.to_data() for result in self.commands]
        return data


async def run_edit_checks(ctx: Any, resolved: Path, text: str, *, tool_name: str) -> EditChecks:
    """写盘成功之后调：先在进程内查语法，再跑外部检查命令。text 是写进去的内容（解码后的文本）。"""
    syntax = await asyncio.to_thread(check_syntax, resolved, text)
    commands = await run_external_checks(ctx, resolved, tool_name=tool_name)
    return EditChecks(syntax, tuple(commands))
