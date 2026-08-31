"""RFC 9449 DPoP proofs (ES256) for T3 Connect.

cryptography is imported only when a key is generated, loaded, or used to
sign — never at module import.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

from .config import get_secret, set_secret

DPOP_TYP = "dpop+jwt"
DPOP_ALG = "ES256"
DPOP_KEY_SECRET = "T3CODE_DPOP_KEY"
_THUMBPRINT_FIELDS = ("crv", "kty", "x", "y")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_json(obj: dict) -> str:
    return _b64url(
        json.dumps(obj, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    )


def _coord(value: int) -> str:
    return _b64url(value.to_bytes(32, "big"))


def normalize_dpop_htu(url: str) -> str:
    """htu without query or fragment (RFC 9449 / t3code normalizeDpopHtu)."""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("DPoP URL is invalid.")
    path = parsed.path if parsed.path else "/"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def access_token_hash(access_token: str) -> str:
    return _b64url(hashlib.sha256(access_token.encode("utf-8")).digest())


def jwk_thumbprint(jwk: dict) -> str:
    """RFC 7638 thumbprint: SHA-256 of canonical {crv,kty,x,y}."""
    canonical = json.dumps(
        {field: jwk[field] for field in _THUMBPRINT_FIELDS},
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return _b64url(hashlib.sha256(canonical.encode("utf-8")).digest())


def _public_jwk(private_key: Any) -> dict[str, str]:
    numbers = private_key.public_key().public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": _coord(numbers.x),
        "y": _coord(numbers.y),
    }


def _pem_bytes(private_key: Any) -> bytes:
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    return private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())


def _load_private(pem: bytes) -> Any:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    return load_pem_private_key(pem, password=None)


def _generate_private() -> Any:
    from cryptography.hazmat.primitives.asymmetric import ec

    return ec.generate_private_key(ec.SECP256R1())


def _sign_es256(private_key: Any, signing_input: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.ec import ECDSA
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    der = private_key.sign(signing_input, ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


@dataclass
class DpopKey:
    """ES256 P-256 key used to sign DPoP proofs."""

    _private_key: Any

    def pem_bytes(self) -> bytes:
        return _pem_bytes(self._private_key)

    def public_jwk(self) -> dict[str, str]:
        return _public_jwk(self._private_key)

    def thumbprint(self) -> str:
        return jwk_thumbprint(self.public_jwk())

    def proof(
        self,
        method: str,
        url: str,
        access_token: str | None = None,
        *,
        now: float | None = None,
        jti: str | None = None,
    ) -> str:
        header = {
            "typ": DPOP_TYP,
            "alg": DPOP_ALG,
            "jwk": self.public_jwk(),
        }
        epoch = int(time.time() if now is None else now)
        payload: dict[str, Any] = {
            "htm": method.upper(),
            "htu": normalize_dpop_htu(url),
            "iat": epoch,
            "jti": jti if jti else str(uuid.uuid4()),
        }
        if access_token:
            payload["ath"] = access_token_hash(access_token)
        signing_input = f"{_b64url_json(header)}.{_b64url_json(payload)}"
        sig = _sign_es256(self._private_key, signing_input.encode("ascii"))
        return f"{signing_input}.{_b64url(sig)}"


def generate_key() -> DpopKey:
    return DpopKey(_generate_private())


def load_pem(pem: bytes) -> DpopKey:
    return DpopKey(_load_private(pem))


def encode_secret(key: DpopKey) -> str:
    """Base64 of the PKCS8 PEM (Decision 7: T3CODE_DPOP_KEY)."""
    return base64.b64encode(key.pem_bytes()).decode("ascii")


def decode_secret(value: str) -> DpopKey:
    try:
        pem = base64.b64decode(value.encode("ascii"), validate=True)
    except Exception as exc:
        raise ValueError("Stored T3CODE_DPOP_KEY is invalid.") from exc
    try:
        return load_pem(pem)
    except Exception as exc:
        raise ValueError("Stored T3CODE_DPOP_KEY is invalid.") from exc


def load_or_create_key(*, store=None) -> DpopKey:
    raw = get_secret(DPOP_KEY_SECRET, store=store)
    if isinstance(raw, str) and raw.strip():
        return decode_secret(raw.strip())
    key = generate_key()
    set_secret(DPOP_KEY_SECRET, encode_secret(key), store=store)
    return key
