"""Leaf-owned signing authority for tenant MCP attachment credentials.

The app owns the user, tenant, billing tier, and active-turn facts. This module
only turns an already resolved fact set into a short-lived RS256 JWT. AWS KMS
holds the private key. The signer resolves aliases once and then uses the
immutable KMS KeyId for both the JWT ``kid`` and every sign call.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import time
import urllib.parse
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey


ATTACHMENT_TTL_SECONDS = 120
APPROVAL_TTL_SECONDS = 120
APPROVAL_AUDIENCE = "urn:leaf:tenant-mcp-approval"
RUNNER_PROFILE_IDS = frozenset({"author", "spine"})
MOUNT_AUTHORITY_MAX_RESPONSE_BYTES = 64 * 1024
MOUNT_AUTHORITY_MAX_ACCOUNTS = 100

PUBLIC_SERVICES = (
    "artifact", "build", "chart", "code", "deployment", "diagram",
    "preview", "research", "runtime", "solar-reference", "source", "time",
    "visual", "workspace",
)

_TIER_AUTHORITY = {
    "restricted": ("starter", ("read",)),
    "hosted_starter": ("starter", ("read", "external_read")),
    "hosted_pro": (
        "pro", ("read", "external_read", "write", "external_write"),
    ),
    "self_hosted": (
        "enterprise", ("read", "external_read", "write", "external_write"),
    ),
    "admin": (
        "enterprise", ("read", "external_read", "write", "external_write"),
    ),
}


class McpAuthorityError(RuntimeError):
    """The app cannot safely mint an MCP authority credential."""


class McpMountDenied(McpAuthorityError):
    """The named subscription mount is not linked to the tenant."""


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _int_b64url(value: int) -> str:
    size = max(1, (value.bit_length() + 7) // 8)
    return _b64url(value.to_bytes(size, "big"))


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise McpAuthorityError(f"{name} is required")
    return value


def attachment_issuer() -> str:
    value = _required("LEAF_TENANT_MCP_ATTACHMENT_ISSUER")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise McpAuthorityError(
            "LEAF_TENANT_MCP_ATTACHMENT_ISSUER must be an HTTPS origin"
        )
    return value.rstrip("/") + "/"


def attachment_audience() -> str:
    return _required("LEAF_TENANT_MCP_ATTACHMENT_AUDIENCE")


def approval_audience() -> str:
    return APPROVAL_AUDIENCE


def runner_profile_id(value: str) -> str:
    if value not in RUNNER_PROFILE_IDS:
        raise McpAuthorityError("the requested MCP runner profile is not allowed")
    return value


def tier_authority(tier: str) -> tuple[str, tuple[str, ...]]:
    try:
        return _TIER_AUTHORITY[tier]
    except (KeyError, TypeError) as exc:
        raise McpAuthorityError("the active tenant tier has no MCP policy") from exc


def verify_subscription_mount(tenant_id: str, mount_id: str) -> None:
    """Verify a mount through the app-to-harness grant-admin credential.

    The dispatch secret does not grant subscription authority. The app uses a
    separate harness-admin secret to read the tenant's token-free grant status
    and requires an exact eligible account match. Missing configuration,
    transport failures, malformed responses, and legacy unscoped grants all
    fail closed.
    """
    base = os.environ.get("LEAF_AUTHOR_HARNESS_URL", "").strip().rstrip("/")
    secret = os.environ.get("LEAF_HARNESS_SECRET", "").strip()
    parsed = urlparse(base)
    if (
        not base
        or not secret
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise McpAuthorityError("subscription mount authority is not configured")
    url = f"{base}/grants/{urllib.parse.quote(tenant_id, safe='')}"
    try:
        import requests

        response = requests.get(
            url,
            headers={"X-Harness-Secret": secret},
            timeout=5,
            allow_redirects=False,
            stream=True,
        )
    except Exception as exc:  # noqa: BLE001 - provider errors are sanitized
        raise McpAuthorityError("subscription mount authority is unavailable") from exc
    try:
        if response.status_code != 200:
            raise McpAuthorityError(
                "subscription mount authority refused verification"
            )
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_length = int(content_length, 10)
            except (TypeError, ValueError) as exc:
                raise McpAuthorityError(
                    "subscription mount authority returned invalid data"
                ) from exc
            if declared_length < 0:
                raise McpAuthorityError(
                    "subscription mount authority returned invalid data"
                )
            if declared_length > MOUNT_AUTHORITY_MAX_RESPONSE_BYTES:
                raise McpAuthorityError(
                    "subscription mount authority response is too large"
                )

        raw = getattr(response, "raw", None)
        if raw is None or not callable(getattr(raw, "read", None)):
            raise McpAuthorityError(
                "subscription mount authority returned invalid data"
            )
        chunks: list[bytes] = []
        received = 0
        while received <= MOUNT_AUTHORITY_MAX_RESPONSE_BYTES:
            remaining = MOUNT_AUTHORITY_MAX_RESPONSE_BYTES + 1 - received
            try:
                chunk = raw.read(min(8192, remaining), decode_content=True)
            except Exception as exc:  # noqa: BLE001 - transport details are private
                raise McpAuthorityError(
                    "subscription mount authority is unavailable"
                ) from exc
            if not chunk:
                break
            if not isinstance(chunk, bytes) or len(chunk) > remaining:
                raise McpAuthorityError(
                    "subscription mount authority returned invalid data"
                )
            chunks.append(chunk)
            received += len(chunk)
            if received > MOUNT_AUTHORITY_MAX_RESPONSE_BYTES:
                raise McpAuthorityError(
                    "subscription mount authority response is too large"
                )
        try:
            text = b"".join(chunks).decode("utf-8")

            def reject_constant(value: str) -> None:
                raise ValueError(f"invalid JSON constant: {value}")

            def reject_duplicate_keys(
                pairs: list[tuple[str, Any]],
            ) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate JSON key")
                    result[key] = value
                return result

            body = json.loads(
                text,
                parse_constant=reject_constant,
                object_pairs_hook=reject_duplicate_keys,
            )
        except (UnicodeDecodeError, TypeError, ValueError) as exc:
            raise McpAuthorityError(
                "subscription mount authority returned invalid data"
            ) from exc
    finally:
        try:
            response.close()
        except Exception:  # noqa: BLE001 - close must not expose provider details
            pass
    if not isinstance(body, dict) or body.get("linked") is not True:
        raise McpMountDenied("subscription mount is not linked to this tenant")
    accounts = body.get("accounts")
    if (
        not isinstance(accounts, list)
        or len(accounts) > MOUNT_AUTHORITY_MAX_ACCOUNTS
        or any(
            not isinstance(account, dict)
            or not isinstance(account.get("id"), str)
            or type(account.get("eligible")) is not bool
            for account in accounts
        )
    ):
        raise McpAuthorityError("subscription mount authority returned invalid data")
    for account in accounts:
        if (
            isinstance(account, dict)
            and account.get("id") == mount_id
            and account.get("eligible") is True
        ):
            return
    raise McpMountDenied("subscription mount is not linked to this tenant")


class KmsRs256Signer:
    """Sign JWTs with one resolved RSA KMS key and publish its public JWK."""

    def __init__(
        self,
        kms_client: Any,
        key_id: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._kms = kms_client
        self._clock = clock
        try:
            response = self._kms.get_public_key(KeyId=key_id)
        except Exception as exc:  # noqa: BLE001 - provider errors are sanitized
            raise McpAuthorityError("KMS public key resolution failed") from exc
        resolved = response.get("KeyId")
        if not isinstance(resolved, str) or not resolved.strip():
            raise McpAuthorityError("KMS did not return an immutable KeyId")
        self._resolved_key_id = resolved
        try:
            public_key = serialization.load_der_public_key(response["PublicKey"])
        except (KeyError, TypeError, ValueError) as exc:
            raise McpAuthorityError("KMS returned an invalid public key") from exc
        if not isinstance(public_key, RSAPublicKey):
            raise McpAuthorityError("the tenant MCP signing key must be RSA")
        self._public_key = public_key

    @property
    def resolved_key_id(self) -> str:
        return self._resolved_key_id

    def issue(
        self,
        claims: Mapping[str, Any],
        *,
        audience: str,
        ttl_seconds: int,
    ) -> tuple[str, int]:
        if ttl_seconds < 1 or ttl_seconds > 300:
            raise McpAuthorityError("MCP token lifetime must be between 1 and 300 seconds")
        now = int(self._clock())
        expires_at = now + ttl_seconds
        body = {
            **dict(claims),
            "iss": attachment_issuer(), "aud": audience, "iat": now, "nbf": now,
            "exp": expires_at, "jti": secrets.token_urlsafe(24),
        }
        header = {"alg": "RS256", "kid": self._resolved_key_id, "typ": "JWT"}
        segments = (
            _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            _b64url(json.dumps(
                body, sort_keys=True, separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")),
        )
        signing_input = ".".join(segments).encode("ascii")
        try:
            response = self._kms.sign(
                KeyId=self._resolved_key_id,
                Message=signing_input,
                MessageType="RAW",
                SigningAlgorithm="RSASSA_PKCS1_V1_5_SHA_256",
            )
        except Exception as exc:  # noqa: BLE001 - provider errors are sanitized
            raise McpAuthorityError("KMS signing failed") from exc
        signature = response.get("Signature")
        if not isinstance(signature, (bytes, bytearray)) or not signature:
            raise McpAuthorityError("KMS returned an invalid signature")
        return f"{segments[0]}.{segments[1]}.{_b64url(bytes(signature))}", expires_at

    def public_jwk(self) -> dict[str, str]:
        numbers = self._public_key.public_numbers()
        return {
            "kty": "RSA", "use": "sig", "alg": "RS256",
            "kid": self._resolved_key_id,
            "n": _int_b64url(numbers.n), "e": _int_b64url(numbers.e),
        }


@lru_cache(maxsize=1)
def signer() -> KmsRs256Signer:
    try:
        import boto3  # type: ignore
    except ImportError as exc:  # pragma: no cover - production dependency gate
        raise McpAuthorityError("boto3 is required for tenant MCP signing") from exc
    region = os.environ.get("AWS_REGION", "us-east-1").strip() or "us-east-1"
    kms = boto3.client("kms", region_name=region)
    return KmsRs256Signer(
        kms, _required("LEAF_TENANT_MCP_ATTACHMENT_KMS_KEY_ID")
    )


def expires_at_iso(expires_at: int) -> str:
    return datetime.fromtimestamp(expires_at, timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
