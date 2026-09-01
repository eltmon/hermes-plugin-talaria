"""WI-10: Effect RPC WebSocket client — fixture fake server, mocked clock."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from talaria.t3_ws import (
    INITIAL_BACKOFF_S,
    MAX_BACKOFF_S,
    RpcExitFailure,
    RpcProtocolError,
    T3WsClient,
    TicketRejected,
)

FRAMES = Path(__file__).resolve().parent / "fixtures" / "frames" / "transcript.json"
BASE_HTTPS = "https://t3.example.test"
BASE_HTTP = "http://127.0.0.1:3773"
_CLOSE = object()


def _transcript() -> dict:
    return json.loads(FRAMES.read_text(encoding="utf-8"))


def _raw_frames(direction: str, tag: str | None = None) -> list[dict]:
    out = []
    for row in _transcript()["frames"]:
        if row["dir"] != direction:
            continue
        raw = row["raw"]
        if tag is not None and raw.get("_tag") != tag:
            continue
        out.append(raw)
    return out


def _request_template(rpc_tag: str) -> dict:
    for raw in _raw_frames("out", "Request"):
        if raw.get("tag") == rpc_tag:
            return raw
    raise AssertionError(f"no Request {rpc_tag} in transcript")


def _exit_template(request_id: str) -> dict:
    for raw in _raw_frames("in", "Exit"):
        if raw.get("requestId") == request_id:
            return raw
    raise AssertionError(f"no Exit {request_id} in transcript")


def _chunks_for(request_id: str) -> list[dict]:
    return [
        raw
        for raw in _raw_frames("in", "Chunk")
        if raw.get("requestId") == request_id
    ]


PROBE_REQ = _request_template("server.probe")
SHELL_REQ = _request_template("orchestration.subscribeShell")
LS_REQ = _request_template("projects.listEntries")
PROBE_EXIT = _exit_template("probe-1")
LS_EXIT = _exit_template("ls-1")
SHELL_CHUNKS = _chunks_for("shell-1")
SHELL_EXIT = _exit_template("shell-1")


class FakeClosed(Exception):
    def __init__(self, code: int = 1006) -> None:
        super().__init__(f"closed {code}")
        self.code = code
        self.rcvd = type("Close", (), {"code": code})()


class ImmediateClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ScriptedWs:
    """In-memory Effect RPC peer. Replays transcript payloads, remaps requestId.

    Streaming chunks wait for Ack before the next Chunk (or Interrupt Exit).
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._incoming: asyncio.Queue = asyncio.Queue()
        self._rest_chunks: dict[str, list[dict]] = {}
        self.closed = False
        self.close_code = 1000

    async def send(self, data: str) -> None:
        if self.closed:
            raise ConnectionError("closed")
        frame = json.loads(data)
        self.sent.append(frame)
        tag = frame.get("_tag")
        if tag == "Ping":
            self._incoming.put_nowait(json.dumps({"_tag": "Pong"}))
        elif tag == "Request":
            self._reply_request(frame)
        elif tag == "Ack":
            self._release_after_ack(frame.get("requestId"))
        elif tag == "Interrupt":
            exit_ = json.loads(json.dumps(SHELL_EXIT))
            exit_["requestId"] = frame.get("requestId")
            self._incoming.put_nowait(json.dumps(exit_))

    def _push(self, frame: dict) -> None:
        self._incoming.put_nowait(json.dumps(frame))

    def _release_after_ack(self, request_id: str | None) -> None:
        rest = self._rest_chunks.get(request_id)
        if not rest:
            return
        nxt = rest.pop(0)
        self._push(nxt)
        if not rest:
            self._rest_chunks.pop(request_id, None)

    def _reply_request(self, frame: dict) -> None:
        rid = frame["id"]
        rpc = frame.get("tag")
        if rpc == "server.probe":
            exit_ = json.loads(json.dumps(PROBE_EXIT))
            exit_["requestId"] = rid
            exit_["unexpectedEnvelope"] = True
            self._push(exit_)
            return
        if rpc == "orchestration.subscribeShell":
            chunks = []
            for chunk in SHELL_CHUNKS:
                copied = json.loads(json.dumps(chunk))
                copied["requestId"] = rid
                chunks.append(copied)
            first, rest = chunks[0], chunks[1:]
            if rest:
                self._rest_chunks[rid] = rest
            self._push(first)
            return
        if rpc == "projects.listEntries":
            payload = frame.get("payload") or {}
            if "cwd" not in payload:
                self._incoming.put_nowait(
                    json.dumps(
                        {
                            "_tag": "Exit",
                            "requestId": rid,
                            "exit": {
                                "_tag": "Failure",
                                "cause": [
                                    {
                                        "_tag": "Die",
                                        "defect": 'Missing key\\n  at ["cwd"]',
                                    }
                                ],
                            },
                        }
                    )
                )
                return
            exit_ = json.loads(json.dumps(LS_EXIT))
            exit_["requestId"] = rid
            self._incoming.put_nowait(json.dumps(exit_))
            return
        self._incoming.put_nowait(
            json.dumps(
                {
                    "_tag": "Exit",
                    "requestId": rid,
                    "exit": {"_tag": "Success", "value": {}},
                }
            )
        )

    async def recv(self) -> str:
        item = await self._incoming.get()
        if item is _CLOSE:
            raise FakeClosed(self.close_code)
        return item

    async def close(self) -> None:
        self.closed = True
        self._incoming.put_nowait(_CLOSE)

    def peer_close(self, code: int = 1006) -> None:
        self.close_code = code
        self.closed = True
        self._incoming.put_nowait(_CLOSE)


def _ticket_from_url(url: str) -> str:
    return parse_qs(urlparse(url).query).get("wsTicket", [""])[0]


def _run(coro):
    return asyncio.run(coro)


def test_import_opens_no_socket(socket_guard):
    import talaria.t3_ws as mod

    assert callable(mod.T3WsClient)
    client = mod.T3WsClient("https://example.test", lambda: "tix")
    assert client.auth_latched is False


def test_probe_round_trip_from_transcript():
    urls: list[str] = []
    sockets: list[ScriptedWs] = []

    async def open_ws(url: str) -> ScriptedWs:
        urls.append(url)
        ws = ScriptedWs()
        sockets.append(ws)
        return ws

    async def body():
        client = T3WsClient(BASE_HTTPS, lambda: "tix-1", open_ws=open_ws)
        try:
            value = await client.request("server.probe", {})
            assert value == PROBE_EXIT["exit"]["value"]
        finally:
            await client.close()

    _run(body())
    parsed = urlparse(urls[0])
    assert parsed.scheme == "wss"
    assert parsed.netloc == "t3.example.test"
    assert parsed.path == "/ws"
    assert _ticket_from_url(urls[0]) == "tix-1"
    sent = sockets[0].sent
    assert sent[0] == {"_tag": "Ping"}
    req = next(frame for frame in sent if frame.get("_tag") == "Request")
    assert set(req) == set(PROBE_REQ)
    assert req["_tag"] == "Request"
    assert req["tag"] == "server.probe"
    assert req["payload"] == {}
    assert req["headers"] == []
    assert isinstance(req["id"], str) and req["id"]


def test_ws_url_http_to_ws():
    urls: list[str] = []

    async def open_ws(url: str) -> ScriptedWs:
        urls.append(url)
        return ScriptedWs()

    async def body():
        client = T3WsClient(BASE_HTTP, lambda: "tix-loop", open_ws=open_ws)
        try:
            await client.request("server.probe", {})
        finally:
            await client.close()

    _run(body())
    parsed = urlparse(urls[0])
    assert parsed.scheme == "ws"
    assert parsed.netloc == "127.0.0.1:3773"
    assert parsed.path == "/ws"
    assert _ticket_from_url(urls[0]) == "tix-loop"


def test_subscribe_shell_chunks_then_interrupt():
    sockets: list[ScriptedWs] = []

    async def open_ws(url: str) -> ScriptedWs:
        ws = ScriptedWs()
        sockets.append(ws)
        return ws

    async def body():
        client = T3WsClient(BASE_HTTPS, lambda: "tix-1", open_ws=open_ws)
        items = []
        agen = client.subscribe(
            "orchestration.subscribeShell", SHELL_REQ["payload"]
        )
        try:
            async for item in agen:
                items.append(item)
                if item.get("kind") == "synchronized":
                    break
        finally:
            await agen.aclose()
            await client.close()
        return items

    items = _run(body())
    expected = [value for chunk in SHELL_CHUNKS for value in chunk["values"]]
    assert items == expected
    sent = sockets[0].sent
    req = next(frame for frame in sent if frame.get("_tag") == "Request")
    assert set(req) == set(SHELL_REQ)
    assert req["tag"] == "orchestration.subscribeShell"
    assert req["payload"] == SHELL_REQ["payload"]
    assert req["headers"] == []
    acks = [frame for frame in sent if frame.get("_tag") == "Ack"]
    assert acks == [{"_tag": "Ack", "requestId": req["id"]}] * len(SHELL_CHUNKS)
    interrupt = next(frame for frame in sent if frame.get("_tag") == "Interrupt")
    assert interrupt == {"_tag": "Interrupt", "requestId": req["id"]}
    ack_at = [i for i, frame in enumerate(sent) if frame.get("_tag") == "Ack"]
    int_at = next(i for i, frame in enumerate(sent) if frame.get("_tag") == "Interrupt")
    assert ack_at and ack_at[-1] < int_at


def test_list_entries_unary_from_transcript():
    async def open_ws(url: str) -> ScriptedWs:
        return ScriptedWs()

    async def body():
        client = T3WsClient(BASE_HTTPS, lambda: "tix-1", open_ws=open_ws)
        try:
            value = await client.request(
                "projects.listEntries", LS_REQ["payload"]
            )
            assert value == LS_EXIT["exit"]["value"]
            assert value["entries"][0]["path"] == "README.md"
        finally:
            await client.close()

    _run(body())


def test_unknown_fields_on_exit_are_kept():
    class ExtraWs(ScriptedWs):
        def _reply_request(self, frame: dict) -> None:
            if frame.get("tag") == "server.probe":
                self._incoming.put_nowait(
                    json.dumps(
                        {
                            "_tag": "Exit",
                            "requestId": frame["id"],
                            "unexpectedEnvelope": True,
                            "exit": {
                                "_tag": "Success",
                                "value": {"ok": True, "brandNew": {"nested": 1}},
                            },
                        }
                    )
                )
                return
            super()._reply_request(frame)

    async def open_ws(url: str) -> ScriptedWs:
        return ExtraWs()

    async def body():
        client = T3WsClient(BASE_HTTPS, lambda: "tix-1", open_ws=open_ws)
        try:
            value = await client.request("server.probe", {})
            assert value == {"ok": True, "brandNew": {"nested": 1}}
        finally:
            await client.close()

    _run(body())


def test_exit_failure_raises():
    async def open_ws(url: str) -> ScriptedWs:
        return ScriptedWs()

    async def body():
        client = T3WsClient(BASE_HTTPS, lambda: "tix-1", open_ws=open_ws)
        try:
            with pytest.raises(RpcExitFailure) as caught:
                await client.request("projects.listEntries", {})
            cause = caught.value.cause
            assert isinstance(cause, list)
            assert cause[0]["_tag"] == "Die"
        finally:
            await client.close()

    _run(body())


def test_reconnect_backoff_capped(monkeypatch):
    clock = ImmediateClock()
    attempts = {"n": 0}
    sockets: list[ScriptedWs] = []

    async def boom_sleep(*_args, **_kwargs):
        raise AssertionError("real asyncio.sleep")

    monkeypatch.setattr("talaria.t3_ws.asyncio.sleep", boom_sleep)

    async def open_ws(url: str) -> ScriptedWs:
        attempts["n"] += 1
        if attempts["n"] < 8:
            raise OSError("down")
        ws = ScriptedWs()
        sockets.append(ws)
        return ws

    async def body():
        client = T3WsClient(
            BASE_HTTPS,
            lambda: "tix-1",
            open_ws=open_ws,
            sleep=clock.sleep,
            now=clock.time,
        )
        try:
            value = await client.request("server.probe", {})
            assert value == {}
        finally:
            await client.close()

    _run(body())
    assert clock.sleeps == [
        INITIAL_BACKOFF_S,
        2.0,
        4.0,
        8.0,
        16.0,
        MAX_BACKOFF_S,
        MAX_BACKOFF_S,
    ]
    assert attempts["n"] == 8
    assert sockets


def test_ticket_rejection_latches_without_spin(monkeypatch):
    clock = ImmediateClock()
    tickets: list[str] = []
    opens = {"n": 0}

    async def boom_sleep(*_args, **_kwargs):
        raise AssertionError("real asyncio.sleep")

    monkeypatch.setattr("talaria.t3_ws.asyncio.sleep", boom_sleep)

    def ticket_fn() -> str:
        name = f"tix-{len(tickets) + 1}"
        tickets.append(name)
        return name

    async def open_ws(url: str) -> ScriptedWs:
        opens["n"] += 1
        raise TicketRejected()

    async def body():
        client = T3WsClient(
            BASE_HTTPS,
            ticket_fn,
            open_ws=open_ws,
            sleep=clock.sleep,
            now=clock.time,
        )
        try:
            with pytest.raises(TicketRejected):
                await client.request("server.probe", {})
            assert client.auth_latched is True
            assert len(tickets) == 2
            assert opens["n"] == 2
            assert clock.sleeps == []
        finally:
            await client.close()

    _run(body())
    assert len(tickets) == 2
    assert opens["n"] == 2


def test_ticket_rejection_mints_fresh_ticket():
    tickets: list[str] = []
    urls: list[str] = []

    def ticket_fn() -> str:
        name = f"tix-{len(tickets) + 1}"
        tickets.append(name)
        return name

    async def open_ws(url: str) -> ScriptedWs:
        urls.append(url)
        ticket = _ticket_from_url(url)
        if ticket == "tix-1":
            raise TicketRejected()
        return ScriptedWs()

    async def body():
        client = T3WsClient(BASE_HTTPS, ticket_fn, open_ws=open_ws)
        try:
            value = await client.request("server.probe", {})
            assert value == {}
        finally:
            await client.close()

    _run(body())
    assert tickets == ["tix-1", "tix-2"]
    assert [_ticket_from_url(url) for url in urls] == ["tix-1", "tix-2"]


def test_http_401_upgrade_is_ticket_rejection():
    class Unauthorized(Exception):
        status_code = 401

    tickets: list[str] = []

    def ticket_fn() -> str:
        tickets.append(f"tix-{len(tickets) + 1}")
        return tickets[-1]

    async def open_ws(url: str) -> ScriptedWs:
        raise Unauthorized()

    async def body():
        client = T3WsClient(BASE_HTTPS, ticket_fn, open_ws=open_ws)
        try:
            with pytest.raises(TicketRejected):
                await client.request("server.probe", {})
            assert client.auth_latched is True
        finally:
            await client.close()

    _run(body())
    assert len(tickets) == 2


def test_close_interrupts_open_subscribe():
    sockets: list[ScriptedWs] = []

    async def open_ws(url: str) -> ScriptedWs:
        ws = ScriptedWs()
        sockets.append(ws)
        return ws

    async def body():
        client = T3WsClient(BASE_HTTPS, lambda: "tix-1", open_ws=open_ws)
        items = []
        try:
            async for item in client.subscribe(
                "orchestration.subscribeShell", SHELL_REQ["payload"]
            ):
                items.append(item)
                if item.get("kind") == "synchronized":
                    break
        finally:
            await client.close()
        return items

    items = _run(body())
    assert [item["kind"] for item in items] == ["snapshot", "synchronized"]
    sent = sockets[0].sent
    req = next(frame for frame in sent if frame.get("_tag") == "Request")
    acks = [frame for frame in sent if frame.get("_tag") == "Ack"]
    assert acks == [{"_tag": "Ack", "requestId": req["id"]}] * len(SHELL_CHUNKS)
    interrupt = next(frame for frame in sent if frame.get("_tag") == "Interrupt")
    assert interrupt == {"_tag": "Interrupt", "requestId": req["id"]}


def test_defect_fails_waiters():
    class DefectWs(ScriptedWs):
        def _reply_request(self, frame: dict) -> None:
            self._push({"_tag": "Defect", "defect": "boom", "extra": True})

    async def open_ws(url: str) -> ScriptedWs:
        return DefectWs()

    async def body():
        client = T3WsClient(BASE_HTTPS, lambda: "tix-1", open_ws=open_ws)
        try:
            with pytest.raises(RpcProtocolError) as caught:
                await client.request("server.probe", {})
            assert caught.value.tag == "Defect"
            assert caught.value.payload == "boom"
        finally:
            await client.close()

    _run(body())


def test_client_protocol_error_fails_waiters():
    class ProtocolWs(ScriptedWs):
        def _reply_request(self, frame: dict) -> None:
            self._push(
                {
                    "_tag": "ClientProtocolError",
                    "error": {"reason": "socket closed"},
                }
            )

    async def open_ws(url: str) -> ScriptedWs:
        return ProtocolWs()

    async def body():
        client = T3WsClient(BASE_HTTPS, lambda: "tix-1", open_ws=open_ws)
        try:
            with pytest.raises(RpcProtocolError) as caught:
                await client.request("server.probe", {})
            assert caught.value.tag == "ClientProtocolError"
            assert caught.value.payload == {"reason": "socket closed"}
        finally:
            await client.close()

    _run(body())


def test_reconnect_after_drop():
    clock = ImmediateClock()
    sockets: list[ScriptedWs] = []

    async def open_ws(url: str) -> ScriptedWs:
        ws = ScriptedWs()
        sockets.append(ws)
        return ws

    async def wait_disconnected(client: T3WsClient) -> None:
        for _ in range(50):
            if client._ws is None:
                return
            await asyncio.sleep(0)
        raise AssertionError("socket still attached")

    async def body():
        client = T3WsClient(
            BASE_HTTPS,
            lambda: "tix-1",
            open_ws=open_ws,
            sleep=clock.sleep,
            now=clock.time,
        )
        try:
            assert await client.request("server.probe", {}) == {}
            sockets[0].peer_close(1006)
            await wait_disconnected(client)
            assert await client.request("server.probe", {}) == {}
        finally:
            await client.close()

    _run(body())
    assert len(sockets) == 2
    assert clock.sleeps == []
