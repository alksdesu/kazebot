"""隔离副本用到的 git 操作：相对基准提交的改动（已提交、未提交、删掉的、未跟踪的、二进制的都算，被忽略的和链进来的目录不算），
补丁在原仓库上试套、套上和冲突报告，建 worktree 时上次留下的分支，删目录时只拆链接、绝不进链接指向的目录。"""
from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _worktree_fixture import branches, git, head, make_repo, requires_git, write  # noqa: E402
from supervisor.worktrees import git as wt_git  # noqa: E402

BINARY = b"\x00\x01\x02binary\x00\xff"


@pytest.fixture
def checkout(tmp_path: Path) -> SimpleNamespace:
    """原仓库，以及从它的 HEAD 开出来的一个 worktree。"""
    repo = make_repo(tmp_path / "repo")
    base = head(repo)
    worktree = tmp_path / "repo-worktrees" / "repo-key" / "task-1"
    wt_git.add_worktree(repo, worktree, wt_git.branch_name("task-1"), base)
    return SimpleNamespace(
        tmp=tmp_path, repo=repo, base=base, wt=worktree, patch=tmp_path / "repo-worktrees" / "repo-key" / "task-1.patch",
    )


def _status_lines(repo: Path) -> list[str]:
    return git(repo, "status", "--porcelain").splitlines()


@requires_git
class TestComputeDiff:
    def test_every_kind_of_change_is_listed(self, checkout: SimpleNamespace) -> None:
        wt = checkout.wt
        write(wt / "engine" / "x.py", "X = 2\n")
        git(wt, "commit", "-qam", "change x")
        write(wt / "README.md", "hello\nworld\n")
        (wt / "app" / "old.py").unlink()
        write(wt / "app" / "new.py", "NEW = 1\n")
        write(wt / "assets" / "logo.bin", BINARY)
        write(wt / "data" / "cache.txt", "ignored by .gitignore\n")

        summary = wt_git.compute_diff(wt, checkout.base, checkout.patch)

        files = {item.path: item for item in summary.files}
        assert set(files) == {"engine/x.py", "README.md", "app/old.py", "app/new.py", "assets/logo.bin"}
        assert files["engine/x.py"].to_data() == {"path": "engine/x.py", "status": "M", "added": 1, "deleted": 1}
        assert files["README.md"].to_data() == {"path": "README.md", "status": "M", "added": 1, "deleted": 0}
        assert files["app/old.py"].to_data() == {"path": "app/old.py", "status": "D", "added": 0, "deleted": 1}
        assert files["app/new.py"].to_data() == {"path": "app/new.py", "status": "A", "added": 1, "deleted": 0}
        assert files["assets/logo.bin"].to_data() == {"path": "assets/logo.bin", "status": "A", "binary": True}
        assert (summary.added, summary.deleted, summary.gitlinks) == (3, 2, [])

        data = checkout.patch.read_bytes()
        assert summary.patch_bytes == len(data)
        assert summary.digest == hashlib.sha256(data).hexdigest()
        assert b"GIT binary patch" in data and b"b/app/new.py" in data
        assert b"data/cache.txt" not in data

    def test_the_worktree_index_is_left_alone(self, checkout: SimpleNamespace) -> None:
        wt = checkout.wt
        (wt / "app" / "old.py").unlink()
        write(wt / "app" / "new.py", "NEW = 1\n")

        wt_git.compute_diff(wt, checkout.base, checkout.patch)

        lines = _status_lines(wt)
        assert "?? app/new.py" in lines and " D app/old.py" in lines

    def test_the_same_tree_gives_the_same_digest(self, checkout: SimpleNamespace) -> None:
        write(checkout.wt / "app" / "new.py", "NEW = 1\n")

        first = wt_git.compute_diff(checkout.wt, checkout.base, checkout.patch)
        second = wt_git.compute_diff(checkout.wt, checkout.base, checkout.patch)
        write(checkout.wt / "app" / "new.py", "NEW = 2\n")
        third = wt_git.compute_diff(checkout.wt, checkout.base, checkout.patch)

        assert first.digest == second.digest != third.digest

    def test_no_changes_means_an_empty_patch(self, checkout: SimpleNamespace) -> None:
        summary = wt_git.compute_diff(checkout.wt, checkout.base, checkout.patch)

        assert summary.files == () and summary.patch_bytes == 0
        assert checkout.patch.read_bytes() == b""
        assert summary.digest == hashlib.sha256(b"").hexdigest()

    def test_excluded_directories_do_not_count(self, checkout: SimpleNamespace) -> None:
        write(checkout.wt / "vendor" / "lib.js", "x\n")
        write(checkout.wt / "app" / "new.py", "NEW = 1\n")

        summary = wt_git.compute_diff(checkout.wt, checkout.base, checkout.patch, excluded=("vendor", "/missing/", ""))

        assert [item.path for item in summary.files] == ["app/new.py"]

    def test_a_linked_directory_is_left_out_and_untouched(self, checkout: SimpleNamespace) -> None:
        shared = checkout.tmp / "shared-deps"
        write(shared / "pkg" / "index.js", "module.exports = 1\n")
        wt_git.make_dir_link(checkout.wt / "deps", shared)
        write(checkout.wt / "app" / "new.py", "NEW = 1\n")

        summary = wt_git.compute_diff(checkout.wt, checkout.base, checkout.patch, excluded=("deps",))

        assert [item.path for item in summary.files] == ["app/new.py"]
        assert (shared / "pkg" / "index.js").read_text(encoding="utf-8") == "module.exports = 1\n"

    def test_a_linked_directory_the_gitignore_already_covers_is_left_out_too(self, checkout: SimpleNamespace) -> None:
        shared = checkout.tmp / "shared-node-modules"
        write(shared / "pkg" / "index.js", "module.exports = 1\n")
        wt_git.make_dir_link(checkout.wt / "node_modules", shared)
        write(checkout.wt / "app" / "new.py", "NEW = 1\n")

        summary = wt_git.compute_diff(checkout.wt, checkout.base, checkout.patch, excluded=("node_modules",))

        assert [item.path for item in summary.files] == ["app/new.py"]

    def test_a_change_that_keeps_the_size_and_the_timestamp_is_still_found(self, checkout: SimpleNamespace) -> None:
        target = checkout.wt / "app" / "main.py"
        stamp = target.stat().st_mtime_ns - 10_000_000_000
        os.utime(target, ns=(stamp, stamp))
        # 副本自己的索引记下这个时间戳，而且不算「和索引同一刻写下」
        git(checkout.wt, "update-index", "--refresh")
        original = target.read_bytes()
        changed = original.replace(b"return 1", b"return 7")
        assert len(changed) == len(original) and changed != original
        target.write_bytes(changed)
        os.utime(target, ns=(stamp, stamp))

        summary = wt_git.compute_diff(checkout.wt, checkout.base, checkout.patch)

        assert [item.path for item in summary.files] == ["app/main.py"]

    def test_a_base_that_is_not_a_commit_id_is_refused(self, checkout: SimpleNamespace) -> None:
        for base in ("HEAD", "abc123", ""):
            with pytest.raises(wt_git.GitError):
                wt_git.compute_diff(checkout.wt, base, checkout.patch)


@requires_git
class TestApply:
    def test_a_clean_repo_takes_the_whole_patch(self, checkout: SimpleNamespace) -> None:
        wt, repo = checkout.wt, checkout.repo
        write(wt / "README.md", "hello\nworld\n")
        (wt / "app" / "old.py").unlink()
        write(wt / "app" / "new.py", "NEW = 1\n")
        write(wt / "assets" / "logo.bin", BINARY)
        wt_git.compute_diff(wt, checkout.base, checkout.patch)

        assert wt_git.check_apply(repo, checkout.patch) == []
        assert (repo / "README.md").read_bytes() == b"hello\n"
        wt_git.apply_patch(repo, checkout.patch)

        assert (repo / "README.md").read_bytes() == b"hello\nworld\n"
        assert not (repo / "app" / "old.py").exists()
        assert (repo / "app" / "new.py").read_bytes() == b"NEW = 1\n"
        assert (repo / "assets" / "logo.bin").read_bytes() == BINARY
        assert git(repo, "diff", "--cached", "--name-only").strip() == ""

    def test_a_repo_that_moved_on_reports_each_conflict_and_changes_nothing(self, checkout: SimpleNamespace) -> None:
        wt, repo = checkout.wt, checkout.repo
        write(wt / "app" / "main.py", "def main():\n    return 2\n")
        write(wt / "app" / "new.py", "NEW = 1\n")
        (wt / "app" / "old.py").unlink()
        wt_git.compute_diff(wt, checkout.base, checkout.patch)
        write(repo / "app" / "main.py", "def main():\n    return 3\n")
        write(repo / "app" / "new.py", "OTHER = 1\n")

        conflicts = {item.path: item for item in wt_git.check_apply(repo, checkout.patch)}

        assert conflicts["app/main.py"].line == 1 and conflicts["app/main.py"].reason
        assert conflicts["app/new.py"].reason == "原仓库里已经有这个文件了"
        assert "app/old.py" not in conflicts
        assert (repo / "app" / "main.py").read_bytes() == b"def main():\n    return 3\n"
        assert (repo / "app" / "new.py").read_bytes() == b"OTHER = 1\n"
        assert (repo / "app" / "old.py").is_file()


class TestApplyErrors:
    def test_git_apply_messages_become_conflicts_per_file(self) -> None:
        stderr = "\n".join([
            "error: patch failed: app/main.py:12",
            "error: app/main.py: patch does not apply",
            "error: lib/gone.py: does not exist in index",
            "error: app/new.py: already exists in working directory",
            "error: docs/a b.md: patch does not apply",
            "warning: app/x.py has trailing whitespace",
        ])

        conflicts = wt_git.parse_apply_errors(stderr)

        assert [item.to_data() for item in conflicts] == [
            {"path": "app/main.py", "line": 12, "reason": "这一段和原仓库现在的内容对不上"},
            {"path": "lib/gone.py", "reason": "原仓库里没有这个文件了"},
            {"path": "app/new.py", "reason": "原仓库里已经有这个文件了"},
            {"path": "docs/a b.md", "reason": "补丁套不上"},
        ]

    def test_messages_it_does_not_know_are_kept_as_one_entry(self) -> None:
        assert wt_git.parse_apply_errors("error: corrupt patch at line 7\nerror: no valid patches\n") == [
            wt_git.ApplyConflict("", None, "corrupt patch at line 7；no valid patches"),
        ]
        assert wt_git.parse_apply_errors("") == []
        assert wt_git.parse_apply_errors("warning: nothing wrong\n") == []


class TestReadHead:
    def test_a_short_patch_comes_back_whole(self, tmp_path: Path) -> None:
        path = write(tmp_path / "a.patch", "line 1\nline 2\n")

        assert wt_git.read_head(path, 100) == ("line 1\nline 2\n", False)

    def test_a_long_patch_is_cut_at_a_line_end(self, tmp_path: Path) -> None:
        path = write(tmp_path / "a.patch", "".join(f"line {index}\n" for index in range(100)))

        text, truncated = wt_git.read_head(path, 50)

        assert truncated is True and text == "".join(f"line {index}\n" for index in range(7))

    def test_a_line_longer_than_the_limit_is_cut_inside_the_line(self, tmp_path: Path) -> None:
        path = write(tmp_path / "a.patch", "x" * 30 + "\n")

        assert wt_git.read_head(path, 10) == ("x" * 10, True)

    def test_the_limit_counts_characters_not_bytes(self, tmp_path: Path) -> None:
        path = write(tmp_path / "a.patch", "改动\n" * 3)

        assert wt_git.read_head(path, 9) == ("改动\n" * 3, False)
        assert wt_git.read_head(path, 6) == ("改动\n" * 2, True)

    def test_zero_means_nothing_inline(self, tmp_path: Path) -> None:
        path = write(tmp_path / "a.patch", "abc\n")

        assert wt_git.read_head(path, 0) == ("", True)


class TestNames:
    def test_the_repo_key_is_a_readable_name_plus_a_path_hash(self, tmp_path: Path) -> None:
        first = tmp_path / "one" / "My Repo!"
        second = tmp_path / "two" / "My Repo!"
        long_name = tmp_path / ("a" * 60)
        for path in (first, second, long_name):
            path.mkdir(parents=True)

        key = wt_git.repo_key(first)

        name, digest = key.rsplit("-", 1)
        assert name == "My-Repo" and len(digest) == 8 and int(digest, 16) >= 0
        assert wt_git.repo_key(first) == key != wt_git.repo_key(second)
        assert wt_git.repo_key(long_name).rsplit("-", 1)[0] == "a" * 32
        if os.name == "nt":
            assert wt_git.repo_key(Path(str(first).upper())) == key

    def test_branches_live_under_their_own_prefix(self) -> None:
        assert wt_git.branch_name("task-1") == "clonoth/task-1"

    def test_only_full_commit_ids_count_as_commits(self) -> None:
        assert wt_git.is_commit("0123456789abcdef0123456789abcdef01234567")
        assert wt_git.is_commit("0123456789ABCDEF0123456789ABCDEF01234567")
        assert wt_git.is_commit("a" * 64)
        for text in ("HEAD", "0123456", "g" * 40, "a" * 41, ""):
            assert not wt_git.is_commit(text), text


@requires_git
class TestWorktrees:
    def test_a_branch_left_from_an_earlier_attempt_is_recreated(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "repo")
        base = head(repo)
        git(repo, "branch", "clonoth/task-9", base)
        worktree = tmp_path / "wt" / "task-9"

        wt_git.add_worktree(repo, worktree, "clonoth/task-9", base)

        assert (worktree / "README.md").read_text(encoding="utf-8") == "hello\n"
        assert git(worktree, "rev-parse", "--abbrev-ref", "HEAD").strip() == "clonoth/task-9"
        assert git(worktree, "rev-parse", "HEAD").strip() == base

    def test_an_existing_directory_is_never_taken_over(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "repo")
        worktree = tmp_path / "wt" / "task-8"
        write(worktree / "keep.txt", "mine\n")

        with pytest.raises(wt_git.GitError):
            wt_git.add_worktree(repo, worktree, "clonoth/task-8", head(repo))

        assert (worktree / "keep.txt").read_text(encoding="utf-8") == "mine\n"

    def test_a_worktree_is_recognised_only_by_its_own_repo(self, checkout: SimpleNamespace) -> None:
        other = make_repo(checkout.tmp / "other")
        fake = checkout.tmp / "fake"
        write(fake / ".git", f"gitdir: {(checkout.tmp / 'elsewhere' / 'worktrees' / 'x').as_posix()}\n")

        assert wt_git.worktree_belongs_to(checkout.wt, checkout.repo) is True
        assert wt_git.worktree_belongs_to(checkout.wt, other) is False
        assert wt_git.worktree_belongs_to(checkout.repo, checkout.repo) is False
        assert wt_git.worktree_belongs_to(fake, checkout.repo) is False
        assert wt_git.worktree_belongs_to(checkout.tmp / "missing", checkout.repo) is False

    def test_removing_a_worktree_with_leftovers_and_its_branch(self, checkout: SimpleNamespace) -> None:
        repo = checkout.repo
        write(checkout.wt / "scratch.txt", "untracked\n")
        write(checkout.wt / "README.md", "changed\n")
        git(repo, "branch", "keep-me")

        assert wt_git.remove_worktree(repo, checkout.wt) is True
        wt_git.prune_worktrees(repo)

        assert not checkout.wt.exists()
        assert wt_git.delete_branch(repo, "clonoth/task-1") is True
        assert "clonoth/task-1" not in branches(repo)
        assert wt_git.delete_branch(repo, "keep-me") is False and "keep-me" in branches(repo)
        assert wt_git.delete_branch(repo, "clonoth/never-made") is False

    def test_toplevel_and_head(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path / "repo")
        empty = make_repo(tmp_path / "empty", commit=False)

        assert wt_git.toplevel(repo / "app") == repo.resolve()
        assert wt_git.toplevel(repo / "app" / "main.py") == repo.resolve()
        assert wt_git.head_commit(repo) == head(repo)
        with pytest.raises(wt_git.GitError):
            wt_git.head_commit(empty)


class TestRemoval:
    @pytest.fixture
    def shared(self, tmp_path: Path) -> Path:
        target = tmp_path / "shared"
        write(target / "keep.txt", "keep\n")
        write(target / "nested" / "deep.txt", "deep\n")
        return target

    def _intact(self, shared: Path) -> None:
        assert (shared / "keep.txt").read_text(encoding="utf-8") == "keep\n"
        assert (shared / "nested" / "deep.txt").read_text(encoding="utf-8") == "deep\n"

    def test_removing_a_tree_only_unlinks_the_links_inside(self, tmp_path: Path, shared: Path) -> None:
        root = tmp_path / "wt"
        write(root / "src" / "a.py", "A\n")
        wt_git.make_dir_link(root / "node_modules", shared)
        wt_git.make_dir_link(root / "src" / "deps", shared)

        assert {path.name for path in wt_git.links_under(root)} == {"node_modules", "deps"}
        wt_git.remove_tree(root)

        assert not os.path.lexists(root)
        self._intact(shared)

    def test_removing_a_link_leaves_what_it_points_at(self, tmp_path: Path, shared: Path) -> None:
        link = tmp_path / "link"
        wt_git.make_dir_link(link, shared)
        assert wt_git.is_link(link) and not wt_git.is_link(shared)

        wt_git.remove_link(link)

        assert not os.path.lexists(link)
        self._intact(shared)

    def test_a_tree_that_is_itself_a_link_is_only_unlinked(self, tmp_path: Path, shared: Path) -> None:
        link = tmp_path / "link-root"
        wt_git.make_dir_link(link, shared)

        wt_git.remove_tree(link)

        assert not os.path.lexists(link)
        self._intact(shared)

    def test_read_only_files_are_removed_too(self, tmp_path: Path) -> None:
        root = tmp_path / "wt"
        locked = write(root / "pack" / "objects.idx", "x")
        os.chmod(locked, stat.S_IREAD)

        wt_git.remove_tree(root)

        assert not os.path.lexists(root)

    def test_a_missing_tree_is_not_an_error(self, tmp_path: Path) -> None:
        wt_git.remove_tree(tmp_path / "never-made")
