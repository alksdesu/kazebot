"""QQ 侧的定期维护：NapCat 客户端缓存和适配器自己写的 data/cache/onebot_* 由平台的 maintenance 钩子清。

删除一律经上下文，核心的 dry-run 才管得住；钩子模块跑在清理进程里，导入时不能拉起
NoneBot 或 adapters.onebot.config，否则定时清理会连着整个适配器配置一起加载。
"""
from __future__ import annotations

import ast
import logging
import os
import subprocess
import sys
import time
from dataclasses import FrozenInstanceError, dataclass, field
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import adapters.onebot.maintenance as maint  # noqa: E402
import engine.data_cleanup as cleanup  # noqa: E402
from platforms.maintenance import MaintenanceTask  # noqa: E402

_DAY = 24 * 3600


@dataclass
class _Recorder:
    """记下钩子想删什么，但一个都不真删：分得清「调用了上下文」和「自己动手删了」。"""

    workspace_root: Path
    data_dir: Path
    dry_run: bool = False
    files: list[Path] = field(default_factory=list)
    dirs: list[Path] = field(default_factory=list)
    purges: list[tuple[Path, float, str, bool]] = field(default_factory=list)

    def remove_file(self, path: Path) -> None:
        self.files.append(path)

    def remove_empty_dir(self, path: Path) -> None:
        self.dirs.append(path)

    def purge_dir(self, directory: Path, max_age: float, label: str, *, recursive: bool = False) -> None:
        self.purges.append((directory, max_age, label, recursive))


def _recorder(tmp_path: Path) -> _Recorder:
    return _Recorder(workspace_root=tmp_path, data_dir=tmp_path / "data")


def _age(path: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def _write(path: Path, text: str = "x", *, age: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if age:
        _age(path, age)
    return path


class TestTheTaskContract:
    def test_a_task_cannot_be_rewritten_after_it_is_declared(self) -> None:
        # 任务表是各平台交给核心的声明，核心按名字报错误；名字被改掉日志就对不上人。
        task = MaintenanceTask("qq_cache", lambda _context: None)

        with pytest.raises(FrozenInstanceError):
            task.name = "other"  # type: ignore[misc]

    def test_a_task_carries_its_name_and_its_callable(self) -> None:
        def run(_context: object) -> None:
            return None

        task = MaintenanceTask("onebot_cache", run)

        assert (task.name, task.run) == ("onebot_cache", run)

    def test_the_qq_tasks_are_named_and_ordered(self) -> None:
        tasks = maint.maintenance_tasks()

        assert [task.name for task in tasks] == ["onebot_cache", "qq_cache"]
        assert [task.run for task in tasks] == [
            maint.purge_adapter_cache, maint.purge_qq_internal_cache,
        ]


class TestTheHookStaysLightweight:
    """清理进程只跑标准库这一层，钩子模块把 NoneBot 或适配器配置牵进来就白搭。"""

    def test_importing_it_pulls_in_neither_nonebot_nor_the_adapter_config(self) -> None:
        probe = (
            "import sys\n"
            f"sys.path.insert(0, {str(_ROOT)!r})\n"
            "import adapters.onebot.maintenance\n"
            "leaked = sorted(\n"
            "    name for name in sys.modules\n"
            "    if name == 'nonebot' or name.startswith('nonebot.') or name == 'adapters.onebot.config'\n"
            ")\n"
            "print(','.join(leaked))\n"
        )
        done = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, encoding="utf-8", timeout=120,
        )

        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == ""

    def test_it_names_neither_of_them_in_its_imports(self) -> None:
        tree = ast.parse((_ROOT / "adapters" / "onebot" / "maintenance.py").read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level:
                imported.append(node.module or "")

        assert [name for name in imported if name.split(".")[0] == "nonebot"] == []
        assert "adapters.onebot.config" not in imported


class TestTheReplyIndexPath:
    def test_it_defaults_under_the_workspace_cache_dir(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("ONEBOT_REPLY_ATTACHMENT_CACHE_FILE", raising=False)

        assert maint.reply_attachment_cache_file(tmp_path) == (
            tmp_path / "data" / "cache" / "onebot_reply_attachments.json"
        )

    def test_the_env_moves_it(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("ONEBOT_REPLY_ATTACHMENT_CACHE_FILE", str(tmp_path / "elsewhere" / "idx.json"))

        assert maint.reply_attachment_cache_file(tmp_path) == tmp_path / "elsewhere" / "idx.json"

    @pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
    def test_a_blank_env_is_not_a_location(self, tmp_path: Path, monkeypatch, blank: str) -> None:
        # 空串当成路径就会写到当前目录，而且清理会照着那个位置去删。
        monkeypatch.setenv("ONEBOT_REPLY_ATTACHMENT_CACHE_FILE", blank)

        assert maint.reply_attachment_cache_file(tmp_path) == (
            tmp_path / "data" / "cache" / "onebot_reply_attachments.json"
        )

    def test_surrounding_whitespace_is_trimmed(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("ONEBOT_REPLY_ATTACHMENT_CACHE_FILE", f"  {tmp_path / 'idx.json'}  ")

        assert maint.reply_attachment_cache_file(tmp_path) == tmp_path / "idx.json"

    def test_the_adapter_and_the_cleanup_agree_on_where_it_lives(self) -> None:
        """写的人和清的人各算各的路径，就会出现「清理跑了但文件永远在」。"""
        from adapters.onebot import config

        workspace = Path(config.CLONOTH_WORKSPACE)
        assert config.REPLY_ATTACHMENT_CACHE_FILE == str(maint.reply_attachment_cache_file(workspace))


class TestPurgingTheAdapterCache:
    @pytest.fixture(autouse=True)
    def _fixed_age(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("ONEBOT_REPLY_ATTACHMENT_CACHE_FILE", raising=False)
        monkeypatch.setattr(maint, "ADAPTER_CACHE_MAX_AGE", float(_DAY))

    def _index(self, tmp_path: Path) -> Path:
        return tmp_path / "data" / "cache" / "onebot_reply_attachments.json"

    def test_an_old_index_is_deleted_through_the_context(self, tmp_path: Path) -> None:
        index = _write(self._index(tmp_path), age=3 * _DAY)
        context = _recorder(tmp_path)

        maint.purge_adapter_cache(context)

        assert context.files == [index]
        # 钩子自己不许动文件系统：dry-run 全靠上下文那一层。
        assert index.exists()

    def test_a_half_written_tmp_file_is_deleted_too(self, tmp_path: Path) -> None:
        index = _write(self._index(tmp_path), age=3 * _DAY)
        tmp = _write(self._index(tmp_path).with_suffix(".json.tmp"), age=3 * _DAY)
        context = _recorder(tmp_path)

        maint.purge_adapter_cache(context)

        assert set(context.files) == {index, tmp}

    def test_a_stale_tmp_file_is_reached_even_with_a_fresh_index(self, tmp_path: Path) -> None:
        _write(self._index(tmp_path))
        tmp = _write(self._index(tmp_path).with_suffix(".json.tmp"), age=3 * _DAY)
        context = _recorder(tmp_path)

        maint.purge_adapter_cache(context)

        assert context.files == [tmp]

    def test_a_recent_index_stays(self, tmp_path: Path) -> None:
        _write(self._index(tmp_path))
        context = _recorder(tmp_path)

        maint.purge_adapter_cache(context)

        assert context.files == []

    def test_another_adapter_cache_file_is_swept_too(self, tmp_path: Path) -> None:
        """按前缀扫而不是点名一个文件：再加一份缓存不用回来改清理。"""
        future = _write(tmp_path / "data" / "cache" / "onebot_future.json", age=3 * _DAY)
        leftover = _write(tmp_path / "data" / "cache" / "onebot_future.json.tmp", age=3 * _DAY)
        context = _recorder(tmp_path)

        maint.purge_adapter_cache(context)

        assert set(context.files) == {future, leftover}

    def test_a_recent_adapter_file_stays(self, tmp_path: Path) -> None:
        _write(tmp_path / "data" / "cache" / "onebot_future.json")
        context = _recorder(tmp_path)

        maint.purge_adapter_cache(context)

        assert context.files == []

    def test_a_foreign_file_in_the_cache_dir_is_not_touched(self, tmp_path: Path) -> None:
        """data/cache 是共用目录，按龄整扫就会替别的模块决定它的缓存能留多久。"""
        stranger = _write(tmp_path / "data" / "cache" / "somebody_else.json", age=30 * _DAY)
        context = _recorder(tmp_path)

        maint.purge_adapter_cache(context)

        assert context.files == []
        assert stranger.exists()

    def test_a_file_in_a_subdirectory_is_out_of_reach(self, tmp_path: Path) -> None:
        # 只扫一层：子目录归谁管不清楚，递归下去就可能删到别人放在那里的东西。
        nested = _write(tmp_path / "data" / "cache" / "sub" / "onebot_x.json", age=30 * _DAY)
        context = _recorder(tmp_path)

        maint.purge_adapter_cache(context)

        assert context.files == []
        assert nested.exists()

    def test_a_matching_directory_is_left_alone(self, tmp_path: Path) -> None:
        folder = tmp_path / "data" / "cache" / "onebot_dir"
        folder.mkdir(parents=True)
        _age(folder, 30 * _DAY)
        context = _recorder(tmp_path)

        maint.purge_adapter_cache(context)

        assert context.files == []
        assert folder.exists()

    def test_a_directory_at_the_index_path_is_left_alone(self, tmp_path: Path) -> None:
        index = self._index(tmp_path)
        index.mkdir(parents=True)
        _age(index, 3 * _DAY)
        context = _recorder(tmp_path)

        maint.purge_adapter_cache(context)

        assert context.files == []

    def test_nothing_there_is_not_an_error(self, tmp_path: Path, caplog) -> None:
        context = _recorder(tmp_path)

        with caplog.at_level(logging.INFO):
            maint.purge_adapter_cache(context)

        assert context.files == []
        assert any("[onebot_cache] deleted 0 files" in r.getMessage() for r in caplog.records)

    def test_a_missing_cache_dir_is_not_an_error(self, tmp_path: Path, caplog) -> None:
        assert not (tmp_path / "data" / "cache").exists()
        context = _recorder(tmp_path)

        with caplog.at_level(logging.INFO):
            maint.purge_adapter_cache(context)

        assert any("[onebot_cache] deleted 0 files" in r.getMessage() for r in caplog.records)

    def test_a_relocated_index_is_reached_without_losing_the_cache_dir(self, tmp_path: Path, monkeypatch) -> None:
        # 改过位置的索引以前永远清不到，而留在 data/cache 里的那批还得照清。
        moved = tmp_path / "elsewhere" / "idx.json"
        monkeypatch.setenv("ONEBOT_REPLY_ATTACHMENT_CACHE_FILE", str(moved))
        _write(moved, age=3 * _DAY)
        leftover = _write(tmp_path / "elsewhere" / "idx.json.tmp", age=3 * _DAY)
        stale = _write(tmp_path / "data" / "cache" / "onebot_future.json", age=3 * _DAY)
        context = _recorder(tmp_path)

        maint.purge_adapter_cache(context)

        assert set(context.files) == {moved, leftover, stale}

    def test_the_cache_dir_comes_from_the_workspace_root_alone(self, tmp_path: Path) -> None:
        """索引默认位置和按前缀扫的目录必须同源；一个跟 workspace_root、一个跟 data_dir 就会各扫各的。"""
        index = _write(self._index(tmp_path), age=3 * _DAY)
        stale = _write(tmp_path / "data" / "cache" / "onebot_future.json", age=3 * _DAY)
        decoy = _write(tmp_path / "elsewhere" / "cache" / "onebot_future.json", age=3 * _DAY)
        context = _Recorder(workspace_root=tmp_path, data_dir=tmp_path / "elsewhere")

        maint.purge_adapter_cache(context)

        assert set(context.files) == {index, stale}
        assert decoy.exists()

    def test_the_env_pointing_back_at_the_default_file_deletes_it_once(
        self, tmp_path: Path, monkeypatch, caplog,
    ) -> None:
        """两条候选来源指向同一个文件，去重没做就会 deleted=2、第二次删还抛。"""
        index = _write(self._index(tmp_path), age=3 * _DAY)
        monkeypatch.setenv("ONEBOT_REPLY_ATTACHMENT_CACHE_FILE", str(index))
        context = _recorder(tmp_path)

        with caplog.at_level(logging.INFO):
            maint.purge_adapter_cache(context)

        assert context.files == [index]
        assert any("[onebot_cache] deleted 1 files" in r.getMessage() for r in caplog.records)

    def test_an_unreadable_candidate_does_not_abort_the_task(self, tmp_path: Path, caplog) -> None:
        _write(self._index(tmp_path), age=3 * _DAY)
        _write(self._index(tmp_path).with_suffix(".json.tmp"), age=3 * _DAY)

        class _Refusing(_Recorder):
            def remove_file(self, path: Path) -> None:
                raise OSError("busy")

        context = _Refusing(workspace_root=tmp_path, data_dir=tmp_path / "data")

        with caplog.at_level(logging.INFO):
            maint.purge_adapter_cache(context)

        assert any("[onebot_cache] deleted 0 files" in r.getMessage() for r in caplog.records)

    def test_a_dry_run_reports_instead_of_deleting(self, tmp_path: Path, monkeypatch, caplog) -> None:
        index = _write(self._index(tmp_path), age=3 * _DAY)
        future = _write(tmp_path / "data" / "cache" / "onebot_future.json", age=3 * _DAY)
        monkeypatch.setattr(cleanup, "DRY_RUN", True)
        context = cleanup._CleanupContext(workspace_root=tmp_path, data_dir=tmp_path / "data")

        with caplog.at_level(logging.INFO):
            maint.purge_adapter_cache(context)

        assert index.exists() and future.exists()
        reported = [r.getMessage() for r in caplog.records if "would delete file" in r.getMessage()]
        assert len(reported) == 2

    def test_a_real_run_really_deletes(self, tmp_path: Path, monkeypatch) -> None:
        index = _write(self._index(tmp_path), age=3 * _DAY)
        future = _write(tmp_path / "data" / "cache" / "onebot_future.json", age=3 * _DAY)
        monkeypatch.setattr(cleanup, "DRY_RUN", False)
        context = cleanup._CleanupContext(workspace_root=tmp_path, data_dir=tmp_path / "data")

        maint.purge_adapter_cache(context)

        assert not index.exists() and not future.exists()

    def test_the_pattern_covers_everything_the_platform_claims_in_the_cache_dir(self) -> None:
        """owned_files 在 data/cache 下新加一个前缀而清理没跟上，那批文件就再没人收。"""
        from fnmatch import fnmatch

        from platforms.builtin import QQ_NAPCAT

        prefix = "data/cache/"
        claimed = [glob for glob in QQ_NAPCAT.owned_files if glob.startswith(prefix)]
        assert claimed, "QQ 不再声明 data/cache 下的文件了，这条守卫要跟着改"
        for glob in claimed:
            rel = glob[len(prefix):]
            # 清理只扫一层，声明里带子目录就等于声明了一批清不到的文件。
            assert "/" not in rel, f"{glob} 在子目录里，按龄清理扫不到"
            assert fnmatch(rel, maint.ADAPTER_CACHE_PATTERN), f"{glob} 不在 {maint.ADAPTER_CACHE_PATTERN} 的范围里"


class TestFindingTheClientCacheRoots:
    def test_configured_roots_come_first_and_accept_both_separators(self, monkeypatch) -> None:
        monkeypatch.setattr(maint, "QQ_CACHE_ROOTS_RAW", "/a/qq , /b/napcat\n/c/qq")

        assert maint._discover_qq_cache_roots()[:3] == [Path("/a/qq"), Path("/b/napcat"), Path("/c/qq")]

    def test_the_packaged_defaults_are_always_candidates(self, monkeypatch) -> None:
        monkeypatch.setattr(maint, "QQ_CACHE_ROOTS_RAW", "")
        roots = maint._discover_qq_cache_roots()

        assert {Path("/app/.config/QQ"), Path("/app/napcat"), Path("/root/.config/QQ")} <= set(roots)

    def test_a_root_is_never_listed_twice(self, monkeypatch) -> None:
        monkeypatch.setattr(maint, "QQ_CACHE_ROOTS_RAW", "/app/napcat,/app/napcat")
        roots = maint._discover_qq_cache_roots()

        assert [str(root) for root in roots].count(str(Path("/app/napcat"))) == 1

    @pytest.mark.parametrize("raw, expected", [
        ("", []),
        ("   ", []),
        (",,", []),
        (" /a/qq ", [Path("/a/qq")]),
        ("/a/qq\n/b/qq", [Path("/a/qq"), Path("/b/qq")]),
    ])
    def test_the_configured_list_is_split_and_trimmed(self, raw: str, expected: list[Path]) -> None:
        assert maint._split_configured_roots(raw) == expected

    def test_an_empty_or_dot_path_is_not_a_root(self) -> None:
        # Path("") 是 "."，当成根就会从工作目录开始递归删。
        roots: list[Path] = []
        maint._append_unique_path(roots, Path(""))
        maint._append_unique_path(roots, Path("."))

        assert roots == []

    def test_proc_strings_are_split_on_nul(self, tmp_path: Path) -> None:
        path = tmp_path / "cmdline"
        path.write_bytes(b"/opt/QQ/qq\x00--user-data-dir=/app/.config/QQ\x00\x00")

        assert maint._read_proc_strings(path) == ["/opt/QQ/qq", "--user-data-dir=/app/.config/QQ"]

    def test_an_unreadable_proc_entry_is_empty(self, tmp_path: Path) -> None:
        assert maint._read_proc_strings(tmp_path / "gone") == []

    @pytest.mark.parametrize("root", ["/", "/app", "/root", "/proc", "", "/var/lib/other"])
    def test_a_root_outside_the_client_dirs_is_refused(self, root: str) -> None:
        assert maint._is_safe_qq_cache_root(Path(root)) is False

    @pytest.mark.parametrize("root", ["/app/.config/QQ", "/app/napcat", "/proc/7/root/app/NapCat"])
    def test_a_client_dir_is_accepted(self, root: str) -> None:
        assert maint._is_safe_qq_cache_root(Path(root)) is True


class TestRecognisingCacheLikePaths:
    @pytest.mark.parametrize("rel", [
        "Cache/x", "Code Cache/x", "CodeCache/x", "GPUCache/x", "blob_storage/x",
        "tmp/x", "Downloads/x", "thumbnails/x", "12345/Image/blob", "12345/Video/blob",
        "nt_qq/emoji/x", "record/x",
    ])
    def test_a_cache_directory_marks_the_file(self, rel: str) -> None:
        assert maint._is_cache_like_relative_path(Path(rel)) is True

    @pytest.mark.parametrize("rel", ["login.json", "nt_db/msg.db", "12345/config.json", "versions/app.asar"])
    def test_durable_state_is_not_cache(self, rel: str) -> None:
        assert maint._is_cache_like_relative_path(Path(rel)) is False

    def test_only_the_parent_directories_count(self) -> None:
        # 文件名自己带 cache 不算：nt_db/msg_cache.db 是数据库不是缓存。
        assert maint._is_cache_like_relative_path(Path("nt_db/msg_cache.db")) is False


class TestPurgingTheClientCache:
    @pytest.fixture
    def root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        # 安全判定要求根路径里有 qq/napcat，随机的 tmp 目录名满足不了。
        target = tmp_path / "napcat"
        target.mkdir()
        monkeypatch.setattr(maint, "QQ_CACHE_MAX_AGE", 7.0 * _DAY)
        monkeypatch.setattr(maint, "_discover_qq_cache_roots", lambda: [target])
        return target

    def test_an_old_media_file_goes_through_the_context(self, tmp_path: Path, root: Path) -> None:
        stale = _write(root / "old.jpg", age=30 * _DAY)
        context = _recorder(tmp_path)

        maint.purge_qq_internal_cache(context)

        assert context.files == [stale]
        assert stale.exists()

    def test_an_old_file_inside_a_cache_directory_goes_too(self, tmp_path: Path, root: Path) -> None:
        # NTQQ 的 <qq号>/Image 里的文件常常没有扩展名，只能靠目录名认出来。
        stale = _write(root / "12345" / "Image" / "blob", age=30 * _DAY)
        context = _recorder(tmp_path)

        maint.purge_qq_internal_cache(context)

        assert context.files == [stale]

    def test_a_recent_file_stays(self, tmp_path: Path, root: Path) -> None:
        _write(root / "fresh.jpg")
        context = _recorder(tmp_path)

        maint.purge_qq_internal_cache(context)

        assert context.files == []

    def test_login_state_is_never_touched(self, tmp_path: Path, root: Path) -> None:
        """整目录清掉就等于把机器人踢下线，还要人工重新扫码。"""
        _write(root / "config.json", age=365 * _DAY)
        _write(root / "nt_db" / "msg.db", age=365 * _DAY)
        context = _recorder(tmp_path)

        maint.purge_qq_internal_cache(context)

        assert context.files == []

    def test_an_emptied_cache_directory_is_collected(self, tmp_path: Path, root: Path) -> None:
        cache_dir = root / "12345" / "Image"
        cache_dir.mkdir(parents=True)
        context = _recorder(tmp_path)

        maint.purge_qq_internal_cache(context)

        assert context.dirs == [cache_dir]

    def test_the_root_itself_and_its_plain_dirs_survive(self, tmp_path: Path, root: Path) -> None:
        (root / "12345").mkdir()
        context = _recorder(tmp_path)

        maint.purge_qq_internal_cache(context)

        assert context.dirs == []

    def test_a_zero_max_age_disables_the_whole_task(self, tmp_path: Path, root: Path, monkeypatch, caplog) -> None:
        _write(root / "old.jpg", age=30 * _DAY)
        monkeypatch.setattr(maint, "QQ_CACHE_MAX_AGE", 0.0)
        context = _recorder(tmp_path)

        with caplog.at_level(logging.INFO):
            maint.purge_qq_internal_cache(context)

        assert context.files == []
        assert any("[qq_cache] disabled" in r.getMessage() for r in caplog.records)

    def test_it_reports_what_it_scanned(self, tmp_path: Path, root: Path, caplog) -> None:
        _write(root / "old.jpg", "12345", age=30 * _DAY)

        with caplog.at_level(logging.INFO):
            maint.purge_qq_internal_cache(_recorder(tmp_path))

        assert any(
            "[qq_cache] scanned_roots=1 deleted=1" in r.getMessage() for r in caplog.records
        ), [r.getMessage() for r in caplog.records]

    def test_a_root_that_is_not_a_client_dir_is_skipped(self, tmp_path: Path, monkeypatch, caplog) -> None:
        other = tmp_path / "somewhere"
        _write(other / "old.jpg", age=30 * _DAY)
        monkeypatch.setattr(maint, "QQ_CACHE_MAX_AGE", 7.0 * _DAY)
        monkeypatch.setattr(maint, "_discover_qq_cache_roots", lambda: [other])
        context = _recorder(tmp_path)

        with caplog.at_level(logging.INFO):
            maint.purge_qq_internal_cache(context)

        assert context.files == []
        assert any("scanned_roots=0" in r.getMessage() for r in caplog.records)

    def test_a_missing_root_is_skipped(self, tmp_path: Path, monkeypatch, caplog) -> None:
        monkeypatch.setattr(maint, "QQ_CACHE_MAX_AGE", 7.0 * _DAY)
        monkeypatch.setattr(maint, "_discover_qq_cache_roots", lambda: [tmp_path / "napcat-gone"])

        with caplog.at_level(logging.INFO):
            maint.purge_qq_internal_cache(_recorder(tmp_path))

        assert any("scanned_roots=0" in r.getMessage() for r in caplog.records)

    def test_a_dry_run_reports_instead_of_deleting(self, tmp_path: Path, root: Path, monkeypatch, caplog) -> None:
        stale = _write(root / "old.jpg", age=30 * _DAY)
        empty = root / "12345" / "Image"
        empty.mkdir(parents=True)
        monkeypatch.setattr(cleanup, "DRY_RUN", True)
        context = cleanup._CleanupContext(workspace_root=tmp_path, data_dir=tmp_path / "data")

        with caplog.at_level(logging.INFO):
            maint.purge_qq_internal_cache(context)

        assert stale.exists() and empty.exists()
        messages = [r.getMessage() for r in caplog.records]
        assert any("would delete file" in m for m in messages)
        assert any("would remove empty dir" in m for m in messages)

    def test_a_real_run_really_deletes(self, tmp_path: Path, root: Path, monkeypatch) -> None:
        stale = _write(root / "12345" / "Image" / "blob", age=30 * _DAY)
        monkeypatch.setattr(cleanup, "DRY_RUN", False)
        context = cleanup._CleanupContext(workspace_root=tmp_path, data_dir=tmp_path / "data")

        maint.purge_qq_internal_cache(context)

        assert not stale.exists()
        # 文件删完这一层就空了，同一趟里连空目录一起收走。
        assert not stale.parent.exists()
        assert root.exists()
