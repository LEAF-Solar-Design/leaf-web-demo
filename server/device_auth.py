"""Raw-body Ed25519 authentication for leaf.device-bridge.v1."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import re
import time
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

import device_store

MAX_BODY = 1_048_576


class DeviceError(HTTPException):
    def __init__(self, status: int, code: str):
        self.code = code
        super().__init__(status_code=status, detail="Device request rejected")


async def raw_body(request: Request) -> bytes:
    """Read once, bounding even chunked requests before JSON is parsed."""
    if not hasattr(request.state, "device_body"):
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_BODY:
                raise DeviceError(413, "payload_too_large")
            body.extend(chunk)
        request.state.device_body = bytes(body)
    return request.state.device_body


def canonical(method: str, target: str, timestamp: str, nonce: str, body: bytes) -> bytes:
    return "\n".join(("LEAF-DEVICE-V1", method.upper(), target, timestamp, nonce,
                      hashlib.sha256(body).hexdigest())).encode("utf-8")


def _decode(value: str, length: int) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid encoding")
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if len(raw) != length or base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != value:
        raise ValueError("invalid encoding")
    return raw


async def require_device(request: Request) -> dict:
    body = await raw_body(request)
    device_id = request.headers.get("X-Leaf-Device", "")
    try:
        UUID(device_id)
    except ValueError:
        raise DeviceError(401, "signature_invalid") from None
    device = await run_in_threadpool(device_store.get_device, device_id)
    if device is None:
        raise DeviceError(401, "signature_invalid")
    if device["revoked_at"] is not None:
        raise DeviceError(401, "device_revoked")
    timestamp = request.headers.get("X-Leaf-Timestamp", "")
    nonce = request.headers.get("X-Leaf-Nonce", "")
    try:
        for name in ("x-leaf-device", "x-leaf-timestamp", "x-leaf-nonce", "x-leaf-signature"):
            if len(request.headers.getlist(name)) != 1:
                raise ValueError("duplicate signing header")
        if not re.fullmatch(r"[0-9]{1,20}", timestamp):
            raise ValueError("invalid timestamp")
        _decode(nonce, 16)
        signature = _decode(request.headers.get("X-Leaf-Signature", ""), 64)
        public_key = Ed25519PublicKey.from_public_bytes(_decode(device["public_key"], 32))
    except ValueError:
        raise DeviceError(401, "signature_invalid") from None
    now = time.time()
    if abs(now - int(timestamp)) > 60:
        raise DeviceError(401, "signature_stale")
    target = request.scope.get("raw_path", request.scope["path"].encode("utf-8"))
    query = request.scope.get("query_string", b"")
    if query:
        target += b"?" + query
    try:
        public_key.verify(signature, canonical(request.method, target.decode("ascii"), timestamp, nonce, body))
    except (ValueError, UnicodeError, InvalidSignature):
        raise DeviceError(401, "signature_invalid") from None
    try:
        remembered = await run_in_threadpool(device_store.remember_nonce, device_id, nonce,
                                             datetime.fromtimestamp(now, timezone.utc))
    except device_store.DeviceRevoked:
        raise DeviceError(401, "device_revoked") from None
    if not remembered:
        raise DeviceError(401, "signature_replay")
    return device
