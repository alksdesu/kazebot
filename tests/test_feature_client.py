from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from toolbox.builtins import community, execution_plans, materials, reminders
from toolbox.feature_client import FeatureRequestError, request


def feature_context(http):
    return SimpleNamespace(http=http, supervisor_url="http://test", task_id="task")


def test_request_raises_the_refusal_reason_and_keeps_the_timeout():
    seen = []

    async def handler(sent):
        seen.append(sent)
        if sent.method == "GET":
            return httpx.Response(200, json={"items": []})
        return httpx.Response(400, json={"detail": {"code": "invalid_input", "message": "请提供 1–500 个识别字段。"}})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=7) as http:
            listed = await request(feature_context(http), "GET", "/v1/materials/extractions")
            with pytest.raises(FeatureRequestError) as refused:
                await request(feature_context(http), "POST", "/v1/materials/extractions", body={"fields": []}, timeout=20)
        return listed, refused.value

    listed, error = asyncio.run(exercise())
    assert listed == {"items": []}
    assert isinstance(error, httpx.HTTPStatusError)
    assert str(error) == "请提供 1–500 个识别字段。（invalid_input，HTTP 400）"
    assert error.response.status_code == 400
    assert [sent.headers["X-Clonoth-Task-Id"] for sent in seen] == ["task", "task"]
    assert [sent.extensions["timeout"]["read"] for sent in seen] == [7, 20]


FEATURE_TOOLS = [
    (materials.document, {"action": "list"}),
    (materials.chat_search, {"query": "上周的报价"}),
    (materials.structure, {"action": "list"}),
    (materials.image_versions, {"action": "list"}),
    (materials.generate, {"action": "status", "job_id": "job1"}),
    (community.community, {"operation": "list"}),
    (reminders.create_reminder, {"text": "交周报", "due_at": "2026-10-28T09:00:00"}),
    (reminders.list_reminders, {}),
    (reminders.update_reminder, {"reminder_id": "r1", "action": "complete", "expected_revision": 1}),
    (execution_plans.draft_execution_plan, {"goal": "整理链接"}),
    (execution_plans.inspect_execution_plan, {"plan_id": "p1"}),
    (execution_plans.revise_execution_plan, {"plan_id": "p1", "goal": "整理链接", "expected_revision": 1}),
]


@pytest.mark.parametrize("tool,args", FEATURE_TOOLS, ids=[tool.__name__ for tool, _ in FEATURE_TOOLS])
def test_every_feature_tool_hands_back_the_refusal_reason(tool, args):
    async def handler(sent):
        return httpx.Response(403, json={"detail": "只能操作本人创建的内容"})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await tool(args, feature_context(http))

    reason = "只能操作本人创建的内容"
    assert asyncio.run(exercise()) == {"ok": False, "error": reason, "data": {"result": f"ERROR: {reason}"}}


def test_errors_that_are_not_refusals_still_reach_the_registry():
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda sent: httpx.Response(200, json={}))) as http:
            await reminders.update_reminder({"reminder_id": "r1", "action": "archive"}, feature_context(http))

    with pytest.raises(ValueError, match="无效提醒操作"):
        asyncio.run(exercise())
