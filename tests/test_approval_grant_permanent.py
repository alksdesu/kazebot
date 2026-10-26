"""永久授权和授权落盘：管理员同意自己发起的请求时可以要永久的，收回、移出管理员名单或关掉开关前一直免审；生效中的授权另存一份，事件日志没了也不丢。"""
from __future__ import annotations

import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

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
    QQ_KEY,
    SENSITIVE_WRITE,
    START,
    Clock,
    approve,
    drop_event_logs,
    events_path,
    grant_for,
    grants_file,
    logged_events,
    make_state,
    pending_approval,
    qq_approver,
    qq_auth,
    request_op,
    start_task,
    write_policy,
)
from supervisor import approval_grants  # noqa: E402
from supervisor.approval_grants import (  # noqa: E402
    GRANT_NOTE_NO_REQUESTER,
    GRANT_NOTE_NOT_REQUESTER,
    GRANT_NOTE_PERMANENT_DISABLED,
    GRANT_NOTE_PERMANENT_NOT_ADMIN,
    ApprovalGrantBook,
    ApprovalGrantFile,
    GrantIdentity,
)
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import ApprovalDecisionIn, ApprovalGrant, OpRequestOut, SafetyLevel  # noqa: E402

WEB_KEY = "web:console-u1"
WEB_USER = "u1"


def web_auth(*, is_admin: bool) -> dict[str, Any]:
    """控制台这类内部入口的任务：没有外部平台那几道硬拒绝，发起人在不在管理员名单里只看 is_admin。"""
    return {"platform": "web", "user_id": WEB_USER, "is_admin": is_admin}


def web_approver() -> dict[str, str]:
    return {"platform": "web", "user_id": WEB_USER, "bot_scope": ""}


def web_request(state: SupervisorState, *, is_admin: bool) -> OpRequestOut:
    sid, task = start_task(state, auth=web_auth(is_admin=is_admin), key=WEB_KEY, channel="web")
    return request_op(state, sid, task.task_id)


def web_pending(state: SupervisorState, *, is_admin: bool) -> str:
    out = web_request(state, is_admin=is_admin)
    assert out.safety_level == SafetyLevel.approval_required, out
    assert out.approval_id
    return out.approval_id


def qq_request(state: SupervisorState, user_id: str = ADMIN) -> OpRequestOut:
    sid, task = start_task(state, auth=qq_auth(user_id))
    return request_op(state, sid, task.task_id)


def saved_grants(workspace: Path) -> list[dict[str, Any]]:
    body = json.loads(grants_file(workspace).read_text(encoding="utf-8"))
    assert body["version"] == approval_grants.GRANT_FILE_VERSION
    return body["grants"]


def write_grants_file(workspace: Path, body: Any) -> None:
    path = grants_file(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")


def raw_grant(**overrides: Any) -> dict[str, Any]:
    body = {
        "grant_id": "g-file", "platform": "qq", "user_id": ADMIN, "bot_scope": BOT,
        "until": None, "minutes": 0, "approval_id": "a-file", "issued_at": START.isoformat(), "permanent": True,
    }
    return {**body, **overrides}


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr("supervisor.state._now", clock)
    return clock


@pytest.fixture()
def state(tmp_path: Path, clock: Clock) -> SupervisorState:
    return make_state(tmp_path)


class TestWhoGetsAPermanentGrant:
    def test_an_admin_approving_their_own_request_gets_one(self, state: SupervisorState, clock: Clock) -> None:
        approval = approve(state, pending_approval(state), approver=qq_approver(), permanent=True)

        assert approval is not None and approval.grant is not None
        grant = approval.grant
        assert grant.permanent and grant.until is None and grant.minutes == 0
        assert (grant.platform, grant.user_id, grant.bot_scope) == ("qq", ADMIN, BOT)
        assert approval.grant_note is None
        clock.advance(days=400)
        out = qq_request(state)
        assert out.safety_level == SafetyLevel.auto
        assert out.reason.startswith(f"auto-approved: approval grant {grant.grant_id} until revoked")

    def test_the_account_comes_from_the_request_when_the_approver_leaves_it_out(self, state: SupervisorState) -> None:
        approval = approve(state, pending_approval(state), approver=qq_approver(bot_scope=""), permanent=True)

        assert approval is not None and approval.grant is not None
        assert approval.grant.permanent and approval.grant.bot_scope == BOT

    def test_someone_outside_the_admin_list_gets_the_ceiling_instead(self, state: SupervisorState, clock: Clock) -> None:
        approval = approve(state, web_pending(state, is_admin=False), approver=web_approver(), permanent=True)

        assert approval is not None and approval.grant is not None
        assert not approval.grant.permanent
        assert approval.grant.minutes == 1440 and approval.grant.until == clock.now + timedelta(minutes=1440)
        assert approval.grant_note == GRANT_NOTE_PERMANENT_NOT_ADMIN

    def test_the_ceiling_follows_the_policy(self, tmp_path: Path, clock: Clock) -> None:
        write_policy(tmp_path, {"default_minutes": 30, "max_minutes": 90})
        state = make_state(tmp_path)

        approval = approve(state, web_pending(state, is_admin=False), approver=web_approver(), permanent=True)

        assert approval is not None and approval.grant is not None and approval.grant.minutes == 90

    def test_the_policy_can_switch_it_off(self, tmp_path: Path, clock: Clock) -> None:
        write_policy(tmp_path, {"permanent_for_admins": False})
        state = make_state(tmp_path)

        approval = approve(state, pending_approval(state), approver=qq_approver(), permanent=True)

        assert approval is not None and approval.grant is not None
        assert not approval.grant.permanent and approval.grant.minutes == 1440
        assert approval.grant_note == GRANT_NOTE_PERMANENT_DISABLED

    def test_an_admin_on_an_internal_entry_gets_one_too(self, state: SupervisorState) -> None:
        approval = approve(state, web_pending(state, is_admin=True), approver=web_approver(), permanent=True)

        assert approval is not None and approval.grant is not None and approval.grant.permanent

    def test_asking_on_someone_elses_request_grants_nothing(self, state: SupervisorState) -> None:
        approval = approve(state, pending_approval(state, OTHER_ADMIN), approver=qq_approver(ADMIN), permanent=True)

        assert approval is not None
        assert approval.grant is None and approval.grant_note == GRANT_NOTE_NOT_REQUESTER

    def test_an_approval_without_a_task_grants_nothing(self, state: SupervisorState) -> None:
        sid = state.get_or_create_session(channel="qq_private", conversation_key="qq_private:x")
        # 没有 task_id 时 details 里的身份是调用方自报的，自称管理员也不算。
        created = state.create_approval(
            session_id=sid, operation="write_file", details={**SENSITIVE_WRITE, "platform_auth": qq_auth()},
        )

        approval = approve(state, created.approval_id, approver=qq_approver(), permanent=True)

        assert approval is not None
        assert approval.grant is None and approval.grant_note == GRANT_NOTE_NO_REQUESTER

    def test_the_admin_flag_comes_from_what_the_supervisor_recorded(self, state: SupervisorState) -> None:
        # details 里的身份是 supervisor 按任务写进去的：任务上的记录事后变了，按审批挂起时那份算。
        approval_id = pending_approval(state)
        task = state.tasks[state.approvals[approval_id].task_id or ""]
        task.input["task_context"]["platform_auth"]["is_admin"] = False

        approval = approve(state, approval_id, approver=qq_approver(), permanent=True)

        assert approval is not None and approval.grant is not None and approval.grant.permanent

    def test_the_decision_event_records_it(self, state: SupervisorState, tmp_path: Path) -> None:
        approval = approve(state, pending_approval(state), approver=qq_approver(), permanent=True)

        [event] = logged_events(tmp_path, "approval_decided")
        grant = event["payload"]["grant"]
        assert approval is not None and approval.grant is not None
        assert grant["grant_id"] == approval.grant.grant_id
        assert grant["permanent"] is True and grant["until"] is None and grant["minutes"] == 0
        assert event["payload"]["grant_note"] is None

    def test_the_refusal_reason_is_recorded_with_the_timed_grant(self, state: SupervisorState, tmp_path: Path) -> None:
        approve(state, web_pending(state, is_admin=False), approver=web_approver(), permanent=True)

        [event] = logged_events(tmp_path, "approval_decided")
        assert event["payload"]["grant"]["permanent"] is False
        assert event["payload"]["grant_note"] == GRANT_NOTE_PERMANENT_NOT_ADMIN


class TestWhatAPermanentGrantCovers:
    def test_it_stops_counting_while_the_requester_is_off_the_admin_list(self, state: SupervisorState) -> None:
        approve(state, web_pending(state, is_admin=True), approver=web_approver(), permanent=True)

        assert web_request(state, is_admin=True).safety_level == SafetyLevel.auto
        assert web_request(state, is_admin=False).safety_level == SafetyLevel.approval_required
        # 授权还在：回到名单就又算数，不用再批一次。
        assert web_request(state, is_admin=True).safety_level == SafetyLevel.auto

    def test_a_timed_grant_still_counts_off_the_admin_list(self, state: SupervisorState) -> None:
        approve(state, web_pending(state, is_admin=False), approver=web_approver(), minutes=60)

        assert web_request(state, is_admin=False).safety_level == SafetyLevel.auto

    def test_switching_the_policy_off_puts_it_to_sleep(self, tmp_path: Path, clock: Clock) -> None:
        state = make_state(tmp_path)
        grant_for(state, permanent=True)

        write_policy(tmp_path, {"permanent_for_admins": False})
        assert qq_request(state).safety_level == SafetyLevel.approval_required

        write_policy(tmp_path, {"permanent_for_admins": True})
        assert qq_request(state).safety_level == SafetyLevel.auto

    def test_a_dispatched_child_task_rides_on_it(self, state: SupervisorState) -> None:
        grant_for(state, permanent=True)
        _parent_sid, parent = start_task(state, auth=qq_auth())
        child_sid, child = start_task(
            state, auth=None, key=f"agent:bootstrap.executor:{QQ_KEY}:abc", caller_task_id=parent.task_id,
            extra_context={"parent_conversation_key": QQ_KEY, "route_conversation_key": QQ_KEY},
        )

        assert request_op(state, child_sid, child.task_id).safety_level == SafetyLevel.auto

    def test_a_policy_deny_still_wins(self, state: SupervisorState) -> None:
        grant_for(state, permanent=True)
        sid, task = start_task(state, auth=qq_auth())

        out = request_op(state, sid, task.task_id, "write_file", {"path": "config/policy.yaml"})

        assert out.safety_level == SafetyLevel.deny

    def test_the_external_non_admin_hard_denial_still_wins(self, state: SupervisorState) -> None:
        grant_for(state, permanent=True)
        sid, task = start_task(state, auth=qq_auth(is_admin=False))

        out = request_op(state, sid, task.task_id, "write_file", {"path": "notes.txt"})

        assert out.safety_level == SafetyLevel.deny

    def test_another_person_does_not_ride_on_it(self, state: SupervisorState) -> None:
        grant_for(state, permanent=True)

        assert qq_request(state, OTHER_ADMIN).safety_level == SafetyLevel.approval_required

    def test_the_audit_entry_marks_it(self, state: SupervisorState, tmp_path: Path) -> None:
        grant = grant_for(state, permanent=True)

        qq_request(state)

        [used] = logged_events(tmp_path, "approval_grant_used")
        assert used["payload"]["grant_id"] == grant.grant_id
        assert used["payload"]["permanent"] is True and used["payload"]["until"] is None

    def test_a_timed_audit_entry_says_it_is_not_permanent(self, state: SupervisorState, tmp_path: Path) -> None:
        grant = grant_for(state, minutes=60)

        qq_request(state)

        [used] = logged_events(tmp_path, "approval_grant_used")
        assert used["payload"]["permanent"] is False and used["payload"]["until"] == grant.until.isoformat()

    def test_revoking_ends_it(self, state: SupervisorState, tmp_path: Path) -> None:
        grant_for(state, permanent=True)

        [revoked] = state.revoke_approval_grants(identity=qq_approver())

        assert revoked.permanent
        assert qq_request(state).safety_level == SafetyLevel.approval_required
        [event] = logged_events(tmp_path, "approval_grant_revoked")
        assert event["payload"]["grants"][0]["permanent"] is True

    def test_it_is_listed_after_the_timed_ones(self, state: SupervisorState) -> None:
        permanent = grant_for(state, permanent=True)
        timed = grant_for(state, user_id=OTHER_ADMIN, minutes=60)

        assert [grant.grant_id for grant in state.list_approval_grants()] == [timed.grant_id, permanent.grant_id]


class TestOneGrantPerPerson:
    def test_a_timed_approval_keeps_the_permanent_grant(self, state: SupervisorState) -> None:
        # 离开名单期间永久授权不算数，请求照样挂审批；这时批一个限时的，不能把永久的那条顶掉。
        approve(state, web_pending(state, is_admin=True), approver=web_approver(), permanent=True)

        approval = approve(state, web_pending(state, is_admin=False), approver=web_approver(), minutes=30)

        assert approval is not None and approval.grant is not None
        assert approval.grant.permanent and approval.grant.until is None and approval.grant.minutes == 0
        [only] = state.list_approval_grants()
        assert only.grant_id == approval.grant.grant_id and only.permanent

    def test_a_permanent_approval_replaces_a_timed_grant(self, state: SupervisorState) -> None:
        approve(state, web_pending(state, is_admin=False), approver=web_approver(), minutes=30)
        sid, task = start_task(state, auth=web_auth(is_admin=True), key=WEB_KEY, channel="web")
        # 限时授权在的时候他的请求直接放行，挂不出审批：照 supervisor 的写法直接建一条。
        created = state.create_approval(
            session_id=sid, operation="write_file",
            details={**SENSITIVE_WRITE, "platform_auth": web_auth(is_admin=True)}, task_id=task.task_id,
        )

        approval = approve(state, created.approval_id, approver=web_approver(), permanent=True)

        assert approval is not None and approval.grant is not None and approval.grant.permanent
        [only] = state.list_approval_grants()
        assert only.permanent


class TestTheBook:
    IDENTITY = GrantIdentity(platform="qq", user_id=ADMIN, bot_scope=BOT)

    def test_a_permanent_draft_never_runs_out(self) -> None:
        book = ApprovalGrantBook()
        grant = book.draft(self.IDENTITY, minutes=10, permanent=True, approval_id="a", now=START)

        assert grant.permanent and grant.until is None and grant.minutes == 0
        assert book.store(grant, now=START) is not None
        assert book.active_for(self.IDENTITY, now=START + timedelta(days=3650)) == grant

    def test_a_timed_draft_does_not_shorten_a_permanent_grant(self) -> None:
        book = ApprovalGrantBook()
        book.store(book.draft(self.IDENTITY, minutes=0, permanent=True, approval_id="a", now=START), now=START)

        later = book.draft(self.IDENTITY, minutes=30, approval_id="b", now=START)

        assert later.permanent and later.until is None and later.approval_id == "b"

    def test_a_permanent_draft_outlasts_a_timed_grant(self) -> None:
        book = ApprovalGrantBook()
        book.store(book.draft(self.IDENTITY, minutes=600, approval_id="a", now=START), now=START)

        later = book.draft(self.IDENTITY, minutes=0, permanent=True, approval_id="b", now=START)

        assert later.permanent and later.until is None

    def test_permanent_grants_can_be_left_out_of_a_lookup(self) -> None:
        book = ApprovalGrantBook()
        book.store(book.draft(self.IDENTITY, minutes=0, permanent=True, approval_id="a", now=START), now=START)

        assert book.active_for(self.IDENTITY, now=START, permanent_ok=False) is None
        assert book.active_for(self.IDENTITY, now=START) is not None

    def test_a_timed_grant_under_another_account_still_answers_without_permanent(self) -> None:
        book = ApprovalGrantBook()
        loose = GrantIdentity(platform="qq", user_id=ADMIN)
        other_account = GrantIdentity(platform="qq", user_id=ADMIN, bot_scope="90002")
        book.store(book.draft(self.IDENTITY, minutes=0, permanent=True, approval_id="a", now=START), now=START)
        timed = book.store(book.draft(other_account, minutes=60, approval_id="b", now=START), now=START)

        assert book.active_for(loose, now=START, permanent_ok=False) == timed
        found = book.active_for(loose, now=START)
        assert found is not None and found.permanent

    def test_pruning_never_drops_a_permanent_grant(self) -> None:
        book = ApprovalGrantBook()
        book.store(book.draft(self.IDENTITY, minutes=0, permanent=True, approval_id="a", now=START), now=START)

        assert book.prune(now=START + timedelta(days=36500)) == 0
        assert len(book.active(now=START + timedelta(days=36500))) == 1


class TestTheGrantShape:
    def test_a_permanent_grant_cannot_carry_an_expiry(self) -> None:
        with pytest.raises(ValidationError):
            ApprovalGrant.model_validate(raw_grant(until=START.isoformat()))

    def test_a_missing_expiry_does_not_make_a_grant_permanent(self) -> None:
        with pytest.raises(ValidationError):
            ApprovalGrant.model_validate(raw_grant(permanent=False))
        body = raw_grant()
        del body["until"]
        with pytest.raises(ValidationError):
            ApprovalGrant.model_validate(body)

    def test_an_old_timed_grant_reads_as_before(self) -> None:
        body = raw_grant(until=(START + timedelta(hours=1)).isoformat(), minutes=60)
        del body["permanent"]

        grant = ApprovalGrant.model_validate(body)

        assert not grant.permanent and grant.until == START + timedelta(hours=1)

    @pytest.mark.parametrize("minutes", [0, 30])
    def test_permanent_and_minutes_cannot_be_asked_for_together(self, minutes: int) -> None:
        with pytest.raises(ValidationError):
            ApprovalDecisionIn(decision="allow", grant_minutes=minutes, grant_permanent=True)

    def test_asking_for_permanent_alone_is_fine(self) -> None:
        body = ApprovalDecisionIn(decision="allow", grant_permanent=True)

        assert body.grant_permanent and body.grant_minutes is None


class TestTheGrantsFile:
    def test_every_change_is_written_down(self, state: SupervisorState, tmp_path: Path) -> None:
        grant = grant_for(state, permanent=True)

        [saved] = saved_grants(tmp_path)
        assert saved["grant_id"] == grant.grant_id and saved["permanent"] is True and saved["until"] is None

        state.revoke_approval_grants(identity=qq_approver())

        assert saved_grants(tmp_path) == []

    def test_a_denial_leaves_the_file_alone(self, state: SupervisorState, tmp_path: Path) -> None:
        grant = grant_for(state, permanent=True)
        before = grants_file(tmp_path).read_bytes()
        approval_id = pending_approval(state, OTHER_ADMIN)

        state.decide_approval(approval_id=approval_id, decision="deny", approver=qq_approver(OTHER_ADMIN))

        assert grants_file(tmp_path).read_bytes() == before
        assert [item["grant_id"] for item in saved_grants(tmp_path)] == [grant.grant_id]

    @pytest.mark.parametrize("permanent", [True, False], ids=["permanent", "timed"])
    def test_a_grant_survives_losing_the_event_log(self, state: SupervisorState, tmp_path: Path, permanent: bool) -> None:
        grant = grant_for(state, permanent=permanent, minutes=None if permanent else 600)

        drop_event_logs(tmp_path)
        restarted = make_state(tmp_path, run_id="run-after-restart")

        assert [item.grant_id for item in restarted.list_approval_grants()] == [grant.grant_id]
        assert qq_request(restarted).safety_level == SafetyLevel.auto

    def test_a_revocation_survives_losing_the_event_log(self, state: SupervisorState, tmp_path: Path) -> None:
        grant_for(state, permanent=True)
        state.revoke_approval_grants(identity=qq_approver())

        drop_event_logs(tmp_path)
        restarted = make_state(tmp_path, run_id="run-after-restart")

        assert restarted.list_approval_grants() == []
        assert qq_request(restarted).safety_level == SafetyLevel.approval_required

    def test_without_the_file_grants_are_rebuilt_from_events(self, state: SupervisorState, tmp_path: Path) -> None:
        grant = grant_for(state, permanent=True)
        grants_file(tmp_path).unlink()

        restarted = make_state(tmp_path, run_id="run-after-restart")

        assert [item.grant_id for item in restarted.list_approval_grants()] == [grant.grant_id]
        assert [item["grant_id"] for item in saved_grants(tmp_path)] == [grant.grant_id]

    @pytest.mark.parametrize("body", [
        pytest.param("{not json", id="not-json"),
        pytest.param({"version": 99, "grants": []}, id="another-version"),
        pytest.param({"version": 1, "grants": "g-1"}, id="grants-not-a-list"),
        pytest.param(["g-1"], id="not-a-mapping"),
    ])
    def test_an_unusable_file_falls_back_to_events(self, state: SupervisorState, tmp_path: Path, body: Any) -> None:
        grant = grant_for(state, permanent=True)
        write_grants_file(tmp_path, body)

        restarted = make_state(tmp_path, run_id="run-after-restart")

        assert [item.grant_id for item in restarted.list_approval_grants()] == [grant.grant_id]
        assert [item["grant_id"] for item in saved_grants(tmp_path)] == [grant.grant_id]

    @pytest.mark.parametrize("bad", [
        pytest.param({"grant_id": "g-broken"}, id="half-a-grant"),
        pytest.param(raw_grant(grant_id="g-null-until", permanent=False), id="until-null-but-not-permanent"),
        pytest.param(raw_grant(grant_id="g-dated-permanent", until=START.isoformat()), id="permanent-with-an-expiry"),
        pytest.param("g-1", id="not-a-mapping"),
    ])
    def test_a_malformed_entry_is_skipped_and_the_rest_kept(self, tmp_path: Path, clock: Clock, bad: Any) -> None:
        write_grants_file(tmp_path, {"version": 1, "grants": [bad, raw_grant()]})

        state = make_state(tmp_path)

        assert [item.grant_id for item in state.list_approval_grants()] == ["g-file"]
        assert [item["grant_id"] for item in saved_grants(tmp_path)] == ["g-file"]

    def test_an_expired_grant_in_the_file_is_dropped(self, tmp_path: Path, clock: Clock) -> None:
        until = START + timedelta(minutes=30)
        write_grants_file(tmp_path, {"version": 1, "grants": [
            raw_grant(grant_id="g-old", until=until.isoformat(), minutes=30, permanent=False),
        ]})
        clock.advance(minutes=31)

        state = make_state(tmp_path)

        assert state.list_approval_grants() == []
        assert saved_grants(tmp_path) == []

    def test_events_newer_than_the_file_are_replayed_on_top(
        self, state: SupervisorState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        grant_for(state, permanent=True)
        written = grants_file(tmp_path).read_bytes()

        def disk_full(self: ApprovalGrantFile, grants: Any) -> None:
            raise OSError("no space left on device")

        monkeypatch.setattr(ApprovalGrantFile, "write", disk_full)
        # 写不进文件不影响收回本身：事件记下了，重启时补得回来。
        assert len(state.revoke_approval_grants(identity=qq_approver())) == 1
        later = grant_for(state, user_id=OTHER_ADMIN, permanent=True)
        monkeypatch.undo()
        assert grants_file(tmp_path).read_bytes() == written

        restarted = make_state(tmp_path, run_id="run-after-restart")

        assert [item.grant_id for item in restarted.list_approval_grants()] == [later.grant_id]

    def test_a_failed_write_is_logged_not_raised(
        self, state: SupervisorState, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        def disk_full(self: ApprovalGrantFile, grants: Any) -> None:
            raise OSError("no space left on device")

        monkeypatch.setattr(ApprovalGrantFile, "write", disk_full)

        with caplog.at_level("ERROR", logger="supervisor.state"):
            approval = approve(state, pending_approval(state), approver=qq_approver(), permanent=True)

        assert approval is not None and approval.grant is not None and approval.grant.permanent
        assert any("approval grants file write failed" in record.getMessage() for record in caplog.records)
        assert qq_request(state).safety_level == SafetyLevel.auto

    @pytest.mark.parametrize("kept_from", [1, 2], ids=["issue-rotated-out", "issue-and-revocation-rotated-out"])
    def test_a_rotated_log_on_top_of_the_file_reaches_the_same_answer(
        self, state: SupervisorState, tmp_path: Path, kept_from: int,
    ) -> None:
        # 轮转只会挤掉最旧的那几段：在文件上重放留下的这一截，结论和从头重放一样，收回过的不会复活。
        grant_for(state, permanent=True)
        state.revoke_approval_grants(identity=qq_approver())
        later = grant_for(state, permanent=True)
        lines = events_path(tmp_path).read_text(encoding="utf-8").splitlines(keepends=True)
        marks = [
            index for index, line in enumerate(lines)
            if json.loads(line).get("type") in {"approval_decided", "approval_grant_revoked"}
        ]
        assert len(marks) == 3
        events_path(tmp_path).write_text("".join(lines[marks[kept_from]:]), encoding="utf-8")

        restarted = make_state(tmp_path, run_id="run-after-restart")

        assert [item.grant_id for item in restarted.list_approval_grants()] == [later.grant_id]

    @pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
    def test_the_file_is_private(self, state: SupervisorState, tmp_path: Path) -> None:
        grant_for(state, permanent=True)

        assert grants_file(tmp_path).stat().st_mode & 0o777 == 0o600
