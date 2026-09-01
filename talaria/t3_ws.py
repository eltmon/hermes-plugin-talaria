"""Effect RPC WebSocket client for T3 Code (CP-3 frame schema).

One JSON object per WS text message (Effect ``layerJson``). Not JSON-RPC,
not NDJSON. Connect: ``ticket_fn()`` → ``ws(s)://{host}/ws?wsTicket=…``.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

from .errors import NotAuthenticated

OpenWs = Callable[[str], Awaitable[Any]]
TicketFn = Callable[[], str]
SleepFn = Callable[[float], Awaitable[Any]]
NowFn = Callable[[], float]

PING_INTERVAL_S = 30.0
INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 30.0
AUTH_CLOSE_CODES = frozenset({1008, 4001, 4003, 4401, 4403})
AUTH_HTTP_STATUSES = frozenset({401, 403})


class TicketRejected(Exception):
    """WS upgrade rejected the ticket. Stop reconnecting; mint a fresh ticket."""


class RpcExitFailure(Exception):
    """RPC completed with ``Exit`` / ``Failure``."""

    def __init__(self, cause: Any) -> None:
        self.cause = cause
        super().__init__(str(cause))


class RpcProtocolError(Exception):
    """Server ``Defect`` or ``ClientProtocolError`` failed in-flight waiters."""

    def __init__(self, tag: str, payload: Any = None) -> None:
        self.tag = tag
        self.payload = payload
        super().__init__(f"{tag}: {payload}")


def _ws_url(base_url: str, ticket: str) -> str:
    parsed = urlparse(base_url)
    scheme = (parsed.scheme or "https").lower()
    if scheme in ("http", "ws"):
        ws_scheme = "ws"
    else:
        ws_scheme = "wss"
    return urlunparse(
        (ws_scheme, parsed.netloc, "/ws", "", urlencode({"wsTicket": ticket}), "")
    )


def _close_code(exc: BaseException) -> int | None:
    for attr in ("rcvd", "sent"):
        frame = getattr(exc, attr, None)
        code = getattr(frame, "code", None)
        if isinstance(code, int):
            return code
    code = getattr(exc, "code", None)
    return code if isinstance(code, int) else None


def _http_status(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _is_ticket_rejection(exc: BaseException) -> bool:
    if isinstance(exc, (TicketRejected, NotAuthenticated)):
        return True
    status = _http_status(exc)
    if status in AUTH_HTTP_STATUSES:
        return True
    return _close_code(exc) in AUTH_CLOSE_CODES


def _request_frame(
    request_id: str,
    tag: str,
    payload: Mapping[str, Any] | None,
    headers: list | None,
) -> dict[str, Any]:
    return {
        "_tag": "Request",
        "id": request_id,
        "tag": tag,
        "payload": dict(payload) if payload is not None else {},
        "headers": list(headers) if headers is not None else [],
    }


def _is_interrupt_exit(exit_: Mapping[str, Any]) -> bool:
    if exit_.get("_tag") != "Failure":
        return False
    cause = exit_.get("cause")
    if isinstance(cause, list):
        return any(
            isinstance(item, dict) and item.get("_tag") == "Interrupt" for item in cause
        )
    return isinstance(cause, dict) and cause.get("_tag") == "Interrupt"


def _unwrap_exit(exit_: Any) -> Any:
    if not isinstance(exit_, dict):
        raise RpcExitFailure(exit_)
    if exit_.get("_tag") == "Success":
        return exit_.get("value")
    raise RpcExitFailure(exit_.get("cause", exit_))


async def _default_open_ws(url: str) -> Any:
    import websockets

    return await websockets.connect(
        url,
        ping_interval=PING_INTERVAL_S,
        ping_timeout=60,
    )


class T3WsClient:
    """Async Effect RPC client. Connections are lazy; ``ticket_fn`` is called per dial."""

    def __init__(
        self,
        base_url: str,
        ticket_fn: TicketFn,
        *,
        open_ws: OpenWs | None = None,
        sleep: SleepFn | None = None,
        now: NowFn | None = None,
    ) -> None:
        self.base_url = base_url
        self._ticket_fn = ticket_fn
        self._open_ws = open_ws
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._now = now if now is not None else time.monotonic
        self._ws: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._streams: dict[str, asyncio.Queue[Any]] = {}
        self._seq = 0
        self._closing = False
        self._auth_latched = False

    @property
    def auth_latched(self) -> bool:
        return self._auth_latched

    def _new_id(self) -> str:
        self._seq += 1
        return f"req-{self._seq}"

    async def request(
        self,
        tag: str,
        payload: Mapping[str, Any] | None = None,
        *,
        headers: list | None = None,
    ) -> Any:
        await self._ensure_connected()
        request_id = self._new_id()
        fut = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        try:
            await self._send(_request_frame(request_id, tag, payload, headers))
            exit_ = await fut
        finally:
            self._pending.pop(request_id, None)
        return _unwrap_exit(exit_)

    async def subscribe(
        self,
        tag: str,
        payload: Mapping[str, Any] | None = None,
        *,
        headers: list | None = None,
    ) -> AsyncIterator[Any]:
        await self._ensure_connected()
        request_id = self._new_id()
        queue: asyncio.Queue[Any] = asyncio.Queue()
        self._streams[request_id] = queue
        await self._send(_request_frame(request_id, tag, payload, headers))
        finished = False
        try:
            while True:
                kind, data = await queue.get()
                if kind == "chunk":
                    for item in data:
                        yield item
                elif kind == "exit":
                    finished = True
                    if not isinstance(data, dict):
                        raise RpcExitFailure(data)
                    if data.get("_tag") == "Success" or _is_interrupt_exit(data):
                        return
                    raise RpcExitFailure(data.get("cause", data))
                elif isinstance(data, BaseException):
                    raise data
                else:
                    raise ConnectionError("websocket closed")
        finally:
            self._streams.pop(request_id, None)
            if not finished and not self._closing:
                try:
                    await self._send({"_tag": "Interrupt", "requestId": request_id})
                except Exception:
                    pass

    async def close(self) -> None:
        self._closing = True
        for request_id in list(self._streams):
            try:
                await self._send({"_tag": "Interrupt", "requestId": request_id})
            except Exception:
                pass
        reader = self._reader
        ws = self._ws
        self._reader = None
        self._ws = None
        if reader is not None:
            reader.cancel()
            try:
                await reader
            except (asyncio.CancelledError, Exception):
                pass
        if ws is not None:
            close = getattr(ws, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    pass
        self._fail_waiters(ConnectionError("closed"))

    async def _ensure_connected(self) -> None:
        if self._ws is not None:
            return
        if self._closing:
            raise RuntimeError("T3WsClient is closed")
        async with self._lock:
            if self._ws is not None:
                return
            if self._closing:
                raise RuntimeError("T3WsClient is closed")
            await self._connect_with_retry()

    async def _connect_with_retry(self) -> None:
        backoff = INITIAL_BACKOFF_S
        auth_tries = 0
        while True:
            if self._closing:
                raise RuntimeError("T3WsClient is closed")
            try:
                await self._dial()
                return
            except Exception as exc:
                if self._closing:
                    raise
                if _is_ticket_rejection(exc):
                    auth_tries += 1
                    self._auth_latched = True
                    if auth_tries >= 2:
                        raise TicketRejected() from exc
                    continue
                await self._sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)

    async def _dial(self) -> None:
        ticket = self._ticket_fn()
        url = _ws_url(self.base_url, ticket)
        open_ws = self._open_ws if self._open_ws is not None else _default_open_ws
        ws = await open_ws(url)
        self._ws = ws
        self._auth_latched = False
        self._reader = asyncio.create_task(self._read_loop(ws))
        await self._send({"_tag": "Ping"})

    async def _send(self, frame: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise ConnectionError("not connected")
        await ws.send(json.dumps(frame))

    async def _read_loop(self, ws: Any) -> None:
        try:
            while True:
                try:
                    raw = await ws.recv()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if _is_ticket_rejection(exc):
                        self._auth_latched = True
                    break
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                try:
                    frame = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(frame, dict):
                    await self._dispatch(frame)
        except asyncio.CancelledError:
            raise
        finally:
            if self._ws is ws:
                self._ws = None
                self._fail_waiters(ConnectionError("connection lost"))

    async def _dispatch(self, frame: dict[str, Any]) -> None:
        tag = frame.get("_tag")
        if tag == "Pong":
            return
        if tag in ("Defect", "ClientProtocolError"):
            payload = frame.get("defect") if tag == "Defect" else frame.get("error")
            self._fail_waiters(RpcProtocolError(tag, payload))
            return
        if tag == "Chunk":
            request_id = frame.get("requestId")
            stream = self._streams.get(request_id)
            if stream is not None:
                await stream.put(("chunk", frame.get("values") or []))
            if request_id is not None and (
                stream is not None or request_id in self._pending
            ):
                await self._send({"_tag": "Ack", "requestId": request_id})
            return
        if tag == "Exit":
            request_id = frame.get("requestId")
            exit_ = frame.get("exit") if isinstance(frame.get("exit"), dict) else {}
            fut = self._pending.pop(request_id, None)
            if fut is not None and not fut.done():
                fut.set_result(exit_)
            stream = self._streams.pop(request_id, None)
            if stream is not None:
                await stream.put(("exit", exit_))

    def _fail_waiters(self, reason: BaseException) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(reason)
        self._pending.clear()
        for queue in list(self._streams.values()):
            queue.put_nowait(("closed", reason))
        self._streams.clear()
