"""supervisor 拒绝请求时应答里的原因：refusal_reason 交给模型，refusal_detail、error_detail 只取 detail 里写了的那部分。"""
from __future__ import annotations

import json

import httpx
import pytest

from clonoth_sdk.refusals import error_detail, refusal_detail, refusal_reason

URL = "http://127.0.0.1:8765/v1/reminders/R0123456789ab/snooze"

VALIDATION_ERRORS = [
    {"type": "greater_than_equal", "loc": ["query", "page"], "msg": "Input should be greater than or equal to 0", "input": "-1", "ctx": {"ge": 0}},
    {"type": "missing", "loc": ["body", "steps", 0, "tool"], "msg": "Field required", "input": {}},
    {"type": "missing", "loc": ["body"], "msg": "Field required", "input": None},
]


def _refused(status: int, **content) -> httpx.HTTPStatusError:
    """和 SDK 客户端 raise_for_status 抛出的一样：异常原文里带着 supervisor 的地址。"""
    reply = httpx.Response(status, request=httpx.Request("POST", URL), **content)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        reply.raise_for_status()
    return caught.value


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


@pytest.mark.parametrize("reply", [
    httpx.Response(404, json={"detail": "  "}),
    httpx.Response(400, json={"message": "not under detail"}),
    httpx.Response(500, text="Internal Server Error"),
    httpx.Response(503, content=b""),
], ids=["blank-string", "no-detail", "plain-text", "empty-body"])
def test_refusal_detail_has_nothing_when_the_supervisor_gave_no_reason(reply):
    assert refusal_detail(reply) == ""


def test_refusal_detail_has_nothing_when_the_reply_cannot_be_read():
    class Unreadable:
        status_code = 500

        def json(self):
            raise RuntimeError("stream already closed")

    assert refusal_detail(Unreadable()) == ""


@pytest.mark.parametrize("error,reason", [
    (_refused(409, json={"detail": "提醒已完成，不能再延后"}), "提醒已完成，不能再延后"),
    (_refused(400, json={"detail": {"code": "invalid_input", "message": "延后时长超出范围"}}),
     "延后时长超出范围（invalid_input，HTTP 400）"),
    (_refused(500, text="Internal Server Error"), ""),
], ids=["string", "structured", "no-detail"])
def test_error_detail_reads_the_reply_a_refused_request_carries(error, reason):
    assert URL in str(error)
    assert error_detail(error) == reason


@pytest.mark.parametrize("error", [
    httpx.ConnectError("All connection attempts failed"),
    KeyError("revision"),
    RuntimeError("boom"),
], ids=["unreachable", "malformed-reply", "other"])
def test_error_detail_is_empty_for_errors_that_carry_no_reply(error):
    assert error_detail(error) == ""
