"""QQ 的账号运维实现：状态、切号、扫码、钉自动登录，以及死号表怎么影响可切换的号。

WebUI 调用全换成假的，死号表走真实文件 —— 置灰和撤销都是它读写出来的。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.onebot.account_ops import (  # noqa: E402
    NapCatAccountOps,
    NapCatClient,
    NapCatError,
    NapCatUnreachable,
    account_ops,
)
from platforms.account_ops import (  # noqa: E402
    AccountConflict,
    AccountOpsError,
    AccountRequestInvalid,
    AccountUnreachable,
)

_UIN = "10000001"
_OTHER = "10000002"
_WEBUI_ENV = "NAPCAT_WEBUI_TOKEN"


class _Endpoints:
    """按端点名给回答，值是异常就抛；记下调用顺序和参数。"""

    def __init__(self, **answers: Any) -> None:
        self._answers = answers
        self.seen: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, endpoint: str, payload: dict[str, Any] | None = None) -> Any:
        self.seen.append((endpoint, dict(payload or {})))
        answer = self._answers.get(endpoint)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    @property
    def endpoints(self) -> list[str]:
        return [name for name, _ in self.seen]


def _ops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **answers: Any) -> tuple[NapCatAccountOps, _Endpoints]:
    ops = NapCatAccountOps(tmp_path)
    calls = _Endpoints(**answers)
    monkeypatch.setattr(ops.client, "call", calls)
    return ops, calls


def _pins(ops: NapCatAccountOps, monkeypatch: pytest.MonkeyPatch, *, error: Exception | None = None) -> list[str]:
    pinned: list[str] = []

    async def set_auto_login(uin: str) -> None:
        pinned.append(uin)
        if error is not None:
            raise error

    monkeypatch.setattr(ops.client, "set_auto_login", set_auto_login)
    return pinned


def _dead_path(workspace: Path) -> Path:
    return workspace / "data" / "qq_dead_logins.json"


def _write_dead(workspace: Path, reasons: dict[str, str]) -> None:
    path = _dead_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {uin: {"at": "2026-01-01T00:00:00+00:00", "reason": reason} for uin, reason in reasons.items()}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _status(ops: NapCatAccountOps) -> dict[str, Any]:
    return dict(asyncio.run(ops.status()))


class TestStatus:
    def test_the_account_is_not_called_uin(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ops, _ = _ops(
            tmp_path, monkeypatch,
            GetQQLoginInfo={"uin": _UIN, "nick": "咪啪"},
            CheckLoginStatus={"isLogin": True},
            GetQuickLoginListNew=[],
        )

        assert _status(ops) == {
            "account": _UIN, "nick": "咪啪", "online": True, "is_login": True,
            "login_error": "", "qrcode": "", "quick_login": [],
        }

    def test_a_logged_in_but_offline_session_is_not_online(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ops, _ = _ops(
            tmp_path, monkeypatch,
            GetQQLoginInfo={"uin": _UIN},
            CheckLoginStatus={"isLogin": True, "isOffline": True},
        )

        status = _status(ops)

        assert (status["is_login"], status["online"]) == (True, False)

    def test_the_leftovers_of_the_last_scan_are_dropped_once_logged_in(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ops, _ = _ops(
            tmp_path, monkeypatch,
            GetQQLoginInfo={"uin": _UIN},
            CheckLoginStatus={"isLogin": True, "loginError": "二维码已过期", "qrcodeurl": "qrcode://stale"},
        )

        status = _status(ops)

        assert (status["login_error"], status["qrcode"]) == ("", "")

    def test_waiting_for_a_scan_carries_the_error_and_the_qrcode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ops, _ = _ops(
            tmp_path, monkeypatch,
            GetQQLoginInfo={},
            CheckLoginStatus={"isLogin": False, "loginError": "请扫码", "qrcodeurl": "qrcode://now"},
        )

        assert _status(ops) == {
            "account": "", "nick": "", "online": False, "is_login": False,
            "login_error": "请扫码", "qrcode": "qrcode://now", "quick_login": [],
        }

    def test_a_failing_login_check_still_renders_the_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ops, calls = _ops(
            tmp_path, monkeypatch,
            GetQQLoginInfo={"uin": _UIN, "nick": "咪啪"},
            CheckLoginStatus=NapCatError("CheckLoginStatus 返回错误"),
            GetQuickLoginListNew=[_OTHER],
        )

        status = _status(ops)

        assert (status["account"], status["is_login"], status["online"]) == (_UIN, False, False)
        assert [target["account"] for target in status["quick_login"]] == [_OTHER]
        assert calls.endpoints == ["GetQQLoginInfo", "CheckLoginStatus", "GetQuickLoginListNew"]

    def test_a_failing_quick_list_still_renders_the_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ops, _ = _ops(
            tmp_path, monkeypatch,
            GetQQLoginInfo={"uin": _UIN},
            CheckLoginStatus={"isLogin": True},
            GetQuickLoginListNew=NapCatError("GetQuickLoginListNew 返回错误"),
        )

        status = _status(ops)

        assert (status["account"], status["is_login"], status["quick_login"]) == (_UIN, True, [])

    def test_empty_answers_are_an_empty_account(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ops, _ = _ops(tmp_path, monkeypatch)

        assert _status(ops) == {
            "account": "", "nick": "", "online": False, "is_login": False,
            "login_error": "", "qrcode": "", "quick_login": [],
        }

    def test_an_unreachable_webui_surfaces_instead_of_an_empty_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 上层靠这个异常判「正在重启」，吞掉就只剩一张空白的已登出页面。
        ops, _ = _ops(tmp_path, monkeypatch, GetQQLoginInfo=NapCatUnreachable("连不上 NapCat WebUI"))

        with pytest.raises(AccountUnreachable):
            _status(ops)


class TestQuickLoginTargets:
    def _targets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quick: list[Any], **login: Any,
    ) -> list[dict[str, Any]]:
        ops, _ = _ops(
            tmp_path, monkeypatch,
            GetQQLoginInfo={"uin": login.pop("uin", "")},
            CheckLoginStatus=login or {},
            GetQuickLoginListNew=quick,
        )
        return _status(ops)["quick_login"]

    def test_a_target_from_the_new_list_keeps_its_nick_and_avatar(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        quick = [{"uin": _OTHER, "nickName": "小号", "faceUrl": "http://face/2", "isQuickLogin": True}]

        assert self._targets(tmp_path, monkeypatch, quick) == [
            {"account": _OTHER, "nick": "小号", "avatar": "http://face/2", "available": True},
        ]

    def test_a_bare_uin_from_the_old_list_is_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        assert self._targets(tmp_path, monkeypatch, [_OTHER]) == [
            {"account": _OTHER, "nick": "", "avatar": "", "available": True},
        ]

    def test_a_target_without_the_quick_flag_stays_clickable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 旧版本不给这个标记，缺失当禁用会把号全灰掉。
        assert self._targets(tmp_path, monkeypatch, [{"uin": _OTHER}])[0]["available"] is True

    def test_napcat_calling_a_target_stale_greys_it_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        quick = [{"uin": _OTHER, "isQuickLogin": False}]

        assert self._targets(tmp_path, monkeypatch, quick)[0]["available"] is False

    @pytest.mark.parametrize("item", ["", "   ", None, "abc", "1000-0002", {"uin": "abc"}, {}, {"uin": None}])
    def test_a_target_the_switch_would_refuse_is_dropped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, item: Any,
    ) -> None:
        assert self._targets(tmp_path, monkeypatch, [item, _OTHER]) == [
            {"account": _OTHER, "nick": "", "avatar": "", "available": True},
        ]

    def test_a_numeric_uin_is_still_text(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._targets(tmp_path, monkeypatch, [{"uin": 10000002}])[0]["account"] == _OTHER

    def test_an_account_qq_refused_is_greyed_out_with_the_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_dead(tmp_path, {_OTHER: "登录态失效"})

        targets = self._targets(tmp_path, monkeypatch, [{"uin": _OTHER, "isQuickLogin": True}])

        assert targets == [
            {"account": _OTHER, "nick": "", "avatar": "", "available": False, "dead_reason": "登录态失效"},
        ]

    def test_scanning_back_in_lifts_the_mark_on_the_current_account(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_dead(tmp_path, {_UIN: "登录态失效", _OTHER: "被拒"})

        targets = self._targets(tmp_path, monkeypatch, [_UIN, _OTHER], uin=_UIN, isLogin=True)

        assert [(target["account"], target["available"]) for target in targets] == [(_UIN, True), (_OTHER, False)]
        assert json.loads(_dead_path(tmp_path).read_text(encoding="utf-8")).keys() == {_OTHER}

    def test_the_mark_stays_while_nobody_is_logged_in(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_dead(tmp_path, {_UIN: "登录态失效"})

        assert self._targets(tmp_path, monkeypatch, [_UIN], uin=_UIN, isLogin=False)[0]["available"] is False
        assert json.loads(_dead_path(tmp_path).read_text(encoding="utf-8")).keys() == {_UIN}


class TestDeadLogins:
    def test_an_absent_table_reads_as_nothing_refused(self, tmp_path: Path) -> None:
        assert NapCatClient(tmp_path).dead_logins() == {}

    @pytest.mark.parametrize("body", ["{", "[]", '"x"', ""])
    def test_an_unreadable_table_reads_as_nothing_refused(self, tmp_path: Path, body: str) -> None:
        path = _dead_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

        assert NapCatClient(tmp_path).dead_logins() == {}

    def test_a_mark_is_readable_and_liftable(self, tmp_path: Path) -> None:
        client = NapCatClient(tmp_path)
        client.mark_login_dead(_UIN, "登录态失效")

        assert client.dead_logins()[_UIN]["reason"] == "登录态失效"
        assert client.clear_login_dead(_OTHER) is False
        assert client.clear_login_dead(_UIN) is True
        assert client.dead_logins() == {}


class TestSwitch:
    @pytest.mark.parametrize("account", ["", "   ", "abc", "1000a", "1000-0002", "+10000002"])
    def test_an_account_qq_could_not_accept_never_leaves_the_process(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, account: str,
    ) -> None:
        ops, calls = _ops(tmp_path, monkeypatch)
        pinned = _pins(ops, monkeypatch)

        with pytest.raises(AccountRequestInvalid, match="QQ 号必须是数字"):
            asyncio.run(ops.switch(account))

        assert (calls.seen, pinned) == ([], [])
        assert not _dead_path(tmp_path).exists()

    def test_a_switch_pins_the_account_for_the_next_restart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ops, calls = _ops(tmp_path, monkeypatch, SetQuickLogin={})
        pinned = _pins(ops, monkeypatch)

        result = asyncio.run(ops.switch(f"  {_OTHER}  "))

        assert result == {"ok": True, "account": _OTHER, "pinned": True, "pin_error": ""}
        assert calls.seen == [("SetQuickLogin", {"uin": _OTHER})]
        assert pinned == [_OTHER]

    def test_a_failed_pin_does_not_look_like_a_failed_switch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ops, _ = _ops(tmp_path, monkeypatch, SetQuickLogin={})
        _pins(ops, monkeypatch, error=NapCatError("等待账号操作超时"))

        result = asyncio.run(ops.switch(_OTHER))

        assert result == {"ok": True, "account": _OTHER, "pinned": False, "pin_error": "等待账号操作超时"}
        assert not _dead_path(tmp_path).exists()

    def test_an_unreachable_container_does_not_kill_the_account(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ops, _ = _ops(tmp_path, monkeypatch, SetQuickLogin=NapCatUnreachable("SetQuickLogin 调用失败"))
        pinned = _pins(ops, monkeypatch)

        with pytest.raises(NapCatUnreachable):
            asyncio.run(ops.switch(_OTHER))

        assert not _dead_path(tmp_path).exists()
        assert pinned == []

    def test_an_account_qq_refused_is_remembered_as_dead(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ops, _ = _ops(tmp_path, monkeypatch, SetQuickLogin=NapCatError("SetQuickLogin 返回错误：登录态失效"))
        pinned = _pins(ops, monkeypatch)

        with pytest.raises(NapCatError, match="登录态失效"):
            asyncio.run(ops.switch(_OTHER))

        assert "登录态失效" in ops.client.dead_logins()[_OTHER]["reason"]
        assert pinned == []


class TestLoginQrcode:
    @pytest.mark.parametrize(
        "data, qrcode",
        [
            ({"qrcode": "qrcode://a"}, "qrcode://a"),
            ({"qrcodeurl": "qrcode://b"}, "qrcode://b"),
            ({"qrcode": "", "qrcodeurl": "qrcode://b"}, "qrcode://b"),
            ({"qrcode": "qrcode://a", "qrcodeurl": "qrcode://b"}, "qrcode://a"),
            ({}, ""),
            (None, ""),
            ("qrcode://plain", ""),
            ([{"qrcode": "qrcode://a"}], ""),
        ],
    )
    def test_either_field_is_accepted_and_nothing_readable_is_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: Any, qrcode: str,
    ) -> None:
        ops, calls = _ops(tmp_path, monkeypatch, GetQQLoginQrcode=data)

        assert asyncio.run(ops.login_qrcode()) == qrcode
        assert calls.endpoints == ["GetQQLoginQrcode"]


class TestRelogin:
    def test_relogin_puts_the_container_back_into_scan_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ops, calls = _ops(tmp_path, monkeypatch)
        entered: list[int] = []

        async def enter_login_mode() -> None:
            entered.append(1)

        monkeypatch.setattr(ops.client, "enter_login_mode", enter_login_mode)

        assert asyncio.run(ops.relogin()) is None
        assert (entered, calls.seen) == ([1], [])


class TestPin:
    def test_nothing_logged_in_is_a_conflict(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ops, _ = _ops(tmp_path, monkeypatch, GetQQLoginInfo={}, CheckLoginStatus={"isLogin": False})
        pinned = _pins(ops, monkeypatch)

        with pytest.raises(AccountConflict, match="当前没有登录任何账号"):
            asyncio.run(ops.pin())

        assert pinned == []

    def test_the_account_on_screen_is_the_one_pinned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ops, _ = _ops(
            tmp_path, monkeypatch, GetQQLoginInfo={"uin": _UIN}, CheckLoginStatus={"isLogin": True},
        )
        pinned = _pins(ops, monkeypatch)

        assert asyncio.run(ops.pin()) == _UIN
        assert pinned == [_UIN]

    def test_a_refused_pin_surfaces(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ops, _ = _ops(tmp_path, monkeypatch, GetQQLoginInfo={"uin": _UIN}, CheckLoginStatus={"isLogin": True})
        _pins(ops, monkeypatch, error=NapCatError("读不到 admin token"))

        with pytest.raises(NapCatError, match="admin token"):
            asyncio.run(ops.pin())


class TestErrorKinds:
    @pytest.mark.parametrize("kind", [AccountUnreachable, AccountRequestInvalid, AccountConflict])
    def test_every_refusal_is_an_account_ops_error(self, kind: type[Exception]) -> None:
        assert issubclass(kind, AccountOpsError) and issubclass(AccountOpsError, RuntimeError)

    def test_the_qq_errors_answer_to_the_platform_neutral_names(self) -> None:
        # supervisor 只认约定里的四类，QQ 的异常不挂上去就全被翻成 502。
        assert issubclass(NapCatError, AccountOpsError)
        assert issubclass(NapCatUnreachable, NapCatError) and issubclass(NapCatUnreachable, AccountUnreachable)
        assert isinstance(NapCatUnreachable("x"), AccountUnreachable)
        assert isinstance(NapCatUnreachable("x"), NapCatError)


class TestHookFactory:
    def test_the_hook_builds_the_qq_implementation(self, tmp_path: Path) -> None:
        ops = account_ops(tmp_path)

        assert isinstance(ops, NapCatAccountOps) and isinstance(ops.client, NapCatClient)

    def test_configured_follows_the_webui_token(self, tmp_path: Path) -> None:
        ops = account_ops(tmp_path)
        assert ops.configured is False

        (tmp_path / ".env").write_text(f"{_WEBUI_ENV}=backfilled\n", encoding="utf-8")

        assert ops.configured is True
