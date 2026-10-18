from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from typing import Any, AsyncIterator

import httpx


class PluginTransportError(RuntimeError):
    pass


@contextmanager
def _safe_transport():
    from plugin_sdk import PluginExecutionError

    try:
        yield
    except (asyncio.CancelledError, PluginExecutionError, PluginTransportError):
        raise
    except Exception as exc:
        raise PluginTransportError(f"Provider transport failed ({type(exc).__name__})") from None


async def iter_sse_events(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    data: list[str] = []
    event = ""
    event_id: str | None = None
    retry: int | None = None
    first_line = True
    async for line in response.aiter_lines():
        if first_line:
            line = line.removeprefix("\ufeff")
            first_line = False
        if not line:
            if data:
                yield {"event": event, "data": "\n".join(data), "id": event_id, "retry": retry}
            data = []
            event = ""
            retry = None
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "data":
            data.append(value)
        elif name == "event":
            event = value
        elif name == "id" and "\x00" not in value:
            event_id = value
        elif name == "retry" and value.isascii() and value.isdecimal():
            retry = int(value)
    if data:
        yield {"event": event, "data": "\n".join(data), "id": event_id, "retry": retry}


def _encode_event(event: dict[str, Any]) -> bytes:
    lines = []
    for key in ("event", "id", "retry"):
        value = event.get(key)
        if value is not None:
            value = str(value)
            if "\r" in value or "\n" in value or "\x00" in value:
                raise ValueError("Invalid SSE event metadata")
            lines.append(f"{key}: {value}")
    data = event.get("data", "")
    if not isinstance(data, str):
        raise TypeError("SSE event data must be text")
    lines.extend(f"data: {line}" for line in data.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def _decode_body(body: bytes) -> Any:
    try:
        return json.loads(body)
    except (ValueError, UnicodeError):
        return body


def _encode_body(body: Any) -> bytes:
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode("utf-8")
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _body_headers(headers: Any) -> httpx.Headers:
    result = httpx.Headers(headers)
    for key in ("content-length", "content-encoding", "transfer-encoding"):
        result.pop(key, None)
    return result


def _failed(session: Any, exc: BaseException) -> None:
    session.set_outcome(ok=False, cancelled=isinstance(exc, asyncio.CancelledError), error=type(exc).__name__)


class _EventStream(httpx.AsyncByteStream):
    def __init__(self, response: httpx.Response, session: Any, lifetime: AsyncExitStack | None = None):
        self._response = response
        self._session = session
        self._lifetime = lifetime
        self._closed = False
        self._exception: BaseException | None = None

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            with _safe_transport():
                async for event in iter_sse_events(self._response):
                    for replacement in await self._session.stream_event(event):
                        yield _encode_event(replacement)
        except BaseException as exc:
            if not isinstance(exc, GeneratorExit):
                self._exception = exc
                _failed(self._session, exc)
            raise
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            self._session.set_outcome(ok=False, cancelled=True, error="CancelledError")
            if self._exception is None:
                self._exception = asyncio.CancelledError()
        try:
            await self._response.aclose()
        finally:
            if self._lifetime is not None:
                exc = self._exception
                await self._lifetime.__aexit__(type(exc) if exc is not None else None, exc, exc.__traceback__ if exc is not None else None)


class PluginHTTPClient:
    def __init__(self, client: httpx.AsyncClient, provider: Any):
        self._client = client
        self._provider = provider

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    async def _perform(self, session: Any, request: httpx.Request, *, stream: bool, lifetime: AsyncExitStack | None = None, **kwargs: Any) -> httpx.Response:
        body = await request.aread()
        wire = await session.request({
            "method": request.method,
            "url": str(request.url),
            "headers": dict(request.headers),
            "body": _decode_body(body),
        })
        headers = _body_headers(wire["headers"])
        if wire["url"] != str(request.url) and headers.get("host") == request.headers.get("host"):
            headers.pop("host", None)
        request = httpx.Request(
            wire["method"], wire["url"], headers=headers,
            content=_encode_body(wire["body"]), extensions=request.extensions,
        )
        response = await self._client.send(request, stream=stream, **kwargs)
        handed_over = False
        try:
            if stream and response.is_success:
                wire_response = await session.response({
                    "status_code": response.status_code,
                    "headers": dict(response.headers),
                    "body": None,
                })
                if wire_response["body"] is None and 200 <= wire_response["status_code"] < 300:
                    result = httpx.Response(
                        wire_response["status_code"], headers=_body_headers(wire_response["headers"]), request=request,
                        stream=_EventStream(response, session, lifetime), extensions=response.extensions,
                    )
                    session.set_outcome(ok=True, status_code=result.status_code)
                    handed_over = True
                    return result
            else:
                wire_response = await session.response({
                    "status_code": response.status_code,
                    "headers": dict(response.headers),
                    "body": _decode_body(await response.aread()),
                })
            result = httpx.Response(
                wire_response["status_code"], headers=_body_headers(wire_response["headers"]),
                content=_encode_body(wire_response["body"]), request=request, extensions=response.extensions,
            )
            session.set_outcome(ok=result.is_success, status_code=result.status_code)
            return result
        finally:
            if not handed_over:
                await response.aclose()

    async def post(self, url: Any, **kwargs: Any) -> httpx.Response:
        from plugin_runtime import provider_request

        async with provider_request(provider=self._provider, streaming=False) as session:
            if not session.active:
                return await self._client.post(url, **kwargs)
            with _safe_transport():
                send_options = {name: kwargs.pop(name) for name in ("auth", "follow_redirects") if name in kwargs}
                request = self._client.build_request("POST", url, **kwargs)
                return await self._perform(session, request, stream=False, **send_options)

    @asynccontextmanager
    async def stream(self, method: str, url: Any, **kwargs: Any) -> AsyncIterator[httpx.Response]:
        from plugin_runtime import provider_request

        async with provider_request(provider=self._provider, streaming=True) as session:
            if not session.active:
                async with self._client.stream(method, url, **kwargs) as response:
                    yield response
                return
            with _safe_transport():
                send_options = {name: kwargs.pop(name) for name in ("auth", "follow_redirects") if name in kwargs}
                request = self._client.build_request(method, url, **kwargs)
                response = await self._perform(session, request, stream=True, **send_options)
            try:
                yield response
            except BaseException as exc:
                _failed(session, exc)
                raise
            finally:
                await response.aclose()

    async def send(self, request: httpx.Request, *, stream: bool = False, **kwargs: Any) -> httpx.Response:
        from plugin_runtime import provider_request

        lifetime = AsyncExitStack()
        session = await lifetime.enter_async_context(provider_request(provider=self._provider, streaming=stream))
        try:
            if not session.active:
                await lifetime.aclose()
                return await self._client.send(request, stream=stream, **kwargs)
            with _safe_transport():
                response = await self._perform(session, request, stream=stream, lifetime=lifetime, **kwargs)
            if not isinstance(response.stream, _EventStream):
                await lifetime.aclose()
            return response
        except BaseException as exc:
            _failed(session, exc)
            await lifetime.__aexit__(type(exc), exc, exc.__traceback__)
            raise


def wrap_client(client: httpx.AsyncClient, provider: Any) -> PluginHTTPClient:
    return PluginHTTPClient(client, provider)
