"""审批授权的 HTTP 接口和 SDK：同意时带上审批人和时长（或要永久授权），列出、收回授权都要管理员令牌；不带新字段的旧客户端照旧能用。"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from _approval_grant_fixture import (  # noqa: E402
    ADMIN,
    BOT,
    OTHER_ADMIN,
    SENSITIVE_WRITE,
    START,
    Clock,
    grant_for,
    make_state,
    pending_approval,
    qq_approver,
    qq_auth,
    start_task,
    write_policy,
)
import supervisor.admin_api as admin_api  # noqa: E402
from clonoth_sdk.client import ClonothClient  # noqa: E402
from clonoth_sdk.types import (  # noqa: E402
    GRANT_NOTE_NO_APPROVER,
    GRANT_NOTE_NOT_REQUESTER,
    GRANT_NOTE_ONCE,
    GRANT_NOTE_PERMANENT_DISABLED,
    ApprovalDecision,
    ApprovalGrant,
    PlatformAuth,
)
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import ApprovalStatus  # noqa: E402

_TOKEN = "test-approval-grant-api-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_BASE_URL = "http://supervisor.test"


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr("supervisor.state._now", clock)
    return clock


@pytest.fixture()
def state(tmp_path: Path, clock: Clock) -> SupervisorState:
    return make_state(tmp_path)


@pytest.fixture()
def app(tmp_path: Path, state: SupervisorState, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    # 认证失败按来源退避，前三次免费：每条用例都从干净的计数开始。
    monkeypatch.setattr(admin_api, "_auth_failures", type(admin_api._auth_failures)())
    return create_app(
        state=state,
        process_manager=None,
        config_store=ConfigStore(path=tmp_path / "config" / "models.yaml", workspace_root=tmp_path),
    )


@pytest.fixture()
def api(app):
    def call(method: str, url: str, *, auth: bool = True, **kwargs: Any) -> httpx.Response:
        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=_BASE_URL) as http:
                return await http.request(method, url, headers=_AUTH if auth else {}, **kwargs)

        return asyncio.run(go())

    return call


def _moment(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _decide(api: Any, approval_id: str, **body: Any) -> httpx.Response:
    return api("POST", f"/v1/approvals/{approval_id}", json={"decision": "allow", **body})


class TestTheDecisionEndpoint:
    def test_the_requester_approving_gets_a_grant_back(self, api: Any, state: SupervisorState) -> None:
        approval_id = pending_approval(state)

        response = _decide(api, approval_id, comment="ok", approver=qq_approver(), grant_minutes=120)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "allowed"
        assert body["approver"] == qq_approver()
        assert body["grant_note"] is None
        grant = body["grant"]
        assert (grant["platform"], grant["user_id"], grant["bot_scope"]) == ("qq", ADMIN, BOT)
        assert grant["minutes"] == 120 and grant["approval_id"] == approval_id
        assert _moment(grant["until"]) == START + timedelta(minutes=120)
        assert [item.grant_id for item in state.list_approval_grants()] == [grant["grant_id"]]

    def test_the_grant_covers_the_next_operation_request(self, api: Any, state: SupervisorState) -> None:
        _decide(api, pending_approval(state), approver=qq_approver())
        sid, task = start_task(state, auth=qq_auth())

        response = api("POST", "/v1/ops/request", json={
            "session_id": sid, "op": "write_file", "parameters": dict(SENSITIVE_WRITE), "task_id": task.task_id,
        })

        assert response.status_code == 200, response.text
        assert response.json()["safety_level"] == "auto"
        assert response.json()["reason"].startswith("auto-approved: approval grant")
        assert response.json()["approval_id"] is None

    def test_without_minutes_the_policy_default_applies(self, api: Any, state: SupervisorState) -> None:
        body = _decide(api, pending_approval(state), approver=qq_approver()).json()

        assert body["grant"]["minutes"] == 30

    def test_zero_minutes_approves_this_one_only(self, api: Any, state: SupervisorState) -> None:
        body = _decide(api, pending_approval(state), approver=qq_approver(), grant_minutes=0).json()

        assert body["status"] == "allowed"
        assert body["grant"] is None and body["grant_note"] == GRANT_NOTE_ONCE

    def test_an_admin_asking_for_permanent_gets_it_back(self, api: Any, state: SupervisorState) -> None:
        approval_id = pending_approval(state)

        response = _decide(api, approval_id, approver=qq_approver(), grant_permanent=True)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "allowed" and body["grant_note"] is None
        grant = body["grant"]
        assert grant["permanent"] is True and grant["until"] is None and grant["minutes"] == 0
        assert grant["approval_id"] == approval_id
        assert [item.grant_id for item in state.list_approval_grants()] == [grant["grant_id"]]

    @pytest.mark.parametrize("minutes", [0, 30])
    def test_permanent_and_minutes_together_are_refused(self, api: Any, state: SupervisorState, minutes: int) -> None:
        approval_id = pending_approval(state)

        response = _decide(api, approval_id, approver=qq_approver(), grant_minutes=minutes, grant_permanent=True)

        assert response.status_code == 422
        assert state.approvals[approval_id].status == ApprovalStatus.pending
        assert state.list_approval_grants() == []

    @pytest.mark.parametrize("value", ["maybe", 2, None, [True]])
    def test_a_permanent_flag_that_is_not_a_switch_is_refused(self, api: Any, state: SupervisorState, value: Any) -> None:
        approval_id = pending_approval(state)

        response = _decide(api, approval_id, approver=qq_approver(), grant_permanent=value)

        assert response.status_code == 422
        assert state.approvals[approval_id].status == ApprovalStatus.pending
        assert state.list_approval_grants() == []

    def test_an_old_client_without_an_approver_still_decides(self, api: Any, state: SupervisorState) -> None:
        # 控制台、TUI 和没升级的适配器只发 decision/comment：照旧定案，只是不生成授权。
        approval_id = pending_approval(state)

        response = api("POST", f"/v1/approvals/{approval_id}", json={"decision": "allow", "comment": "ok"})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "allowed" and body["comment"] == "ok"
        assert body["approver"] is None and body["grant"] is None
        assert body["grant_note"] == GRANT_NOTE_NO_APPROVER

    def test_approving_someone_elses_request_returns_no_grant(self, api: Any, state: SupervisorState) -> None:
        body = _decide(api, pending_approval(state, ADMIN), approver=qq_approver(OTHER_ADMIN), grant_minutes=60).json()

        assert body["status"] == "allowed"
        assert body["grant"] is None and body["grant_note"] == GRANT_NOTE_NOT_REQUESTER
        assert state.list_approval_grants() == []

    def test_a_deny_returns_no_grant(self, api: Any, state: SupervisorState) -> None:
        approval_id = pending_approval(state)

        response = api("POST", f"/v1/approvals/{approval_id}", json={
            "decision": "deny", "approver": qq_approver(), "grant_minutes": 60,
        })

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "denied"
        assert body["grant"] is None and body["grant_note"] is None
        assert state.list_approval_grants() == []

    @pytest.mark.parametrize("minutes", [-5, 1.5, "两小时"])
    def test_a_bad_duration_is_refused_and_decides_nothing(self, api: Any, state: SupervisorState, minutes: Any) -> None:
        approval_id = pending_approval(state)

        response = _decide(api, approval_id, approver=qq_approver(), grant_minutes=minutes)

        assert response.status_code == 422, response.text
        assert state.approvals[approval_id].status == ApprovalStatus.pending
        assert state.list_approval_grants() == []

    def test_an_oversized_approver_field_is_refused(self, api: Any, state: SupervisorState) -> None:
        approval_id = pending_approval(state)

        response = _decide(api, approval_id, approver={"platform": "qq", "user_id": "1" * 200})

        assert response.status_code == 422, response.text
        assert state.approvals[approval_id].status == ApprovalStatus.pending

    def test_deciding_needs_the_admin_token(self, api: Any, state: SupervisorState) -> None:
        approval_id = pending_approval(state)

        response = api(
            "POST", f"/v1/approvals/{approval_id}", auth=False,
            json={"decision": "allow", "approver": qq_approver(), "grant_minutes": 600},
        )

        assert response.status_code == 401
        assert state.approvals[approval_id].status == ApprovalStatus.pending
        assert state.list_approval_grants() == []

    def test_an_unknown_approval_is_not_found(self, api: Any) -> None:
        response = _decide(api, "00000000-0000-0000-0000-000000000000", approver=qq_approver())

        assert response.status_code == 404


class TestTheGrantEndpoints:
    def test_listing_shows_a_permanent_grant_without_an_expiry(self, api: Any, state: SupervisorState) -> None:
        grant = grant_for(state, permanent=True)

        response = api("GET", "/v1/approval-grants")

        assert response.status_code == 200, response.text
        [listed] = response.json()["grants"]
        assert listed["grant_id"] == grant.grant_id
        assert listed["permanent"] is True and listed["until"] is None

    def test_revoking_a_permanent_grant(self, api: Any, state: SupervisorState) -> None:
        grant = grant_for(state, permanent=True)

        response = api("POST", "/v1/approval-grants/revoke", json={"identity": qq_approver()})

        assert response.status_code == 200, response.text
        assert response.json()["count"] == 1
        assert response.json()["revoked"][0]["grant_id"] == grant.grant_id
        assert response.json()["revoked"][0]["permanent"] is True
        assert state.list_approval_grants() == []

    def test_listing_shows_the_active_grants(self, api: Any, state: SupervisorState) -> None:
        grant = grant_for(state, minutes=45)

        response = api("GET", "/v1/approval-grants")

        assert response.status_code == 200, response.text
        [listed] = response.json()["grants"]
        assert listed["grant_id"] == grant.grant_id
        assert (listed["platform"], listed["user_id"], listed["bot_scope"]) == ("qq", ADMIN, BOT)
        assert _moment(listed["until"]) == START + timedelta(minutes=45)

    def test_listing_leaves_out_expired_grants(self, api: Any, state: SupervisorState, clock: Clock) -> None:
        grant_for(state, minutes=30)
        clock.advance(minutes=30)

        assert api("GET", "/v1/approval-grants").json() == {"grants": []}

    def test_revoking_by_identity(self, api: Any, state: SupervisorState) -> None:
        grant = grant_for(state)

        response = api("POST", "/v1/approval-grants/revoke", json={"identity": qq_approver()})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["count"] == 1 and [item["grant_id"] for item in body["revoked"]] == [grant.grant_id]
        assert api("GET", "/v1/approval-grants").json() == {"grants": []}

    def test_revoking_by_grant_id(self, api: Any, state: SupervisorState) -> None:
        grant = grant_for(state)

        body = api("POST", "/v1/approval-grants/revoke", json={"grant_id": grant.grant_id}).json()

        assert body["count"] == 1 and body["revoked"][0]["grant_id"] == grant.grant_id
        assert state.list_approval_grants() == []

    def test_grant_id_and_identity_together_must_both_match(self, api: Any, state: SupervisorState) -> None:
        grant = grant_for(state)

        body = api("POST", "/v1/approval-grants/revoke", json={
            "grant_id": grant.grant_id, "identity": qq_approver(OTHER_ADMIN),
        }).json()

        assert body == {"revoked": [], "count": 0}
        assert [item.grant_id for item in state.list_approval_grants()] == [grant.grant_id]

    def test_revoking_someone_without_a_grant_is_zero(self, api: Any, state: SupervisorState) -> None:
        grant_for(state)

        body = api("POST", "/v1/approval-grants/revoke", json={"identity": qq_approver(OTHER_ADMIN)}).json()

        assert body == {"revoked": [], "count": 0}
        assert len(state.list_approval_grants()) == 1

    @pytest.mark.parametrize("payload", [
        pytest.param({}, id="nothing"),
        pytest.param({"grant_id": ""}, id="empty-grant-id"),
        pytest.param({"identity": {"platform": "qq"}}, id="no-user-id"),
        pytest.param({"identity": {"user_id": ADMIN}}, id="no-platform"),
        pytest.param({"identity": {"platform": "qq", "user_id": "not a number"}}, id="invalid-user-id"),
    ])
    def test_revoking_without_a_usable_selector_is_four_hundred(
        self, api: Any, state: SupervisorState, payload: dict[str, Any],
    ) -> None:
        grant_for(state)

        response = api("POST", "/v1/approval-grants/revoke", json=payload)

        assert response.status_code == 400, response.text
        assert len(state.list_approval_grants()) == 1

    @pytest.mark.parametrize(("method", "url", "payload"), [
        ("GET", "/v1/approval-grants", None),
        ("POST", "/v1/approval-grants/revoke", {"identity": {"platform": "qq", "user_id": ADMIN}}),
    ])
    def test_both_need_the_admin_token(
        self, api: Any, state: SupervisorState, method: str, url: str, payload: dict[str, Any] | None,
    ) -> None:
        grant_for(state)

        response = api(method, url, auth=False, json=payload)

        assert response.status_code == 401
        assert len(state.list_approval_grants()) == 1

    def test_a_wrong_token_is_refused_too(self, app: Any, state: SupervisorState) -> None:
        grant_for(state)

        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=_BASE_URL) as http:
                return await http.post(
                    "/v1/approval-grants/revoke",
                    headers={"Authorization": "Bearer not-the-token"},
                    json={"identity": qq_approver()},
                )

        assert asyncio.run(go()).status_code == 401
        assert len(state.list_approval_grants()) == 1


def _sdk_over(app: Any) -> ClonothClient:
    client = ClonothClient(_BASE_URL, admin_token=_TOKEN)
    client._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), headers=_AUTH)
    return client


def _run_sdk(client: ClonothClient, work: Callable[[ClonothClient], Awaitable[Any]]) -> Any:
    async def go() -> Any:
        try:
            return await work(client)
        finally:
            await client.close()

    return asyncio.run(go())


class TestTheSdkAgainstTheSupervisor:
    def test_decide_approval_carries_the_approver_and_returns_the_grant(self, app: Any, state: SupervisorState) -> None:
        approval_id = pending_approval(state)
        approver = PlatformAuth(platform="qq", user_id=ADMIN, is_admin=True, bot_scope=BOT)

        result: ApprovalDecision = _run_sdk(
            _sdk_over(app),
            lambda client: client.decide_approval(approval_id, decision="allow", approver=approver, grant_minutes=90),
        )

        assert result.effective and result.status == "allowed"
        assert result.grant is not None and result.grant_note == ""
        assert (result.grant.platform, result.grant.user_id, result.grant.bot_scope) == ("qq", ADMIN, BOT)
        assert result.grant.minutes == 90 and result.grant.approval_id == approval_id
        assert result.grant.until == START + timedelta(minutes=90)
        assert result.grant.issued_at == START

    def test_a_mapping_works_as_the_approver(self, app: Any, state: SupervisorState) -> None:
        approval_id = pending_approval(state)

        result: ApprovalDecision = _run_sdk(
            _sdk_over(app),
            lambda client: client.decide_approval(approval_id, decision="allow", approver={**qq_auth()}),
        )

        assert result.grant is not None and result.grant.minutes == 30

    def test_the_reason_for_no_grant_comes_back(self, app: Any, state: SupervisorState) -> None:
        approval_id = pending_approval(state, ADMIN)

        result: ApprovalDecision = _run_sdk(
            _sdk_over(app),
            lambda client: client.decide_approval(approval_id, decision="allow", approver=qq_approver(OTHER_ADMIN)),
        )

        assert result.effective
        assert result.grant is None and result.grant_note == GRANT_NOTE_NOT_REQUESTER

    def test_once_comes_back_as_once(self, app: Any, state: SupervisorState) -> None:
        approval_id = pending_approval(state)

        result: ApprovalDecision = _run_sdk(
            _sdk_over(app),
            lambda client: client.decide_approval(
                approval_id, decision="allow", approver=qq_approver(), grant_minutes=0,
            ),
        )

        assert result.grant is None and result.grant_note == GRANT_NOTE_ONCE

    def test_asking_for_permanent(self, app: Any, state: SupervisorState) -> None:
        approval_id = pending_approval(state)

        result: ApprovalDecision = _run_sdk(
            _sdk_over(app),
            lambda client: client.decide_approval(
                approval_id, decision="allow", approver=qq_approver(), grant_permanent=True,
            ),
        )

        assert result.effective and result.grant_note == ""
        assert result.grant is not None and result.grant.permanent
        assert result.grant.until is None and result.grant.minutes == 0

    def test_a_refused_permanent_comes_back_timed_with_its_reason(
        self, app: Any, state: SupervisorState, tmp_path: Path,
    ) -> None:
        write_policy(tmp_path, {"permanent_for_admins": False})
        approval_id = pending_approval(state)

        result: ApprovalDecision = _run_sdk(
            _sdk_over(app),
            lambda client: client.decide_approval(
                approval_id, decision="allow", approver=qq_approver(), grant_permanent=True,
            ),
        )

        assert result.grant is not None and not result.grant.permanent
        assert result.grant.minutes == 1440 and result.grant.until == START + timedelta(minutes=1440)
        assert result.grant_note == GRANT_NOTE_PERMANENT_DISABLED

    def test_listing_a_permanent_grant(self, app: Any, state: SupervisorState) -> None:
        grant = grant_for(state, permanent=True)

        [listed] = _run_sdk(_sdk_over(app), lambda client: client.list_approval_grants())

        assert listed.grant_id == grant.grant_id and listed.permanent and listed.until is None

    def test_listing_and_revoking(self, app: Any, state: SupervisorState) -> None:
        grant = grant_for(state)

        async def work(client: ClonothClient) -> tuple[list[ApprovalGrant], list[ApprovalGrant], list[ApprovalGrant]]:
            listed = await client.list_approval_grants()
            revoked = await client.revoke_approval_grants(
                identity=PlatformAuth(platform="qq", user_id=ADMIN, bot_scope=BOT),
            )
            return listed, revoked, await client.list_approval_grants()

        listed, revoked, after = _run_sdk(_sdk_over(app), work)

        assert [item.grant_id for item in listed] == [grant.grant_id]
        assert listed[0].until == grant.until
        assert [item.grant_id for item in revoked] == [grant.grant_id]
        assert after == []

    def test_revoking_one_grant_by_its_id(self, app: Any, state: SupervisorState) -> None:
        grant = grant_for(state)

        revoked = _run_sdk(_sdk_over(app), lambda client: client.revoke_approval_grants(grant_id=grant.grant_id))

        assert [item.grant_id for item in revoked] == [grant.grant_id]
        assert state.list_approval_grants() == []


def _sdk_mock(handler: Callable[[httpx.Request], httpx.Response]) -> ClonothClient:
    client = ClonothClient(_BASE_URL, admin_token=_TOKEN)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=_AUTH)
    return client


def _recording(response: dict[str, Any] | Any, *, status: int = 200) -> tuple[list[httpx.Request], Callable[[httpx.Request], httpx.Response]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=response)

    return seen, handler


_GRANT_REPLY = {
    "grant_id": "g-1", "platform": "qq", "user_id": ADMIN, "bot_scope": BOT,
    "until": "2026-10-26T10:00:00Z", "minutes": 120, "approval_id": "a-1", "issued_at": "2026-10-26T08:00:00Z",
}


class TestTheSdkOnTheWire:
    def test_only_the_identifying_fields_of_the_approver_are_sent(self) -> None:
        seen, handler = _recording({"approval_id": "a-1", "status": "allowed"})
        approver = PlatformAuth(
            platform="qq", user_id=ADMIN, is_admin=True, group_role="owner", group_scope="123", bot_scope=BOT,
        )

        _run_sdk(_sdk_mock(handler), lambda client: client.decide_approval(
            "a-1", decision="allow", approver=approver, grant_minutes=45,
        ))

        [request] = seen
        assert json.loads(request.content) == {
            "decision": "allow",
            "approver": {"platform": "qq", "user_id": ADMIN, "bot_scope": BOT},
            "grant_minutes": 45,
        }

    def test_without_the_new_arguments_the_body_is_unchanged(self) -> None:
        seen, handler = _recording({"approval_id": "a-1", "status": "allowed"})

        _run_sdk(_sdk_mock(handler), lambda client: client.decide_approval("a-1", decision="allow", comment="ok"))

        assert json.loads(seen[0].content) == {"decision": "allow", "comment": "ok"}

    def test_permanent_is_sent_as_a_flag(self) -> None:
        seen, handler = _recording({"approval_id": "a-1", "status": "allowed"})

        _run_sdk(_sdk_mock(handler), lambda client: client.decide_approval("a-1", decision="allow", grant_permanent=True))

        assert json.loads(seen[0].content) == {"decision": "allow", "grant_permanent": True}

    @pytest.mark.parametrize("minutes", [0, 30])
    def test_permanent_and_minutes_together_send_nothing(self, minutes: int) -> None:
        seen, handler = _recording({"approval_id": "a-1", "status": "allowed"})

        with pytest.raises(ValueError):
            _run_sdk(_sdk_mock(handler), lambda client: client.decide_approval(
                "a-1", decision="allow", grant_minutes=minutes, grant_permanent=True,
            ))

        assert seen == []

    @pytest.mark.parametrize(("given", "sent"), [(0, 0), (-3, 0), (90, 90)])
    def test_the_duration_is_never_sent_negative(self, given: int, sent: int) -> None:
        seen, handler = _recording({"approval_id": "a-1", "status": "allowed"})

        _run_sdk(_sdk_mock(handler), lambda client: client.decide_approval("a-1", decision="allow", grant_minutes=given))

        assert json.loads(seen[0].content)["grant_minutes"] == sent

    def test_a_wrong_approver_type_sends_nothing(self) -> None:
        seen, handler = _recording({"approval_id": "a-1", "status": "allowed"})

        with pytest.raises(TypeError):
            _run_sdk(_sdk_mock(handler), lambda client: client.decide_approval("a-1", decision="allow", approver=42))

        assert seen == []

    def test_an_old_supervisor_reply_has_no_grant(self) -> None:
        _seen, handler = _recording({"approval_id": "a-1", "status": "allowed", "comment": "ok"})

        result = _run_sdk(_sdk_mock(handler), lambda client: client.decide_approval("a-1", decision="allow"))

        assert result.effective and result.grant is None and result.grant_note == ""

    def test_the_grant_in_the_reply_is_parsed(self) -> None:
        _seen, handler = _recording({"approval_id": "a-1", "status": "allowed", "grant": _GRANT_REPLY})

        result = _run_sdk(_sdk_mock(handler), lambda client: client.decide_approval("a-1", decision="allow"))

        assert result.grant == ApprovalGrant(
            grant_id="g-1", platform="qq", user_id=ADMIN, bot_scope=BOT,
            until=datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc), minutes=120, approval_id="a-1",
            issued_at=datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc),
        )

    @pytest.mark.parametrize("grant", [
        pytest.param({"minutes": 5}, id="no-grant-id"),
        pytest.param("g-1", id="not-a-mapping"),
        pytest.param([], id="a-list"),
    ])
    def test_a_malformed_grant_in_the_reply_is_dropped(self, grant: Any) -> None:
        _seen, handler = _recording({"approval_id": "a-1", "status": "allowed", "grant": grant, "grant_note": None})

        result = _run_sdk(_sdk_mock(handler), lambda client: client.decide_approval("a-1", decision="allow"))

        assert result.grant is None and result.grant_note == ""

    def test_revoking_without_a_selector_sends_nothing(self) -> None:
        seen, handler = _recording({"revoked": [], "count": 0})

        with pytest.raises(ValueError):
            _run_sdk(_sdk_mock(handler), lambda client: client.revoke_approval_grants(grant_id="  "))

        assert seen == []

    def test_revoking_sends_the_selectors_given(self) -> None:
        seen, handler = _recording({"revoked": [_GRANT_REPLY], "count": 1})

        revoked = _run_sdk(_sdk_mock(handler), lambda client: client.revoke_approval_grants(
            grant_id=" g-1 ", identity={"platform": "qq", "user_id": ADMIN, "is_admin": True},
        ))

        [request] = seen
        assert request.url.path == "/v1/approval-grants/revoke"
        assert json.loads(request.content) == {
            "grant_id": "g-1", "identity": {"platform": "qq", "user_id": ADMIN, "bot_scope": ""},
        }
        assert [item.grant_id for item in revoked] == ["g-1"]

    @pytest.mark.parametrize("reply", [{"grants": "x"}, {"grants": [{"minutes": 1}, _GRANT_REPLY]}, ["not", "a", "mapping"]])
    def test_listing_tolerates_an_odd_reply(self, reply: Any) -> None:
        _seen, handler = _recording(reply)

        listed = _run_sdk(_sdk_mock(handler), lambda client: client.list_approval_grants())

        assert [item.grant_id for item in listed] == (["g-1"] if isinstance(reply, dict) and isinstance(reply["grants"], list) else [])

    @pytest.mark.parametrize("call", [
        pytest.param(lambda client: client.list_approval_grants(), id="list"),
        pytest.param(lambda client: client.revoke_approval_grants(grant_id="g-1"), id="revoke"),
    ])
    def test_an_http_error_is_raised(self, call: Callable[[ClonothClient], Awaitable[Any]]) -> None:
        _seen, handler = _recording({"detail": "Unauthorized"}, status=401)

        with pytest.raises(httpx.HTTPStatusError):
            _run_sdk(_sdk_mock(handler), call)


class TestTheGrantShape:
    @pytest.mark.parametrize(("raw", "expected"), [
        ("2026-10-26T10:00:00Z", datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)),
        ("2026-10-26T18:00:00+08:00", datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)),
        ("2026-10-26T10:00:00", datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)),
        ("soon", None),
        ("", None),
        (None, None),
    ])
    def test_until_is_read_as_an_aware_time(self, raw: Any, expected: datetime | None) -> None:
        grant = ApprovalGrant.from_dict({**_GRANT_REPLY, "until": raw})

        assert grant is not None and grant.until == expected

    @pytest.mark.parametrize(("raw", "expected"), [(45, 45), ("45", 45), (-3, 0), (True, 0), ("abc", 0), (None, 0)])
    def test_minutes_is_a_non_negative_whole_number(self, raw: Any, expected: int) -> None:
        grant = ApprovalGrant.from_dict({**_GRANT_REPLY, "minutes": raw})

        assert grant is not None and grant.minutes == expected

    @pytest.mark.parametrize(("raw", "expected"), [
        (True, True), (False, False), ("true", False), (1, False), (None, False), ("yes", False),
    ])
    def test_permanent_takes_only_a_real_true(self, raw: Any, expected: bool) -> None:
        grant = ApprovalGrant.from_dict({**_GRANT_REPLY, "until": None, "minutes": 0, "permanent": raw})

        assert grant is not None and grant.permanent is expected

    def test_a_permanent_grant_in_the_reply_is_parsed(self) -> None:
        reply = {**_GRANT_REPLY, "until": None, "minutes": 0, "permanent": True}
        _seen, handler = _recording({"approval_id": "a-1", "status": "allowed", "grant": reply})

        result = _run_sdk(_sdk_mock(handler), lambda client: client.decide_approval("a-1", decision="allow"))

        assert result.grant is not None
        assert result.grant.permanent and result.grant.until is None and result.grant.minutes == 0

    def test_the_decision_can_still_be_built_positionally(self) -> None:
        # 调用方按位置构造回执的写法要继续成立：新字段都排在最后、带默认值。
        decision = ApprovalDecision("a-1", "allow", "allowed", "ok")

        assert decision.effective and decision.grant is None and decision.grant_note == ""
