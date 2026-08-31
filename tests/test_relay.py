"""WI-9: relay discovery, DPoP token exchange, refresh — MockTransport only."""

from __future__ import annotations

import base64
import inspect
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ec import (
    ECDSA,
    EllipticCurvePublicNumbers,
    SECP256R1,
)
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from talaria.auth_t3connect import (
    ACCESS_REFRESH_SKEW,
    ACCESS_SECRET,
    JWT_SUBJECT_TOKEN_TYPE,
    REFRESH_SECRET,
    RELAY_CLIENT_ID,
    RELAY_DPOP_SCOPE,
    DpopEnvClient,
    RelayNotAuthenticated,
    connect_environment,
    dpop_token,
    ensure_environment_access,
    environment_status,
    list_environments,
    reset_relay_cache,
    sync_discovered_environments,
)
from talaria.config import (
    DISCOVERED_STATE_KEY,
    resolve_environment,
    resolve_environments,
)
from talaria.errors import NotAuthenticated, T3ApiError
from talaria.dpop import DPOP_KEY_SECRET, generate_key, jwk_thumbprint, load_or_create_key
from talaria.t3_env import (
    GRANT_TYPE,
    REQUESTED_TOKEN_TYPE,
    SUBJECT_TOKEN_TYPE,
    T3EnvClient,
)
from talaria.tools import handle_t3_environments, handle_t3_list


def _b64url_decode(data: str) -> bytes:
    pad = "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(data + pad)


def verify_proof(proof: str, public_jwk: dict | None = None):
    header_b64, payload_b64, sig_b64 = proof.split(".")
    header = json.loads(_b64url_decode(header_b64))
    payload = json.loads(_b64url_decode(payload_b64))
    jwk = public_jwk or header["jwk"]
    x = int.from_bytes(_b64url_decode(jwk["x"]), "big")
    y = int.from_bytes(_b64url_decode(jwk["y"]), "big")
    pub = EllipticCurvePublicNumbers(x, y, SECP256R1()).public_key()
    sig = _b64url_decode(sig_b64)
    r = int.from_bytes(sig[:32], "big")
    s = int.from_bytes(sig[32:], "big")
    pub.verify(
        encode_dss_signature(r, s),
        f"{header_b64}.{payload_b64}".encode("ascii"),
        ECDSA(hashes.SHA256()),
    )
    return header, payload

RELAY = "https://relay.example.test"
TUNNEL = "https://desktop.example.test"
LAPTOP = "https://t3.example.test"
CLERK = "tok-clerk-access"
CLERK_REFRESH = "tok-clerk-refresh"
CLERK_REFRESHED = "tok-clerk-access-2"
RELAY_ACCESS = "tok-relay-dpop"
SECRET_401 = "secret-body-xyz"
ENV_ACCESS = "tok-env-dpop"
CREDENTIAL = "tok-bootstrap-cred"
ENV_ID = "env-studio-1"
NOW = 1_700_000_000.0


class FakeCtx:
    def __init__(self, settings=None, state=None, secrets=None) -> None:
        self._settings = dict(settings or {})
        self.state = {} if state is None else state
        self.secrets = dict(secrets or {})
        self.config_writes: list[tuple[str, object]] = []

    def get_config(self, key, default=None):
        return self._settings.get(key, default)

    def set_config(self, key, value):
        self.config_writes.append((key, value))
        self._settings[key] = value


class PluginStateLike:
    def __init__(self, data=None) -> None:
        self._data = dict(data or {})

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def _json(status: int, payload) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _endpoint(http_base: str = TUNNEL) -> dict:
    return {
        "httpBaseUrl": http_base,
        "wsBaseUrl": http_base.replace("https://", "wss://") + "/ws",
        "providerKind": "cloudflare_tunnel",
    }


def _dpop_token_payload(access: str = RELAY_ACCESS) -> dict:
    return {
        "access_token": access,
        "issued_token_type": REQUESTED_TOKEN_TYPE,
        "token_type": "DPoP",
        "expires_in": 1800,
        "scope": RELAY_DPOP_SCOPE,
    }


def _connect_payload(*, credential: str | None = CREDENTIAL) -> dict:
    body = {
        "environmentId": ENV_ID,
        "endpoint": _endpoint(),
        "expiresAt": _iso(NOW + 600),
    }
    if credential is not None:
        body["credential"] = credential
    return body


def _patch_get_client(monkeypatch, http) -> None:
    env_mod = inspect.getmodule(T3EnvClient)
    monkeypatch.setattr(env_mod, "get_client", lambda: http)
    relay_mod = list_environments.__globals__["t3_env"]
    if relay_mod is not env_mod:
        monkeypatch.setattr(relay_mod, "get_client", lambda: http)


def _assert_connect_hint(exc: NotAuthenticated) -> dict:
    raw = exc.to_json()
    payload = json.loads(raw)
    assert "not authenticated" in payload["error"]
    assert "hermes t3code connect" in payload["hint"]
    assert "login" not in payload["hint"]
    assert SECRET_401 not in raw
    assert CLERK not in raw
    return payload


def _t3connect_ctx(*, state=None, extra_secrets=None, extra_settings=None) -> FakeCtx:
    settings = {
        "t3connect": {"enabled": True, "relay_url": RELAY},
        "environments": {},
    }
    if extra_settings:
        settings.update(extra_settings)
    secrets = {ACCESS_SECRET: CLERK}
    if extra_secrets:
        secrets.update(extra_secrets)
    return FakeCtx(settings=settings, state=state if state is not None else {}, secrets=secrets)


@pytest.fixture(autouse=True)
def _reset_caches():
    reset_relay_cache()
    yield
    reset_relay_cache()


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


def test_import_opens_no_socket(socket_guard):
    import talaria.auth_t3connect as mod

    assert callable(mod.list_environments)
    assert callable(mod.dpop_token)
    assert callable(mod.connect_environment)
    assert callable(mod.ensure_environment_access)
    assert mod.RELAY_CLIENT_ID == "t3-web"
    assert mod.DEFAULT_RELAY_URL == "https://relay.t3.codes"


def test_list_environments_sends_clerk_bearer(make_http):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(
            200,
            {
                "environments": [
                    {
                        "environmentId": ENV_ID,
                        "label": "studio",
                        "endpoint": _endpoint(),
                        "linkedAt": "2026-08-01T00:00:00.000Z",
                    }
                ]
            },
        )

    ctx = _t3connect_ctx()
    rows = list_environments(ctx, store=ctx.secrets, client=make_http(handler))
    assert rows[0]["environmentId"] == ENV_ID
    req = seen[0]
    assert req.method == "GET"
    assert str(req.url) == f"{RELAY}/v1/environments"
    assert req.headers["authorization"] == f"Bearer {CLERK}"
    assert "dpop" not in req.headers


def test_dpop_token_form_and_proof(make_http):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(
            200,
            {
                "access_token": RELAY_ACCESS,
                "issued_token_type": REQUESTED_TOKEN_TYPE,
                "token_type": "DPoP",
                "expires_in": 1800,
                "scope": RELAY_DPOP_SCOPE,
            },
        )

    ctx = _t3connect_ctx()
    token = dpop_token(ctx, now=NOW, store=ctx.secrets, client=make_http(handler))
    assert token == RELAY_ACCESS
    req = seen[0]
    assert req.method == "POST"
    assert str(req.url) == f"{RELAY}/v1/client/dpop-token"
    assert "authorization" not in req.headers
    form = parse_qs(req.content.decode(), keep_blank_values=True)
    assert form["grant_type"] == [GRANT_TYPE]
    assert form["subject_token"] == [CLERK]
    assert form["subject_token_type"] == [JWT_SUBJECT_TOKEN_TYPE]
    assert form["requested_token_type"] == [REQUESTED_TOKEN_TYPE]
    assert form["resource"] == [RELAY]
    assert form["scope"] == [RELAY_DPOP_SCOPE]
    assert form["client_id"] == [RELAY_CLIENT_ID]
    assert set(form) == {
        "grant_type",
        "subject_token",
        "subject_token_type",
        "requested_token_type",
        "resource",
        "scope",
        "client_id",
    }
    header, payload = verify_proof(req.headers["dpop"])
    assert header["typ"] == "dpop+jwt"
    assert header["alg"] == "ES256"
    assert "d" not in header["jwk"]
    assert payload["htm"] == "POST"
    assert payload["htu"] == f"{RELAY}/v1/client/dpop-token"
    assert "ath" not in payload
    key = load_or_create_key(store=ctx.secrets)
    assert jwk_thumbprint(header["jwk"]) == key.thumbprint()
    assert DPOP_KEY_SECRET in ctx.secrets
    assert "BEGIN" not in ctx.secrets[DPOP_KEY_SECRET]
    assert CLERK not in json.dumps(ctx._settings)


def test_status_and_connect_use_dpop_authorization(make_http):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/client/dpop-token":
            return _json(
                200,
                {
                    "access_token": RELAY_ACCESS,
                    "issued_token_type": REQUESTED_TOKEN_TYPE,
                    "token_type": "DPoP",
                    "expires_in": 1800,
                    "scope": RELAY_DPOP_SCOPE,
                },
            )
        if request.url.path.endswith("/status"):
            return _json(
                200,
                {
                    "environmentId": ENV_ID,
                    "endpoint": _endpoint(),
                    "status": "online",
                    "checkedAt": "2026-08-31T00:00:00.000Z",
                },
            )
        return _json(
            200,
            {
                "environmentId": ENV_ID,
                "endpoint": _endpoint(),
                "credential": CREDENTIAL,
                "expiresAt": _iso(NOW + 600),
            },
        )

    ctx = _t3connect_ctx()
    http = make_http(handler)
    status = environment_status(
        ctx, ENV_ID, now=NOW, store=ctx.secrets, client=http
    )
    assert status["status"] == "online"
    grant = connect_environment(
        ctx, ENV_ID, now=NOW, store=ctx.secrets, client=http
    )
    assert grant.credential == CREDENTIAL
    assert grant.base_url == TUNNEL
    key = load_or_create_key(store=ctx.secrets)
    status_req = next(r for r in seen if r.url.path.endswith("/status"))
    connect_req = next(r for r in seen if r.url.path.endswith("/connect"))
    assert status_req.method == "POST"
    assert status_req.headers["authorization"] == f"DPoP {RELAY_ACCESS}"
    assert connect_req.headers["authorization"] == f"DPoP {RELAY_ACCESS}"
    _header, payload = verify_proof(status_req.headers["dpop"], key.public_jwk())
    assert payload["htm"] == "POST"
    assert payload["htu"] == f"{RELAY}/v1/environments/{ENV_ID}/status"
    assert payload["ath"]
    connect_body = json.loads(connect_req.content)
    assert connect_body == {"clientKeyThumbprint": key.thumbprint()}
    _ch, connect_payload = verify_proof(connect_req.headers["dpop"], key.public_jwk())
    assert connect_payload["htu"] == f"{RELAY}/v1/environments/{ENV_ID}/connect"
    assert connect_payload["ath"]


def test_dpop_env_client_proof_matches_method_and_skips_descriptor(make_http):
    seen: list[httpx.Request] = []
    key = generate_key()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(200, {"ok": True, "projects": [], "threads": []})

    client = DpopEnvClient(
        TUNNEL,
        lambda: ENV_ACCESS,
        lambda method, url, token: key.proof(
            method, url, access_token=token, now=NOW, jti=f"{method}-jti"
        ),
        client=make_http(handler),
    )
    client.descriptor()
    client.shell()
    client.dispatch({"type": "noop"})
    desc, shell, dispatch = seen
    assert desc.url.path == "/.well-known/t3/environment"
    assert "authorization" not in desc.headers
    assert "dpop" not in desc.headers
    assert shell.method == "GET"
    assert shell.headers["authorization"] == f"DPoP {ENV_ACCESS}"
    _h, shell_payload = verify_proof(shell.headers["dpop"], key.public_jwk())
    assert shell_payload["htm"] == "GET"
    assert shell_payload["htu"] == f"{TUNNEL}/api/orchestration/shell"
    assert dispatch.method == "POST"
    _h, post_payload = verify_proof(dispatch.headers["dpop"], key.public_jwk())
    assert post_payload["htm"] == "POST"
    assert post_payload["htu"] == f"{TUNNEL}/api/orchestration/dispatch"


def _access_handler(counts: dict, *, access_expires: int, cred_ttl: float, env_token: str):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        counts[path] = counts.get(path, 0) + 1
        if path == "/v1/client/dpop-token":
            return _json(
                200,
                {
                    "access_token": RELAY_ACCESS,
                    "issued_token_type": REQUESTED_TOKEN_TYPE,
                    "token_type": "DPoP",
                    "expires_in": 7200,
                    "scope": RELAY_DPOP_SCOPE,
                },
            )
        if path.endswith("/connect"):
            return _json(
                200,
                {
                    "environmentId": ENV_ID,
                    "endpoint": _endpoint(),
                    "credential": CREDENTIAL,
                    "expiresAt": _iso(NOW + cred_ttl),
                },
            )
        if path == "/oauth/token":
            form = parse_qs(request.content.decode(), keep_blank_values=True)
            assert form["subject_token_type"] == [SUBJECT_TOKEN_TYPE]
            assert form["subject_token"] == [CREDENTIAL]
            assert form["grant_type"] == [GRANT_TYPE]
            assert "authorization" not in request.headers
            _header, payload = verify_proof(request.headers["dpop"])
            assert payload["htm"] == "POST"
            assert payload["htu"] == f"{TUNNEL}/oauth/token"
            assert "ath" not in payload
            return _json(
                200,
                {
                    "access_token": env_token,
                    "token_type": "DPoP",
                    "expires_in": access_expires,
                    "scope": "orchestration:read orchestration:operate",
                    "issued_token_type": REQUESTED_TOKEN_TYPE,
                },
            )
        return httpx.Response(404, text="unexpected")

    return handler


def test_access_token_reexchanged_before_five_minutes(make_http):
    counts: dict[str, int] = {}
    ctx = _t3connect_ctx()
    http = make_http(
        _access_handler(counts, access_expires=3600, cred_ttl=7200, env_token=ENV_ACCESS)
    )
    first = ensure_environment_access(
        ctx, ENV_ID, now=NOW, store=ctx.secrets, client=http
    )
    assert first.access_token == ENV_ACCESS
    assert first.base_url == TUNNEL
    assert counts["/v1/environments/" + ENV_ID + "/connect"] == 1
    assert counts["/oauth/token"] == 1

    still_ok = NOW + (3600 - ACCESS_REFRESH_SKEW)
    second = ensure_environment_access(
        ctx, ENV_ID, now=still_ok, store=ctx.secrets, client=http
    )
    assert second.access_token == ENV_ACCESS
    assert counts["/oauth/token"] == 1
    assert counts["/v1/environments/" + ENV_ID + "/connect"] == 1

    expiring = NOW + (3600 - ACCESS_REFRESH_SKEW) + 1
    third = ensure_environment_access(
        ctx, ENV_ID, now=expiring, store=ctx.secrets, client=http
    )
    assert third.access_token == ENV_ACCESS
    assert counts["/oauth/token"] == 2
    assert counts["/v1/environments/" + ENV_ID + "/connect"] == 1
    assert counts["/v1/client/dpop-token"] == 1


def test_relay_credential_refetched_when_expired(make_http):
    counts: dict[str, int] = {}
    ctx = _t3connect_ctx()
    http = make_http(
        _access_handler(counts, access_expires=3600, cred_ttl=100, env_token=ENV_ACCESS)
    )
    ensure_environment_access(ctx, ENV_ID, now=NOW, store=ctx.secrets, client=http)
    assert counts["/v1/environments/" + ENV_ID + "/connect"] == 1

    # Access still fresh, so an expired credential is not consulted.
    ensure_environment_access(
        ctx, ENV_ID, now=NOW + 50, store=ctx.secrets, client=http
    )
    assert counts["/v1/environments/" + ENV_ID + "/connect"] == 1

    # Access expiring and credential expired → connect then exchange.
    ensure_environment_access(
        ctx, ENV_ID, now=NOW + (3600 - ACCESS_REFRESH_SKEW) + 1,
        store=ctx.secrets,
        client=http,
    )
    assert counts["/v1/environments/" + ENV_ID + "/connect"] == 2
    assert counts["/oauth/token"] == 2


def test_t3_environments_merges_relay_discovery(make_http, monkeypatch):
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(f"{request.method} {request.url.host}{request.url.path}")
        if request.url.path == "/v1/environments":
            assert request.headers["authorization"] == f"Bearer {CLERK}"
            return _json(
                200,
                {
                    "environments": [
                        {
                            "environmentId": "env-laptop-relay",
                            "label": "laptop",
                            "endpoint": _endpoint("https://other.example.test"),
                            "linkedAt": "2026-08-01T00:00:00.000Z",
                        },
                        {
                            "environmentId": ENV_ID,
                            "label": "studio",
                            "endpoint": _endpoint(TUNNEL),
                            "linkedAt": "2026-08-01T00:00:00.000Z",
                        },
                    ]
                },
            )
        if request.url.path == "/v1/client/dpop-token":
            return _json(200, _dpop_token_payload())
        if request.url.path.endswith("/status"):
            env = request.url.path.rstrip("/").split("/")[-2]
            if env == "env-laptop-relay":
                return _json(
                    200,
                    {
                        "environmentId": "env-laptop-relay",
                        "endpoint": _endpoint("https://other.example.test"),
                        "status": "offline",
                        "checkedAt": "2026-08-31T00:00:00.000Z",
                    },
                )
            return _json(
                200,
                {
                    "environmentId": ENV_ID,
                    "endpoint": _endpoint(TUNNEL),
                    "status": "online",
                    "checkedAt": "2026-08-31T00:00:00.000Z",
                    "descriptor": {
                        "environmentId": ENV_ID,
                        "label": "Studio Box",
                    },
                },
            )
        if request.url.path == "/.well-known/t3/environment":
            assert request.url.host == "t3.example.test"
            return _json(
                200,
                {"environmentId": "env-laptop-1", "label": "Eltmon's MacBook"},
            )
        return httpx.Response(404, text=str(request.url))

    http = make_http(handler)
    _patch_get_client(monkeypatch, http)
    ctx = _t3connect_ctx(
        extra_settings={"environments": {"laptop": {"base_url": LAPTOP}}},
        extra_secrets={"T3CODE_TOKEN_LAPTOP": "tok-laptop"},
    )
    raw = handle_t3_environments({}, ctx=ctx)
    payload = json.loads(raw)
    by_name = {row["name"]: row for row in payload["environments"]}
    assert set(by_name) == {"laptop", "studio", "env-laptop-relay"}
    assert by_name["laptop"]["mode"] == "direct"
    assert by_name["laptop"]["environmentId"] == "env-laptop-1"
    assert by_name["laptop"]["auth"] == "ok"
    assert by_name["laptop"]["live"] is True
    assert by_name["studio"]["mode"] == "t3connect"
    assert by_name["studio"]["environmentId"] == ENV_ID
    assert by_name["studio"]["auth"] == "ok"
    assert by_name["studio"]["live"] is True
    assert by_name["studio"]["label"] == "Studio Box"
    assert by_name["env-laptop-relay"]["mode"] == "t3connect"
    assert by_name["env-laptop-relay"]["live"] is False
    assert by_name["env-laptop-relay"]["environmentId"] == "env-laptop-relay"
    discovered = ctx.state[DISCOVERED_STATE_KEY]
    assert "laptop" not in discovered
    assert discovered["env-laptop-relay"]["base_url"] == "https://other.example.test"
    assert discovered["studio"]["environment_id"] == ENV_ID
    resolved = resolve_environments(ctx)
    assert resolved["laptop"].mode == "direct"
    assert resolved["laptop"].base_url == LAPTOP
    relay_ref = resolve_environment(ctx, "env-laptop-relay")
    assert relay_ref.mode == "t3connect"
    assert relay_ref.base_url == "https://other.example.test"
    assert resolved["studio"].mode == "t3connect"
    assert resolved["studio"].base_url == TUNNEL
    assert "GET relay.example.test/v1/environments" in seen_paths
    assert "POST relay.example.test/v1/environments/env-studio-1/status" in seen_paths
    assert (
        "POST relay.example.test/v1/environments/env-laptop-relay/status"
        in seen_paths
    )
    assert not any(
        "desktop.example.test" in path and "well-known" in path
        for path in seen_paths
    )
    blob = json.dumps(ctx._settings) + json.dumps(ctx.config_writes)
    assert CLERK not in blob
    assert CREDENTIAL not in raw
    assert RELAY_ACCESS not in raw
    assert "tok-laptop" not in raw


def test_sync_discovered_uses_state_set(make_http):
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json(
            200,
            {
                "environments": [
                    {
                        "environmentId": ENV_ID,
                        "label": "Studio Box",
                        "endpoint": _endpoint(),
                        "linkedAt": "2026-08-01T00:00:00.000Z",
                    }
                ]
            },
        )

    ctx = _t3connect_ctx(state=PluginStateLike())
    mapping = sync_discovered_environments(
        ctx, store=ctx.secrets, client=make_http(handler)
    )
    assert "studio-box" in mapping
    assert ctx.state.get(DISCOVERED_STATE_KEY)["studio-box"]["environment_id"] == ENV_ID
    assert mapping["studio-box"]["mode"] == "t3connect"


def _401() -> httpx.Response:
    return httpx.Response(401, json={"error": SECRET_401, "access_token": CLERK})


@pytest.mark.parametrize("kind", ["list", "dpop-token", "status", "connect", "oauth"])
def test_relay_401_is_not_authenticated(make_http, kind):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if kind == "list" and path == "/v1/environments":
            return _401()
        if kind == "dpop-token" and path == "/v1/client/dpop-token":
            return _401()
        if path == "/v1/client/dpop-token":
            return _json(200, _dpop_token_payload())
        if kind == "status" and path.endswith("/status"):
            return _401()
        if kind == "connect" and path.endswith("/connect"):
            return _401()
        if path.endswith("/connect"):
            return _json(200, _connect_payload())
        if kind == "oauth" and path == "/oauth/token":
            return _401()
        return httpx.Response(404, text=str(request.url))

    ctx = _t3connect_ctx()
    http = make_http(handler)
    with pytest.raises(NotAuthenticated) as ei:
        if kind == "list":
            list_environments(ctx, store=ctx.secrets, client=http)
        elif kind == "dpop-token":
            dpop_token(ctx, now=NOW, store=ctx.secrets, client=http)
        elif kind == "status":
            environment_status(ctx, ENV_ID, now=NOW, store=ctx.secrets, client=http)
        elif kind == "connect":
            connect_environment(ctx, ENV_ID, now=NOW, store=ctx.secrets, client=http)
        else:
            ensure_environment_access(
                ctx, ENV_ID, now=NOW, store=ctx.secrets, client=http
            )
    assert isinstance(ei.value, RelayNotAuthenticated)
    _assert_connect_hint(ei.value)


@pytest.mark.parametrize("kind", ["list", "dpop-token", "connect"])
def test_relay_200_non_json_is_t3_api_error(make_http, kind):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if kind == "list" and path == "/v1/environments":
            return httpx.Response(200, text="not-json-oops")
        if kind == "dpop-token" and path == "/v1/client/dpop-token":
            return httpx.Response(200, text="not-json-oops")
        if path == "/v1/client/dpop-token":
            return _json(200, _dpop_token_payload())
        if kind == "connect" and path.endswith("/connect"):
            return httpx.Response(200, text="not-json-oops")
        return httpx.Response(404, text=str(request.url))

    ctx = _t3connect_ctx()
    http = make_http(handler)
    with pytest.raises(T3ApiError) as ei:
        if kind == "list":
            list_environments(ctx, store=ctx.secrets, client=http)
        elif kind == "dpop-token":
            dpop_token(ctx, now=NOW, store=ctx.secrets, client=http)
        else:
            connect_environment(ctx, ENV_ID, now=NOW, store=ctx.secrets, client=http)
    assert ei.value.status == 200
    payload = json.loads(ei.value.to_json())
    assert "200" in payload["error"]
    assert CLERK not in ei.value.to_json()


def test_connect_missing_credential_is_t3_api_error(make_http):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/client/dpop-token":
            return _json(200, _dpop_token_payload())
        if request.url.path.endswith("/connect"):
            return _json(200, _connect_payload(credential=None))
        return httpx.Response(404, text=str(request.url))

    ctx = _t3connect_ctx()
    with pytest.raises(T3ApiError) as ei:
        connect_environment(
            ctx, ENV_ID, now=NOW, store=ctx.secrets, client=make_http(handler)
        )
    payload = json.loads(ei.value.to_json())
    assert "credential" in payload["hint"]
    assert CREDENTIAL not in ei.value.to_json()


def test_t3_environments_mode_b_401_is_not_empty_config(make_http, monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return _401()

    _patch_get_client(monkeypatch, make_http(handler))
    raw = handle_t3_environments({}, ctx=_t3connect_ctx())
    payload = json.loads(raw)
    assert payload.get("error") != "no environments configured"
    assert "not authenticated" in payload["error"]
    assert "hermes t3code connect" in payload["hint"]
    assert "login" not in payload["hint"]
    assert SECRET_401 not in raw
    assert CLERK not in raw
    assert "environments" not in payload


def test_t3_environments_retries_list_after_clerk_refresh(make_http, monkeypatch):
    seen_auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "clerk.t3.codes" and request.url.path == "/oauth/token":
            form = parse_qs(request.content.decode(), keep_blank_values=True)
            assert form["grant_type"] == ["refresh_token"]
            assert form["refresh_token"] == [CLERK_REFRESH]
            return _json(
                200,
                {
                    "access_token": CLERK_REFRESHED,
                    "refresh_token": CLERK_REFRESH,
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        if request.url.path == "/v1/environments":
            seen_auth.append(request.headers.get("authorization", ""))
            if request.headers.get("authorization") == f"Bearer {CLERK}":
                return _401()
            assert request.headers.get("authorization") == f"Bearer {CLERK_REFRESHED}"
            return _json(
                200,
                {
                    "environments": [
                        {
                            "environmentId": ENV_ID,
                            "label": "studio",
                            "endpoint": _endpoint(),
                            "linkedAt": "2026-08-01T00:00:00.000Z",
                        }
                    ]
                },
            )
        if request.url.path == "/v1/client/dpop-token":
            return _json(200, _dpop_token_payload())
        if request.url.path.endswith("/status"):
            return _json(
                200,
                {
                    "environmentId": ENV_ID,
                    "endpoint": _endpoint(),
                    "status": "online",
                    "checkedAt": "2026-08-31T00:00:00.000Z",
                    "descriptor": {"environmentId": ENV_ID, "label": "studio"},
                },
            )
        return httpx.Response(404, text=str(request.url))

    http = make_http(handler)
    _patch_get_client(monkeypatch, http)
    ctx = _t3connect_ctx(extra_secrets={REFRESH_SECRET: CLERK_REFRESH})
    payload = json.loads(handle_t3_environments({}, ctx=ctx))
    assert [row["name"] for row in payload["environments"]] == ["studio"]
    assert payload["environments"][0]["live"] is True
    assert seen_auth == [f"Bearer {CLERK}", f"Bearer {CLERK_REFRESHED}"]
    assert ctx.secrets[ACCESS_SECRET] == CLERK_REFRESHED
    assert SECRET_401 not in json.dumps(payload)


def _mode_b_runtime_handler(*, shell_status: int = 200) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/environments":
            return _json(
                200,
                {
                    "environments": [
                        {
                            "environmentId": ENV_ID,
                            "label": "studio",
                            "endpoint": _endpoint(),
                            "linkedAt": "2026-08-01T00:00:00.000Z",
                        }
                    ]
                },
            )
        if path == "/v1/client/dpop-token":
            return _json(200, _dpop_token_payload())
        if path.endswith("/connect"):
            return _json(200, _connect_payload())
        if path == "/oauth/token":
            return _json(
                200,
                {
                    "access_token": ENV_ACCESS,
                    "token_type": "DPoP",
                    "expires_in": 3600,
                    "issued_token_type": REQUESTED_TOKEN_TYPE,
                },
            )
        if path == "/api/orchestration/shell":
            if shell_status == 401:
                return _401()
            return _json(200, {"projects": [], "threads": []})
        return httpx.Response(404, text=str(request.url))

    return handler


def test_t3_list_discovered_env_returns_json(make_http, monkeypatch):
    _patch_get_client(monkeypatch, make_http(_mode_b_runtime_handler()))
    raw = handle_t3_list({}, ctx=_t3connect_ctx())
    payload = json.loads(raw)
    assert payload["environment"] == "studio"
    assert payload["projects"] == []
    assert payload["threads"] == []
    assert ENV_ACCESS not in raw
    assert CREDENTIAL not in raw


def test_t3_list_discovered_env_401_is_json_not_raise(make_http, monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return _401()

    _patch_get_client(monkeypatch, make_http(handler))
    raw = handle_t3_list({}, ctx=_t3connect_ctx())
    payload = json.loads(raw)
    assert payload.get("error") != "no environments configured"
    assert "not authenticated" in payload["error"]
    assert "hermes t3code connect" in payload["hint"]
    assert SECRET_401 not in raw
