"""QQ 用户称呼 Profile 的加载。

这份文件只喂给模型、不授予权限，读错了没有任何硬性信号：`address_as: no` 被 YAML 1.1
读成布尔，bot 就会一直管人叫「False」，而日志里什么都没有。
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

from _onebot_harness import load_runtime  # noqa: E402


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    return load_runtime(monkeypatch, tmp_path)


def _profiles(runtime: Any, tmp_path: Path, text: str, suffix: str = ".yaml") -> dict[str, Any]:
    path = tmp_path / f"qq_user_profiles{suffix}"
    path.write_text(text, encoding="utf-8")
    return runtime._load_qq_user_profiles(str(path))


class TestBooleanLiteralsStayText:
    def test_an_address_of_no_is_not_a_boolean(self, runtime: Any, tmp_path: Path) -> None:
        profiles = _profiles(
            runtime, tmp_path,
            'users:\n  "10001":\n    display_name: on\n    address_as: no\n',
        )

        assert profiles["10001"] == {"display_name": "on", "address_as": "no"}

    def test_a_title_of_off_is_not_a_boolean(self, runtime: Any, tmp_path: Path) -> None:
        profiles = _profiles(
            runtime, tmp_path,
            'users:\n  "10001":\n    title: off\n    note: yes\n',
        )

        assert profiles["10001"] == {"title": "off", "note": "yes"}

    def test_json_profiles_are_unaffected(self, runtime: Any, tmp_path: Path) -> None:
        """JSON 没有 on/off 这层歧义，换 loader 不该动到这条分支。"""
        profiles = _profiles(
            runtime, tmp_path,
            '{"users": {"10001": {"address_as": "no"}}}', suffix=".json",
        )

        assert profiles["10001"] == {"address_as": "no"}


class TestTheRestOfTheContractHolds:
    def test_a_flat_mapping_without_users_still_works(self, runtime: Any, tmp_path: Path) -> None:
        profiles = _profiles(runtime, tmp_path, '"10001":\n  display_name: 张三\n')

        assert profiles == {"10001": {"display_name": "张三"}}

    def test_a_missing_file_is_not_an_error(self, runtime: Any, tmp_path: Path) -> None:
        assert runtime._load_qq_user_profiles(str(tmp_path / "nope.yaml")) == {}

    def test_a_broken_file_yields_nothing(self, runtime: Any, tmp_path: Path) -> None:
        assert _profiles(runtime, tmp_path, "users: [oops\n") == {}
