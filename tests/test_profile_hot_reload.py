"""按人资料改完不用重启 bot。

控制台和共享组里别的实例都直接改这份文件，没人通知 bot 进程；只能自己按修改时间、大小和 inode 认出来。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import config_layout  # noqa: E402
from _onebot_harness import body_attr, load_runtime, patch_imported  # noqa: E402
from adapters._shared.user_profiles import ProfileFile  # noqa: E402

ONE = 'users:\n  "10001":\n    display_name: 张三\n'
TWO = 'users:\n  "10001":\n    display_name: 张三\n  "10002":\n    display_name: 李四\n'
# 和 ONE 等长：同一刻替换时修改时间和大小都认不出来。
SAME_SIZE = 'users:\n  "10001":\n    display_name: 王五\n'


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
#  ProfileFile
# ---------------------------------------------------------------------------

class TestProfileFile:
    def test_loading_records_the_version_it_read(self, tmp_path: Path) -> None:
        source = ProfileFile(_write(tmp_path / "people.yaml", ONE))
        assert source.changed() is True

        assert source.load() == {"10001": {"display_name": "张三"}}
        assert source.changed() is False

    def test_an_edit_is_noticed(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "people.yaml", ONE)
        source = ProfileFile(path)
        source.load()

        path.write_text(TWO, encoding="utf-8")

        assert source.changed() is True
        assert source.load() == {"10001": {"display_name": "张三"}, "10002": {"display_name": "李四"}}
        assert source.changed() is False

    def test_a_same_size_rewrite_in_the_same_tick_is_noticed(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "people.yaml", ONE)
        source = ProfileFile(path)
        source.load()
        assert len(SAME_SIZE) == len(ONE)

        config_layout.write_config_text(path, SAME_SIZE)

        assert source.changed() is True
        assert source.load() == {"10001": {"display_name": "王五"}}

    def test_a_deleted_file_is_noticed_and_reads_back_empty(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "people.yaml", ONE)
        source = ProfileFile(path)
        source.load()

        path.unlink()

        assert source.changed() is True
        assert source.load() == {}
        assert source.changed() is False

    def test_a_file_that_never_existed_never_changes(self, tmp_path: Path) -> None:
        source = ProfileFile(tmp_path / "missing.yaml")

        assert source.changed() is False
        assert source.load() == {}
        assert source.changed() is False

    def test_a_file_appearing_later_is_noticed(self, tmp_path: Path) -> None:
        path = tmp_path / "people.yaml"
        source = ProfileFile(path)
        source.load()

        _write(path, ONE)

        assert source.changed() is True
        assert source.load() == {"10001": {"display_name": "张三"}}

    def test_no_path_at_all_is_quietly_empty(self) -> None:
        source = ProfileFile(None)

        assert source.load() == {}
        assert source.changed() is False

    def test_a_broken_file_reads_back_empty_without_raising(self, tmp_path: Path) -> None:
        source = ProfileFile(_write(tmp_path / "people.yaml", "users: [unclosed\n"))

        assert source.load() == {}
        assert source.changed() is False


# ---------------------------------------------------------------------------
#  bot 里的热载
# ---------------------------------------------------------------------------

@pytest.fixture
def bot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """一份独立的 QQ 插件实例，按人资料文件在工作区的默认位置。"""
    _write(tmp_path / "config" / "qq_user_profiles.yaml", ONE)
    runtime = load_runtime(monkeypatch, tmp_path)
    body_attr(runtime, "_load_user_profiles")()
    return runtime


def _profiles_path(root: Path) -> Path:
    return root / "config" / "qq_user_profiles.yaml"


def _reload(runtime: Any) -> bool:
    return body_attr(runtime, "_reload_user_profiles_if_changed")()


class TestTheBotReloadsProfiles:
    def test_startup_loads_what_is_on_disk(self, bot: Any) -> None:
        assert bot.state._adapter.qq_user_profiles == {"10001": {"display_name": "张三"}}

    def test_an_untouched_file_changes_nothing(self, bot: Any) -> None:
        before = bot.state._adapter.qq_user_profiles

        assert _reload(bot) is False
        # 整份换上新表会换掉对象；没变化时连对象都不该换。
        assert bot.state._adapter.qq_user_profiles is before

    def test_an_edit_swaps_the_whole_table(self, bot: Any, tmp_path: Path) -> None:
        _profiles_path(tmp_path).write_text(TWO, encoding="utf-8")

        assert _reload(bot) is True
        assert bot.state._adapter.qq_user_profiles == {
            "10001": {"display_name": "张三"}, "10002": {"display_name": "李四"},
        }

    def test_a_removed_person_is_gone_after_the_swap(self, bot: Any, tmp_path: Path) -> None:
        _profiles_path(tmp_path).write_text(TWO, encoding="utf-8")
        _reload(bot)

        _profiles_path(tmp_path).write_text(ONE, encoding="utf-8")

        assert _reload(bot) is True
        assert "10002" not in bot.state._adapter.qq_user_profiles

    def test_one_edit_reloads_only_once(self, bot: Any, tmp_path: Path) -> None:
        _profiles_path(tmp_path).write_text(TWO, encoding="utf-8")

        assert _reload(bot) is True
        assert _reload(bot) is False

    def test_a_deleted_file_empties_the_table(self, bot: Any, tmp_path: Path) -> None:
        _profiles_path(tmp_path).unlink()

        assert _reload(bot) is True
        assert bot.state._adapter.qq_user_profiles == {}

    def test_a_broken_file_empties_the_table_without_raising(self, bot: Any, tmp_path: Path) -> None:
        _profiles_path(tmp_path).write_text("users: [unclosed\n", encoding="utf-8")

        assert _reload(bot) is True
        assert bot.state._adapter.qq_user_profiles == {}


class TestTheWatcherLivesOnTheAdapter:
    """进程内状态挂在 OneBotAdapter 实例上，不落回模块级：拆文件时会散落到各个模块里。"""

    def test_it_starts_out_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write(tmp_path / "config" / "qq_user_profiles.yaml", ONE)

        runtime = load_runtime(monkeypatch, tmp_path)

        assert runtime.state._adapter.qq_profiles_file is None

    def test_startup_puts_the_watcher_on_the_adapter(self, bot: Any, tmp_path: Path) -> None:
        watcher = bot.state._adapter.qq_profiles_file

        assert isinstance(watcher, ProfileFile)
        assert watcher.path == _profiles_path(tmp_path)
        assert body_attr(bot, "_profiles_source")() is watcher

    def test_a_reload_keeps_the_same_watcher(self, bot: Any, tmp_path: Path) -> None:
        watcher = bot.state._adapter.qq_profiles_file
        _profiles_path(tmp_path).write_text(TWO, encoding="utf-8")

        assert _reload(bot) is True

        assert bot.state._adapter.qq_profiles_file is watcher

    def test_the_module_keeps_no_copy_of_it(self, bot: Any) -> None:
        import sys as _sys

        lifecycle = next(
            module for module in _sys.modules.values()
            if getattr(module, "__name__", "").endswith(".napcat.lifecycle")
            and module.__name__.startswith(bot.__name__)
        )
        assert not hasattr(lifecycle, "_profiles_file")


class TestTheWatchedPathFollowsTheSetting:
    def test_moving_the_file_moves_what_is_watched(
        self, bot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        moved = _write(tmp_path / "private" / "people.yaml", TWO)
        source = body_attr(bot, "_profiles_source")
        assert source().path == _profiles_path(tmp_path)

        patch_imported(monkeypatch, bot, "USER_PROFILES_PATH", str(moved))

        assert source().path == moved
        # 换掉的是适配器实例上那一个，不是模块里的某份副本。
        assert bot.state._adapter.qq_profiles_file is source()
        assert _reload(bot) is True
        assert bot.state._adapter.qq_user_profiles == {
            "10001": {"display_name": "张三"}, "10002": {"display_name": "李四"},
        }

    def test_the_old_file_stops_being_watched(
        self, bot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        moved = _write(tmp_path / "private" / "people.yaml", ONE)
        patch_imported(monkeypatch, bot, "USER_PROFILES_PATH", str(moved))
        _reload(bot)

        _profiles_path(tmp_path).write_text(TWO, encoding="utf-8")

        assert _reload(bot) is False
        assert "10002" not in bot.state._adapter.qq_user_profiles

    def test_the_same_path_keeps_the_same_watcher(self, bot: Any, tmp_path: Path) -> None:
        source = body_attr(bot, "_profiles_source")

        assert source() is source()
