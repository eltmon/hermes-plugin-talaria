"""Mode A: pairing URL → bearer secret.

Parse a ``t3 pair`` URL (hash token, legacy query token, hosted ``?host=``),
exchange it at the environment, store the bearer via ``set_secret``. The
token is never written to plugin config.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlparse, urlunparse

from .config import secret_name_for, set_secret
from .t3_env import T3EnvClient

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z\d+-]*://")
_SUPPORTED_SCHEMES = frozenset({"http", "https", "ws", "wss"})
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


class PairingUrlError(ValueError):
    """Pairing URL could not be parsed into a backend + credential."""


@dataclass(frozen=True)
class PairingTarget:
    credential: str
    http_base_url: str


@dataclass(frozen=True)
class LoginResult:
    name: str
    base_url: str
    scope: str
    expires_in: int | None


def slugify_label(label: str) -> str:
    """Lowercase, non-alnum → ``-``, collapse dashes, strip edges."""
    return _NON_ALNUM.sub("-", label.lower()).strip("-")


def parse_pairing_url(pairing_url: str) -> PairingTarget:
    """Mirror t3code ``resolveRemotePairingTarget({ pairingUrl })``."""
    raw = pairing_url.strip() if isinstance(pairing_url, str) else ""
    if not raw:
        raise PairingUrlError("Pairing URL is invalid.")
    parsed = urlparse(raw)
    if parsed.scheme not in _SUPPORTED_SCHEMES or not parsed.netloc:
        raise PairingUrlError("Pairing URL is invalid.")

    token = _pairing_token(parsed)
    hosted_host = _first_param(parsed.query, "host")
    if hosted_host and token:
        return PairingTarget(
            credential=token,
            http_base_url=_normalize_remote_base_url(hosted_host),
        )
    if not token:
        raise PairingUrlError("Pairing URL is missing its token.")
    return PairingTarget(credential=token, http_base_url=_to_http_base_url(parsed))


def login(
    pairing_url: str,
    *,
    ctx,
    name: str | None = None,
    store: MutableMapping[str, str] | None = None,
    client: Any = None,
) -> LoginResult:
    """Exchange a pairing URL and persist bearer + ``environments`` entry."""
    target = parse_pairing_url(pairing_url)
    env = T3EnvClient(target.http_base_url, client=client)
    descriptor = env.descriptor()
    exchanged = env.exchange_pairing(target.credential)
    env_name = _resolve_name(name, descriptor)
    token = _access_token(exchanged)
    set_secret(secret_name_for(env_name), token, store=store)
    _upsert_environment(ctx, env_name, target.http_base_url)
    return LoginResult(
        name=env_name,
        base_url=target.http_base_url,
        scope=_scope(exchanged),
        expires_in=_expires_in(exchanged),
    )


def logout(
    env: str,
    *,
    ctx,
    store: MutableMapping[str, str] | None = None,
) -> None:
    """Delete the environment secret and drop its config entry."""
    env_name = env.strip() if isinstance(env, str) else ""
    if not env_name:
        raise ValueError("environment name is required")
    _remove_secret(secret_name_for(env_name), store=store)
    existing = ctx.get_config("environments", {}) or {}
    if isinstance(existing, Mapping) and env_name in existing:
        ctx.set_config(
            "environments",
            {key: value for key, value in existing.items() if key != env_name},
        )


def _pairing_token(parsed) -> str:
    hash_token = _first_param(parsed.fragment, "token")
    if hash_token:
        return hash_token
    return _first_param(parsed.query, "token")


def _first_param(source: str, key: str) -> str:
    for name, value in parse_qsl(source, keep_blank_values=True):
        if name == key:
            return (value or "").strip()
    return ""


def _normalize_remote_base_url(raw: str) -> str:
    trimmed = raw.strip()
    if not trimmed:
        raise PairingUrlError("Backend URL is invalid.")
    stripped = trimmed.lstrip("/")
    if not _SCHEME_RE.match(stripped):
        stripped = "https://" + stripped
    parsed = urlparse(stripped)
    if parsed.scheme not in _SUPPORTED_SCHEMES or not parsed.netloc:
        raise PairingUrlError("Backend URL is invalid.")
    return _to_http_base_url(parsed)


def _to_http_base_url(parsed) -> str:
    scheme = parsed.scheme
    if scheme == "ws":
        scheme = "http"
    elif scheme == "wss":
        scheme = "https"
    return urlunparse((scheme, parsed.netloc, "/", "", "", ""))


def _resolve_name(name: str | None, descriptor: Any) -> str:
    explicit = name.strip() if isinstance(name, str) else ""
    if explicit:
        return explicit
    label = ""
    env_id = ""
    if isinstance(descriptor, Mapping):
        raw = descriptor.get("label")
        if isinstance(raw, str):
            label = raw
        raw_id = descriptor.get("environmentId")
        if isinstance(raw_id, str):
            env_id = raw_id
    return slugify_label(label) or slugify_label(env_id) or "t3"


def _access_token(exchanged: Any) -> str:
    token = exchanged.get("access_token") if isinstance(exchanged, Mapping) else None
    if not isinstance(token, str) or not token:
        raise PairingUrlError("pairing exchange returned no access_token")
    return token


def _scope(exchanged: Any) -> str:
    if not isinstance(exchanged, Mapping):
        return ""
    scope = exchanged.get("scope")
    return scope if isinstance(scope, str) else ""


def _expires_in(exchanged: Any) -> int | None:
    if not isinstance(exchanged, Mapping):
        return None
    value = exchanged.get("expires_in")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _upsert_environment(ctx, name: str, base_url: str) -> None:
    existing = ctx.get_config("environments", {}) or {}
    if not isinstance(existing, Mapping):
        existing = {}
    ctx.set_config("environments", {**dict(existing), name: {"base_url": base_url}})


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
