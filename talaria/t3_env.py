"""Mode-agnostic T3 Code environment HTTP client.

One reused httpx.Client (lazy singleton, 30s timeout). Auth is an injected
headers callable so mode A (static bearer) and mode B (DPoP token + proof)
do not branch at call sites. T3 JSON is returned as dicts; unknown fields
are kept. No payload schema validation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

import httpx

from .errors import NotAuthenticated, T3ApiError, TalariaError
from .protocol import shell_snapshot, thread_snapshot, history_snapshot, dispatch_commands
from .async_runtime import run_sync
from .t3_ws import T3WsClient

try:
    from plugins.plugin_utils import lazy_singleton as _lazy_singleton
except ImportError:
    import functools
    import threading
    from typing import TypeVar

    T = TypeVar("T")

    def _lazy_singleton(factory: Callable[[], T]) -> Callable[[], T]:
        lock = threading.Lock()
        box: list = []

        @functools.wraps(factory)
        def accessor() -> T:
            if box:
                return box[0]
            with lock:
                if box:
                    return box[0]
                instance = factory()
                box.append(instance)
                return instance

        def reset() -> None:
            with lock:
                box.clear()

        accessor.reset = reset  # type: ignore[attr-defined]
        return accessor

DEFAULT_TIMEOUT = 30.0
CLIENT_LABEL = "hermes-talaria"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
SUBJECT_TOKEN_TYPE = "urn:t3:params:oauth:token-type:environment-bootstrap"
REQUESTED_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
STANDARD_SCOPE = (
    "orchestration:read orchestration:operate terminal:operate "
    "review:write relay:read"
)
V2_STANDARD_SCOPE = "orchestration:read orchestration:operate filesystem:read filesystem:write"

HeadersFn = Callable[[], Mapping[str, str]]
DpopSigner = Callable[[str, str], str]


@_lazy_singleton
def get_client() -> httpx.Client:
    return httpx.Client(timeout=DEFAULT_TIMEOUT)


def _join(base_url: str, path: str) -> str:
    parsed = urlparse(base_url)
    if not path.startswith("/"):
        path = "/" + path
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def _token_exchange_form(subject_token: str, protocol_version: int = 1) -> dict[str, str]:
    return {
        "grant_type": GRANT_TYPE,
        "subject_token": subject_token,
        "subject_token_type": SUBJECT_TOKEN_TYPE,
        "requested_token_type": REQUESTED_TOKEN_TYPE,
        "scope": V2_STANDARD_SCOPE if protocol_version == 2 else STANDARD_SCOPE,
        "client_label": CLIENT_LABEL,
    }


def _parse(response: httpx.Response) -> Any:
    if response.status_code == 401:
        raise NotAuthenticated()
    if response.status_code < 200 or response.status_code >= 300:
        raise T3ApiError(response.status_code, response.text)
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError as exc:
        raise T3ApiError(response.status_code, response.text) from exc


class T3EnvClient:
    """HTTP client bound to one environment base URL."""

    def __init__(
        self,
        base_url: str,
        headers_fn: HeadersFn | None = None,
        *,
        client: httpx.Client | None = None,
        protocol_version: int = 1,
        open_ws=None,
    ) -> None:
        self.base_url = base_url
        self.headers_fn = headers_fn
        self._client = client
        if protocol_version not in (1, 2) or isinstance(protocol_version, bool):
            raise TalariaError("unsupported orchestration protocol", "use protocol_version 1 or 2")
        self.protocol_version = protocol_version
        self._protocol_checked = False
        self._open_ws = open_ws

    def check_protocol(self) -> None:
        if self.protocol_version != 2 or self._protocol_checked:
            return
        descriptor = self.descriptor()
        version = descriptor.get("orchestrationProtocolVersion") if isinstance(descriptor, dict) else None
        if version != 2:
            raise TalariaError(
                f"unsupported server orchestration protocol: {version!r}",
                "this connection requires protocol 2; use an explicit legacy connection only for T3 0.0.37",
            )
        self._protocol_checked = True

    def _http(self) -> httpx.Client:
        return self._client if self._client is not None else get_client()

    def _url(self, path: str) -> str:
        return _join(self.base_url, path)

    def _request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        data: Mapping[str, str] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> Any:
        url = self._url(path)
        headers: dict[str, str] = dict(extra_headers or {})
        if self.protocol_version == 2 and path.startswith("/api/orchestration/"):
            headers["x-t3-orchestration-protocol"] = "2"
        if auth and self.headers_fn is not None:
            headers.update(self.headers_fn())
        return _parse(
            self._http().request(
                method,
                url,
                headers=headers or None,
                params=params,
                json=json,
                data=data,
            )
        )

    def descriptor(self) -> Any:
        return self._request("GET", "/.well-known/t3/environment", auth=False)

    def shell(self) -> Any:
        self.check_protocol()
        raw = self._request("GET", "/api/orchestration/shell")
        return shell_snapshot(raw) if self.protocol_version == 2 else raw

    def thread(
        self,
        thread_id: str,
        turn_limit: int | None = None,
        before_cursor: str | None = None,
    ) -> Any:
        self.check_protocol()
        params: dict[str, Any] = {}
        if self.protocol_version == 2:
            path = "/api/orchestration/threads/" + quote(str(thread_id), safe="")
            if before_cursor:
                return history_snapshot(self._request("GET", path + "/history",
                    params={"cursor": before_cursor, "view": "conversation"}), thread_id)
            return thread_snapshot(self._request("GET", path), turn_limit)
        if turn_limit is not None:
            params["turnLimit"] = turn_limit
        if before_cursor is not None:
            params["beforeCursor"] = before_cursor
        path = "/api/orchestration/threads/" + quote(str(thread_id), safe="")
        return self._request("GET", path, params=params or None)

    def dispatch(self, command: dict) -> Any:
        if self.protocol_version == 2:
            self.check_protocol()
            shell = self.shell() if command["type"] in (
                "thread.turn.interrupt", "thread.approval.respond", "thread.user-input.respond",
            ) else None
            commands = dispatch_commands(command, shell)
            async def send():
                client = T3WsClient(self.base_url, lambda: self.ws_ticket()["ticket"],
                                    protocol_version=2, open_ws=self._open_ws)
                try:
                    result = None
                    for item in commands:
                        result = await client.request("orchestration.dispatchCommand", item)
                    return result
                finally:
                    await client.close()
            return run_sync(send())
        return self._request("POST", "/api/orchestration/dispatch", json=command)

    def ws_ticket(self) -> Any:
        self.check_protocol()
        return self._request("POST", "/api/auth/websocket-ticket")

    def exchange_pairing(self, subject_token: str) -> Any:
        return self._request(
            "POST",
            "/oauth/token",
            auth=False,
            data=_token_exchange_form(subject_token, self.protocol_version),
        )

    def exchange_dpop(self, credential: str, dpop_signer: DpopSigner) -> Any:
        url = self._url("/oauth/token")
        proof = dpop_signer("POST", url)
        return self._request(
            "POST",
            "/oauth/token",
            auth=False,
            data=_token_exchange_form(credential, self.protocol_version),
            extra_headers={"dpop": proof},
        )


def exchange_pairing(base_url: str, subject_token: str) -> Any:
    return T3EnvClient(base_url).exchange_pairing(subject_token)


def exchange_dpop(
    base_url: str, credential: str, dpop_signer: DpopSigner
) -> Any:
    return T3EnvClient(base_url).exchange_dpop(credential, dpop_signer)
