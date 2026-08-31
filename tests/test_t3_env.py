"""WI-3: T3 environment HTTP client — MockTransport only, no live I/O."""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from talaria import t3_env
from talaria.errors import NotAuthenticated, T3ApiError
from talaria.t3_env import T3EnvClient, exchange_dpop, exchange_pairing, get_client

BASE = "https://t3.example.test"
TOKEN = "tok-test"
PROOF = "proof-test"

SUBJECT_TOKEN_TYPE = "urn:t3:params:oauth:token-type:environment-bootstrap"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
REQUESTED_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
STANDARD_SCOPE = (
    "orchestration:read orchestration:operate terminal:operate "
    "review:write relay:read"
)

PAIRING_RESULT = {
    "access_token": TOKEN,
    "issued_token_type": REQUESTED_TOKEN_TYPE,
    "token_type": "Bearer",
    "expires_in": 2592000,
    "scope": STANDARD_SCOPE,
    "unexpectedField": "keep-me",
}


def _json(status: int, payload) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _parse_form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode(), keep_blank_values=True)


@pytest.fixture(autouse=True)
def _reset_singleton():
    yield
    reset = getattr(t3_env.get_client, "reset", None)
    if callable(reset):
        reset()


@pytest.fixture
def make_env():
    clients: list[httpx.Client] = []

    def factory(handler, headers_fn=None, base_url: str = BASE) -> T3EnvClient:
        http = httpx.Client(transport=httpx.MockTransport(handler), timeout=30.0)
        clients.append(http)
        return T3EnvClient(base_url, headers_fn=headers_fn, client=http)

    yield factory
    for http in clients:
        http.close()


def test_import_opens_no_socket(socket_guard):
    import talaria.t3_env as mod  # noqa: F401

    assert callable(mod.get_client)


def test_get_client_is_lazy_singleton():
    first = get_client()
    try:
        second = get_client()
        assert first is second
        assert first.timeout.read == 30.0
    finally:
        first.close()


def test_descriptor_returns_unknown_fields(make_env):
    payload = {
        "environmentId": "env-1",
        "label": "laptop",
        "capabilities": [],
        "sessionMethods": ["bearer-access-token"],
        "unexpectedField": {"nested": True},
    }
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(200, payload)

    result = make_env(handler, headers_fn=_bearer).descriptor()
    assert result == payload
    assert result["unexpectedField"] == {"nested": True}
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/.well-known/t3/environment"
    assert "authorization" not in seen[0].headers


def test_shell_sends_auth_headers(make_env):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(200, {"projects": [], "extra": 1})

    result = make_env(handler, headers_fn=_bearer).shell()
    assert result["extra"] == 1
    assert seen[0].url.path == "/api/orchestration/shell"
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"


def test_headers_fn_runs_per_request(make_env):
    calls = {"n": 0}

    def headers_fn() -> dict[str, str]:
        calls["n"] += 1
        return _bearer()

    def handler(_request: httpx.Request) -> httpx.Response:
        return _json(200, {"ok": True})

    env = make_env(handler, headers_fn=headers_fn)
    env.shell()
    env.shell()
    assert calls["n"] == 2


def test_thread_query_params_are_camel_case(make_env):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(200, {"thread": {"id": "th-1"}, "newerKey": True})

    result = make_env(handler, headers_fn=_bearer).thread(
        "th-1", turn_limit=5, before_cursor="cur-1"
    )
    assert result["newerKey"] is True
    req = seen[0]
    assert req.url.path == "/api/orchestration/threads/th-1"
    assert req.url.params["turnLimit"] == "5"
    assert req.url.params["beforeCursor"] == "cur-1"


def test_thread_omits_optional_query_params(make_env):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(200, {})

    make_env(handler, headers_fn=_bearer).thread("th-1")
    assert "turnLimit" not in seen[0].url.params
    assert "beforeCursor" not in seen[0].url.params


def test_dispatch_posts_command_json(make_env):
    command = {
        "type": "thread.turn.interrupt",
        "commandId": "cmd-1",
        "threadId": "th-1",
        "futureField": "passthrough",
    }
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(200, {"accepted": True, "serverExtra": 2})

    result = make_env(handler, headers_fn=_bearer).dispatch(command)
    assert result == {"accepted": True, "serverExtra": 2}
    req = seen[0]
    assert req.method == "POST"
    assert req.url.path == "/api/orchestration/dispatch"
    assert json.loads(req.content) == command
    assert "application/json" in req.headers["content-type"]


def test_ws_ticket_posts(make_env):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(200, {"ticket": "tix-1", "expiresAt": "2026-01-01T00:00:00Z"})

    result = make_env(handler, headers_fn=_bearer).ws_ticket()
    assert result["ticket"] == "tix-1"
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/api/auth/websocket-ticket"


def test_exchange_pairing_form_encoding(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(200, PAIRING_RESULT)

    http = httpx.Client(transport=httpx.MockTransport(handler), timeout=30.0)
    monkeypatch.setattr(t3_env, "get_client", lambda: http)
    try:
        result = exchange_pairing(BASE, TOKEN)
    finally:
        http.close()

    assert result["unexpectedField"] == "keep-me"
    req = seen[0]
    assert req.method == "POST"
    assert str(req.url) == f"{BASE}/oauth/token"
    assert "application/x-www-form-urlencoded" in req.headers["content-type"]
    assert "authorization" not in req.headers
    form = _parse_form(req)
    assert form["subject_token_type"] == [SUBJECT_TOKEN_TYPE]
    assert form["grant_type"] == [GRANT_TYPE]
    assert form["requested_token_type"] == [REQUESTED_TOKEN_TYPE]
    assert form["scope"] == [STANDARD_SCOPE]
    assert form["client_label"] == ["hermes-talaria"]
    assert form["subject_token"] == [TOKEN]


def test_exchange_dpop_sends_proof_header(make_env):
    seen: list[httpx.Request] = []
    signed: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(
            200,
            {
                "access_token": TOKEN,
                "token_type": "DPoP",
                "expires_in": 3600,
                "scope": STANDARD_SCOPE,
                "issued_token_type": REQUESTED_TOKEN_TYPE,
            },
        )

    def signer(method: str, url: str) -> str:
        signed.append((method, url))
        return PROOF

    result = make_env(handler, headers_fn=_bearer).exchange_dpop(TOKEN, signer)
    assert result["token_type"] == "DPoP"
    assert signed == [("POST", f"{BASE}/oauth/token")]
    req = seen[0]
    assert req.headers["dpop"] == PROOF
    assert "authorization" not in req.headers
    form = _parse_form(req)
    assert form["subject_token_type"] == [SUBJECT_TOKEN_TYPE]
    assert form["subject_token"] == [TOKEN]
    assert form["client_label"] == ["hermes-talaria"]


def test_exchange_dpop_module_level(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(200, {"access_token": TOKEN, "token_type": "DPoP", "expires_in": 3600})

    http = httpx.Client(transport=httpx.MockTransport(handler), timeout=30.0)
    monkeypatch.setattr(t3_env, "get_client", lambda: http)
    try:
        exchange_dpop(BASE + "/", TOKEN, lambda method, url: PROOF)
    finally:
        http.close()

    assert seen[0].headers["dpop"] == PROOF
    assert str(seen[0].url) == f"{BASE}/oauth/token"


def test_trailing_slash_base_url_joins_on_origin(make_env):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(200, {"ok": True})

    make_env(handler, base_url=BASE + "/").descriptor()
    assert str(seen[0].url) == f"{BASE}/.well-known/t3/environment"


@pytest.mark.parametrize(
    "invoke",
    [
        pytest.param(lambda env: env.descriptor(), id="descriptor"),
        pytest.param(lambda env: env.shell(), id="shell"),
        pytest.param(lambda env: env.thread("th-1"), id="thread"),
        pytest.param(lambda env: env.dispatch({"type": "noop"}), id="dispatch"),
        pytest.param(lambda env: env.ws_ticket(), id="ws_ticket"),
        pytest.param(lambda env: env.exchange_pairing(TOKEN), id="exchange_pairing"),
        pytest.param(
            lambda env: env.exchange_dpop(TOKEN, lambda method, url: PROOF),
            id="exchange_dpop",
        ),
    ],
)
def test_401_maps_to_not_authenticated(invoke, make_env):
    env = make_env(lambda _req: httpx.Response(401, json={"code": "auth_invalid"}))
    with pytest.raises(NotAuthenticated) as ei:
        invoke(env)
    payload = json.loads(ei.value.to_json())
    assert "not authenticated" in payload["error"]
    assert "hermes t3code login" in payload["hint"]
    assert TOKEN not in payload["error"]
    assert TOKEN not in payload["hint"]
    assert PROOF not in payload["error"]
    assert PROOF not in payload["hint"]


def test_non_2xx_maps_to_t3_api_error(make_env):
    env = make_env(
        lambda _req: httpx.Response(502, text="upstream failed extra detail"),
        headers_fn=_bearer,
    )
    with pytest.raises(T3ApiError) as ei:
        env.shell()
    assert ei.value.status == 502
    payload = json.loads(ei.value.to_json())
    assert "502" in payload["error"]
    assert "upstream failed" in payload["hint"]
    assert TOKEN not in payload["error"]
    assert TOKEN not in payload["hint"]


def test_403_is_t3_api_error_not_auth(make_env):
    env = make_env(
        lambda _req: httpx.Response(403, json={"code": "insufficient_scope"}),
        headers_fn=_bearer,
    )
    with pytest.raises(T3ApiError) as ei:
        env.dispatch({"type": "thread.create"})
    assert ei.value.status == 403
