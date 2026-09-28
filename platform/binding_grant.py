"""Canonical server grants for first binding a native drawing to a project."""

import base64
import binascii
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, fields
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa


AUDIENCE = "leaf-automation:autocad:drawing.bind:v1"
TYPE = "leaf-binding-grant+jws"
MAX_TOKEN_BYTES = 8192


class GrantError(ValueError):
    """A bounded, stable refusal reason; never includes grant contents."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class GrantClaims:
    schemaVersion: int
    iss: str
    aud: str
    platformTenantId: str
    workspaceName: str
    projectId: str
    projectName: str
    drawingId: str
    drawingName: str
    drawingVersionId: str
    versionName: str
    actorBindingId: str
    pluginSessionId: str
    documentFingerprint: str
    iat: int
    exp: int
    nonce: str


def b64u(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def b64u_decode(text: str) -> bytes:
    if not isinstance(text, str) or re.fullmatch(r"[A-Za-z0-9_-]*", text) is None:
        raise GrantError("base64url")
    try:
        value = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, binascii.Error):
        raise GrantError("base64url") from None
    if b64u(value) != text:
        raise GrantError("base64url")
    return value


def _safe_string(value):
    return isinstance(value, str) and all(
        unicodedata.category(c) not in ("Cc", "Cs")
        and ord(c) not in (0x061C, 0x200E, 0x200F, 0x202A, 0x202B,
                           0x202C, 0x202D, 0x202E, 0x2066, 0x2067,
                           0x2068, 0x2069)
        for c in value
    )


def validate_claims(claims: GrantClaims):
    if not isinstance(claims, GrantClaims):
        raise GrantError("claims")
    if type(claims.schemaVersion) is not int or claims.schemaVersion != 1:
        raise GrantError("schema_version")
    if not _safe_string(claims.iss) or not claims.iss:
        raise GrantError("issuer")
    if claims.aud != AUDIENCE:
        raise GrantError("audience")
    for name in ("platformTenantId", "projectId", "drawingId", "drawingVersionId",
                 "actorBindingId", "pluginSessionId"):
        value = getattr(claims, name)
        if (not isinstance(value, str)
                or re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value) is None
                or value == "00000000-0000-0000-0000-000000000000"):
            raise GrantError("uuid")
    for name in ("workspaceName", "projectName", "drawingName", "versionName"):
        value = getattr(claims, name)
        if not _safe_string(value) or not 1 <= len(value) <= 200:
            raise GrantError("name")
    if (not isinstance(claims.documentFingerprint, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", claims.documentFingerprint) is None):
        raise GrantError("fingerprint")
    if type(claims.iat) is not int or type(claims.exp) is not int:
        raise GrantError("time_type")
    if not 0 < claims.exp - claims.iat <= 300:
        raise GrantError("lifetime")
    try:
        nonce = b64u_decode(claims.nonce)
    except GrantError:
        raise GrantError("nonce") from None
    if len(nonce) != 32:
        raise GrantError("nonce")


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def canonical_header(kid: str) -> bytes:
    if not isinstance(kid, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", kid) is None:
        raise GrantError("kid")
    return _json_bytes({"alg": "RS256", "kid": kid, "typ": TYPE})


def canonical_payload(claims: GrantClaims) -> bytes:
    validate_claims(claims)
    return _json_bytes(asdict(claims))


class Signer(Protocol):
    @property
    def kid(self) -> str: ...

    def sign(self, message: bytes) -> bytes: ...


def _signature(value):
    if not isinstance(value, bytes) or len(value) != 384:
        raise GrantError("signature_length")
    return value


class KmsSigner:
    def __init__(self, key_id, kid, client):
        canonical_header(kid)
        self.key_id, self.kid, self.client = key_id, kid, client

    def sign(self, message: bytes) -> bytes:
        response = self.client.sign(KeyId=self.key_id, Message=message,
                                    MessageType="RAW",
                                    SigningAlgorithm="RSASSA_PKCS1_V1_5_SHA_256")
        return _signature(response.get("Signature"))


class LocalTestSigner:
    """In-memory fixture signer. Must never be used in a running service."""

    def __init__(self, private_key, kid="fixture-key"):
        canonical_header(kid)
        if not isinstance(private_key, rsa.RSAPrivateKey) or private_key.key_size != 3072:
            raise GrantError("key_size")
        self.private_key, self.kid = private_key, kid

    def sign(self, message: bytes) -> bytes:
        return _signature(self.private_key.sign(message, padding.PKCS1v15(), hashes.SHA256()))


def encode_grant(claims: GrantClaims, signer: Signer) -> str:
    message = b64u(canonical_header(signer.kid)) + "." + b64u(canonical_payload(claims))
    token = message + "." + b64u(_signature(signer.sign(message.encode("ascii"))))
    if len(token) > MAX_TOKEN_BYTES:
        raise GrantError("token_size")
    return token


def _parse(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise GrantError("json")
            result[key] = value
        return result

    def reject(_):
        raise GrantError("json")

    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                            parse_float=reject, parse_constant=reject)
    except (ValueError, UnicodeError, RecursionError):
        raise GrantError("json") from None
    if not isinstance(result, dict):
        raise GrantError("json")
    return result


def verify_grant(token, public_key, *, now, expected_iss) -> GrantClaims:
    if not isinstance(token, str):
        raise GrantError("token_format")
    if len(token) > MAX_TOKEN_BYTES:
        raise GrantError("token_size")
    try:
        token.encode("ascii")
    except UnicodeError:
        raise GrantError("token_format") from None
    parts = token.split(".")
    if len(parts) != 3:
        raise GrantError("token_format")
    header_raw, payload_raw, signature = map(b64u_decode, parts)
    _signature(signature)
    if not isinstance(public_key, rsa.RSAPublicKey) or public_key.key_size != 3072:
        raise GrantError("key_size")
    try:
        public_key.verify(signature, (parts[0] + "." + parts[1]).encode("ascii"),
                          padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        raise GrantError("signature") from None
    header, payload = _parse(header_raw), _parse(payload_raw)
    if set(header) != {"alg", "kid", "typ"}:
        raise GrantError("header_fields")
    if header["alg"] != "RS256":
        raise GrantError("algorithm")
    if header["typ"] != TYPE:
        raise GrantError("type")
    if set(payload) != {field.name for field in fields(GrantClaims)}:
        raise GrantError("payload_fields")
    claims = GrantClaims(**payload)
    # Compare before semantic checks, including for deliberately invalid claims.
    try:
        canonical = _json_bytes(asdict(claims))
    except (ValueError, UnicodeError):
        raise GrantError("json") from None
    if canonical_header(header["kid"]) != header_raw or canonical != payload_raw:
        raise GrantError("noncanonical")
    validate_claims(claims)
    if claims.iss != expected_iss:
        raise GrantError("issuer")
    if claims.iat > now + 30:
        raise GrantError("issued_in_future")
    if now >= claims.exp:
        raise GrantError("expired")
    return claims
