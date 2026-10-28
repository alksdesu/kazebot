"""执行计划和提醒的上限每次取值都看一眼配置文件。

共享组里别的实例改了 config/execution.yaml、config/reminders.yaml，这边不重启也要跟上；
文件还不在或空着按默认值走。写坏了不能把服务带下去，也不能悄悄当成没写：
这个进程读成过一份就沿用那份，一份都没读成过才按默认值，原因记一次日志、进健康检查。
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import clonoth_runtime  # noqa: E402
import config_layout  # noqa: E402
from supervisor.execution.service import EXECUTION_SETTINGS, ExecutionService  # noqa: E402
from supervisor.feature_auth import FeatureActor  # noqa: E402
from supervisor.reminders.service import REMINDER_SETTINGS, ReminderService  # noqa: E402

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_PARALLEL = 3
DEFAULT_MAX_OPEN = 50

UNREADABLE = pytest.mark.parametrize("text,reason", [
    pytest.param("a: [1, 2\n", "第 2 行", id="yaml-error"),
    pytest.param("- one\n- two\n", "顶层必须是映射", id="list"),
    pytest.param("just a string\n", "顶层必须是映射", id="scalar"),
])
EMPTY = pytest.mark.parametrize("text", [
    pytest.param("", id="empty"),
    pytest.param("# 先空着\n", id="comment-only"),
    pytest.param("\n\n", id="blank-lines"),
])

# 解析得出来但不是个整数的值。填错一个字符不能让这个组里每个实例的每个请求都 500。
NOT_AN_INTEGER = pytest.mark.parametrize("written", [
    pytest.param("abc", id="text"),
    pytest.param("[1, 2]", id="list"),
    pytest.param("{a: 1}", id="mapping"),
    pytest.param("null", id="null"),
    pytest.param("true", id="bool"),
    pytest.param("4096.5", id="float"),
    pytest.param("''", id="empty-string"),
])


def _config(root: Path, name: str, text: str) -> Path:
    path = root / "config" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _settings_warnings(caplog: pytest.LogCaptureFixture, rel: str) -> list[str]:
    return [
        record.getMessage() for record in caplog.records
        if record.name == clonoth_runtime._log.name and record.levelno == logging.WARNING
        and record.getMessage().startswith(f"{rel} ")
    ]


@pytest.fixture
def execution(tmp_path: Path) -> ExecutionService:
    return ExecutionService(SimpleNamespace(workspace_root=tmp_path))


@pytest.fixture
def reminders(tmp_path: Path) -> ReminderService:
    return ReminderService(SimpleNamespace(workspace_root=tmp_path))


# ---------------------------------------------------------------------------
#  执行计划
# ---------------------------------------------------------------------------

class TestExecutionLimits:
    def test_no_config_file_means_the_defaults(self, execution: ExecutionService) -> None:
        assert execution.max_bytes == DEFAULT_MAX_BYTES
        assert execution.max_parallel == DEFAULT_MAX_PARALLEL

    def test_the_configured_values_are_used(self, execution: ExecutionService, tmp_path: Path) -> None:
        _config(tmp_path, "execution.yaml", "max_input_bytes: 4096\nmax_parallel_steps: 6\n")

        assert execution.max_bytes == 4096
        assert execution.max_parallel == 6

    def test_a_key_that_is_not_set_keeps_its_default(self, execution: ExecutionService, tmp_path: Path) -> None:
        _config(tmp_path, "execution.yaml", "max_parallel_steps: 6\n")

        assert execution.max_bytes == DEFAULT_MAX_BYTES
        assert execution.max_parallel == 6

    @pytest.mark.parametrize("configured,expected", [(1, 1024), (0, 1024), (-5, 1024), (99 * 1024 * 1024, 50 * 1024 * 1024)])
    def test_the_input_size_is_clamped(
        self, execution: ExecutionService, tmp_path: Path, configured: int, expected: int,
    ) -> None:
        _config(tmp_path, "execution.yaml", f"max_input_bytes: {configured}\n")

        assert execution.max_bytes == expected

    @pytest.mark.parametrize("configured,expected", [(0, 1), (-1, 1), (99, 8)])
    def test_the_parallel_count_is_clamped(
        self, execution: ExecutionService, tmp_path: Path, configured: int, expected: int,
    ) -> None:
        _config(tmp_path, "execution.yaml", f"max_parallel_steps: {configured}\n")

        assert execution.max_parallel == expected

    @UNREADABLE
    def test_a_file_broken_before_anything_was_read_uses_the_defaults(
        self, execution: ExecutionService, tmp_path: Path, text: str, reason: str,
    ) -> None:
        path = _config(tmp_path, "execution.yaml", text)

        assert execution.max_bytes == DEFAULT_MAX_BYTES
        assert execution.max_parallel == DEFAULT_MAX_PARALLEL
        problem = EXECUTION_SETTINGS.problem(path)
        assert problem.startswith(f"{config_layout.EXECUTION_SETTINGS_FILE} 用不了，各项按默认值算：")
        assert reason in problem

    @EMPTY
    def test_an_empty_file_means_the_defaults_and_is_not_a_problem(
        self, execution: ExecutionService, tmp_path: Path, text: str,
    ) -> None:
        path = _config(tmp_path, "execution.yaml", text)

        assert execution.max_bytes == DEFAULT_MAX_BYTES
        assert execution.max_parallel == DEFAULT_MAX_PARALLEL
        assert EXECUTION_SETTINGS.problem(path) == ""

    def test_a_missing_file_is_not_a_problem(self, execution: ExecutionService, tmp_path: Path) -> None:
        assert execution.max_bytes == DEFAULT_MAX_BYTES
        assert EXECUTION_SETTINGS.problem(config_layout.execution_settings_path(tmp_path)) == ""

    def test_a_file_that_is_not_utf8_is_reported_as_unreadable(
        self, execution: ExecutionService, tmp_path: Path,
    ) -> None:
        path = config_layout.execution_settings_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"max_parallel_steps: 6\n# \xff\xfe\n")

        assert execution.max_parallel == DEFAULT_MAX_PARALLEL
        assert "读不了：不是 UTF-8 编码" in EXECUTION_SETTINGS.problem(path)

    def test_the_problem_names_the_place_but_not_what_was_written(
        self, execution: ExecutionService, tmp_path: Path,
    ) -> None:
        path = _config(tmp_path, "execution.yaml", "max_input_bytes: [sk-live-0123456789abcdef\n")

        assert execution.max_bytes == DEFAULT_MAX_BYTES
        problem = EXECUTION_SETTINGS.problem(path)
        assert "第 2 行第 1 列" in problem
        assert "sk-live-0123456789abcdef" not in problem

    @NOT_AN_INTEGER
    def test_a_value_that_is_not_an_integer_falls_back_to_the_defaults(
        self, execution: ExecutionService, tmp_path: Path, written: str,
    ) -> None:
        _config(tmp_path, "execution.yaml", f"max_input_bytes: {written}\nmax_parallel_steps: {written}\n")

        assert execution.max_bytes == DEFAULT_MAX_BYTES
        assert execution.max_parallel == DEFAULT_MAX_PARALLEL

    @NOT_AN_INTEGER
    def test_reading_it_again_and_again_never_raises(
        self, execution: ExecutionService, tmp_path: Path, written: str,
    ) -> None:
        # 每次取值都重读，抛出去就是每个请求都 500，而不是启动时一次就发现。
        _config(tmp_path, "execution.yaml", f"max_input_bytes: {written}\nmax_parallel_steps: {written}\n")

        for _ in range(3):
            assert execution.max_bytes == DEFAULT_MAX_BYTES
            assert execution.max_parallel == DEFAULT_MAX_PARALLEL

    def test_a_number_written_as_text_is_still_a_number(
        self, execution: ExecutionService, tmp_path: Path,
    ) -> None:
        _config(tmp_path, "execution.yaml", "max_input_bytes: '4096'\nmax_parallel_steps: '2'\n")

        assert (execution.max_bytes, execution.max_parallel) == (4096, 2)

    def test_only_the_broken_key_falls_back(self, execution: ExecutionService, tmp_path: Path) -> None:
        _config(tmp_path, "execution.yaml", "max_input_bytes: abc\nmax_parallel_steps: 6\n")

        assert execution.max_bytes == DEFAULT_MAX_BYTES
        assert execution.max_parallel == 6

    def test_fixing_the_value_takes_effect_on_the_next_access(
        self, execution: ExecutionService, tmp_path: Path,
    ) -> None:
        path = _config(tmp_path, "execution.yaml", "max_input_bytes: abc\n")
        assert execution.max_bytes == DEFAULT_MAX_BYTES

        config_layout.write_config_text(path, "max_input_bytes: 4096\n")

        assert execution.max_bytes == 4096

    def test_an_edit_lands_on_the_very_next_access(self, execution: ExecutionService, tmp_path: Path) -> None:
        path = _config(tmp_path, "execution.yaml", "max_input_bytes: 4096\nmax_parallel_steps: 6\n")
        assert (execution.max_bytes, execution.max_parallel) == (4096, 6)

        config_layout.write_config_text(path, "max_input_bytes: 8192\nmax_parallel_steps: 2\n")

        assert (execution.max_bytes, execution.max_parallel) == (8192, 2)

    def test_removing_the_file_goes_back_to_the_defaults(
        self, execution: ExecutionService, tmp_path: Path,
    ) -> None:
        path = _config(tmp_path, "execution.yaml", "max_input_bytes: 4096\n")
        assert execution.max_bytes == 4096

        path.unlink()

        assert execution.max_bytes == DEFAULT_MAX_BYTES

    def test_breaking_the_file_keeps_the_values_read_last(
        self, execution: ExecutionService, tmp_path: Path,
    ) -> None:
        # 改到一半存了一份坏的，不能让并行上限悄悄回到默认值。
        path = _config(tmp_path, "execution.yaml", "max_input_bytes: 4096\nmax_parallel_steps: 6\n")
        assert (execution.max_bytes, execution.max_parallel) == (4096, 6)

        config_layout.write_config_text(path, "max_parallel_steps: [6\n")

        assert (execution.max_bytes, execution.max_parallel) == (4096, 6)
        problem = EXECUTION_SETTINGS.problem(path)
        assert problem.startswith(f"{config_layout.EXECUTION_SETTINGS_FILE} 用不了，沿用上一份能用的：")
        assert "第 2 行第 1 列" in problem

    def test_fixing_a_broken_file_takes_effect_and_clears_the_problem(
        self, execution: ExecutionService, tmp_path: Path,
    ) -> None:
        path = _config(tmp_path, "execution.yaml", "max_parallel_steps: 6\n")
        assert execution.max_parallel == 6
        config_layout.write_config_text(path, "- not a mapping\n")
        assert execution.max_parallel == 6

        config_layout.write_config_text(path, "max_parallel_steps: 2\n")

        assert execution.max_parallel == 2
        assert EXECUTION_SETTINGS.problem(path) == ""

    def test_removing_a_broken_file_goes_back_to_the_defaults(
        self, execution: ExecutionService, tmp_path: Path,
    ) -> None:
        # 删掉是不要这些设置了，不是写坏，不沿用上一份。
        path = _config(tmp_path, "execution.yaml", "max_parallel_steps: 6\n")
        assert execution.max_parallel == 6
        config_layout.write_config_text(path, "max_parallel_steps: [6\n")
        assert execution.max_parallel == 6

        path.unlink()

        assert execution.max_parallel == DEFAULT_MAX_PARALLEL
        assert EXECUTION_SETTINGS.problem(path) == ""

    def test_a_broken_version_is_logged_once(
        self, execution: ExecutionService, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        path = _config(tmp_path, "execution.yaml", "max_parallel_steps: 6\n")
        assert execution.max_parallel == 6
        caplog.set_level(logging.WARNING, logger=clonoth_runtime._log.name)

        config_layout.write_config_text(path, "max_parallel_steps: [6\n")
        for _ in range(3):
            assert execution.max_parallel == 6
            assert execution.max_bytes == DEFAULT_MAX_BYTES
        assert len(_settings_warnings(caplog, config_layout.EXECUTION_SETTINGS_FILE)) == 1

        config_layout.write_config_text(path, "max_parallel_steps: [66\n")
        assert execution.max_parallel == 6

        warnings = _settings_warnings(caplog, config_layout.EXECUTION_SETTINGS_FILE)
        assert len(warnings) == 2
        assert all("沿用上一份能用的" in warning for warning in warnings)

    def test_a_second_service_on_the_same_workspace_agrees(self, tmp_path: Path) -> None:
        # 共享组里几个实例读的是同一份文件，算出来的上限必须一样。
        _config(tmp_path, "execution.yaml", "max_input_bytes: 4096\nmax_parallel_steps: 2\n")
        first = ExecutionService(SimpleNamespace(workspace_root=tmp_path))
        second = ExecutionService(SimpleNamespace(workspace_root=tmp_path))

        config_layout.write_config_text(tmp_path / "config" / "execution.yaml", "max_input_bytes: 5120\n")

        assert (first.max_bytes, second.max_bytes) == (5120, 5120)
        assert (first.max_parallel, second.max_parallel) == (DEFAULT_MAX_PARALLEL, DEFAULT_MAX_PARALLEL)

    def test_the_limit_is_read_under_the_config_directory(self, tmp_path: Path) -> None:
        # 位置由 config_layout 说了算，不是硬编码的 "config/execution.yaml"。
        service = ExecutionService(SimpleNamespace(workspace_root=tmp_path))
        _config(tmp_path, "execution.yaml", "max_input_bytes: 4096\n")

        assert config_layout.config_dir(tmp_path) == tmp_path / "config"
        assert service.max_bytes == 4096


# ---------------------------------------------------------------------------
#  提醒
# ---------------------------------------------------------------------------

def _actor() -> FeatureActor:
    return FeatureActor(scope="group:10001", owner="10001", channel="qq", bot_scope="bot:10001", user_id="10001")


def _due() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


class TestReminderLimit:
    def test_no_config_file_means_the_default(self, reminders: ReminderService) -> None:
        assert reminders.max_open == DEFAULT_MAX_OPEN

    def test_the_configured_value_is_used(self, reminders: ReminderService, tmp_path: Path) -> None:
        _config(tmp_path, "reminders.yaml", "max_open_per_owner: 7\n")

        assert reminders.max_open == 7

    @pytest.mark.parametrize("configured,expected", [(0, 1), (-3, 1), (5000, 500)])
    def test_it_is_clamped(
        self, reminders: ReminderService, tmp_path: Path, configured: int, expected: int,
    ) -> None:
        _config(tmp_path, "reminders.yaml", f"max_open_per_owner: {configured}\n")

        assert reminders.max_open == expected

    @UNREADABLE
    def test_a_file_broken_before_anything_was_read_uses_the_default(
        self, reminders: ReminderService, tmp_path: Path, text: str, reason: str,
    ) -> None:
        path = _config(tmp_path, "reminders.yaml", text)

        assert reminders.max_open == DEFAULT_MAX_OPEN
        problem = REMINDER_SETTINGS.problem(path)
        assert problem.startswith(f"{config_layout.REMINDER_SETTINGS_FILE} 用不了，各项按默认值算：")
        assert reason in problem

    @EMPTY
    def test_an_empty_file_means_the_default_and_is_not_a_problem(
        self, reminders: ReminderService, tmp_path: Path, text: str,
    ) -> None:
        path = _config(tmp_path, "reminders.yaml", text)

        assert reminders.max_open == DEFAULT_MAX_OPEN
        assert REMINDER_SETTINGS.problem(path) == ""

    def test_breaking_the_file_keeps_the_limit_read_last(
        self, reminders: ReminderService, tmp_path: Path,
    ) -> None:
        # 改到一半存了一份坏的，上限不能悄悄从 2 跳回 50。
        path = _config(tmp_path, "reminders.yaml", "max_open_per_owner: 2\n")
        actor = _actor()
        reminders.create(actor, "第一条", _due())
        reminders.create(actor, "第二条", _due())

        config_layout.write_config_text(path, "max_open_per_owner: [2\n")

        assert reminders.max_open == 2
        with pytest.raises(ValueError, match="2 条"):
            reminders.create(actor, "第三条", _due())
        assert REMINDER_SETTINGS.problem(path).startswith(
            f"{config_layout.REMINDER_SETTINGS_FILE} 用不了，沿用上一份能用的：",
        )

    @NOT_AN_INTEGER
    def test_a_value_that_is_not_an_integer_falls_back_to_the_default(
        self, reminders: ReminderService, tmp_path: Path, written: str,
    ) -> None:
        _config(tmp_path, "reminders.yaml", f"max_open_per_owner: {written}\n")

        for _ in range(3):
            assert reminders.max_open == DEFAULT_MAX_OPEN

    def test_a_number_written_as_text_is_still_a_number(
        self, reminders: ReminderService, tmp_path: Path,
    ) -> None:
        _config(tmp_path, "reminders.yaml", "max_open_per_owner: '7'\n")

        assert reminders.max_open == 7

    def test_a_broken_value_does_not_break_creating_a_reminder(
        self, reminders: ReminderService, tmp_path: Path,
    ) -> None:
        # 上限读不出来时按默认放行，而不是让每一次新建都炸在取上限这一步。
        _config(tmp_path, "reminders.yaml", "max_open_per_owner: abc\n")

        assert reminders.create(_actor(), "第一条", _due())["text"] == "第一条"

    def test_an_edit_lands_on_the_very_next_access(self, reminders: ReminderService, tmp_path: Path) -> None:
        path = _config(tmp_path, "reminders.yaml", "max_open_per_owner: 7\n")
        assert reminders.max_open == 7

        config_layout.write_config_text(path, "max_open_per_owner: 3\n")

        assert reminders.max_open == 3

    def test_the_guard_uses_the_limit_that_is_on_disk_right_now(
        self, reminders: ReminderService, tmp_path: Path,
    ) -> None:
        path = _config(tmp_path, "reminders.yaml", "max_open_per_owner: 2\n")
        actor = _actor()
        reminders.create(actor, "第一条", _due())
        reminders.create(actor, "第二条", _due())
        with pytest.raises(ValueError, match="2 条"):
            reminders.create(actor, "第三条", _due())

        config_layout.write_config_text(path, "max_open_per_owner: 3\n")

        # 组里别的实例调高了上限，这边不重启就该放行。
        assert reminders.create(actor, "第三条", _due())["text"] == "第三条"
        with pytest.raises(ValueError, match="3 条"):
            reminders.create(actor, "第四条", _due())

    def test_an_admin_is_not_held_to_the_limit(self, reminders: ReminderService, tmp_path: Path) -> None:
        _config(tmp_path, "reminders.yaml", "max_open_per_owner: 1\n")
        actor = FeatureActor(scope="group:10001", owner="admin", is_admin=True, bot_scope="bot:10001")

        for text in ("第一条", "第二条", "第三条"):
            assert reminders.create(actor, text, _due())["text"] == text
