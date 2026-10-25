"""换 config/ 的那一下：能原子对调就对调；退回两步改名时，空档里别的进程新建的 config/ 回滚前挪到一边。"""
from __future__ import annotations

import ctypes
import errno
import os
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config_layout  # noqa: E402
from supervisor import share_groups  # noqa: E402
from supervisor.instances import save_instance  # noqa: E402


@pytest.fixture
def code_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "code"
    (root / "config").mkdir(parents=True)
    (root / "data").mkdir()
    monkeypatch.setattr(config_layout, "CODE_ROOT", root)
    return root


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "kazebot-data"
    root.mkdir()
    monkeypatch.setenv(config_layout.INSTANCES_FILE_ENV, str(root / "instances.yaml"))
    monkeypatch.setenv("CLONOTH_URL_PREFIX", "/i/beta")
    return root


@pytest.fixture
def ws(data_root: Path, code_root: Path) -> Path:
    workspace = data_root / "beta"
    (workspace / "data").mkdir(parents=True)
    (workspace / "config").mkdir()
    save_instance(workspace, instance_id="alpha", label="主号", idx=0)
    save_instance(workspace, instance_id="beta", label="小号", idx=1)
    (workspace / "config" / "models.yaml").write_text("version: 1\n", encoding="utf-8")
    return workspace


def _fake_exchange(first: Path, second: Path) -> bool:
    """RENAME_EXCHANGE 的替身：三次改名，结果一样，只是不原子。"""
    spare = Path(first).with_name(Path(first).name + ".swap")
    os.rename(first, spare)
    os.rename(second, first)
    os.rename(spare, second)
    return True


def _leftovers(workspace: Path) -> list[str]:
    return sorted(entry.name for entry in workspace.iterdir() if entry.name.startswith(".config."))


def _data_entries(workspace: Path, prefix: str) -> list[Path]:
    return sorted(entry for entry in (workspace / "data").iterdir() if entry.name.startswith(prefix))


def _rename_failing_when(test: Callable[[Path, Path], bool], before: Callable[[Path, Path], None] | None = None):
    real = os.rename

    def rename(src: Any, dst: Any, **kwargs: Any) -> None:
        if test(Path(src), Path(dst)):
            if before is not None:
                before(Path(src), Path(dst))
                return real(src, dst, **kwargs)
            raise OSError("改名失败")
        return real(src, dst, **kwargs)

    return rename


class TestWithAnAtomicExchange:
    @pytest.fixture(autouse=True)
    def exchange(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(share_groups, "_exchange", _fake_exchange)

    def test_joining_archives_the_old_config_and_leaves_nothing_staged(self, ws: Path) -> None:
        result = share_groups.create_group(ws, "team")

        assert config_layout.config_share_group(ws) == "team"
        assert (ws / result["archived"] / "models.yaml").read_text(encoding="utf-8") == "version: 1\n"
        assert _leftovers(ws) == []

    def test_an_archive_that_fails_after_the_exchange_is_swapped_back(
        self, ws: Path, data_root: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(share_groups.os, "rename", _rename_failing_when(
            lambda _src, dst: dst.name.startswith(share_groups.ARCHIVE_PREFIX),
        ))

        with pytest.raises(OSError):
            share_groups.create_group(ws, "team")

        assert not config_layout.is_dir_link(ws / "config")
        assert (ws / "config" / "models.yaml").read_text(encoding="utf-8") == "version: 1\n"
        assert _leftovers(ws) == []
        assert list((data_root / config_layout.SHARED_DIR).glob("team*")) == []

    def test_leaving_puts_a_real_copy_in_place(self, ws: Path) -> None:
        share_groups.create_group(ws, "team")

        share_groups.leave_group(ws)

        assert not config_layout.is_dir_link(ws / "config")
        assert (ws / "config" / "models.yaml").read_text(encoding="utf-8") == "version: 1\n"
        assert config_layout.share_group_config_dir("team").is_dir()
        assert _leftovers(ws) == []


class TestTheTwoStepFallback:
    @pytest.fixture(autouse=True)
    def no_exchange(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(share_groups, "_exchange", lambda *_args: False)

    def test_a_config_created_in_the_window_is_set_aside_and_the_old_one_restored(
        self, ws: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 两次改名之间 config/ 不在，bot 写配置时会顺手把目录建出来：原来那份得放得回去。
        real_replace = os.replace

        def replace(src: Any, dst: Any, **kwargs: Any) -> None:
            if Path(dst) == ws / "config":
                (ws / "config").mkdir()
                (ws / "config" / "stray.yaml").write_text("written in the gap\n", encoding="utf-8")
                raise OSError("目标已经被占了")
            return real_replace(src, dst, **kwargs)

        monkeypatch.setattr(share_groups.os, "replace", replace)

        with pytest.raises(OSError):
            share_groups.create_group(ws, "team")

        assert not config_layout.is_dir_link(ws / "config")
        assert (ws / "config" / "models.yaml").read_text(encoding="utf-8") == "version: 1\n"
        strays = _data_entries(ws, share_groups.INTERRUPTED_PREFIX)
        assert len(strays) == 1 and (strays[0] / "stray.yaml").read_text(encoding="utf-8") == "written in the gap\n"
        assert _data_entries(ws, share_groups.ARCHIVE_PREFIX) == []
        assert _leftovers(ws) == []

    def test_leaving_with_a_config_created_in_the_window_links_back_to_the_group(
        self, ws: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        share_groups.create_group(ws, "team")

        def squat(_src: Path, dst: Path) -> None:
            dst.mkdir()
            (dst / "stray.yaml").write_text("written in the gap\n", encoding="utf-8")

        monkeypatch.setattr(share_groups.os, "rename", _rename_failing_when(
            lambda src, dst: dst == ws / "config" and src.name.endswith(".leaving"), before=squat,
        ))

        with pytest.raises(OSError):
            share_groups.leave_group(ws)

        assert config_layout.config_share_group(ws) == "team"
        strays = _data_entries(ws, share_groups.INTERRUPTED_PREFIX)
        assert len(strays) == 1 and (strays[0] / "stray.yaml").is_file()
        assert _leftovers(ws) == []


class TestTheExchangeCall:
    @staticmethod
    def _failing_with(code: int) -> Callable[[], Callable[..., int]]:
        def call(*_args: Any) -> int:
            ctypes.set_errno(code)
            return -1

        return lambda: call

    @pytest.mark.parametrize("code", [errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP])
    def test_an_unsupported_kernel_or_filesystem_falls_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int,
    ) -> None:
        monkeypatch.setattr(share_groups, "_renameat2", self._failing_with(code))

        assert share_groups._exchange(tmp_path / "a", tmp_path / "b") is False

    def test_a_real_failure_is_raised(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(share_groups, "_renameat2", self._failing_with(errno.EACCES))

        with pytest.raises(OSError) as raised:
            share_groups._exchange(tmp_path / "a", tmp_path / "b")
        assert raised.value.errno == errno.EACCES

    def test_no_renameat2_means_no_exchange(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(share_groups, "_renameat2", lambda: None)

        assert share_groups._exchange(tmp_path / "a", tmp_path / "b") is False

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="renameat2 只有 Linux 有")
    def test_the_real_call_swaps_two_directories(self, tmp_path: Path) -> None:
        first, second = tmp_path / "first", tmp_path / "second"
        first.mkdir()
        second.mkdir()
        (first / "a.txt").write_text("a", encoding="utf-8")
        (second / "b.txt").write_text("b", encoding="utf-8")

        if not share_groups._exchange(first, second):
            pytest.skip("这个文件系统不支持 RENAME_EXCHANGE")

        assert [entry.name for entry in first.iterdir()] == ["b.txt"]
        assert [entry.name for entry in second.iterdir()] == ["a.txt"]

    @pytest.mark.skipif(sys.platform.startswith("linux"), reason="Linux 上找得到 renameat2")
    def test_elsewhere_there_is_no_renameat2(self) -> None:
        assert share_groups._renameat2() is None


class TestMembersFromAHandEditedRoster:
    def test_a_row_whose_name_is_not_an_instance_id_is_skipped(self, ws: Path, data_root: Path) -> None:
        # 拿 ../ 之类的名字拼路径，会去读数据根外面的 config/。
        roster = data_root / "instances.yaml"
        roster.write_text(
            roster.read_text(encoding="utf-8") + "- {id: ../outside, label: 越界, path: /i/x, idx: 3}\n",
            encoding="utf-8",
        )

        assert [row["id"] for row, _ in share_groups._instance_workspaces(ws)] == ["alpha", "beta"]
