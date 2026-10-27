"""磁盘在解析半路被别的进程或任务动过时，按解析结果判归属的地方照样认得出自己目录里的路径。

否则文件工具把工作区里的路径判成无效或外部，编辑日志的防写守卫放行，各处目录报越界，密钥文件的兜底规则对不上号。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TESTS = Path(__file__).resolve().parent
for _p in (str(_ROOT), str(_TESTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _midway import change_midway, windows_only  # noqa: E402
from clonoth_runtime import classify_path  # noqa: E402
from engine import memory_namespace  # noqa: E402
from engine.context_archive import ContextArchiveStore  # noqa: E402
from engine.context_store import load_context_snapshot, write_context_snapshot  # noqa: E402
from engine.tool_outputs import ToolOutputStore, session_dir_name  # noqa: E402
from platforms.descriptor import CHANNEL_GROUP, ChannelDescriptor, PlatformDescriptor, SecretField  # noqa: E402
from platforms.protection import policy_floor  # noqa: E402
from plugin_runtime.packages import checked_directory  # noqa: E402
from toolbox import edit_journal  # noqa: E402

pytestmark = windows_only

_SIGNED = PlatformDescriptor(
    id="midway",
    display_name="Midway Chat",
    short_name="Midway",
    channels=(ChannelDescriptor("midway_room", CHANNEL_GROUP),),
    secrets=(
        SecretField(
            "signing_key", "签名密钥", env="MIDWAY_SIGNING", file="data/midway_signing_key", file_env="MIDWAY_SIGNING_FILE",
        ),
    ),
)


@pytest.mark.parametrize("absolute", [False, True])
def test_a_file_tool_writing_where_another_call_makes_the_folders_stays_in_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, absolute: bool,
) -> None:
    # 两个并行的 write_file 往同一个新目录里写：一个刚把目录建出来，另一个正在判路径。
    target = tmp_path / "out" / "sub" / "a.txt"
    fired = change_midway(monkeypatch, target, lambda: target.parent.mkdir(parents=True))

    found = classify_path(tmp_path, [], str(target) if absolute else "out/sub/a.txt")

    assert fired
    assert found == (target.resolve(), "out/sub/a.txt", False)


def test_the_edit_journal_still_refuses_writes_while_a_task_folder_is_being_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 守卫是拒绝清单，判成在外面就等于放行，编辑工具就能改掉 undo 要放回去的原文件。
    root = edit_journal.journal_root(tmp_path)
    root.mkdir(parents=True)
    target = root / "session" / "task" / "manifest.json"
    fired = change_midway(monkeypatch, target, lambda: target.parent.mkdir(parents=True))

    inside = edit_journal.inside_journal(tmp_path, target)

    assert fired
    assert inside


def test_an_edit_journal_task_folder_stays_inside_while_a_sibling_task_makes_the_session_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = edit_journal.EditJournal(tmp_path)
    session = journal.root / session_dir_name("s")
    own = session / session_dir_name("t1")
    fired = change_midway(monkeypatch, own, lambda: (session / session_dir_name("t2")).mkdir(parents=True))

    folder = journal.task_dir("s", "t1")

    assert fired
    assert folder == own


def test_a_tool_output_folder_stays_inside_while_another_session_makes_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ToolOutputStore(tmp_path)
    own = store.root / session_dir_name("a")
    fired = change_midway(monkeypatch, own, lambda: (store.root / session_dir_name("b")).mkdir(parents=True))

    folder = store.session_dir("a")

    assert fired
    assert folder == own


def test_a_memory_namespace_stays_inside_while_another_namespace_makes_the_memory_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = memory_namespace.memory_root(tmp_path)
    fired = change_midway(monkeypatch, root / "notebook_a", lambda: (root / "notebook_b").mkdir(parents=True))

    folder = memory_namespace.memory_dir(tmp_path, "notebook_a")

    assert fired
    assert folder == root / "notebook_a"


def test_a_context_snapshot_is_written_while_another_write_makes_its_session_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref = "data/node_contexts/s1/c1.json"
    fired = change_midway(monkeypatch, tmp_path / ref, lambda: (tmp_path / ref).parent.mkdir(parents=True))

    written = write_context_snapshot(tmp_path, ref, {"messages": []})

    assert fired
    assert written == ref
    assert load_context_snapshot(tmp_path, ref) == {"messages": [], "version": 1}


def test_a_plugin_version_folder_passes_while_another_version_of_the_plugin_is_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    version = plugins / ".packages" / "demo" / ("a" * 64)
    fired = change_midway(monkeypatch, version, lambda: version.with_name("b" * 64).mkdir(parents=True))

    checked = checked_directory(version, plugins)

    assert fired
    assert checked == version


def test_a_secret_file_moved_by_env_is_floored_by_its_workspace_name_while_its_folder_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 兜底规则在 policy 重载时算一次，算成带前缀的绝对路径就一直挡不住这份密钥，直到下次重载。
    moved = tmp_path / "keys" / "signing"
    fired = change_midway(monkeypatch, moved, moved.parent.mkdir)

    read, write = policy_floor(platforms=[_SIGNED], environ={"MIDWAY_SIGNING_FILE": "keys/signing"}, workspace=tmp_path)

    assert fired
    assert "keys/signing" in {rule.pattern for rule in read}
    assert not [rule.pattern for rule in (*read, *write) if rule.pattern.startswith("//?/")]


def test_the_context_archive_still_finds_its_wal_file_inside_while_sqlite_removes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 别的连接关掉时 SQLite 会删 -wal、-shm，归档库核对自己的文件时不能因此判成越界。
    store = ContextArchiveStore(tmp_path)
    wal = Path(f"{store.path}-wal")
    wal.parent.mkdir(parents=True)
    wal.write_bytes(b"")
    fired = change_midway(monkeypatch, wal, wal.unlink)

    purged = store.purge(dry_run=True)

    assert fired
    assert purged == 0
