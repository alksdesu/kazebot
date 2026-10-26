"""审批授权：本人同意自己发起的请求后限时免审；别人的请求、硬拒绝、定时任务不受影响；重启按事件重建。"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

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
    QQ_KEY,
    SENSITIVE_WRITE,
    START,
    Clock,
    approve,
    grant_for,
    logged_events,
    make_state,
    pending_approval,
    qq_approver,
    qq_auth,
    request_op,
    start_task,
    write_policy,
)
from clonoth_sdk import types as sdk_types  # noqa: E402
from supervisor import approval_grants  # noqa: E402
from supervisor.approval_grants import (  # noqa: E402
    GRANT_NOTE_DISABLED,
    GRANT_NOTE_NO_APPROVER,
    GRANT_NOTE_NO_REQUESTER,
    GRANT_NOTE_NOT_REQUESTER,
    GRANT_NOTE_ONCE,
    ApprovalGrantBook,
    GrantIdentity,
)
from supervisor.state import SupervisorState  # noqa: E402
from supervisor.types import ApprovalStatus, SafetyLevel  # noqa: E402


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr("supervisor.state._now", clock)
    return clock


@pytest.fixture()
def state(tmp_path: Path, clock: Clock) -> SupervisorState:
    return make_state(tmp_path)


class TestWhoGetsAGrant:
    def test_approving_your_own_request_gives_you_a_window(self, state: SupervisorState) -> None:
        approval = approve(state, pending_approval(state), approver=qq_approver())

        assert approval.status == ApprovalStatus.allowed
        assert approval.approver is not None and approval.approver.user_id == ADMIN
        grant = approval.grant
        assert grant is not None
        assert (grant.platform, grant.user_id, grant.bot_scope) == ("qq", ADMIN, BOT)
        assert grant.minutes == 30
        assert grant.until == START + timedelta(minutes=30)
        assert grant.issued_at == START
        assert grant.approval_id == approval.approval_id
        assert approval.grant_note is None
        assert [item.grant_id for item in state.list_approval_grants()] == [grant.grant_id]

    def test_approving_someone_elses_request_grants_nothing(self, state: SupervisorState) -> None:
        approval = approve(state, pending_approval(state, ADMIN), approver=qq_approver(OTHER_ADMIN))

        assert approval.status == ApprovalStatus.allowed
        assert approval.grant is None
        assert approval.grant_note == GRANT_NOTE_NOT_REQUESTER
        assert state.list_approval_grants() == []
        sid, task = start_task(state, auth=qq_auth(OTHER_ADMIN))
        assert request_op(state, sid, task.task_id).safety_level == SafetyLevel.approval_required

    @pytest.mark.parametrize("approver", [None, {}, {"platform": "qq"}, {"user_id": ADMIN}, {"platform": "qq", "user_id": "not a qq id"}])
    def test_an_unknown_approver_grants_nothing(self, state: SupervisorState, approver: dict | None) -> None:
        approval = approve(state, pending_approval(state), approver=approver)

        assert approval.status == ApprovalStatus.allowed
        assert approval.grant is None and approval.approver is None
        assert approval.grant_note == GRANT_NOTE_NO_APPROVER

    def test_another_bot_account_is_another_person(self, state: SupervisorState) -> None:
        approval = approve(state, pending_approval(state), approver=qq_approver(bot_scope="90002"))

        assert approval.grant is None and approval.grant_note == GRANT_NOTE_NOT_REQUESTER

    def test_a_missing_bot_account_on_the_approver_side_still_matches(self, state: SupervisorState) -> None:
        approval = approve(state, pending_approval(state), approver=qq_approver(bot_scope=""))

        # 授权落在请求那边记下的账号上，不会变成对所有账号都生效。
        assert approval.grant is not None and approval.grant.bot_scope == BOT

    def test_an_invalid_bot_account_is_not_treated_as_missing(self, state: SupervisorState) -> None:
        approval = approve(state, pending_approval(state), approver=qq_approver(bot_scope="bot/../x"))

        assert approval.grant is None and approval.grant_note == GRANT_NOTE_NO_APPROVER

    def test_the_platform_is_compared_without_case(self, state: SupervisorState) -> None:
        approval = approve(state, pending_approval(state), approver=qq_approver(platform=" QQ "))

        assert approval.grant is not None and approval.grant.platform == "qq"

    def test_an_approval_without_a_task_never_grants(self, state: SupervisorState) -> None:
        # 没有 task_id 时 details 里的身份是调用方自报的，拿它认人等于谁都能冒充。
        sid = state.get_or_create_session(channel="qq_private", conversation_key=QQ_KEY)
        out = state.request_operation(
            session_id=sid, op="write_file", parameters={**SENSITIVE_WRITE, "platform_auth": qq_auth()},
        )
        assert out.safety_level == SafetyLevel.approval_required

        approval = approve(state, out.approval_id, approver=qq_approver())

        assert approval.grant is None and approval.grant_note == GRANT_NOTE_NO_REQUESTER

    def test_the_requester_comes_from_the_task_when_details_lack_it(self, state: SupervisorState) -> None:
        sid, task = start_task(state, auth=qq_auth())
        approval = state.create_approval(
            session_id=sid, operation="write_file", details=dict(SENSITIVE_WRITE), task_id=task.task_id,
        )

        decided = approve(state, approval.approval_id, approver=qq_approver())

        assert decided.grant is not None and decided.grant.user_id == ADMIN

    def test_the_identity_written_into_details_survives_the_task(self, state: SupervisorState) -> None:
        approval_id = pending_approval(state)
        with state._lock:
            task_id = state.approvals[approval_id].task_id
            state.tasks.pop(task_id)

        decided = approve(state, approval_id, approver=qq_approver())

        assert decided.grant is not None and decided.grant.user_id == ADMIN

    def test_a_deny_never_grants(self, state: SupervisorState) -> None:
        decided = state.decide_approval(
            approval_id=pending_approval(state), decision="deny", comment="no",
            approver=qq_approver(), grant_minutes=60,
        )

        assert decided.status == ApprovalStatus.denied
        assert decided.grant is None and decided.grant_note is None
        assert decided.approver is not None and decided.approver.user_id == ADMIN
        assert state.list_approval_grants() == []

    def test_a_second_decision_changes_nothing(self, state: SupervisorState) -> None:
        approval_id = pending_approval(state)
        approve(state, approval_id, approver=qq_approver(OTHER_ADMIN))

        again = approve(state, approval_id, approver=qq_approver())

        assert again.grant is None and state.list_approval_grants() == []

    def test_the_decision_event_records_approver_and_grant(self, state: SupervisorState, tmp_path: Path) -> None:
        grant = grant_for(state)

        [decided] = logged_events(tmp_path, "approval_decided")
        payload = decided["payload"]
        assert payload["approver"] == {"platform": "qq", "user_id": ADMIN, "bot_scope": BOT}
        assert payload["grant"]["grant_id"] == grant.grant_id
        assert set(payload["grant"]) >= {"grant_id", "platform", "bot_scope", "user_id", "until", "minutes", "approval_id"}
        assert payload["grant_note"] is None


class TestWhatAGrantCovers:
    @pytest.mark.parametrize(("op", "parameters"), [
        ("write_file", {"path": "supervisor/api.py"}),
        ("execute_command", {"command": "pip install requests"}),
        ("restart", {"target": "engine"}),
        ("read_file", {"path": "data/notes.txt"}),
    ])
    def test_sensitive_operations_run_without_approval(
        self, state: SupervisorState, tmp_path: Path, op: str, parameters: dict,
    ) -> None:
        grant = grant_for(state)
        sid, task = start_task(state, auth=qq_auth())
        before = len(state.approvals)

        out = request_op(state, sid, task.task_id, op, parameters)

        assert out.safety_level == SafetyLevel.auto
        assert out.approval_id is None
        assert out.reason.startswith(f"auto-approved: approval grant {grant.grant_id}")
        assert len(state.approvals) == before
        [used] = logged_events(tmp_path, "approval_grant_used")
        assert used["session_id"] == sid
        assert used["payload"]["op"] == op
        assert used["payload"]["task_id"] == task.task_id
        assert used["payload"]["grant_id"] == grant.grant_id
        assert used["payload"]["session_id"] == sid
        assert used["payload"]["sensitive"] is True

    def test_the_audit_entry_names_the_target(self, state: SupervisorState, tmp_path: Path) -> None:
        grant_for(state)
        sid, task = start_task(state, auth=qq_auth())

        request_op(state, sid, task.task_id, "execute_command", {"command": "pip install " + "x" * 2000})

        [used] = logged_events(tmp_path, "approval_grant_used")
        assert used["payload"]["target"].startswith("pip install x")
        assert len(used["payload"]["target"]) == 500

    def test_a_dispatched_child_task_rides_on_the_starters_grant(self, state: SupervisorState) -> None:
        grant_for(state)
        _parent_sid, parent = start_task(state, auth=qq_auth())
        child_key = f"agent:bootstrap.executor:{QQ_KEY}:abc"
        child_sid, child = start_task(
            state, auth=None, key=child_key, caller_task_id=parent.task_id,
            extra_context={"parent_conversation_key": QQ_KEY, "route_conversation_key": QQ_KEY},
        )

        out = request_op(state, child_sid, child.task_id)

        assert out.safety_level == SafetyLevel.auto
        assert out.reason.startswith("auto-approved: approval grant")

    def test_a_policy_deny_still_wins(self, state: SupervisorState) -> None:
        grant_for(state)
        sid, task = start_task(state, auth=qq_auth())

        for path in ("config/policy.yaml", "data/events.jsonl", ".env"):
            assert request_op(state, sid, task.task_id, "write_file", {"path": path}).safety_level == SafetyLevel.deny
        assert request_op(
            state, sid, task.task_id, "execute_command", {"command": "rm -rf /"},
        ).safety_level == SafetyLevel.deny

    def test_the_external_non_admin_hard_denial_still_wins(self, state: SupervisorState) -> None:
        grant_for(state)
        # 同一个人，但这一轮入站时已经不在管理员名单里。
        sid, task = start_task(state, auth=qq_auth(is_admin=False))

        out = request_op(state, sid, task.task_id, "write_file", {"path": "notes.txt"})

        assert out.safety_level == SafetyLevel.deny
        assert out.reason == "qq non-admin users cannot modify files"

    def test_a_scheduler_task_still_refuses_sensitive_operations(self, state: SupervisorState) -> None:
        grant_for(state)
        sid, task = start_task(state, auth=qq_auth(), key="scheduler:nightly", channel="internal")

        out = request_op(state, sid, task.task_id)

        assert out.safety_level == SafetyLevel.deny
        assert out.reason.startswith("scheduler tasks cannot run sensitive operations")

    def test_a_self_reported_identity_without_a_task_is_ignored(self, state: SupervisorState) -> None:
        grant_for(state)
        sid = state.get_or_create_session(channel="qq_private", conversation_key=QQ_KEY)

        out = state.request_operation(
            session_id=sid, op="write_file", parameters={**SENSITIVE_WRITE, "platform_auth": qq_auth()},
        )

        assert out.safety_level == SafetyLevel.approval_required

    def test_a_task_the_supervisor_does_not_know_is_not_trusted(self, state: SupervisorState) -> None:
        grant_for(state)
        sid = state.get_or_create_session(channel="cli", conversation_key="cli:default")

        out = state.request_operation(
            session_id=sid, op="write_file", parameters={**SENSITIVE_WRITE, "platform_auth": qq_auth()},
            task_id="no-such-task",
        )

        assert out.safety_level == SafetyLevel.approval_required

    def test_another_person_does_not_ride_on_it(self, state: SupervisorState) -> None:
        grant_for(state)
        sid, task = start_task(state, auth=qq_auth(OTHER_ADMIN))

        assert request_op(state, sid, task.task_id).safety_level == SafetyLevel.approval_required

    def test_the_same_person_through_another_bot_account_does_not_ride_on_it(self, state: SupervisorState) -> None:
        grant_for(state)
        sid, task = start_task(state, auth=qq_auth(bot_scope="90002"))

        assert request_op(state, sid, task.task_id).safety_level == SafetyLevel.approval_required

    def test_a_task_without_platform_identity_does_not_ride_on_it(self, state: SupervisorState) -> None:
        grant_for(state)
        sid, task = start_task(state, auth=None, key="cli:default", channel="cli")

        assert request_op(state, sid, task.task_id).safety_level == SafetyLevel.approval_required


class TestDuration:
    def test_the_requested_minutes_are_used(self, state: SupervisorState) -> None:
        grant = grant_for(state, minutes=120)

        assert grant.minutes == 120 and grant.until == START + timedelta(minutes=120)

    def test_a_request_above_the_ceiling_is_capped(self, tmp_path: Path, clock: Clock) -> None:
        write_policy(tmp_path, {"default_minutes": 10, "max_minutes": 60})
        state = make_state(tmp_path)

        grant = grant_for(state, minutes=600)

        assert grant.minutes == 60 and grant.until == START + timedelta(minutes=60)

    def test_once_grants_nothing(self, state: SupervisorState) -> None:
        approval = approve(state, pending_approval(state), approver=qq_approver(), minutes=0)

        assert approval.status == ApprovalStatus.allowed
        assert approval.grant is None and approval.grant_note == GRANT_NOTE_ONCE

    def test_a_zero_default_grants_only_when_asked(self, tmp_path: Path, clock: Clock) -> None:
        write_policy(tmp_path, {"default_minutes": 0, "max_minutes": 1440})
        state = make_state(tmp_path)

        plain = approve(state, pending_approval(state), approver=qq_approver())
        asked = approve(state, pending_approval(state), approver=qq_approver(), minutes=45)

        assert plain.grant is None and plain.grant_note == GRANT_NOTE_DISABLED
        assert asked.grant is not None and asked.grant.minutes == 45

    def test_without_the_policy_section_the_built_in_default_applies(self, tmp_path: Path, clock: Clock) -> None:
        write_policy(tmp_path, None)
        state = make_state(tmp_path)

        grant = grant_for(state)

        assert grant.minutes == 30

    def test_one_person_keeps_one_grant_and_the_later_expiry(self, state: SupervisorState, clock: Clock) -> None:
        # 授权生效后本人的请求直接放行，不会再有新的待审批，所以三条都要在授权之前挂好。
        first_id, second_id, third_id = (pending_approval(state) for _ in range(3))
        first = approve(state, first_id, approver=qq_approver(), minutes=120).grant
        clock.advance(minutes=10)

        shorter = approve(state, second_id, approver=qq_approver(), minutes=30).grant

        assert shorter is not None and first is not None
        assert shorter.grant_id != first.grant_id
        assert shorter.until == first.until
        assert [grant.grant_id for grant in state.list_approval_grants()] == [shorter.grant_id]

        longer = approve(state, third_id, approver=qq_approver(), minutes=600).grant

        assert longer is not None and longer.until == START + timedelta(minutes=10 + 600)
        assert [grant.grant_id for grant in state.list_approval_grants()] == [longer.grant_id]

    def test_an_expired_grant_does_not_stretch_a_new_one(self, state: SupervisorState, clock: Clock) -> None:
        grant_for(state, minutes=30)
        clock.advance(minutes=40)

        fresh = grant_for(state, minutes=5)

        assert fresh.until == START + timedelta(minutes=45)


class TestLifetime:
    def test_a_grant_runs_out(self, state: SupervisorState, clock: Clock) -> None:
        grant = grant_for(state, minutes=30)
        sid, task = start_task(state, auth=qq_auth())
        clock.advance(minutes=29)
        assert request_op(state, sid, task.task_id).safety_level == SafetyLevel.auto

        clock.now = grant.until

        assert request_op(state, sid, task.task_id).safety_level == SafetyLevel.approval_required
        assert state.list_approval_grants() == []

    def test_revoking_by_identity(self, state: SupervisorState, tmp_path: Path) -> None:
        grant = grant_for(state)

        revoked = state.revoke_approval_grants(identity=qq_approver())

        assert [item.grant_id for item in revoked] == [grant.grant_id]
        sid, task = start_task(state, auth=qq_auth())
        assert request_op(state, sid, task.task_id).safety_level == SafetyLevel.approval_required
        [event] = logged_events(tmp_path, "approval_grant_revoked")
        assert event["payload"]["grant_ids"] == [grant.grant_id]
        assert event["payload"]["count"] == 1

    def test_revoking_by_grant_id(self, state: SupervisorState, tmp_path: Path) -> None:
        grant = grant_for(state)

        assert state.revoke_approval_grants(grant_id="someone-elses") == []
        assert logged_events(tmp_path, "approval_grant_revoked") == []
        assert [item.grant_id for item in state.revoke_approval_grants(grant_id=grant.grant_id)] == [grant.grant_id]
        assert state.list_approval_grants() == []

    def test_revoking_follows_the_same_person_rule(self, state: SupervisorState) -> None:
        grant_for(state)

        assert state.revoke_approval_grants(identity=qq_approver(bot_scope="90002")) == []
        assert state.revoke_approval_grants(identity=qq_approver(OTHER_ADMIN)) == []
        assert len(state.revoke_approval_grants(identity=qq_approver(bot_scope=""))) == 1

    def test_an_expired_grant_is_not_counted_as_revoked(self, state: SupervisorState, clock: Clock) -> None:
        grant_for(state, minutes=30)
        clock.advance(minutes=31)

        assert state.revoke_approval_grants(identity=qq_approver()) == []

    @pytest.mark.parametrize("kwargs", [{}, {"identity": {"platform": "qq"}}, {"identity": {"user_id": ADMIN}}])
    def test_revoking_needs_a_usable_selector(self, state: SupervisorState, kwargs: dict) -> None:
        grant_for(state)

        with pytest.raises(ValueError):
            state.revoke_approval_grants(**kwargs)
        assert len(state.list_approval_grants()) == 1


class TestReplay:
    def test_a_restart_rebuilds_an_active_grant(self, state: SupervisorState, tmp_path: Path) -> None:
        grant = grant_for(state, minutes=120)

        restarted = make_state(tmp_path, run_id="run-after-restart")

        assert [item.grant_id for item in restarted.list_approval_grants()] == [grant.grant_id]
        sid, task = start_task(restarted, auth=qq_auth())
        assert request_op(restarted, sid, task.task_id).safety_level == SafetyLevel.auto

    def test_an_expired_grant_is_not_rebuilt(self, state: SupervisorState, tmp_path: Path, clock: Clock) -> None:
        grant_for(state, minutes=30)
        clock.advance(minutes=31)

        restarted = make_state(tmp_path, run_id="run-after-restart")

        assert restarted.list_approval_grants() == []
        assert restarted.approval_grants.active(now=clock.now) == []

    def test_a_revoked_grant_does_not_come_back(self, state: SupervisorState, tmp_path: Path) -> None:
        grant_for(state)
        state.revoke_approval_grants(identity=qq_approver())

        restarted = make_state(tmp_path, run_id="run-after-restart")

        assert restarted.list_approval_grants() == []
        sid, task = start_task(restarted, auth=qq_auth())
        assert request_op(restarted, sid, task.task_id).safety_level == SafetyLevel.approval_required

    def test_a_grant_issued_after_a_revocation_survives_it(self, state: SupervisorState, tmp_path: Path) -> None:
        grant_for(state)
        state.revoke_approval_grants(identity=qq_approver())
        later = grant_for(state, minutes=60)

        restarted = make_state(tmp_path, run_id="run-after-restart")

        assert [item.grant_id for item in restarted.list_approval_grants()] == [later.grant_id]

    def test_the_later_grant_replaces_the_earlier_on_replay(self, state: SupervisorState, tmp_path: Path, clock: Clock) -> None:
        first_id, second_id = pending_approval(state), pending_approval(state)
        approve(state, first_id, approver=qq_approver(), minutes=30)
        clock.advance(minutes=5)
        second = approve(state, second_id, approver=qq_approver(), minutes=120).grant
        assert second is not None

        restarted = make_state(tmp_path, run_id="run-after-restart")

        [only] = restarted.list_approval_grants()
        assert only.grant_id == second.grant_id and only.until == second.until

    def test_rebuild_from_events_replays_grants_and_revocations(self, state: SupervisorState, clock: Clock) -> None:
        grant_for(state)
        state.revoke_approval_grants(identity=qq_approver())
        later = grant_for(state, minutes=90)
        events = list(state.eventlog.iter_persisted_events())

        state.approval_grants = ApprovalGrantBook()
        state.rebuild_from_events(events)

        assert [item.grant_id for item in state.list_approval_grants()] == [later.grant_id]

    def test_old_events_without_grant_fields_still_replay(self, state: SupervisorState) -> None:
        requested = {
            "approval_id": "old-1", "session_id": "s-old", "operation": "write_file",
            "details": {"path": "engine/runner.py"}, "status": "pending", "fingerprint": "f",
            "requested_at": START.isoformat(), "task_id": "t-old",
        }
        decided = {"approval_id": "old-1", "decision": "allow", "comment": "ok", "ts": START.isoformat()}

        state.rebuild_from_events([
            {"seq": 1, "type": "approval_requested", "session_id": "s-old", "payload": requested},
            {"seq": 2, "type": "approval_decided", "session_id": "s-old", "payload": decided},
        ])

        approval = state.approvals["old-1"]
        assert approval.status == ApprovalStatus.allowed
        assert approval.approver is None and approval.grant is None and approval.grant_note is None
        assert state.list_approval_grants() == []

    @pytest.mark.parametrize("grant", [
        {"grant_id": "g-broken"},
        {"grant_id": "g-1", "platform": "qq", "user_id": "", "until": "2099-01-01T00:00:00+00:00", "minutes": 5, "approval_id": "a"},
        "not a mapping",
    ])
    def test_a_malformed_grant_in_an_event_is_ignored(self, state: SupervisorState, grant: Any) -> None:
        state.rebuild_from_events([{
            "seq": 1, "type": "approval_decided", "session_id": "s",
            "payload": {"approval_id": "a", "decision": "allow", "grant": grant},
        }])

        assert state.list_approval_grants() == []

    def test_a_naive_timestamp_in_an_old_grant_is_read_as_utc(self, state: SupervisorState, clock: Clock) -> None:
        state.rebuild_from_events([{
            "seq": 1, "type": "approval_decided", "session_id": "s",
            "payload": {"approval_id": "a", "decision": "allow", "grant": {
                "grant_id": "g-naive", "platform": "qq", "user_id": ADMIN, "bot_scope": BOT,
                "until": (START + timedelta(hours=1)).replace(tzinfo=None).isoformat(), "minutes": 60, "approval_id": "a",
            }},
        }])

        [grant] = state.list_approval_grants()
        assert grant.until == START + timedelta(hours=1)

    def test_a_grant_on_a_denied_decision_is_not_rebuilt(self, state: SupervisorState) -> None:
        state.rebuild_from_events([{
            "seq": 1, "type": "approval_decided", "session_id": "s",
            "payload": {"approval_id": "a", "decision": "deny", "grant": {
                "grant_id": "g-1", "platform": "qq", "user_id": ADMIN, "bot_scope": BOT,
                "until": (START + timedelta(hours=1)).isoformat(), "minutes": 60, "approval_id": "a",
            }},
        }])

        assert state.list_approval_grants() == []


class TestTheIdentityRule:
    @pytest.mark.parametrize(("left", "right", "same"), [
        (("qq", "1", "9"), ("QQ", "1", "9"), True),
        (("qq", "1", "9"), ("qq", "1", ""), True),
        (("qq", "1", ""), ("qq", "1", ""), True),
        (("qq", "1", "9"), ("qq", "1", "8"), False),
        (("qq", "1", "9"), ("qq", "2", "9"), False),
        (("qq", "1", "9"), ("telegram", "1", "9"), False),
    ])
    def test_same_person(self, left: tuple, right: tuple, same: bool) -> None:
        a = GrantIdentity.from_auth(dict(zip(("platform", "user_id", "bot_scope"), left)))
        b = GrantIdentity.from_auth(dict(zip(("platform", "user_id", "bot_scope"), right)))

        assert a is not None and b is not None
        assert a.same_person(b) is same and b.same_person(a) is same

    def test_a_drafted_grant_only_counts_once_stored(self) -> None:
        book = ApprovalGrantBook()
        identity = GrantIdentity.from_auth({"platform": "qq", "user_id": ADMIN, "bot_scope": BOT})
        assert identity is not None
        grant = book.draft(identity, minutes=10, approval_id="a", now=START)

        # 拟好的授权还没登记，查不到。
        assert book.active_for(identity, now=START) is None
        assert book.store(grant, now=START) is not None
        assert book.active_for(identity, now=START + timedelta(minutes=9)) == grant
        assert book.active_for(identity, now=START + timedelta(minutes=10)) is None


def test_the_sdk_mirrors_the_grant_notes() -> None:
    names = [name for name in vars(approval_grants) if name.startswith("GRANT_NOTE_")]

    assert names
    for name in names:
        assert getattr(sdk_types, name) == getattr(approval_grants, name), name
