"""WI-9: RFC 9449 DPoP proofs — in-memory keys, no live network."""

from __future__ import annotations

import base64
import hashlib
import json

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ec import (
    ECDSA,
    EllipticCurvePublicNumbers,
    SECP256R1,
)
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from talaria.dpop import (
    DPOP_ALG,
    DPOP_KEY_SECRET,
    DPOP_TYP,
    access_token_hash,
    generate_key,
    jwk_thumbprint,
    load_or_create_key,
    normalize_dpop_htu,
)


def _b64url_decode(data: str) -> bytes:
    pad = "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(data + pad)


def jwt_parts(token: str):
    header_b64, payload_b64, sig_b64 = token.split(".")
    header = json.loads(_b64url_decode(header_b64))
    payload = json.loads(_b64url_decode(payload_b64))
    return header, payload, header_b64, payload_b64, sig_b64


def verify_proof(proof: str, public_jwk: dict | None = None):
    header, payload, header_b64, payload_b64, sig_b64 = jwt_parts(proof)
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


def test_import_opens_no_socket(socket_guard):
    import talaria.dpop as mod

    assert callable(mod.generate_key)
    assert callable(mod.load_or_create_key)


def test_proof_verifies_and_carries_required_header_claims():
    key = generate_key()
    proof = key.proof(
        "post",
        "https://example.com/oauth/token",
        now=100,
        jti="proof-1",
    )
    header, payload = verify_proof(proof, key.public_jwk())
    assert header["typ"] == DPOP_TYP
    assert header["alg"] == DPOP_ALG
    assert header["jwk"] == key.public_jwk()
    assert "d" not in header["jwk"]
    assert payload["htm"] == "POST"
    assert payload["htu"] == "https://example.com/oauth/token"
    assert payload["iat"] == 100
    assert payload["jti"] == "proof-1"
    assert "ath" not in payload


def test_proof_includes_ath_for_access_token():
    key = generate_key()
    token = "clerk-access-token"
    proof = key.proof(
        "POST",
        "https://example.com/v1/environments/env/connect",
        access_token=token,
        now=100,
        jti="proof-ath",
    )
    header, payload = verify_proof(proof)
    assert payload["ath"] == access_token_hash(token)
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    assert payload["ath"] == expected
    assert "d" not in header["jwk"]


def test_htu_strips_query_and_fragment():
    key = generate_key()
    url = "https://example.com/v1/environments/env/connect?foo=bar#frag"
    assert normalize_dpop_htu(url) == (
        "https://example.com/v1/environments/env/connect"
    )
    proof = key.proof("POST", url, now=100, jti="htu-1")
    _header, payload = verify_proof(proof)
    assert payload["htu"] == "https://example.com/v1/environments/env/connect"


def test_thumbprint_is_sha256_of_canonical_jwk():
    key = generate_key()
    jwk = key.public_jwk()
    canonical = json.dumps(
        {"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"], "y": jwk["y"]},
        separators=(",", ":"),
    )
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(canonical.encode("utf-8")).digest()
    ).decode("ascii").rstrip("=")
    assert key.thumbprint() == expected
    assert jwk_thumbprint(jwk) == expected
    assert list(jwk) == ["kty", "crv", "x", "y"]


def test_load_or_create_persists_base64_pem_secret_not_raw_pem():
    store: dict[str, str] = {}
    first = load_or_create_key(store=store)
    assert DPOP_KEY_SECRET in store
    secret = store[DPOP_KEY_SECRET]
    assert secret
    assert "BEGIN" not in secret
    assert "PRIVATE" not in secret
    second = load_or_create_key(store=store)
    assert second.thumbprint() == first.thumbprint()
    proof = second.proof("GET", "https://example.com/x", now=50, jti="reload")
    verify_proof(proof, first.public_jwk())
