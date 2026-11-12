"""QQ 的账号运维：经 NapCat WebUI 查询、切换、扫码登录 bot 的 QQ 号。

WebUI 只监听 127.0.0.1，且有独立于本系统的 token——浏览器既够不到它，也不该拿到
那个 token，所以账号相关操作一律由 supervisor 经这里转发。
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

import config_layout
from clonoth_runtime import platform_secret, read_admin_token
from platforms import live as platform_live
from platforms.account_ops import (
    AccountConflict,
    AccountOpsError,
    AccountRequestInvalid,
    AccountStatus,
    AccountUnreachable,
    QuickLoginTarget,
    SwitchResult,
)
from platforms.builtin import QQ_NAPCAT

_BASE_URL = "http://127.0.0.1:6099"
_TOKEN = QQ_NAPCAT.secret("napcat_webui_token")
_URL_KEY = "NAPCAT_WEBUI_URL"
# NapCat 的凭证一小时作废，留足余量提前换。
_CREDENTIAL_TTL_SEC = 2400.0
_TIMEOUT = httpx.Timeout(20.0, connect=5.0)

_UNAUTHORIZED = object()
# 改 root 的 webui.json 与重启容器都超出本进程权限（服务带 NoNewPrivileges，提不了权）。
# 请求写成文件，由 napcat-account.path 触发的 root 侧 runner 校验令牌后执行。
_REQUEST_NAME = "napcat-account.request"
_RESULT_NAME = "napcat-account.result"
# 切换被 QQ 拒过的号。NapCat 自报的 isQuickLogin 会在登录态早就失效时仍然给 true，
# 唯一可信的信号是真切一次的结果，所以记下来供下次置灰。
_DEAD_LOGINS_NAME = "qq_dead_logins.json"
_ACTION_TIMEOUT_SEC = 150.0
# 容器重启到 QQ 核心能应答通常十几秒，留足余量。
_READY_TIMEOUT_SEC = 90.0
_READY_POLL_SEC = 1.5
# RefreshQRcode 立刻返回，新码稍后才由登录监听回填，通常一秒内到。
_QRCODE_REFRESH_TIMEOUT_SEC = 5.0
_QRCODE_REFRESH_POLL_SEC = 0.3
# SetQuickLogin 立刻返回，登录态要等 QQ 核心和服务端走完注册才真的在；SsoInfoSync 一般几秒内到。
_QUICK_RELOGIN_TIMEOUT_SEC = 60.0
_QUICK_RELOGIN_POLL_SEC = 2.0
# QQ 密码/快速登录在新设备或异常设备上要求验证时的返回码；这两种人不来扫码就过不去。
_NEEDS_VERIFY_CODES = ("140022010", "140022011")
_SESSION_LOST_HINT = "NapCat 在这个状态下发不出新二维码，点「换个号登录」重启它再扫"


class NapCatError(AccountOpsError):
    """失败原因可以直接展示给人。"""


class NapCatUnreachable(NapCatError, AccountUnreachable):
    """WebUI 还没应答。换号重启和首次部署都会经过这里，不是故障。"""


def _read_env_value(env_path: Path, key: str) -> str:
    # NapCat 的 webui.json 是 root 0600，supervisor 以普通用户跑，只能从 .env 拿。
    return config_layout.read_env_file(env_path).get(key, "")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _quick_login_target(item: Any) -> QuickLoginTarget | None:
    """GetQuickLoginListNew 给对象，旧版 GetQuickLoginList 给裸 uin，两种都认。

    非数字 uin 一律丢掉：切换接口只收数字，放它过去就是让人点一个必然 400 的按钮。
    """
    if isinstance(item, dict):
        uin = str(item.get("uin") or "").strip()
        nick = str(item.get("nickName") or "").strip()
        avatar = str(item.get("faceUrl") or "").strip()
        # 字段缺失时按可用处理：旧版本没有这个标记，不能因此把号全禁掉。
        available = bool(item.get("isQuickLogin", True))
    else:
        uin, nick, avatar, available = str(item or "").strip(), "", "", True
    if not uin.isdigit():
        return None
    return {"account": uin, "nick": nick, "avatar": avatar, "available": available}


def _qrcode_text(data: Any) -> str:
    payload = data if isinstance(data, dict) else {}
    return str(payload.get("qrcode") or payload.get("qrcodeurl") or "")


@dataclass(frozen=True)
class _LoginState:
    """GetQQLoginInfo 与 CheckLoginStatus 合起来的登录处境。"""

    uin: str
    nick: str
    is_login: bool
    is_offline: bool
    login_error: str
    qrcode: str
    # CheckLoginStatus 没答上时上面几项都是缺省值，不能拿来下结论。
    checked: bool

    @property
    def session_lost(self) -> bool:
        """登录过的号被踢或登录态失效。NapCat 这时不再出码，只有重启才回到等扫码。"""
        return self.checked and bool(self.uin) and not self.is_login and not self.is_offline

    @property
    def visible_error(self) -> str:
        # NapCat 登录成功后不清扫码流程的残留，loginError 会一直留着上一轮的值。
        if self.is_login:
            return ""
        if self.session_lost:
            return "；".join(part for part in (self.login_error, _SESSION_LOST_HINT) if part)
        return self.login_error

    @property
    def visible_qrcode(self) -> str:
        # 只有还没登录过任何号时 qrcodeurl 才是眼下能扫的；登录过以后它留的是上一轮扫码的旧码。
        return "" if self.is_login or self.uin else self.qrcode


def _read_dead_logins(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


class NapCatClient:
    """带凭证缓存的最小 WebUI 客户端。"""

    def __init__(self, workspace_root: Path, *, base_url: str = "") -> None:
        self.workspace_root = Path(workspace_root)
        self._env_path = self.workspace_root / ".env"
        # 与 token 读同一份 .env：多开时每个实例连自己那个容器，地址和令牌必须成对，
        # 分成两个来源迟早会拿甲的令牌敲乙的容器。
        resolved = base_url or _read_env_value(self._env_path, _URL_KEY) or _BASE_URL
        self._base = resolved.rstrip("/")
        self._credential = ""
        self._issued_at = 0.0

    def _webui_token(self) -> str:
        # 两份文件每次都重读：令牌是容器首启后才回填进 .env 的，supervisor 不重启也得认得出。
        return platform_secret(
            self._env_path.parent, QQ_NAPCAT, _TOKEN.name, environ=config_layout.read_env_file(self._env_path),
        ).value

    @property
    def configured(self) -> bool:
        return bool(self._webui_token())

    async def _authenticate(self, http: httpx.AsyncClient) -> str:
        token = self._webui_token()
        if not token:
            raise NapCatError(
                _TOKEN.env + " 未配置。它在 NapCat 容器的 /app/napcat/config/webui.json 里，"
                "用 manage_secret 写进 .env，或写在 data/platform_secrets.yaml 的 qq." + _TOKEN.name + "。"
            )
        # NapCat 校验 sha256(token + ".napcat")，明文 token 不离开本机。
        digest = hashlib.sha256((token + ".napcat").encode("utf-8")).hexdigest()
        try:
            resp = await http.post(self._base + "/api/auth/login", json={"hash": digest})
            body = resp.json()
        except Exception as exc:
            raise NapCatUnreachable("连不上 NapCat WebUI：" + str(exc)) from exc
        credential = str(((body or {}).get("data") or {}).get("Credential") or "")
        if not credential:
            raise NapCatError("NapCat 拒绝了 token：" + str((body or {}).get("message") or "未知原因"))
        self._credential = credential
        self._issued_at = time.monotonic()
        return credential

    async def _post(
        self, http: httpx.AsyncClient, endpoint: str, payload: dict[str, Any], credential: str,
    ) -> Any:
        try:
            resp = await http.post(
                self._base + "/api/QQLogin/" + endpoint,
                json=payload,
                headers={"Authorization": "Bearer " + credential},
            )
            body = resp.json()
        except Exception as exc:
            raise NapCatUnreachable(endpoint + " 调用失败：" + str(exc)) from exc
        if str((body or {}).get("message") or "").lower() == "unauthorized":
            return _UNAUTHORIZED
        if (body or {}).get("code") != 0:
            raise NapCatError(endpoint + " 返回错误：" + str((body or {}).get("message") or body))
        return (body or {}).get("data")

    async def call(self, endpoint: str, payload: dict[str, Any] | None = None) -> Any:
        args = dict(payload or {})
        async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
            credential = self._credential
            if not credential or time.monotonic() - self._issued_at > _CREDENTIAL_TTL_SEC:
                credential = await self._authenticate(http)
            result = await self._post(http, endpoint, args, credential)
            if result is _UNAUTHORIZED:
                # NapCat 重启会作废旧凭证，重登一次再试。
                credential = await self._authenticate(http)
                result = await self._post(http, endpoint, args, credential)
            if result is _UNAUTHORIZED:
                raise NapCatError("NapCat 鉴权失败，请确认 " + _TOKEN.env + " 是当前的 WebUI token")
            return result

    async def _helper(self, action: str, uin: str = "") -> None:
        """把动作交给 root 侧 runner 执行。

        请求文件落在工作区里，任何以同一用户跑的东西（包括模型的工具子进程）都写得到，
        所以带上 admin token 让 runner 校验；policy 另外禁止模型写这个文件名。
        """
        root = Path(self._env_path).parent
        token = read_admin_token(root)
        if not token:
            raise NapCatError("读不到 admin token，无法请求账号操作")
        request = root / "data" / _REQUEST_NAME
        result = root / "data" / _RESULT_NAME
        nonce = secrets.token_hex(8)
        payload: dict[str, Any] = {"nonce": nonce, "token": token, "action": action}
        if uin:
            payload["uin"] = uin

        with contextlib.suppress(OSError):
            result.unlink()
        try:
            # 请求里带着 token，落盘那一刻就必须是 0600，不能先写完再 chmod。
            fd = os.open(str(request), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
        except OSError as exc:
            raise NapCatError("写账号操作请求失败：" + str(exc)) from exc

        deadline = time.monotonic() + _ACTION_TIMEOUT_SEC
        while time.monotonic() < deadline:
            await asyncio.sleep(0.4)
            try:
                data = json.loads(result.read_text(encoding="utf-8"))
            except Exception:
                continue
            if str(data.get("nonce") or "") != nonce:
                continue
            if not data.get("ok"):
                raise NapCatError(str(data.get("message") or "账号操作失败"))
            return
        with contextlib.suppress(OSError):
            request.unlink()
        raise NapCatError(
            "等待账号操作超时：root 侧没有接走请求，确认 napcat-account 路径单元已启用"
            "（主实例是 napcat-account.path，多开实例是 napcat-account@<实例名>.path）"
        )

    async def set_auto_login(self, uin: str) -> None:
        """让容器重启后自己登回来。不设的话 NapCat 起来只会停在等扫码。"""
        await self._helper("set-auto", uin=uin)

    async def wait_ready(self, timeout: float = _READY_TIMEOUT_SEC) -> bool:
        """等 NapCat 重新能应答。

        判据用 CheckLoginStatus 而不是端口通：WebUI 先起来、QQ 核心还没起来的那几秒里
        端口已经接受连接，但所有 QQLogin 接口都在报错。
        """
        deadline = time.monotonic() + timeout
        while True:
            try:
                await self.call("CheckLoginStatus")
                return True
            except NapCatError:
                if time.monotonic() >= deadline:
                    return False
                await asyncio.sleep(_READY_POLL_SEC)

    async def enter_login_mode(self) -> None:
        """清掉自动登录再重启，容器起来就停在等扫码，这时才拿得到二维码。"""
        await self._helper("clear-auto")
        await self._helper("restart")
        self._credential = ""
        self._issued_at = 0.0
        # runner 报告的 ok 只说明重启命令执行完了，容器这时还在起。不等这一下，
        # 调用方拿到 ok 就会立刻来查账号，撞上的必然是连不上。
        await self.wait_ready()

    @property
    def _dead_path(self) -> Path:
        return self._env_path.parent / "data" / _DEAD_LOGINS_NAME

    def _save_dead_logins(self, dead: dict[str, Any]) -> None:
        path = self._dead_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(dead, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            # 标记只影响一个按钮灰不灰，写不进去不值得让切换整体失败。
            pass

    def mark_login_dead(self, uin: str, reason: str) -> None:
        """记下这个号切不过去。只在 QQ 明确拒绝时调，连不上不算。"""
        key = str(uin or "").strip()
        if not key:
            return
        dead = self.dead_logins()
        dead[key] = {"at": _now_iso(), "reason": str(reason or "").strip()}
        self._save_dead_logins(dead)

    def dead_logins(self) -> dict[str, Any]:
        return _read_dead_logins(self._dead_path)

    def clear_login_dead(self, uin: str) -> bool:
        """这个号又能用了，撤掉标记。"""
        key = str(uin or "").strip()
        dead = self.dead_logins()
        if not key or key not in dead:
            return False
        dead.pop(key)
        self._save_dead_logins(dead)
        return True


class NapCatAccountOps:
    """账号运维约定的 QQ 实现。凭证缓存在 NapCatClient 上，supervisor 按平台只留一个实例。"""

    def __init__(self, workspace_root: Path) -> None:
        self.client = NapCatClient(workspace_root)

    @property
    def configured(self) -> bool:
        return self.client.configured

    async def _login_state(self) -> _LoginState:
        client = self.client
        current = await client.call("GetQQLoginInfo") or {}
        try:
            login = await client.call("CheckLoginStatus") or {}
            checked = True
        except NapCatError:
            login, checked = {}, False
        return _LoginState(
            uin=str(current.get("uin") or ""),
            nick=str(current.get("nick") or ""),
            is_login=bool(login.get("isLogin", False)),
            is_offline=bool(login.get("isOffline", False)),
            login_error=str(login.get("loginError") or ""),
            qrcode=str(login.get("qrcodeurl") or ""),
            checked=checked,
        )

    async def status(self) -> AccountStatus:
        """任一子查询失败都不该让整页打不开。"""
        client = self.client
        state = await self._login_state()
        try:
            quick = await client.call("GetQuickLoginListNew") or []
        except NapCatError:
            quick = []
        # 扫码登回来了就撤掉死号标记，否则一次失效会永久跟着这个号。
        if state.is_login and state.uin:
            client.clear_login_dead(state.uin)
        dead = client.dead_logins()
        targets: list[QuickLoginTarget] = []
        for item in quick:
            target = _quick_login_target(item)
            if target is None:
                continue
            note = dead.get(target["account"])
            if isinstance(note, dict):
                target["available"] = False
                target["dead_reason"] = str(note.get("reason") or "")
            targets.append(target)
        return {
            "account": state.uin,
            "nick": state.nick,
            # GetQQLoginInfo 不返回在线状态，只有 CheckLoginStatus 有；用它推会恒为「不在线」。
            "online": state.is_login and not state.is_offline,
            "is_login": state.is_login,
            "login_error": state.visible_error,
            "qrcode": state.visible_qrcode,
            "quick_login": targets,
            **self._link_facts(),
        }

    def _link_facts(self) -> dict[str, Any]:
        """bot 进程公布的掉线原因与恢复处境。bot 没在跑时这几项是空的，不去猜。"""
        status = platform_live.read_live_status(QQ_NAPCAT, self.client.workspace_root)
        return {
            "offline_reason": status.offline_reason if status.fresh else "",
            "last_offline_at": status.last_offline_at or 0.0,
            "recovery": dict(status.recovery) if status.fresh and status.recovery is not None else {},
        }

    async def switch(self, account: str) -> SwitchResult:
        uin = str(account or "").strip()
        if not uin.isdigit():
            raise AccountRequestInvalid("QQ 号必须是数字")
        try:
            await self.client.call("SetQuickLogin", {"uin": uin})
        except NapCatUnreachable:
            # 容器没起来跟这个号本身没关系，标死了下次就再也点不动。
            raise
        except NapCatError as exc:
            # QQ 明确拒了才算死号：缓存的登录态过期后 isQuickLogin 仍报 true，
            # 不记一笔的话这个号会永远停在列表里，点一次失败一次。
            self.client.mark_login_dead(uin, str(exc))
            raise
        # 号已经切过去了，钉自动登录只是锦上添花：这一步失败不该让人以为没切成。
        try:
            await self.client.set_auto_login(uin)
        except NapCatError as exc:
            return {"ok": True, "account": uin, "pinned": False, "pin_error": str(exc)}
        return {"ok": True, "account": uin, "pinned": True, "pin_error": ""}

    async def login_qrcode(self) -> str:
        client = self.client
        if (await self._login_state()).session_lost:
            return ""
        before = _qrcode_text(await client.call("GetQQLoginQrcode"))
        try:
            await client.call("RefreshQRcode")
        except NapCatError:
            # 没有这个接口的老版本 NapCat 只在过期时自己换码，读到的就是当前这张。
            return before
        return await self._await_new_qrcode(before)

    async def _await_new_qrcode(self, before: str) -> str:
        deadline = time.monotonic() + _QRCODE_REFRESH_TIMEOUT_SEC
        while True:
            current = _qrcode_text(await self.client.call("GetQQLoginQrcode"))
            if current != before or time.monotonic() >= deadline:
                return current
            await asyncio.sleep(_QRCODE_REFRESH_POLL_SEC)

    async def relogin(self) -> None:
        # NapCat 在已登录状态下拒发二维码，且不提供登出，只能重启进等扫码状态。
        await self.client.enter_login_mode()

    async def login_state(self) -> dict[str, Any]:
        """掉线恢复要的那几项登录处境，不带可切换的号。"""
        state = await self._login_state()
        return {
            "account": state.uin, "is_login": state.is_login, "is_offline": state.is_offline,
            "login_error": state.login_error, "session_lost": state.session_lost, "checked": state.checked,
        }

    async def enter_login_mode(self) -> None:
        await self.client.enter_login_mode()

    async def quick_relogin(self, uin: str) -> dict[str, Any]:
        """不重启容器，直接让 NapCat 用本地凭据把这个号登回去。

        被服务端注销登录态时本地快速登录凭据多半还在，这一步能省掉一次重启和扫码；
        被拒才记死号，连不上不算。返回里的 needs_verify 表示 QQ 要人验证设备，自动化到此为止。
        """
        account = str(uin or "").strip()
        if not account.isdigit():
            raise AccountRequestInvalid("QQ 号必须是数字")
        before = await self._login_state()
        if before.is_login and not before.is_offline and before.uin == account:
            return {"ok": True, "account": account, "message": "登录态仍在", "needs_verify": False}
        try:
            await self.client.call("SetQuickLogin", {"uin": account})
        except NapCatUnreachable:
            raise
        except NapCatError as exc:
            message = str(exc)
            needs_verify = any(code in message for code in _NEEDS_VERIFY_CODES)
            self.client.mark_login_dead(account, message)
            return {"ok": False, "account": account, "message": message, "needs_verify": needs_verify}
        deadline = time.monotonic() + _QUICK_RELOGIN_TIMEOUT_SEC
        while True:
            state = await self._login_state()
            if state.is_login and not state.is_offline:
                self.client.clear_login_dead(account)
                return {"ok": True, "account": account, "message": "", "needs_verify": False}
            if state.checked and not state.is_login and state.login_error and state.login_error != before.login_error:
                # 登录监听回了新的错误而不是还在等：不必耗完超时。
                break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(_QUICK_RELOGIN_POLL_SEC)
        message = state.login_error or "快速登录后登录态一直没有建立"
        self.client.mark_login_dead(account, message)
        return {"ok": False, "account": account, "message": message, "needs_verify": any(code in message for code in _NEEDS_VERIFY_CODES)}

    async def pin(self) -> str:
        uin = (await self.status()).get("account", "")
        if not uin:
            raise AccountConflict("当前没有登录任何账号")
        await self.client.set_auto_login(uin)
        return uin


def account_ops(workspace_root: Path) -> NapCatAccountOps:
    return NapCatAccountOps(workspace_root)
