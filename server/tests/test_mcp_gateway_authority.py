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
# What every mint/review boundary now emits for SUBJECT: the vendored
# tenant-services ID contract [A-Za-z0-9][A-Za-z0-9._:-]{0,255} rejects
# the pipe, so mcp_authority.harness_safe_subject maps it to ":".
SAFE_SUBJECT = "auth0:alice"
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
        {"subject_id": SAFE_SUBJECT, "iss": "https://attacker.example/", "jti": "fixed"},
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


def test_human_approval_host_uses_fixed_private_route_and_closes(monkeypatch):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal:8120")
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", "dispatch-secret")
    response = StreamingGrantResponse(b'{"status":"pending"}')
    seen = {}

    def post(url, *, headers, json, timeout, allow_redirects, stream):
        seen.update(
            url=url,
            headers=headers,
            json=json,
            timeout=timeout,
            allow_redirects=allow_redirects,
            stream=stream,
        )
        return response

    monkeypatch.setattr("requests.post", post)
    assert mcp_gateway._harness_approval_call("review", {"approval_id": "x"}) == {
        "status": "pending"
    }
    assert seen == {
        "url": "http://harness.internal:8120/internal/standard-services/approvals/review",
        "headers": {
            "X-Dispatch-Secret": "dispatch-secret",
            "Content-Type": "application/json",
        },
        "json": {"approval_id": "x"},
        "timeout": 5,
        "allow_redirects": False,
        "stream": True,
    }
    assert response.closed is True


@pytest.mark.parametrize(
    "base",
    [
        "https://user:password@harness.example",
        "https://harness.example/attacker-path",
        "https://harness.example?redirect=1",
        "file:///tmp/harness",
    ],
)
def test_human_approval_host_rejects_non_origin_configuration_before_network(
    monkeypatch, base
):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", base)
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", "dispatch-secret")
    post = lambda *_args, **_kwargs: pytest.fail("network must not be reached")
    monkeypatch.setattr("requests.post", post)

    with pytest.raises(mcp_authority.McpAuthorityError, match="unavailable"):
        mcp_gateway._harness_approval_call("review", {})


@pytest.mark.parametrize(
    "response",
    [
        StreamingGrantResponse(
            b'{}', headers={"Content-Length": str(64 * 1024 + 1)}
        ),
        StreamingGrantResponse(b"x" * (64 * 1024 + 100)),
        StreamingGrantResponse(b'{"status":"one","status":"two"}'),
        StreamingGrantResponse(b'{"status":'),
        StreamingGrantResponse(b"", read_error=OSError("private transport")),
    ],
    ids=["declared", "actual", "duplicate-key", "malformed", "transport"],
)
def test_human_approval_host_rejects_unbounded_or_invalid_response(
    monkeypatch, response
):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal:8120")
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", "dispatch-secret")
    monkeypatch.setattr("requests.post", lambda *_args, **_kwargs: response)

    with pytest.raises(mcp_authority.McpAuthorityError) as exc:
        mcp_gateway._harness_approval_call("review", {})

    assert "private transport" not in str(exc.value)
    assert response.closed is True
    if response._body.startswith(b"x"):
        assert response.returned_bytes == 64 * 1024 + 1
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
        "subject_id": SAFE_SUBJECT,
        "session_id": MODEL_SESSION,
        "authority_turn_id": TURN,
        "subscription_mount_id": MOUNT,
        "runner_profile_id": "spine",
    }
    assert claims["tenant_id"] == TENANT
    assert claims["subject_id"] == SAFE_SUBJECT
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
    assert claims["sub"] == SAFE_SUBJECT
    assert claims["tenant_id"] == TENANT
    assert claims["subject_id"] == SAFE_SUBJECT
    assert claims["session_id"] == MODEL_SESSION
    assert claims["authority_turn_id"] == TURN
    assert claims["subscription_mount_id"] == MOUNT
    assert claims["runner_profile_id"] == "spine"
    assert claims["approval_id"] == "approval_12345678"
    assert claims["argument_digest"] == "a" * 64
    assert response.headers["cache-control"] == "no-store"


def test_mcp_expiry_wire_is_canonical_bounded_rfc3339():
    assert mcp_authority.expires_at_iso(0) == "1970-01-01T00:00:00.000Z"


def test_authenticated_human_executes_only_reviewed_binding_and_gets_safe_receipt(
    authority, monkeypatch
):
    client, authority_session_id, kms = authority
    client.app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext(
        TENANT, tier="hosted_pro", subject=SUBJECT, authority_resolved=True
    )
    calls: list[tuple[str, dict]] = []

    def approval_call(path, payload):
        calls.append((path, payload))
        if path == "review":
            return {
                "status": "pending",
                "identity": {
                    "tenant_id": TENANT,
                    "subject_id": SAFE_SUBJECT,
                    "session_id": authority_session_id,
                    "authority_turn_id": TURN,
                    "subscription_mount_id": MOUNT,
                    "runner_profile_id": "spine",
                },
            }
        assert path == "execute"
        return {
            "status": "completed",
            "receipt_id": "b" * 64,
            "artifact_ids": ["-aaaaaaaaaaaaaaa", "_bbbbbbbbbbbbbbb"],
            "result": "must-not-be-reflected",
        }

    monkeypatch.setattr(mcp_gateway, "_harness_approval_call", approval_call)
    response = client.post(
        "/api/mcp/gateway/approvals/execute",
        json={
            "approval_id": "approval_12345678",
            "argument_digest": "a" * 64,
        },
        headers={"Authorization": "Bearer test-human"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "completed",
        "receipt_id": "b" * 64,
        "artifact_ids": ["-aaaaaaaaaaaaaaa", "_bbbbbbbbbbbbbbb"],
    }
    assert len(calls) == 2
    assert calls[0] == (
        "review",
        {
            "approval_id": "approval_12345678",
            "argument_digest": "a" * 64,
            "tenant_id": TENANT,
            "subject_id": SAFE_SUBJECT,
        },
    )
    execute_payload = calls[1][1]
    human_claims = decode(
        execute_payload["human_bearer"],
        kms.public,
        "urn:leaf:tenant-mcp-approval",
    )
    attachment_claims = decode(
        execute_payload["attachment"]["bearer_token"],
        kms.public,
        "urn:leaf:tenant-mcp-broker",
    )
    for claims in (human_claims, attachment_claims):
        assert claims["tenant_id"] == TENANT
        assert claims["subject_id"] == SAFE_SUBJECT
        assert claims["session_id"] == authority_session_id
        assert claims["authority_turn_id"] == TURN
        assert claims["subscription_mount_id"] == MOUNT
        assert claims["runner_profile_id"] == "spine"
    assert human_claims["approval_id"] == "approval_12345678"
    assert human_claims["argument_digest"] == "a" * 64
    assert execute_payload["identity"] == {
        "tenant_id": TENANT,
        "subject_id": SAFE_SUBJECT,
        "session_id": authority_session_id,
        "authority_turn_id": TURN,
        "subscription_mount_id": MOUNT,
        "runner_profile_id": "spine",
    }
    response_text = response.text
    assert execute_payload["human_bearer"] not in response_text
    assert execute_payload["attachment"]["bearer_token"] not in response_text
    assert execute_payload["attachment"]["channel_secret"] not in response_text
    assert "must-not-be-reflected" not in response_text
    receipt_events = [
        event for event in session_store.recent_events(authority_session_id, 20)
        if event["type"] == "standard_service_approval_receipt"
    ]
    assert receipt_events[-1]["data"] == {
        "status": "completed",
        "receipt_id": "b" * 64,
        "artifact_ids": ["-aaaaaaaaaaaaaaa", "_bbbbbbbbbbbbbbb"],
    }


@pytest.mark.parametrize(
    ("artifact_ids", "expected_status"),
    [
        (
            [f"artifact_{index:08d}" for index in range(64)],
            200,
        ),
        (
            [f"artifact_{index:08d}" for index in range(65)],
            409,
        ),
        ([".artifact00000000"], 409),
        (["artifact-short"], 409),
    ],
)
def test_human_approval_enforces_provider_artifact_contract_end_to_end(
    authority, monkeypatch, artifact_ids, expected_status
):
    client, authority_session_id, _kms = authority
    client.app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext(
        TENANT, tier="hosted_pro", subject=SUBJECT, authority_resolved=True
    )

    def approval_call(path, _payload):
        if path == "review":
            return {
                "status": "pending",
                "identity": {
                    "tenant_id": TENANT,
                    "subject_id": SAFE_SUBJECT,
                    "session_id": authority_session_id,
                    "authority_turn_id": TURN,
                    "subscription_mount_id": MOUNT,
                    "runner_profile_id": "spine",
                },
            }
        return {
            "status": "completed",
            "receipt_id": "d" * 64,
            "artifact_ids": artifact_ids,
        }

    monkeypatch.setattr(mcp_gateway, "_harness_approval_call", approval_call)

    response = client.post(
        "/api/mcp/gateway/approvals/execute",
        json={"approval_id": "approval_12345678", "argument_digest": "a" * 64},
        headers={"Authorization": "Bearer test-human"},
    )

    assert response.status_code == expected_status, response.text
    if expected_status == 200:
        assert response.json()["artifact_ids"] == artifact_ids


def test_vanished_session_fails_before_human_approval_execution(
    authority, monkeypatch
):
    client, authority_session_id, kms = authority
    client.app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext(
        TENANT, tier="hosted_pro", subject=SUBJECT, authority_resolved=True
    )
    calls: list[str] = []

    def approval_call(path, _payload):
        calls.append(path)
        assert path == "review"
        return {
            "status": "pending",
            "identity": {
                "tenant_id": TENANT,
                "subject_id": SAFE_SUBJECT,
                "session_id": authority_session_id,
                "authority_turn_id": TURN,
                "subscription_mount_id": MOUNT,
                "runner_profile_id": "spine",
            },
        }

    monkeypatch.setattr(mcp_gateway, "_harness_approval_call", approval_call)
    monkeypatch.setattr(session_store, "get_session", lambda _session_id: None)

    response = client.post(
        "/api/mcp/gateway/approvals/execute",
        json={"approval_id": "approval_12345678", "argument_digest": "a" * 64},
        headers={"Authorization": "Bearer test-human"},
    )

    assert response.status_code == 409
    assert calls == ["review"]
    assert kms.sign_calls == []


def test_completed_human_approval_retry_returns_journaled_receipt_without_mint_or_execute(
    authority, monkeypatch
):
    client, authority_session_id, kms = authority
    client.app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext(
        TENANT, tier="hosted_pro", subject=SUBJECT, authority_resolved=True
    )
    calls: list[str] = []

    def approval_call(path, _payload):
        calls.append(path)
        assert path == "review"
        return {
            "status": "completed",
            "identity": {
                "tenant_id": TENANT,
                "subject_id": SAFE_SUBJECT,
                "session_id": authority_session_id,
                "authority_turn_id": TURN,
                "subscription_mount_id": MOUNT,
                "runner_profile_id": "spine",
            },
            "receipt_id": "c" * 64,
            "artifact_ids": ["-aaaaaaaaaaaaaaa", "_bbbbbbbbbbbbbbb"],
        }

    monkeypatch.setattr(mcp_gateway, "_harness_approval_call", approval_call)
    first = client.post(
        "/api/mcp/gateway/approvals/execute",
        json={"approval_id": "approval_12345678", "argument_digest": "a" * 64},
        headers={"Authorization": "Bearer test-human"},
    )
    second = client.post(
        "/api/mcp/gateway/approvals/execute",
        json={"approval_id": "approval_12345678", "argument_digest": "a" * 64},
        headers={"Authorization": "Bearer test-human"},
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json() == second.json() == {
        "status": "completed",
        "receipt_id": "c" * 64,
        "artifact_ids": ["-aaaaaaaaaaaaaaa", "_bbbbbbbbbbbbbbb"],
    }
    assert calls == ["review", "review"]
    assert kms.sign_calls == []
    receipt_events = [
        event for event in session_store.recent_events(authority_session_id, 20)
        if event["type"] == "standard_service_approval_receipt"
    ]
    assert len(receipt_events) == 1


def test_uncertain_human_approval_is_observable_and_never_reexecutes(
    authority, monkeypatch
):
    client, authority_session_id, kms = authority
    client.app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext(
        TENANT, tier="hosted_pro", subject=SUBJECT, authority_resolved=True
    )
    state = "pending"
    calls: list[str] = []

    def approval_call(path, _payload):
        nonlocal state
        calls.append(path)
        if path == "execute":
            state = "uncertain"
            return {"status": "uncertain", "result": "must-not-be-reflected"}
        return {
            "status": state,
            "identity": {
                "tenant_id": TENANT,
                "subject_id": SAFE_SUBJECT,
                "session_id": authority_session_id,
                "authority_turn_id": TURN,
                "subscription_mount_id": MOUNT,
                "runner_profile_id": "spine",
            },
        }

    monkeypatch.setattr(mcp_gateway, "_harness_approval_call", approval_call)
    request = {
        "json": {"approval_id": "approval_12345678", "argument_digest": "a" * 64},
        "headers": {"Authorization": "Bearer test-human"},
    }
    first = client.post("/api/mcp/gateway/approvals/execute", **request)
    sign_count = len(kms.sign_calls)
    second = client.post("/api/mcp/gateway/approvals/execute", **request)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json() == {"status": "uncertain"}
    assert second.json() == {"status": "uncertain"}
    assert "must-not-be-reflected" not in first.text
    assert calls == ["review", "execute", "review"]
    assert len(kms.sign_calls) == sign_count
    status_events = [
        event for event in session_store.recent_events(authority_session_id, 20)
        if event["type"] == "standard_service_approval_status"
    ]
    assert status_events[-1]["data"] == {
        "status": "uncertain",
        "approval_id": "approval_12345678",
    }


@pytest.mark.parametrize(
    "review_identity",
    [
        {
            "tenant_id": TENANT,
            "subject_id": OTHER_SUBJECT,
            "session_id": "session-other",
            "authority_turn_id": TURN,
            "subscription_mount_id": MOUNT,
            "runner_profile_id": "spine",
        },
        None,
    ],
    ids=["cross-subject", "missing-binding"],
)
def test_human_execution_fails_before_broker_call_for_unowned_binding(
    authority, monkeypatch, review_identity
):
    client, _authority_session_id, kms = authority
    client.app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext(
        TENANT, tier="hosted_pro", subject=SUBJECT, authority_resolved=True
    )
    calls: list[str] = []

    def approval_call(path, _payload):
        calls.append(path)
        assert path == "review"
        if review_identity is None:
            return {"status": "pending"}
        return {"status": "pending", "identity": review_identity}

    monkeypatch.setattr(mcp_gateway, "_harness_approval_call", approval_call)
    response = client.post(
        "/api/mcp/gateway/approvals/execute",
        json={"approval_id": "approval_12345678", "argument_digest": "a" * 64},
        headers={"Authorization": "Bearer test-human"},
    )

    assert response.status_code == 409
    assert calls == ["review"]
    assert kms.sign_calls == []


def test_human_execution_rechecks_mount_before_mint_or_execute(authority, monkeypatch):
    client, authority_session_id, kms = authority
    client.app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext(
        TENANT, tier="hosted_pro", subject=SUBJECT, authority_resolved=True
    )
    calls: list[str] = []
    monkeypatch.setattr(
        mcp_gateway,
        "_harness_approval_call",
        lambda path, _payload: calls.append(path) or {
            "status": "pending",
            "identity": {
                "tenant_id": TENANT,
                "subject_id": SAFE_SUBJECT,
                "session_id": authority_session_id,
                "authority_turn_id": TURN,
                "subscription_mount_id": MOUNT,
                "runner_profile_id": "spine",
            },
        },
    )
    monkeypatch.setattr(
        mcp_authority,
        "verify_subscription_mount",
        lambda *_args: (_ for _ in ()).throw(mcp_authority.McpMountDenied()),
    )

    response = client.post(
        "/api/mcp/gateway/approvals/execute",
        json={"approval_id": "approval_12345678", "argument_digest": "a" * 64},
        headers={"Authorization": "Bearer test-human"},
    )

    assert response.status_code == 403
    assert calls == ["review"]
    assert kms.sign_calls == []


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


def test_minted_subject_id_satisfies_the_vendored_id_contract(authority):
    """auth0 subjects carry a pipe; the vendored tenant-services ID contract
    is [A-Za-z0-9][A-Za-z0-9._:-]{0,255} (harness parseAttachment). The raw
    subject leaking into the minted identity killed every authority-carrying
    stage on staging (standard_services_resolver_setup_failed:
    standard_services_exchange_invalid:subject_id, observed 2026-08-26)."""
    import re as _re

    client, authority_session_id, kms = authority
    response = client.post(
        "/internal/mcp/gateway/attachment",
        json=attachment_body(authority_session_id),
        headers={"X-Tenant-Id": TENANT, "X-Dispatch-Secret": "dispatch-secret"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    claims = decode(body["bearer_token"], kms.public, "urn:leaf:tenant-mcp-broker")
    contract = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
    assert contract.fullmatch(body["identity"]["subject_id"])
    assert contract.fullmatch(claims["subject_id"])
    assert "|" not in body["identity"]["subject_id"]
