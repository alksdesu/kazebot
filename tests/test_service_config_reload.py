"""执行计划和提醒的上限每次取值都重读配置文件。

共享组里别的实例改了 config/execution.yaml、config/reminders.yaml，这边不重启也要跟上；
文件读不懂或还不在就按默认值走，不能让一份写坏的配置把服务带下去。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config_layout  # noqa: E402
from supervisor.execution.service import ExecutionService  # noqa: E402
from supervisor.feature_auth import FeatureActor  # noqa: E402
from supervisor.reminders.service import ReminderService  # noqa: E402

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_PARALLEL = 3
DEFAULT_MAX_OPEN = 50

UNREADABLE = ("a: [1, 2\n", "- one\n- two\n", "just a string\n", "")

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

    @pytest.mark.parametrize("text", UNREADABLE)
    def test_an_unreadable_file_falls_back_to_the_defaults(
        self, execution: ExecutionService, tmp_path: Path, text: str,
    ) -> None:
        _config(tmp_path, "execution.yaml", text)

        assert execution.max_bytes == DEFAULT_MAX_BYTES
        assert execution.max_parallel == DEFAULT_MAX_PARALLEL

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

    def test_breaking_the_file_goes_back_to_the_defaults(
        self, execution: ExecutionService, tmp_path: Path,
    ) -> None:
        path = _config(tmp_path, "execution.yaml", "max_parallel_steps: 6\n")
        assert execution.max_parallel == 6

        path.write_text("max_parallel_steps: [6\n", encoding="utf-8")

        assert execution.max_parallel == DEFAULT_MAX_PARALLEL

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

    @pytest.mark.parametrize("text", UNREADABLE)
    def test_an_unreadable_file_falls_back_to_the_default(
        self, reminders: ReminderService, tmp_path: Path, text: str,
    ) -> None:
        _config(tmp_path, "reminders.yaml", text)

        assert reminders.max_open == DEFAULT_MAX_OPEN

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
