"""读进程环境的命令按敏感处理。

工具子进程 cat /proc/$PPID/environ 读到的是 engine 没剥过的完整环境，绕开 safe_subprocess_env 的剥离。
策略只有字面量 /proc/self/environ，挡不住 $PPID、通配、ps e 这些写法，这里把它们补成内置的一层。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from supervisor.policy import PolicyEngine, _process_environment_reader  # noqa: E402
from supervisor.types import SafetyLevel  # noqa: E402

_READS_ENV = [
    "cat /proc/self/environ",
    "cat /proc/$PPID/environ",
    "cat /proc/${PPID}/environ",
    "cat /proc/$$/environ",
    "cat /proc/1/environ",
    "cat /proc/self/task/5/environ",
    "cd /proc/$PPID && cat environ",
    "cat /proc/self/e*",
    "cat /proc/$PPID/*",
    "tr '\\0' '\\n' < /proc/self/environ",
    "cat /proc/$(pgrep -f engine)/environ",
    "ps e",
    "ps eww",
    "ps auxe",
    "ps eww $PPID",
    "ps auxeww",
]

_LEAVES_ALONE = [
    "cat /proc/self/status",
    "cat /proc/1/cmdline",
    "ps -e",
    "ps -ef",
    "ps aux",
    "ps -C kazebot",
    "ps -o pid,etime",
    "ps --sort=-pcpu",
    "echo environ",
    "cat environ.txt",
    "ls /proc",
]

_PERMISSIVE = "\n".join([
    "version: 1",
    "read_file: {default: auto, rules: []}",
    "write_file: {default: auto, rules: []}",
    "execute_command: {default: approval_required, sensitive_path_patterns: []}",
    "",
])

_ALL_AUTO = "\n".join([
    "version: 1",
    "read_file: {default: auto, rules: []}",
    "write_file: {default: auto, rules: []}",
    "execute_command: {default: auto, deny_patterns: [], sensitive_patterns: [], sensitive_path_patterns: []}",
    "",
])


def _engine(tmp_path: Path, policy: str | None = None) -> PolicyEngine:
    (tmp_path / "data").mkdir(exist_ok=True)
    if policy is not None:
        (tmp_path / "config").mkdir(exist_ok=True)
        (tmp_path / "config" / "policy.yaml").write_text(policy, encoding="utf-8")
    return PolicyEngine(workspace_root=tmp_path)


@pytest.mark.parametrize("command", _READS_ENV)
def test_the_reader_spots_every_env_read(command: str) -> None:
    assert _process_environment_reader(command), command


@pytest.mark.parametrize("command", _LEAVES_ALONE)
def test_the_reader_leaves_other_commands_alone(command: str) -> None:
    assert _process_environment_reader(command) == "", command


@pytest.mark.parametrize("command", _READS_ENV)
def test_reading_the_environment_is_sensitive_under_the_builtin_default(tmp_path: Path, command: str) -> None:
    decision = _engine(tmp_path).evaluate_execute_command(command=command)
    assert decision.safety_level == SafetyLevel.approval_required, command
    assert decision.sensitive, command


@pytest.mark.parametrize("command", _READS_ENV)
def test_even_an_emptied_sensitive_path_list_cannot_turn_it_off(tmp_path: Path, command: str) -> None:
    # 只关掉敏感路径那层、default 仍要审批时，读进程环境照样挡
    decision = _engine(tmp_path, _PERMISSIVE).evaluate_execute_command(command=command)
    assert decision.safety_level == SafetyLevel.approval_required, command
    assert decision.sensitive, command


@pytest.mark.parametrize("command", _LEAVES_ALONE)
def test_other_commands_keep_their_decision(tmp_path: Path, command: str) -> None:
    # 普通命令在内置默认下只是「要审批但不敏感」或命中别的规则，不该被这层误判成敏感
    decision = _engine(tmp_path).evaluate_execute_command(command=command)
    if decision.safety_level == SafetyLevel.approval_required:
        assert _process_environment_reader(command) == "", command


def test_the_whole_class_being_auto_turns_the_layer_off(tmp_path: Path) -> None:
    # 控制台一键全自动：整个 execute_command 放行，这层也不生效
    engine = _engine(tmp_path, _ALL_AUTO)
    assert engine.evaluate_execute_command(command="cat /proc/$PPID/environ").safety_level == SafetyLevel.auto
    assert engine.evaluate_execute_command(command="ps eww").safety_level == SafetyLevel.auto


def test_a_denied_path_still_wins_over_the_env_layer(tmp_path: Path) -> None:
    # 命令同时碰到 deny 路径时仍然 deny，不降级成审批
    decision = _engine(tmp_path).evaluate_execute_command(command="cat /proc/$PPID/environ config/secrets.env")
    assert decision.safety_level == SafetyLevel.deny


def test_a_public_curl_that_reads_the_environment_is_refused(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    assert engine.is_safe_public_curl("curl http://example.com/proc/self/environ") is False
