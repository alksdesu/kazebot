"""runner 写的 TaskRecord 带完成时间和实际出答案的模型、provider、渠道。

父会话、子会话各写一份，两份只差 session_id：用量汇总靠这一点认出是同一段，只算一次。
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine import runner  # noqa: E402
from engine.builtin.usage_tracker import note_serving_provider  # noqa: E402
from engine.node import Node  # noqa: E402
from engine.protocol import TaskAction  # noqa: E402
from engine.task_record import load_task_records, parse_record_timestamp  # noqa: E402
from toolbox.registry import ToolRegistry  # noqa: E402

USAGE = {"prompt_tokens": 1200, "completion_tokens": 30, "total_tokens": 1230, "cache_read_tokens": 0}


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    child: bool,
    served_by: Any = None,
    action: str = "finish",
) -> SimpleNamespace:
    """跑一次节点任务，返回节点自己的那个 provider（runner 会给它挂上渠道名）。"""
    main = SimpleNamespace(model="main-model", name="deepseek")
    monkeypatch.setattr(runner, "load_node", lambda *_args: Node(id="entry", type="ai", prompt="test"))
    monkeypatch.setattr(runner, "_fetch_session_provider_override", AsyncMock(return_value=None))
    monkeypatch.setattr(runner, "_create_provider_from_registry", lambda *_args, **_kwargs: main)
    monkeypatch.setattr(runner, "install_event_bridge", lambda *_args, **_kwargs: None)

    async def run_ai_node(**kwargs: Any) -> TaskAction:
        rctx = kwargs["rctx"]
        rctx.total_usage.update(USAGE)
        if served_by is not None:
            note_serving_provider(rctx, served_by)
        return TaskAction(action=action, node_id="entry", result={"text": "done"}, error="" if action == "finish" else "boom")

    monkeypatch.setattr(runner, "run_ai_node", run_ai_node)
    task_input: dict[str, Any] = {"instruction": "hello"}
    if child:
        task_input.update({"child_session_id": "child-1", "parent_session_id": "parent", "context_mode": "accumulate"})
    task = {"task_id": "task-1", "node_id": "entry", "input": task_input}

    async def go() -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        async with httpx.AsyncClient(transport=transport) as http:
            await runner._run_node_task(
                http=http, llm_http=http, sup_url="http://local", ws_root=tmp_path,
                registry=ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools"),
                task=task, worker_id="worker", session_id="parent", session_generation=1,
                api_key="test", base_url="http://model.invalid", default_model="test",
            )

    asyncio.run(go())
    return main


def _lines(tmp_path: Path, session_id: str) -> list[dict[str, Any]]:
    path = tmp_path / "data" / "transcripts" / f"{session_id}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_the_record_carries_the_completion_time_and_the_main_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = datetime.now(timezone.utc)
    main = _run(tmp_path, monkeypatch, child=False)
    after = datetime.now(timezone.utc)

    [record] = _lines(tmp_path, "parent")
    completed = parse_record_timestamp(record["completed_at"])
    assert completed is not None and before <= completed <= after
    assert completed.utcoffset() is not None and completed.utcoffset().total_seconds() == 0
    # 一次成功的请求都没记下时，退回节点自己的渠道
    assert main.plugin_channel
    assert (record["model"], record["provider"], record["channel"]) == ("main-model", "deepseek", main.plugin_channel)
    assert record["token_usage"] == USAGE
    [loaded] = load_task_records(tmp_path, "parent")
    assert (loaded.completed_at, loaded.model, loaded.provider, loaded.channel) == (
        record["completed_at"], "main-model", "deepseek", main.plugin_channel,
    )


def test_the_provider_that_actually_answered_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backup = SimpleNamespace(model="backup-model", name="gemini", plugin_channel="gemini-中转A")

    _run(tmp_path, monkeypatch, child=False, served_by=backup)

    [record] = _lines(tmp_path, "parent")
    assert (record["model"], record["provider"], record["channel"]) == ("backup-model", "gemini", "gemini-中转A")


def test_a_failed_task_still_gets_a_completion_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(tmp_path, monkeypatch, child=False, action="fail")

    [record] = _lines(tmp_path, "parent")
    assert record["action"] == "fail"
    assert parse_record_timestamp(record["completed_at"]) is not None
    assert record["model"] == "main-model"


def test_parent_and_child_copies_differ_only_in_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup = SimpleNamespace(model="backup-model", name="gemini", plugin_channel="gemini-中转A")

    _run(tmp_path, monkeypatch, child=True, served_by=backup)

    [parent] = _lines(tmp_path, "parent")
    [child] = _lines(tmp_path, "child-1")
    assert (parent["session_id"], child["session_id"]) == ("parent", "child-1")
    assert parent["child_session_id"] == child["child_session_id"] == "child-1"
    assert {key: value for key, value in parent.items() if key != "session_id"} == {
        key: value for key, value in child.items() if key != "session_id"
    }
    assert parent["completed_at"] and parent["model"] == "backup-model"
