"""Exact leaf.device-bridge.v1 signing bytes."""
import base64
import hashlib
import secrets
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def canonical(method: str, target: str, timestamp: str, nonce: str, body: bytes = b"") -> bytes:
    return "\n".join(("LEAF-DEVICE-V1", method.upper(), target, timestamp, nonce,
                      hashlib.sha256(body).hexdigest())).encode("utf-8")


def public_key(seed: bytes) -> str:
    key = Ed25519PrivateKey.from_private_bytes(seed).public_key()
    return b64url(key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def headers(seed: bytes, device_id: str, method: str, target: str, body: bytes = b"",
            *, timestamp: str | None = None, nonce: str | None = None) -> dict:
    timestamp = str(int(time.time())) if timestamp is None else timestamp
    nonce = b64url(secrets.token_bytes(16)) if nonce is None else nonce
    signature = Ed25519PrivateKey.from_private_bytes(seed).sign(canonical(method, target, timestamp, nonce, body))
    return {"X-Leaf-Device": device_id, "X-Leaf-Timestamp": timestamp,
            "X-Leaf-Nonce": nonce, "X-Leaf-Signature": b64url(signature)}
