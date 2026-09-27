"""模型在伪工具参数里点名的附件路径，和它直接调 read_file 走同一道策略。

reply / finish / dispatch 的 attachment_paths 以前直接落到磁盘回退上：只要路径在工作区里，
.env、data/config.yaml、别人的会话记录都能被当成附件发出去，而同一个模型调 read_file 读它们
是要过审批的。判定按解析后的真实位置做，换种写法或经 data/ 里的符号链接指过去都绕不开。
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.inference import attachment_access, pseudo_handlers  # noqa: E402
from engine.inference.attachment_access import (  # noqa: E402
    RefusedAttachment,
    screen_model_attachment_paths,
)
from engine.inference.loop_state import (  # noqa: E402
    TOOL_CONTEXT_EXTRA_ATTRS,
    _LoopState,
    build_tool_context,
)
from toolbox.context import ToolContext  # noqa: E402

_DATA_ONLY_REASON = "only files under data/ can be handed to a child node"
_MISSING_REASON = "no such file in the workspace"


def _symlinks_allowed() -> bool:
    with tempfile.TemporaryDirectory() as box:
        target = Path(box) / "target"
        target.write_text("x", encoding="utf-8")
        try:
            os.symlink(target, Path(box) / "link")
        except (OSError, NotImplementedError, AttributeError):
            return False
    return True


needs_symlink = pytest.mark.skipif(not _symlinks_allowed(), reason="本机不允许建符号链接")


class _Call:
    def __init__(self, name: str = "reply", call_id: str = "call-1") -> None:
        self.name = name
        self.id = call_id
        self.arguments: dict[str, Any] = {}


class _Guard:
    """request_guard 替身：按 parameters["path"] 决定放行还是拒绝。"""

    def __init__(self, *, denied: dict[str, dict] | None = None, boom: bool = False) -> None:
        self.denied = denied or {}
        self.boom = boom
        self.calls: list[dict[str, Any]] = []
        self.contexts: list[ToolContext] = []

    async def __call__(self, ctx, op, parameters):  # noqa: ANN001
        self.calls.append({"op": op, **parameters})
        self.contexts.append(ctx)
        if self.boom:
            raise RuntimeError("supervisor unreachable")
        return {}, self.denied.get(str(parameters.get("path") or ""))

    @property
    def paths(self) -> list[str]:
        return [call["path"] for call in self.calls]


@pytest.fixture()
def guard(monkeypatch: pytest.MonkeyPatch) -> _Guard:
    stub = _Guard()
    monkeypatch.setattr(attachment_access, "request_guard", stub)
    return stub


def _loop_state(
    workspace: Path,
    *,
    collected: list[dict] | None = None,
    task_context: dict | None = None,
    tool_produced: list[dict] | None = None,
    allowed_dispatch_targets: set[str] | None = None,
    http: Any = None,
) -> _LoopState:
    events: list[dict] = []

    async def _emit_event(type_: str, payload: dict) -> None:
        events.append({"type": type_, "payload": payload})

    rctx = SimpleNamespace(
        workspace_root=workspace,
        supervisor_url="http://supervisor.test",
        session_id="branch-1",
        parent_session_id="session-1",
        session_generation=7,
        worker_id="worker-9",
        task_id="task-42",
        http=http if http is not None else SimpleNamespace(),
        task_context=task_context if task_context is not None else {},
        emit_event=_emit_event,
        current_llm_request_id="llm-1",
    )
    node = SimpleNamespace(id="qq.orchestrator", extra={"memory_book": "profile"}, tool_mode="json")
    ls = _LoopState(
        rctx=rctx,
        node=node,
        provider=None,
        registry=_REGISTRY_SENTINEL,
        run_id="run-1",
        context_ref="",
        runtime_cfg={},
        streaming=False,
        messages=[],
        system_prompt=[],
        is_block_mode=False,
        openai_tools=[],
        history=[],
        collected_attachments=list(collected or []),
        tool_produced_attachments=list(tool_produced or []),
    )
    ls.allowed_dispatch_targets = allowed_dispatch_targets or set()
    ls.events = events  # type: ignore[attr-defined]
    return ls


_REGISTRY_SENTINEL = object()


def _screen(ls: _LoopState, paths: Any, *, name: str = "reply", data_dir_only: bool = False):
    return asyncio.run(screen_model_attachment_paths(
        ls, _Call(name), paths, data_dir_only=data_dir_only,
    ))


def _file(workspace: Path, rel: str, body: str = "x") -> Path:
    target = workspace / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def test_a_collected_attachment_is_handed_back_without_asking_policy(
    tmp_path: Path, guard: _Guard,
) -> None:
    """本任务自己收到的附件不必再判一次——用户刚发过来的图不该弹审批。"""
    ls = _loop_state(tmp_path, collected=[{"path": "data/attachments/s/a.png"}])

    kept, refused = _screen(ls, ["data/attachments/s/a.png"])

    assert kept == ["data/attachments/s/a.png"]
    assert refused == []
    assert guard.calls == []


def test_a_collected_attachment_passes_even_when_the_file_is_gone(
    tmp_path: Path, guard: _Guard,
) -> None:
    ls = _loop_state(tmp_path, collected=[{"path": "data/attachments/s/gone.png"}])

    kept, refused = _screen(ls, ["data/attachments/s/gone.png"])

    assert (kept, refused, guard.calls) == (["data/attachments/s/gone.png"], [], [])


def test_another_spelling_of_a_collected_attachment_is_still_free(
    tmp_path: Path, guard: _Guard,
) -> None:
    """同一个文件换种写法还是同一个文件，不该因为写法变了就去弹审批。"""
    _file(tmp_path, "data/a.png")
    ls = _loop_state(tmp_path, collected=[{"path": "data/a.png"}])

    kept, refused = _screen(ls, ["./data/../data/a.png", str(tmp_path / "data" / "a.png")])

    assert len(kept) == 2
    assert refused == []
    assert guard.calls == []


def test_a_path_outside_data_is_refused_for_dispatch_without_asking_policy(
    tmp_path: Path, guard: _Guard,
) -> None:
    _file(tmp_path, "notes/plan.md")
    ls = _loop_state(tmp_path)

    kept, refused = _screen(ls, ["notes/plan.md"], name="dispatch_to_scout", data_dir_only=True)

    assert kept == []
    assert refused == [RefusedAttachment("notes/plan.md", _DATA_ONLY_REASON)]
    assert guard.calls == []


def test_the_data_only_rule_is_off_for_reply_and_finish(tmp_path: Path, guard: _Guard) -> None:
    _file(tmp_path, "notes/plan.md")
    ls = _loop_state(tmp_path)

    kept, refused = _screen(ls, ["notes/plan.md"])

    assert kept == ["notes/plan.md"]
    assert refused == []
    assert guard.paths == ["notes/plan.md"]


@pytest.mark.parametrize("rel", ["data/nope.png", "data", "../outside.txt"])
def test_anything_that_is_not_a_file_in_the_workspace_is_refused(
    tmp_path: Path, guard: _Guard, rel: str,
) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path.parent / "outside.txt").write_text("secret", encoding="utf-8")
    ls = _loop_state(tmp_path)

    kept, refused = _screen(ls, [rel])

    assert kept == []
    assert refused == [RefusedAttachment(rel, _MISSING_REASON)]
    assert guard.calls == []


def test_an_absolute_path_outside_the_workspace_is_refused(tmp_path: Path, guard: _Guard) -> None:
    outside = tmp_path.parent / "elsewhere.txt"
    outside.write_text("secret", encoding="utf-8")
    ls = _loop_state(tmp_path)

    kept, refused = _screen(ls, [str(outside)])

    assert kept == []
    assert refused[0].reason == _MISSING_REASON
    assert guard.calls == []


def test_an_allowed_path_reaches_policy_as_its_workspace_relative_posix_path(
    tmp_path: Path, guard: _Guard,
) -> None:
    _file(tmp_path, "data/reports/q3.md")
    ls = _loop_state(tmp_path)

    kept, refused = _screen(ls, ["data/reports/../reports/q3.md"], name="finish")

    assert kept == ["data/reports/../reports/q3.md"]
    assert refused == []
    assert guard.calls == [{
        "op": "read_file",
        "path": "data/reports/q3.md",
        "tool_name": "finish",
        "reason": "finish attaching data/reports/q3.md",
    }]


def test_a_denied_path_is_refused_with_the_policy_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _file(tmp_path, ".env", "OPENAI_API_KEY=sk-real")
    stub = _Guard(denied={".env": {"ok": False, "error": "user denied approval"}})
    monkeypatch.setattr(attachment_access, "request_guard", stub)
    ls = _loop_state(tmp_path)

    kept, refused = _screen(ls, [".env"])

    assert kept == []
    assert refused == [RefusedAttachment(".env", "user denied approval")]


def test_a_denial_without_a_message_still_says_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _file(tmp_path, "data/a.png")
    monkeypatch.setattr(attachment_access, "request_guard", _Guard(denied={"data/a.png": {"ok": False}}))
    ls = _loop_state(tmp_path)

    _kept, refused = _screen(ls, ["data/a.png"])

    assert refused == [RefusedAttachment("data/a.png", "denied")]


def test_a_broken_policy_check_refuses_rather_than_lets_the_file_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """判不出来就别发。查不通策略时放行等于把审批变成「supervisor 在线时才生效」。"""
    _file(tmp_path, "data/a.png")
    monkeypatch.setattr(attachment_access, "request_guard", _Guard(boom=True))
    ls = _loop_state(tmp_path)

    kept, refused = _screen(ls, ["data/a.png"])

    assert kept == []
    assert refused[0].path == "data/a.png"
    assert refused[0].reason.startswith("policy check failed: ")
    assert "supervisor unreachable" in refused[0].reason


def test_a_cancelled_task_refuses_the_rest_without_more_policy_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for rel in ("data/a.png", "data/b.png", "data/c.png"):
        _file(tmp_path, rel)
    stub = _Guard(denied={"data/b.png": {"ok": False, "error": "task cancelled", "cancelled": True}})
    monkeypatch.setattr(attachment_access, "request_guard", stub)
    ls = _loop_state(tmp_path)

    kept, refused = _screen(ls, ["data/a.png", "data/b.png", "data/c.png"])

    assert kept == ["data/a.png"]
    assert refused == [
        RefusedAttachment("data/b.png", "task cancelled"),
        RefusedAttachment("data/c.png", "task cancelled"),
    ]
    assert stub.paths == ["data/a.png", "data/b.png"]


def test_duplicate_and_blank_entries_collapse_to_one_check(tmp_path: Path, guard: _Guard) -> None:
    _file(tmp_path, "data/a.png")
    ls = _loop_state(tmp_path)

    kept, refused = _screen(ls, ["data/a.png", " data/a.png ", "", "   ", None, 7, "data/a.png"])

    assert kept == ["data/a.png"]
    assert refused == []
    assert guard.paths == ["data/a.png"]


@pytest.mark.parametrize("paths", [None, [], "data/a.png", {"path": "data/a.png"}])
def test_a_non_list_argument_screens_to_nothing(tmp_path: Path, guard: _Guard, paths: Any) -> None:
    ls = _loop_state(tmp_path)

    assert _screen(ls, paths) == ([], [])
    assert guard.calls == []


@pytest.mark.parametrize("paths", [[], ["", "  "]])
def test_nothing_to_screen_reads_nothing_off_the_loop_state(guard: _Guard, paths: list) -> None:
    """不带附件的 dispatch 每次都要过这里，没东西要判就别去解析一遍已收附件的路径。"""
    assert _screen(SimpleNamespace(), paths) == ([], [])
    assert guard.calls == []


def test_one_tool_context_serves_the_whole_call(tmp_path: Path, guard: _Guard) -> None:
    for rel in ("data/a.png", "data/b.png"):
        _file(tmp_path, rel)
    ls = _loop_state(tmp_path)

    _screen(ls, ["data/a.png", "data/b.png"], name="finish")

    assert len(guard.contexts) == 2
    assert guard.contexts[0] is guard.contexts[1]
    assert guard.contexts[0].tool_call_id == "call-1"
    assert guard.contexts[0].node_id == "qq.orchestrator"


@needs_symlink
def test_a_symlink_under_data_is_judged_by_what_it_points_at(
    tmp_path: Path, guard: _Guard,
) -> None:
    """不看目标的话，在 data/ 里建个链接就能把整个工作区变成附件目录。"""
    secret = _file(tmp_path, "secrets/private.key", "-----BEGIN KEY-----")
    (tmp_path / "data").mkdir(exist_ok=True)
    os.symlink(secret, tmp_path / "data" / "innocent.png")
    ls = _loop_state(tmp_path)

    kept, refused = _screen(ls, ["data/innocent.png"], name="dispatch_to_scout", data_dir_only=True)

    assert (kept, refused) == (["data/innocent.png"], [])
    assert guard.paths == ["secrets/private.key"]


@needs_symlink
def test_a_symlinked_secret_can_be_denied_by_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = _file(tmp_path, "secrets/private.key", "-----BEGIN KEY-----")
    (tmp_path / "data").mkdir(exist_ok=True)
    os.symlink(secret, tmp_path / "data" / "innocent.png")
    stub = _Guard(denied={"secrets/private.key": {"ok": False, "error": "user denied approval"}})
    monkeypatch.setattr(attachment_access, "request_guard", stub)
    ls = _loop_state(tmp_path)

    kept, refused = _screen(ls, ["data/innocent.png"])

    assert kept == []
    assert refused == [RefusedAttachment("data/innocent.png", "user denied approval")]


def test_refused_attachment_renders_flat_for_the_model() -> None:
    assert RefusedAttachment("data/a.png", "denied").as_dict() == {
        "path": "data/a.png", "reason": "denied",
    }


_CONTEXT_FIELDS = {
    "supervisor_url", "session_id", "run_id", "worker_id", "workspace_root", "http", "registry",
    "task_id", "session_generation", "parent_session_id", "conversation_key",
    "approval_poll_interval_sec", "approval_timeout_sec", "tool_call_id", "node_id", "platform_auth",
}


def test_the_parity_list_still_matches_the_tool_context_dataclass() -> None:
    """ToolContext 加了字段而这里没跟上时，下面那条逐字段断言就成了摆设。"""
    assert {f.name for f in dataclasses.fields(ToolContext)} == _CONTEXT_FIELDS


def test_build_tool_context_fills_every_field_from_the_run_context(tmp_path: Path) -> None:
    auth = {"is_admin": True, "user_id": "10001"}
    ls = _loop_state(tmp_path, task_context={
        "conversation_key": "  qq_private:123  ",
        "platform_auth": auth,
        "memory_hints": {"subjects": ["UserA", "UserB"]},
        "route_conversation_key": "qq_private:123",
        "parent_conversation_key": "qq_group:456",
    })

    ctx = build_tool_context(ls, tool_call_id="call-7")

    assert ctx.supervisor_url == "http://supervisor.test"
    assert ctx.session_id == "branch-1"
    assert ctx.run_id == "task-42"
    assert ctx.worker_id == "worker-9"
    assert ctx.workspace_root is tmp_path
    assert ctx.http is ls.rctx.http
    assert ctx.registry is _REGISTRY_SENTINEL
    assert ctx.task_id == "task-42"
    assert ctx.session_generation == 7
    assert ctx.parent_session_id == "session-1"
    assert ctx.conversation_key == "qq_private:123"
    assert ctx.tool_call_id == "call-7"
    assert ctx.node_id == "qq.orchestrator"
    assert ctx.platform_auth == auth and ctx.platform_auth is not auth
    assert ctx.approval_poll_interval_sec == 0.5
    assert ctx.approval_timeout_sec == 3600.0


def test_build_tool_context_carries_the_node_level_extras(tmp_path: Path) -> None:
    ls = _loop_state(tmp_path, task_context={
        "memory_hints": {"subjects": ["UserA"]},
        "route_conversation_key": "qq_private:123",
        "parent_conversation_key": "qq_group:456",
    })

    ctx = build_tool_context(ls)

    assert TOOL_CONTEXT_EXTRA_ATTRS == (
        "_node_id", "_node_extra", "_memory_subjects",
        "_route_conversation_key", "_parent_conversation_key",
    )
    assert all(hasattr(ctx, attr) for attr in TOOL_CONTEXT_EXTRA_ATTRS)
    assert ctx._node_id == "qq.orchestrator"
    assert ctx._node_extra == {"memory_book": "profile"}
    assert ctx._memory_subjects == ["UserA"]
    assert ctx._route_conversation_key == "qq_private:123"
    assert ctx._parent_conversation_key == "qq_group:456"


def test_build_tool_context_defaults_an_empty_task_context(tmp_path: Path) -> None:
    ls = _loop_state(tmp_path, task_context={})

    ctx = build_tool_context(ls)

    assert ctx.conversation_key == ""
    assert ctx.platform_auth == {}
    assert ctx.tool_call_id == ""
    assert ctx._memory_subjects == []
    assert ctx._route_conversation_key == ""


@pytest.mark.parametrize("task_id, run_id, expected", [
    ("task-42", "run-1", "task-42"),
    ("", "run-1", "run-1"),
    ("", "", "qq.orchestrator"),
])
def test_run_id_falls_back_from_task_id_to_run_id_to_node(
    tmp_path: Path, task_id: str, run_id: str, expected: str,
) -> None:
    ls = _loop_state(tmp_path)
    ls.rctx.task_id = task_id
    ls.run_id = run_id

    assert build_tool_context(ls).run_id == expected


@pytest.fixture()
def emitted(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    records: list[tuple[str, str]] = []

    def _emit(ls, pseudo_call, content, **kwargs):  # noqa: ANN001
        records.append((pseudo_call.name, content))

    monkeypatch.setattr(pseudo_handlers, "_emit_pseudo_tool_result", _emit)
    monkeypatch.setattr(pseudo_handlers, "_persist_ctx", lambda ls, step: "ctx-ref")
    return records


def _call_with(name: str, **arguments: Any) -> _Call:
    call = _Call(name)
    call.arguments = arguments
    return call


def _handle(ls: _LoopState, call: _Call):
    return asyncio.run(pseudo_handlers._handle_pseudo_tool(ls, call, 0))


def test_reply_sends_kept_attachments_and_names_the_refused_ones(
    tmp_path: Path, emitted: list, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _file(tmp_path, "data/a.png")
    _file(tmp_path, ".env", "sk-real")
    monkeypatch.setattr(attachment_access, "request_guard", _Guard(
        denied={".env": {"ok": False, "error": "user denied approval"}},
    ))
    ls = _loop_state(tmp_path, collected=[{"path": "data/a.png", "type": "image"}])

    _handle(ls, _call_with("reply", text="看这个", attachment_paths=["data/a.png", ".env"]))

    assert [e["type"] for e in ls.events] == ["intermediate_reply"]
    assert ls.events[0]["payload"]["attachments"] == [{"path": "data/a.png", "type": "image"}]
    assert emitted == [("reply", "ok\n以下附件没有发出：.env（user denied approval）")]


def test_reply_with_nothing_left_to_send_emits_no_event_but_tells_the_model(
    tmp_path: Path, emitted: list, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _file(tmp_path, ".env", "sk-real")
    monkeypatch.setattr(attachment_access, "request_guard", _Guard(
        denied={".env": {"ok": False, "error": "user denied approval"}},
    ))
    ls = _loop_state(tmp_path)

    _handle(ls, _call_with("reply", text="", attachment_paths=[".env"]))

    assert ls.events == []
    assert emitted == [("reply", "以下附件没有发出：.env（user denied approval）")]


def test_reply_without_text_or_paths_is_unchanged(tmp_path: Path, emitted: list, guard: _Guard) -> None:
    ls = _loop_state(tmp_path)

    _handle(ls, _call_with("reply", text=""))

    assert ls.events == []
    assert emitted == []
    assert guard.calls == []


def test_reply_says_plain_ok_when_nothing_was_refused(
    tmp_path: Path, emitted: list, guard: _Guard,
) -> None:
    ls = _loop_state(tmp_path, collected=[{"path": "data/a.png", "type": "image"}])

    _handle(ls, _call_with("reply", text="hi", attachment_paths=["data/a.png"]))

    assert emitted == [("reply", "ok")]
    assert guard.calls == []


def test_finish_with_every_named_path_refused_sends_no_attachment_at_all(
    tmp_path: Path, emitted: list, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """点了名就按点名的算。回退到工具产物等于模型说「只发这个」却发出了别的。"""
    _file(tmp_path, ".env", "sk-real")
    monkeypatch.setattr(attachment_access, "request_guard", _Guard(
        denied={".env": {"ok": False, "error": "user denied approval"}},
    ))
    ls = _loop_state(tmp_path, tool_produced=[{"path": "data/generated.png", "type": "image"}])

    action = _handle(ls, _call_with("finish", text="done", attachment_paths=[".env"]))

    assert action.result["attachments"] == []
    assert emitted == [("finish", "ok\n以下附件没有发出：.env（user denied approval）")]


def test_finish_keeps_the_allowed_half_of_a_mixed_selection(
    tmp_path: Path, emitted: list, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _file(tmp_path, ".env", "sk-real")
    monkeypatch.setattr(attachment_access, "request_guard", _Guard(
        denied={".env": {"ok": False, "error": "user denied approval"}},
    ))
    ls = _loop_state(
        tmp_path,
        collected=[{"path": "data/a.png", "type": "image"}, {"path": "data/b.png", "type": "image"}],
    )

    action = _handle(ls, _call_with("finish", text="done", attachment_paths=["data/a.png", ".env"]))

    assert action.result["attachments"] == [{"path": "data/a.png", "type": "image"}]


def test_finish_without_named_paths_still_forwards_tool_output(
    tmp_path: Path, emitted: list, guard: _Guard,
) -> None:
    produced = [{"path": "data/generated.png", "type": "image"}]
    ls = _loop_state(tmp_path, tool_produced=produced)

    action = _handle(ls, _call_with("finish", text="done"))

    assert action.result["attachments"] == produced
    assert emitted == [("finish", "ok")]
    assert guard.calls == []


def test_finish_without_named_paths_still_forwards_dispatch_result_images(
    tmp_path: Path, emitted: list, guard: _Guard,
) -> None:
    inbound = [{"path": "data/attachments/child/nai.png", "type": "image"}]
    ls = _loop_state(tmp_path, task_context={
        "inbound_message_type": "dispatch_result",
        "inbound_attachments": inbound,
    })

    action = _handle(ls, _call_with("finish", text="done"))

    assert action.result["attachments"] == inbound
    assert guard.calls == []


def test_finish_on_an_already_sent_dispatch_result_sends_nothing_and_asks_nothing(
    tmp_path: Path, emitted: list, guard: _Guard,
) -> None:
    ls = _loop_state(tmp_path, task_context={
        "inbound_message_type": "dispatch_result",
        "inbound_attachments_outbound_sent": True,
    })

    action = _handle(ls, _call_with("finish", text="done", attachment_paths=["data/a.png"]))

    assert action.result["attachments"] == []
    assert guard.calls == []
    assert emitted == [("finish", "ok")]


def test_a_short_reply_turn_screens_nothing(tmp_path: Path, emitted: list, guard: _Guard) -> None:
    ls = _loop_state(tmp_path, task_context={"short_reply": True})

    action = _handle(ls, _call_with("finish", text="嗯", attachment_paths=["data/a.png"]))

    assert action.result["attachments"] == []
    assert guard.calls == []


def _dispatch_ls(tmp_path: Path, **kwargs: Any) -> _LoopState:
    response = SimpleNamespace(status_code=200, json=lambda: {"session_id": "child-1"})
    http = SimpleNamespace(post=AsyncMock(return_value=response))
    return _loop_state(
        tmp_path,
        allowed_dispatch_targets={"draw.image_gen"},
        task_context={"conversation_key": "qq_private:123", "channel": "qq_private"},
        http=http,
        **kwargs,
    )


def _dispatch(ls: _LoopState, emitted: list[tuple[str, str]], target: str = "draw.image_gen", **args: Any) -> dict:
    call = _Call(f"dispatch_to_{target.replace('.', '_')}", "call-9")
    asyncio.run(pseudo_handlers._handle_pseudo_dispatch(
        ls, {"target": target, "instruction": "draw", **args}, call,
    ))
    return json.loads(emitted[-1][1])


def test_dispatch_reports_refused_paths_next_to_the_result(
    tmp_path: Path, emitted: list, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _file(tmp_path, "data/report.md")
    _file(tmp_path, "notes/plan.md")
    monkeypatch.setattr(attachment_access, "request_guard", _Guard())
    ls = _dispatch_ls(tmp_path)

    said = _dispatch(ls, emitted, attachment_paths=["data/report.md", "notes/plan.md"])

    assert said["success"] is True
    assert said["refused_attachments"] == [{"path": "notes/plan.md", "reason": _DATA_ONLY_REASON}]
    sent = ls.rctx.http.post.call_args.kwargs["json"]["attachments"]
    assert [att["path"] for att in sent] == ["data/report.md"]


def test_dispatch_says_nothing_extra_when_nothing_was_refused(
    tmp_path: Path, emitted: list, guard: _Guard,
) -> None:
    _file(tmp_path, "data/report.md")
    ls = _dispatch_ls(tmp_path)

    said = _dispatch(ls, emitted, attachment_paths=["data/report.md"])

    assert "refused_attachments" not in said
    assert said["success"] is True
    assert guard.paths == ["data/report.md"]


def test_a_rejected_delegate_target_never_reaches_the_policy_check(
    tmp_path: Path, emitted: list, guard: _Guard,
) -> None:
    """先看能不能派，再看附件。顺序反了就成了替不许派的目标去弹审批。"""
    _file(tmp_path, "data/report.md")
    ls = _dispatch_ls(tmp_path)

    said = _dispatch(
        ls, emitted, target="bootstrap.executor", attachment_paths=["data/report.md"],
    )

    assert said["success"] is False
    assert "delegate_targets" in said["error"]
    assert "refused_attachments" not in said
    assert guard.calls == []
    assert ls.rctx.http.post.await_count == 0
