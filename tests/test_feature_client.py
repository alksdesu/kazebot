from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from toolbox.builtins import community, execution_plans, materials, reminders
from toolbox.feature_client import FeatureRequestError, refusal_reason, request


def feature_context(http):
    return SimpleNamespace(http=http, supervisor_url="http://test", task_id="task")


VALIDATION_ERRORS = [
    {"type": "greater_than_equal", "loc": ["query", "page"], "msg": "Input should be greater than or equal to 0", "input": "-1", "ctx": {"ge": 0}},
    {"type": "missing", "loc": ["body", "steps", 0, "tool"], "msg": "Field required", "input": {}},
    {"type": "missing", "loc": ["body"], "msg": "Field required", "input": None},
]


@pytest.mark.parametrize("reply,reason", [
    (httpx.Response(400, json={"detail": {"code": "invalid_input", "message": "定位框须为 0–1 范围的 [左,上,右,下]。"}}),
     "定位框须为 0–1 范围的 [左,上,右,下]。（invalid_input，HTTP 400）"),
    (httpx.Response(409, json={"detail": {"message": "校对版本已变化，请刷新后再改。"}}), "校对版本已变化，请刷新后再改。（HTTP 409）"),
    (httpx.Response(403, json={"detail": "需要本群管理权限"}), "需要本群管理权限"),
    (httpx.Response(422, json={"detail": VALIDATION_ERRORS}),
     "page：Input should be greater than or equal to 0；steps.0.tool：Field required；Field required（HTTP 422）"),
    (httpx.Response(409, json={"detail": {"candidates": [{"task_id": "t1"}]}}), '{"candidates": [{"task_id": "t1"}]}（HTTP 409）'),
    (httpx.Response(404, json={"detail": "  "}), "supervisor 回了 HTTP 404"),
    (httpx.Response(404, json={"detail": None}), "supervisor 回了 HTTP 404"),
    (httpx.Response(422, json={"detail": []}), "supervisor 回了 HTTP 422"),
    (httpx.Response(400, json={"message": "not under detail"}), "supervisor 回了 HTTP 400"),
    (httpx.Response(502, json=["not", "an", "object"]), "supervisor 回了 HTTP 502"),
    (httpx.Response(500, text="Internal Server Error"), "supervisor 回了 HTTP 500"),
    (httpx.Response(503, content=b""), "supervisor 回了 HTTP 503"),
], ids=[
    "message-and-code", "message-only", "string", "validation-list", "unknown-object",
    "blank-string", "null", "empty-list", "no-detail", "json-array", "plain-text", "empty-body",
])
def test_refusal_reason_reads_every_detail_shape(reply, reason):
    assert refusal_reason(reply) == reason


def test_refusal_reason_lists_the_first_problems_and_counts_the_rest():
    problems = [{"type": "missing", "loc": ["body", f"field{n}"], "msg": "Field required"} for n in range(8)]
    listed = "；".join(f"field{n}：Field required" for n in range(5))
    assert refusal_reason(httpx.Response(422, json={"detail": problems})) == f"{listed}；另有 3 处（HTTP 422）"


def test_refusal_reason_clips_an_unknown_detail():
    detail = {"items": ["x" * 2000]}
    clipped = json.dumps(detail, ensure_ascii=False)[:500]
    assert refusal_reason(httpx.Response(409, json={"detail": detail})) == f"{clipped}…（HTTP 409）"


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
