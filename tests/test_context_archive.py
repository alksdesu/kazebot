from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine import context_archive
from engine.context_archive import ContextArchiveStore, MAX_PAGE_CHARS
from engine.conversation_store import Message
from toolbox.builtins.context_archive import read_context_archive, search_context_archive
from toolbox.context import ToolContext


@pytest.fixture
def archive_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ContextArchiveStore:
    monkeypatch.setattr(context_archive, "load_runtime_config", lambda root: {})
    return ContextArchiveStore(tmp_path)


def _records() -> list[dict]:
    return [
        {
            "id": "u1", "role": "user", "content": "修复 100%_literal 中文检索",
            "created_at": "2024-01-02T03:00:00+08:00", "meta": {"message_id": "qq1"},
            "source_task_id": "t1",
        },
        {
            "id": "a1", "role": "assistant", "content": "", "created_at": "2024-01-02T03:01:00+08:00",
            "tool_calls": [{"id": "call1", "function": {"name": "read_file", "arguments": '{"path":"x.py"}'}}],
            "source_task_id": "t1",
        },
        {
            "id": "tool1", "role": "tool", "content": [{"type": "text", "text": "完整结果 STRASSE"}],
            "tool_call_id": "call1", "name": "read_file", "created_at": "2024-01-02T03:02:00+08:00",
            "meta": {"cost": 12}, "source_task_id": "t1",
        },
    ]


def test_archive_preserves_original_records_and_message_objects(archive_store: ContextArchiveStore) -> None:
    records = _records()
    records.append(Message(id="summary1", role="user", content="旧摘要", meta={"summary": True}).to_dict())
    receipt = archive_store.archive("s1", [*records[:-1], Message.from_dict(records[-1])], reason="compact")
    page = archive_store.read("s1", receipt["archive_id"])
    assert [entry["message"] for entry in page["messages"]] == records
    assert page["next_offset"] is None


def test_archive_idempotency_does_not_extend_retention(archive_store: ContextArchiveStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(context_archive.time, "time", lambda: 1_000_000)
    first = archive_store.archive("s1", _records(), reason="compact", operation_id="operation1")
    monkeypatch.setattr(context_archive.time, "time", lambda: 1_000_100)
    second = archive_store.archive("s1", _records(), reason="compact", operation_id="operation1")
    third = archive_store.archive("s1", _records(), reason="compact")
    assert first == second == third
    with pytest.raises(ValueError, match="different snapshot"):
        archive_store.archive("s1", [], reason="compact", operation_id="operation1")


@pytest.mark.parametrize("attempt", range(6))
def test_concurrent_archives_are_atomic_and_deduplicated(archive_store: ContextArchiveStore, attempt: int) -> None:
    def save(_: int) -> dict:
        return archive_store.archive("s1", _records(), reason="compact")

    with ThreadPoolExecutor(max_workers=6) as executor:
        receipts = list(executor.map(save, range(12)))
    assert len({receipt["archive_id"] for receipt in receipts}) == 1
    with sqlite3.connect(archive_store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM archives").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3


def test_concurrent_processes_initialize_once(archive_store: ContextArchiveStore) -> None:
    script = """
import json
import sys
from pathlib import Path
from engine.context_archive import ContextArchiveStore
print("ready", flush=True)
sys.stdin.readline()
receipt = ContextArchiveStore(Path(sys.argv[1])).archive(
    "s1", [{"id": "m1", "role": "user", "content": "original"}], reason="compact")
print(json.dumps(receipt), flush=True)
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(archive_store.workspace_root)],
            cwd=_ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
        )
        for _ in range(4)
    ]
    try:
        for process in processes:
            assert process.stdout.readline().strip() == "ready"
        for process in processes:
            process.stdin.write("\n")
            process.stdin.flush()
        receipts = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, stderr
            receipts.append(json.loads(stdout))
        assert len({receipt["archive_id"] for receipt in receipts}) == 1
        assert archive_store.read("s1", receipts[0]["archive_id"])["message_count"] == 1
        with sqlite3.connect(archive_store.path) as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)


def test_initialized_reader_does_not_wait_for_archive_writer(archive_store: ContextArchiveStore) -> None:
    archive_store.archive("s1", _records(), reason="compact")
    with ThreadPoolExecutor(max_workers=1) as executor:
        with archive_store._connect(create=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE archives SET reason='uncommitted' WHERE session_id='s1'")
            page = executor.submit(archive_store.search, "s1").result(timeout=3)
            assert len(page["matches"]) == 3
            connection.rollback()


@pytest.mark.parametrize("operation_id", ["", "operation1"])
@pytest.mark.parametrize("already_purged", [False, True])
def test_expired_identical_snapshot_gets_new_readable_archive(
    archive_store: ContextArchiveStore, monkeypatch: pytest.MonkeyPatch,
    operation_id: str, already_purged: bool,
) -> None:
    now = 1_000_000
    monkeypatch.setattr(context_archive.time, "time", lambda: now)
    first = archive_store.archive("s1", _records(), reason="compact", operation_id=operation_id)
    now += 7 * 86400 + 1
    if already_purged:
        assert archive_store.purge() == 1
    second = archive_store.archive("s1", _records(), reason="compact", operation_id=operation_id)
    assert first["archive_id"] != second["archive_id"]
    assert first["created_at"] != second["created_at"]
    assert archive_store.read("s1", second["archive_id"])["message_count"] == 3
    with pytest.raises(FileNotFoundError):
        archive_store.read("s1", first["archive_id"])
    now += 10
    assert archive_store.archive("s1", _records(), reason="compact", operation_id=operation_id) == second
    with sqlite3.connect(archive_store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM archives").fetchone()[0] == 1


def test_failed_expired_archive_replacement_rolls_back(archive_store: ContextArchiveStore, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_000_000
    monkeypatch.setattr(context_archive.time, "time", lambda: now)
    first = archive_store.archive("s1", _records(), reason="compact")
    with sqlite3.connect(archive_store.path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_insert BEFORE INSERT ON messages BEGIN SELECT RAISE(ABORT, 'disk failure'); END"
        )
    now += 7 * 86400 + 1
    with pytest.raises(sqlite3.DatabaseError, match="disk failure"):
        archive_store.archive("s1", _records(), reason="compact")
    with sqlite3.connect(archive_store.path) as connection:
        assert connection.execute("SELECT id FROM archives").fetchone()[0] == first["archive_id"]
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3


def test_unversioned_archive_schema_is_adopted_without_data_loss(archive_store: ContextArchiveStore) -> None:
    receipt = archive_store.archive("s1", _records(), reason="compact")
    with sqlite3.connect(archive_store.path) as connection:
        connection.execute("PRAGMA user_version=0")
    assert archive_store.read("s1", receipt["archive_id"])["message_count"] == 3
    with sqlite3.connect(archive_store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_failed_archive_does_not_replace_existing_snapshot(archive_store: ContextArchiveStore) -> None:
    receipt = archive_store.archive("s1", _records(), reason="compact")
    with sqlite3.connect(archive_store.path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_insert BEFORE INSERT ON messages BEGIN SELECT RAISE(ABORT, 'disk failure'); END"
        )
    with pytest.raises(sqlite3.DatabaseError, match="disk failure"):
        archive_store.archive("s1", [{"id": "new", "content": "new"}], reason="compact")
    with sqlite3.connect(archive_store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM archives").fetchone()[0] == 1
    assert archive_store.read("s1", receipt["archive_id"])["message_count"] == 3


def test_search_uses_scope_literal_keyword_and_original_timestamps(archive_store: ContextArchiveStore) -> None:
    receipt = archive_store.archive("s1", _records(), reason="compact")
    archive_store.archive("s2", [{"id": "other", "content": "private other session"}], reason="compact")
    assert archive_store.search("s1", "private")["matches"] == []
    assert len(archive_store.search("s1", "100%_literal")["matches"]) == 1
    assert len(archive_store.search("s1", "中文")["matches"]) == 1
    assert len(archive_store.search("s1", "straße")["matches"]) == 1
    assert archive_store.search("s1", "' OR 1=1--")["matches"] == []
    filtered = archive_store.search("s1", after="2024-01-01T19:01:00Z", before="2024-01-01T19:01:00Z")
    assert [item["message_id"] for item in filtered["matches"]] == ["a1"]
    assert "read_file" in filtered["matches"][0]["excerpt"]
    assert filtered["matches"][0]["archive_id"] == receipt["archive_id"]
    with pytest.raises(FileNotFoundError):
        archive_store.read("s2", receipt["archive_id"])


def test_expiry_uses_archive_time_and_purge_respects_dry_run(
    archive_store: ContextArchiveStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_000_000
    monkeypatch.setattr(context_archive.time, "time", lambda: now)
    receipt = archive_store.archive("s1", _records(), reason="compact")
    assert len(archive_store.search("s1")["matches"]) == 3
    now += 7 * 86400
    assert archive_store.search("s1")["matches"] == []
    with pytest.raises(FileNotFoundError):
        archive_store.read("s1", receipt["archive_id"])
    assert archive_store.purge(dry_run=True) == 1
    with sqlite3.connect(archive_store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3
    assert archive_store.purge() == 1
    with sqlite3.connect(archive_store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_retention_changes_apply_to_existing_archives(archive_store: ContextArchiveStore, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_000_000
    monkeypatch.setattr(context_archive.time, "time", lambda: now)
    archive_store.archive("s1", _records(), reason="compact")
    now += 2 * 86400
    monkeypatch.setattr(context_archive, "load_runtime_config", lambda root: {"engine": {"context_archive": {"retention_days": 1}}})
    assert archive_store.search("s1")["matches"] == []
    assert archive_store.purge() == 1


def test_large_messages_are_fully_recoverable_in_bounded_pages(archive_store: ContextArchiveStore) -> None:
    large = {"id": "large", "role": "tool", "content": "\\\"\x00中文" * 12_000, "tool_call_id": "call1"}
    receipt = archive_store.archive("s1", [large, {"id": "last", "content": "last"}], reason="snip")
    offset = char_offset = 0
    chunks: list[str] = []
    normal = []
    for _ in range(200):
        page = archive_store.read("s1", receipt["archive_id"], offset=offset, char_offset=char_offset)
        assert len(json.dumps(page, ensure_ascii=False, separators=(",", ":"))) <= MAX_PAGE_CHARS
        assert page["messages"]
        for item in page["messages"]:
            if "data_json_chunk" in item:
                chunks.append(item["data_json_chunk"])
            else:
                normal.append(item["message"])
        offset, char_offset = page["next_offset"], page["next_char_offset"]
        if offset is None:
            break
    else:
        pytest.fail("Archive pagination did not finish")
    assert json.loads("".join(chunks)) == large
    assert normal == [{"id": "last", "content": "last"}]


def test_search_budget_and_pagination(archive_store: ContextArchiveStore) -> None:
    records = [{"id": f"m{i}", "content": "\x00" * 1500} for i in range(40)]
    archive_store.archive("s1", records, reason="compact")
    offset = 0
    found = []
    while offset is not None:
        page = archive_store.search("s1", offset=offset, limit=50)
        assert len(json.dumps(page, ensure_ascii=False)) <= MAX_PAGE_CHARS
        assert page["matches"]
        found.extend(item["message_id"] for item in page["matches"])
        offset = page["next_offset"]
    assert found == [record["id"] for record in records]


@pytest.mark.parametrize("session_id", ["../s1", "/etc/passwd", "", "s1\n", "..\\s1", "x" * 257])
def test_invalid_session_is_rejected(archive_store: ContextArchiveStore, session_id: str) -> None:
    with pytest.raises(ValueError):
        archive_store.archive(session_id, [], reason="compact")
    with pytest.raises(ValueError):
        archive_store.search(session_id)


def test_archive_paths_cannot_escape_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    try:
        (workspace / "data").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks unavailable")
    with pytest.raises(ValueError, match="escapes workspace"):
        ContextArchiveStore(workspace).archive("s1", [], reason="compact")
    assert not (outside / "context_archive.sqlite3").exists()


def test_unknown_message_time_is_preserved_without_invention(archive_store: ContextArchiveStore) -> None:
    records = [{"id": "missing", "content": "old"}, {"id": "bad", "created_at": 1e99, "content": "bad"}]
    receipt = archive_store.archive("s1", records, reason="compact")
    assert all(item["created_at"] is None for item in archive_store.search("s1")["matches"])
    assert archive_store.search("s1", after="2020-01-01T00:00:00Z")["matches"] == []
    assert [item["message"] for item in archive_store.read("s1", receipt["archive_id"])["messages"]] == records


def test_reads_do_not_create_empty_database(archive_store: ContextArchiveStore) -> None:
    assert archive_store.search("s1")["matches"] == []
    assert archive_store.purge() == 0
    assert not archive_store.path.exists()


def test_history_metadata_time_is_searchable(archive_store: ContextArchiveStore) -> None:
    record = {"role": "user", "content": "legacy", "_meta": {"timestamp": "2024-01-01T10:00:00Z"}}
    archive_store.archive("s1", [record], reason="compact")
    result = archive_store.search("s1", after="2024-01-01T09:59:00Z", before="2024-01-01T10:01:00Z")
    assert len(result["matches"]) == 1
    assert result["matches"][0]["created_at"] == "2024-01-01T10:00:00+00:00"


def test_purge_session_removes_only_selected_archive(archive_store: ContextArchiveStore) -> None:
    first = archive_store.archive("s1", _records(), reason="compact")
    second = archive_store.archive("s2", _records(), reason="compact")
    assert archive_store.purge_session("s1") == 1
    assert archive_store.purge_session("s1") == 0
    with pytest.raises(FileNotFoundError):
        archive_store.read("s1", first["archive_id"])
    assert archive_store.read("s2", second["archive_id"])["message_count"] == 3


def test_all_default_tool_enabled_nodes_can_retrieve_archives() -> None:
    import yaml

    from toolbox.registry import ToolRegistry

    registered = {}

    class Registry:
        def register_builtin_tool(self, name, description, schema, func):
            registered[name] = schema

    from toolbox.builtins.context_archive import register_tools

    register_tools(Registry())
    assert set(registered) == {"search_context_archive", "read_context_archive"}
    for schema in registered.values():
        assert "session_id" not in schema["properties"]
        assert schema["additionalProperties"] is False
    for path in (_ROOT / "defaults" / "nodes").glob("*.yaml"):
        node = yaml.safe_load(path.read_text(encoding="utf-8"))
        access = node.get("tool_access", {})
        if access.get("mode") == "allowlist":
            assert set(registered) <= set(access.get("allow", [])), path.name


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_id", "bound_id", "expected"),
    [("branch", "parent", "parent"), ("child", "child", "child"), ("child", "", "child")],
)
async def test_tools_use_trusted_compaction_scope(
    archive_store: ContextArchiveStore, session_id: str, bound_id: str, expected: str,
) -> None:
    parent = archive_store.archive("parent", [{"id": "parent", "content": "parent secret"}], reason="compact")
    child = archive_store.archive("child", [{"id": "child", "content": "child secret"}], reason="compact")
    ctx = ToolContext(
        supervisor_url="", session_id=session_id, parent_session_id="parent", run_id="r", worker_id="w",
        workspace_root=archive_store.workspace_root, http=None, registry=None,
    )
    if bound_id:
        ctx._context_archive_session_id = bound_id
    response = await search_context_archive({"session_id": "parent"}, ctx)
    result = json.loads(response["data"]["result"])
    assert [entry["message_id"] for entry in result["matches"]] == [expected]
    allowed, denied = (parent, child) if expected == "parent" else (child, parent)
    assert (await read_context_archive({"archive_id": allowed["archive_id"]}, ctx))["ok"]
    with pytest.raises(FileNotFoundError):
        await read_context_archive({"archive_id": denied["archive_id"], "session_id": "parent"}, ctx)


def test_cleanup_entry_uses_current_workspace_and_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from engine import data_cleanup

    called = []
    monkeypatch.setattr(data_cleanup, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(data_cleanup, "DRY_RUN", True)
    monkeypatch.setattr(ContextArchiveStore, "purge", lambda self, dry_run=False: called.append((self.workspace_root, dry_run)) or 2)
    data_cleanup.purge_expired_context_archives()
    assert called == [(tmp_path.resolve(), True)]
