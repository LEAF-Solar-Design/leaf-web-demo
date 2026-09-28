"""Produce public-only, disposable binding-grant interoperability fixtures."""

import argparse
import importlib.util
import json
from pathlib import Path
import sys

from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("leaf_binding_grant", ROOT / "platform/binding_grant.py")
grant = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = grant
SPEC.loader.exec_module(grant)
FIXTURE = ROOT / "contract/fixtures/leaf-binding-grant.v1.json"


def sample_claims():
    return grant.GrantClaims(
        1, "https://binding.example.test", grant.AUDIENCE,
        "11111111-1111-1111-1111-111111111111", "Workspace",
        "22222222-2222-2222-2222-222222222222", "Project",
        "33333333-3333-3333-3333-333333333333", "Drawing",
        "44444444-4444-4444-4444-444444444444", "Version 1",
        "55555555-5555-5555-5555-555555555555",
        "66666666-6666-6666-6666-666666666666", "sha256:" + "a" * 64,
        1700000000, 1700000120, grant.b64u(bytes(range(32))))


def generate_fixture():
    # No private-key serialization or filesystem persistence, even temporarily.
    signer = grant.LocalTestSigner(rsa.generate_private_key(public_exponent=65537, key_size=3072))
    numbers = signer.private_key.public_key().public_numbers()
    encode_int = lambda n: grant.b64u(n.to_bytes((n.bit_length() + 7) // 8, "big"))
    claims = sample_claims()
    base = grant.asdict(claims)
    header = grant.canonical_header(signer.kid)
    cases = []

    def add(name, expect="accept", changes=None, payload=None, protected=None):
        body = grant._json_bytes(dict(base, **(changes or {}))) if payload is None else payload
        message = grant.b64u(header if protected is None else protected) + "." + grant.b64u(body)
        token = message + "." + grant.b64u(signer.sign(message.encode("ascii")))
        cases.append({"name": name, "token": token, "expect": expect})

    add("ascii")
    add("quotes-backslashes", changes={"workspaceName": 'A "quoted" \\ workspace'})
    add("unicode", changes={"projectName": "Caf\u00e9 \u6771\u4eac"})
    add("lifetime-300", changes={"exp": claims.iat + 300})
    add("lifetime-301", "lifetime", {"exp": claims.iat + 301})
    add("future-30", changes={"iat": claims.iat + 30})
    add("future-31", "issued_in_future", {"iat": claims.iat + 31})
    add("expiry-equality", "expired", {"iat": claims.iat - 120, "exp": claims.iat})
    parts = cases[0]["token"].split(".")
    signature = bytearray(grant.b64u_decode(parts[2]))
    signature[0] ^= 1
    cases.append({"name": "flipped-signature", "token": ".".join(parts[:2] + [grant.b64u(signature)]), "expect": "signature"})
    add("whitespace", "noncanonical", payload=json.dumps(base, ensure_ascii=False).encode("utf-8"))
    add("key-order", "noncanonical", payload=grant._json_bytes(dict(reversed(list(base.items())))))
    add("extra-key", "payload_fields", changes={"extra": 1})
    cases.append({"name": "padding", "token": parts[0] + "=." + ".".join(parts[1:]), "expect": "base64url"})
    add("unknown-alg", "algorithm", protected=grant._json_bytes({"alg": "HS256", "kid": signer.kid, "typ": grant.TYPE}))
    cases.append({"name": "oversize", "token": "A" * 8193, "expect": "token_size"})
    return {"schema": "leaf.binding-grant.fixture.v1", "testOnly": True,
            "publicKey": {"kid": signer.kid, "n": encode_int(numbers.n), "e": encode_int(numbers.e)},
            "now": claims.iat, "expected_iss": claims.iss, "cases": cases}


def check_fixture(fixture):
    assert fixture["schema"] == "leaf.binding-grant.fixture.v1"
    assert fixture["testOnly"] is True
    key = fixture["publicKey"]
    assert set(key) == {"kid", "n", "e"}
    public_key = rsa.RSAPublicNumbers(
        int.from_bytes(grant.b64u_decode(key["e"]), "big"),
        int.from_bytes(grant.b64u_decode(key["n"]), "big")).public_key()
    assert fixture["cases"], "fixture has no cases"
    for case in fixture["cases"]:
        try:
            grant.verify_grant(case["token"], public_key, now=fixture["now"],
                               expected_iss=fixture["expected_iss"])
            actual = "accept"
        except grant.GrantError as error:
            actual = error.code
        assert actual == case["expect"], (case["name"], actual, case["expect"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    fresh = generate_fixture()
    check_fixture(fresh)
    if args.check:
        check_fixture(json.loads(FIXTURE.read_text(encoding="utf-8")))
    else:
        FIXTURE.write_text(json.dumps(fresh, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
