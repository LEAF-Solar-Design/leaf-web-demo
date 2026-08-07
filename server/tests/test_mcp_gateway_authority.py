"""Tenant MCP credentials are app-owned, short-lived, and turn-bound."""
from __future__ import annotations

import hashlib
import json

import jwt
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

import deps
import mcp_authority
import session_store
from routers import mcp_gateway


TENANT = "tenant-a"
SUBJECT = "auth0|alice"
OTHER_SUBJECT = "auth0|mallory"
TURN = "turn-a"
MODEL_SESSION = "model-session-a"
MOUNT = "subscription-a"
RESOLVED_KEY = "arn:aws:kms:us-east-1:807034087062:key/key-a"


class FakeKms:
    def __init__(self) -> None:
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = self.private.public_key()
        self.public_der = public.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self.public = public
        self.get_calls: list[str] = []
        self.sign_calls: list[str] = []

    def get_public_key(self, *, KeyId):
        self.get_calls.append(KeyId)
        return {"KeyId": RESOLVED_KEY, "PublicKey": self.public_der}

    def sign(self, *, KeyId, Message, MessageType, SigningAlgorithm):
        assert MessageType == "RAW"
        assert SigningAlgorithm == "RSASSA_PKCS1_V1_5_SHA_256"
        self.sign_calls.append(KeyId)
        return {
            "Signature": self.private.sign(
                Message, padding.PKCS1v15(), hashes.SHA256()
            )
        }


@pytest.fixture()
def authority(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", "dispatch-secret")
    monkeypatch.setenv(
        "LEAF_TENANT_MCP_ATTACHMENT_ISSUER", "https://platform.example/"
    )
    monkeypatch.setenv(
        "LEAF_TENANT_MCP_ATTACHMENT_AUDIENCE", "urn:leaf:tenant-mcp-broker"
    )
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "sessions.db")
    monkeypatch.setattr(session_store, "_conn", None)
    session_store.ensure_started()
    session = session_store.get_or_create_session(TENANT, "drawing-a")
    assert session_store.try_begin_turn(
        session["session_id"], TURN, 60, tier="hosted_pro", subject=SUBJECT
    )
    monkeypatch.setattr(
        deps,
        "resolve_active_platform_tenant_authority",
        lambda subject: (
            (TENANT, "hosted_pro")
            if subject == SUBJECT
            else (TENANT, "restricted")
        ),
    )
    kms = FakeKms()
    signer = mcp_authority.KmsRs256Signer(kms, "alias/attachment", clock=lambda: 1000)
    monkeypatch.setattr(mcp_authority, "signer", lambda: signer)
    monkeypatch.setattr(
        mcp_authority, "verify_subscription_mount", lambda tenant, mount: None
    )

    app = FastAPI()
    app.include_router(mcp_gateway.router)
    app.add_middleware(mcp_gateway.McpAuthorityBodyLimitMiddleware)
    client = TestClient(app, raise_server_exceptions=False)
    return client, session["session_id"], kms


def attachment_body(authority_session_id: str) -> dict[str, str]:
    return {
        "session_id": MODEL_SESSION,
        "authority_session_id": authority_session_id,
        "authority_turn_id": TURN,
        "subscription_mount_id": MOUNT,
        "runner_profile_id": "spine",
    }


def decode(token: str, public_key, audience: str) -> dict:
    return jwt.decode(
        token,
        public_key,
        algorithms=["RS256"],
        audience=audience,
        issuer="https://platform.example/",
        options={"verify_exp": False, "verify_iat": False, "verify_nbf": False},
    )


class StreamingGrantResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        read_error: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.closed = False
        self._body = body
        self._offset = 0
        self._read_error = read_error
        self.read_sizes: list[int] = []
        self.returned_bytes = 0
        self.raw = self

    def read(self, size: int, *, decode_content: bool) -> bytes:
        assert decode_content is True
        self.read_sizes.append(size)
        if self._read_error is not None:
            raise self._read_error
        chunk = self._body[self._offset:self._offset + size]
        self._offset += len(chunk)
        self.returned_bytes += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


def grant_body(accounts: list[dict] | None = None) -> bytes:
    return json.dumps({
        "linked": True,
        "accounts": accounts if accounts is not None else [
            {"id": MOUNT, "eligible": True},
            {"id": "ineligible", "eligible": False},
        ],
    }).encode("utf-8")


def test_kms_signer_resolves_alias_and_publishes_matching_jwk(monkeypatch):
    monkeypatch.setenv(
        "LEAF_TENANT_MCP_ATTACHMENT_ISSUER", "https://platform.example"
    )
    kms = FakeKms()
    signer = mcp_authority.KmsRs256Signer(kms, "alias/attachment", clock=lambda: 50)

    token, expires_at = signer.issue(
        {"subject_id": SUBJECT, "iss": "https://attacker.example/", "jti": "fixed"},
        audience="audience-a",
        ttl_seconds=120,
    )

    assert expires_at == 170
    assert kms.get_calls == ["alias/attachment"]
    assert kms.sign_calls == [RESOLVED_KEY]
    assert jwt.get_unverified_header(token)["kid"] == RESOLVED_KEY
    claims = decode(token, kms.public, "audience-a")
    assert claims["iss"] == "https://platform.example/"
    assert claims["jti"] != "fixed"
    assert signer.public_jwk()["kid"] == RESOLVED_KEY


def test_kms_provider_failure_is_sanitized(monkeypatch):
    monkeypatch.setenv(
        "LEAF_TENANT_MCP_ATTACHMENT_ISSUER", "https://platform.example"
    )
    kms = FakeKms()
    signer = mcp_authority.KmsRs256Signer(kms, "alias/attachment", clock=lambda: 50)
    kms.sign = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("provider detail"))

    with pytest.raises(mcp_authority.McpAuthorityError, match="KMS signing failed") as exc:
        signer.issue({}, audience="audience-a", ttl_seconds=120)

    assert "provider detail" not in str(exc.value)


def test_subscription_mount_uses_separate_grant_admin_authority(monkeypatch):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal:8120")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "harness-admin-secret")
    seen = {}

    responses: list[StreamingGrantResponse] = []

    def get(url, *, headers, timeout, allow_redirects, stream):
        seen.update(
            url=url,
            headers=headers,
            timeout=timeout,
            allow_redirects=allow_redirects,
            stream=stream,
        )
        response = StreamingGrantResponse(grant_body())
        responses.append(response)
        return response

    monkeypatch.setattr("requests.get", get)

    mcp_authority.verify_subscription_mount(TENANT, MOUNT)

    assert seen == {
        "url": f"http://harness.internal:8120/grants/{TENANT}",
        "headers": {"X-Harness-Secret": "harness-admin-secret"},
        "timeout": 5,
        "allow_redirects": False,
        "stream": True,
    }
    assert responses[0].closed is True
    with pytest.raises(mcp_authority.McpMountDenied):
        mcp_authority.verify_subscription_mount(TENANT, "ineligible")
    assert responses[1].closed is True


def test_subscription_mount_rejects_oversized_declared_body_without_reading(
    monkeypatch,
):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal:8120")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "harness-admin-secret")
    response = StreamingGrantResponse(
        grant_body(),
        headers={
            "Content-Length": str(
                mcp_authority.MOUNT_AUTHORITY_MAX_RESPONSE_BYTES + 1
            )
        },
    )
    monkeypatch.setattr("requests.get", lambda *_args, **_kwargs: response)

    with pytest.raises(mcp_authority.McpAuthorityError, match="too large"):
        mcp_authority.verify_subscription_mount(TENANT, MOUNT)

    assert response.returned_bytes == 0
    assert response.closed is True


@pytest.mark.parametrize(
    "headers",
    [
        {"Transfer-Encoding": "chunked"},
        {},
    ],
    ids=["chunked", "no-content-length"],
)
def test_subscription_mount_caps_actual_streamed_body(monkeypatch, headers):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal:8120")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "harness-admin-secret")
    limit = mcp_authority.MOUNT_AUTHORITY_MAX_RESPONSE_BYTES
    response = StreamingGrantResponse(b"x" * (limit + 100), headers=headers)
    monkeypatch.setattr("requests.get", lambda *_args, **_kwargs: response)

    with pytest.raises(mcp_authority.McpAuthorityError, match="too large"):
        mcp_authority.verify_subscription_mount(TENANT, MOUNT)

    assert response.returned_bytes == limit + 1
    assert max(response.read_sizes) <= 8192
    assert response.closed is True


@pytest.mark.parametrize(
    "body",
    [
        b'{"linked":true,"accounts":',
        b'{"linked":true,"linked":true,"accounts":[]}',
        json.dumps({
            "linked": True,
            "accounts": [{"id": MOUNT, "eligible": "yes"}],
        }).encode("utf-8"),
        grant_body([
            {"id": f"account-{index}", "eligible": True}
            for index in range(101)
        ]),
    ],
    ids=["malformed-json", "duplicate-key", "malformed-account", "too-many-accounts"],
)
def test_subscription_mount_rejects_malformed_or_unbounded_data(monkeypatch, body):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal:8120")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "harness-admin-secret")
    response = StreamingGrantResponse(body)
    monkeypatch.setattr("requests.get", lambda *_args, **_kwargs: response)

    with pytest.raises(mcp_authority.McpAuthorityError):
        mcp_authority.verify_subscription_mount(TENANT, MOUNT)

    assert response.closed is True


def test_subscription_mount_closes_on_read_transport_failure(monkeypatch):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal:8120")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "harness-admin-secret")
    response = StreamingGrantResponse(b"", read_error=OSError("private transport"))
    monkeypatch.setattr("requests.get", lambda *_args, **_kwargs: response)

    with pytest.raises(mcp_authority.McpAuthorityError, match="unavailable") as exc:
        mcp_authority.verify_subscription_mount(TENANT, MOUNT)

    assert "private transport" not in str(exc.value)
    assert response.closed is True


def test_subscription_mount_sanitizes_request_transport_failure(monkeypatch):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal:8120")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "harness-admin-secret")

    def fail(*_args, **_kwargs):
        raise OSError("private request transport")

    monkeypatch.setattr("requests.get", fail)
    with pytest.raises(mcp_authority.McpAuthorityError, match="unavailable") as exc:
        mcp_authority.verify_subscription_mount(TENANT, MOUNT)
    assert "private request transport" not in str(exc.value)


def test_subscription_mount_fails_closed_without_independent_verifier(monkeypatch):
    monkeypatch.delenv("LEAF_AUTHOR_HARNESS_URL", raising=False)
    monkeypatch.delenv("LEAF_HARNESS_SECRET", raising=False)

    with pytest.raises(mcp_authority.McpAuthorityError):
        mcp_authority.verify_subscription_mount(TENANT, MOUNT)


def test_subscription_mount_never_forwards_secret_across_redirect(monkeypatch):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal:8120")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "harness-admin-secret")

    response = StreamingGrantResponse(b"", status_code=302)

    def get(_url, *, headers, timeout, allow_redirects, stream):
        assert headers == {"X-Harness-Secret": "harness-admin-secret"}
        assert timeout == 5
        assert allow_redirects is False
        assert stream is True
        return response

    monkeypatch.setattr("requests.get", get)
    with pytest.raises(mcp_authority.McpAuthorityError):
        mcp_authority.verify_subscription_mount(TENANT, MOUNT)
    assert response.closed is True


def test_internal_exchange_binds_app_owned_authority_and_random_channel(authority):
    client, authority_session_id, kms = authority
    response = client.post(
        "/internal/mcp/gateway/attachment",
        json=attachment_body(authority_session_id),
        headers={"X-Tenant-Id": TENANT, "X-Dispatch-Secret": "dispatch-secret"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    claims = decode(body["bearer_token"], kms.public, "urn:leaf:tenant-mcp-broker")
    assert body["identity"] == {
        "tenant_id": TENANT,
        "subject_id": SUBJECT,
        "session_id": MODEL_SESSION,
        "authority_turn_id": TURN,
        "subscription_mount_id": MOUNT,
        "runner_profile_id": "spine",
    }
    assert claims["tenant_id"] == TENANT
    assert claims["subject_id"] == SUBJECT
    assert claims["session_id"] == MODEL_SESSION
    assert claims["authority_turn_id"] == TURN
    assert claims["subscription_mount_id"] == MOUNT
    assert claims["runner_profile_id"] == "spine"
    assert claims["plan"] == "pro"
    assert claims["scope"] == "tenant:services"
    assert set(claims["allowed_effects"]) == {
        "read", "external_read", "write", "external_write"
    }
    assert set(claims["allowed_services"]) == set(mcp_authority.PUBLIC_SERVICES)
    assert claims["channel_hash"] == hashlib.sha256(
        body["channel_secret"].encode("utf-8")
    ).hexdigest()
    assert len(body["channel_secret"]) >= 32
    assert claims["exp"] - claims["iat"] == 120
    assert response.headers["cache-control"] == "no-store"


def test_internal_exchange_binds_exact_author_profile(authority):
    client, authority_session_id, kms = authority
    response = client.post(
        "/internal/mcp/gateway/attachment",
        json={
            **attachment_body(authority_session_id),
            "runner_profile_id": "author",
        },
        headers={"X-Tenant-Id": TENANT, "X-Dispatch-Secret": "dispatch-secret"},
    )

    assert response.status_code == 200, response.text
    claims = decode(
        response.json()["bearer_token"], kms.public,
        "urn:leaf:tenant-mcp-broker",
    )
    assert claims["runner_profile_id"] == "author"
    assert response.json()["identity"]["runner_profile_id"] == "author"


def test_internal_exchange_requires_runner_profile(authority):
    client, authority_session_id, kms = authority
    body = attachment_body(authority_session_id)
    body.pop("runner_profile_id")

    response = client.post(
        "/internal/mcp/gateway/attachment",
        json=body,
        headers={"X-Tenant-Id": TENANT, "X-Dispatch-Secret": "dispatch-secret"},
    )

    assert response.status_code == 422
    assert kms.sign_calls == []


@pytest.mark.parametrize(
    "headers,body_change,status",
    [
        ({"X-Tenant-Id": TENANT}, {}, 401),
        (
            {"X-Tenant-Id": TENANT, "X-Dispatch-Secret": "wrong"},
            {},
            401,
        ),
        (
            {"X-Tenant-Id": TENANT, "X-Dispatch-Secret": "dispatch-secret"},
            {"authority_turn_id": "stale-turn"},
            409,
        ),
        (
            {"X-Tenant-Id": "tenant-b", "X-Dispatch-Secret": "dispatch-secret"},
            {},
            409,
        ),
        (
            {"X-Tenant-Id": TENANT, "X-Dispatch-Secret": "dispatch-secret"},
            {"runner_profile_id": "shell-editor"},
            422,
        ),
    ],
)
def test_internal_exchange_fails_closed_before_signing(
    authority, headers, body_change, status
):
    client, authority_session_id, kms = authority
    body = {**attachment_body(authority_session_id), **body_change}
    response = client.post(
        "/internal/mcp/gateway/attachment", json=body, headers=headers
    )

    assert response.status_code == status, response.text
    assert kms.sign_calls == []


@pytest.mark.parametrize(
    "error,status",
    [
        (mcp_authority.McpMountDenied("not owned"), 403),
        (mcp_authority.McpAuthorityError("verifier down"), 503),
    ],
)
def test_internal_exchange_requires_independent_mount_authority(
    authority, monkeypatch, error, status
):
    client, authority_session_id, kms = authority
    monkeypatch.setattr(
        mcp_authority,
        "verify_subscription_mount",
        lambda *_args: (_ for _ in ()).throw(error),
    )

    response = client.post(
        "/internal/mcp/gateway/attachment",
        json=attachment_body(authority_session_id),
        headers={"X-Tenant-Id": TENANT, "X-Dispatch-Secret": "dispatch-secret"},
    )

    assert response.status_code == status
    assert error.args[0] not in response.text
    assert kms.sign_calls == []


def test_human_token_is_bound_to_exact_approval_and_digest(authority):
    client, authority_session_id, kms = authority
    context = deps.TenantContext(
        TENANT,
        tier="hosted_pro",
        subject=SUBJECT,
        authority_resolved=True,
    )
    client.app.dependency_overrides[deps.require_tenant] = lambda: context
    body = {
        **attachment_body(authority_session_id),
        "approval_id": "approval_12345678",
        "argument_digest": "a" * 64,
    }

    response = client.post(
        "/api/mcp/gateway/approvals/token",
        json=body,
        headers={"Authorization": "Bearer test-human"},
    )

    assert response.status_code == 200, response.text
    claims = decode(
        response.json()["bearer_token"], kms.public, "urn:leaf:tenant-mcp-approval"
    )
    assert claims["sub"] == SUBJECT
    assert claims["tenant_id"] == TENANT
    assert claims["subject_id"] == SUBJECT
    assert claims["session_id"] == MODEL_SESSION
    assert claims["authority_turn_id"] == TURN
    assert claims["subscription_mount_id"] == MOUNT
    assert claims["runner_profile_id"] == "spine"
    assert claims["approval_id"] == "approval_12345678"
    assert claims["argument_digest"] == "a" * 64
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "context,change,status",
    [
        (deps.TenantContext(TENANT, subject=SUBJECT), {}, 401),
        (
            deps.TenantContext(
                TENANT, subject=SUBJECT, backedge=True, authority_resolved=True
            ),
            {},
            401,
        ),
        (
            deps.TenantContext(
                TENANT, subject=OTHER_SUBJECT, authority_resolved=True
            ),
            {},
            409,
        ),
        (
            deps.TenantContext(TENANT, subject=SUBJECT, authority_resolved=True),
            {"argument_digest": "A" * 64},
            422,
        ),
        (
            deps.TenantContext(TENANT, subject=SUBJECT, authority_resolved=True),
            {"approval_id": "short"},
            422,
        ),
    ],
)
def test_human_exchange_rejects_unbound_or_inexact_authority(
    authority, context, change, status
):
    client, authority_session_id, kms = authority
    client.app.dependency_overrides[deps.require_tenant] = lambda: context
    body = {
        **attachment_body(authority_session_id),
        "approval_id": "approval_12345678",
        "argument_digest": "a" * 64,
        **change,
    }

    response = client.post(
        "/api/mcp/gateway/approvals/token",
        json=body,
        headers={"Authorization": "Bearer test-human"},
    )

    assert response.status_code == status, response.text
    assert kms.sign_calls == []


def test_human_exchange_rechecks_mount_ownership(authority, monkeypatch):
    client, authority_session_id, kms = authority
    context = deps.TenantContext(
        TENANT, subject=SUBJECT, authority_resolved=True
    )
    client.app.dependency_overrides[deps.require_tenant] = lambda: context
    monkeypatch.setattr(
        mcp_authority,
        "verify_subscription_mount",
        lambda *_args: (_ for _ in ()).throw(mcp_authority.McpMountDenied()),
    )
    body = {
        **attachment_body(authority_session_id),
        "approval_id": "approval_12345678",
        "argument_digest": "a" * 64,
    }

    response = client.post(
        "/api/mcp/gateway/approvals/token",
        json=body,
        headers={"Authorization": "Bearer test-human"},
    )

    assert response.status_code == 403
    assert kms.sign_calls == []


def test_jwks_route_publishes_only_public_key_material(authority):
    client, _authority_session_id, _kms = authority
    response = client.get("/api/mcp/gateway/.well-known/jwks.json")

    assert response.status_code == 200
    body = response.json()
    assert len(body["keys"]) == 1
    assert body["keys"][0]["kid"] == RESOLVED_KEY
    assert set(body["keys"][0]) == {"kty", "use", "alg", "kid", "n", "e"}
    assert "private" not in json.dumps(body).lower()


def test_token_body_limit_runs_before_fastapi_parsing(authority):
    client, authority_session_id, kms = authority
    oversized = json.dumps({
        **attachment_body(authority_session_id),
        "padding": "x" * (mcp_gateway._TOKEN_BODY_MAX_BYTES + 1),
    })

    unauthenticated = client.post(
        "/internal/mcp/gateway/attachment",
        content=oversized,
        headers={"Content-Type": "application/json", "X-Tenant-Id": TENANT},
    )
    authenticated = client.post(
        "/internal/mcp/gateway/attachment",
        content=oversized,
        headers={
            "Content-Type": "application/json",
            "X-Tenant-Id": TENANT,
            "X-Dispatch-Secret": "dispatch-secret",
        },
    )
    missing_human = client.post(
        "/api/mcp/gateway/approvals/token",
        content=oversized,
        headers={"Content-Type": "application/json"},
    )

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 413
    assert missing_human.status_code == 401
    assert kms.sign_calls == []
