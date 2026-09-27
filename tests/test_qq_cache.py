"""宿主清理单独装出去跑的那份 NTQQ 缓存清理：实例目录发现、只跟 root 的进程、按 inode 去重，以及它得能脱开仓库跑。"""
from __future__ import annotations

import ast
import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import adapters.onebot.qq_cache as qq_cache  # noqa: E402

_DAY = 24 * 3600
_SOURCE = _ROOT / "adapters" / "onebot" / "qq_cache.py"


def _symlinks_allowed() -> bool:
    with tempfile.TemporaryDirectory() as box:
        target = Path(box) / "target"
        target.mkdir()
        try:
            os.symlink(target, Path(box) / "link", target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError):
            return False
    return True


_SYMLINKS_ALLOWED = _symlinks_allowed()


@dataclass
class _Recorder:
    """记下清理想删什么，一个都不真删：分得清「交给了上下文」和「自己动手删了」。"""

    files: list[Path] = field(default_factory=list)
    dirs: list[Path] = field(default_factory=list)

    def remove_file(self, path: Path) -> None:
        self.files.append(path)

    def remove_empty_dir(self, path: Path) -> None:
        self.dirs.append(path)


def _write(path: Path, text: str = "x", *, age: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if age:
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
    return path


def _fake_pid(proc: Path, pid: str, *, uid: str, user_data_dir: str) -> Path:
    entry = proc / pid
    entry.mkdir(parents=True)
    (entry / "status").write_text(f"Name:\tQQ\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n", encoding="utf-8")
    (entry / "cmdline").write_bytes(f"/opt/QQ/qq\0--user-data-dir={user_data_dir}\0".encode())
    return entry


class TestTheDeployedInstanceDirs:
    @pytest.fixture(autouse=True)
    def _host_parent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(qq_cache, "_NAPCAT_HOST_PARENT", tmp_path)

    def test_the_first_instance_is_always_a_candidate(self, tmp_path: Path) -> None:
        # 容器停着时 /proc 里没有它的进程，序号 0 那个挂载点只能按约定认。
        assert qq_cache._deployed_ntqq_dirs() == [tmp_path / "napcat" / "qq"]

    def test_every_extra_instance_contributes_its_own_data_dir(self, tmp_path: Path) -> None:
        (tmp_path / "napcat-alpha").mkdir()
        (tmp_path / "napcat-beta").mkdir()

        assert qq_cache._deployed_ntqq_dirs() == [
            tmp_path / "napcat" / "qq",
            tmp_path / "napcat-alpha" / "qq",
            tmp_path / "napcat-beta" / "qq",
        ]

    def test_an_archived_instance_is_left_for_a_human(self, tmp_path: Path) -> None:
        """删实例只是把目录改名成 napcat-<名>.deleted-<日期>，里面的东西还等着人去捞。"""
        (tmp_path / "napcat-foo.deleted-20260101").mkdir()

        assert qq_cache._deployed_ntqq_dirs() == [tmp_path / "napcat" / "qq"]

    def test_a_missing_parent_is_not_an_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(qq_cache, "_NAPCAT_HOST_PARENT", tmp_path / "gone")

        assert qq_cache._deployed_ntqq_dirs() == [tmp_path / "gone" / "napcat" / "qq"]

    def test_discovery_lists_them_after_the_packaged_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(qq_cache, "QQ_CACHE_ROOTS_RAW", "")
        monkeypatch.setattr(qq_cache, "_PROC_ROOT", tmp_path / "no-proc")
        (tmp_path / "napcat-second").mkdir()

        assert qq_cache._discover_qq_cache_roots() == [
            Path("/app/.config/QQ"), Path("/app/napcat"), Path("/root/.config/QQ"),
            tmp_path / "napcat" / "qq", tmp_path / "napcat-second" / "qq",
        ]


class TestTellingRootProcessesApart:
    def test_the_effective_uid_decides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)

        assert qq_cache._running_as_root() is True

    def test_a_normal_user_is_not_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)

        assert qq_cache._running_as_root() is False

    def test_a_platform_without_uids_is_not_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delattr(os, "geteuid", raising=False)

        assert qq_cache._running_as_root() is False

    def test_both_uids_zero_is_owned_by_root(self, tmp_path: Path) -> None:
        assert qq_cache._owned_by_root(_fake_pid(tmp_path, "7", uid="0", user_data_dir="/x/qq")) is True

    def test_a_process_of_another_user_is_not(self, tmp_path: Path) -> None:
        assert qq_cache._owned_by_root(_fake_pid(tmp_path, "7", uid="1000", user_data_dir="/x/qq")) is False

    def test_a_process_that_only_kept_the_real_uid_is_not(self, tmp_path: Path) -> None:
        """setuid 程序把有效 uid 放掉之后，它指的路径已经由非特权那一侧说了算。"""
        pid_dir = _fake_pid(tmp_path, "7", uid="0", user_data_dir="/x/qq")
        (pid_dir / "status").write_text("Uid:\t0\t1000\t1000\t1000\n", encoding="utf-8")

        assert qq_cache._owned_by_root(pid_dir) is False

    def test_a_status_without_a_uid_line_is_not(self, tmp_path: Path) -> None:
        pid_dir = _fake_pid(tmp_path, "7", uid="0", user_data_dir="/x/qq")
        (pid_dir / "status").write_text("Name:\tQQ\n", encoding="utf-8")

        assert qq_cache._owned_by_root(pid_dir) is False

    def test_a_process_that_vanished_is_not(self, tmp_path: Path) -> None:
        assert qq_cache._owned_by_root(tmp_path / "7") is False


class TestFollowingProcessesForRoots:
    @pytest.fixture
    def proc(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        fake = tmp_path / "proc"
        _fake_pid(fake, "101", uid="0", user_data_dir="/root-owned/qq")
        _fake_pid(fake, "202", uid="1000", user_data_dir="/user-owned/qq")
        monkeypatch.setattr(qq_cache, "QQ_CACHE_ROOTS_RAW", "")
        monkeypatch.setattr(qq_cache, "_NAPCAT_HOST_PARENT", tmp_path / "empty")
        monkeypatch.setattr(qq_cache, "_PROC_ROOT", fake)
        return fake

    def test_as_root_only_root_processes_point_the_way(self, proc: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """谁都能起一个命令行带 napcat 的进程，跟着它的 --user-data-dir 走就等于让别人挑 root 删哪儿。"""
        monkeypatch.setattr(qq_cache, "_running_as_root", lambda: True)
        roots = qq_cache._discover_qq_cache_roots()

        assert proc / "101" / "root" / "root-owned" / "qq" in roots
        assert proc / "202" / "root" / "user-owned" / "qq" not in roots

    def test_as_a_normal_user_every_matching_process_counts(
        self, proc: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 非 root 跑时删得动的本来就只有自己的文件，没必要再挑进程。
        monkeypatch.setattr(qq_cache, "_running_as_root", lambda: False)
        roots = qq_cache._discover_qq_cache_roots()

        assert proc / "101" / "root" / "root-owned" / "qq" in roots
        assert proc / "202" / "root" / "user-owned" / "qq" in roots

    def test_a_proc_entry_that_is_not_a_pid_is_skipped(
        self, proc: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _fake_pid(proc, "self", uid="0", user_data_dir="/impostor/qq")
        monkeypatch.setattr(qq_cache, "_running_as_root", lambda: True)

        assert proc / "self" / "root" / "impostor" / "qq" not in qq_cache._discover_qq_cache_roots()


class TestScanningEachDirectoryOnce:
    @pytest.fixture(autouse=True)
    def _max_age(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(qq_cache, "QQ_CACHE_MAX_AGE", 7.0 * _DAY)

    def test_two_spellings_of_one_directory_are_scanned_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog,
    ) -> None:
        root = tmp_path / "napcat"
        root.mkdir()
        (tmp_path / "sub").mkdir()
        stale = _write(root / "old.jpg", age=30 * _DAY)
        monkeypatch.setattr(qq_cache, "_discover_qq_cache_roots", lambda: [root, tmp_path / "sub" / ".." / "napcat"])
        context = _Recorder()

        with caplog.at_level(logging.INFO):
            qq_cache.purge_qq_internal_cache(context)

        assert context.files == [stale]
        assert any("scanned_roots=1 deleted=1" in r.getMessage() for r in caplog.records)

    @pytest.mark.skipif(not _SYMLINKS_ALLOWED, reason="本机建符号链接要特权，宿主上 /proc/<pid>/root 那条路复现不了")
    def test_a_symlinked_second_path_is_scanned_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog,
    ) -> None:
        """宿主挂载点和经 /proc/<pid>/root 看到的容器内路径拼法完全不同，只有 inode 认得出是同一个目录。"""
        root = tmp_path / "napcat"
        root.mkdir()
        alias = tmp_path / "napcat-alias"
        os.symlink(root, alias, target_is_directory=True)
        stale = _write(root / "old.jpg", age=30 * _DAY)
        monkeypatch.setattr(qq_cache, "_discover_qq_cache_roots", lambda: [root, alias])
        context = _Recorder()

        with caplog.at_level(logging.INFO):
            qq_cache.purge_qq_internal_cache(context)

        assert context.files == [stale]
        assert any("scanned_roots=1 deleted=1" in r.getMessage() for r in caplog.records)

    def test_two_separate_directories_are_both_scanned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog,
    ) -> None:
        first = _write(tmp_path / "napcat-a" / "old.jpg", age=30 * _DAY)
        second = _write(tmp_path / "napcat-b" / "old.jpg", age=30 * _DAY)
        monkeypatch.setattr(qq_cache, "_discover_qq_cache_roots", lambda: [first.parent, second.parent])
        context = _Recorder()

        with caplog.at_level(logging.INFO):
            qq_cache.purge_qq_internal_cache(context)

        assert context.files == [first, second]
        assert any("scanned_roots=2 deleted=2" in r.getMessage() for r in caplog.records)


class TestTheHostRemover:
    def test_a_dry_run_reports_the_file_instead_of_deleting_it(self, tmp_path: Path, caplog) -> None:
        stale = _write(tmp_path / "old.jpg")

        with caplog.at_level(logging.INFO):
            qq_cache._HostRemover(dry_run=True).remove_file(stale)

        assert stale.exists()
        assert any(f"would delete file {stale}" in r.getMessage() for r in caplog.records)

    def test_a_dry_run_reports_the_empty_dir_instead_of_removing_it(self, tmp_path: Path, caplog) -> None:
        folder = tmp_path / "Image"
        folder.mkdir()

        with caplog.at_level(logging.INFO):
            qq_cache._HostRemover(dry_run=True).remove_empty_dir(folder)

        assert folder.exists()
        assert any(f"would remove empty dir {folder}" in r.getMessage() for r in caplog.records)

    def test_a_real_run_unlinks_the_file(self, tmp_path: Path) -> None:
        stale = _write(tmp_path / "old.jpg")

        qq_cache._HostRemover(dry_run=False).remove_file(stale)

        assert not stale.exists()

    def test_a_real_run_removes_the_empty_dir(self, tmp_path: Path) -> None:
        folder = tmp_path / "Image"
        folder.mkdir()

        qq_cache._HostRemover(dry_run=False).remove_empty_dir(folder)

        assert not folder.exists()


class TestTheCommandLineEntryPoint:
    def test_a_dry_run_succeeds_without_touching_anything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog,
    ) -> None:
        root = tmp_path / "napcat"
        stale = _write(root / "old.jpg", age=30 * _DAY)
        monkeypatch.setattr(qq_cache, "QQ_CACHE_MAX_AGE", 7.0 * _DAY)
        monkeypatch.setattr(qq_cache, "QQ_CACHE_ROOTS_RAW", str(root))
        monkeypatch.setattr(qq_cache, "_NAPCAT_HOST_PARENT", tmp_path / "no-parent")
        monkeypatch.setattr(qq_cache, "_PROC_ROOT", tmp_path / "no-proc")

        with caplog.at_level(logging.INFO):
            code = qq_cache.main(["--dry-run"])

        assert code == 0
        assert stale.exists()
        assert any(f"would delete file {stale}" in r.getMessage() for r in caplog.records)

    def test_an_isolated_interpreter_runs_the_file_on_its_own(self) -> None:
        """定时任务用系统解释器跑装出去的那份副本，既没有仓库的 sys.path 也没有 venv。"""
        env = {key: value for key, value in os.environ.items() if not key.startswith("CLONOTH_QQ_CACHE")}
        done = subprocess.run(
            [sys.executable, "-I", str(_SOURCE), "--dry-run"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, cwd=str(_ROOT), timeout=120,
        )

        assert done.returncode == 0, done.stderr
        assert any(line.startswith("[qq_cache] scanned_roots=") for line in done.stdout.splitlines()), done.stdout


class TestItOnlyNeedsTheStandardLibrary:
    def _tree(self) -> ast.Module:
        return ast.parse(_SOURCE.read_text(encoding="utf-8"))

    def test_every_import_is_a_standard_library_module(self) -> None:
        # 宿主清理只装这一个文件到 /usr/local/lib/kazebot，多一个第三方依赖它就起不来。
        imported: list[str] = []
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Import):
                imported.extend(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level:
                imported.append((node.module or "").split(".")[0])

        assert sorted({name for name in imported if name not in sys.stdlib_module_names}) == []

    def test_nothing_reaches_back_into_the_package(self) -> None:
        relative = [
            ast.unparse(node) for node in ast.walk(self._tree())
            if isinstance(node, ast.ImportFrom) and node.level
        ]

        assert relative == []
