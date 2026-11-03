"""Shared signing vector plus real SQLite replay and revocation authority."""
import asyncio
import json
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
import pytest

import device_auth
import device_store as store
from routers.devices import DeviceRoute

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "device-agent"))
from leaf_agent import sign

SEED = b"\x01" * 32
PLATFORM = {"os": "darwin", "os_version": "15", "arch": "arm64"}


@pytest.fixture
def signed(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "test")
    monkeypatch.setenv("LEAF_PLATFORM_POSTGRES_REQUIRED", "0")
    monkeypatch.setenv("LEAF_DEVICE_STORE", "sqlite")
    monkeypatch.setenv("LEAF_DEVICE_DB", str(tmp_path / "device.db"))
    _, token = store.create_enrollment("tenant", "project", "Mini", "user:test")
    device = store.consume_enrollment(token, sign.public_key(SEED), "1", PLATFORM, "mini")
    app = FastAPI()
    app.router.route_class = DeviceRoute

    @app.api_route("/signed", methods=["GET", "POST"])
    async def endpoint(request: Request, identity=Depends(device_auth.require_device)):
        first = await device_auth.raw_body(request)
        assert first is await device_auth.raw_body(request)
        return {"device_id": identity["device_id"], "body": first.decode()}

    with TestClient(app) as client:
        yield client, device


def request(signed, *, target="/signed", body=b"", headers=None, method="POST"):
    client, device = signed
    headers = headers or sign.headers(SEED, device["device_id"], method, target, body)
    return client.request(method, target, content=body, headers=headers)


def test_contract_worked_example_verifies_on_server(signed, monkeypatch):
    document = (Path(__file__).resolve().parents[2] / "contract/DEVICE-BRIDGE.md").read_text(encoding="utf-8")
    vector = json.loads(re.search(r"```signing-example\n(.*?)\n```", document, re.S).group(1))
    seed = bytes.fromhex(vector["seed_hex_byte"]) * vector["seed_length"]
    body = vector["body"].encode()
    assert device_auth.canonical(vector["method"], vector["path"], vector["timestamp"], vector["nonce"], body) == vector["signed_string"].encode()
    assert sign.canonical(vector["method"], vector["path"], vector["timestamp"], vector["nonce"], body) == vector["signed_string"].encode()
    headers = sign.headers(seed, vector["device_id"], vector["method"], vector["path"], body,
                           timestamp=vector["timestamp"], nonce=vector["nonce"])
    assert headers["X-Leaf-Signature"] == vector["signature"]
    # Feed the exact vector through the server verifier, retaining SQLite nonce authority.
    device = {**signed[1], "device_id": vector["device_id"], "public_key": vector["public_key"]}
    monkeypatch.setattr(store, "get_device", lambda device_id: device if device_id == vector["device_id"] else None)
    original = store.remember_nonce
    monkeypatch.setattr(store, "remember_nonce", lambda device_id, nonce, now: original(signed[1]["device_id"], nonce, now))
    monkeypatch.setattr(device_auth.time, "time", lambda: int(vector["timestamp"]))
    scope = {"type": "http", "method": vector["method"], "path": vector["path"].split("?")[0],
             "raw_path": vector["path"].split("?")[0].encode(),
             "query_string": vector["path"].partition("?")[2].encode(),
             "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()]}
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    assert asyncio.run(device_auth.require_device(Request(scope, receive)))["device_id"] == vector["device_id"]


def test_agent_signs_raw_body_target_and_server_caches_body(signed):
    response = request(signed, target="/signed?b=%2f&a=1", body=b'{ "x": 1 }')
    assert response.status_code == 200
    assert response.json()["body"] == '{ "x": 1 }'


@pytest.mark.parametrize("skew", [-61, 61])
def test_stale_signature_rejected(signed, monkeypatch, skew):
    now = int(time.time())
    headers = sign.headers(SEED, signed[1]["device_id"], "POST", "/signed", timestamp=str(now + skew))
    monkeypatch.setattr(device_auth.time, "time", lambda: now)
    response = request(signed, headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "signature_stale"


@pytest.mark.parametrize("skew", [-60, 60])
def test_exact_skew_boundary_is_allowed(signed, monkeypatch, skew):
    now = int(time.time())
    headers = sign.headers(SEED, signed[1]["device_id"], "POST", "/signed", timestamp=str(now + skew))
    monkeypatch.setattr(device_auth.time, "time", lambda: now)
    assert request(signed, headers=headers).status_code == 200


def test_nonce_is_shared_and_invalid_signature_does_not_consume_it(signed):
    headers = sign.headers(SEED, signed[1]["device_id"], "POST", "/signed")
    bad = {**headers, "X-Leaf-Signature": sign.b64url(b"\x00" * 64)}
    assert request(signed, headers=bad).json()["error"]["code"] == "signature_invalid"
    assert request(signed, headers=headers).status_code == 200
    replay = request(signed, headers=headers)
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "signature_replay"


@pytest.mark.parametrize("change", ["body", "target", "device", "timestamp", "nonce", "signature", "missing", "padded"])
def test_tampered_and_malformed_signatures_rejected(signed, change):
    headers = sign.headers(SEED, signed[1]["device_id"], "POST", "/signed", b"{}")
    target, body = "/signed", b"{}"
    if change == "body":
        body = b"{ }"
    elif change == "target":
        target = "/signed?changed=1"
    elif change == "device":
        headers["X-Leaf-Device"] = str(uuid4())
    elif change == "timestamp":
        headers["X-Leaf-Timestamp"] = "1.0"
    elif change == "nonce":
        headers["X-Leaf-Nonce"] = "bad"
    elif change == "signature":
        headers["X-Leaf-Signature"] = "bad"
    elif change == "missing":
        del headers["X-Leaf-Signature"]
    else:
        headers["X-Leaf-Nonce"] += "=="
    response = request(signed, headers=headers, target=target, body=body)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "signature_invalid"


def test_duplicate_signing_headers_rejected(signed):
    headers = list(sign.headers(SEED, signed[1]["device_id"], "POST", "/signed").items())
    headers.append(("X-Leaf-Nonce", sign.b64url(b"\x00" * 16)))
    assert request(signed, headers=headers).json()["error"]["code"] == "signature_invalid"


def test_revoked_device_rejected_before_missing_signature(signed):
    store.revoke_device("tenant", signed[1]["device_id"], "user:test")
    response = request(signed, headers={"X-Leaf-Device": signed[1]["device_id"]})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "device_revoked"


def test_body_limit_applies_before_signature_parsing(signed):
    response = request(signed, body=b"x" * (device_auth.MAX_BODY + 1))
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_exact_body_limit_is_allowed(signed):
    assert request(signed, body=b"x" * device_auth.MAX_BODY).status_code == 200
