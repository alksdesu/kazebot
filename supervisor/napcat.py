"""NapCat WebUI 代理：查询与切换 bot 登录的 QQ 账号。

WebUI 只监听 127.0.0.1，且有独立于本系统的 token——浏览器既够不到它，也不该拿到
那个 token，所以账号相关操作一律由 supervisor 转发。
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

import httpx

from clonoth_runtime import read_admin_token

_BASE_URL = "http://127.0.0.1:6099"
_TOKEN_KEY = "NAPCAT_WEBUI_TOKEN"
# NapCat 的凭证一小时作废，留足余量提前换。
_CREDENTIAL_TTL_SEC = 2400.0
_TIMEOUT = httpx.Timeout(20.0, connect=5.0)

_UNAUTHORIZED = object()
# 改 root 的 webui.json 与重启容器都超出本进程权限（服务带 NoNewPrivileges，提不了权）。
# 请求写成文件，由 napcat-account.path 触发的 root 侧 runner 校验令牌后执行。
_REQUEST_NAME = "napcat-account.request"
_RESULT_NAME = "napcat-account.result"
_ACTION_TIMEOUT_SEC = 150.0


class NapCatError(RuntimeError):
    """失败原因可以直接展示给人。"""


def _read_env_value(env_path: Path, key: str) -> str:
    # NapCat 的 webui.json 是 root 0600，supervisor 以普通用户跑，只能从 .env 拿。
    if not env_path.is_file():
        return ""
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        name, value = text.split("=", 1)
        if name.strip() == key:
            return value.strip().strip("'\"")
    return ""


class NapCatClient:
    """带凭证缓存的最小 WebUI 客户端。"""

    def __init__(self, workspace_root: Path, *, base_url: str = _BASE_URL) -> None:
        self._env_path = Path(workspace_root) / ".env"
        self._base = base_url.rstrip("/")
        self._credential = ""
        self._issued_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(_read_env_value(self._env_path, _TOKEN_KEY))

    async def _authenticate(self, http: httpx.AsyncClient) -> str:
        token = _read_env_value(self._env_path, _TOKEN_KEY)
        if not token:
            raise NapCatError(
                _TOKEN_KEY + " 未配置。它在 NapCat 容器的 /app/napcat/config/webui.json 里，"
                "用 manage_secret 或直接写进 .env。"
            )
        # NapCat 校验 sha256(token + ".napcat")，明文 token 不离开本机。
        digest = hashlib.sha256((token + ".napcat").encode("utf-8")).hexdigest()
        try:
            resp = await http.post(self._base + "/api/auth/login", json={"hash": digest})
            body = resp.json()
        except Exception as exc:
            raise NapCatError("连不上 NapCat WebUI：" + str(exc)) from exc
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
            raise NapCatError(endpoint + " 调用失败：" + str(exc)) from exc
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
                raise NapCatError("NapCat 鉴权失败，请确认 " + _TOKEN_KEY + " 是当前的 WebUI token")
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
        raise NapCatError("等待账号操作超时，确认 napcat-account.path 已启用")

    async def set_auto_login(self, uin: str) -> None:
        """让容器重启后自己登回来。不设的话 NapCat 起来只会停在等扫码。"""
        await self._helper("set-auto", uin=uin)

    async def enter_login_mode(self) -> None:
        """清掉自动登录再重启，容器起来就停在等扫码，这时才拿得到二维码。"""
        await self._helper("clear-auto")
        await self._helper("restart")
        self._credential = ""
        self._issued_at = 0.0

    async def account(self) -> dict[str, Any]:
        """当前账号 + 可免扫码切换的号。任一子查询失败都不该让整页打不开。"""
        info = await self.call("GetQQLoginInfo")
        status: dict[str, Any] = {}
        quick: list[Any] = []
        try:
            status = await self.call("CheckLoginStatus") or {}
        except NapCatError:
            status = {}
        try:
            quick = await self.call("GetQuickLoginListNew") or []
        except NapCatError:
            quick = []
        current = info or {}
        return {
            "uin": str(current.get("uin") or ""),
            "nick": str(current.get("nick") or ""),
            "online": bool(current.get("online")),
            "is_login": bool(status.get("isLogin", current.get("online"))),
            "login_error": str(status.get("loginError") or ""),
            "quick_login": [str(item) for item in quick if str(item or "").strip()],
        }
