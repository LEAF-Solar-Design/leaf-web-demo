"""Compact Ed25519 JWS leases backed by the reviewed cryptography provider."""
from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if _b64(decoded) != value:
        raise ValueError("non-canonical base64url")
    return decoded


def public_key(seed: bytes) -> bytes:
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must be 32 bytes")
    return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )


def sign(message: bytes, seed: bytes, public: bytes | None = None) -> bytes:
    del public
    return Ed25519PrivateKey.from_private_bytes(seed).sign(message)


def verify(message: bytes, signature: bytes, public: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, message)
        return True
    except (InvalidSignature, ValueError):
        return False


class LeaseSigner:
    """Issues compact EdDSA JWS leases and exposes only public trust material."""

    def __init__(self, seed: bytes | None = None, kid: str = "control-plane-v1"):
        self._seed = seed or os.urandom(32)
        self.kid = kid
        self.public = public_key(self._seed)

    def sign(self, payload: dict[str, Any]) -> str:
        header = {"alg": "EdDSA", "kid": self.kid, "typ": "JWT"}
        protected = _b64(json.dumps(header, separators=(",", ":"), sort_keys=True).encode())
        body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        signature = _b64(sign(f"{protected}.{body}".encode("ascii"), self._seed))
        return f"{protected}.{body}.{signature}"

    def jwks(self) -> dict[str, Any]:
        return {"keys": [{"kty": "OKP", "crv": "Ed25519", "use": "sig", "alg": "EdDSA",
                          "kid": self.kid, "x": _b64(self.public)}]}


def verify_jws(token: str, jwks: dict[str, Any], now: int | None = None) -> dict[str, Any]:
    """Verify signature, issuer, audience, and lease time fields."""
    try:
        protected, body, encoded_signature = token.split(".")
        header = json.loads(_unb64(protected))
        payload = json.loads(_unb64(body))
        signature = _unb64(encoded_signature)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("malformed compact JWS") from exc
    key = next((item for item in jwks.get("keys", []) if item.get("kid") == header.get("kid")), None)
    if (header.get("alg") != "EdDSA" or key is None
            or not verify(f"{protected}.{body}".encode("ascii"), signature, _unb64(key["x"]))):
        raise ValueError("invalid lease signature")
    current = int(time.time()) if now is None else now
    if (payload.get("iss") != "instant-control-plane" or payload.get("aud") != "instant-executor"
            or current < payload.get("nbf", 0) or current >= payload.get("exp", 0)):
        raise ValueError("lease is not currently valid")
    return payload
