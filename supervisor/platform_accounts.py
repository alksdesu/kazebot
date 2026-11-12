"""平台账号运维与记忆归属搬迁：/v1/platforms/<id>/account…

登录、切号、扫码由平台声明的 account_ops 模块实现，这里只转发、翻译错误、记审计，换上的号记进实例清单；
搬迁算法在 deploy/migrate_qq_bot_scope.py，和离线脚本跑的是同一份。
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
import sys
import threading
import time
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping

from fastapi import APIRouter, Body, Depends, HTTPException, Query

import config_layout
from engine.conversation_migration import RekeyConflict
from engine.memory_hit_cache import HitCacheUnreadable, read_hit_cache
from platforms.account_ops import AccountConflict, AccountOps, AccountOpsError, AccountRequestInvalid, AccountUnreachable
from platforms.builtin import QQ_NAPCAT
from platforms.descriptor import HOOK_ACCOUNT_OPS, PlatformDescriptor
from platforms.ids import normalize_id
from platforms.live import read_live_status
from platforms.registry import MissingPlatformHook, all_platforms, platform_hook
from platforms.wording import name_with

from .admin_api import verify_admin_token
from .conversation_stores import ConversationStoreError, store_counts_payload
from .instances import InstanceDirectoryError, account_owner, record_account, url_prefix
from .platform_api import resolve_platform
from .state import SupervisorState
from .types import AccountSwitchIn, QqQuickLoginIn

_log = logging.getLogger(__name__)

_MIGRATION_MODULE = "_supervisor_scope_migration"
_MIGRATION_SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "migrate_qq_bot_scope.py"

# 兜底轮询的节律。bot 进程自己接掉线信号；这里只在它没在跑或没报的情况下让人知道号掉了。
WATCH_INTERVAL_SEC = 60.0
# 连续两轮不在线才记一笔：一次查询撞上重启窗口不算掉线。
WATCH_CONFIRM_ROUNDS = 2


class AccountWatch:
    """一个平台的登录态观察记录：最近一次查到什么、从什么时候起不在线。"""

    def __init__(self) -> None:
        self.last_checked_at: float = 0.0
        self.last_error: str = ""
        self.online: bool | None = None
        self.offline_since: float = 0.0
        self.offline_rounds: int = 0
        self.reported: bool = False

    def observe(self, *, online: bool | None, error: str, now: float) -> str:
        """记一次观察，返回要写审计的事件名；空串 = 没有变化值得记。"""
        self.last_checked_at = now
        self.last_error = error
        if online is None:
            return ""
        self.online = online
        if online:
            self.offline_rounds = 0
            self.offline_since = 0.0
            if self.reported:
                self.reported = False
                return "platform_account_online"
            return ""
        self.offline_rounds += 1
        if not self.offline_since:
            self.offline_since = now
        if not self.reported and self.offline_rounds >= WATCH_CONFIRM_ROUNDS:
            self.reported = True
            return "platform_account_offline"
        return ""

    def payload(self) -> dict[str, Any]:
        return {
            "last_checked_at": self.last_checked_at,
            "online": self.online,
            "offline_since": self.offline_since,
            "error": self.last_error,
        }


class PlatformAccounts:
    """各平台的账号运维对象，每个平台只建一个：平台模块缓存着登录凭证，每次新建就每次重登。

    带 state 时还能起一个兜底轮询：bot 进程不在、或它的掉线信号没发出来时，这里仍能把号不在线记进审计。
    """

    def __init__(self, workspace_root: Path, *, state: SupervisorState | None = None) -> None:
        self._root = Path(workspace_root)
        self._ops: dict[str, AccountOps] = {}
        self._lock = threading.Lock()
        self._state = state
        self._watches: dict[str, AccountWatch] = {}
        self._watch_task: asyncio.Task | None = None

    def ops(self, platform: PlatformDescriptor) -> AccountOps:
        with self._lock:
            found = self._ops.get(platform.id)
            if found is None:
                try:
                    module = platform_hook(platform, HOOK_ACCOUNT_OPS)
                except MissingPlatformHook:
                    raise HTTPException(status_code=404, detail=name_with(platform.display_name, "没有账号运维")) from None
                found = self._ops[platform.id] = module.account_ops(self._root)
            return found

    def watch(self, platform: PlatformDescriptor) -> AccountWatch:
        with self._lock:
            found = self._watches.get(platform.id)
            if found is None:
                found = self._watches[platform.id] = AccountWatch()
            return found

    def watched_platforms(self) -> tuple[PlatformDescriptor, ...]:
        """有账号运维钩子的平台。目前只有 QQ 声明了，别的平台加了钩子就自动进来。"""
        found = []
        for platform in all_platforms():
            try:
                platform_hook(platform, HOOK_ACCOUNT_OPS)
            except MissingPlatformHook:
                continue
            found.append(platform)
        return tuple(found)

    async def check_once(self, platform: PlatformDescriptor, *, now: float | None = None) -> str:
        """查一轮并记审计。返回写了的事件名，便于测试和日志。"""
        stamp = time.time() if now is None else now
        watch = self.watch(platform)
        try:
            ops = self.ops(platform)
        except HTTPException:
            return ""
        if not ops.configured:
            return ""
        try:
            status = await ops.status()
            online: bool | None = bool(status.get("online"))
            error = ""
        except AccountUnreachable as exc:
            online, error = None, str(exc)
        except AccountOpsError as exc:
            online, error = None, str(exc)
        event = watch.observe(online=online, error=error, now=stamp)
        if event and self._state is not None:
            _audit(
                self._state, event, platform,
                offline_since=watch.offline_since, error=error,
                offline_reason=str(status.get("offline_reason") or "") if online is not None else "",
            )
            _log.warning("%s 账号%s", platform.display_name, "恢复在线" if online else "不在线")
        return event

    async def _watch_forever(self) -> None:
        while True:
            for platform in self.watched_platforms():
                try:
                    await self.check_once(platform)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _log.debug("账号兜底轮询失败：%s", platform.id, exc_info=True)
            await asyncio.sleep(WATCH_INTERVAL_SEC)

    def start(self) -> None:
        if self._state is None or (self._watch_task is not None and not self._watch_task.done()):
            return
        self._watch_task = asyncio.get_running_loop().create_task(self._watch_forever())

    async def stop(self) -> None:
        task, self._watch_task = self._watch_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _refusal(exc: AccountOpsError) -> HTTPException:
    if isinstance(exc, AccountRequestInvalid):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, AccountConflict):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=502, detail=str(exc))


def _audit(state: SupervisorState, type_: str, platform: PlatformDescriptor, **payload: Any) -> None:
    state.eventlog.append(
        session_id="", component="supervisor", type_=type_, payload={"platform": platform.id, **payload},
    )


async def account_status(ops: AccountOps, watch: AccountWatch | None = None) -> dict[str, Any]:
    extra = {"watch": watch.payload()} if watch is not None else {}
    if not ops.configured:
        return {"configured": False, **extra}
    try:
        return {"configured": True, "reachable": True, **await ops.status(), **extra}
    except AccountUnreachable as exc:
        # 换号重启和首次部署都会经过这里。判成 502 的话，页面上什么都渲染不出来，
        # 连「正在重启」都说不了 —— 而这恰恰是最需要告诉人的时候。
        return {"configured": True, "reachable": False, "reason": str(exc), **extra}
    except AccountOpsError as exc:
        raise _refusal(exc) from exc


def _remember_account(state: SupervisorState, platform: PlatformDescriptor, account: Any) -> dict[str, str]:
    """记进实例清单，别的实例据此拦着同一个号双登。记不上不影响已经切成的号，但要随响应告诉人。"""
    text = str(account or "").strip()
    try:
        record_account(state.workspace_root, prefix=url_prefix(), platform_id=platform.id, account=text)
    except (OSError, InstanceDirectoryError) as exc:
        if isinstance(exc, InstanceDirectoryError):
            reason = exc.reason
        else:
            # 系统报的写错误连着绝对路径，只留原因；代码自己抛的照原话
            reason = config_layout.config_error_text(exc) if exc.strerror else str(exc)
        _log.warning("实例清单没记上 %s 的新账号 %s：%s", platform.id, text, reason)
        return {"roster_error": f"号已经换上，但没记进实例清单，别的实例拦不住同一个号重复登录：{reason}"}
    return {}


async def switch_account(
    state: SupervisorState, platform: PlatformDescriptor, ops: AccountOps, account: str,
) -> dict[str, Any]:
    target = normalize_id(account, platform.id_shape)
    try:
        owner = account_owner(state.workspace_root, platform.id, target, prefix=url_prefix())
    except InstanceDirectoryError as exc:
        raise HTTPException(
            status_code=503, detail=f"查不了 {target} 是不是已经登在别的实例上，没有换号：{exc.reason}",
        ) from exc
    if owner is not None:
        # 同一个号两边登，会互相把对方踢下线，数据也会分成两份。
        raise HTTPException(status_code=409, detail=f"{target} 归实例 {owner['label']} 管，去它的控制台登录")
    try:
        result = await ops.switch(account)
    except AccountOpsError as exc:
        raise _refusal(exc) from exc
    # 换号等于换掉 bot 的身份，出事时必须查得到是谁在什么时候换的。
    _audit(state, "platform_account_switched", platform, account=result.get("account"), pinned=result.get("pinned"))
    return {**result, **_remember_account(state, platform, result.get("account"))}


async def login_qrcode(ops: AccountOps) -> dict[str, Any]:
    try:
        qrcode = await ops.login_qrcode()
    except AccountUnreachable as exc:
        return {"qrcode": "", "reachable": False, "reason": str(exc)}
    except AccountOpsError as exc:
        raise _refusal(exc) from exc
    return {"qrcode": qrcode, "reachable": True}


async def relogin(state: SupervisorState, platform: PlatformDescriptor, ops: AccountOps) -> dict[str, Any]:
    # 先记后做：重启卡在半路也查得到有人要过换号。
    _audit(state, "platform_account_relogin_requested", platform)
    try:
        await ops.relogin()
    except AccountOpsError as exc:
        raise _refusal(exc) from exc
    return {"ok": True}


async def pin_account(state: SupervisorState, platform: PlatformDescriptor, ops: AccountOps) -> dict[str, Any]:
    try:
        account = await ops.pin()
    except AccountOpsError as exc:
        raise _refusal(exc) from exc
    # 扫码登上的号只有这里说得清是哪个：控制台见到登录成功就来钉自动登录。
    return {"ok": True, "account": account, **_remember_account(state, platform, account)}


def scope_migration() -> ModuleType:
    """按文件加载迁移脚本：deploy/ 不是包；摘要算法和 bot 跑的是同一份。"""
    cached = sys.modules.get(_MIGRATION_MODULE)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(_MIGRATION_MODULE, _MIGRATION_SCRIPT)
    if spec is None or spec.loader is None:
        raise HTTPException(status_code=500, detail="迁移脚本不可用")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MIGRATION_MODULE] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # 留着半截模块，下一次就会拿它去搬。
        sys.modules.pop(_MIGRATION_MODULE, None)
        raise
    return module


def scope_platform(platform_id: str) -> PlatformDescriptor:
    platform = resolve_platform(platform_id)
    migration = scope_migration()
    try:
        return migration.check_platform(platform)
    except migration.UnsupportedPlatform as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def bot_liveness(state: SupervisorState, platform: PlatformDescriptor) -> tuple[bool, str]:
    """(bot 是否可能还在跑, 当前登录的号)。搬迁必须在 bot 停掉之后做，拿不准就当它还在跑。"""
    status = read_live_status(platform, state.workspace_root)
    return status.may_be_running, status.account


def _scope_count_rows(scopes: Mapping[str, int]) -> list[dict[str, Any]]:
    return [{"scope": scope, "conversations": count} for scope, count in scopes.items()]


def _scope_plan_rows(report: Any) -> list[dict[str, Any]]:
    return [
        {
            "old_namespace": rename.old_ns,
            "new_namespace": rename.new_ns,
            "has_memory": rename.dir_exists,
            "blocked": rename.blocked,
        }
        for rename in report.changed_renames
    ]


def scope_status(
    state: SupervisorState, platform: PlatformDescriptor, target: str, source: str | None,
) -> dict[str, Any]:
    """当前账号归属，以及把 source（不给就自动认）名下的数据搬到 target 会动哪些东西的预览。"""
    migration = scope_migration()
    bot_alive, live_scope = bot_liveness(state, platform)
    payload: dict[str, Any] = {
        "current_scope": migration.recorded_scope(state.workspace_root, platform),
        "live_scope": live_scope,
        "bot_alive": bot_alive,
        "preview": None,
        "ambiguous_sources": [],
    }
    if not target.strip():
        return payload
    try:
        report = migration.run_scope_migration(
            workspace=state.workspace_root, target_scope=target, source_scope=source, apply=False,
            live_sessions=state.rekey_session_registry, platform=platform,
        )
    except migration.AmbiguousSource as exc:
        # 让人从盘上实际有的几个账号里挑一个，再带着 source 重新预览。
        payload["ambiguous_sources"] = _scope_count_rows(exc.scopes)
        return payload
    except (migration.ScopeMismatch, RekeyConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConversationStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    payload["preview"] = {
        "source_scope": report.source_scope,
        "target_scope": report.target_scope,
        "conversations": _scope_plan_rows(report),
        "unknown_namespaces": report.unknown_conv_dirs,
        "unresolved_namespaces": [item.old_ns for item in report.unresolved],
        "other_scopes": _scope_count_rows(report.other_scopes),
        "stores": store_counts_payload(report.stores),
    }
    return payload


def _hit_cache_problem(state: SupervisorState, report: Any) -> str:
    """搬了记忆目录、命中记录却读不了时的原因：里面的 key 没跟着改名，修好之前它们对不上搬过去的记忆。"""
    if not report.moved_dirs or report.hit_keys_rewritten:
        return ""
    # 迁移脚本的报告里没有这一项：搬完紧接着再读一次，读不了就是改名那一步也跳过了。
    try:
        read_hit_cache(state.workspace_root)
    except HitCacheUnreadable as exc:
        return exc.reason
    return ""


def migrate_scope(
    state: SupervisorState, platform: PlatformDescriptor, request: Mapping[str, Any],
) -> dict[str, Any]:
    """把全部会话与长期记忆搬到另一个 bot 账号名下。"""
    migration = scope_migration()
    bot_alive, _live_scope = bot_liveness(state, platform)
    if bot_alive:
        raise HTTPException(
            status_code=409,
            detail="bot 进程还在运行。它内存里存着按旧账号算出的会话键，搬迁会被它写回去 —— 请先停掉 bot。",
        )
    try:
        report = migration.run_scope_migration(
            workspace=state.workspace_root,
            target_scope=str(request.get("target") or ""),
            source_scope=request.get("source"),
            apply=True,
            live_sessions=state.rekey_session_registry,
            platform=platform,
        )
    except (migration.ScopeMismatch, RekeyConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConversationStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    reloaded = state.reload_session_registry()
    hit_cache_problem = _hit_cache_problem(state, report)
    if hit_cache_problem:
        _log.warning(
            "%s 的账号搬迁做完了，记忆命中记录读不了、没跟着改名（%s）；修好它时照这份对照把旧前缀换掉：%s",
            platform.id, hit_cache_problem, ", ".join(f"{old} -> {new}" for old, new in report.moved_dirs),
        )
    _audit(
        state, "platform_scope_migrated", platform,
        source_scope=report.source_scope, target_scope=report.target_scope,
        moved=len(report.moved_dirs), sessions=report.sessions_changed,
        hit_cache_problem=hit_cache_problem,
    )
    return {
        "ok": True,
        "source_scope": report.source_scope,
        "target_scope": report.target_scope,
        "moved_memory_dirs": len(report.moved_dirs),
        "hit_keys_rewritten": report.hit_keys_rewritten,
        "hit_cache_problem": hit_cache_problem,
        "route_keys_changed": report.route_keys_changed,
        "sessions_changed": report.sessions_changed,
        "sessions_reloaded": reloaded,
        "stores": store_counts_payload(report.stores),
        "skipped": [
            {"old_namespace": rename.old_ns, "new_namespace": rename.new_ns}
            for rename in report.blocked
        ],
        "unknown_namespaces": report.unknown_conv_dirs,
        "unresolved_namespaces": [item.old_ns for item in report.unresolved],
        "backup_dir": str(report.backup_dir) if report.backup_dir else "",
    }


def create_platform_account_router(state: SupervisorState, accounts: PlatformAccounts) -> APIRouter:
    router = APIRouter(dependencies=[Depends(verify_admin_token)])

    def resolve(platform_id: str) -> tuple[PlatformDescriptor, AccountOps]:
        platform = resolve_platform(platform_id)
        return platform, accounts.ops(platform)

    @router.get("/{platform_id}/account")
    async def get_account(platform_id: str) -> dict[str, Any]:
        platform, ops = resolve(platform_id)
        return await account_status(ops, accounts.watch(platform))

    @router.post("/{platform_id}/account/switch")
    async def post_switch(platform_id: str, body: AccountSwitchIn) -> dict[str, Any]:
        platform, ops = resolve(platform_id)
        return await switch_account(state, platform, ops, body.account)

    @router.post("/{platform_id}/account/qrcode")
    async def post_qrcode(platform_id: str) -> dict[str, Any]:
        _, ops = resolve(platform_id)
        return await login_qrcode(ops)

    @router.post("/{platform_id}/account/relogin")
    async def post_relogin(platform_id: str) -> dict[str, Any]:
        platform, ops = resolve(platform_id)
        return await relogin(state, platform, ops)

    @router.post("/{platform_id}/account/pin")
    async def post_pin(platform_id: str) -> dict[str, Any]:
        platform, ops = resolve(platform_id)
        return await pin_account(state, platform, ops)

    @router.get("/{platform_id}/account/scope")
    async def get_scope(
        platform_id: str, target: str = Query(""), source: str | None = Query(None),
    ) -> dict[str, Any]:
        return scope_status(state, scope_platform(platform_id), target, source)

    @router.post("/{platform_id}/account/scope/migrate")
    async def post_migrate(platform_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return migrate_scope(state, scope_platform(platform_id), payload)

    return router


def _named_uin(payload: Mapping[str, Any]) -> dict[str, Any]:
    """老接口把账号叫 uin，可切换的号列表里也是。"""
    renamed = {("uin" if key == "account" else key): value for key, value in payload.items()}
    targets = renamed.get("quick_login")
    if isinstance(targets, list):
        renamed["quick_login"] = [_named_uin(item) if isinstance(item, Mapping) else item for item in targets]
    return renamed


def create_legacy_qq_account_router(state: SupervisorState, accounts: PlatformAccounts) -> APIRouter:
    """改路径之前的 QQ 专用路由。

    没刷新的旧控制台和照文档写的运维脚本还在调这几条，行为与 /v1/platforms/qq/account… 一致，账号字段照旧叫 uin。
    """
    router = APIRouter(dependencies=[Depends(verify_admin_token)])

    @router.get("/v1/qq/account")
    async def get_account() -> dict[str, Any]:
        return _named_uin(await account_status(accounts.ops(QQ_NAPCAT), accounts.watch(QQ_NAPCAT)))

    @router.post("/v1/qq/account/quick-login")
    async def post_quick_login(body: QqQuickLoginIn) -> dict[str, Any]:
        return _named_uin(await switch_account(state, QQ_NAPCAT, accounts.ops(QQ_NAPCAT), body.uin))

    @router.post("/v1/qq/account/qrcode")
    async def post_qrcode() -> dict[str, Any]:
        return await login_qrcode(accounts.ops(QQ_NAPCAT))

    @router.post("/v1/qq/account/relogin")
    async def post_relogin() -> dict[str, Any]:
        return await relogin(state, QQ_NAPCAT, accounts.ops(QQ_NAPCAT))

    @router.post("/v1/qq/account/pin")
    async def post_pin() -> dict[str, Any]:
        return _named_uin(await pin_account(state, QQ_NAPCAT, accounts.ops(QQ_NAPCAT)))

    @router.get("/v1/admin/qq/scope")
    async def get_scope(target: str = Query(""), source: str | None = Query(None)) -> dict[str, Any]:
        return scope_status(state, QQ_NAPCAT, target, source)

    @router.post("/v1/admin/qq/scope/migrate")
    async def post_migrate(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return migrate_scope(state, QQ_NAPCAT, payload)

    return router
