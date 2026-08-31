"""Mode B: Clerk PKCE OAuth, relay discovery, and DPoP for T3 Connect.

Mirrors t3code ``connectAuth.ts`` (hash authorize URL, ``<code>.<state>`` blob),
``CliTokenManager.outOfBandOAuthLogin``, and ``managedRelay.ts``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlparse

import httpx

from . import t3_env
from .auth_direct import slugify_label
from .config import DISCOVERED_STATE_KEY, get_secret, resolve_environment, set_secret
from .dpop import load_or_create_key
from .errors import EnvironmentNotFound, NotAuthenticated, T3ApiError, TalariaError
from .t3_env import GRANT_TYPE, REQUESTED_TOKEN_TYPE, T3EnvClient

DEFAULT_HOSTED_APP_URL = "https://app.t3.codes"
DEFAULT_CLERK_OAUTH_CLIENT_ID = "hzxSgY2cH10sDU2r"
DEFAULT_CLERK_PUBLISHABLE_KEY = "pk_live_Y2xlcmsudDMuY29kZXMk"
DEFAULT_RELAY_URL = "https://relay.t3.codes"
RELAY_CLIENT_ID = "t3-web"
RELAY_DPOP_SCOPE = "environment:connect environment:status"
JWT_SUBJECT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"
ACCESS_REFRESH_SKEW = 300

ACCESS_SECRET = "T3CODE_CLERK_ACCESS_TOKEN"
REFRESH_SECRET = "T3CODE_CLERK_REFRESH_TOKEN"
PENDING_SECRET = "T3CODE_CLERK_PKCE_PENDING"

CONNECT_AUTHORIZE_PATH = "/connect"
CONNECT_CALLBACK_PATH = "/connect/callback"
CONNECT_AUTH_CODE_SEPARATOR = "."

MALFORMED_CODE_MESSAGE = (
    "That does not look like a T3 Connect code. Copy the full code."
)
WRONG_STATE_MESSAGE = (
    "That code belongs to a different connect request. Open the URL above and try again."
)
NO_PENDING_MESSAGE = (
    "No connect request in progress. Run `hermes t3code connect` first."
)


class ConnectAuthError(TalariaError):
    """User-facing T3 Connect OAuth failure."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            "run `hermes t3code connect` to sign in to T3 Connect",
        )


class RelayNotAuthenticated(NotAuthenticated):
    """Mode B 401 / missing Clerk token — hint names ``hermes t3code connect``."""

    def __init__(self, environment: str | None = None) -> None:
        self.environment = environment
        error = (
            f"not authenticated for {environment}"
            if environment
            else "not authenticated"
        )
        TalariaError.__init__(
            self,
            error,
            "run `hermes t3code connect` to sign in to T3 Connect",
        )


@dataclass(frozen=True)
class Pkce:
    verifier: str
    challenge: str
    state: str


@dataclass(frozen=True)
class ConnectAuthCode:
    code: str
    state: str


@dataclass(frozen=True)
class ConnectSession:
    verifier: str
    challenge: str
    state: str
    authorize_url: str
    hosted_app_url: str
    client_id: str
    publishable_key: str


@dataclass(frozen=True)
class ClerkTokens:
    access_token: str
    refresh_token: str
    expires_in: int | None = None


@dataclass
class _CachedRelayToken:
    access_token: str
    expires_at: float
    relay_url: str


@dataclass
class _CachedEnvAccess:
    environment_id: str
    base_url: str
    access_token: str
    access_expires_at: float
    credential: str
    credential_expires_at: float


@dataclass(frozen=True)
class EnvironmentAccess:
    base_url: str
    access_token: str
    environment_id: str


@dataclass(frozen=True)
class ConnectGrant:
    environment_id: str
    base_url: str
    credential: str
    expires_at: float
    endpoint: dict


_pending: ConnectSession | None = None
_relay_token: _CachedRelayToken | None = None
_env_access: dict[str, _CachedEnvAccess] = {}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def generate_pkce() -> Pkce:
    """RFC 7636 S256: 32-byte verifier, challenge = base64url(SHA256(verifier))."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = _b64url(secrets.token_bytes(16))
    return Pkce(verifier=verifier, challenge=challenge, state=state)


def clerk_frontend_api_hostname(publishable_key: str) -> str:
    """Strip ``pk_live_``/``pk_test_``, base64-decode, drop trailing ``$``."""
    encoded = "_".join((publishable_key or "").split("_")[2:])
    try:
        pad = "=" * ((4 - len(encoded) % 4) % 4)
        frontend_api = base64.b64decode(encoded + pad).decode("utf-8")
        if frontend_api.endswith("$"):
            frontend_api = frontend_api[:-1]
    except Exception as exc:
        raise ConnectAuthError("Failed to decode Clerk publishable key.") from exc
    if not frontend_api:
        raise ConnectAuthError("Failed to decode Clerk publishable key.")
    if "/" in frontend_api:
        raise ConnectAuthError("Invalid Clerk frontend API in publishable key.")
    hostname = urlparse(f"https://{frontend_api}").hostname
    if not hostname:
        raise ConnectAuthError("Invalid Clerk frontend API in publishable key.")
    return hostname


def clerk_frontend_api_url(publishable_key: str) -> str:
    return f"https://{clerk_frontend_api_hostname(publishable_key)}"


def clerk_token_endpoint(publishable_key: str) -> str:
    return f"{clerk_frontend_api_url(publishable_key)}/oauth/token"


def build_connect_authorize_url(
    hosted_app_url: str, state: str, challenge: str
) -> str:
    """``{hosted}/connect#state=…&challenge=…``. No port param (out-of-band)."""
    url = urljoin(hosted_app_url, CONNECT_AUTHORIZE_PATH)
    fragment = urlencode({"state": state, "challenge": challenge})
    return f"{url}#{fragment}"


def connect_callback_url(hosted_app_url: str) -> str:
    return urljoin(hosted_app_url, CONNECT_CALLBACK_PATH)


def parse_connect_auth_code(blob: str) -> ConnectAuthCode | None:
    trimmed = blob.strip() if isinstance(blob, str) else ""
    separator_index = trimmed.rfind(CONNECT_AUTH_CODE_SEPARATOR)
    if separator_index <= 0 or separator_index == len(trimmed) - 1:
        return None
    return ConnectAuthCode(
        code=trimmed[:separator_index],
        state=trimmed[separator_index + 1 :],
    )


def check_connect_auth_code(
    blob: str, expected_state: str
) -> ConnectAuthCode | str:
    """Return parsed code or a user-facing error string (checkConnectAuthCode)."""
    parsed = parse_connect_auth_code(blob)
    if parsed is None:
        return MALFORMED_CODE_MESSAGE
    if parsed.state != expected_state:
        return WRONG_STATE_MESSAGE
    return parsed


def start_connect(*, ctx=None, store: MutableMapping[str, str] | None = None) -> ConnectSession:
    hosted, client_id, publishable_key = _t3connect_settings(ctx)
    pkce = generate_pkce()
    session = ConnectSession(
        verifier=pkce.verifier,
        challenge=pkce.challenge,
        state=pkce.state,
        authorize_url=build_connect_authorize_url(
            hosted, pkce.state, pkce.challenge
        ),
        hosted_app_url=hosted,
        client_id=client_id,
        publishable_key=publishable_key,
    )
    _save_pending(session, store=store)
    return session


def complete_connect(
    blob: str,
    *,
    session: ConnectSession | None = None,
    ctx=None,
    store: MutableMapping[str, str] | None = None,
    client: httpx.Client | None = None,
) -> ClerkTokens:
    resolved = session or _load_pending(store=store)
    if resolved is None:
        raise ConnectAuthError(NO_PENDING_MESSAGE)
    checked = check_connect_auth_code(blob, resolved.state)
    if isinstance(checked, str):
        raise ConnectAuthError(checked)
    tokens = _exchange_clerk_token(
        {
            "grant_type": "authorization_code",
            "code": checked.code,
            "redirect_uri": connect_callback_url(resolved.hosted_app_url),
            "client_id": resolved.client_id,
            "code_verifier": resolved.verifier,
        },
        publishable_key=resolved.publishable_key,
        client=client,
        fallback_refresh="",
    )
    _store_tokens(tokens, store=store)
    _clear_pending(store=store)
    return tokens


def refresh_clerk_tokens(
    *,
    ctx=None,
    store: Mapping[str, str] | MutableMapping[str, str] | None = None,
    client: httpx.Client | None = None,
) -> ClerkTokens:
    refresh = get_secret(REFRESH_SECRET, store=store)
    if not refresh:
        raise ConnectAuthError(
            "No T3 Connect refresh token. Run `hermes t3code connect`."
        )
    _hosted, client_id, publishable_key = _t3connect_settings(ctx)
    tokens = _exchange_clerk_token(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": client_id,
        },
        publishable_key=publishable_key,
        client=client,
        fallback_refresh=refresh,
    )
    _store_tokens(tokens, store=store)
    return tokens


def t3connect_enabled(ctx) -> bool:
    return _t3connect_raw(ctx).get("enabled", True) is not False


def relay_origin(ctx) -> str:
    return _setting(_t3connect_raw(ctx), "relay_url", DEFAULT_RELAY_URL).rstrip("/")


def reset_relay_cache() -> None:
    global _relay_token
    _relay_token = None
    _env_access.clear()


def list_environments(
    ctx,
    *,
    store: Mapping[str, str] | None = None,
    client: httpx.Client | None = None,
) -> list[dict]:
    clerk = _clerk_access(store)
    url = f"{relay_origin(ctx)}/v1/environments"
    payload = _relay_json(
        _http(client).get(url, headers={"Authorization": f"Bearer {clerk}"})
    )
    rows = payload.get("environments") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def dpop_token(
    ctx,
    *,
    now: float | None = None,
    store: Mapping[str, str] | MutableMapping[str, str] | None = None,
    client: httpx.Client | None = None,
) -> str:
    """Exchange the Clerk JWT for a relay DPoP access token (cached)."""
    global _relay_token
    epoch = _now(now)
    origin = relay_origin(ctx)
    cached = _relay_token
    if (
        cached is not None
        and cached.relay_url == origin
        and cached.expires_at - ACCESS_REFRESH_SKEW >= epoch
    ):
        return cached.access_token
    clerk = _clerk_access(store)
    key = load_or_create_key(store=store)
    url = f"{origin}/v1/client/dpop-token"
    proof = key.proof("POST", url, now=epoch)
    payload = _relay_json(
        _http(client).post(
            url,
            data={
                "grant_type": GRANT_TYPE,
                "subject_token": clerk,
                "subject_token_type": JWT_SUBJECT_TOKEN_TYPE,
                "requested_token_type": REQUESTED_TOKEN_TYPE,
                "resource": origin,
                "scope": RELAY_DPOP_SCOPE,
                "client_id": RELAY_CLIENT_ID,
            },
            headers={"dpop": proof},
        )
    )
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise T3ApiError(200, "relay dpop-token returned no access_token")
    _relay_token = _CachedRelayToken(
        access_token=token,
        expires_at=epoch + _expires_in(payload),
        relay_url=origin,
    )
    return token


def environment_status(
    ctx,
    env_id: str,
    *,
    now: float | None = None,
    store: Mapping[str, str] | MutableMapping[str, str] | None = None,
    client: httpx.Client | None = None,
) -> dict:
    epoch = _now(now)
    origin = relay_origin(ctx)
    url = f"{origin}/v1/environments/{quote(str(env_id), safe='')}/status"
    payload = _relay_json(
        _http(client).post(
            url,
            headers=_dpop_auth_headers(
                ctx, url, now=epoch, store=store, client=client
            ),
        )
    )
    return payload if isinstance(payload, dict) else {}


def connect_environment(
    ctx,
    env_id: str,
    *,
    now: float | None = None,
    store: Mapping[str, str] | MutableMapping[str, str] | None = None,
    client: httpx.Client | None = None,
) -> ConnectGrant:
    epoch = _now(now)
    origin = relay_origin(ctx)
    key = load_or_create_key(store=store)
    url = f"{origin}/v1/environments/{quote(str(env_id), safe='')}/connect"
    payload = _relay_json(
        _http(client).post(
            url,
            headers=_dpop_auth_headers(
                ctx, url, now=epoch, store=store, key=key, client=client
            ),
            json={"clientKeyThumbprint": key.thumbprint()},
        )
    )
    if not isinstance(payload, Mapping):
        raise T3ApiError(200, "relay connect returned invalid JSON")
    credential = payload.get("credential")
    if not isinstance(credential, str) or not credential:
        raise T3ApiError(200, "relay connect returned no credential")
    endpoint = payload.get("endpoint")
    if not isinstance(endpoint, Mapping):
        raise T3ApiError(200, "relay connect returned no endpoint")
    base_url = endpoint.get("httpBaseUrl")
    if not isinstance(base_url, str) or not base_url.strip():
        raise T3ApiError(200, "relay connect returned no httpBaseUrl")
    base_url = base_url.strip().rstrip("/")
    connected_id = payload.get("environmentId", env_id)
    return ConnectGrant(
        environment_id=str(connected_id),
        base_url=base_url,
        credential=credential,
        expires_at=_as_epoch(payload.get("expiresAt"), now=epoch),
        endpoint=dict(endpoint),
    )


def ensure_environment_access(
    ctx,
    env_id: str,
    *,
    now: float | None = None,
    store: Mapping[str, str] | MutableMapping[str, str] | None = None,
    client: httpx.Client | None = None,
) -> EnvironmentAccess:
    """Return a live environment DPoP access token, refreshing both token tiers."""
    epoch = _now(now)
    resolved_id, base_hint = _resolve_env_id(ctx, env_id)
    cached = _env_access.get(resolved_id)
    if cached is not None and cached.access_expires_at - ACCESS_REFRESH_SKEW >= epoch:
        return EnvironmentAccess(
            base_url=cached.base_url,
            access_token=cached.access_token,
            environment_id=cached.environment_id,
        )
    key = load_or_create_key(store=store)
    if (
        cached is not None
        and cached.credential
        and cached.credential_expires_at > epoch
    ):
        return _store_env_access(
            _exchange_env_access(
                cached.base_url,
                cached.credential,
                key,
                environment_id=cached.environment_id,
                credential_expires_at=cached.credential_expires_at,
                now=epoch,
                client=client,
            )
        )
    grant = connect_environment(
        ctx, resolved_id, now=epoch, store=store, client=client
    )
    base_url = grant.base_url or base_hint or ""
    return _store_env_access(
        _exchange_env_access(
            base_url,
            grant.credential,
            key,
            environment_id=grant.environment_id,
            credential_expires_at=grant.expires_at,
            now=epoch,
            client=client,
        )
    )


def sync_discovered_environments(
    ctx,
    *,
    store: Mapping[str, str] | MutableMapping[str, str] | None = None,
    client: httpx.Client | None = None,
) -> dict[str, dict]:
    try:
        rows = list_environments(ctx, store=store, client=client)
    except NotAuthenticated:
        if not get_secret(REFRESH_SECRET, store=store):
            raise
        refresh_clerk_tokens(ctx=ctx, store=store, client=client)
        rows = list_environments(ctx, store=store, client=client)
    mapping = discovered_environment_map(
        rows, mode_a_names=_mode_a_names(ctx)
    )
    _write_discovered(ctx, mapping)
    return mapping


def discovered_environment_map(
    rows: list, *, mode_a_names: set[str] | None = None
) -> dict[str, dict]:
    taken = set(mode_a_names or ())
    out: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        env_id = row.get("environmentId")
        endpoint = row.get("endpoint")
        base = None
        if isinstance(endpoint, Mapping):
            base = endpoint.get("httpBaseUrl")
        if not isinstance(env_id, str) or not env_id:
            continue
        if not isinstance(base, str) or not base.strip():
            continue
        label = row.get("label") if isinstance(row.get("label"), str) else ""
        name = slugify_label(label) or env_id
        if name in taken or name in out:
            name = env_id
        out[name] = {
            "base_url": base.strip().rstrip("/") or base.strip(),
            "environment_id": env_id,
            "mode": "t3connect",
        }
        taken.add(name)
    return out


class DpopEnvClient(T3EnvClient):
    """Attach a method/URL-bound DPoP proof on each authenticated request."""

    def __init__(
        self,
        base_url: str,
        get_access_token: Callable[[], str],
        make_proof: Callable[[str, str, str], str],
        *,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(base_url, headers_fn=None, client=client)
        self._get_access_token = get_access_token
        self._make_proof = make_proof

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
        extra = dict(extra_headers or {})
        if auth:
            token = self._get_access_token()
            extra["Authorization"] = f"DPoP {token}"
            extra["dpop"] = self._make_proof(method, self._url(path), token)
        return super()._request(
            method,
            path,
            auth=False,
            params=params,
            json=json,
            data=data,
            extra_headers=extra,
        )


def make_dpop_env_client(ref, ctx, store, *, inner: T3EnvClient | None = None) -> DpopEnvClient:
    env_id = getattr(ref, "environment_id", None) or ref.name

    def get_token() -> str:
        return ensure_environment_access(ctx, env_id, store=store).access_token

    def make_proof(method: str, url: str, access_token: str) -> str:
        return load_or_create_key(store=store).proof(
            method, url, access_token=access_token
        )

    http = inner._client if inner is not None else None
    base_url = inner.base_url if inner is not None else ref.base_url
    return DpopEnvClient(base_url, get_token, make_proof, client=http)


def _t3connect_raw(ctx) -> Mapping[str, Any]:
    getter = getattr(ctx, "get_config", None) if ctx is not None else None
    if not callable(getter):
        return {}
    raw = getter("t3connect", {}) or {}
    if not isinstance(raw, Mapping):
        return {}
    return raw


def _t3connect_settings(ctx) -> tuple[str, str, str]:
    raw = _t3connect_raw(ctx)
    return (
        _setting(raw, "hosted_app_url", DEFAULT_HOSTED_APP_URL),
        _setting(raw, "clerk_oauth_client_id", DEFAULT_CLERK_OAUTH_CLIENT_ID),
        _setting(raw, "clerk_publishable_key", DEFAULT_CLERK_PUBLISHABLE_KEY),
    )


def _setting(raw: Mapping[str, Any], key: str, default: str) -> str:
    value = raw.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _exchange_clerk_token(
    form: dict[str, str],
    *,
    publishable_key: str,
    client: httpx.Client | None,
    fallback_refresh: str,
) -> ClerkTokens:
    endpoint = clerk_token_endpoint(publishable_key)
    http = _http(client)
    try:
        response = http.post(endpoint, data=form)
    except httpx.HTTPError as exc:
        raise ConnectAuthError("Could not reach Clerk token endpoint.") from exc
    if response.status_code < 200 or response.status_code >= 300:
        raise ConnectAuthError(
            f"Clerk token exchange failed (HTTP {response.status_code})."
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ConnectAuthError("Clerk token endpoint returned invalid JSON.") from exc
    if not isinstance(payload, Mapping):
        raise ConnectAuthError("Clerk token endpoint returned invalid JSON.")
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise ConnectAuthError("Clerk token endpoint returned no access_token.")
    refresh = payload.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        refresh = fallback_refresh
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, bool) or not isinstance(expires_in, int):
        expires_in = None
    return ClerkTokens(
        access_token=access,
        refresh_token=refresh,
        expires_in=expires_in,
    )


def _store_tokens(
    tokens: ClerkTokens,
    *,
    store: MutableMapping[str, str] | None,
) -> None:
    set_secret(ACCESS_SECRET, tokens.access_token, store=store)
    set_secret(REFRESH_SECRET, tokens.refresh_token, store=store)


def _save_pending(
    session: ConnectSession,
    *,
    store: MutableMapping[str, str] | None,
) -> None:
    global _pending
    _pending = session
    if store is None:
        try:
            from hermes_cli.config import save_env_value  # noqa: F401
        except ImportError:
            return
    set_secret(
        PENDING_SECRET,
        json.dumps(
            {
                "verifier": session.verifier,
                "challenge": session.challenge,
                "state": session.state,
                "hosted_app_url": session.hosted_app_url,
                "client_id": session.client_id,
                "publishable_key": session.publishable_key,
            }
        ),
        store=store,
    )


def _load_pending(
    *, store: Mapping[str, str] | None
) -> ConnectSession | None:
    if _pending is not None:
        return _pending
    raw = get_secret(PENDING_SECRET, store=store)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, Mapping):
        return None
    try:
        verifier = data["verifier"]
        challenge = data["challenge"]
        state = data["state"]
        hosted = data["hosted_app_url"]
        client_id = data["client_id"]
        publishable_key = data["publishable_key"]
    except KeyError:
        return None
    if not all(
        isinstance(v, str) and v
        for v in (verifier, challenge, state, hosted, client_id, publishable_key)
    ):
        return None
    return ConnectSession(
        verifier=verifier,
        challenge=challenge,
        state=state,
        authorize_url=build_connect_authorize_url(hosted, state, challenge),
        hosted_app_url=hosted,
        client_id=client_id,
        publishable_key=publishable_key,
    )


def _clear_pending(*, store: MutableMapping[str, str] | None) -> None:
    global _pending
    _pending = None
    _remove_secret(PENDING_SECRET, store=store)


def _remove_secret(
    name: str, *, store: MutableMapping[str, str] | None = None
) -> None:
    if store is not None:
        store.pop(name, None)
        return
    try:
        from hermes_cli.config import remove_env_value
    except ImportError:
        os.environ.pop(name, None)
        return
    remove_env_value(name)


def _http(client: httpx.Client | None) -> httpx.Client:
    return client if client is not None else t3_env.get_client()


def _now(now: float | None) -> float:
    return time.time() if now is None else float(now)


def _clerk_access(store: Mapping[str, str] | None) -> str:
    token = get_secret(ACCESS_SECRET, store=store)
    if not isinstance(token, str) or not token.strip():
        raise RelayNotAuthenticated()
    return token.strip()


def _relay_json(response: httpx.Response) -> Any:
    if response.status_code == 401:
        raise RelayNotAuthenticated()
    if response.status_code < 200 or response.status_code >= 300:
        raise T3ApiError(response.status_code, response.text)
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError as exc:
        raise T3ApiError(response.status_code, response.text) from exc


def _dpop_auth_headers(
    ctx,
    url: str,
    *,
    now: float,
    store: Mapping[str, str] | MutableMapping[str, str] | None,
    key=None,
    client: httpx.Client | None = None,
) -> dict[str, str]:
    token = dpop_token(ctx, now=now, store=store, client=client)
    signer = key if key is not None else load_or_create_key(store=store)
    return {
        "Authorization": f"DPoP {token}",
        "dpop": signer.proof("POST", url, access_token=token, now=now),
    }


def _expires_in(payload: Mapping[str, Any]) -> int:
    raw = payload.get("expires_in")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return 0
    return raw


def _as_epoch(value: Any, *, now: float) -> float:
    if isinstance(value, bool):
        return now
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return now
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return now
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _resolve_env_id(ctx, env_id: str) -> tuple[str, str | None]:
    try:
        ref = resolve_environment(ctx, env_id)
    except EnvironmentNotFound:
        return str(env_id), None
    except Exception:
        return str(env_id), None
    resolved = ref.environment_id or ref.name
    return resolved, ref.base_url


def _exchange_env_access(
    base_url: str,
    credential: str,
    key,
    *,
    environment_id: str,
    credential_expires_at: float,
    now: float,
    client: httpx.Client | None,
) -> _CachedEnvAccess:
    def signer(method: str, url: str) -> str:
        return key.proof(method, url, now=now)

    try:
        payload = T3EnvClient(base_url, client=_http(client)).exchange_dpop(
            credential, signer
        )
    except NotAuthenticated:
        raise RelayNotAuthenticated() from None
    if not isinstance(payload, Mapping):
        raise T3ApiError(200, "environment token exchange returned invalid JSON")
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise T3ApiError(200, "environment token exchange returned no access_token")
    return _CachedEnvAccess(
        environment_id=environment_id,
        base_url=base_url,
        access_token=token,
        access_expires_at=now + _expires_in(payload),
        credential=credential,
        credential_expires_at=credential_expires_at,
    )


def _store_env_access(entry: _CachedEnvAccess) -> EnvironmentAccess:
    _env_access[entry.environment_id] = entry
    return EnvironmentAccess(
        base_url=entry.base_url,
        access_token=entry.access_token,
        environment_id=entry.environment_id,
    )


def _mode_a_names(ctx) -> set[str]:
    getter = getattr(ctx, "get_config", None)
    if not callable(getter):
        return set()
    raw = getter("environments", {}) or {}
    if not isinstance(raw, Mapping):
        return set()
    names: set[str] = set()
    for key in raw:
        name = str(key).strip()
        if name:
            names.add(name)
    return names


def _write_discovered(ctx, mapping: dict) -> None:
    state = getattr(ctx, "state", None)
    if state is None:
        return
    setter = getattr(state, "set", None)
    if callable(setter):
        setter(DISCOVERED_STATE_KEY, mapping)
        return
    try:
        state[DISCOVERED_STATE_KEY] = mapping
    except Exception:
        return
