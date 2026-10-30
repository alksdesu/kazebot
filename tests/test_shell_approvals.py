"""shell 里的审批提示：人批的说同意或拒绝，没人定案、被系统收掉的说已失效和原因。

推送过的审批在别处定案或失效时补一句，免得照着旧提示去批；在这里亲自批的不再重复说。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from shell import cli  # noqa: E402
from shell.approvals import approval_settled_text, decision_took_effect  # noqa: E402

_ORPHANED_COMMENT = "task ended before a decision; auto-denied"
_TIMEOUT_COMMENT = "approval timed out after 300s with no decision; auto-denied"


@pytest.mark.parametrize("record, text", [
    ({"decision": "allow", "status": "allowed"}, "已同意"),
    ({"status": "allowed"}, "已同意"),
    ({"decision": "deny", "status": "denied", "comment": "不行"}, "已拒绝"),
    ({"decision": "deny", "status": "denied", "comment": _ORPHANED_COMMENT, "expired_reason": "任务已取消"}, "已失效（任务已取消）"),
    ({"decision": "deny", "status": "denied", "comment": _TIMEOUT_COMMENT}, "已失效"),
    ({"decision": "deny", "status": "denied", "expired_reason": "  "}, "已拒绝"),
])
def test_the_settled_text_tells_a_person_from_the_system(record: dict[str, Any], text: str) -> None:
    assert approval_settled_text(record) == text


@pytest.mark.parametrize("receipt, decision, effective", [
    ({"status": "allowed"}, "allow", True),
    ({"status": "denied"}, "deny", True),
    ({"status": "denied"}, "allow", False),
    ({"status": "denied", "expired_reason": "任务已取消"}, "deny", False),
    ({"status": "denied", "expired_reason": None}, "deny", True),
    ({"approval_id": "a1"}, "allow", True),
    ("not a receipt", "allow", True),
])
def test_a_decision_took_effect_only_when_the_receipt_matches_it(receipt: Any, decision: str, effective: bool) -> None:
    assert decision_took_effect(receipt, decision) is effective


def _receipt(body: Any) -> httpx.Response:
    request = httpx.Request("POST", "http://supervisor/v1/approvals/a1")
    if isinstance(body, bytes):
        return httpx.Response(200, content=body, request=request)
    return httpx.Response(200, json=body, request=request)


def test_the_cli_says_when_its_decision_did_not_take_effect() -> None:
    assert cli._decision_report(_receipt({"status": "allowed"}), "allow") == "[approval] decided: allow"
    assert cli._decision_report(
        _receipt({"status": "denied", "comment": _ORPHANED_COMMENT, "expired_reason": "任务已取消"}), "allow",
    ) == "[approval] 这次决定没有生效：这条审批已失效（任务已取消）"
    assert cli._decision_report(_receipt({"status": "allowed", "comment": "控制台批了"}), "deny") == (
        "[approval] 这次决定没有生效：这条审批已同意"
    )
    assert cli._decision_report(_receipt(b"<html>bad gateway</html>"), "deny") == "[approval] decided: deny"


class _Events:
    """/v1/events：每轮轮询交出一批事件。"""

    def __init__(self, *batches: list[dict[str, Any]]) -> None:
        self.batches = list(batches)

    def get(self, url: str, params: Any = None) -> httpx.Response:
        events = self.batches.pop(0) if self.batches else []
        return httpx.Response(200, json=events, request=httpx.Request("GET", url))


def _event(seq: int, event_type: str, **payload: Any) -> dict[str, Any]:
    return {"seq": seq, "type": event_type, "payload": payload}


def test_an_announced_approval_settled_elsewhere_is_called_off(capsys: pytest.CaptureFixture[str]) -> None:
    poller = cli._PushPoller(_Events(
        [_event(1, "approval_requested", approval_id="a1", operation="execute_command")],
        [_event(2, "approval_decided", approval_id="a1", decision="deny", status="denied",
                comment=_ORPHANED_COMMENT, expired_reason="任务已取消")],
    ), "http://supervisor", poll_sec=1.0)

    poller._tick()
    poller._tick()

    out = capsys.readouterr().out
    assert "/approve a1" in out
    assert "[push] 审批 a1 已失效（任务已取消），不用再处理" in out
    assert poller.global_seq == 2


def test_decisions_made_here_or_on_approvals_never_shown_are_not_repeated(capsys: pytest.CaptureFixture[str]) -> None:
    poller = cli._PushPoller(_Events(
        [_event(1, "approval_requested", approval_id="a1", operation="execute_command")],
        [
            _event(2, "approval_decided", approval_id="a1", decision="allow", status="allowed"),
            _event(3, "approval_decided", approval_id="elsewhere", decision="deny", status="denied"),
        ],
    ), "http://supervisor", poll_sec=1.0)
    poller._tick()
    poller.forget_approval("a1")
    capsys.readouterr()

    poller._tick()

    assert "不用再处理" not in capsys.readouterr().out
    assert poller.global_seq == 3
