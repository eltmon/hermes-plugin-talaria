"""WI-8: Clerk PKCE headless OAuth — MockTransport only, no live Clerk."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from talaria.auth_t3connect import (
    ACCESS_SECRET,
    DEFAULT_CLERK_OAUTH_CLIENT_ID,
    DEFAULT_HOSTED_APP_URL,
    MALFORMED_CODE_MESSAGE,
    PENDING_SECRET,
    REFRESH_SECRET,
    WRONG_STATE_MESSAGE,
    ConnectAuthError,
    check_connect_auth_code,
    clerk_frontend_api_hostname,
    complete_connect,
    generate_pkce,
    parse_connect_auth_code,
    refresh_clerk_tokens,
    start_connect,
)
from talaria.cli import register_cli, t3code_command

PUBLISHABLE_KEY = "pk_live_Y2xlcmsudDMuY29kZXMk"
ACCESS = "tok-clerk-access"
REFRESH = "tok-clerk-refresh"
CODE = "clerk-code-123"
TOKEN_RESPONSE = {
    "access_token": ACCESS,
    "refresh_token": REFRESH,
    "expires_in": 3600,
    "token_type": "Bearer",
    "unexpectedField": "keep-me",
}


class FakeCtx:
    def __init__(self, settings=None) -> None:
        self._settings = dict(settings or {})
        self.config_writes: list[tuple[str, object]] = []
        self.cli: list[dict] = []

    def get_config(self, key, default=None):
        return self._settings.get(key, default)

    def set_config(self, key, value):
        self.config_writes.append((key, value))
        self._settings[key] = value

    def register_cli_command(self, name, **kwargs):
        self.cli.append({"name": name, **kwargs})


@pytest.fixture(autouse=True)
def _reset_pending():
    import talaria.auth_t3connect as mod

    mod._pending = None
    yield
    mod._pending = None


@pytest.fixture
def make_http():
    clients: list[httpx.Client] = []

    def factory(handler) -> httpx.Client:
        http = httpx.Client(transport=httpx.MockTransport(handler), timeout=30.0)
        clients.append(http)
        return http

    yield factory
    for http in clients:
        http.close()


def _json(status: int, payload) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _config_blob(ctx: FakeCtx) -> str:
    return json.dumps(ctx._settings) + json.dumps(
        [value for _key, value in ctx.config_writes]
    )


def test_import_opens_no_socket(socket_guard):
    import talaria.auth_t3connect as mod  # noqa: F401

    assert callable(mod.start_connect)
    assert callable(mod.complete_connect)
    assert callable(mod.refresh_clerk_tokens)


def test_publishable_key_derives_clerk_t3_codes():
    assert clerk_frontend_api_hostname(PUBLISHABLE_KEY) == "clerk.t3.codes"


def test_pk_test_publishable_key_with_padding():
    # pk_test_<base64 of "clerk.example.test$"> from t3code CliTokenManager.test.ts
    key = "pk_test_Y2xlcmsuZXhhbXBsZS50ZXN0JA=="
    assert clerk_frontend_api_hostname(key) == "clerk.example.test"


def test_pkce_challenge_is_s256_of_verifier():
    pkce = generate_pkce()
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", pkce.verifier)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", pkce.challenge)
    assert re.fullmatch(r"[A-Za-z0-9_-]{22}", pkce.state)
    assert "=" not in pkce.verifier + pkce.challenge + pkce.state
    digest = hashlib.sha256(pkce.verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    assert pkce.challenge == expected


def test_pkce_state_and_verifier_are_unique():
    a = generate_pkce()
    b = generate_pkce()
    assert a.verifier != b.verifier
    assert a.state != b.state
    assert a.challenge != b.challenge


def test_start_connect_authorize_url_hash_params_no_port():
    session = start_connect(ctx=FakeCtx(), store={})
    parsed = urlparse(session.authorize_url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "app.t3.codes"
    assert parsed.path == "/connect"
    assert parsed.query == ""
    params = parse_qs(parsed.fragment, keep_blank_values=True)
    assert params["state"] == [session.state]
    assert params["challenge"] == [session.challenge]
    assert "port" not in params
    fragment = parsed.fragment
    assert fragment.startswith("state=")
    assert "&challenge=" in fragment
    assert "port=" not in fragment


def test_start_connect_uses_t3connect_config_overrides():
    ctx = FakeCtx(
        settings={
            "t3connect": {
                "hosted_app_url": "https://hosted.example.test",
                "clerk_oauth_client_id": "oauth_client_test",
                "clerk_publishable_key": "pk_test_Y2xlcmsuZXhhbXBsZS50ZXN0JA==",
            }
        }
    )
    session = start_connect(ctx=ctx, store={})
    parsed = urlparse(session.authorize_url)
    assert parsed.netloc == "hosted.example.test"
    assert parsed.path == "/connect"
    assert session.client_id == "oauth_client_test"
    assert clerk_frontend_api_hostname(session.publishable_key) == "clerk.example.test"


def test_parse_connect_auth_code_preserves_dots_in_code():
    parsed = parse_connect_auth_code("az9.code.chunk.state-uuid")
    assert parsed is not None
    assert parsed.code == "az9.code.chunk"
    assert parsed.state == "state-uuid"
    padded = parse_connect_auth_code("  az9.code.chunk.state-uuid\n")
    assert padded is not None
    assert padded.code == "az9.code.chunk"


def test_parse_connect_auth_code_rejects_malformed():
    for blob in ("", "no-separator", ".leading", "trailing.", "   "):
        assert parse_connect_auth_code(blob) is None


def test_check_connect_auth_code_malformed_user_facing():
    assert check_connect_auth_code("no-separator", "state") == MALFORMED_CODE_MESSAGE


def test_check_connect_auth_code_wrong_state_user_facing():
    assert (
        check_connect_auth_code("clerk-code-123.wrong-state", "expected")
        == WRONG_STATE_MESSAGE
    )


def test_complete_connect_rejects_malformed_without_http(make_http):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(200, TOKEN_RESPONSE)

    ctx = FakeCtx()
    store: dict[str, str] = {}
    session = start_connect(ctx=ctx, store=store)
    with pytest.raises(ConnectAuthError, match="does not look like a T3 Connect code"):
        complete_connect(
            "not-a-blob",
            session=session,
            ctx=ctx,
            store=store,
            client=make_http(handler),
        )
    assert seen == []
    assert ACCESS_SECRET not in store
    assert REFRESH_SECRET not in store


def test_complete_connect_rejects_wrong_state_without_http(make_http):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(200, TOKEN_RESPONSE)

    ctx = FakeCtx()
    store: dict[str, str] = {}
    session = start_connect(ctx=ctx, store=store)
    with pytest.raises(ConnectAuthError, match="different connect request"):
        complete_connect(
            f"{CODE}.wrong-state",
            session=session,
            ctx=ctx,
            store=store,
            client=make_http(handler),
        )
    assert seen == []
    assert ACCESS_SECRET not in store


def test_token_exchange_request_shape(make_http):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(200, TOKEN_RESPONSE)

    ctx = FakeCtx()
    store: dict[str, str] = {}
    session = start_connect(ctx=ctx, store=store)
    blob = f"{CODE}.{session.state}"
    tokens = complete_connect(
        blob, session=session, ctx=ctx, store=store, client=make_http(handler)
    )
    assert tokens.access_token == ACCESS
    assert tokens.refresh_token == REFRESH
    assert tokens.expires_in == 3600
    assert len(seen) == 1
    req = seen[0]
    assert req.method == "POST"
    assert req.url.host == "clerk.t3.codes"
    assert req.url.path == "/oauth/token"
    assert req.url.scheme == "https"
    form = parse_qs(req.content.decode(), keep_blank_values=True)
    assert form["grant_type"] == ["authorization_code"]
    assert form["code"] == [CODE]
    assert form["redirect_uri"] == [f"{DEFAULT_HOSTED_APP_URL}/connect/callback"]
    assert form["client_id"] == [DEFAULT_CLERK_OAUTH_CLIENT_ID]
    assert form["code_verifier"] == [session.verifier]
    assert set(form) == {
        "grant_type",
        "code",
        "redirect_uri",
        "client_id",
        "code_verifier",
    }
    digest = hashlib.sha256(session.verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    assert expected == session.challenge
    ctype = req.headers.get("content-type", "")
    assert "application/x-www-form-urlencoded" in ctype


def test_complete_connect_stores_secrets_not_config(make_http):
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json(200, TOKEN_RESPONSE)

    ctx = FakeCtx()
    store: dict[str, str] = {}
    session = start_connect(ctx=ctx, store=store)
    complete_connect(
        f"{CODE}.{session.state}",
        session=session,
        ctx=ctx,
        store=store,
        client=make_http(handler),
    )
    assert store[ACCESS_SECRET] == ACCESS
    assert store[REFRESH_SECRET] == REFRESH
    assert PENDING_SECRET not in store
    blob = _config_blob(ctx)
    assert ACCESS not in blob
    assert REFRESH not in blob
    assert session.verifier not in blob
    assert ctx.config_writes == []


def test_refresh_clerk_tokens_request_shape(make_http):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(
            200,
            {
                "access_token": "tok-new-access",
                "refresh_token": "tok-new-refresh",
                "expires_in": 1800,
                "token_type": "Bearer",
            },
        )

    ctx = FakeCtx()
    store = {ACCESS_SECRET: ACCESS, REFRESH_SECRET: REFRESH}
    tokens = refresh_clerk_tokens(ctx=ctx, store=store, client=make_http(handler))
    assert tokens.access_token == "tok-new-access"
    assert tokens.refresh_token == "tok-new-refresh"
    assert store[ACCESS_SECRET] == "tok-new-access"
    assert store[REFRESH_SECRET] == "tok-new-refresh"
    req = seen[0]
    assert req.method == "POST"
    assert req.url.host == "clerk.t3.codes"
    assert req.url.path == "/oauth/token"
    form = parse_qs(req.content.decode(), keep_blank_values=True)
    assert form["grant_type"] == ["refresh_token"]
    assert form["refresh_token"] == [REFRESH]
    assert form["client_id"] == [DEFAULT_CLERK_OAUTH_CLIENT_ID]
    assert set(form) == {"grant_type", "refresh_token", "client_id"}


def test_refresh_keeps_old_refresh_token_when_omitted(make_http):
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json(
            200,
            {
                "access_token": "tok-new-access",
                "expires_in": 1800,
                "token_type": "Bearer",
            },
        )

    store = {ACCESS_SECRET: ACCESS, REFRESH_SECRET: REFRESH}
    tokens = refresh_clerk_tokens(
        ctx=FakeCtx(), store=store, client=make_http(handler)
    )
    assert tokens.refresh_token == REFRESH
    assert store[REFRESH_SECRET] == REFRESH
    assert store[ACCESS_SECRET] == "tok-new-access"


def test_cli_connect_prints_authorize_url(capsys):
    ctx = FakeCtx()
    store: dict[str, str] = {}
    ns = argparse.Namespace(t3code_command="connect", code=None)
    assert t3code_command(ns, ctx, store=store) == 0
    out = capsys.readouterr().out
    assert "https://app.t3.codes/connect#" in out
    assert "state=" in out
    assert "challenge=" in out
    assert "port=" not in out
    assert ACCESS not in out
    assert ACCESS_SECRET not in store
    parsed = urlparse(
        next(line.strip() for line in out.splitlines() if line.strip().startswith("https://"))
    )
    params = parse_qs(parsed.fragment)
    assert "state" in params and "challenge" in params


def test_cli_connect_code_completes_and_hides_tokens(make_http, capsys):
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json(200, TOKEN_RESPONSE)

    ctx = FakeCtx()
    store: dict[str, str] = {}
    start = argparse.Namespace(t3code_command="connect", code=None)
    assert t3code_command(start, ctx, store=store) == 0
    out = capsys.readouterr().out
    url = next(line.strip() for line in out.splitlines() if line.strip().startswith("https://"))
    state = parse_qs(urlparse(url).fragment)["state"][0]
    ns = argparse.Namespace(t3code_command="connect", code=f"{CODE}.{state}")
    assert t3code_command(ns, ctx, store=store, client=make_http(handler)) == 0
    captured = capsys.readouterr()
    assert "Connected to T3 Connect" in captured.out
    assert ACCESS not in captured.out
    assert ACCESS not in captured.err
    assert REFRESH not in captured.out
    assert store[ACCESS_SECRET] == ACCESS
    assert store[REFRESH_SECRET] == REFRESH
    assert ACCESS not in _config_blob(ctx)


def test_cli_connect_wrong_state_prints_user_facing(make_http, capsys):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(200, TOKEN_RESPONSE)

    ctx = FakeCtx()
    store: dict[str, str] = {}
    start = argparse.Namespace(t3code_command="connect", code=None)
    assert t3code_command(start, ctx, store=store) == 0
    capsys.readouterr()
    ns = argparse.Namespace(t3code_command="connect", code=f"{CODE}.wrong-state")
    assert t3code_command(ns, ctx, store=store, client=make_http(handler)) == 1
    err = capsys.readouterr().err
    assert WRONG_STATE_MESSAGE in err
    assert seen == []
    assert ACCESS_SECRET not in store


def test_cli_connect_malformed_prints_user_facing(capsys):
    ctx = FakeCtx()
    store: dict[str, str] = {}
    start = argparse.Namespace(t3code_command="connect", code=None)
    assert t3code_command(start, ctx, store=store) == 0
    capsys.readouterr()
    ns = argparse.Namespace(t3code_command="connect", code="no-separator")
    assert t3code_command(ns, ctx, store=store) == 1
    assert MALFORMED_CODE_MESSAGE in capsys.readouterr().err


def test_register_cli_connect_accepts_code_flag():
    ctx = FakeCtx()
    register_cli(ctx)
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers()
    t3 = subs.add_parser("t3code")
    ctx.cli[0]["setup_fn"](t3)
    ns = parser.parse_args(["t3code", "connect"])
    assert ns.t3code_command == "connect"
    assert ns.code is None
    ns2 = parser.parse_args(["t3code", "connect", "--code", "abc.state"])
    assert ns2.code == "abc.state"
