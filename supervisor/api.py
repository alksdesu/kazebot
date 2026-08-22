from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import secrets
from fastapi import Body, FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse, Response

from . import provisioning
from .config_store import ConfigStore
from .sticker_api import create_sticker_router
from .instances import load_instances, url_prefix
from .log_access import LogReader
from .process_manager import ProcessManager
from .qq_intent import IntentResult as QQIntentResult, build_instruction as qq_intent_instruction
from .state import SupervisorState
from .types import (
    AdminStateOut,
    AppConfigPublic,
    Approval,
    ApprovalDecisionIn,
    ApprovalRequestIn,
    ApprovalStatus,
    ConfigReloadOut,
    Event,
    HandoffEventIn,
    HealthOut,
    ImageDefaultChannelIn,
    InboundAckIn,
    InboundAckOut,
    InboundMessageIn,
    InboundMessageOut,
    InboundWorkItem,
    OpenAIConfigPublic,
    ActiveProviderSecret,
    SystemModelUpdateIn,
    DreamRunIn,
    DreamRunOut,
    OpenAIConfigUpdateIn,
    ProviderModelsIn,
    ProviderUpdateIn,
    ActiveProviderIn,
    FallbacksUpdateIn,
    NodeFallbacksUpdateIn,
    PolicyUpdateIn,
    QqQuickLoginIn,
    OpRequestIn,
    OpRequestOut,
    OutboundMessageIn,
    OutboundMessageOut,
    RestartIn,
    RestartOut,
    Task,
    TaskCompleteIn,
    TaskKind,
    TaskStatus,
)
from .admin_api import create_admin_router
from .admin_api import init_admin_token, verify_admin_token
from .napcat import NapCatClient, NapCatError, NapCatUnreachable


log = logging.getLogger(__name__)

_attachment_policy_cache: dict[str, Any] = {}


def _load_attachment_policy() -> Any:
    """按文件路径加载纯策略模块：import adapters.onebot 会触发 NoneBot，supervisor 进程里没有。"""
    import importlib.util

    path = (Path(__file__).resolve().parents[1] / "adapters" / "onebot" / "attachment_policy.py")
    key = str(path)
    cached = _attachment_policy_cache.get(key)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(f"_clonoth_attachment_policy_{abs(hash(key))}", path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"attachment policy not found at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _attachment_policy_cache[key] = module
    return module


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _audit_ops_caller(st: Any, request: Request, inp: Any) -> None:
    """观察期：只记录，不拦。

    /v1/ops/request 是全部策略判定的入口，而 is_admin 由调用方自报，端点本身不认凭证 ——
    能连上这个端口的进程都可以把 approval_required 换成 auto。改成拒绝之前先跑一段，
    确认日志里除了 engine 没有别的调用方，免得漏掉某条路径把工具调用全打死。

    不走 verify_admin_token：那个失败会进退避表，观察期不该有任何副作用。
    """
    from .admin_api import get_admin_token

    auth = request.headers.get("Authorization", "")
    presented = auth[7:].strip() if auth.startswith("Bearer ") else ""
    expected = get_admin_token()
    if presented and expected and secrets.compare_digest(presented, expected):
        return
    try:
        st.eventlog.append(
            session_id="__system__",
            component="supervisor",
            type_="ops_request_unauthenticated",
            # 只记有没有带、带了多长，不记值本身。
            payload={
                "op": str(getattr(inp, "op", "") or ""),
                "node_id": str(getattr(inp, "node_id", "") or ""),
                "session_id": str(getattr(inp, "session_id", "") or ""),
                "token_presented": bool(presented),
                "token_len": len(presented),
                "client": getattr(getattr(request, "client", None), "host", "") or "",
                "ts": _now().isoformat(),
            },
        )
    except Exception:
        pass  # 观察用的旁路，记不上不能影响工具执行


def _verify_channel_ref(cs: ConfigStore, name: str) -> None:
    """provider 字段可以是已配渠道名，也可以是裸线格式名。都不是就当场拦。

    放过去的话 engine 会静默回退成 openai 格式，拿着别家的 url+key 发出去，
    只看得到一个没头没尾的 400。渠道名区分大小写，线格式名不区分。
    """
    wanted = (name or "").strip()
    if not wanted:
        return
    from providers import registry as provider_registry
    channels = cs.channel_names()
    if wanted in channels or wanted.lower() in provider_registry.list():
        return
    raise HTTPException(
        status_code=400,
        detail=f"不认识的渠道 '{wanted}'。已配渠道：{channels}；线格式：{provider_registry.list()}",
    )


# [WS events 2026-05-17] Why: WebSocket clients should keep long-lived event
# streams through proxies. How: send an application-level ping at this cadence
# when no EventLog row is available. Purpose: avoid idle timeout without changing
# the EventLog schema.
_WS_HEARTBEAT_SEC = 30.0

# [2026-06-03] Why: clients may send an optional initial message for backward
# compat; the server consumes and ignores it. How: brief timeout, then proceed.
_WS_INITIAL_MESSAGE_TIMEOUT_SEC = 0.5

_WS_MAX_EVENT_BYTES = 65_536  # 64 KiB soft cap for individual WS events


async def _send_ws_json(websocket: WebSocket, payload: dict[str, Any]) -> None:
    """Send one JSON object over a WebSocket as UTF-8 text.

    [2026-06-03] Why: tool_call_end events from read_file can exceed 100 KiB,
    causing browsers to close the socket with code 1009 (Message Too Big).
    How: pre-serialize, check size, and truncate large result payloads before
    sending. Purpose: keep the WS stream alive for all clients."""
    # [WS events 2026-05-17] Why: EventLog payloads are plain dicts but may later
    # contain values FastAPI's send_json cannot serialize by default. How: use the
    # same explicit json.dumps path for events and ping frames. Purpose: make the
    # wire shape predictable and resilient to harmless non-string values.
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) > _WS_MAX_EVENT_BYTES:
        # Truncate the result field in tool_call_end events to stay under the cap.
        inner = payload.get("payload")
        if isinstance(inner, dict) and "result" in inner:
            inner["result"] = str(inner["result"])[:2000] + "... [truncated for WS]"
            text = json.dumps(payload, ensure_ascii=False, default=str)
    await websocket.send_text(text)


def create_app(
    *,
    state: SupervisorState,
    process_manager: ProcessManager | None,
    config_store: ConfigStore,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> FastAPI:
    app = FastAPI(title="Clonoth Supervisor", version="0.1.0")
    app.state.state = state
    app.state.process_manager = process_manager
    app.state.config_store = config_store
    app.state.napcat = NapCatClient(state.workspace_root)

    @app.get("/v1/health", response_model=HealthOut)
    async def health() -> HealthOut:
        st: SupervisorState = app.state.state
        uptime = (_now() - st.started_at).total_seconds()
        return HealthOut(
            run_id=st.eventlog.run_id, started_at=st.started_at,
            uptime_seconds=uptime,
            workspace_root=str(st.workspace_root),
        )

    @app.get("/v1/config", response_model=AppConfigPublic)
    async def get_config() -> AppConfigPublic:
        cs: ConfigStore = app.state.config_store
        return cs.get_public()

    @app.get("/v1/config/openai", response_model=OpenAIConfigPublic)
    async def get_openai_config_public() -> OpenAIConfigPublic:
        cs: ConfigStore = app.state.config_store
        return cs.get_openai_public()

    @app.get("/v1/config/openai/secret", response_model=ActiveProviderSecret)
    async def get_openai_config_secret(request: Request) -> ActiveProviderSecret:
        verify_admin_token(request)
        cs: ConfigStore = app.state.config_store
        return cs.get_openai_secret()

    @app.post("/v1/config/openai", response_model=AppConfigPublic)
    async def update_openai_config(body: OpenAIConfigUpdateIn, request: Request) -> AppConfigPublic:
        verify_admin_token(request)
        cs: ConfigStore = app.state.config_store
        st: SupervisorState = app.state.state

        out = cs.update_openai(body)
        st.eventlog.append(
            session_id="__system__",
            component="supervisor",
            type_="config_updated",
            payload={
                "provider": out.provider,
                "openai": out.openai.model_dump(mode="json"),
                "ts": _now().isoformat(),
            },
        )
        return out

    @app.post("/v1/config/reload", response_model=ConfigReloadOut)
    async def reload_config(request: Request) -> ConfigReloadOut:
        verify_admin_token(request)
        cs: ConfigStore = app.state.config_store
        st: SupervisorState = app.state.state

        cs.reload()
        out = cs.get_public()
        st.eventlog.append(
            session_id="__system__",
            component="supervisor",
            type_="config_reloaded",
            payload={"ts": _now().isoformat()},
        )
        return ConfigReloadOut(ok=True, config=out)

    # ================================================================
    #  Multi-provider config API
    # ================================================================

    @app.get("/v1/config/providers")
    async def get_providers(request: Request) -> dict[str, Any]:
        verify_admin_token(request)
        cs: ConfigStore = app.state.config_store
        from providers import registry as provider_registry
        result = cs.get_providers_public()
        result["registered"] = provider_registry.list()
        return result

    @app.get("/v1/config/policy")
    async def get_policy(request: Request) -> dict[str, Any]:
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        return {"policy": st.policy.export_config()}

    @app.put("/v1/config/policy")
    async def put_policy(body: PolicyUpdateIn, request: Request) -> dict[str, Any]:
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        try:
            saved = st.policy.replace_config(body.policy)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # 策略是整套权限判定的根。改了什么必须能事后查，否则出事时无从复盘。
        st.eventlog.append(
            session_id="",
            component="supervisor",
            type_="policy_updated",
            payload={
                "read_rules": len((saved.get("read_file") or {}).get("rules") or []),
                "write_rules": len((saved.get("write_file") or {}).get("rules") or []),
                "command_default": (saved.get("execute_command") or {}).get("default", ""),
                "policy": saved,
            },
        )
        return {"policy": saved}

    @app.get("/v1/qq/account")
    async def get_qq_account(request: Request) -> dict[str, Any]:
        verify_admin_token(request)
        client: NapCatClient = app.state.napcat
        if not client.configured:
            return {"configured": False}
        try:
            return {"configured": True, "reachable": True, **await client.account()}
        except NapCatUnreachable as exc:
            # 换号重启和首次部署都会经过这里。判成 502 的话，页面上什么都渲染不出来，
            # 连「正在重启」都说不了 —— 而这恰恰是最需要告诉人的时候。
            return {"configured": True, "reachable": False, "reason": str(exc)}
        except NapCatError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/v1/qq/account/quick-login")
    async def qq_quick_login(body: QqQuickLoginIn, request: Request) -> dict[str, Any]:
        verify_admin_token(request)
        client: NapCatClient = app.state.napcat
        uin = body.uin.strip()
        if not uin.isdigit():
            raise HTTPException(status_code=400, detail="QQ 号必须是数字")
        try:
            await client.call("SetQuickLogin", {"uin": uin})
        except NapCatUnreachable as exc:
            # 容器没起来跟这个号本身没关系，标死了下次就再也点不动。
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except NapCatError as exc:
            # QQ 明确拒了才算死号：缓存的登录态过期后 isQuickLogin 仍报 true，
            # 不记一笔的话这个号会永远停在列表里，点一次失败一次。
            client.mark_login_dead(uin, str(exc))
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        # 号已经切过去了，钉自动登录只是锦上添花：这一步失败不该让人以为没切成。
        pinned = True
        pin_error = ""
        try:
            await client.set_auto_login(uin)
        except NapCatError as exc:
            pinned = False
            pin_error = str(exc)
        st: SupervisorState = app.state.state
        # 换号等于换掉 bot 的身份，出事时必须查得到是谁在什么时候换的。
        st.eventlog.append(
            session_id="",
            component="supervisor",
            type_="qq_account_switched",
            payload={"uin": uin, "pinned": pinned},
        )
        return {"ok": True, "uin": uin, "pinned": pinned, "pin_error": pin_error}

    @app.post("/v1/qq/account/qrcode")
    async def qq_login_qrcode(request: Request) -> dict[str, Any]:
        verify_admin_token(request)
        client: NapCatClient = app.state.napcat
        try:
            data = await client.call("GetQQLoginQrcode")
        except NapCatUnreachable as exc:
            return {"qrcode": "", "reachable": False, "reason": str(exc)}
        except NapCatError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        payload = data if isinstance(data, dict) else {}
        return {
            "qrcode": str(payload.get("qrcode") or payload.get("qrcodeurl") or ""),
            "reachable": True,
        }

    @app.post("/v1/qq/account/relogin")
    async def qq_relogin(request: Request) -> dict[str, Any]:
        verify_admin_token(request)
        client: NapCatClient = app.state.napcat
        st: SupervisorState = app.state.state
        # NapCat 在已登录状态下拒发二维码，且不提供登出，只能重启进等扫码状态。
        st.eventlog.append(
            session_id="",
            component="supervisor",
            type_="qq_account_relogin_requested",
            payload={},
        )
        try:
            await client.enter_login_mode()
        except NapCatError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/v1/qq/account/pin")
    async def qq_pin_account(request: Request) -> dict[str, Any]:
        verify_admin_token(request)
        client: NapCatClient = app.state.napcat
        try:
            info = await client.account()
            uin = str(info.get("uin") or "")
            if not uin:
                raise HTTPException(status_code=409, detail="当前没有登录任何账号")
            await client.set_auto_login(uin)
        except NapCatError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True, "uin": uin}

    @app.get("/v1/config/system-models")
    async def get_system_models(request: Request) -> dict[str, Any]:
        verify_admin_token(request)
        cs: ConfigStore = app.state.config_store
        return cs.get_system_models_public()

    @app.put("/v1/config/system-models/{slot}")
    async def put_system_model(slot: str, body: SystemModelUpdateIn, request: Request) -> dict[str, Any]:
        verify_admin_token(request)
        cs: ConfigStore = app.state.config_store
        _verify_channel_ref(cs, body.provider or "")
        try:
            return cs.update_system_model(
                slot, base_url=body.base_url, api_key=body.api_key,
                model=body.model, provider=body.provider,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/v1/config/image-default-channel")
    async def put_image_default_channel(
        body: ImageDefaultChannelIn, request: Request,
    ) -> dict[str, Any]:
        verify_admin_token(request)
        cs: ConfigStore = app.state.config_store
        try:
            return cs.set_image_default_channel(body.default_channel)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/v1/config/provider-options")
    async def get_provider_options(request: Request) -> dict[str, Any]:
        """每个 provider 支持哪些可调参数、发哪种请求格式、官方域名是什么。

        界面照这份清单渲染并判断「地址和渠道对不上」，不自己写一遍。
        """
        verify_admin_token(request)
        from providers import registry as provider_registry
        return {
            "options": provider_registry.options_catalog(),
            "wire_formats": provider_registry.wire_formats(),
            "host_profiles": provider_registry.host_profiles(),
            "default_vision": provider_registry.default_vision_support(),
        }

    @app.put("/v1/config/providers/{name}")
    async def upsert_provider(name: str, body: ProviderUpdateIn, request: Request) -> dict[str, Any]:
        verify_admin_token(request)
        from providers import registry as provider_registry
        cs: ConfigStore = app.state.config_store
        # 渠道名随便起，同一家可以开好几个；被校验的是它说自己走哪种线格式。
        wire = (body.type or "").strip().lower() or cs.wire_of(name)
        if wire not in provider_registry.list():
            raise HTTPException(
                status_code=400,
                detail=f"不认识的线格式 '{wire}'。可选：{provider_registry.list()}",
            )
        return cs.upsert_provider(
            name, base_url=body.base_url, api_key=body.api_key, model=body.model,
            supports_vision=body.supports_vision, wire=body.type, label=body.label,
        )

    @app.post("/v1/config/providers/{name}/models")
    async def list_provider_models(
        name: str, body: ProviderModelsIn, request: Request,
    ) -> dict[str, Any]:
        """问上游这个渠道有哪些模型。密钥只出不进：不回显、不进错误信息。"""
        verify_admin_token(request)
        import httpx
        from providers import registry as provider_registry

        cs: ConfigStore = app.state.config_store
        # 列模型的接口按家族走，名字是用户自己起的，先换成线格式再查。
        cls = provider_registry.get(cs.wire_of(name))
        if cls is None:
            raise HTTPException(status_code=400, detail=f"没有叫 '{name}' 的渠道")
        stored_url, stored_key = cs.resolve_provider_credentials(name)
        if body.slot:
            # 槽位自己配的那份优先：它才是这个页面在编辑的东西，渠道块只是它的兜底。
            slot_url, slot_key = cs.resolve_slot_credentials(body.slot.strip())
            stored_url = slot_url or stored_url
            stored_key = slot_key or stored_key
        # 页面上清空了地址是「用默认」，没传这个字段才是「沿用已存的」。
        base_url = stored_url if body.base_url is None else body.base_url.strip()
        api_key = (body.api_key or "").strip() or stored_key
        if not api_key:
            raise HTTPException(status_code=400, detail="这个渠道还没有密钥，先填上再拉取")
        target = cls.catalog_request(base_url=base_url, api_key=api_key)
        if target is None:
            raise HTTPException(status_code=400, detail=f"{name} 没有列模型的接口，模型名请手填")
        url, headers = target
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, headers=headers)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"连不上 {url}：{exc}") from exc
        if resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"上游返回 {resp.status_code}：{resp.text[:200]}",
            )
        try:
            payload = resp.json()
        except Exception as exc:
            raise HTTPException(status_code=502, detail="上游返回的不是 JSON") from exc
        models = cls.parse_catalog(payload)
        if not models:
            # 空列表当成功回去，界面会显示成「这家没有模型」，比报错更难查。
            raise HTTPException(status_code=502, detail="上游没有返回任何模型")
        return {"models": sorted(set(models))}

    @app.delete("/v1/config/providers/{name}")
    async def delete_provider(name: str, request: Request) -> dict[str, Any]:
        verify_admin_token(request)
        cs: ConfigStore = app.state.config_store
        try:
            return cs.delete_provider(name)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.put("/v1/config/active-provider")
    async def set_active_provider(body: ActiveProviderIn, request: Request) -> dict[str, Any]:
        verify_admin_token(request)
        cs: ConfigStore = app.state.config_store
        try:
            return cs.set_active_provider(body.provider)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.put("/v1/config/fallbacks")
    async def update_fallbacks(body: FallbacksUpdateIn, request: Request) -> dict[str, Any]:
        verify_admin_token(request)
        cs: ConfigStore = app.state.config_store
        return cs.update_fallbacks(body.fallbacks)

    @app.put("/v1/config/node-fallbacks/{node_id}")
    async def update_node_fallbacks(
        node_id: str, body: NodeFallbacksUpdateIn, request: Request,
    ) -> dict[str, Any]:
        verify_admin_token(request)
        cs: ConfigStore = app.state.config_store
        try:
            return cs.update_node_fallbacks(node_id, body.fallbacks)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.delete("/v1/config/node-fallbacks/{node_id}")
    async def clear_node_fallbacks(node_id: str, request: Request) -> dict[str, Any]:
        """删掉专属链，让这个节点回退到全局链。空列表是「禁用」，与此不同。"""
        verify_admin_token(request)
        cs: ConfigStore = app.state.config_store
        try:
            return cs.update_node_fallbacks(node_id, None)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/v1/attachments/upload")
    async def upload_attachment(
        request: Request,
        file: UploadFile = File(...),
        conversation_key: str = Query("default"),
    ) -> dict[str, Any]:
        """Upload a file attachment. Returns path relative to workspace root."""
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        safe_key = conversation_key.replace(":", "_").replace("/", "_").replace("..", "_")
        att_root = (st.workspace_root / "data" / "attachments").resolve()
        att_dir = (att_root / safe_key).resolve()
        # 反斜杠在 Windows 上仍是路径分隔符，safe_key 的字符替换挡不住穿越，靠 resolve 兜底。
        if att_dir != att_root and att_root not in att_dir.parents:
            raise HTTPException(status_code=400, detail="Invalid conversation_key")
        att_dir.mkdir(parents=True, exist_ok=True)

        ext = Path(file.filename or "file").suffix or ""
        unique_name = f"{int(time.time())}_{os.urandom(6).hex()}{ext}"
        save_path = att_dir / unique_name

        content = await file.read()
        if len(content) > 50 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="File too large (max 50MB)")
        # 只挡「永远不该收」的可执行/脚本宿主：通用上传端点不套 QQ 入站白名单，否则会误拒 Discord/Web 的正当上传。
        policy = _load_attachment_policy()
        reject = policy.blocked_file_reject_reason(file.filename or unique_name, content)
        if reject:
            raise HTTPException(status_code=415, detail=f"Rejected attachment: {reject}")
        save_path.write_bytes(content)

        # as_posix：Windows 上不转就是反斜杠，拼进 URL 直接取不到。
        rel_path = save_path.relative_to(st.workspace_root).as_posix()
        mime_type = file.content_type or "application/octet-stream"
        return {
            "path": rel_path,
            "name": file.filename or unique_name,
            "size": len(content),
            "mime_type": mime_type,
            "type": "image" if mime_type.startswith("image/") else "file",
        }

    @app.get("/v1/files/{rel:path}")
    async def read_attachment(rel: str, request: Request) -> FileResponse:
        """按工作区相对路径取附件。

        只开放 data/attachments 子树。token 走 query 也认（见 verify_admin_token）——
        img 标签带不了 Authorization 头。
        """
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        root = (st.workspace_root / "data" / "attachments").resolve()
        target = (st.workspace_root / rel).resolve()
        # resolve 之后再比：'..'、绝对路径和符号链接都会在这一步现形。
        if root not in target.parents or not target.is_file():
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(target)

    @app.post("/v1/inbound", response_model=InboundMessageOut)
    async def inbound(msg: InboundMessageIn, request: Request) -> InboundMessageOut:
        # platform_auth 是整套鉴权的源头，这里不校验就等于谁都能声明自己是管理员。
        # adapter、engine、cli、tui 四个调用方本来就都带令牌。
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        session_id = st.get_or_create_session(channel=msg.channel, conversation_key=msg.conversation_key)

        # [2026-05-28] 异步 dispatch 统一走 inbound：透传新增的 dispatch 字段到 payload。
        # 为什么：model_dump() 已包含这些字段，但 record_inbound_message_event 依赖
        #   payload dict 来传递给 _create_entry_task_for_inbound_locked。
        # 怎么改：无需额外处理，Pydantic model_dump 已包含新字段。
        # 目的：确保 dispatch_origin/dispatch_context_mode/dispatch_fork_from_session
        #   能通过 event payload 传递到 task 创建逻辑。
        evt = st.eventlog.append(
            session_id=session_id,
            component="shell",
            type_="inbound_message",
            payload=msg.model_dump(),
        )
        st.record_inbound_message_event(evt)
        inbound_seq = int(evt.get("seq", 0) or 0)
        return InboundMessageOut(session_id=session_id, inbound_seq=inbound_seq, accepted=True)

    @app.get("/v1/inbound/next", response_model=InboundWorkItem)
    async def inbound_next(
        worker_id: str = Query(..., min_length=1),
        lease_sec: float = Query(30.0, ge=1.0, le=600.0),
    ) -> InboundWorkItem:
        st: SupervisorState = app.state.state
        st.mark_engine_seen(worker_id=worker_id)
        item = st.assign_next_inbound(worker_id=worker_id, lease_sec=float(lease_sec))
        if item is None:
            return Response(status_code=204)  # type: ignore[return-value]
        return InboundWorkItem.model_validate(item)

    @app.post("/v1/inbound/{inbound_seq}/ack", response_model=InboundAckOut)
    async def inbound_ack(inbound_seq: int, body: InboundAckIn) -> InboundAckOut:
        st: SupervisorState = app.state.state
        ok = st.ack_inbound(inbound_seq=int(inbound_seq), worker_id=body.worker_id)
        if not ok:
            raise HTTPException(status_code=404, detail="inbound item not found")
        return InboundAckOut(ok=True)

    @app.get("/v1/tasks/next", response_model=Task)
    async def task_next(
        worker_id: str = Query(..., min_length=1),
        lease_sec: float = Query(120.0, ge=1.0, le=3600.0),
    ) -> Task:
        st: SupervisorState = app.state.state
        st.mark_engine_seen(worker_id=worker_id)
        item = st.assign_next_task(worker_id=worker_id, lease_sec=float(lease_sec))
        if item is None:
            return Response(status_code=204)  # type: ignore[return-value]
        return Task.model_validate(item)

    @app.post("/v1/tasks/{task_id}/complete")
    async def task_complete(task_id: str, body: TaskCompleteIn) -> dict[str, Any]:
        st: SupervisorState = app.state.state
        task = st.complete_task(task_id=task_id, worker_id=body.worker_id, result=dict(body.result or {}))
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return {"ok": True, "task_id": task.task_id, "status": task.status.value}

    @app.get("/v1/tasks/{task_id}/cancelled")
    async def task_cancelled(task_id: str) -> dict[str, Any]:
        st: SupervisorState = app.state.state
        return {"cancelled": st.is_task_cancelled(task_id)}

    @app.post("/v1/tasks/{task_id}/preempt")
    async def task_preempt(task_id: str, request: Request) -> dict[str, Any]:
        """Bot 调用：标记单个 task 为 preempt_requested。"""
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        msg = body.get("message", "")
        atts = body.get("attachments", [])
        st: SupervisorState = app.state.state
        ok = st.preempt_task(task_id, message=msg, attachments=atts)
        if not ok:
            raise HTTPException(status_code=404, detail="task not found or not active")
        return {"ok": True, "task_id": task_id}

    @app.get("/v1/tasks/{task_id}/preempted")
    async def task_preempted(task_id: str) -> dict[str, Any]:
        """Engine 查询：task 是否被请求 preempt。"""
        st: SupervisorState = app.state.state
        return st.is_task_preempted(task_id)

    @app.post("/v1/tasks/{task_id}/preempt_consumed")
    async def task_preempt_consumed(task_id: str) -> dict[str, Any]:
        """Engine 读取完 preempt message 后调用，清空 message 防止重复注入。"""
        st: SupervisorState = app.state.state
        result = st.consume_preempt_message(task_id)
        return {"ok": True, **result}

    @app.post("/v1/sessions/{session_id}/async_tool_result")
    async def session_async_tool_result(session_id: str, request: Request) -> dict[str, Any]:
        """Engine 调用：异步工具完成后注入结果到 session。

        复用子节点三级回退：preempt running → 标记 suspended → 创建 inbound。
        """
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        msg = body.get("message", "")
        atts = body.get("attachment_paths", [])
        st: SupervisorState = app.state.state
        if session_id not in st.sessions:
            raise HTTPException(status_code=404, detail="session not found")
        result = st.inject_async_result(
            session_id,
            text=msg,
            attachments=atts,
            node_id=str(body.get("node_id") or ""),
            task_id=str(body.get("task_id") or ""),
            tool_name=str(body.get("tool_name") or ""),
            success=body.get("success") if isinstance(body.get("success"), bool) else None,
            error=str(body.get("error") or ""),
        )
        if not result.get("ok"):
            raise HTTPException(status_code=500, detail=result.get("error", "unknown"))
        return result

    @app.get("/v1/sessions/{session_id}/running_tasks")
    async def session_running_tasks(session_id: str) -> dict[str, Any]:
        """Bot 查询当前 session 中 running/pending 状态的 task 列表。
        自动收割 lease 过期超过 grace period 的僵尸 task。
        跳过 session 中存在 pending approval 的 task（等审批不算僵尸）。"""
        st: SupervisorState = app.state.state
        if session_id not in st.sessions:
            raise HTTPException(status_code=404, detail="session not found")
        now = _now()
        _GRACE = timedelta(seconds=180)
        tasks: list[dict[str, Any]] = []
        with st._lock:
            # [Fork/Merge 2026-05-12] running_tasks 查询主 session 时也返回入口分支任务。
            # 原因：adapter 以后需要在多个并发 branch 中选择显式 preempt 目标。
            # 做法：把主 session 与 parent→branches 索引合并为查询集合。
            # 目的：端点仍以主 session_id 调用，但能观察所有活跃分支。
            session_ids = {session_id, *st._entry_branch_ids_for_parent_locked(session_id)}
            # 检查该 session 或任一分支是否有 pending approval
            _has_pending_approval = any(
                a.status == ApprovalStatus.pending and a.session_id in session_ids
                for a in st.approvals.values()
            )
            for task in st.tasks.values():
                if task.session_id not in session_ids:
                    continue
                if task.status not in (TaskStatus.running, TaskStatus.pending):
                    continue
                # 收割僵尸：running + lease 过期超过 grace period
                # 但如果 session 有 pending approval，跳过回收（等审批是合法阻塞）
                # fix: lease_expires_at 为 None 时也视为僵尸，避免无 lease 的 running 任务永远无法被收割
                if (task.status == TaskStatus.running
                        and (not task.lease_expires_at or task.lease_expires_at + _GRACE < now)
                        and not _has_pending_approval):
                    task.status = TaskStatus.failed
                    task.updated_at = now
                    task.lease_expires_at = None
                    task.result = {"action": "fail", "error": "lease expired (zombie reaped)"}
                    st._reset_task_route_state_locked(task)
                    # 写事件，使 events.jsonl 与内存状态一致
                    st.eventlog.append(
                        session_id=task.session_id,
                        component="supervisor",
                        type_="task_completed",
                        payload=task.model_dump(mode="json"),
                    )
                    # [Fork/Merge 2026-05-12] 僵尸回收是 fail 终态，也必须走统一路由。
                    # 原因：入口分支被回收时需要 merge 回主 session，并输出错误事件。
                    # 做法：复用 task_router 的 fail 路由。目的：避免 reaped branch 永久悬挂。
                    st._route_completed_task_locked(task)
                    continue
                _is_async = bool(task.input.get("_async_dispatch"))
                _is_system = bool(task.input.get("_system_task"))
                _is_scheduled = bool(task.input.get("schedule_id"))
                branch_session_id = str(task.input.get("branch_session_id") or "")
                if not branch_session_id and task.session_id != session_id:
                    branch_session_id = task.session_id
                tasks.append({
                    "task_id": task.task_id,
                    "node_id": task.node_id or "",
                    "status": task.status.value,
                    "created_at": task.created_at.isoformat() if task.created_at else "",
                    "caller_task_id": task.caller_task_id or "",
                    "is_user_entry": bool(not task.caller_task_id and not _is_async and not _is_system and not _is_scheduled),
                    "source_inbound_seq": task.source_inbound_seq,
                    "branch_session_id": branch_session_id,
                    "parent_session_id": str(task.input.get("parent_session_id") or (session_id if branch_session_id else "")),
                })
        return {"tasks": tasks}

    # [2026-05-28] 全局按 node_id 查找活跃任务（跨 session）。
    # 为什么：dispatch 到持久节点的任务运行在独立 session 上，调用方不知道目标 session_id。
    # 怎么改：新增端点，遍历所有活跃 task，按 node_id 匹配返回第一个。
    # 目的：支持 preempt_task 跨 session 查找持久节点任务。
    @app.get("/v1/tasks/active-by-node/{node_id}")
    async def global_task_by_node(node_id: str) -> dict[str, Any]:
        """[2026-05-28] 全局查找指定 node_id 的活跃任务（running/pending）。

        遍历所有任务，不限定 session。用于跨 session 定位持久节点任务。
        """
        st: SupervisorState = app.state.state
        with st._lock:
            for task in st.tasks.values():
                if task.node_id != node_id:
                    continue
                if task.status not in (TaskStatus.running, TaskStatus.pending):
                    continue
                return {
                    "task_id": task.task_id,
                    "session_id": task.session_id,
                    "status": task.status.value,
                }
        raise HTTPException(status_code=404, detail=f"no active task for node '{node_id}'")

    # [2026-05-28] 按 node_id 查找 session 中活跃任务。
    # 为什么：preempt_task 原本只接受 task_id（UUID），调用者需知道精确 ID 才能操作。
    # 怎么改：新增端点，遍历 session 内所有活跃 task，按 node_id 匹配返回第一个。
    # 目的：允许 engine 侧用 node_id（如 "bob"）定位子节点任务再执行 preempt。
    @app.get("/v1/sessions/{session_id}/tasks/by-node/{node_id}")
    async def session_task_by_node(session_id: str, node_id: str) -> dict[str, Any]:
        """按 node_id 查找 session 中活跃（running/pending）的 task。"""
        st: SupervisorState = app.state.state
        if session_id not in st.sessions:
            raise HTTPException(status_code=404, detail="session not found")
        with st._lock:
            # 与 running_tasks 端点一致，查询主 session 及其入口分支
            session_ids = {session_id, *st._entry_branch_ids_for_parent_locked(session_id)}
            for task in st.tasks.values():
                if task.session_id not in session_ids:
                    continue
                if task.node_id != node_id:
                    continue
                if task.status not in (TaskStatus.running, TaskStatus.pending):
                    continue
                return {"task_id": task.task_id, "status": task.status.value}
        raise HTTPException(status_code=404, detail=f"no active task for node '{node_id}'")

    @app.post("/v1/tasks/{task_id}/renew_lease")
    async def renew_lease(task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        st: SupervisorState = app.state.state
        worker_id = str(body.get("worker_id") or "").strip()
        lease_sec = float(body.get("lease_sec", 120.0))
        ok = st.renew_lease(task_id, worker_id, lease_sec)
        return {"ok": ok}

    @app.post("/v1/engine/register")
    async def engine_register(body: dict[str, Any]) -> dict[str, Any]:
        """Engine worker registers itself with a generation ID on startup.

        Direction 2: triggers cleanup of orphaned tasks from previous generations.
        Direction 2: triggers cleanup of orphaned tasks from previous generations.
        """
        st: SupervisorState = app.state.state
        worker_id = str(body.get("worker_id") or "").strip()
        generation_id = str(body.get("generation_id") or "").strip()
        if not worker_id or not generation_id:
            raise HTTPException(status_code=400, detail="worker_id and generation_id required")
        result = st.register_engine(worker_id, generation_id)
        return result

    @app.get("/v1/tools/reload-seq")
    async def tools_reload_seq() -> dict[str, Any]:
        st: SupervisorState = app.state.state
        return {"seq": st.tools_reload_seq()}

    @app.post("/v1/tools/reload")
    async def tools_reload_trigger() -> dict[str, Any]:
        st: SupervisorState = app.state.state
        seq = st.bump_tools_reload()
        return {"ok": True, "seq": seq}

    @app.post("/v1/memory/dream/run", response_model=DreamRunOut)
    async def run_dream_now(body: DreamRunIn, request: Request) -> DreamRunOut:
        verify_admin_token(request)
        st: SupervisorState = app.state.state

        notify_session_id = ""
        key = str(body.notify_conversation_key or "").strip()
        if key:
            channel = str(body.notify_channel or "").strip()
            if not channel:
                channel = key.split(":", 1)[0] if ":" in key else "system"
            notify_session_id = st.get_or_create_session(channel=channel, conversation_key=key)

        outcome = st.fire_manual_schedule("dream", notify_session_id=notify_session_id)
        status = str(outcome.get("status") or "unavailable")
        return DreamRunOut(ok=status == "started", status=status)

    @app.post("/v1/sessions/{session_id}/outbound", response_model=OutboundMessageOut)
    async def session_outbound(session_id: str, body: OutboundMessageIn) -> OutboundMessageOut:
        st: SupervisorState = app.state.state
        try:
            st.append_outbound_message(
                session_id=session_id,
                text=str(body.text or ""),
                attachments=body.attachments,
                source_inbound_seq=body.source_inbound_seq,
                llm_request_id=body.llm_request_id,
                delivery_id=body.delivery_id,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="session not found")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e) or "bad request")
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e) or "conflict")
        return OutboundMessageOut(ok=True)

    @app.get("/v1/sessions/{session_id}/events", response_model=list[Event])
    async def session_events(
        session_id: str,
        after_seq: int = Query(0, ge=0),
        limit: int = Query(5000, ge=1, le=5000),
    ) -> list[Event]:
        st: SupervisorState = app.state.state
        evts = st.list_events(session_id=session_id, after_seq=after_seq)
        out: list[Event] = []
        for e in evts:
            # Why: session event payloads can include large task snapshots. How:
            # stop conversion once the requested page size is reached. Purpose:
            # keep per-session polling from serializing the whole memory window.
            if len(out) >= limit:
                break
            try:
                out.append(
                    Event(
                        schema_version=int(e.get("schema_version", 1)),
                        seq=int(e.get("seq", 0)),
                        event_id=str(e.get("event_id")),
                        ts=datetime.fromisoformat(str(e.get("ts"))),
                        run_id=str(e.get("run_id")),
                        session_id=str(e.get("session_id")),
                        component=str(e.get("component")),
                        type=str(e.get("type")),
                        payload=dict(e.get("payload") or {}),
                    )
                )
            except Exception:
                continue
        return out

    @app.websocket("/v1/sessions/{session_id}/ws")
    async def session_ws(websocket: WebSocket, session_id: str) -> None:
        """Stream durable EventLog rows for one session over WebSocket."""
        st: SupervisorState = app.state.state
        if session_id not in st.sessions:
            await websocket.close(code=4004, reason="session not found")
            return
        await websocket.accept()

        # [2026-06-03] Why: replay is removed. WS is a pure live-forward stream.
        # Clients rebuild historical state via GET /v1/sessions/{id}/history.
        # How: consume the optional initial message for backward compat, then go
        # straight to the live event loop. Purpose: eliminate catch-up replay that
        # caused stale events (e.g. old approval_requested) to be re-delivered.
        try:
            await asyncio.wait_for(
                websocket.receive_text(),
                timeout=_WS_INITIAL_MESSAGE_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            pass
        except WebSocketDisconnect:
            return
        except Exception:
            pass

        queue = st.eventlog.subscribe(session_id)
        sent_seq = 0
        receive_task: asyncio.Task | None = None
        try:
            receive_task = asyncio.create_task(websocket.receive_text())
            while True:
                event_task = asyncio.create_task(queue.get())
                done, _pending = await asyncio.wait(
                    {event_task, receive_task},
                    timeout=_WS_HEARTBEAT_SEC,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if not done:
                    event_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await event_task
                    await _send_ws_json(websocket, {"type": "ping"})
                    continue

                if event_task in done:
                    evt = event_task.result()
                    try:
                        evt_seq = int(evt.get("seq", 0) or 0)
                    except Exception:
                        evt_seq = 0
                    if evt_seq > sent_seq:
                        await _send_ws_json(websocket, evt)
                        sent_seq = evt_seq
                else:
                    event_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await event_task

                if receive_task in done:
                    try:
                        receive_task.result()
                    except WebSocketDisconnect:
                        break
                    except Exception:
                        break
                    # [WS events 2026-05-17] Why: clients may send harmless control
                    # frames after the initial last_seq. How: consume and ignore the
                    # text, then wait for the next client frame. Purpose: a normal
                    # client message does not terminate the event stream.
                    receive_task = asyncio.create_task(websocket.receive_text())
        except WebSocketDisconnect:
            pass
        finally:
            if receive_task is not None and not receive_task.done():
                receive_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await receive_task
            elif receive_task is not None and receive_task.done():
                with contextlib.suppress(Exception):
                    receive_task.result()
            st.eventlog.unsubscribe(session_id, queue)

    @app.websocket("/v1/ws")
    async def global_ws(websocket: WebSocket) -> None:
        """Stream durable EventLog rows for all sessions over WebSocket."""
        st: SupervisorState = app.state.state
        await websocket.accept()

        # [2026-06-03] Why: replay is removed. WS is a pure live-forward stream.
        # Web frontend rebuilds state via loadSessionHistoryIntoStore(); SDK uses
        # _init_seq() to fast-forward before connecting. No client depends on WS
        # catch-up replay. How: consume the optional initial message for backward
        # compat, then go straight to the live loop. Purpose: eliminate full-history
        # replay that caused 109KB tool_call_end events to blow up browsers and
        # stale approval_requested events to trigger 404 auto-approve errors.
        try:
            await asyncio.wait_for(
                websocket.receive_text(),
                timeout=_WS_INITIAL_MESSAGE_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            pass
        except WebSocketDisconnect:
            return
        except Exception:
            pass

        queue = st.eventlog.subscribe_global()
        sent_seq = 0
        receive_task: asyncio.Task | None = None
        try:
            receive_task = asyncio.create_task(websocket.receive_text())
            while True:
                event_task = asyncio.create_task(queue.get())
                done, _pending = await asyncio.wait(
                    {event_task, receive_task},
                    timeout=_WS_HEARTBEAT_SEC,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if not done:
                    event_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await event_task
                    await _send_ws_json(websocket, {"type": "ping"})
                    continue

                if event_task in done:
                    evt = event_task.result()
                    try:
                        evt_seq = int(evt.get("seq", 0) or 0)
                    except Exception:
                        evt_seq = 0
                    if evt_seq > sent_seq:
                        await _send_ws_json(websocket, evt)
                        sent_seq = evt_seq
                else:
                    event_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await event_task

                if receive_task in done:
                    try:
                        receive_task.result()
                    except WebSocketDisconnect:
                        break
                    except Exception:
                        break
                    # [WS events 2026-05-19] Why: clients may send control frames
                    # after the initial last_seq. How: consume and ignore each text
                    # frame, then wait for another. Purpose: keep global streaming
                    # behavior aligned with the per-session WebSocket endpoint.
                    receive_task = asyncio.create_task(websocket.receive_text())
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            if receive_task is not None and not receive_task.done():
                receive_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await receive_task
            elif receive_task is not None and receive_task.done():
                # [2026-06-03] Why: a finished receive_task whose exception was never
                # retrieved causes 'Task exception was never retrieved' warnings.
                # How: consume the result/exception so Python GC does not warn.
                # Purpose: clean shutdown of the global WS without log noise.
                with contextlib.suppress(Exception):
                    receive_task.result()
            st.eventlog.unsubscribe_global(queue)

    @app.get("/v1/events", response_model=list[Event])
    async def global_events(
        after_seq: int = Query(0, ge=0),
        types: str = Query("", description="comma-separated event types to filter"),
        limit: int = Query(5000, ge=1, le=5000),
    ) -> list[Event]:
        st: SupervisorState = app.state.state
        evts = st.eventlog.list_all_events(after_seq=after_seq)
        type_filter = {t.strip() for t in types.split(",") if t.strip()} if types else set()
        out: list[Event] = []
        for e in evts:
            if type_filter and str(e.get("type")) not in type_filter:
                continue
            if len(out) >= limit:
                break
            try:
                # Why: /v1/events may contain large task snapshots even with the
                # in-memory ring bounded. How: honor the explicit page limit
                # during conversion instead of materializing every cached Event.
                # Purpose: prevent polling/debug requests from serializing many
                # megabytes when the caller asks for a small page.
                out.append(Event(
                    schema_version=int(e.get("schema_version", 1)),
                    seq=int(e.get("seq", 0)),
                    event_id=str(e.get("event_id")),
                    ts=datetime.fromisoformat(str(e.get("ts"))),
                    run_id=str(e.get("run_id")),
                    session_id=str(e.get("session_id")),
                    component=str(e.get("component")),
                    type=str(e.get("type")),
                    payload=dict(e.get("payload") or {}),
                ))
            except Exception:
                continue
        return out

    @app.post("/v1/sessions/{session_id}/events")
    async def session_event(session_id: str, ev: HandoffEventIn) -> dict[str, Any]:
        st: SupervisorState = app.state.state
        if session_id not in st.sessions:
            raise HTTPException(status_code=404, detail="session not found")

        transient = ev.type in {"stream_delta", "stream_end", "tool_call_delta"}
        # [tool-stream 2026-05-19] tool_call_delta 是实时展示事件，不写入 JSONL。
        # 原因：参数片段可能很碎，持久化会膨胀事件日志且与 stream_delta 语义一致。
        # 做法：把它加入 supervisor transient 类型集合。
        # 目的：WebSocket 继续实时广播，但磁盘事件日志只保留稳定状态事件。
        if ev.type == "context_usage":
            transient = True
        evt = st.eventlog.append(
            session_id=session_id,
            component="shell",
            type_=ev.type,
            payload=dict(ev.payload or {}),
            transient=transient,
        )
        if ev.type == "outbound_message":
            st.record_outbound_message_event(evt)
        if ev.type == "context_usage":
            st.update_context_usage(session_id, dict(ev.payload or {}))
        # [AutoC 2026-06-03] Why: scheduler stale-task reaper checks task.updated_at
        # but the engine heartbeat (renew_lease) only refreshes lease_expires_at,
        # causing live tasks to be falsely reaped after 10 min of no state mutation.
        # How: extract task_id from event payload and touch updated_at on running
        # tasks. Purpose: any engine activity (stream_delta, tool events, replies)
        # resets the stale timer so only truly dead tasks get reaped.
        _evt_task_id = str((ev.payload or {}).get("task_id") or "").strip()
        if _evt_task_id:
            with st._lock:
                _evt_task = st.tasks.get(_evt_task_id)
                if _evt_task is not None and _evt_task.status == TaskStatus.running:
                    _evt_task.updated_at = datetime.now(timezone.utc)
                    # [AutoC 2026-06-04] Why: GET /v1/admin/tasks/active must
                    # return the current activity of each task without relying on
                    # EventLog or frontend WS inference. How: update current_phase
                    # and current_detail on the live Task object whenever a
                    # transient event arrives. Purpose: modal shows real-time
                    # task state on first open, not just after WS events.
                    _ev_type = ev.type
                    _payload = ev.payload or {}
                    if _ev_type == "stream_delta":
                        _evt_task.current_phase = "thinking" if _payload.get("type") == "thinking" else "generating"
                        _evt_task.current_detail = ""
                    elif _ev_type == "stream_end":
                        _evt_task.current_phase = ""
                        _evt_task.current_detail = ""
                    elif _ev_type == "tool_call_start":
                        _evt_task.current_phase = "tool_call"
                        _evt_task.current_detail = str(_payload.get("tool_name") or "")
                    elif _ev_type == "tool_call_end":
                        _evt_task.current_phase = ""
                        _evt_task.current_detail = ""
                    elif _ev_type == "approval_requested":
                        _evt_task.current_phase = "awaiting_approval"
                        _evt_task.current_detail = str(_payload.get("tool_name") or "")
                    elif _ev_type == "approval_decided":
                        _evt_task.current_phase = ""
                        _evt_task.current_detail = ""
        return {"ok": True}

    @app.get("/v1/sessions")
    async def list_sessions(
        channel: str = Query("", description="Filter by channel (e.g. 'web')"),
        limit: int = Query(50, ge=1, le=200),
    ) -> list[dict[str, Any]]:
        """List sessions, optionally filtered by channel."""
        st: SupervisorState = app.state.state
        results = []
        for sid, si in st.sessions.items():
            if channel and si.channel != channel:
                continue
            results.append({
                "session_id": si.session_id,
                "conversation_key": si.conversation_key,
                "channel": si.channel,
                "created_at": si.created_at.isoformat() if si.created_at else "",
                "updated_at": si.updated_at.isoformat() if si.updated_at else "",
            })
        # Sort by updated_at desc, most recent first
        results.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        return results[:limit]

    @app.post("/v1/sessions/get_or_create")
    async def get_or_create_session(
        request: Request,
        body: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """Get or create a session by channel + conversation_key. No task created."""
        st: SupervisorState = app.state.state
        channel = str(body.get("channel") or "").strip()
        conv_key = str(body.get("conversation_key") or "").strip()
        if not conv_key:
            raise HTTPException(status_code=400, detail="conversation_key is required")
        if not channel:
            channel = conv_key.split(":", 1)[0] if ":" in conv_key else "unknown"
        with st._lock:
            session_id = st.get_or_create_session(channel=channel, conversation_key=conv_key)
        return {"session_id": session_id, "conversation_key": conv_key}

    @app.delete("/v1/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, Any]:
        """Delete a session and its conversation store."""
        st: SupervisorState = app.state.state
        if session_id not in st.sessions:
            raise HTTPException(status_code=404, detail="session not found")
        si = st.sessions[session_id]
        # Remove from sessions and conversation_map
        with st._lock:
            del st.sessions[session_id]
            conv_key = si.conversation_key
            if conv_key and st.conversation_map.get(conv_key) == session_id:
                del st.conversation_map[conv_key]
        # Delete ConversationStore JSONL
        try:
            from pathlib import Path
            from engine.conversation_store import ConversationStore
            conv_store = ConversationStore(Path(st.workspace_root) / "data" / "conversations")
            conv_store.delete(session_id)
        except Exception:
            pass
        # Clean up node contexts
        try:
            from engine.context_store import cleanup_session_contexts
            cleanup_session_contexts(st.workspace_root, session_id)
        except Exception:
            pass
        # Mark as reset in sessions.json
        try:
            st._session_store.on_session_reset(session_id)
        except Exception:
            pass
        return {"ok": True, "session_id": session_id}

    @app.get("/v1/sessions/{session_id}/messages")
    async def session_messages(session_id: str, limit: int = Query(50, ge=0, le=500)) -> list[dict[str, Any]]:
        st: SupervisorState = app.state.state
        return st.session_messages(session_id=session_id, limit=limit)

    @app.get("/v1/sessions/{session_id}/history")
    async def session_history(
        session_id: str,
        limit: int = Query(200, ge=0, le=1000),
        task_id: str = Query("", description="Filter messages by source_task_id"),
    ) -> list[dict[str, Any]]:
        """Structured message history from ConversationStore (for web frontend)."""
        st: SupervisorState = app.state.state
        return st.session_history_structured(session_id=session_id, limit=limit, task_id=task_id.strip() or None)

    @app.get("/v1/sessions/{session_id}/children")
    async def session_children(session_id: str) -> list[dict[str, Any]]:
        """Child sessions for a parent session, used by the web frontend refresh path."""
        # [2026-06-03] Why: frontend childNodes is memory-only and cannot be rebuilt
        # from /history. How: expose the supervisor's durable child-session registry
        # through a small read-only endpoint. Purpose: page refresh restores child
        # status rows and can navigate to each child session's own /history stream.
        st: SupervisorState = app.state.state
        return st.session_children(session_id=session_id)

    @app.post("/v1/sessions/{session_id}/cancel")
    async def session_cancel(session_id: str) -> dict[str, Any]:
        st: SupervisorState = app.state.state
        ok = st.cancel_session(session_id)
        if not ok:
            raise HTTPException(status_code=404, detail="session not found")
        return {"ok": True, "session_id": session_id}

    @app.get("/v1/sessions/{session_id}/cancelled")
    async def session_cancelled(session_id: str) -> dict[str, Any]:
        st: SupervisorState = app.state.state
        return {"cancelled": st.is_cancelled(session_id)}

    @app.post("/v1/sessions/{session_id}/cancel/clear")
    async def session_cancel_clear(session_id: str) -> dict[str, Any]:
        st: SupervisorState = app.state.state
        st.clear_cancelled(session_id)
        return {"ok": True}

    def _reset_conversation(st: "SupervisorState", conv_key: str) -> dict[str, Any]:
        """重置一个会话。控制台和 /clear 走同一条路，否则一边清了另一边还留着。"""
        result = st.reset_conversation(conversation_key=conv_key)
        if not result.get("ok"):
            raise HTTPException(status_code=404, detail=result.get("error", "not found"))
        # emit context_reset 事件，通知 bot 侧重置高水位
        old_sid = result.get("old_session_id", "")
        if old_sid:
            st.eventlog.append(
                session_id=old_sid,
                component="supervisor",
                type_="context_reset",
                payload={"conversation_key": conv_key, "reason": "clear"},
            )
        # Also clean up node_contexts for old session
        if old_sid:
            from engine.context_store import cleanup_session_contexts
            try:
                cleaned = cleanup_session_contexts(st.workspace_root, old_sid)
                result["context_files_cleaned"] = cleaned
            except Exception:
                pass
        return result

    @app.post("/v1/conversations/reset")
    async def conversation_reset(body: dict[str, Any]) -> dict[str, Any]:
        """Reset a conversation, forcing next message to create a new session."""
        st: SupervisorState = app.state.state
        conv_key = str(body.get("conversation_key") or "").strip()
        if not conv_key:
            raise HTTPException(status_code=400, detail="conversation_key required")
        return _reset_conversation(st, conv_key)

    @app.get("/v1/admin/conversations")
    async def admin_list_conversations(request: Request) -> dict[str, Any]:
        """控制台用的会话清单：每条带归属、消息数和体积。"""
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        from .conversation_labels import (
            describe_namespaces, is_internal_task_key, is_runtime_copy_session,
            load_sessions, memory_namespace, scoped_conversation_keys,
        )

        labels = describe_namespaces(st.workspace_root)
        sessions = load_sessions(st.workspace_root)
        # 适配器没发布过归属就全部按当前号算，不凭空把人分成两组。
        scoped_keys = scoped_conversation_keys(st.workspace_root)
        conv_dir = st.workspace_root / "data" / "conversations"
        rows: list[dict[str, Any]] = []
        for path in sorted(conv_dir.glob("*.jsonl")) if conv_dir.is_dir() else []:
            session_id = path.stem
            # 入口分支/子代理副本会照抄父会话的 conversation_key，不挡掉就是同名同大小的第二行。
            if is_runtime_copy_session(session_id):
                continue
            info = sessions.get(session_id) or {}
            conv_key = str(info.get("conversation_key") or "")
            # dream 的整理任务借 namespace 当会话键，会在这里冒出一条与真实群同名的
            # 假会话 —— 它不是对话，点删除也只是清掉任务记录，下轮又写回来。
            if is_internal_task_key(conv_key):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            rows.append({
                "session_id": session_id,
                "conversation_key": conv_key,
                # session 已被清掉但 jsonl 还在的孤儿也要列出来，否则永远没人删。
                "owner": labels.get(memory_namespace(conv_key)) if conv_key else None,
                # 非 QQ 会话（web / cli）不参与账号归属，始终算当前。
                "current_account": (
                    True if scoped_keys is None or not conv_key.startswith(("qq_group:", "qq_private:"))
                    else conv_key in scoped_keys
                ),
                "channel": str(info.get("channel") or ""),
                "bytes": stat.st_size,
                "updated_at": stat.st_mtime,
            })
        rows.sort(key=lambda row: row["updated_at"], reverse=True)
        return {"conversations": rows}

    @app.get("/v1/admin/conversations/{session_id}/messages")
    async def admin_conversation_messages(
        session_id: str, request: Request, limit: int = Query(200, ge=1, le=2000),
    ) -> dict[str, Any]:
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        from engine.conversation_store import ConversationStore

        store = ConversationStore(st.workspace_root / "data" / "conversations")
        messages = store.load(session_id)
        return {
            "total": len(messages),
            "messages": [message.to_dict() for message in messages[-limit:]],
        }

    @app.post("/v1/admin/conversations/{session_id}/reset")
    async def admin_conversation_reset(session_id: str, request: Request) -> dict[str, Any]:
        """按 session 重置。走和 /clear 同一条路，adapter 侧的群历史缓存才会跟着清。"""
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        from .conversation_labels import load_sessions

        info = load_sessions(st.workspace_root).get(session_id) or {}
        conv_key = str(info.get("conversation_key") or "").strip()
        if not conv_key:
            raise HTTPException(status_code=404, detail="session has no conversation to reset")
        return _reset_conversation(st, conv_key)

    def _scope_migration():
        """按文件加载迁移脚本：它绕开了 adapters.onebot 的 nonebot 依赖，
        而摘要算法必须和 bot 跑的是同一份。"""
        import importlib.util
        import sys

        key = "_supervisor_scope_migration"
        cached = sys.modules.get(key)
        if cached is not None:
            return cached
        path = Path(__file__).resolve().parents[1] / "deploy" / "migrate_qq_bot_scope.py"
        spec = importlib.util.spec_from_file_location(key, path)
        if spec is None or spec.loader is None:
            raise HTTPException(status_code=500, detail="迁移脚本不可用")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
        return module

    def _bot_liveness(st: "SupervisorState") -> tuple[bool, str]:
        """(bot 是否还活着, 当前登录的号)。搬迁必须在 bot 停掉之后做。"""
        path = st.workspace_root / "data" / "qq_live_state.json"
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False, ""
        if not isinstance(state, dict):
            return False, ""
        runtime = state.get("runtime") if isinstance(state.get("runtime"), dict) else {}
        # bot 至少每 30s 重写一次，留三倍余量再判定它真的停了。
        alive = (time.time() - float(state.get("published_at") or 0.0)) <= 90.0
        return alive, str(runtime.get("bot_scope") or "")

    def _scope_plan_rows(report: Any) -> list[dict[str, Any]]:
        return [
            {
                "old_namespace": rename.old_ns,
                "new_namespace": rename.new_ns,
                "has_memory": rename.dir_exists,
                "blocked": rename.target_exists,
            }
            for rename in report.changed_renames
        ]

    @app.get("/v1/admin/qq/scope")
    async def admin_qq_scope(request: Request, target: str = Query("")) -> dict[str, Any]:
        """当前账号归属，以及搬到 target 名下会动哪些东西的预览。"""
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        migration = _scope_migration()
        bot_alive, live_scope = _bot_liveness(st)
        current = migration._bot_scope.load_scope(st.workspace_root)
        payload: dict[str, Any] = {
            "current_scope": current,
            "live_scope": live_scope,
            "bot_alive": bot_alive,
            "preview": None,
        }
        if not target.strip():
            return payload
        try:
            report = migration.run_scope_migration(
                workspace=st.workspace_root, target_scope=target, apply=False,
            )
        except migration.ScopeMismatch as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        payload["preview"] = {
            "source_scope": report.source_scope,
            "target_scope": report.target_scope,
            "conversations": _scope_plan_rows(report),
            "unknown_namespaces": report.unknown_conv_dirs,
        }
        return payload

    @app.post("/v1/admin/qq/scope/migrate")
    async def admin_qq_scope_migrate(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """把全部会话与长期记忆搬到另一个 bot 账号名下。"""
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        migration = _scope_migration()
        bot_alive, _live_scope = _bot_liveness(st)
        if bot_alive:
            raise HTTPException(
                status_code=409,
                detail="bot 进程还在运行。它内存里存着按旧账号算出的会话键，搬迁会被它写回去 —— 请先停掉 bot。",
            )
        try:
            report = migration.run_scope_migration(
                workspace=st.workspace_root,
                target_scope=str(payload.get("target") or ""),
                source_scope=payload.get("source"),
                apply=True,
            )
        except migration.ScopeMismatch as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # 立刻把内存拉回与盘一致：晚一步 flush 就会拿旧的 conversation_key 覆盖掉刚搬好的。
        reloaded = st.reload_session_registry()
        st.eventlog.append(
            session_id="", component="supervisor", type_="qq_scope_migrated",
            payload={
                "source_scope": report.source_scope, "target_scope": report.target_scope,
                "moved": len(report.moved_dirs), "sessions": report.sessions_changed,
            },
        )
        return {
            "ok": True,
            "source_scope": report.source_scope,
            "target_scope": report.target_scope,
            "moved_memory_dirs": len(report.moved_dirs),
            "moved_attachment_dirs": len(report.moved_attachment_dirs),
            "hit_keys_rewritten": report.hit_keys_rewritten,
            "route_keys_changed": report.route_keys_changed,
            "sessions_changed": report.sessions_changed,
            "sessions_reloaded": reloaded,
            "skipped": [
                {"old_namespace": old, "new_namespace": new}
                for old, new in report.skipped_target_exists
            ],
            "unknown_namespaces": report.unknown_conv_dirs,
            "backup_dir": str(report.backup_dir) if report.backup_dir else "",
        }

    @app.post("/v1/tasks/{task_id}/cancel")
    async def task_cancel(task_id: str) -> dict[str, Any]:
        """取消单个 task 及其所有子任务链。"""
        st: SupervisorState = app.state.state
        result = st.cancel_single_task(task_id)
        if not result.get("ok"):
            raise HTTPException(status_code=404, detail=result.get("error", "cancel failed"))
        return result

    @app.post("/v1/sessions/{session_id}/cancel_active_tasks")
    async def session_cancel_active_tasks(
        session_id: str,
        exclude_task_id: str = Query(""),
        # [2026-05-28] 可选 node_id 过滤：只取消指定节点的活跃任务。
        # 为什么：有时只想取消某个子节点的任务，而非 session 内全部。
        # 怎么改：新增 query param，透传到 cancel_active_tasks 方法。
        # 目的：更细粒度的任务取消控制。
        node_id: str = Query(""),
    ) -> dict[str, Any]:
        """取消 session 中所有活跃 task。供 AI 工具调用。"""
        st: SupervisorState = app.state.state
        with st._lock:
            # [Fork/Merge 2026-05-17] Why: this endpoint can still be called with
            # a branch id from ToolContext in older workers. How: normalize entry
            # branches to the parent before cancellation. Purpose: sibling branches
            # under the same user conversation are included.
            route_session_id = st._route_session_id_for_session_locked(session_id)
            if route_session_id not in st.sessions:
                raise HTTPException(status_code=404, detail="session not found")
        return st.cancel_active_tasks(
            route_session_id, exclude_task_id=exclude_task_id,
            node_id=node_id or None,
        )

    @app.post("/v1/sessions/{session_id}/switch_node")
    async def session_switch_node(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """AI 或外部调用：设置/清除 session 级入口节点覆盖。"""
        st: SupervisorState = app.state.state
        target = str(body.get("target_node_id") or "").strip()
        return st.switch_session_node(session_id, target)

    @app.get("/v1/sessions/{session_id}/active_node")
    async def session_active_node(session_id: str) -> dict[str, Any]:
        """查询 session 当前实际使用的入口节点。"""
        st: SupervisorState = app.state.state
        return st.get_session_active_node(session_id)

    @app.get("/v1/sessions/{session_id}/provider_override")
    async def session_provider_override_get(session_id: str, request: Request) -> dict[str, Any]:
        """查询 session 级 provider 覆盖配置。"""
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        result = st.get_session_provider_override(session_id)
        if result is None:
            raise HTTPException(status_code=404, detail="session not found")
        return result

    @app.put("/v1/sessions/{session_id}/provider_override")
    async def session_provider_override_put(
        session_id: str,
        request: Request,
        body: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """设置 session 级 provider 覆盖配置。"""
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        # [AutoC 2026-06-01] Why: provider overrides are intentionally generic
        # JSON dictionaries, but the endpoint must reject non-object bodies before
        # they reach SessionInfo. How: FastAPI parses the body and this guard keeps
        # only dict values. Purpose: provider adapters can add fields later while
        # malformed requests get a clear 400 response.
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="provider_override must be a JSON object")
        _verify_channel_ref(
            app.state.config_store,
            str(body.get("provider") or body.get("provider_type") or ""),
        )
        result = st.set_session_provider_override(session_id, body)
        if result is None:
            raise HTTPException(status_code=404, detail="session not found")
        return result

    @app.delete("/v1/sessions/{session_id}/provider_override")
    async def session_provider_override_delete(session_id: str, request: Request) -> dict[str, Any]:
        """清除 session 级 provider 覆盖配置。"""
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        result = st.clear_session_provider_override(session_id)
        if result is None:
            raise HTTPException(status_code=404, detail="session not found")
        return result

    @app.get("/v1/sessions/{session_id}/context_window")
    async def session_context_window(session_id: str) -> dict[str, Any]:
        """获取 session 当前上下文窗口的 token 用量信息。"""
        st: SupervisorState = app.state.state
        with st._lock:
            # [Fork/Merge 2026-05-17] Why: context_usage events are emitted on the
            # parent route session while branch sessions are temporary storage.
            # How: normalize entry branches to the parent for this read endpoint.
            # Purpose: get_context_window reports real session usage.
            route_session_id = st._route_session_id_for_session_locked(session_id)
            if route_session_id not in st.sessions:
                raise HTTPException(status_code=404, detail="session not found")
        return st.get_session_context_usage(route_session_id)

    @app.post("/v1/approvals/request", response_model=Approval)
    async def approval_request(inp: ApprovalRequestIn, request: Request) -> Approval:
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        # [AutoC 2026-05-31] Why: direct approval API callers may know which tool
        # produced the request. How: forward optional identity fields accepted by
        # ApprovalRequestIn. Purpose: both direct and policy-created approvals can
        # render inside ToolCallCard when possible.
        return st.create_approval(
            session_id=inp.session_id,
            operation=inp.operation,
            details=inp.details,
            tool_call_id=inp.tool_call_id,
            node_id=inp.node_id,
            task_id=inp.task_id,
        )

    @app.get("/v1/approvals/{approval_id}", response_model=Approval)
    async def approval_get(approval_id: str) -> Approval:
        st: SupervisorState = app.state.state
        if approval_id not in st.approvals:
            raise HTTPException(status_code=404, detail="approval not found")
        return st.approvals[approval_id]

    @app.post("/v1/approvals/{approval_id}", response_model=Approval)
    async def approval_decide(
        approval_id: str, body: ApprovalDecisionIn, request: Request,
    ) -> Approval:
        # 这个端点能批准写源码、改 policy、重启；:8765 只绑回环不等于同机进程都可信。
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        a = st.decide_approval(approval_id=approval_id, decision=body.decision, comment=body.comment)
        if a is None:
            raise HTTPException(status_code=404, detail="approval not found")
        return a

    @app.post("/v1/ops/request", response_model=OpRequestOut)
    async def ops_request(inp: OpRequestIn, request: Request) -> OpRequestOut:
        st: SupervisorState = app.state.state
        _audit_ops_caller(st, request, inp)
        # [AutoC 2026-05-31] Why: ops/request is the policy path used by tools.
        # How: pass optional tool_call_id/node_id/task_id through to the supervisor
        # state layer. Purpose: approval_requested events can update the active tool
        # card instead of creating a detached approval card.
        return st.request_operation(
            session_id=inp.session_id,
            op=inp.op,
            parameters=inp.parameters,
            tool_call_id=inp.tool_call_id,
            node_id=inp.node_id,
            task_id=inp.task_id,
        )

    @app.get("/v1/admin/state", response_model=AdminStateOut)
    async def admin_state(request: Request) -> AdminStateOut:
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        return st.admin_state()

    @app.get("/v1/admin/tasks/active")
    async def admin_active_tasks(request: Request) -> list[dict[str, Any]]:
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        active_statuses = {TaskStatus.running, TaskStatus.pending, TaskStatus.suspended}
        result: list[dict[str, Any]] = []
        with st._lock:
            for t in st.tasks.values():
                if t.status not in active_statuses:
                    continue
                # [AutoC 2026-06-04] Why: the System dashboard modal needs task
                # details, but full Task objects include large input/result and
                # continuation payloads. How: return only identifying metadata and
                # timestamps while holding the state lock during iteration. Purpose:
                # operators can inspect active work without leaking heavy payloads or
                # racing concurrent task updates.
                # [AutoC 2026-06-04] Why: operators need enough context to
                # identify a task in the modal, but returning full Task.input can be
                # large or sensitive. How: extract only text/instruction, normalize it
                # to a trimmed string, and cap it at 200 characters. Purpose: the
                # active-task API supports a safe human-readable input preview.
                input_text = str(t.input.get("text") or t.input.get("instruction") or "").strip()
                result.append({
                    "task_id": t.task_id,
                    "session_id": t.session_id,
                    "node_id": t.node_id,
                    "status": t.status.value,
                    "kind": t.kind.value,
                    "created_at": t.created_at.isoformat(),
                    "updated_at": t.updated_at.isoformat(),
                    "worker_id": t.worker_id,
                    "caller_task_id": t.caller_task_id,
                    "input_summary": input_text[:200] if input_text else "",
                    "cancel_requested": t.cancel_requested,
                    "current_phase": t.current_phase,
                    "current_detail": t.current_detail,
                })
        result.sort(key=lambda x: x["created_at"], reverse=True)
        return result

    @app.get("/v1/admin/runtime/logs")
    async def runtime_logs(request: Request) -> dict[str, Any]:
        verify_admin_token(request)
        reader = LogReader(app.state.state.workspace_root / "data" / "logs")
        return {
            "files": [
                {"name": row.name, "size": row.size, "modified": row.modified}
                for row in reader.list()
            ],
        }

    @app.get("/v1/admin/runtime/logs/{name}")
    async def runtime_log_tail(name: str, request: Request, lines: int = 500) -> dict[str, Any]:
        verify_admin_token(request)
        reader = LogReader(app.state.state.workspace_root / "data" / "logs")
        result = reader.tail(name, lines=lines)
        if result is None:
            raise HTTPException(status_code=404, detail="没有这个日志文件")
        text, truncated = result
        return {"name": name, "text": text, "truncated": truncated}

    @app.get("/v1/admin/runtime/status")
    async def runtime_status(request: Request) -> dict[str, Any]:
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        pm: ProcessManager | None = app.state.process_manager
        with st._lock:
            generations = dict(st._engine_generations)
            queued = sum(1 for t in st.tasks.values() if t.status == "queued")
            running = sum(1 for t in st.tasks.values() if t.status == "running")
        workers = pm.worker_health() if pm is not None else {}
        for name, row in workers.items():
            row["generation"] = generations.get(name, "")
        return {
            # 没有 process manager 时 workers 是空的 —— 界面要能说清「测不到」而不是「都挂了」。
            "supervised": pm is not None,
            "workers": workers,
            "tasks": {"queued": queued, "running": running},
            "started_at": st.started_at.isoformat(),
            "uptime_sec": round((_now() - st.started_at).total_seconds(), 1),
        }

    @app.post("/v1/admin/runtime/engine/retry")
    async def runtime_engine_retry(request: Request) -> dict[str, Any]:
        """看门狗停手之后的人工重试。"""
        verify_admin_token(request)
        pm: ProcessManager | None = app.state.process_manager
        if pm is None:
            raise HTTPException(status_code=409, detail="这个部署没有 process manager，engine 不由它拉起")
        pm.clear_given_up()
        return {"ok": True}

    @app.post("/v1/admin/restart", response_model=RestartOut)
    async def admin_restart(inp: RestartIn, request: Request) -> RestartOut:
        verify_admin_token(request)
        pm: ProcessManager | None = app.state.process_manager
        st: SupervisorState = app.state.state

        st.eventlog.append(
            session_id="__system__",
            component="supervisor",
            type_="restart_requested",
            payload={
                "target": inp.target,
                "reason": inp.reason,
                "approval_id": inp.approval_id,
                "ts": _now().isoformat(),
            },
        )

        # 只重启 engine 依赖两处隔离，缺一处就会把 supervisor 一起带走：
        # process_manager._spawn 的 start_new_session（engine 独立进程组）、以及
        # main.py 关掉 uvicorn 的信号捕获 + _restarting_engine 抑制标志。
        if inp.target == "engine":
            if pm is None:
                # 没有 process manager 就没有 engine 可停。这里必须报失败而不是假装排上了：
                # 全量重启那条路靠 os._exit(75) 让外层 launcher 拉起，和这里不是一回事。
                st.eventlog.append(
                    session_id="__system__",
                    component="supervisor",
                    type_="restart_failed",
                    payload={"target": inp.target, "reason": "no process manager", "ts": _now().isoformat()},
                )
                return RestartOut(scheduled=False, target=inp.target)
            # 重新加载 .env，确保修改后的环境变量在 supervisor 进程中生效
            try:
                from dotenv import load_dotenv
                load_dotenv(override=True)
            except Exception:
                pass
            # 先注入 outbound 让 handle_agent 正常闭合（log embed 有终态）
            if inp.session_id:
                # 找到当前 session 活跃任务的 source_inbound_seq，
                # 确保 Bot 端 poller 能匹配到 trigger 并正确关闭 status_msg
                _restart_src_seq = None
                for _rt in st.tasks.values():
                    if (_rt.session_id == inp.session_id
                            and _rt.status in (TaskStatus.running, TaskStatus.pending)
                            and _rt.source_inbound_seq):
                        _restart_src_seq = _rt.source_inbound_seq
                        break
                _restart_outbound_payload: dict[str, Any] = {"text": "✅ 已触发 Engine 重启，正在执行..."}
                if _restart_src_seq:
                    _restart_outbound_payload["source_inbound_seq"] = _restart_src_seq
                st.eventlog.append(
                    session_id=inp.session_id,
                    component="supervisor",
                    type_="outbound_message",
                    payload=_restart_outbound_payload,
                )
            # Deferred engine restart: return HTTP 200 first, then kill+restart.
            # This ensures the tool call in the dying engine receives its response
            # and can shadow_write the tool_result before being terminated.
            _restart_session_id = inp.session_id
            _restart_target = inp.target

            def _deferred_engine_restart() -> None:
                time.sleep(1)  # let HTTP response reach engine first
                pm._restarting_engine = True  # 抑制信号 handler 退出
                try:
                    pm.stop_engine()
                    time.sleep(1)  # 等待延迟信号消散
                    pm.start_engine()
                    # Brief health check: verify engine process is alive
                    time.sleep(0.5)
                    _alive = any(p.popen.poll() is None for p in pm.engines)
                    if not _alive:
                        st.eventlog.append(
                            session_id=_restart_session_id or "__system__",
                            component="supervisor",
                            type_="outbound_message",
                            payload={"text": "❌ Engine 重启失败：进程启动后立即退出。"},
                        )
                        return
                except Exception as exc:
                    pm._restarting_engine = False
                    st.eventlog.append(
                        session_id=_restart_session_id or "__system__",
                        component="supervisor",
                        type_="outbound_message",
                        payload={"text": f"❌ Engine 重启失败：{exc}"},
                    )
                    return
                # ---- 清理旧 engine 遗留的孤儿 task ----
                _orphan_count = st.cancel_orphaned_tasks()
                if _orphan_count:
                    st.eventlog.append(
                        session_id="__system__",
                        component="supervisor",
                        type_="orphan_cleanup",
                        payload={
                            "count": _orphan_count,
                            "trigger": "engine_restart",
                            "ts": _now().isoformat(),
                        },
                    )
                st.eventlog.append(
                    session_id="__system__",
                    component="supervisor",
                    type_="restart_completed",
                    payload={"target": _restart_target, "ts": _now().isoformat()},
                )
                # Defer restart notification — will be injected in register_engine()
                # after orphan cleanup, so the task won't be reaped.
                if _restart_session_id:
                    st._pending_restart_notify = _restart_session_id
                pm._restarting_engine = False  # 清除信号抑制

            threading.Thread(target=_deferred_engine_restart, daemon=True, name="restart-engine").start()
            return RestartOut(scheduled=True, target=_restart_target)

        # --no-shell 模式下没有 _watch_shell 线程，需要直接退出
        # 先停 engine，再延迟退出让 HTTP 响应发出去
        if inp.session_id:
            _pending_path = Path(st.workspace_root) / "data" / "restart_pending.json"
            _si = st.sessions.get(inp.session_id)
            try:
                _pending_path.write_text(json.dumps({
                    "session_id": inp.session_id,
                    "target": "all",
                    "conversation_key": _si.conversation_key if _si else "",
                    "channel": _si.channel if _si else "",
                    "ts": _now().isoformat(),
                }), encoding="utf-8")
            except Exception:
                pass
            # 找 source_inbound_seq
            _all_restart_src_seq = None
            for _art in st.tasks.values():
                if (_art.session_id == inp.session_id
                        and _art.status in (TaskStatus.running, TaskStatus.pending)
                        and _art.source_inbound_seq):
                    _all_restart_src_seq = _art.source_inbound_seq
                    break
            _all_restart_payload: dict[str, Any] = {"text": "✅ 已触发全量重启，系统即将重启..."}
            if _all_restart_src_seq:
                _all_restart_payload["source_inbound_seq"] = _all_restart_src_seq
            st.eventlog.append(
                session_id=inp.session_id,
                component="supervisor",
                type_="outbound_message",
                payload=_all_restart_payload,
            )

        def _deferred_exit() -> None:
            time.sleep(1)
            if pm is not None:
                try:
                    pm.stop_all()  # stop engine + shell, with wait
                except Exception:
                    pass
                # Double-check: wait for all engine processes to be reaped
                for _eng in getattr(pm, 'engines', []):
                    try:
                        _eng.popen.wait(timeout=5)
                    except Exception:
                        pass
            import traceback as _tb
            _msg = f'[DIAG] os._exit(75) called from api.py! stack:\n{"" .join(_tb.format_stack())}'
            print(_msg, flush=True)
            import sys as _sys; _sys.stdout.flush(); _sys.stderr.flush()
            import time as _t; _t.sleep(0.5)  # 确保日志写出
            os._exit(75)  # main.py 外层循环检测到 75 会重启

        if pm is not None:
            pm._restart_pending = True
        threading.Thread(target=_deferred_exit, daemon=True, name="restart-all").start()
        return RestartOut(scheduled=True, target="all")

    # ---- 异步委派 API ----
    @app.post("/v1/tasks/dispatch-async")
    async def dispatch_async(request: Request) -> dict[str, Any]:
        """异步委派子节点：创建子任务后立即返回 task_id，父任务不挂起。"""
        st: SupervisorState = app.state.state
        body = await request.json()

        session_id = str(body.get("session_id") or "").strip()
        session_generation = int(body.get("session_generation", 1))
        node_id = str(body.get("node_id") or "").strip()
        instruction = str(body.get("instruction") or "").strip()
        # [AutoC 2026-07-09] Why: default no longer hardwired to accumulate; explicit
        # arg wins, otherwise inferred from target node persistent declaration
        # (persistent -> accumulate, else fresh).
        _raw_ctx_mode = str(body.get("context_mode") or "").strip()
        if _raw_ctx_mode:
            context_mode = _raw_ctx_mode
        else:
            _node_persistent = False
            if node_id:
                try:
                    from engine.node import load_node
                    _tn = load_node(Path(st.workspace_root), node_id)
                    _node_persistent = bool(_tn is not None and getattr(_tn, "persistent", False))
                except Exception:
                    _node_persistent = False
            context_mode = "accumulate" if _node_persistent else "fresh"
        context_key = str(body.get("context_key") or "").strip() or None
        source_inbound_seq = body.get("source_inbound_seq")
        caller_node_id = str(body.get("caller_node_id") or "").strip()
        # [Fork/Merge 2026-05-17] Why: newer engine workers include the parent
        # route session when an async dispatch is requested from a branch. How:
        # read it as a fallback for branch index recovery. Purpose: async children
        # are anchored to the durable conversation even if branch indexes are stale.
        parent_session_id = str(body.get("parent_session_id") or "").strip()
        # [2026-04-22] 读取父节点传来的附件列表，透传到 input_data 供 runner.py 消费
        attachments = body.get("attachments")

        if not session_id or not node_id:
            raise HTTPException(status_code=400, detail="session_id and node_id required")
        if session_id not in st.sessions:
            raise HTTPException(status_code=404, detail="session not found")

        input_data: dict[str, Any] = {
            "instruction": instruction,
            "_async_dispatch": True,
            "_caller_node_id": caller_node_id,
        }
        # [2026-04-22] 将附件列表注入 input_data，runner.py L594 已支持读取 input_data["attachments"]
        if attachments and isinstance(attachments, list):
            input_data["attachments"] = attachments
        if context_key:
            input_data["_context_key"] = context_key

        src_seq: int | None = None
        if source_inbound_seq is not None:
            try:
                src_seq = int(source_inbound_seq)
            except (ValueError, TypeError):
                pass

        with st._lock:
            # [2026-05-14] 异步子任务应挂到 parent session 而非 caller 的 branch。
            # 问题：caller 跑在 entry branch 上，caller finish 后 branch 被清理，
            # 导致异步子任务被连带 cancelled。
            # 修复：如果 session_id 是 entry branch，追溯到 parent session。
            # 子任务完成后 _inject_async_dispatch_result_locked 会往 parent 注入
            # inbound 并 fork 新 branch 处理结果，路径不受影响。
            st._ensure_entry_branch_indexes_locked()
            _task_session_id = session_id
            _task_generation = session_generation
            _parent_of_branch = st.entry_branch_parents.get(session_id)
            if not _parent_of_branch and parent_session_id:
                # [Fork/Merge 2026-05-17] Why: a restarted supervisor may have to
                # infer branch ancestry from the request payload or sessions.json.
                # How: trust parent_session_id only when the supplied session is an
                # entry branch for that parent. Purpose: avoid misrouting ordinary
                # child sessions while recovering branch async dispatch routing.
                if st._is_entry_branch_session_locked(session_id, parent_session_id=parent_session_id):
                    _parent_of_branch = parent_session_id
            if _parent_of_branch:
                _task_session_id = _parent_of_branch
                _task_generation = st._current_session_generation_locked(_task_session_id) or 1

            # Child Session 隔离（Phase B）：async dispatch 也走 child session
            _child_sid, _is_new = st.get_or_create_child_session(
                _task_session_id, node_id, context_key or "", context_mode,
            )
            input_data["child_session_id"] = _child_sid
            input_data["context_mode"] = context_mode
            input_data["use_context"] = False
            if context_mode == "fork":
                input_data["fork_from_session_id"] = _task_session_id
            # 审计报告 Step 1（2026-04-16）：删除 async dispatch 的 accumulate fallback。
            # engine/runner.py:514 在 child_session_id 非空时会无条件清空 context_ref，
            # 此 fallback 注入永远不会被消费，属于兼容期死代码。

            task = st._create_task_locked(
                session_id=_task_session_id,
                session_generation=_task_generation,
                kind=TaskKind.node,
                node_id=node_id,
                input_data=input_data,
                continuation={},
                source_inbound_seq=src_seq,
                caller_task_id=None,
            )

        return {"ok": True, "task_id": task.task_id}

    @app.post("/v1/qq/intent/judge")
    async def qq_intent_judge(request: Request) -> dict[str, Any]:
        """判断一条群消息是不是在跟 bot 说话，等模型答完再返回。

        supervisor 不直接调模型，所以这里建一个 node task 交给 engine worker，
        在 HTTP 请求里等它回来。等不到就按「不接话」放行，绝不让判定层拖住群消息。
        """
        # 这个端点每次调用都建一个 LLM 任务并占住一个 engine worker，裸奔等于给任何人
        # 一个烧钱 + 把真实对话挤出队列的开关。bot 侧的 ClonothClient 本来就带 token。
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        body = await request.json()

        conversation_key = str(body.get("conversation_key") or "").strip()
        text = str(body.get("text") or "")
        if not conversation_key or not text.strip():
            raise HTTPException(status_code=400, detail="conversation_key and text required")
        channel = str(body.get("channel") or "qq_group").strip() or "qq_group"
        node_id = str(body.get("node_id") or "qq.intent").strip() or "qq.intent"
        try:
            timeout_sec = min(60.0, max(1.0, float(body.get("timeout_sec") or 8.0)))
        except (TypeError, ValueError):
            timeout_sec = 8.0
        try:
            max_inflight = min(16, max(1, int(body.get("max_inflight") or 1)))
        except (TypeError, ValueError):
            max_inflight = 1
        context_lines = [str(x) for x in (body.get("context_lines") or []) if str(x).strip()]
        bot_names = [str(x) for x in (body.get("bot_names") or []) if str(x).strip()]

        registry = st.qq_intent
        # 闸门在建 task 之前：排队等 worker 的判定任务毫无价值 —— 等它轮到的时候，
        # 那条群消息早就翻页了，而它占的还是真实对话要用的那个 worker。
        if registry.has_conversation(conversation_key):
            return {"task_id": "", **QQIntentResult(error="conversation_busy").as_dict()}
        if registry.inflight() >= max_inflight:
            return {"task_id": "", **QQIntentResult(error="max_inflight").as_dict()}

        instruction = qq_intent_instruction(
            text=text,
            context_lines=context_lines,
            bot_names=bot_names,
            speaker=str(body.get("speaker") or ""),
        )
        # 判定复用这个群的会话：conversation_key 和真实消息用的是同一个，所以第一条
        # 消息判 no 也只是提前建了个空会话，不会多出一条平行会话。
        session_id = st.get_or_create_session(channel=channel, conversation_key=conversation_key)
        with st._lock:
            generation = st._current_session_generation_locked(session_id) or 1
            child_sid, _is_new = st.get_or_create_child_session(
                session_id, node_id, f"intent:{conversation_key}", "fresh",
            )
            task = st._create_task_locked(
                session_id=session_id,
                session_generation=generation,
                kind=TaskKind.node,
                node_id=node_id,
                input_data={
                    "instruction": instruction,
                    "context_mode": "fresh",
                    "child_session_id": child_sid,
                    "use_context": False,
                    "_system_task": True,
                    "_qq_intent": True,
                },
                continuation={},
                source_inbound_seq=None,
                caller_task_id=None,
            )
            future = registry.register(task.task_id, conversation_key)

        try:
            result = await asyncio.wait_for(asyncio.shield(future), timeout=timeout_sec)
        except asyncio.TimeoutError:
            registry.discard(task.task_id)
            # 超时了还留着任务，模型慢的时候判定会一直堆积，把 worker 全占住。
            st.cancel_single_task(task.task_id)
            return {"task_id": task.task_id, **QQIntentResult(error="timeout").as_dict()}
        except Exception as exc:
            registry.discard(task.task_id)
            st.cancel_single_task(task.task_id)
            log.exception("qq intent judge failed")
            return {"task_id": task.task_id, **QQIntentResult(error=str(exc)[:200]).as_dict()}
        return {"task_id": task.task_id, **result.as_dict()}

    @app.post("/v1/tasks/compact-async")
    async def compact_async(request: Request) -> dict[str, Any]:
        """Create a background compaction task that does not suspend the caller.

        engine 侧返回 dispatch action 会让当轮任务让位并挂起，用户干等一次额外的
        LLM 调用。这个端点让软阈值触发的压缩在后台跑完，当轮继续用旧上下文回完话，
        下一轮才吃到压缩结果。
        """
        st: SupervisorState = app.state.state
        body = await request.json()
        target_session_id = str(body.get("target_session_id") or "").strip()
        instruction = str(body.get("instruction") or "")
        node_id = str(body.get("compactor_node_id") or "system.compactor").strip()
        if not target_session_id or not instruction.strip():
            raise HTTPException(status_code=400, detail="target_session_id and instruction required")

        def _int_field(key: str, default: int = 0) -> int:
            try:
                return max(0, int(body.get(key) or default))
            except (TypeError, ValueError):
                return default

        with st._lock:
            # 连续失败后进冷却：压缩模型持续不可用时，engine 每一步都会再请求一次，
            # 每次都要起一个 compactor task。engine 已经会把 reason 当成「跳过」处理。
            if st.compact_breaker.is_open(target_session_id):
                return {"ok": True, "task_id": "", "reason": "compact breaker open"}

            # 同一个会话只允许一个后台压缩在飞：并发两个压缩会各自基于同一份历史
            # 算出摘要，后落盘的那个把前一个的结果整段覆盖掉。
            for existing in st.tasks.values():
                if existing.status in (TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled):
                    continue
                if not existing.input.get("_compact_async"):
                    continue
                if str(existing.input.get("_compact_target_session_id") or "") == target_session_id:
                    return {"ok": True, "task_id": "", "reason": "compact already running"}

            # 要压的可能是 child session，而 child 从不进 st.sessions；不换成 anchor 就
            # 直接 404，等于 bootstrap.executor / draw.* / persistent 节点全部只有硬阈值。
            anchor_session_id = st.anchor_session_for_locked(target_session_id)
            if not anchor_session_id:
                raise HTTPException(status_code=404, detail="session not found")
            generation = st._current_session_generation_locked(anchor_session_id) or 1
            child_sid, _is_new = st.get_or_create_child_session(
                anchor_session_id, node_id, f"compact:{target_session_id}", "fresh",
            )
            task = st._create_task_locked(
                session_id=anchor_session_id,
                session_generation=generation,
                kind=TaskKind.node,
                node_id=node_id,
                input_data={
                    "instruction": instruction,
                    "context_mode": "fresh",
                    "child_session_id": child_sid,
                    "use_context": False,
                    "_system_task": True,
                    "_compact_async": True,
                    "_compact_target_session_id": target_session_id,
                    "_compact_keep_recent": _int_field("keep_recent", 6),
                    "_compact_keep_recent_tokens": _int_field("keep_recent_tokens"),
                    "_compact_threshold_tokens": _int_field("threshold_tokens"),
                    "_caller_node_id": str(body.get("caller_node_id") or "").strip(),
                },
                continuation={},
                source_inbound_seq=None,
                caller_task_id=None,
            )
        return {"ok": True, "task_id": task.task_id}

    admin_router = create_admin_router(workspace_root=state.workspace_root)
    app.include_router(admin_router, prefix="/v1/admin/config")
    app.include_router(
        create_sticker_router(workspace_root=state.workspace_root), prefix="/v1/stickers",
    )

    # 认证校验端点：前端用来验证 token 是否正确
    @app.get("/v1/admin/auth/check")
    async def admin_auth_check(request: Request) -> dict[str, Any]:
        try:
            verify_admin_token(request)
        except HTTPException:
            raise HTTPException(status_code=401, detail="Unauthorized")
        return {"ok": True}

    @app.get("/v1/instances")
    async def list_instances(request: Request) -> dict[str, Any]:
        """控制台账号切换器的数据源。单实例时 instances 为空，前端据此不渲染切换器。"""
        # 清单里是真实 QQ 号，跟其它 admin 数据同一个门槛。
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        prefix = url_prefix()
        return {
            "current_prefix": prefix,
            "instances": [
                {**row, "current": row["path"] == prefix}
                for row in load_instances(st.workspace_root)
            ],
        }

    @app.post("/v1/instances")
    async def create_instance(request: Request) -> dict[str, Any]:
        """开一个新号。工作区在这里建，docker/systemd/cloudflared 交给 root 侧单元。"""
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        body = await request.json() if await request.body() else {}
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="请求体要是对象")
        try:
            plan = provisioning.plan_instance(
                st.workspace_root,
                uin=str(body.get("uin") or ""),
                label=str(body.get("label") or ""),
            )
            # 先占清单再触发：并发两个请求时后来的会看见序号已被占，不会分到同一组端口。
            provisioning.scaffold(plan, st.workspace_root)
            await provisioning.start_unit(provisioning.CREATE_UNIT.format(uin=plan.uin))
        except provisioning.ProvisionError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {
            "uin": plan.uin, "label": plan.label, "idx": plan.idx,
            "prefix": plan.prefix, "ports": plan.ports,
            "workspace": str(plan.workspace),
        }

    @app.get("/v1/instances/{uin}/progress")
    async def instance_progress(uin: str, request: Request) -> dict[str, Any]:
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        try:
            return provisioning.progress(st.workspace_root, uin)
        except provisioning.ProvisionError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.delete("/v1/instances/{uin}")
    async def delete_instance(uin: str, request: Request) -> dict[str, Any]:
        """停掉并归档一个号。清单条目由 root 侧完成后自己摘，避免留下幽灵实例。"""
        verify_admin_token(request)
        st: SupervisorState = app.state.state
        try:
            row = provisioning.guard_removable(
                st.workspace_root, uin, current_prefix=url_prefix(),
            )
            await provisioning.start_unit(provisioning.REMOVE_UNIT.format(uin=row["uin"]))
        except provisioning.ProvisionError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"ok": True, "uin": row["uin"]}

    web_dist = state.workspace_root / "adapters" / "web" / "frontend" / "dist"
    console_url = ""
    if web_dist.is_dir():
        @app.get("/", include_in_schema=False)
        async def web_root(request: Request) -> RedirectResponse:
            """域名直接指到这个端口，根路径不给个去处就只有一个 404。"""
            # 挂在前缀下时 root_path 是那个前缀；不带上就会跳出本实例。
            return RedirectResponse(url=f"{request.scope.get('root_path', '')}/web/")

        app.mount("/web", StaticFiles(directory=str(web_dist), html=True), name="web")
        console_url = f"http://{host}:{port}{url_prefix()}/web/"
        print(f"[web] 前端地址: {console_url}", flush=True)

    init_admin_token(state.workspace_root, console_url=console_url)

    return app
