"""Mode B: Clerk PKCE headless OAuth for T3 Connect.

Mirrors t3code ``connectAuth.ts`` (hash authorize URL, ``<code>.<state>`` blob)
and ``CliTokenManager.outOfBandOAuthLogin`` (token exchange + refresh).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

import httpx

from .config import get_secret, set_secret
from .t3_env import get_client

DEFAULT_HOSTED_APP_URL = "https://app.t3.codes"
DEFAULT_CLERK_OAUTH_CLIENT_ID = "hzxSgY2cH10sDU2r"
DEFAULT_CLERK_PUBLISHABLE_KEY = "pk_live_Y2xlcmsudDMuY29kZXMk"

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


class ConnectAuthError(ValueError):
    """User-facing T3 Connect OAuth failure."""


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


_pending: ConnectSession | None = None


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


def _t3connect_settings(ctx) -> tuple[str, str, str]:
    raw: Any = {}
    getter = getattr(ctx, "get_config", None) if ctx is not None else None
    if callable(getter):
        raw = getter("t3connect", {}) or {}
    if not isinstance(raw, Mapping):
        raw = {}
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
    http = client if client is not None else get_client()
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
