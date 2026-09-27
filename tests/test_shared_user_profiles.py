"""按人资料的加载。

这份文件只喂给模型、不授予权限，读错了没有任何硬性信号：`address_as: no` 被 YAML 1.1
读成布尔，bot 就会一直管人叫「False」，而日志里什么都没有。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adapters._shared.user_profiles import load_profiles  # noqa: E402


def _profiles(tmp_path: Path, text: str, suffix: str = ".yaml") -> dict[str, dict[str, str]]:
    path = tmp_path / f"user_profiles{suffix}"
    path.write_text(text, encoding="utf-8")
    return load_profiles(path)


class TestBooleanLiteralsStayText:
    def test_an_address_of_no_is_not_a_boolean(self, tmp_path: Path) -> None:
        profiles = _profiles(tmp_path, 'users:\n  "10001":\n    display_name: on\n    address_as: no\n')

        assert profiles["10001"] == {"display_name": "on", "address_as": "no"}

    def test_a_title_of_off_is_not_a_boolean(self, tmp_path: Path) -> None:
        profiles = _profiles(tmp_path, 'users:\n  "10001":\n    title: off\n    note: yes\n')

        assert profiles["10001"] == {"title": "off", "note": "yes"}

    def test_json_profiles_are_unaffected(self, tmp_path: Path) -> None:
        """JSON 没有 on/off 这层歧义，换 loader 不该动到这条分支。"""
        profiles = _profiles(tmp_path, '{"users": {"10001": {"address_as": "no"}}}', suffix=".json")

        assert profiles["10001"] == {"address_as": "no"}


class TestTheRestOfTheContractHolds:
    def test_a_flat_mapping_without_users_still_works(self, tmp_path: Path) -> None:
        profiles = _profiles(tmp_path, '"10001":\n  display_name: 张三\n')

        assert profiles == {"10001": {"display_name": "张三"}}

    def test_the_older_field_names_are_still_read(self, tmp_path: Path) -> None:
        profiles = _profiles(
            tmp_path, 'users:\n  "10001":\n    name: 张三\n    call_as: 老张\n    role: 群主\n    style_note: 爱开玩笑\n',
        )

        assert profiles["10001"] == {"display_name": "张三", "address_as": "老张", "title": "群主", "note": "爱开玩笑"}

    def test_the_current_field_name_wins_over_the_older_one(self, tmp_path: Path) -> None:
        profiles = _profiles(tmp_path, 'users:\n  "10001":\n    display_name: 张三\n    name: 旧名\n')

        assert profiles["10001"] == {"display_name": "张三"}

    def test_blank_fields_and_malformed_entries_are_dropped(self, tmp_path: Path) -> None:
        profiles = _profiles(tmp_path, 'users:\n  "10001":\n    title: "  "\n  "10002": 张三\n  "": {note: x}\n')

        assert profiles == {"10001": {}}

    def test_keys_are_taken_as_written(self, tmp_path: Path) -> None:
        # 按账号分 ID 的平台，键和别名表一样带着机器人账号。
        profiles = _profiles(tmp_path, 'users:\n  "app-1/u1":\n    address_as: 老板\n')

        assert profiles == {"app-1/u1": {"address_as": "老板"}}

    def test_a_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        assert load_profiles(tmp_path / "nope.yaml") == {}

    def test_a_platform_without_profiles_reads_nothing(self) -> None:
        assert load_profiles(None) == {}

    def test_a_broken_file_yields_nothing_and_says_so(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="adapters._shared.user_profiles"):
            assert _profiles(tmp_path, "users: [oops\n") == {}

        assert "failed to load user profiles" in caplog.text
