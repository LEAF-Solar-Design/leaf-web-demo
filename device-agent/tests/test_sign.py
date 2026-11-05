import base64
import hashlib
import json
from pathlib import Path
import re

from leaf_agent import sign


def test_contract_worked_example_exact_bytes():
    document = (Path(__file__).resolve().parents[2] / "contract/DEVICE-BRIDGE.md").read_text(encoding="utf-8")
    vector = json.loads(re.search(r"```signing-example\n(.*?)\n```", document, re.S).group(1))
    seed = bytes.fromhex(vector["seed_hex_byte"]) * vector["seed_length"]
    body = vector["body"].encode("utf-8")
    canonical = sign.canonical(vector["method"], vector["path"], vector["timestamp"], vector["nonce"], body)
    assert canonical == vector["signed_string"].encode("utf-8")
    assert hashlib.sha256(body).hexdigest() == vector["body_sha256"]
    assert sign.public_key(seed) == vector["public_key"]
    headers = sign.headers(seed, vector["device_id"], vector["method"], vector["path"], body,
                           timestamp=vector["timestamp"], nonce=vector["nonce"])
    assert headers == {"X-Leaf-Device": vector["device_id"], "X-Leaf-Timestamp": vector["timestamp"],
                       "X-Leaf-Nonce": vector["nonce"], "X-Leaf-Signature": vector["signature"]}


def test_raw_target_body_and_nonce_are_not_normalized():
    first = sign.canonical("post", "/a?b=%2f&a=1", "1", "nonce", b'{ "x": 1 }')
    assert first.startswith(b"LEAF-DEVICE-V1\nPOST\n/a?b=%2f&a=1\n1\nnonce\n")
    assert first != sign.canonical("POST", "/a?a=1&b=/", "1", "nonce", b'{"x":1}')
    values = [sign.headers(b"\x01" * 32, "id", "GET", "/") for _ in range(30)]
    assert len({v["X-Leaf-Nonce"] for v in values}) == 30
    assert all(len(base64.urlsafe_b64decode(v["X-Leaf-Nonce"] + "==")) == 16 for v in values)
