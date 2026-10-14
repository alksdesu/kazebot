from __future__ import annotations

from pathlib import Path

from toolbox.builtins.search_in_files import _collect_files, _match_pattern


def test_glob_matches_the_full_relative_path() -> None:
    assert _match_pattern("src/pkg/main.py", "src/**/*.py")
    assert not _match_pattern("tests/pkg/main.py", "src/**/*.py")
    assert _match_pattern("src/pkg/main.py", "**/*.py")


def test_collector_prunes_backups_databases_and_build_artifacts(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "dist.backup-2026").mkdir()
    (tmp_path / ".cache").mkdir()
    (tmp_path / "src" / "main.py").write_text("needle", encoding="utf-8")
    (tmp_path / "src" / "state.sqlite3").write_text("needle", encoding="utf-8")
    (tmp_path / "src" / "bundle.min.js").write_text("needle", encoding="utf-8")
    (tmp_path / "dist.backup-2026" / "old.py").write_text("needle", encoding="utf-8")
    (tmp_path / ".cache" / "old.py").write_text("needle", encoding="utf-8")

    files = list(_collect_files(tmp_path, "**/*.py", 100_000, tmp_path))

    assert [rel for _, rel in files] == ["src/main.py"]
