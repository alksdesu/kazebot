"""real_path：resolve 到底之后，同一位置只给一种写法。

磁盘在解析半路被别的进程动过时，CPython 会把扩展长度前缀留在 resolve 的结果里；去掉的只是它自己添上、去掉后照样能用的那种。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
for _p in (str(_ROOT), str(_TESTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _midway import EXTENDED_PREFIX, change_midway, windows_only  # noqa: E402
from real_paths import real_path  # noqa: E402

Scene = Callable[[Path], tuple[Path, Callable[[], None]]]


def _folder_on_the_way_appears(root: Path) -> tuple[Path, Callable[[], None]]:
    target = root / "a" / "b" / "c.txt"
    return target, lambda: target.parent.mkdir(parents=True)


def _folder_on_the_way_vanishes(root: Path) -> tuple[Path, Callable[[], None]]:
    target = root / "a" / "c.txt"
    target.parent.mkdir()
    return target, target.parent.rmdir


def _file_vanishes(root: Path) -> tuple[Path, Callable[[], None]]:
    target = root / "a" / "c.txt"
    target.parent.mkdir()
    target.write_bytes(b"c")
    return target, target.unlink


@windows_only
@pytest.mark.parametrize("scene", [_folder_on_the_way_appears, _folder_on_the_way_vanishes, _file_vanishes])
def test_the_disk_changing_midway_leaves_no_prefix_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scene: Scene,
) -> None:
    target, change = scene(tmp_path)
    expected = target.resolve()
    fired = change_midway(monkeypatch, target, change)

    found = real_path(target)

    assert fired
    assert found == expected


@windows_only
def test_strict_leaves_no_prefix_behind_when_the_file_vanishes_after_it_was_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target, change = _file_vanishes(tmp_path)
    expected = target.resolve(strict=True)
    fired = change_midway(monkeypatch, target, change)

    found = real_path(target, strict=True)

    assert fired
    assert found == expected


@windows_only
def test_a_prefix_the_caller_wrote_is_kept(tmp_path: Path) -> None:
    written = EXTENDED_PREFIX + str(tmp_path / "a.txt")

    assert real_path(written) == Path(written)


@windows_only
@pytest.mark.parametrize(("resolved", "expected"), [
    ("\\\\?\\C:\\data\\a.png", "C:\\data\\a.png"),
    ("\\\\?\\UNC\\server\\share\\data\\a.png", "\\\\server\\share\\data\\a.png"),
    # 卷 GUID 路径没有盘符，去掉前缀就成了相对路径。
    (
        "\\\\?\\Volume{0a1b2c3d-0000-4000-8000-000000000000}\\data\\a.png",
        "\\\\?\\Volume{0a1b2c3d-0000-4000-8000-000000000000}\\data\\a.png",
    ),
])
def test_only_drive_and_share_paths_lose_the_prefix(
    monkeypatch: pytest.MonkeyPatch, resolved: str, expected: str,
) -> None:
    monkeypatch.setattr(Path, "resolve", lambda self, strict=False: Path(resolved))

    assert str(real_path("C:\\data\\a.png")) == expected


@pytest.mark.parametrize("rel", ["a.txt", "x/../a.txt", "missing/dir/b.txt"])
def test_with_nothing_changing_it_is_resolve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rel: str) -> None:
    (tmp_path / "a.txt").write_bytes(b"a")
    monkeypatch.chdir(tmp_path)

    assert real_path(tmp_path / rel) == (tmp_path / rel).resolve()
    assert real_path(str(tmp_path / rel)) == (tmp_path / rel).resolve()
    assert real_path(rel) == Path(rel).resolve()


def test_strict_still_refuses_a_missing_path(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        real_path(tmp_path / "missing", strict=True)
