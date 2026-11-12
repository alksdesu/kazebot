"""掉线分类、登录动作预算与自动重登状态机。

账号动作、通知、时钟全换成假的；事件文件走真实路径 —— 预算就是从它读回来的。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.onebot.napcat import recovery  # noqa: E402

_UIN = "10000001"
_NOW = 1_800_000_000.0


class _Clock:
    def __init__(self, now: float = _NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _Settings:
    def __init__(self, **values: Any) -> None:
        self.enabled = values.get("enabled", True)
        self.daily_limit = values.get("daily_limit", 3)
        self.min_interval_sec = values.get("min_interval_sec", 0.0)
        self.login_mode_on_failure = values.get("login_mode_on_failure", True)
        self.notify_admins = values.get("notify_admins", True)


class _Gateway(recovery.AccountGateway):
    """按脚本回答的账号动作：login_state 依次给，quick_relogin 一直给同一个结果。"""

    def __init__(self, *, states: list[dict[str, Any]] | None = None, quick: dict[str, Any] | None = None) -> None:
        self.states = list(states or [{"is_login": False, "is_offline": False}])
        self.quick = quick if quick is not None else {"ok": True, "account": _UIN, "message": "", "needs_verify": False}
        self.calls: list[tuple[str, Any]] = []

    async def login_state(self) -> dict[str, Any]:
        self.calls.append(("login_state", None))
        if isinstance(self.states[0], BaseException):
            raise self.states[0]
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    async def quick_relogin(self, uin: str) -> dict[str, Any]:
        self.calls.append(("quick_relogin", uin))
        return dict(self.quick)

    async def enter_login_mode(self) -> None:
        self.calls.append(("enter_login_mode", None))


class _Notifier:
    def __init__(self, *, deliver: bool = True) -> None:
        self.deliver = deliver
        self.sent: list[str] = []

    async def __call__(self, text: str) -> bool:
        self.sent.append(text)
        return self.deliver


def _recovery(
    tmp_path: Path, *, gateway: _Gateway | None = None, notifier: _Notifier | None = None,
    settings: _Settings | None = None, clock: _Clock | None = None, account: str = _UIN,
) -> tuple[recovery.Recovery, _Gateway, _Notifier, _Clock]:
    gateway = gateway or _Gateway()
    notifier = notifier or _Notifier()
    clock = clock or _Clock()
    engine = recovery.Recovery(
        workspace=tmp_path, settings=settings or _Settings(), gateway=gateway, notifier=notifier,
        current_account=lambda: account, clock=clock,
    )
    return engine, gateway, notifier, clock


def _events(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "data" / recovery.EVENTS_FILE_NAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def _settled(engine: recovery.Recovery) -> None:
    task = engine._handling
    if task is not None:
        await task


def _kick(engine: recovery.Recovery, **fields: Any) -> recovery.OfflineEvent:
    return engine.on_notice({"notice_type": "bot_offline", "user_id": int(_UIN), **fields})


class TestClassification:
    @pytest.mark.parametrize("title, message, reason", [
        ("下线通知", "你的账号当前登录已失效，请重新登录。", recovery.REASON_SESSION_INVALID),
        ("安全提醒", "账号存在异常，请验证后重新登录", recovery.REASON_SESSION_INVALID),
        ("下线通知", "你的账号在其他设备上登录", recovery.REASON_OTHER_DEVICE),
        ("下线通知", "你已在手机上将电脑 QQ 退出", recovery.REASON_OTHER_DEVICE),
        ("下线通知", "密码已修改，请重新登录", recovery.REASON_PASSWORD_CHANGED),
        ("提示", "当前版本过低，请升级后使用", recovery.REASON_LOW_VERSION),
        ("", "", recovery.REASON_UNKNOWN),
    ])
    def test_kick_text_maps_to_a_reason(self, title: str, message: str, reason: str) -> None:
        assert recovery.reason_from_text(title, message) == reason

    def test_the_notice_fields_napcat_sends_are_read(self) -> None:
        event = recovery.classify_kick(
            {"tag": "下线通知", "message": "你的账号当前登录已失效，请重新登录。", "kicked_type": "7", "same_device": 0},
            at=_NOW,
        )

        assert (event.kind, event.reason, event.kicked_type, event.same_device) == (
            recovery.KIND_KICK, recovery.REASON_SESSION_INVALID, 7, False,
        )
        assert event.auto_relogin is True
        assert event.summary() == "kick/session_invalid：下线通知；你的账号当前登录已失效，请重新登录。"

    def test_an_unexplained_kick_from_the_same_device_is_not_fought_over(self) -> None:
        event = recovery.classify_kick({"tag": "", "message": "", "same_device": True}, at=_NOW)

        assert event.reason == recovery.REASON_OTHER_DEVICE
        assert event.auto_relogin is False

    def test_extra_fields_of_an_unknown_notice_event_are_collected(self) -> None:
        event = SimpleNamespace(
            notice_type="bot_offline", user_id=10000001, model_extra={"tag": "下线通知", "message": "登录已失效"},
            model_dump=lambda: {"time": 1, "self_id": 10000001, "post_type": "notice", "notice_type": "bot_offline"},
        )

        fields = recovery.notice_fields(event)

        assert fields["tag"] == "下线通知" and fields["message"] == "登录已失效"
        assert fields["notice_type"] == "bot_offline" and fields["user_id"] == 10000001


class TestBudget:
    def test_past_actions_are_read_back_from_the_event_file(self, tmp_path: Path) -> None:
        log = recovery.OfflineEventLog(recovery.events_path(tmp_path))
        for at in (_NOW - 90000, _NOW - 3600, _NOW - 60):
            log.append({"at": at, "type": "action", "action": "quick_relogin"})
        log.append({"at": _NOW - 10, "type": "offline", "reason": "x"})

        budget = recovery.ActionBudget(log, now=_NOW)

        assert budget.used(_NOW) == 2
        assert budget.refusal(_NOW, limit=3, min_interval_sec=0) == ""
        assert "到预算上限 2" in budget.refusal(_NOW, limit=2, min_interval_sec=0)
        assert "不足 600 秒" in budget.refusal(_NOW, limit=3, min_interval_sec=600)
        assert budget.refusal(_NOW, limit=0, min_interval_sec=0) == "自动恢复的日预算为 0"

    def test_a_corrupt_line_does_not_break_the_file(self, tmp_path: Path) -> None:
        path = recovery.events_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text('{"at": ' + str(_NOW - 5) + ', "type": "action"}\nnot json\n', encoding="utf-8")

        assert recovery.ActionBudget(recovery.OfflineEventLog(path), now=_NOW).used(_NOW) == 1


class TestStateMachine:
    def test_an_invalidated_session_is_quick_relogged_and_the_admins_told(self, tmp_path: Path) -> None:
        async def run() -> None:
            engine, gateway, notifier, _ = _recovery(tmp_path)
            engine.on_connect()

            _kick(engine, tag="下线通知", message="你的账号当前登录已失效，请重新登录。")
            assert engine.state.online is False
            assert engine.state.offline_reason == recovery.REASON_SESSION_INVALID
            await _settled(engine)

            assert [name for name, _ in gateway.calls] == ["login_state", "quick_relogin"]
            assert engine.state.recovery_state == recovery.STATE_IDLE
            assert engine.state.actions_used == 1
            assert notifier.sent and "已自动快速登录回 " + _UIN in notifier.sent[0]
            assert [item["type"] for item in _events(tmp_path)] == ["offline", "action", "settled"]

        asyncio.run(run())

    def test_a_kick_from_another_device_is_left_alone(self, tmp_path: Path) -> None:
        async def run() -> None:
            engine, gateway, notifier, _ = _recovery(tmp_path)
            engine.on_connect()

            _kick(engine, tag="下线通知", message="你的账号在其他设备上登录")
            await _settled(engine)

            assert gateway.calls == []
            assert engine.state.recovery_state == recovery.STATE_MANUAL
            assert "不自动重登" in engine.state.recovery_note
            assert notifier.sent == []
            assert engine.state.pending_notice

        asyncio.run(run())

    def test_the_daily_budget_stops_further_logins(self, tmp_path: Path) -> None:
        async def run() -> None:
            clock = _Clock()
            engine, gateway, _, _ = _recovery(tmp_path, settings=_Settings(daily_limit=1), clock=clock)
            engine.on_connect()
            _kick(engine, tag="下线通知", message="登录已失效")
            await _settled(engine)
            engine.on_connect()
            clock.now += 60

            _kick(engine, tag="下线通知", message="登录已失效")
            await _settled(engine)

            assert [name for name, _ in gateway.calls].count("quick_relogin") == 1
            assert engine.state.recovery_state == recovery.STATE_EXHAUSTED
            assert "到预算上限 1" in engine.state.recovery_note

        asyncio.run(run())

    def test_a_refused_quick_login_falls_back_to_login_mode(self, tmp_path: Path) -> None:
        async def run() -> None:
            gateway = _Gateway(quick={"ok": False, "account": _UIN, "message": "140022011 异常设备", "needs_verify": True})
            engine, _, notifier, _ = _recovery(tmp_path, gateway=gateway)
            engine.on_connect()

            _kick(engine, tag="下线通知", message="登录已失效")
            await _settled(engine)

            assert [name for name, _ in gateway.calls] == ["login_state", "quick_relogin", "enter_login_mode"]
            assert engine.state.recovery_state == recovery.STATE_LOGIN_MODE
            assert "需要人工处理" in engine.state.recovery_note and "等人扫码" in engine.state.recovery_note
            assert engine.state.actions_used == 2
            assert notifier.sent == [] and engine.state.pending_notice

        asyncio.run(run())

    def test_login_mode_fallback_can_be_switched_off(self, tmp_path: Path) -> None:
        async def run() -> None:
            gateway = _Gateway(quick={"ok": False, "account": _UIN, "message": "拒绝", "needs_verify": False})
            engine, _, _, _ = _recovery(tmp_path, gateway=gateway, settings=_Settings(login_mode_on_failure=False))
            engine.on_connect()

            _kick(engine, tag="下线通知", message="登录已失效")
            await _settled(engine)

            assert [name for name, _ in gateway.calls] == ["login_state", "quick_relogin"]
            assert engine.state.recovery_state == recovery.STATE_MANUAL

        asyncio.run(run())

    def test_disabled_recovery_only_records(self, tmp_path: Path) -> None:
        async def run() -> None:
            engine, gateway, _, _ = _recovery(tmp_path, settings=_Settings(enabled=False))
            engine.on_connect()

            _kick(engine, tag="下线通知", message="登录已失效")
            await _settled(engine)

            assert gateway.calls == []
            assert engine.state.recovery_state == recovery.STATE_MANUAL
            assert [item["type"] for item in _events(tmp_path)] == ["offline", "settled"]

        asyncio.run(run())

    def test_a_session_that_is_still_alive_needs_no_action(self, tmp_path: Path) -> None:
        async def run() -> None:
            gateway = _Gateway(states=[{"is_login": True, "is_offline": False}])
            engine, _, _, _ = _recovery(tmp_path, gateway=gateway)
            engine.on_connect()

            _kick(engine, tag="", message="")
            await _settled(engine)

            assert [name for name, _ in gateway.calls] == ["login_state"]
            assert engine.state.recovery_state == recovery.STATE_IDLE
            assert engine.state.actions_used == 0

        asyncio.run(run())

    def test_without_a_known_account_nothing_is_attempted(self, tmp_path: Path) -> None:
        async def run() -> None:
            engine, gateway, _, _ = _recovery(tmp_path, account="")
            engine.on_connect()

            _kick(engine, tag="下线通知", message="登录已失效")
            await _settled(engine)

            assert gateway.calls == []
            assert engine.state.recovery_state == recovery.STATE_MANUAL

        asyncio.run(run())


class TestLinkDown:
    def test_a_disconnect_waits_then_leaves_a_live_core_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(recovery, "LINK_DOWN_GRACE_SEC", 0.0)

        async def run() -> None:
            gateway = _Gateway(states=[{"is_login": True, "is_offline": False}])
            engine, _, _, _ = _recovery(tmp_path, gateway=gateway)
            engine.on_connect()

            engine.on_disconnect()
            assert engine.state.offline_reason == recovery.REASON_LINK_DOWN
            assert engine.state.recovery_state in {recovery.STATE_WAITING, recovery.STATE_IDLE}
            await _settled(engine)

            assert [name for name, _ in gateway.calls] == ["login_state"]
            assert engine.state.recovery_state == recovery.STATE_WAITING
            assert "仍在线" in engine.state.recovery_note

        asyncio.run(run())

    def test_a_disconnect_that_turns_out_to_be_a_lost_session_is_recovered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(recovery, "LINK_DOWN_GRACE_SEC", 0.0)

        async def run() -> None:
            gateway = _Gateway(states=[{"is_login": False, "is_offline": False, "login_error": "登录已失效"}])
            engine, _, _, _ = _recovery(tmp_path, gateway=gateway)
            engine.on_connect()

            engine.on_disconnect()
            await _settled(engine)

            assert [name for name, _ in gateway.calls] == ["login_state", "login_state", "quick_relogin"]
            assert engine.state.recovery_state == recovery.STATE_IDLE

        asyncio.run(run())

    def test_a_reconnect_during_the_grace_period_cancels_nothing_further(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(recovery, "LINK_DOWN_GRACE_SEC", 0.01)

        async def run() -> None:
            engine, gateway, _, _ = _recovery(tmp_path)
            engine.on_connect()

            engine.on_disconnect()
            engine.on_connect()
            await _settled(engine)

            assert gateway.calls == []
            assert engine.state.online is True
            assert engine.state.recovery_state == recovery.STATE_IDLE

        asyncio.run(run())

    def test_a_kick_arriving_after_the_disconnect_takes_over(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(recovery, "LINK_DOWN_GRACE_SEC", 10.0)

        async def run() -> None:
            engine, gateway, _, _ = _recovery(tmp_path)
            engine.on_connect()

            engine.on_disconnect()
            waiting = engine._handling
            _kick(engine, tag="下线通知", message="登录已失效")
            await _settled(engine)

            assert waiting is not None and waiting.cancelled()
            assert engine.state.offline_kind == recovery.KIND_KICK
            assert [name for name, _ in gateway.calls] == ["login_state", "quick_relogin"]

        asyncio.run(run())

    def test_a_second_link_signal_while_handling_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(recovery, "LINK_DOWN_GRACE_SEC", 0.0)

        async def run() -> None:
            engine, gateway, _, _ = _recovery(tmp_path)
            engine.on_connect()

            engine.on_disconnect()
            first = engine._handling
            engine.on_disconnect()
            assert engine._handling is first
            await _settled(engine)

            assert [name for name, _ in gateway.calls].count("quick_relogin") == 1

        asyncio.run(run())


    def test_a_link_that_comes_back_within_the_grace_leaves_no_trace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 进程重启、docker restart 都会断几秒再连回来，事件文件里不该多一条掉线加一条恢复。
        monkeypatch.setattr(recovery, "LINK_DOWN_GRACE_SEC", 0.05)

        async def run() -> None:
            engine, gateway, _, _ = _recovery(tmp_path)
            engine.on_connect()

            engine.on_disconnect()
            assert engine.state.online is False
            assert _events(tmp_path) == []
            engine.on_connect()
            await _settled(engine)

            assert engine.state.online is True
            assert engine.state.recovery_state == recovery.STATE_IDLE
            assert _events(tmp_path) == []
            assert gateway.calls == []

        asyncio.run(run())

    def test_a_link_still_down_after_the_grace_is_recorded_then_recovered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(recovery, "LINK_DOWN_GRACE_SEC", 0.0)

        async def run() -> None:
            gateway = _Gateway(states=[{"is_login": True, "is_offline": False}])
            engine, _, _, _ = _recovery(tmp_path, gateway=gateway)
            engine.on_connect()

            engine.on_disconnect()
            await _settled(engine)
            assert [item["type"] for item in _events(tmp_path)] == ["offline", "settled"]
            assert _events(tmp_path)[0]["reason"] == recovery.REASON_LINK_DOWN

            engine.on_connect()
            assert [item["type"] for item in _events(tmp_path)] == ["offline", "settled", "recovered"]

        asyncio.run(run())

    def test_signals_after_close_are_not_outages(self, tmp_path: Path) -> None:
        # 进程退出时 WS 必然断开，那是我们自己关的，不该在事件文件里留一条掉线。
        async def run() -> None:
            engine, gateway, notifier, _ = _recovery(tmp_path)
            engine.on_connect()

            engine.close()
            engine.on_disconnect()
            engine.on_heartbeat(online=False, interval_ms=5000)
            _kick(engine, title="下线通知", message="你的账号当前登录已失效")

            assert engine._handling is None
            assert engine.state.online is True
            assert _events(tmp_path) == []
            assert gateway.calls == [] and notifier.sent == []

        asyncio.run(run())


class TestHeartbeat:
    def test_silence_beyond_three_beats_is_a_link_down(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(recovery, "HEARTBEAT_CHECK_SEC", 0.0)
        monkeypatch.setattr(recovery, "LINK_DOWN_GRACE_SEC", 10.0)

        async def run() -> None:
            clock = _Clock()
            engine, _, _, _ = _recovery(tmp_path, clock=clock)
            engine.on_connect()
            engine.on_heartbeat(online=True, interval_ms=5000)
            watcher = asyncio.get_running_loop().create_task(engine.watch_forever())
            try:
                clock.now += 14.0
                await asyncio.sleep(0.01)
                assert engine.state.online is True

                clock.now += 2.0
                await asyncio.sleep(0.01)
                assert engine.state.online is False
                assert engine.state.offline_kind == recovery.KIND_HEARTBEAT
                assert "没有心跳" in engine.state.offline_detail
            finally:
                watcher.cancel()
                engine.close()

        asyncio.run(run())

    def test_a_heartbeat_reporting_offline_is_treated_as_link_down(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(recovery, "LINK_DOWN_GRACE_SEC", 10.0)

        async def run() -> None:
            engine, _, _, _ = _recovery(tmp_path)
            engine.on_connect()

            engine.on_heartbeat(online=False, interval_ms=None)

            assert engine.state.online is False
            assert engine.state.offline_kind == recovery.KIND_HEARTBEAT
            engine.close()

        asyncio.run(run())

    def test_a_heartbeat_saying_online_brings_the_link_back(self, tmp_path: Path) -> None:
        async def run() -> None:
            engine, _, _, _ = _recovery(tmp_path)
            # 一次已经落盘的掉线
            engine.state.online = False
            engine.state.last_offline_at = _NOW - 5
            engine._offline_logged = True

            engine.on_heartbeat(online=True, interval_ms=30000)

            assert engine.state.online is True
            assert engine.state.heartbeat_interval_sec == 30.0
            assert [item["type"] for item in _events(tmp_path)] == ["recovered"]

        asyncio.run(run())


class TestNotices:
    def test_a_notice_that_could_not_be_sent_is_flushed_once_online(self, tmp_path: Path) -> None:
        async def run() -> None:
            notifier = _Notifier(deliver=False)
            gateway = _Gateway(quick={"ok": False, "account": _UIN, "message": "拒绝", "needs_verify": False})
            engine, _, _, _ = _recovery(tmp_path, gateway=gateway, notifier=notifier, settings=_Settings(login_mode_on_failure=False))
            engine.on_connect()
            _kick(engine, tag="下线通知", message="登录已失效")
            await _settled(engine)
            assert notifier.sent == [] and engine.state.pending_notice

            notifier.deliver = True
            engine.on_connect()
            await engine.flush_notice()

            assert len(notifier.sent) == 1 and "拒绝" in notifier.sent[0]
            assert engine.state.pending_notice == ""

        asyncio.run(run())

    def test_notifications_can_be_turned_off(self, tmp_path: Path) -> None:
        async def run() -> None:
            notifier = _Notifier()
            engine, _, _, _ = _recovery(tmp_path, notifier=notifier, settings=_Settings(notify_admins=False))
            engine.on_connect()

            _kick(engine, tag="下线通知", message="登录已失效")
            await _settled(engine)

            assert notifier.sent == []

        asyncio.run(run())

    def test_live_facts_expose_the_whole_situation(self, tmp_path: Path) -> None:
        engine, _, _, _ = _recovery(tmp_path)
        engine.on_connect()

        facts = engine.state.facts()

        assert facts["online"] is True
        assert set(facts) == {"online", "offline_reason", "offline_kind", "offline_detail", "last_offline_at", "last_online_at", "recovery"}
        assert set(facts["recovery"]) == {"state", "note", "last_action", "last_action_at", "actions_used_24h"}
