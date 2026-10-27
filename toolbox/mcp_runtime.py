from __future__ import annotations

import asyncio
import atexit
import base64
import binascii
import contextvars
import dataclasses
import hashlib
import itertools
import json
import logging
import os
import threading
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar

import yaml

import config_layout
from clonoth_runtime import parse_bool, resolve_env_ref, switch_arg

logger = logging.getLogger(__name__)

_MCP_IMPORT_ERROR: Exception | None = None

try:
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.sse import sse_client
    from mcp.client.stdio import stdio_client
    from mcp.shared.exceptions import McpError
    from mcp.types import CONNECTION_CLOSED

    try:
        from mcp.client.streamable_http import streamable_http_client
    except Exception:
        from mcp.client.streamable_http import streamablehttp_client as streamable_http_client
except Exception as e:  # pragma: no cover - depends on optional dependency at runtime
    _MCP_IMPORT_ERROR = e
    anyio = None  # type: ignore[assignment]
    ClientSession = None  # type: ignore[assignment]
    StdioServerParameters = None  # type: ignore[assignment]
    stdio_client = None  # type: ignore[assignment]
    sse_client = None  # type: ignore[assignment]
    streamable_http_client = None  # type: ignore[assignment]
    McpError = None  # type: ignore[assignment,misc]
    CONNECTION_CLOSED = None  # type: ignore[assignment]


_CONFIG_REL_PATH = config_layout.MCP_FILE
_CLIENT_ID_RE = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_MCP_IMAGE_INLINE_DATA_THRESHOLD = 10000
_MCP_IMAGE_MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
    "image/tiff": ".tiff",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/avif": ".avif",
}
# 空闲会话攥着子进程或长连接，闲置超过这么久就关
_SESSION_IDLE_SEC = 300.0
# 一个作用域（一个对话，或共享作用域）里一个 client 最多常驻几个会话；都在忙时另开一个用完即关的，不排队等，卡住的调用只占住它自己那个会话
_MAX_POOLED_SESSIONS_PER_SCOPE = 4
# 一个事件循环里一个 client 跨所有作用域最多常驻几个会话，对话再多常驻进程也有个头；满了先关最久没用的空闲会话
_MAX_POOLED_SESSIONS_PER_CLIENT = 8
# 退役、退出时最多等旧会话关这么久；只是不再干等，关闭本身照常在后台走完
_SESSION_CLOSE_WAIT_SEC = 10.0
# SDK 等应答超时报的码（httpx 的 408）。这里不设读超时碰不到，真碰到了请求还悬在服务端，会话不能再借
_SDK_TIMEOUT_CODE = 408


def _ensure_sdk() -> None:
    if _MCP_IMPORT_ERROR is not None:
        raise RuntimeError(f"MCP Python SDK 未安装或导入失败：{_MCP_IMPORT_ERROR}")


def _config_path(workspace_root: Path) -> Path:
    return config_layout.mcp_path(workspace_root)


def _default_config() -> dict[str, Any]:
    return {"version": 1, "clients": {}}


def _normalize_transport(v: Any) -> str:
    s = str(v or "stdio").strip().lower().replace("-", "_")
    if s in {"streamablehttp", "streamable_http", "http", "streamable"}:
        return "streamable_http"
    if s in {"sse"}:
        return "sse"
    return "stdio"


def _normalize_client_id(client_id: Any) -> str:
    s = str(client_id or "").strip()
    if not s:
        raise ValueError("empty client id")
    if not _CLIENT_ID_RE.fullmatch(s):
        raise ValueError("invalid client id: only [A-Za-z0-9][A-Za-z0-9_-]{0,63} is allowed")
    return s


def _load_raw_config(workspace_root: Path) -> dict[str, Any]:
    p = _config_path(workspace_root)
    if not p.exists():
        return _default_config()

    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception:
        return _default_config()

    if not isinstance(data, dict):
        return _default_config()

    raw_clients = data.get("clients")
    clients: dict[str, Any] = {}

    if isinstance(raw_clients, dict):
        for cid, spec in raw_clients.items():
            if isinstance(cid, str) and isinstance(spec, dict):
                clients[cid] = dict(spec)
    elif isinstance(raw_clients, list):
        for item in raw_clients:
            if not isinstance(item, dict):
                continue
            cid = item.get("id")
            if isinstance(cid, str) and cid.strip():
                copied = dict(item)
                copied.pop("id", None)
                clients[cid.strip()] = copied

    return {
        "version": int(data.get("version") or 1),
        "clients": clients,
    }


def _save_raw_config(workspace_root: Path, cfg: dict[str, Any]) -> None:
    """调用方已经拿着这份配置的锁：读改写之间别的实例插进来改的那笔会被整份覆盖掉。"""
    p = _config_path(workspace_root)
    text = yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True)
    config_layout.write_locked(p, text, mode=config_layout.private_mode_for(workspace_root, p))


def _normalize_public_spec(client_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    transport = _normalize_transport(spec.get("transport"))
    out: dict[str, Any] = {
        "id": client_id,
        "enabled": bool(parse_bool(spec.get("enabled"), True)),
        "share_sessions": bool(parse_bool(spec.get("share_sessions"), False)),
        "transport": transport,
    }

    description = spec.get("description")
    if isinstance(description, str) and description.strip():
        out["description"] = description.strip()

    if transport == "stdio":
        out["command"] = str(spec.get("command") or "")
        args = spec.get("args")
        out["args"] = [str(x) for x in args] if isinstance(args, list) else []
        env = spec.get("env")
        out["env"] = {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}
    else:
        out["url"] = str(spec.get("url") or "")
        headers = spec.get("headers")
        out["headers"] = {str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {}

    return out


def list_clients(workspace_root: Path) -> list[dict[str, Any]]:
    cfg = _load_raw_config(workspace_root)
    clients = cfg.get("clients") or {}
    if not isinstance(clients, dict):
        return []

    out: list[dict[str, Any]] = []
    for client_id in sorted(clients.keys()):
        spec = clients.get(client_id)
        if isinstance(spec, dict):
            out.append(_normalize_public_spec(client_id, spec))
    return out


def get_client_spec(workspace_root: Path, client_id: str) -> dict[str, Any]:
    cid = _normalize_client_id(client_id)
    cfg = _load_raw_config(workspace_root)
    clients = cfg.get("clients") or {}
    if not isinstance(clients, dict):
        raise ValueError(f"client not found: {cid}")
    spec = clients.get(cid)
    if not isinstance(spec, dict):
        raise ValueError(f"client not found: {cid}")
    return _normalize_public_spec(cid, spec)


def requested_switches(spec: dict[str, Any]) -> tuple[bool, bool | None]:
    """保存客户端时给的 enabled 和 share_sessions；share_sessions 没写是 None。认不出的值抛 SwitchValueError。"""
    return switch_arg(spec, "enabled", True), switch_arg(spec, "share_sessions", None)


def _shares_sessions(requested: bool | None, previous: Any) -> bool:
    """没传 share_sessions 就沿用文件里原来的值，传了才改。"""
    if requested is None:
        return isinstance(previous, dict) and bool(parse_bool(previous.get("share_sessions"), False))
    return requested


def upsert_client(workspace_root: Path, spec: dict[str, Any]) -> dict[str, Any]:
    cid = _normalize_client_id(spec.get("id"))
    transport = _normalize_transport(spec.get("transport"))
    enabled, share_sessions = requested_switches(spec)
    description = str(spec.get("description") or "").strip()

    connection: dict[str, Any]
    if transport == "stdio":
        command = str(spec.get("command") or "").strip()
        if not command:
            raise ValueError("stdio client requires non-empty command")
        args = spec.get("args")
        env = spec.get("env")
        connection = {
            "command": command,
            "args": [str(x) for x in args] if isinstance(args, list) else [],
            "env": {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {},
        }
    else:
        url = str(spec.get("url") or "").strip()
        if not url:
            raise ValueError(f"{transport} client requires non-empty url")
        headers = spec.get("headers")
        connection = {
            "url": url,
            "headers": {str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {},
        }

    with config_layout.locked(_config_path(workspace_root)):
        cfg = _load_raw_config(workspace_root)
        clients = cfg.get("clients")
        if not isinstance(clients, dict):
            clients = {}
            cfg["clients"] = clients
        record: dict[str, Any] = {
            "transport": transport,
            "enabled": enabled,
        }
        # 只在为真时写进文件：默认按对话隔离，文件里不出现这个键
        if _shares_sessions(share_sessions, clients.get(cid)):
            record["share_sessions"] = True
        if description:
            record["description"] = description
        record.update(connection)
        clients[cid] = record
        _save_raw_config(workspace_root, cfg)
    _retire_client_sessions(workspace_root, cid)
    return _normalize_public_spec(cid, record)


def delete_client(workspace_root: Path, client_id: str) -> bool:
    cid = _normalize_client_id(client_id)
    with config_layout.locked(_config_path(workspace_root)):
        cfg = _load_raw_config(workspace_root)
        clients = cfg.get("clients")
        if not isinstance(clients, dict) or cid not in clients:
            return False
        clients.pop(cid, None)
        _save_raw_config(workspace_root, cfg)
    _retire_client_sessions(workspace_root, cid)
    return True


def _to_plain(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_plain(v) for v in obj]
    if dataclasses.is_dataclass(obj):
        return _to_plain(dataclasses.asdict(obj))
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        try:
            return _to_plain(model_dump(mode="json"))
        except TypeError:
            return _to_plain(model_dump())
        except Exception:
            pass
    dict_method = getattr(obj, "dict", None)
    if callable(dict_method):
        try:
            return _to_plain(dict_method())
        except Exception:
            pass
    if hasattr(obj, "__dict__"):
        try:
            return _to_plain(vars(obj))
        except Exception:
            pass
    return str(obj)


def _field(obj: Any, *names: str) -> Any:
    """Read a field from SDK objects or already-plain dictionaries."""
    # Why: MCP SDK content parts may be Pydantic objects, dataclasses, or plain
    # dictionaries depending on SDK version and transport. How: try dict keys and
    # attributes without first converting the full result. Purpose: save large
    # image data before generic serialization can alter or drop SDK-specific
    # fields.
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj.get(name)
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


def _image_extension_from_mime(mime_type: Any) -> str:
    """Return a safe file extension for an MCP image MIME type."""
    # Why: saved MCP image attachments need stable names that downstream clients
    # can open. How: normalize MIME types and use a conservative allow-list.
    # Purpose: avoid writing ambiguous or path-like extensions from untrusted
    # tool output.
    mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    return _MCP_IMAGE_MIME_EXTENSIONS.get(mime, ".bin")


def _save_large_mcp_image_part(workspace_root: Path, client_id: str, part: Any, attachments: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Save one large MCP image content part and return its text replacement."""
    # Why: some MCP servers return multi-megabyte base64 image content, which can
    # overflow conversation context when preserved inline. How: only intercept
    # image parts whose encoded data is above the configured threshold, decode
    # them, save them under data/attachments/mcp/{client_id}, and emit a short
    # text marker. Purpose: keep the context small while preserving the generated
    # image through the existing attachment collection pipeline.
    if _field(part, "type") != "image":
        return None

    data = _field(part, "data")
    if not isinstance(data, str) or len(data) <= _MCP_IMAGE_INLINE_DATA_THRESHOLD:
        return None

    try:
        image_bytes = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return None

    safe_client_id = _normalize_client_id(client_id)
    ext = _image_extension_from_mime(_field(part, "mimeType", "mime_type", "mime"))
    filename = f"{hashlib.md5(image_bytes).hexdigest()[:12]}{ext}"
    attachment_dir = workspace_root / "data" / "attachments" / "mcp" / safe_client_id
    attachment_dir.mkdir(parents=True, exist_ok=True)
    attachment_path = attachment_dir / filename
    if not attachment_path.exists():
        attachment_path.write_bytes(image_bytes)

    relative_path = attachment_path.relative_to(workspace_root).as_posix()
    attachments.append({"path": relative_path, "name": filename, "type": "image"})
    return {
        "type": "text",
        "text": f"[Image saved to disk: {relative_path} ({len(image_bytes)} bytes)]",
    }


def _postprocess_mcp_content(workspace_root: Path, client_id: str, content: Any) -> tuple[list[Any], list[dict[str, Any]], bool]:
    """Convert MCP content parts while spilling oversized image data to disk."""
    # Why: call_tool must return plain JSON-compatible data, but image spilling
    # must happen before the large base64 field is exposed to conversation
    # history. How: walk the original content sequence, replace only qualifying
    # image parts, and serialize every other part with _to_plain. Purpose: keep
    # small icons and non-image content unchanged while protecting context size.
    if not isinstance(content, (list, tuple)):
        return [], [], False

    attachments: list[dict[str, Any]] = []
    processed: list[Any] = []
    for part in content:
        replacement = _save_large_mcp_image_part(workspace_root, client_id, part, attachments)
        processed.append(replacement if replacement is not None else _to_plain(part))
    return processed, attachments, True


def _to_plain_mcp_result(workspace_root: Path, client_id: str, result: Any) -> tuple[Any, list[dict[str, Any]]]:
    """Serialize an MCP tool result after replacing large image parts."""
    # Why: the generic _to_plain helper intentionally preserves arbitrary fields,
    # including huge image data. How: first inspect the SDK result's original
    # content list and save large image parts, then patch the serialized result's
    # content field with the processed list. Purpose: guarantee the final tool
    # result contains paths instead of large inline base64 payloads.
    processed_content, attachments, has_content = _postprocess_mcp_content(
        workspace_root,
        client_id,
        _field(result, "content"),
    )
    plain = _to_plain(result)

    if isinstance(plain, dict):
        if has_content:
            plain["content"] = processed_content
        elif isinstance(plain.get("content"), list):
            processed_content, attachments, _ = _postprocess_mcp_content(
                workspace_root,
                client_id,
                plain.get("content"),
            )
            plain["content"] = processed_content

    return plain, attachments


def _normalize_tool_entry(tool: Any) -> dict[str, Any]:
    raw = _to_plain(tool)
    if not isinstance(raw, dict):
        return {"name": str(raw), "description": "", "input_schema": {"type": "object", "properties": {}, "required": []}}

    name = str(raw.get("name") or "").strip()
    description = str(raw.get("description") or "").strip()
    input_schema = raw.get("inputSchema")
    if not isinstance(input_schema, dict):
        input_schema = raw.get("input_schema")
    if not isinstance(input_schema, dict):
        input_schema = {"type": "object", "properties": {}, "required": []}
    entry: dict[str, Any] = {"name": name, "description": description, "input_schema": input_schema}
    annotations = raw.get("annotations")
    if isinstance(annotations, dict):
        # SDK 把服务端没给的提示也序列化成 None，只留真给了的
        hints = {str(key): value for key, value in annotations.items() if value is not None}
        if hints:
            entry["annotations"] = hints
    return entry


_T = TypeVar("_T")


@dataclasses.dataclass(frozen=True)
class _LaunchPlan:
    """按当前配置解析好的启动参数。指纹算的是解析后的值，引用的密钥换了也算配置变了。"""

    key: tuple[str, str]
    client_id: str
    transport: str
    fingerprint: str
    share_sessions: bool = False
    command: str = ""
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = dataclasses.field(default=(), repr=False)
    url: str = ""
    headers: tuple[tuple[str, str], ...] = dataclasses.field(default=(), repr=False)


@dataclasses.dataclass(frozen=True)
class _Connection:
    session: Any
    read_stream: Any = None
    write_stream: Any = None


def _root_key(workspace_root: Path) -> str:
    return os.path.normcase(os.path.abspath(str(workspace_root)))


def _pool_key(workspace_root: Path, client_id: str) -> tuple[str, str]:
    return _root_key(workspace_root), _normalize_client_id(client_id)


def _launch_plan(workspace_root: Path, client_id: str) -> _LaunchPlan:
    spec = get_client_spec(workspace_root, client_id)
    cid = str(spec["id"])
    if not bool(spec.get("enabled", True)):
        raise RuntimeError(f"MCP client is disabled: {cid}")

    transport = str(spec.get("transport") or "stdio")
    share_sessions = bool(spec.get("share_sessions"))
    fields: dict[str, Any]
    if transport == "stdio":
        command = str(spec.get("command") or "").strip()
        if not command:
            raise RuntimeError("stdio client missing command")
        fields = {
            "command": command,
            "args": tuple(str(x) for x in spec.get("args") or []),
            "env": tuple(sorted((str(k), resolve_env_ref(v)) for k, v in (spec.get("env") or {}).items())),
        }
    else:
        url = str(spec.get("url") or "").strip()
        if not url:
            raise RuntimeError(f"{transport} client missing url")
        fields = {
            "url": url,
            "headers": tuple(sorted((str(k), resolve_env_ref(v)) for k, v in (spec.get("headers") or {}).items())),
        }
    # 共享与否也进指纹：切换后旧会话一律退役，里面攒下的状态不带进新的分法
    material = json.dumps([transport, share_sessions, fields], ensure_ascii=False, sort_keys=True)
    return _LaunchPlan(
        key=_pool_key(workspace_root, cid),
        client_id=cid,
        transport=transport,
        fingerprint=hashlib.sha256(material.encode("utf-8")).hexdigest(),
        share_sessions=share_sessions,
        **fields,
    )


def _transport_streams(transport_obj: Any, label: str) -> tuple[Any, Any]:
    if not isinstance(transport_obj, tuple) or len(transport_obj) < 2:
        raise RuntimeError(f"invalid {label} transport object")
    return transport_obj[0], transport_obj[1]


@asynccontextmanager
async def _connect(plan: _LaunchPlan) -> AsyncIterator[_Connection]:
    """建连并完成 initialize。进出必须在同一个 task 里：SDK 的 anyio task group 不许换 task 退出。"""
    _ensure_sdk()
    async with AsyncExitStack() as stack:
        if plan.transport == "stdio":
            env = os.environ.copy()
            env.update(plan.env)
            read_stream, write_stream = await stack.enter_async_context(
                stdio_client(StdioServerParameters(command=plan.command, args=list(plan.args), env=env))
            )
        elif plan.transport == "sse":
            transport_obj = await stack.enter_async_context(sse_client(plan.url, headers=dict(plan.headers)))
            read_stream, write_stream = _transport_streams(transport_obj, "SSE")
        else:
            options: dict[str, Any] = {}
            if plan.headers:
                import httpx

                # 传进去的客户端 SDK 不负责关，跟着会话一起关
                options["http_client"] = await stack.enter_async_context(httpx.AsyncClient(headers=dict(plan.headers)))
            transport_obj = await stack.enter_async_context(streamable_http_client(plan.url, **options))
            read_stream, write_stream = _transport_streams(transport_obj, "streamable HTTP")

        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        yield _Connection(session=session, read_stream=read_stream, write_stream=write_stream)


def _stream_open(stream: Any) -> bool:
    # 传输层断开时会关掉 anyio 内存流的一端；没有统计接口的流按通着算
    statistics = getattr(stream, "statistics", None)
    if not callable(statistics):
        return True
    try:
        stats = statistics()
    except Exception:
        return True
    return getattr(stats, "open_send_streams", 1) > 0 and getattr(stats, "open_receive_streams", 1) > 0


def _unwrap_error(error: BaseException) -> BaseException:
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    return error


def _describe_error(error: BaseException) -> str:
    text = str(error).strip()
    name = type(error).__name__
    if not text:
        return name
    return text if name in text else f"{name}: {text}"


def _answered_error(error: BaseException) -> bool:
    """服务端按协议回了一条错误应答：这次请求有始有终，会话照常能用。连接断了、等应答超时是 SDK 在本地报的，不算。"""
    if McpError is None or not isinstance(error, McpError):
        return False
    code = getattr(getattr(error, "error", None), "code", None)
    return isinstance(code, int) and code not in (CONNECTION_CLOSED, _SDK_TIMEOUT_CODE)


def _settle(future: asyncio.Future, result: Any) -> None:
    if not future.done():
        future.set_result(result)


def _fail(future: asyncio.Future, error: BaseException) -> None:
    if not future.done():
        future.set_exception(error)


def _consume_outcome(future: asyncio.Future) -> None:
    # 放弃等待的一方不会再来取结果，先取掉，免得 asyncio 记一条“异常无人取回”
    if not future.cancelled():
        future.exception()


def _closed_error(client_id: str) -> RuntimeError:
    return RuntimeError(f"MCP session of client {client_id} was closed")


class _Job:
    __slots__ = ("request", "future", "cancel_scope", "started", "abandoned")

    def __init__(self, request: Callable[[Any], Awaitable[Any]], loop: asyncio.AbstractEventLoop) -> None:
        self.request = request
        self.future: asyncio.Future[Any] = loop.create_future()
        self.future.add_done_callback(_consume_outcome)
        self.cancel_scope: Any = None
        self.started = False
        self.abandoned = False


class _PooledSession:
    """一个 MCP 会话，只借给一个作用域。建连、发请求、关闭都在它自己的 owner task 里做，调用方只递请求、等结果。"""

    def __init__(self, pool: _ClientPool, plan: _LaunchPlan, scope: str, *, pooled: bool) -> None:
        self.pool = pool
        self.plan = plan
        self.scope = scope
        self.label = f"{plan.client_id}@{scope}" if scope else plan.client_id
        self.pooled = pooled
        self.loop = asyncio.get_running_loop()
        self.ready: asyncio.Future[None] = self.loop.create_future()
        self.ready.add_done_callback(_consume_outcome)
        self.connection: _Connection | None = None
        self.leased = True
        self.retired = False
        self.closing = False
        self.closing_since = 0.0
        self.last_used = 0
        self._jobs: asyncio.Queue[_Job | None] = asyncio.Queue()
        self._current: _Job | None = None
        self._idle_timer: asyncio.TimerHandle | None = None
        # 会话比发起它的那次调用活得久，用空白 context，不攥着那次调用的 contextvars（插件租约等）
        self.task = self.loop.create_task(
            self._serve(), name=f"mcp-session:{self.label}", context=contextvars.Context(),
        )
        self.task.add_done_callback(self._on_finished)

    async def _serve(self) -> None:
        try:
            async with _connect(self.plan) as connection:
                self.connection = connection
                _settle(self.ready, None)
                while not self.closing:
                    job = await self._jobs.get()
                    if job is None or not await self._run(job, connection.session):
                        break
        except Exception as exc:
            error = _unwrap_error(exc)
            if self.ready.done():
                logger.warning("MCP client %s: session closed after an error: %s", self.label, _describe_error(error))
            self._abort(error)
        except BaseException:
            self._abort(_closed_error(self.plan.client_id))
            raise
        else:
            self._abort(_closed_error(self.plan.client_id))

    async def _run(self, job: _Job, session: Any) -> bool:
        """在 owner task 里执行一个请求，返回会话还能不能接着用。"""
        if job.abandoned:
            return True
        if self.closing:
            # 关闭开始前就排进队、关闭后才轮到的请求不再发出；它已经出了队，_abort 收不到它
            _fail(job.future, _closed_error(self.plan.client_id))
            return False
        job.started = True
        self._current = job
        clean = False
        cancel_scope = anyio.CancelScope()
        job.cancel_scope = cancel_scope
        with cancel_scope:
            try:
                result = await job.request(session)
            except Exception as exc:
                _fail(job.future, exc)
                # 和调用方 _finish 用同一个判断，两边对会话去留的结论才一致
                clean = _answered_error(exc)
            else:
                _settle(job.future, result)
                clean = True
        # 走到这里还没结果，说明是调用方放弃、取消落在了本 scope 上；外层取消会直接越过这里，由 _abort 收尾
        _fail(job.future, RuntimeError("MCP call was cancelled"))
        job.cancel_scope = None
        self._current = None
        return clean

    def _abort(self, error: BaseException) -> None:
        """会话没了：还没就绪的、正在跑的、排着队的请求都按这个错误结束。"""
        _fail(self.ready, error)
        jobs = [self._current] if self._current is not None else []
        self._current = None
        while not self._jobs.empty():
            queued = self._jobs.get_nowait()
            if queued is not None:
                jobs.append(queued)
        for job in jobs:
            _fail(job.future, error)

    def _on_finished(self, task: asyncio.Task) -> None:
        self.closing = True
        self._cancel_idle_timer()
        # 任务在第一次执行前就被取消时 _serve 一行都没跑，等就绪的调用方只能靠这里放行
        self._abort(_closed_error(self.plan.client_id))
        self.pool.forget(self)
        if not task.cancelled():
            task.exception()

    def is_alive(self) -> bool:
        if self.closing or self.task.done() or self.connection is None:
            return False
        return _stream_open(self.connection.read_stream) and _stream_open(self.connection.write_stream)

    async def wait_ready(self) -> None:
        try:
            await asyncio.shield(self.ready)
        except BaseException:
            self.close()
            raise
        if not self.is_alive():
            self.close()
            raise RuntimeError(f"MCP session of client {self.plan.client_id} closed right after it started")

    async def run(self, request: Callable[[Any], Awaitable[_T]]) -> _T:
        if self.closing or self.task.done():
            self.close()
            raise _closed_error(self.plan.client_id)
        job = _Job(request, self.loop)
        self._jobs.put_nowait(job)
        try:
            result = await asyncio.shield(job.future)
        except asyncio.CancelledError:
            self._give_up(job)
            raise
        except BaseException:
            self._finish(job)
            raise
        self.release()
        return result

    def _finish(self, job: _Job) -> None:
        """请求有了结果之后的去留：成功或服务端的错误应答放回池子，其余错误说明会话状态不明，关掉。"""
        if not job.future.done() or job.future.cancelled():
            self.close()
            return
        error = job.future.exception()
        if error is None or _answered_error(error):
            self.release()
        else:
            self.close()

    def _give_up(self, job: _Job) -> None:
        """调用方被取消：请求还没交给 owner 就把会话放回去；已经在跑的连会话一起关，不复用状态不明的会话。"""
        if job.future.done():
            self._finish(job)
        elif job.started:
            self.close()
        else:
            job.abandoned = True
            self.release()

    def lease(self) -> None:
        self._cancel_idle_timer()
        self.leased = True

    def release(self) -> None:
        self.leased = False
        if self.closing:
            return
        if not self.pooled or self.retired or not self.is_alive():
            self.close()
            return
        self.last_used = next(self.pool.use_order)
        self._idle_timer = self.loop.call_later(_SESSION_IDLE_SEC, self._expire)
        self.pool.idle.setdefault(self.scope, []).append(self)

    def _expire(self) -> None:
        self._idle_timer = None
        if not self.leased and not self.closing:
            logger.debug("MCP client %s: closing a session idle for %ss", self.label, _SESSION_IDLE_SEC)
            self.close()

    def retire(self) -> None:
        """不再借出：空闲的现在关，正在用的等这次调用结束再关。"""
        self.retired = True
        if not self.leased:
            self.close()

    def close(self) -> None:
        """立即关闭，正在跑的请求一并中止。"""
        if self.closing:
            return
        self.closing = True
        self.closing_since = self.loop.time()
        self._cancel_idle_timer()
        self.pool.detach(self)
        if not self.ready.done():
            self.task.cancel()
            return
        if self._current is not None and self._current.cancel_scope is not None:
            self._current.cancel_scope.cancel()
        self._jobs.put_nowait(None)

    def _cancel_idle_timer(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None


class _ClientPool:
    """一个 client 在一个事件循环里的全部会话。sessions 是各作用域还活着的全部，idle 按作用域放着能借出的。"""

    def __init__(self, key: tuple[str, str]) -> None:
        self.key = key
        self.sessions: list[_PooledSession] = []
        self.idle: dict[str, list[_PooledSession]] = {}
        # 放回池子的先后用序号记：循环时钟在 Windows 上 15.6ms 才走一格，挨着放回的会话会记成同一刻，分不出谁最久没用
        self.use_order = itertools.count()

    async def acquire(self, plan: _LaunchPlan, scope: str) -> _PooledSession:
        # 配置一变，这个 client 所有作用域的旧会话都退役；连同保存、删除配置时退役的，还没关完的都先等它们关完
        self._retire_where(lambda session: session.plan.fingerprint != plan.fingerprint)
        await _wait_closed(self.closing_retired())
        session = self._take_idle(scope, plan.fingerprint) or self._open(plan, scope)
        try:
            await session.wait_ready()
            return session
        except Exception as exc:
            # 请求还没发出去，换个新会话再试一次是安全的
            logger.warning(
                "MCP client %s: session failed to start (%s), trying once more with a new one",
                session.label, _describe_error(exc),
            )
        session = self._take_idle(scope, plan.fingerprint) or self._open(plan, scope)
        await session.wait_ready()
        return session

    def _take_idle(self, scope: str, fingerprint: str) -> _PooledSession | None:
        waiting = self.idle.get(scope, [])
        try:
            while waiting:
                session = waiting.pop()
                if session.retired or session.plan.fingerprint != fingerprint or not session.is_alive():
                    session.close()
                    continue
                session.lease()
                return session
            return None
        finally:
            if not waiting:
                self.idle.pop(scope, None)

    def _open(self, plan: _LaunchPlan, scope: str) -> _PooledSession:
        session = _PooledSession(self, plan, scope, pooled=self._make_room(scope))
        self.sessions.append(session)
        return session

    def _resident(self, scope: str | None = None) -> int:
        return sum(
            1 for session in self.sessions
            if session.pooled and not session.retired and not session.closing and (scope is None or session.scope == scope)
        )

    def _make_room(self, scope: str) -> bool:
        """新会话能不能入池：本作用域没满，且全 client 没满；全 client 满了就关掉最久没用的空闲会话腾位置。"""
        self._drop_dead_idle()
        if self._resident(scope) >= _MAX_POOLED_SESSIONS_PER_SCOPE:
            return False
        if self._resident() < _MAX_POOLED_SESSIONS_PER_CLIENT:
            return True
        idle = [session for waiting in self.idle.values() for session in waiting]
        if not idle:
            return False
        oldest = min(idle, key=lambda session: session.last_used)
        logger.debug("MCP client %s: closing the least recently used idle session to make room", oldest.label)
        oldest.close()
        return True

    def _drop_dead_idle(self) -> None:
        # 服务端进程退出后 SDK 不收场，owner 还在等请求，只能看流认出来；留着白占名额，还会先挤掉活着的会话
        for session in [session for waiting in self.idle.values() for session in waiting if not session.is_alive()]:
            session.close()

    def _retire_where(self, predicate: Callable[[_PooledSession], bool]) -> None:
        for session in list(self.sessions):
            if not session.retired and predicate(session):
                session.retire()

    def closing_retired(self) -> list[asyncio.Task]:
        """退役后已经在关、还没关完的会话；正在用的那些等调用结束才关，不在其中。"""
        # 关了这么久还没关完的不再等：每次借出都要过这里，一个卡住的关闭不能让之后每次调用都干等一轮
        return [
            session.task for session in self.sessions
            if session.retired and session.closing and not session.task.done()
            and session.loop.time() - session.closing_since < _SESSION_CLOSE_WAIT_SEC
        ]

    def retire_all(self) -> list[asyncio.Task]:
        self._retire_where(lambda session: True)
        return self.closing_retired()

    def close_all(self) -> list[asyncio.Task]:
        tasks: list[asyncio.Task] = []
        for session in list(self.sessions):
            session.close()
            if not session.task.done():
                tasks.append(session.task)
        return tasks

    def detach(self, session: _PooledSession) -> None:
        waiting = self.idle.get(session.scope)
        if waiting is None:
            return
        if session in waiting:
            waiting.remove(session)
        # 作用域是对话的会话 id，空了就摘掉，不随对话越积越多
        if not waiting:
            del self.idle[session.scope]

    def forget(self, session: _PooledSession) -> None:
        self.detach(session)
        if session in self.sessions:
            self.sessions.remove(session)


class _LoopPool:
    """一个事件循环的会话池。会话里的 anyio 流和任务都绑在这个循环上，绝不拿到别的循环里用。"""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        # 只留弱引用：池子挂在以循环为键的 WeakKeyDictionary 里，强引用会让循环永远回收不掉
        self._loop_ref = weakref.ref(loop)
        self.clients: dict[tuple[str, str], _ClientPool] = {}

    def client(self, key: tuple[str, str]) -> _ClientPool:
        found = self.clients.get(key)
        if found is None:
            found = self.clients[key] = _ClientPool(key)
        return found

    def retire(self, root: str, client_id: str | None = None) -> list[asyncio.Task]:
        """这个工作区（或其中一个 client）所有作用域的会话一起退役。"""
        closing: list[asyncio.Task] = []
        for (pool_root, pool_client), pool in list(self.clients.items()):
            if pool_root == root and (client_id is None or pool_client == client_id):
                closing.extend(pool.retire_all())
        return closing

    async def aclose(self) -> None:
        tasks = [task for pool in list(self.clients.values()) for task in pool.close_all()]
        await _wait_closed(tasks)

    def drop_if_loop_closed(self) -> None:
        loop = self._loop_ref()
        if loop is None or loop.is_closed():
            self.clients.clear()

    def in_loop(self, callback: Callable[..., Any], *args: Any) -> None:
        """在池子自己的循环里执行：当前就在这个循环上就地执行，否则排进那个循环。"""
        loop = self._loop_ref()
        if loop is None or loop.is_closed():
            self.clients.clear()
            return
        if _running_loop() is loop:
            callback(*args)
            return
        try:
            loop.call_soon_threadsafe(callback, *args)
        except RuntimeError:
            self.clients.clear()

    def shutdown_blocking(self) -> None:
        loop = self._loop_ref()
        if loop is None or loop.is_closed():
            self.clients.clear()
            return
        if not any(pool.sessions for pool in list(self.clients.values())):
            return
        if loop.is_running():
            if _running_loop() is loop:
                return
            future = asyncio.run_coroutine_threadsafe(self.aclose(), loop)
            try:
                future.result(_SESSION_CLOSE_WAIT_SEC + 1.0)
            except Exception:
                future.cancel()
            return
        loop.run_until_complete(self.aclose())


_POOLS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _LoopPool] = weakref.WeakKeyDictionary()
_POOLS_LOCK = threading.Lock()


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _current_pool() -> _LoopPool:
    loop = asyncio.get_running_loop()
    with _POOLS_LOCK:
        pool = _POOLS.get(loop)
        if pool is None:
            for other in list(_POOLS.values()):
                other.drop_if_loop_closed()
            pool = _POOLS[loop] = _LoopPool(loop)
        return pool


def _existing_pool() -> _LoopPool | None:
    loop = _running_loop()
    if loop is None:
        return None
    with _POOLS_LOCK:
        return _POOLS.get(loop)


def _all_pools() -> list[_LoopPool]:
    with _POOLS_LOCK:
        return list(_POOLS.values())


async def _wait_closed(tasks: list[asyncio.Task]) -> None:
    # 先等旧会话退出再起新的：独占资源（浏览器配置目录之类）的服务，新旧进程同时在会撞车
    pending = {task for task in tasks if not task.done()}
    if pending:
        await asyncio.wait(pending, timeout=_SESSION_CLOSE_WAIT_SEC)


def session_scope(ctx: Any) -> str:
    """工具调用所在对话的会话作用域：对话的主会话，分支任务回落到父会话；拿不到时是共享作用域 ""。"""
    route = getattr(ctx, "route_session_id", None)
    if callable(route):
        return str(route() or "").strip()
    return str(getattr(ctx, "parent_session_id", "") or getattr(ctx, "session_id", "") or "").strip()


async def _with_session(
    workspace_root: Path, client_id: str, request: Callable[[Any], Awaitable[_T]], scope: str,
) -> _T:
    _ensure_sdk()
    try:
        plan = await asyncio.to_thread(_launch_plan, workspace_root, client_id)
    except Exception:
        _retire_unusable(workspace_root, client_id)
        raise
    # 有状态的服务（浏览器、记忆）按对话各开各的会话，免得一个对话的状态带进别的对话；配了 share_sessions 的才共用
    effective_scope = "" if plan.share_sessions else scope
    session = await _current_pool().client(plan.key).acquire(plan, effective_scope)
    return await session.run(request)


def _retire_unusable(workspace_root: Path, client_id: str) -> None:
    """配置已经起不了会话（删了、停用了、写坏了），这个 client 留在本循环里所有作用域的会话也别再借出。"""
    try:
        key = _pool_key(workspace_root, client_id)
    except ValueError:
        return
    pool = _existing_pool()
    client_pool = pool.clients.get(key) if pool is not None else None
    if client_pool is not None:
        client_pool.retire_all()


def _retire_client_sessions(workspace_root: Path, client_id: str) -> None:
    root = _root_key(workspace_root)
    cid = _normalize_client_id(client_id)
    for pool in _all_pools():
        pool.in_loop(pool.retire, root, cid)


async def retire_sessions(workspace_root: Path, client_id: str | None = None) -> None:
    """让会话退役：空闲的现在关（当前循环里的会等它们关完），正在用的等这次调用结束再关。不给 client_id 就是整个工作区。"""
    root = _root_key(workspace_root)
    cid = _normalize_client_id(client_id) if client_id is not None else None
    current = _existing_pool()
    closing: list[asyncio.Task] = []
    for pool in _all_pools():
        if pool is current:
            closing.extend(pool.retire(root, cid))
        else:
            pool.in_loop(pool.retire, root, cid)
    await _wait_closed(closing)


def _close_pools_at_exit() -> None:
    for pool in _all_pools():
        try:
            pool.shutdown_blocking()
        except Exception:
            logger.debug("closing MCP sessions at exit failed", exc_info=True)


atexit.register(_close_pools_at_exit)


@asynccontextmanager
async def open_session(workspace_root: Path, client_id: str):
    """不进池的一次性会话，退出即关。"""
    _ensure_sdk()
    plan = await asyncio.to_thread(_launch_plan, workspace_root, client_id)
    async with _connect(plan) as connection:
        yield connection.session


async def _request_tool_list(session: Any) -> Any:
    return await session.list_tools()


async def list_tools(workspace_root: Path, client_id: str, *, scope: str = "") -> dict[str, Any]:
    res = await _with_session(workspace_root, client_id, _request_tool_list, scope)
    raw = _to_plain(res)
    tools = raw.get("tools") if isinstance(raw, dict) else []
    items = [_normalize_tool_entry(t) for t in tools] if isinstance(tools, list) else []
    return {"ok": True, "client_id": client_id, "tools": items}


async def call_tool(
    workspace_root: Path, client_id: str, tool_name: str, arguments: dict[str, Any] | None, *, scope: str = "",
) -> dict[str, Any]:
    async def _request(session: Any) -> Any:
        return await session.call_tool(tool_name, arguments=arguments or {})

    res = await _with_session(workspace_root, client_id, _request, scope)
    # Why: MCP image content can be much larger than the context budget. How:
    # serialize the result through the MCP-aware helper that saves oversized
    # images to disk and returns AttachmentCollector-compatible metadata.
    # Purpose: keep tool history compact while still exposing generated files.
    result, attachments = _to_plain_mcp_result(workspace_root, client_id, res)
    # [AutoC 2026-05-31] Why: MCP SDK results have provider-specific content
    # payloads, but the engine now consumes a unified ok/data/error contract.
    # How: flatten text, image, and resource parts into data.result while keeping
    # the original processed MCP payload under data.mcp_result. Purpose: make MCP
    # tool history readable and preserve structured data plus attachments.
    result_text_parts: list[str] = []
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        for part in result["content"]:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    result_text_parts.append(str(part.get("text", "")))
                elif part.get("type") == "image":
                    result_text_parts.append(f"[image: {part.get('mimeType') or part.get('mime_type') or part.get('mime') or '?'}]")
                elif part.get("type") == "resource":
                    resource = part.get("resource")
                    uri = part.get("uri") or (resource.get("uri") if isinstance(resource, dict) else None)
                    result_text_parts.append(f"[resource: {uri or '?'}]")
    result_text = "\n".join(result_text_parts) if result_text_parts else json.dumps(result, ensure_ascii=False, indent=2)
    is_error = bool(result.get("isError") or result.get("is_error")) if isinstance(result, dict) else False
    return {
        "ok": not is_error,
        "data": {
            "result": result_text,
            "mcp_result": result,
            "client_id": client_id,
            "tool_name": tool_name,
            "attachments": attachments or [],
        },
        "error": result_text if is_error else None,
        "attachments": attachments or [],
    }
