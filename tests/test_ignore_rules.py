"""遍历目录共用的忽略规则：.gitignore 各种写法按 ripgrep 默认口径认，只在 git 工作区里生效。

search_in_files 的 Python 那一路和 glob 都靠它；rg 那一路由 rg 自己认，两边给出的文件得是同一批。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from toolbox.ignore_rules import (  # noqa: E402
    DEFAULT_IGNORED_DIRS,
    GitignoreFile,
    GlobError,
    gitignore_applies,
    glob_regex,
    walk_files,
)


def _rules(text: str) -> GitignoreFile:
    return GitignoreFile.parse(text)


def _touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _repo(root: Path, gitignore: str = "") -> Path:
    (root / ".git").mkdir(parents=True, exist_ok=True)
    if gitignore:
        _touch(root / ".gitignore", gitignore)
    return root


def _walked(root: Path, **kwargs) -> list[str]:
    return [rel for _entry, rel in walk_files(root, **kwargs)]


class TestPatternSyntax:
    def test_a_bare_name_matches_at_any_depth(self) -> None:
        rules = _rules("foo\n")

        assert rules.match("foo", False) is True
        assert rules.match("a/foo", False) is True
        assert rules.match("a/b/foo", True) is True
        assert rules.match("foobar", False) is None
        # 目录里的文件靠遍历时剪掉目录排除，规则本身不匹配它们
        assert rules.match("foo/bar", False) is None

    def test_star_and_question_mark_stay_inside_one_directory(self) -> None:
        rules = _rules("a/*.py\nfile?.txt\n")

        assert rules.match("a/x.py", False) is True
        assert rules.match("a/b/x.py", False) is None
        assert rules.match("file1.txt", False) is True
        assert rules.match("deep/file1.txt", False) is True
        assert rules.match("file12.txt", False) is None

    def test_a_pattern_without_slash_matches_the_name_anywhere(self) -> None:
        rules = _rules("*.py\n")

        assert rules.match("x.py", False) is True
        assert rules.match("b/c/x.py", False) is True

    def test_negation_whitelists_and_the_last_matching_rule_wins(self) -> None:
        later_whitelist = _rules("*.log\n!keep.log\n")
        later_ignore = _rules("!keep.log\n*.log\n")

        assert later_whitelist.match("x.log", False) is True
        assert later_whitelist.match("keep.log", False) is False
        assert later_whitelist.match("sub/keep.log", False) is False
        assert later_ignore.match("keep.log", False) is True

    def test_a_trailing_slash_only_matches_directories(self) -> None:
        rules = _rules("build/\n")

        assert rules.match("build", True) is True
        assert rules.match("src/build", True) is True
        assert rules.match("build", False) is None

    def test_a_leading_slash_anchors_to_the_gitignore_directory(self) -> None:
        rules = _rules("/root.txt\n")

        assert rules.match("root.txt", False) is True
        assert rules.match("a/root.txt", False) is None

    def test_a_slash_in_the_middle_anchors_too(self) -> None:
        rules = _rules("doc/frotz\n")

        assert rules.match("doc/frotz", False) is True
        assert rules.match("a/doc/frotz", False) is None

    def test_double_star_forms(self) -> None:
        leading = _rules("**/foo\n")
        middle = _rules("a/**/b\n")
        trailing = _rules("abc/**\n")

        assert leading.match("foo", False) is True
        assert leading.match("x/y/foo", False) is True
        assert middle.match("a/b", False) is True
        assert middle.match("a/x/b", False) is True
        assert middle.match("a/x/y/b", False) is True
        assert middle.match("a/xb", False) is None
        assert trailing.match("abc/x", False) is True
        assert trailing.match("abc/x/y", False) is True
        # abc/** 只管里面的东西，不管 abc 本身
        assert trailing.match("abc", True) is None

    def test_character_classes(self) -> None:
        rules = _rules("[ab].txt\n[!ab].md\nlog[0-9].txt\na[/]b\n")

        assert rules.match("a.txt", False) is True
        assert rules.match("c.txt", False) is None
        assert rules.match("c.md", False) is True
        assert rules.match("a.md", False) is None
        assert rules.match("log5.txt", False) is True
        assert rules.match("logx.txt", False) is None
        # 字符类和 * 一样不跨目录
        assert rules.match("a/b", False) is None

    def test_braces_pick_one_alternative_like_ripgrep(self) -> None:
        rules = _rules("*.{log,tmp}\n")

        assert rules.match("a.log", False) is True
        assert rules.match("a.tmp", False) is True
        assert rules.match("a.txt", False) is None

    def test_escapes_comments_and_blank_lines(self) -> None:
        rules = _rules("# a comment\n\n\\#hash\n\\!bang\n")

        assert rules.match("#hash", False) is True
        assert rules.match("!bang", False) is True
        assert rules.match("# a comment", False) is None

    def test_trailing_spaces_are_trimmed_unless_escaped(self) -> None:
        rules = _rules("foo   \nbar\\ \n")

        assert rules.match("foo", False) is True
        assert rules.match("bar ", False) is True
        assert rules.match("bar", False) is None

    def test_an_escaped_trailing_slash_still_means_a_directory(self) -> None:
        rules = _rules("out\\/\n")

        assert rules.match("out", True) is True
        assert rules.match("out", False) is None

    def test_crlf_line_endings_and_a_bom_are_tolerated(self) -> None:
        rules = _rules("\ufeff*.tmp\r\n!keep.tmp\r\n")

        assert rules.match("x.tmp", False) is True
        assert rules.match("keep.tmp", False) is False

    def test_a_broken_line_is_skipped_and_the_rest_still_apply(self) -> None:
        rules = _rules("[unclosed\n*.tmp\n")

        assert rules.match("x.tmp", False) is True
        assert rules.match("[unclosed", False) is None

    def test_matching_is_case_sensitive(self) -> None:
        assert _rules("*.LOG\n").match("a.log", False) is None

    def test_an_empty_file_has_no_rules(self) -> None:
        assert not _rules("# only a comment\n\n")


class TestGlobRegex:
    @pytest.mark.parametrize("glob", ["[abc", "{a,b", "{a,{b,c}}", "trailing\\", "[z-a]"])
    def test_a_broken_glob_is_an_error(self, glob: str) -> None:
        with pytest.raises(GlobError):
            glob_regex(glob)

    def test_a_backslash_escapes_the_next_character(self) -> None:
        assert glob_regex("a\\*b").fullmatch("a*b")
        assert not glob_regex("a\\*b").fullmatch("axb")


class TestWhereGitignoreApplies:
    def test_outside_a_git_work_tree_gitignore_is_not_applied(self, tmp_path: Path) -> None:
        _touch(tmp_path / ".gitignore", "*.log\n")
        _touch(tmp_path / "a.log")
        _touch(tmp_path / "b.txt")

        assert gitignore_applies(tmp_path) is False
        assert _walked(tmp_path) == [".gitignore", "a.log", "b.txt"]

    def test_inside_a_work_tree_ignored_files_are_left_out(self, tmp_path: Path) -> None:
        _repo(tmp_path, "*.log\n")
        _touch(tmp_path / "a.log")
        _touch(tmp_path / "b.txt")

        assert gitignore_applies(tmp_path) is True
        assert _walked(tmp_path, skip_dir=lambda name: name == ".git") == [".gitignore", "b.txt"]

    def test_gitignore_false_yields_everything(self, tmp_path: Path) -> None:
        _repo(tmp_path, "*.log\n")
        _touch(tmp_path / "a.log")

        assert _walked(tmp_path, gitignore=False, skip_dir=lambda name: name == ".git") == [".gitignore", "a.log"]

    def test_rules_above_the_walk_root_apply_relative_to_their_own_directory(self, tmp_path: Path) -> None:
        _repo(tmp_path, "*.log\n/top.txt\nsub/inner.txt\n")
        _touch(tmp_path / "sub" / "x.log")
        _touch(tmp_path / "sub" / "inner.txt")
        _touch(tmp_path / "sub" / "top.txt")
        _touch(tmp_path / "sub" / "keep.py")

        assert gitignore_applies(tmp_path / "sub") is True
        assert _walked(tmp_path / "sub") == ["keep.py", "top.txt"]

    def test_a_nested_gitignore_overrides_its_parent(self, tmp_path: Path) -> None:
        _repo(tmp_path, "*.log\n")
        _touch(tmp_path / "keep.log")
        _touch(tmp_path / "sub" / ".gitignore", "!keep.log\n")
        _touch(tmp_path / "sub" / "keep.log")
        _touch(tmp_path / "sub" / "other.log")

        walked = _walked(tmp_path, skip_dir=lambda name: name == ".git")

        assert "sub/keep.log" in walked
        assert "sub/other.log" not in walked
        assert "keep.log" not in walked

    def test_files_under_an_ignored_directory_cannot_be_whitelisted(self, tmp_path: Path) -> None:
        _repo(tmp_path, "out/\n!out/keep.txt\n")
        _touch(tmp_path / "out" / "keep.txt")

        assert _walked(tmp_path, skip_dir=lambda name: name == ".git") == [".gitignore"]

    def test_a_nested_repository_does_not_inherit_the_outer_rules(self, tmp_path: Path) -> None:
        _repo(tmp_path, "*.log\n")
        _repo(tmp_path / "inner")
        _touch(tmp_path / "inner" / "a.log")
        _touch(tmp_path / "outer.log")

        walked = _walked(tmp_path, skip_dir=lambda name: name == ".git")

        assert "inner/a.log" in walked
        assert "outer.log" not in walked

    def test_a_repository_below_a_plain_directory_still_uses_its_rules(self, tmp_path: Path) -> None:
        _repo(tmp_path / "project", "*.tmp\n")
        _touch(tmp_path / "project" / "a.tmp")
        _touch(tmp_path / "project" / "a.py")
        _touch(tmp_path / "loose.tmp")

        walked = _walked(tmp_path, skip_dir=lambda name: name == ".git")

        assert walked == ["loose.tmp", "project/.gitignore", "project/a.py"]


class TestWalkOrder:
    def test_files_come_before_subdirectories_and_both_are_sorted(self, tmp_path: Path) -> None:
        for rel in ("b.txt", "a.txt", "z/c.txt", "m/d.txt", "m/n/e.txt"):
            _touch(tmp_path / rel)

        assert _walked(tmp_path) == ["a.txt", "b.txt", "m/d.txt", "m/n/e.txt", "z/c.txt"]

    def test_skipped_directory_names_are_not_entered(self, tmp_path: Path) -> None:
        _touch(tmp_path / "node_modules" / "x.js")
        _touch(tmp_path / "src" / "node_modules" / "y.js")
        _touch(tmp_path / "src" / "app.js")

        assert _walked(tmp_path, skip_dir=lambda name: name in DEFAULT_IGNORED_DIRS) == ["src/app.js"]

    def test_symbolic_links_are_not_followed(self, tmp_path: Path) -> None:
        target = tmp_path / "real"
        _touch(target / "inside.txt")
        _touch(tmp_path / "plain.txt")
        try:
            os.symlink(target, tmp_path / "linked_dir", target_is_directory=True)
            os.symlink(tmp_path / "plain.txt", tmp_path / "linked.txt")
        except (OSError, NotImplementedError):
            pytest.skip("这台机器上建不了软链接")

        assert _walked(tmp_path) == ["plain.txt", "real/inside.txt"]

    def test_an_unreadable_root_yields_nothing(self, tmp_path: Path) -> None:
        assert _walked(tmp_path / "missing") == []
