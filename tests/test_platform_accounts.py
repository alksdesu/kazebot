"""/v1/platforms/{id}/account…：supervisor 只转发平台的账号运维、翻译错误、记审计。

平台实现换成假的；老的 /v1/qq/account… 与 /v1/admin/qq/scope… 是同一套逻辑的别名，响应里账号仍叫 uin。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import httpx
import pytest
import yaml
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import supervisor.admin_api as admin_api  # noqa: E402
import supervisor.platform_accounts as platform_accounts  # noqa: E402
from adapters._shared.conversation_hash import SECRET_NAME  # noqa: E402
from platforms.account_ops import AccountConflict, AccountOpsError, AccountRequestInvalid, AccountUnreachable  # noqa: E402
from platforms.builtin import QQ_NAPCAT  # noqa: E402
from platforms.descriptor import (  # noqa: E402
    CHANNEL_GROUP,
    HOOK_ACCOUNT_OPS,
    ID_SCOPE_ACCOUNT,
    ChannelDescriptor,
    PlatformDescriptor,
    SecretField,
)
from platforms.registry import register_platform, unregister_platform  # noqa: E402
from supervisor.api import create_app  # noqa: E402
from supervisor.config_store import ConfigStore  # noqa: E402
from supervisor.eventlog import EventLog  # noqa: E402
from supervisor.platform_accounts import (  # noqa: E402
    PlatformAccounts,
    create_legacy_qq_account_router,
    create_platform_account_router,
)
from supervisor.policy import PolicyEngine  # noqa: E402
from supervisor.state import SupervisorState  # noqa: E402

_TOKEN = "accounts-admin-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_ACCOUNT = "10000001"
_OTHER = "10000002"
_TARGET = {"account": _OTHER, "nick": "小号", "avatar": "http://face/2", "available": False, "dead_reason": "登录态失效"}
_STATUS = {
    "account": _ACCOUNT, "nick": "咪啪", "online": True, "is_login": True,
    "login_error": "", "qrcode": "", "quick_login": [_TARGET],
}
_PLATFORM_ROUTES = (
    ("GET", "/v1/platforms/qq/account"),
    ("POST", "/v1/platforms/qq/account/switch"),
    ("POST", "/v1/platforms/qq/account/qrcode"),
    ("POST", "/v1/platforms/qq/account/relogin"),
    ("POST", "/v1/platforms/qq/account/pin"),
    ("GET", "/v1/platforms/qq/account/scope"),
    ("POST", "/v1/platforms/qq/account/scope/migrate"),
)
_LEGACY_ROUTES = (
    ("GET", "/v1/qq/account"),
    ("POST", "/v1/qq/account/quick-login"),
    ("POST", "/v1/qq/account/qrcode"),
    ("POST", "/v1/qq/account/relogin"),
    ("POST", "/v1/qq/account/pin"),
    ("GET", "/v1/admin/qq/scope"),
    ("POST", "/v1/admin/qq/scope/migrate"),
)


class _FakeOps:
    """假的平台实现：每个动作默认成功，outcomes 里换成别的返回值或要抛的异常。"""

    def __init__(self, **outcomes: Any) -> None:
        self.configured = True
        self.calls: list[tuple[str, Any]] = []
        self.outcomes = outcomes

    def _done(self, action: str, argument: Any, default: Any) -> Any:
        self.calls.append((action, argument))
        answer = self.outcomes.get(action, default)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def status(self) -> dict[str, Any]:
        return self._done("status", None, dict(_STATUS))

    async def switch(self, account: str) -> dict[str, Any]:
        return self._done("switch", account, {"ok": True, "account": account, "pinned": True, "pin_error": ""})

    async def login_qrcode(self) -> str:
        return self._done("login_qrcode", None, "qrcode://scan")

    async def relogin(self) -> None:
        return self._done("relogin", None, None)

    async def pin(self) -> str:
        return self._done("pin", None, _ACCOUNT)


class _Hook:
    """冒充平台的 account_ops 钩子模块，记下工厂被调了几次、拿到的是哪个工作区。"""

    def __init__(self, ops: _FakeOps) -> None:
        self.ops = ops
        self.roots: list[Path] = []

    def account_ops(self, workspace_root: Path) -> _FakeOps:
        self.roots.append(Path(workspace_root))
        return self.ops


def _platform(platform_id: str, display_name: str, **fields: Any) -> PlatformDescriptor:
    return PlatformDescriptor(
        id=platform_id, display_name=display_name, short_name=display_name,
        channels=(ChannelDescriptor(f"{platform_id}_group", CHANNEL_GROUP),), **fields,
    )


def _registered(platform: PlatformDescriptor) -> Iterator[PlatformDescriptor]:
    register_platform(platform)
    try:
        yield platform
    finally:
        unregister_platform(platform.id)


def _events(workspace: Path, type_: str) -> list[dict[str, Any]]:
    path = workspace / "data" / "events.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    return [event for line in lines if (event := json.loads(line))["type"] == type_]


@pytest.fixture(autouse=True)
def _forget_the_loaded_migration():
    # 脚本按文件加载后缓存在 sys.modules，不撤掉的话下一条测试量到的是上一条留下的那份。
    name = platform_accounts._MIGRATION_MODULE
    before = sys.modules.get(name)
    yield
    if before is None:
        sys.modules.pop(name, None)
    else:
        sys.modules[name] = before


@pytest.fixture
def ops() -> _FakeOps:
    return _FakeOps()


@pytest.fixture
def hook(ops: _FakeOps, monkeypatch: pytest.MonkeyPatch) -> _Hook:
    stub = _Hook(ops)
    real = platform_accounts.platform_hook

    def lookup(platform: PlatformDescriptor, name: str):
        if platform.id == QQ_NAPCAT.id and name == HOOK_ACCOUNT_OPS:
            return stub
        return real(platform, name)

    monkeypatch.setattr(platform_accounts, "platform_hook", lookup)
    return stub


@pytest.fixture
def call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hook: _Hook):
    monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
    monkeypatch.setattr(admin_api, "_admin_token", "")
    monkeypatch.setattr(admin_api, "_auth_failures", type(admin_api._auth_failures)())
    state = SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="run-accounts"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    accounts = PlatformAccounts(tmp_path)
    app = FastAPI()
    app.include_router(create_platform_account_router(state, accounts), prefix="/v1/platforms")
    app.include_router(create_legacy_qq_account_router(state, accounts))

    def request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
                return await http.request(method, url, headers=kwargs.pop("headers", _AUTH), **kwargs)

        return asyncio.run(go())

    return request


@pytest.fixture
def roster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """两个实例共用一份清单，本控制台是挂在 /i/koki 下的那个。"""
    path = tmp_path / "instances.yaml"
    path.write_text(
        "instances:\n"
        f"  - {{id: rong, label: 主号, path: '', idx: 0, accounts: {{qq: '{_ACCOUNT}'}}}}\n"
        "  - {id: koki, label: 小号, path: /i/koki, idx: 1}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLONOTH_INSTANCES_FILE", str(path))
    monkeypatch.setenv("CLONOTH_URL_PREFIX", "/i/koki")
    return path


def _roster_accounts(path: Path) -> dict[str, dict[str, str]]:
    rows = yaml.safe_load(path.read_text(encoding="utf-8"))["instances"]
    return {row["id"]: row.get("accounts") or {} for row in rows}


@pytest.fixture
def otherchat():
    yield from _registered(_platform("otherchat", "别的平台"))


@pytest.fixture
def per_account_ids():
    yield from _registered(_platform(
        "peraccount", "按号发号平台",
        id_scope=ID_SCOPE_ACCOUNT,
        secrets=(SecretField(SECRET_NAME, "会话键摘要密钥", env="PERACCOUNT_HASH_SECRET"),),
    ))


class TestOpsPerPlatform:
    def test_the_platform_builds_its_ops_once(self, call, hook: _Hook, tmp_path: Path) -> None:
        # 平台实现缓存着登录凭证，每次新建就每次重登。
        assert call("GET", "/v1/platforms/qq/account").status_code == 200
        assert call("POST", "/v1/platforms/qq/account/pin").status_code == 200
        assert call("GET", "/v1/qq/account").status_code == 200

        assert hook.roots == [tmp_path]

    def test_a_platform_without_the_hook_says_so_by_name(self, call, otherchat) -> None:
        response = call("GET", "/v1/platforms/otherchat/account")

        assert response.status_code == 404
        assert response.json()["detail"] == "别的平台 没有账号运维"

    def test_each_platform_keeps_its_own_ops(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, otherchat: PlatformDescriptor,
    ) -> None:
        built: list[str] = []

        def lookup(platform: PlatformDescriptor, name: str) -> Any:
            built.append(f"{platform.id}:{name}")
            return SimpleNamespace(account_ops=lambda root: _FakeOps())

        monkeypatch.setattr(platform_accounts, "platform_hook", lookup)
        accounts = PlatformAccounts(tmp_path)

        first, again, other = accounts.ops(QQ_NAPCAT), accounts.ops(QQ_NAPCAT), accounts.ops(otherchat)

        assert first is again and other is not first
        assert built == [f"qq:{HOOK_ACCOUNT_OPS}", f"otherchat:{HOOK_ACCOUNT_OPS}"]

    def test_an_unknown_platform_is_404(self, call) -> None:
        assert call("GET", "/v1/platforms/nowhere/account").status_code == 404
        assert call("POST", "/v1/platforms/nowhere/account/pin").status_code == 404

    def test_the_real_app_mounts_both_routers(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLONOTH_ADMIN_TOKEN", _TOKEN)
        app = create_app(
            state=SupervisorState(
                workspace_root=tmp_path,
                eventlog=EventLog(tmp_path / "data" / "events.jsonl", run_id="run-mounted"),
                policy=PolicyEngine(workspace_root=tmp_path),
            ),
            process_manager=None,
            config_store=ConfigStore(path=tmp_path / "data" / "config.yaml"),
        )

        mounted = {
            (method.upper(), path.replace("{platform_id}", "qq"))
            for path, operations in app.openapi()["paths"].items()
            for method in operations
        }

        assert {*_PLATFORM_ROUTES, *_LEGACY_ROUTES} <= mounted
        assert isinstance(app.state.platform_accounts, PlatformAccounts)


class TestAccountStatus:
    def test_the_status_is_passed_through_with_the_reachability(self, call) -> None:
        response = call("GET", "/v1/platforms/qq/account")

        assert response.status_code == 200
        assert response.json() == {"configured": True, "reachable": True, **_STATUS}

    def test_an_unconfigured_platform_is_not_contacted(self, call, ops: _FakeOps) -> None:
        ops.configured = False

        assert call("GET", "/v1/platforms/qq/account").json() == {"configured": False}
        assert ops.calls == []

    def test_a_platform_that_has_not_answered_yet_is_not_an_error(self, call, ops: _FakeOps) -> None:
        # 换号重启和首次部署都会经过这里；判成 502 的话页面连「正在重启」都说不了。
        ops.outcomes["status"] = AccountUnreachable("连不上 NapCat WebUI")

        response = call("GET", "/v1/platforms/qq/account")

        assert response.status_code == 200
        assert response.json() == {"configured": True, "reachable": False, "reason": "连不上 NapCat WebUI"}

    def test_any_other_failure_is_a_bad_gateway(self, call, ops: _FakeOps) -> None:
        ops.outcomes["status"] = AccountOpsError("NapCat 拒绝了 token")

        response = call("GET", "/v1/platforms/qq/account")

        assert response.status_code == 502 and response.json()["detail"] == "NapCat 拒绝了 token"


class TestSwitchAccount:
    def test_a_switch_is_forwarded_and_audited(self, call, ops: _FakeOps, tmp_path: Path) -> None:
        response = call("POST", "/v1/platforms/qq/account/switch", json={"account": _OTHER})

        assert response.status_code == 200
        assert response.json() == {"ok": True, "account": _OTHER, "pinned": True, "pin_error": ""}
        assert ops.calls == [("switch", _OTHER)]
        assert [event["payload"] for event in _events(tmp_path, "platform_account_switched")] == [
            {"platform": "qq", "account": _OTHER, "pinned": True},
        ]

    def test_a_failed_pin_is_still_a_switch(self, call, ops: _FakeOps, tmp_path: Path) -> None:
        ops.outcomes["switch"] = {"ok": True, "account": _OTHER, "pinned": False, "pin_error": "等待账号操作超时"}

        response = call("POST", "/v1/platforms/qq/account/switch", json={"account": _OTHER})

        assert response.status_code == 200 and response.json()["pinned"] is False
        assert _events(tmp_path, "platform_account_switched")[0]["payload"]["pinned"] is False

    @pytest.mark.parametrize(
        "refusal, status",
        [
            (AccountRequestInvalid("QQ 号必须是数字"), 400),
            (AccountConflict("当前没有登录任何账号"), 409),
            (AccountUnreachable("连不上 NapCat WebUI"), 502),
            (AccountOpsError("SetQuickLogin 返回错误"), 502),
        ],
    )
    def test_each_refusal_gets_its_own_status(
        self, call, ops: _FakeOps, tmp_path: Path, refusal: Exception, status: int,
    ) -> None:
        ops.outcomes["switch"] = refusal

        response = call("POST", "/v1/platforms/qq/account/switch", json={"account": "nope"})

        assert response.status_code == status and response.json()["detail"] == str(refusal)
        assert _events(tmp_path, "platform_account_switched") == []


class TestLoginQrcode:
    def test_the_qrcode_comes_back_with_the_reachability(self, call, ops: _FakeOps) -> None:
        response = call("POST", "/v1/platforms/qq/account/qrcode")

        assert response.json() == {"qrcode": "qrcode://scan", "reachable": True}
        assert ops.calls == [("login_qrcode", None)]

    def test_a_platform_still_restarting_is_reported_not_raised(self, call, ops: _FakeOps) -> None:
        ops.outcomes["login_qrcode"] = AccountUnreachable("连不上 NapCat WebUI")

        response = call("POST", "/v1/platforms/qq/account/qrcode")

        assert response.status_code == 200
        assert response.json() == {"qrcode": "", "reachable": False, "reason": "连不上 NapCat WebUI"}

    def test_any_other_failure_is_a_bad_gateway(self, call, ops: _FakeOps) -> None:
        ops.outcomes["login_qrcode"] = AccountOpsError("GetQQLoginQrcode 返回错误")

        assert call("POST", "/v1/platforms/qq/account/qrcode").status_code == 502


class TestRelogin:
    def test_a_relogin_is_forwarded_and_audited(self, call, ops: _FakeOps, tmp_path: Path) -> None:
        response = call("POST", "/v1/platforms/qq/account/relogin")

        assert response.status_code == 200 and response.json() == {"ok": True}
        assert ops.calls == [("relogin", None)]
        assert [event["payload"] for event in _events(tmp_path, "platform_account_relogin_requested")] == [
            {"platform": "qq"},
        ]

    def test_the_request_is_recorded_even_when_the_restart_fails(
        self, call, ops: _FakeOps, tmp_path: Path,
    ) -> None:
        # 重启卡在半路也查得到有人要过换号。
        ops.outcomes["relogin"] = AccountOpsError("等待账号操作超时")

        response = call("POST", "/v1/platforms/qq/account/relogin")

        assert response.status_code == 502
        assert len(_events(tmp_path, "platform_account_relogin_requested")) == 1


class TestPinAccount:
    def test_the_current_account_is_pinned(self, call, ops: _FakeOps) -> None:
        response = call("POST", "/v1/platforms/qq/account/pin")

        assert response.json() == {"ok": True, "account": _ACCOUNT}
        assert ops.calls == [("pin", None)]

    def test_nothing_logged_in_is_a_conflict(self, call, ops: _FakeOps) -> None:
        ops.outcomes["pin"] = AccountConflict("当前没有登录任何账号")

        response = call("POST", "/v1/platforms/qq/account/pin")

        assert response.status_code == 409 and response.json()["detail"] == "当前没有登录任何账号"


class TestAccountOwnership:
    """一个号只能归一个实例。两边同时登会互相把对方踢下线，数据也会分成两份。"""

    @pytest.mark.parametrize("url,payload", [
        ("/v1/platforms/qq/account/switch", {"account": _ACCOUNT}),
        ("/v1/qq/account/quick-login", {"uin": _ACCOUNT}),
    ])
    def test_a_number_another_instance_holds_is_a_conflict(
        self, call, ops: _FakeOps, roster: Path, url: str, payload: dict[str, str],
    ) -> None:
        response = call("POST", url, json=payload)

        assert response.status_code == 409
        # 说清楚去哪台控制台登，不然只能挨个实例翻。
        assert response.json()["detail"] == f"{_ACCOUNT} 归实例 主号 管，去它的控制台登录"
        assert ops.calls == []

    def test_the_requested_number_is_normalised_before_the_lookup(
        self, call, ops: _FakeOps, roster: Path,
    ) -> None:
        # 前端表单里粘进来的号常带空白，不规范化就绕过了归属检查。
        assert call("POST", "/v1/platforms/qq/account/switch", json={"account": f"  {_ACCOUNT} "}).status_code == 409
        assert ops.calls == []

    def test_the_number_this_instance_already_holds_can_be_relogged(
        self, call, ops: _FakeOps, roster: Path,
    ) -> None:
        roster.write_text(
            f"instances:\n  - {{id: koki, label: 小号, path: /i/koki, idx: 1, accounts: {{qq: '{_OTHER}'}}}}\n",
            encoding="utf-8",
        )

        assert call("POST", "/v1/platforms/qq/account/switch", json={"account": _OTHER}).status_code == 200

    @pytest.mark.parametrize("url,payload", [
        ("/v1/platforms/qq/account/switch", {"account": _OTHER}),
        ("/v1/qq/account/quick-login", {"uin": _OTHER}),
    ])
    def test_a_switch_lands_on_this_instance_row(
        self, call, ops: _FakeOps, roster: Path, url: str, payload: dict[str, str],
    ) -> None:
        assert call("POST", url, json=payload).status_code == 200
        assert _roster_accounts(roster) == {"rong": {"qq": _ACCOUNT}, "koki": {"qq": _OTHER}}

    @pytest.mark.parametrize("url", ["/v1/platforms/qq/account/pin", "/v1/qq/account/pin"])
    def test_pinning_records_the_number_that_was_scanned_in(
        self, call, ops: _FakeOps, roster: Path, url: str,
    ) -> None:
        # 扫码登上的号只有钉自动登录这一步说得清是哪个。
        ops.outcomes["pin"] = _OTHER

        assert call("POST", url).status_code == 200
        assert _roster_accounts(roster)["koki"] == {"qq": _OTHER}

    @pytest.mark.parametrize("url,payload", [
        ("/v1/platforms/qq/account/switch", {"account": _OTHER}),
        ("/v1/platforms/qq/account/pin", None),
    ])
    def test_a_roster_that_cannot_be_written_does_not_undo_the_switch(
        self, call, ops: _FakeOps, roster: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture, url: str, payload: dict[str, str] | None,
    ) -> None:
        # 号已经换过去了，记不上清单只是别的实例少一道拦截，不能反过来报失败。
        def refuse(*_args: Any, **_kwargs: Any) -> bool:
            raise OSError("清单所在的盘满了")

        monkeypatch.setattr(platform_accounts, "record_account", refuse)

        with caplog.at_level(logging.WARNING, logger="supervisor.platform_accounts"):
            response = call("POST", url, json=payload) if payload else call("POST", url)

        assert response.status_code == 200
        assert [record.message for record in caplog.records if record.levelno == logging.WARNING]
        assert "清单所在的盘满了" in caplog.text


class TestLegacyQqRoutes:
    def test_the_status_still_calls_the_account_uin(self, call) -> None:
        response = call("GET", "/v1/qq/account")

        assert response.status_code == 200
        assert response.json() == {
            "configured": True, "reachable": True,
            "uin": _ACCOUNT, "nick": "咪啪", "online": True, "is_login": True,
            "login_error": "", "qrcode": "",
            "quick_login": [
                {"uin": _OTHER, "nick": "小号", "avatar": "http://face/2", "available": False, "dead_reason": "登录态失效"},
            ],
        }

    def test_an_unconfigured_platform_answers_the_same_as_before(self, call, ops: _FakeOps) -> None:
        ops.configured = False

        assert call("GET", "/v1/qq/account").json() == {"configured": False}

    def test_an_unreachable_platform_answers_the_same_as_before(self, call, ops: _FakeOps) -> None:
        ops.outcomes["status"] = AccountUnreachable("连不上 NapCat WebUI")

        assert call("GET", "/v1/qq/account").json() == {
            "configured": True, "reachable": False, "reason": "连不上 NapCat WebUI",
        }

    def test_quick_login_takes_a_uin_and_answers_with_one(self, call, ops: _FakeOps, tmp_path: Path) -> None:
        response = call("POST", "/v1/qq/account/quick-login", json={"uin": _OTHER})

        assert response.status_code == 200
        assert response.json() == {"ok": True, "uin": _OTHER, "pinned": True, "pin_error": ""}
        assert ops.calls == [("switch", _OTHER)]
        assert _events(tmp_path, "platform_account_switched")[0]["payload"] == {
            "platform": "qq", "account": _OTHER, "pinned": True,
        }

    def test_a_non_numeric_uin_is_still_a_bad_request(self, call, ops: _FakeOps) -> None:
        ops.outcomes["switch"] = AccountRequestInvalid("QQ 号必须是数字")

        response = call("POST", "/v1/qq/account/quick-login", json={"uin": "abc"})

        assert response.status_code == 400 and response.json()["detail"] == "QQ 号必须是数字"

    def test_the_qrcode_and_relogin_answers_are_unchanged(self, call, tmp_path: Path) -> None:
        assert call("POST", "/v1/qq/account/qrcode").json() == {"qrcode": "qrcode://scan", "reachable": True}
        assert call("POST", "/v1/qq/account/relogin").json() == {"ok": True}
        assert len(_events(tmp_path, "platform_account_relogin_requested")) == 1

    def test_pinning_still_answers_with_a_uin(self, call) -> None:
        assert call("POST", "/v1/qq/account/pin").json() == {"ok": True, "uin": _ACCOUNT}


class TestAdminToken:
    @pytest.mark.parametrize("method, url", [*_PLATFORM_ROUTES, *_LEGACY_ROUTES], ids=lambda value: str(value))
    def test_every_route_needs_the_admin_token(self, call, ops: _FakeOps, method: str, url: str) -> None:
        response = call(method, url, headers={}, json={"account": _OTHER, "uin": _OTHER, "target": _ACCOUNT})

        assert response.status_code == 401
        assert ops.calls == []


class TestScopePlatform:
    def test_an_unknown_platform_has_no_scope(self, call) -> None:
        assert call("GET", "/v1/platforms/nowhere/account/scope").status_code == 404

    def test_a_platform_without_scoped_keys_cannot_hand_data_over(self, call, otherchat) -> None:
        response = call("GET", "/v1/platforms/otherchat/account/scope")

        assert response.status_code == 404
        assert response.json()["detail"] == "别的平台 的会话键不按账号分作用域，没有归属可搬"

    def test_a_platform_whose_ids_follow_the_account_is_sent_elsewhere(self, call, per_account_ids) -> None:
        response = call("POST", "/v1/platforms/peraccount/account/scope/migrate", json={"target": _ACCOUNT})

        assert response.status_code == 404
        assert "migrate_platform_identity.py" in response.json()["detail"]


class TestScopeMigrationModule:
    def test_the_script_is_loaded_once_and_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delitem(sys.modules, platform_accounts._MIGRATION_MODULE, raising=False)

        first = platform_accounts.scope_migration()

        assert platform_accounts.scope_migration() is first
        assert sys.modules[platform_accounts._MIGRATION_MODULE] is first
        assert first.check_platform(QQ_NAPCAT) is QQ_NAPCAT

    def test_a_script_that_blows_up_leaves_no_half_module_behind(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 留着半截模块的话，下一次就会拿它去搬。
        broken = tmp_path / "broken_migration.py"
        broken.write_text("raise RuntimeError('脚本坏了')\n", encoding="utf-8")
        monkeypatch.delitem(sys.modules, platform_accounts._MIGRATION_MODULE, raising=False)
        monkeypatch.setattr(platform_accounts, "_MIGRATION_SCRIPT", broken)

        with pytest.raises(RuntimeError, match="脚本坏了"):
            platform_accounts.scope_migration()

        assert platform_accounts._MIGRATION_MODULE not in sys.modules
