"""Regression tests for persisted session entry-node routing.

[2026-05-21] These tests are written before the implementation because the
failure only appears after a supervisor restart: in-memory node overrides are
cleared, and inbound callbacks must still route through the session's own
entry_node_id instead of falling back to the global runtime default.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Why: tests are executed from a source checkout without an installed package.
# How: prepend the repository root to sys.path. Purpose: import the edited
# supervisor modules directly so the persistence regression is covered.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from supervisor._helpers import SessionInfo  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.session_store import SessionStore  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402


def _write_runtime_config(
    workspace: Path,
    entry_node_id: str = "global.default",
    vision_entry_node: str | None = None,
) -> None:
    """Write the smallest runtime config needed by entry-node routing tests."""
    # Why: the production default may change between deployments. How: tests write
    # an explicit shell.entry_node_id into the temporary workspace. Purpose: make
    # assertions about global fallback routing deterministic.
    path = workspace / "config" / "runtime.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f"shell:\n  entry_node_id: {entry_node_id}\n"
    if vision_entry_node is not None:
        text += (
            "routing:\n"
            "  vision:\n"
            "    enabled: true\n"
            f"    entry_node_id: {vision_entry_node}\n"
            "    channels:\n"
            "      qq_group:\n"
            f"        entry_node_id: {vision_entry_node}\n"
        )
    path.write_text(text, encoding="utf-8")


def _make_state(workspace: Path) -> SupervisorState:
    """Create a SupervisorState backed only by temporary persistence files."""
    # Why: entry_node_id must survive the same file-backed path used in production.
    # How: instantiate EventLog and SessionStore through SupervisorState using the
    # provided workspace. Purpose: cover restart behavior without touching live data.
    eventlog = EventLog(workspace / "data" / "events.jsonl", run_id="run-entry-persist")
    return SupervisorState(
        workspace_root=workspace,
        eventlog=eventlog,
        policy=PolicyEngine(workspace_root=workspace),
    )


def test_session_store_persists_and_restores_entry_node_id(tmp_path: Path) -> None:
    """SessionStore should write new entry_node_id values and read old files safely."""
    store_path = tmp_path / "data" / "sessions.json"
    store = SessionStore(store_path)
    created_at = datetime(2026, 5, 21, tzinfo=timezone.utc)

    store.on_session_created(
        SessionInfo(
            session_id="session-new",
            channel="discord",
            conversation_key="discord:new",
            created_at=created_at,
            updated_at=created_at,
            entry_node_id="persisted.entry",
        )
    )
    raw = json.loads(store_path.read_text(encoding="utf-8"))
    # Why: the restart path can only recover the route if sessions.json contains
    # the selected entry node. How: assert the serialized registry stores the new
    # field next to the existing session metadata. Purpose: prevent regressions in
    # the write path.
    assert raw["session-new"]["entry_node_id"] == "persisted.entry"

    raw["session-old"] = {
        "session_id": "session-old",
        "channel": "discord",
        "conversation_key": "discord:old",
        "created_at": created_at.isoformat(),
        "reset": False,
    }
    store_path.write_text(json.dumps(raw), encoding="utf-8")

    restored, conv_map, child_map, parent_children = SessionStore(store_path).load()

    # Why: existing deployments already have sessions.json entries without the new
    # field. How: missing entry_node_id is normalized to an empty string. Purpose:
    # old session registries continue loading after the schema extension.
    assert restored["session-new"].entry_node_id == "persisted.entry"
    assert restored["session-old"].entry_node_id == ""
    assert conv_map["discord:new"] == "session-new"
    assert child_map == {}
    assert parent_children == {}


def test_recorded_entry_node_is_used_after_restart(tmp_path: Path) -> None:
    """Inbound task creation should prefer the session record over the global default."""
    _write_runtime_config(tmp_path, entry_node_id="global.default")
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(channel="discord", conversation_key="discord:restart")
    state.sessions[session_id].entry_node_id = "recorded.entry"
    state._session_store.update_entry_node(session_id, "recorded.entry")

    restarted = _make_state(tmp_path)
    with restarted._lock:
        task = restarted._create_entry_task_for_inbound_locked(
            inbound_seq=1,
            session_id=session_id,
            payload={"text": "hello after restart"},
        )

    # Why: callbacks arriving after a supervisor restart no longer have the
    # in-memory override map. How: route selection reads SessionInfo.entry_node_id
    # before the global default. Purpose: ClonothZX sessions keep their original
    # node binding after process restarts.
    assert task is not None
    assert task.node_id == "recorded.entry"
    assert restarted.session_last_entry_node[session_id] == "recorded.entry"
    assert restarted.get_session_active_node(session_id)["node_id"] == "recorded.entry"


def test_dispatch_result_summary_only_inbound_preserves_structured_metadata(tmp_path: Path) -> None:
    """Dispatch-result inbounds should route even when raw child text is empty."""
    _write_runtime_config(tmp_path, entry_node_id="caller.entry")
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(channel="web", conversation_key="web:dispatch")

    with state._lock:
        task = state._create_entry_task_for_inbound_locked(
            inbound_seq=3,
            session_id=session_id,
            payload={
                "text": "",
                "summary": "child summary",
                "message_type": "dispatch_result",
                "caller_node_id": "caller.node",
                "child_node_id": "child.node",
                "child_task_id": "child-task-id",
                "child_session_id": "child-session-id",
            },
        )

    # [AutoC 2026-06-04] Why: after removing backend notification text, a child may
    # return only summary and no raw text. How: assert task creation still happens and
    # the inbound_* fields survive into runner input. Purpose: runner can build the
    # LLM-only English prefix while ConversationStore keeps raw text empty.
    assert task is not None
    assert task.input["instruction"] == ""
    assert task.input["inbound_message_type"] == "dispatch_result"
    assert task.input["inbound_summary"] == "child summary"
    assert task.input["inbound_caller_node_id"] == "caller.node"
    assert task.input["inbound_child_node_id"] == "child.node"
    assert task.input["inbound_child_task_id"] == "child-task-id"
    assert task.input["inbound_child_session_id"] == "child-session-id"


def test_first_inbound_records_entry_node_when_session_has_none(tmp_path: Path) -> None:
    """The first routed inbound should backfill entry_node_id into sessions.json."""
    _write_runtime_config(tmp_path, entry_node_id="global.default")
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(channel="discord", conversation_key="discord:first")

    with state._lock:
        task = state._create_entry_task_for_inbound_locked(
            inbound_seq=2,
            session_id=session_id,
            payload={"text": "hello", "entry_node_id": "frontend.entry"},
        )

    raw = json.loads((tmp_path / "data" / "sessions.json").read_text(encoding="utf-8"))
    # Why: some sessions are created before the frontend-selected entry node is
    # known. How: the first inbound writes the actual node used for routing back
    # into both memory and sessions.json. Purpose: a later restart can reproduce
    # the same route without relying on the inbound payload being resent.
    assert task is not None
    assert task.node_id == "frontend.entry"
    assert state.sessions[session_id].entry_node_id == "frontend.entry"
    assert raw[session_id]["entry_node_id"] == "frontend.entry"


def test_dispatch_inbound_task_context_carries_route_metadata(tmp_path: Path) -> None:
    """Dispatch-created task snapshots should expose structured parent route metadata."""
    _write_runtime_config(tmp_path, entry_node_id="global.default")
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(channel="discord", conversation_key="agent:coder:discord:parent")

    with state._lock:
        task = state._create_entry_task_for_inbound_locked(
            inbound_seq=3,
            session_id=session_id,
            payload={
                "channel": "discord",
                "conversation_key": "agent:coder:discord:parent",
                "text": "run delegated task",
                "entry_node_id": "coder.node",
                "dispatch_context_mode": "accumulate",
                "dispatch_origin": {
                    "parent_session_id": "parent-session",
                    "caller_node_id": "scout.node",
                    "parent_conversation_key": "discord:parent",
                    "context_mode": "accumulate",
                },
            },
        )

    # Why: EventRouter only sees task_created snapshots, not Supervisor's live Task
    # object after post-create mutation. How: assert route metadata is present in
    # both task.input and the durable task_created payload. Purpose: child sessions
    # can be mapped without reverse-parsing agent:-prefixed conversation keys.
    assert task is not None
    task_context = task.input["task_context"]
    assert task.input["_dispatch_origin"]["parent_conversation_key"] == "discord:parent"
    assert task_context["dispatch_context_mode"] == "accumulate"
    assert task_context["parent_conversation_key"] == "discord:parent"
    assert task_context["route_conversation_key"] == "discord:parent"

    created_events = [evt for evt in state.eventlog.list_all_events() if evt.get("type") == "task_created"]
    created_input = created_events[-1]["payload"]["input"]
    assert created_input["_dispatch_origin"]["parent_conversation_key"] == "discord:parent"
    assert created_input["task_context"]["route_conversation_key"] == "discord:parent"


def _draw_pinned_payload(*, pinned: bool) -> dict:
    payload = {
        "text": "/生图 画一只猫",
        "entry_node_id": "draw.novelai_planner",
        "channel": "qq_group",
        "attachments": [{"type": "image", "path": "a.png"}],
        "route_hints": {"has_image": True, "channel": "qq_group"},
    }
    if pinned:
        payload["entry_node_pinned"] = True
    return payload


def test_pinned_entry_node_survives_vision_routing(tmp_path: Path) -> None:
    """钉住的入口节点不被图片能力路由改写：带图 /生图 必须进绘图节点。"""
    _write_runtime_config(tmp_path, entry_node_id="global.default", vision_entry_node="qq.vision")
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(channel="qq_group", conversation_key="qq_group:pin")

    with state._lock:
        task = state._create_entry_task_for_inbound_locked(
            inbound_seq=1,
            session_id=session_id,
            payload=_draw_pinned_payload(pinned=True),
        )

    assert task is not None
    assert task.node_id == "draw.novelai_planner"


def test_unpinned_image_inbound_still_routes_to_vision(tmp_path: Path) -> None:
    """没钉住的带图消息仍走视觉节点：修复不能把视觉路由整体关掉。"""
    _write_runtime_config(tmp_path, entry_node_id="global.default", vision_entry_node="qq.vision")
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(channel="qq_group", conversation_key="qq_group:unpin")

    with state._lock:
        task = state._create_entry_task_for_inbound_locked(
            inbound_seq=1,
            session_id=session_id,
            payload=_draw_pinned_payload(pinned=False),
        )

    assert task is not None
    assert task.node_id == "qq.vision"


def test_pinned_entry_node_beats_session_override(tmp_path: Path) -> None:
    """AI switch_node 切过节点的会话，/生图 仍进绘图节点。"""
    _write_runtime_config(tmp_path, entry_node_id="global.default", vision_entry_node="qq.vision")
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(channel="qq_group", conversation_key="qq_group:override")
    state.session_entry_overrides[session_id] = "ai.switched"

    with state._lock:
        task = state._create_entry_task_for_inbound_locked(
            inbound_seq=1,
            session_id=session_id,
            payload=_draw_pinned_payload(pinned=True),
        )

    assert task is not None
    assert task.node_id == "draw.novelai_planner"


def test_switch_session_node_persists_and_clears_entry_node(tmp_path: Path) -> None:
    """switch_node persistence should survive restart and clear stale targets."""
    _write_runtime_config(tmp_path, entry_node_id="global.default")
    state = _make_state(tmp_path)
    session_id = state.get_or_create_session(channel="discord", conversation_key="discord:switch")

    result = state.switch_session_node(session_id, "switched.entry")
    raw = json.loads((tmp_path / "data" / "sessions.json").read_text(encoding="utf-8"))

    # Why: switch_node is the source of session-level entry overrides. How: the
    # selected target is written to SessionInfo and SessionStore immediately.
    # Purpose: restart clears volatile overrides without losing the intended route.
    assert result["ok"] is True
    assert state.sessions[session_id].entry_node_id == "switched.entry"
    assert raw[session_id]["entry_node_id"] == "switched.entry"
    assert _make_state(tmp_path).sessions[session_id].entry_node_id == "switched.entry"

    state.switch_session_node(session_id, "global.default")
    raw_after_clear = json.loads((tmp_path / "data" / "sessions.json").read_text(encoding="utf-8"))

    # Why: clearing an override must not leave an old target in sessions.json.
    # How: switching back to the runtime default persists an empty entry-node
    # marker. Purpose: restart fallback returns to the configured global default.
    assert state.sessions[session_id].entry_node_id == ""
    assert raw_after_clear[session_id]["entry_node_id"] == ""
