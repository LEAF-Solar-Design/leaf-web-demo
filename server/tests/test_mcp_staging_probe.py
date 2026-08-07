"""The deployment canary proves the private tenant MCP staging contract."""
from __future__ import annotations

import hashlib

import jwt
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import mcp_authority
import mcp_staging_probe


RESOLVED_KEY = "arn:aws:kms:us-east-1:807034087062:key/canary"


class FakeKms:
    def __init__(self) -> None:
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public = self.private.public_key()
        self.sign_calls: list[str] = []

    def get_public_key(self, *, KeyId):
        return {
            "KeyId": RESOLVED_KEY,
            "PublicKey": self.public.public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ),
        }

    def sign(self, *, KeyId, Message, MessageType, SigningAlgorithm):
        assert MessageType == "RAW"
        assert SigningAlgorithm == "RSASSA_PKCS1_V1_5_SHA_256"
        self.sign_calls.append(KeyId)
        return {
            "Signature": self.private.sign(
                Message, padding.PKCS1v15(), hashes.SHA256()
            )
        }


class CapturingSigner:
    def __init__(self) -> None:
        self.calls: list[tuple[dict, str, int]] = []

    def issue(self, claims, *, audience, ttl_seconds):
        self.calls.append((dict(claims), audience, ttl_seconds))
        return f"header.payload.signature{len(self.calls)}", 1060


class FakeHttpResponse:
    def __init__(
        self,
        body: bytes = b"{}",
        *,
        status_code: int = 200,
        content_type: str = "application/json",
    ) -> None:
        self.body = body
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self.closed = False

    def iter_content(self, chunk_size):
        assert chunk_size == 8192
        yield self.body

    def close(self):
        self.closed = True


@pytest.fixture()
def staging(monkeypatch):
    monkeypatch.setenv(mcp_staging_probe.ENABLE_ENV, "1")
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "staging")
    monkeypatch.setenv(
        mcp_staging_probe.BROKER_URL_ENV, "https://staging-api.leafdesign.ai"
    )
    monkeypatch.setenv(
        "LEAF_TENANT_MCP_ATTACHMENT_ISSUER", "https://platform.example/"
    )
    monkeypatch.setenv(
        "LEAF_TENANT_MCP_ATTACHMENT_AUDIENCE", "urn:leaf:tenant-mcp-broker"
    )


def initialize_response(*, protocol=None, request_id=1):
    protocol = protocol or mcp_staging_probe.MCP_PROTOCOL_VERSION
    return mcp_staging_probe.JsonResponse(
        200,
        {"mcp-session-id": "mcp-session-a"},
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": protocol,
                "capabilities": {},
                "serverInfo": {"name": "tenant-broker", "version": "1"},
            },
        },
    )


def rejected_response(status_code):
    return mcp_staging_probe.JsonResponse(status_code, {}, {})


def status_response(claims, *, request_id=2, **overrides):
    status = {
        "status": "ready",
        "contract_version": "1",
        "tenant_id": claims["tenant_id"],
        "subject_id": claims["subject_id"],
        "session_id": claims["session_id"],
        "authority_turn_id": claims["authority_turn_id"],
        "subscription_mount_id": claims["subscription_mount_id"],
        "runner_profile_id": claims["runner_profile_id"],
        "plan": claims["plan"],
        "scope": claims["scope"],
        "allowed_services": claims["allowed_services"],
        "allowed_effects": claims["allowed_effects"],
        "available_tool_count": 1,
        **overrides,
    }
    return mcp_staging_probe.JsonResponse(
        200,
        {},
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"isError": False, "structuredContent": status},
        },
    )


def wrong_channel_response(code="invalid_channel", request_id=13):
    return mcp_staging_probe.JsonResponse(
        200,
        {},
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "isError": False,
                "structuredContent": {"status": "error", "code": code},
            },
        },
    )


def test_canary_is_kms_signed_with_only_time_read_authority(staging):
    kms = FakeKms()
    signer = mcp_authority.KmsRs256Signer(kms, "alias/canary", clock=lambda: 1000)

    attachment = mcp_staging_probe.create_canary_attachment(signer)

    claims = jwt.decode(
        attachment.bearer,
        kms.public,
        algorithms=["RS256"],
        audience="urn:leaf:tenant-mcp-broker",
        issuer="https://platform.example/",
        options={"verify_exp": False, "verify_iat": False, "verify_nbf": False},
    )
    assert jwt.get_unverified_header(attachment.bearer)["kid"] == RESOLVED_KEY
    assert kms.sign_calls == [RESOLVED_KEY]
    assert attachment.expires_at == 1060
    assert claims["exp"] - claims["iat"] == 60
    assert claims["scope"] == "tenant:services"
    assert claims["tenant_id"] == "leaf-platform-deployment-canary"
    assert claims["subject_id"] == "leaf-platform-deployment-canary"
    assert claims["subscription_mount_id"] == "leaf-platform-deployment-canary"
    assert claims["session_id"].startswith("leaf-platform-deployment-canary-session-")
    assert claims["authority_turn_id"].startswith(
        "leaf-platform-deployment-canary-turn-"
    )
    assert claims["runner_profile_id"] == "spine"
    assert claims["plan"] == "starter"
    assert claims["allowed_services"] == ["time"]
    assert claims["allowed_effects"] == ["read"]
    assert claims["channel_hash"] == hashlib.sha256(
        attachment.channel_secret.encode()
    ).hexdigest()
    assert "authority_session_id" not in claims


def test_canary_uses_fresh_channel_session_turn_and_jti(staging):
    kms = FakeKms()
    signer = mcp_authority.KmsRs256Signer(kms, "alias/canary", clock=lambda: 1000)

    first = mcp_staging_probe.create_canary_attachment(signer)
    second = mcp_staging_probe.create_canary_attachment(signer)
    first_claims = jwt.decode(first.bearer, options={"verify_signature": False})
    second_claims = jwt.decode(second.bearer, options={"verify_signature": False})
    assert first.channel_secret != second.channel_secret
    assert first_claims["session_id"] != second_claims["session_id"]
    assert first_claims["authority_turn_id"] != second_claims["authority_turn_id"]
    assert first_claims["jti"] != second_claims["jti"]


def test_probe_initializes_and_calls_only_services_status(staging):
    signer = CapturingSigner()
    calls = []

    def transport(url, headers, payload):
        calls.append((url, dict(headers), dict(payload)))
        if len(calls) == 1:
            return rejected_response(401)
        if len(calls) == 2:
            return initialize_response(request_id=11)
        if len(calls) == 3:
            return status_response(signer.calls[0][0], request_id=12)
        if len(calls) == 4:
            return wrong_channel_response()
        if len(calls) == 5:
            return initialize_response()
        return status_response(signer.calls[1][0])

    version = mcp_staging_probe.run_probe(
        authority_signer=signer, transport=transport
    )

    assert version == "1"
    assert [call[0] for call in calls] == [
        "https://staging-api.leafdesign.ai/mcp",
        "https://staging-api.leafdesign.ai/mcp",
        "https://staging-api.leafdesign.ai/mcp",
        "https://staging-api.leafdesign.ai/mcp",
        "https://staging-api.leafdesign.ai/mcp",
        "https://staging-api.leafdesign.ai/mcp",
    ]
    assert [call[2]["id"] for call in calls] == [91, 11, 12, 13, 1, 2]
    assert calls[0][1]["Authorization"] != calls[1][1]["Authorization"]
    assert calls[0][1]["x-leaf-gateway-channel"] == (
        calls[1][1]["x-leaf-gateway-channel"]
    )
    assert calls[1][1]["Authorization"] == "Bearer header.payload.signature1"
    assert calls[1][1]["x-leaf-gateway-channel"] == (
        calls[2][1]["x-leaf-gateway-channel"]
    )
    assert calls[1][1]["x-leaf-gateway-channel"] != (
        calls[3][1]["x-leaf-gateway-channel"]
    )
    assert calls[3][2] == {
        "jsonrpc": "2.0",
        "id": 13,
        "method": "tools/call",
        "params": {"name": "services_status", "arguments": {}},
    }
    assert calls[5][2] == {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "services_status", "arguments": {}},
    }
    assert calls[4][1]["Authorization"] == "Bearer header.payload.signature2"
    assert len(calls[4][1]["x-leaf-gateway-channel"]) >= 32
    assert calls[4][1]["x-leaf-gateway-channel"] != (
        calls[1][1]["x-leaf-gateway-channel"]
    )
    assert calls[5][1]["mcp-session-id"] == "mcp-session-a"
    assert calls[5][1]["MCP-Protocol-Version"] == "2025-11-25"
    assert len(signer.calls) == 2
    for index, call_index in ((0, 1), (1, 4)):
        claims, audience, ttl = signer.calls[index]
        assert audience == "urn:leaf:tenant-mcp-broker"
        assert ttl == 60
        assert claims["channel_hash"] == hashlib.sha256(
            calls[call_index][1]["x-leaf-gateway-channel"].encode()
        ).hexdigest()
    assert signer.calls[0][0]["session_id"] != signer.calls[1][0]["session_id"]
    assert signer.calls[0][0]["authority_turn_id"] != (
        signer.calls[1][0]["authority_turn_id"]
    )


def test_http_transport_is_bounded_and_does_not_follow_redirects(monkeypatch):
    response = FakeHttpResponse(b'{"jsonrpc":"2.0","id":1,"result":{}}')
    observed = {}

    def post(url, **kwargs):
        observed.update({"url": url, **kwargs})
        return response

    import requests

    monkeypatch.setattr(requests, "post", post)
    result = mcp_staging_probe._http_post_json(
        "https://broker.example/mcp",
        {"Authorization": "Bearer private"},
        {"jsonrpc": "2.0"},
    )

    assert result.body["jsonrpc"] == "2.0"
    assert observed["allow_redirects"] is False
    assert observed["stream"] is True
    assert observed["timeout"] == mcp_staging_probe.HTTP_TIMEOUT_SECONDS
    assert response.closed is True


def test_http_transport_returns_non_success_without_reading_body(monkeypatch):
    response = FakeHttpResponse(b"private-response", status_code=302)
    response.iter_content = lambda **_kwargs: pytest.fail("response body was read")
    import requests

    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: response)

    result = mcp_staging_probe._http_post_json(
        "https://broker.example/mcp", {}, {}
    )
    assert result.status_code == 302
    assert result.body == {}
    assert response.closed is True


def test_http_transport_rejects_oversized_or_non_json_responses(monkeypatch):
    import requests

    for response in (
        FakeHttpResponse(b"x" * (mcp_staging_probe.MAX_RESPONSE_BYTES + 1)),
        FakeHttpResponse(b"{}", content_type="text/event-stream"),
        FakeHttpResponse(b"not-json"),
    ):
        monkeypatch.setattr(requests, "post", lambda *args, **kwargs: response)
        with pytest.raises(mcp_staging_probe.ProbeError):
            mcp_staging_probe._http_post_json("https://broker.example/mcp", {}, {})
        assert response.closed is True


def test_probe_requires_explicit_enable(staging, monkeypatch):
    monkeypatch.delenv(mcp_staging_probe.ENABLE_ENV)
    signer = CapturingSigner()
    with pytest.raises(mcp_staging_probe.ProbeError):
        mcp_staging_probe.run_probe(
            authority_signer=signer, transport=pytest.fail
        )
    with pytest.raises(mcp_staging_probe.ProbeError):
        mcp_staging_probe.create_canary_attachment(signer)
    assert signer.calls == []


def test_probe_refuses_non_staging_runtime(staging, monkeypatch):
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "production")
    with pytest.raises(mcp_staging_probe.ProbeError):
        mcp_staging_probe.run_probe(
            authority_signer=CapturingSigner(), transport=pytest.fail
        )


def test_probe_rejects_any_broker_url_that_is_not_an_https_origin(
    staging, monkeypatch
):
    invalid_urls = (
        "http://broker.example",
        "https://user:pass@broker.example",
        "https://attacker.example",
        "https://broker.example/mcp",
        "https://broker.example?target=other",
        "https://broker.example#fragment",
    )
    for value in invalid_urls:
        monkeypatch.setenv(mcp_staging_probe.BROKER_URL_ENV, value)
        with pytest.raises(mcp_staging_probe.ProbeError):
            mcp_staging_probe.run_probe(
                authority_signer=CapturingSigner(), transport=pytest.fail
            )


def test_probe_rejects_initialize_contract_mismatches(staging):
    invalid = (
        mcp_staging_probe.JsonResponse(200, {}, {"jsonrpc": "2.0", "id": 11}),
        initialize_response(protocol="wrong", request_id=11),
        mcp_staging_probe.JsonResponse(
            200, {}, {"jsonrpc": "2.0", "id": 11, "error": {"code": -1}}
        ),
    )
    for initialized in invalid:
        responses = iter((rejected_response(401), initialized))
        with pytest.raises(mcp_staging_probe.ProbeError):
            mcp_staging_probe.run_probe(
                authority_signer=CapturingSigner(),
                transport=lambda *_args: next(responses),
            )


def test_probe_rejects_status_contract_mismatches(staging):
    overrides = (
        {"status": "degraded"},
        {"contract_version": "2"},
        {"available_tool_count": 0},
        {"available_tool_count": True},
    )
    for override in overrides:
        signer = CapturingSigner()
        call_count = 0

        def transport(*_args):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return rejected_response(401)
            if call_count == 2:
                return initialize_response(request_id=11)
            return status_response(
                signer.calls[0][0], request_id=12, **override
            )

        with pytest.raises(mcp_staging_probe.ProbeError):
            mcp_staging_probe.run_probe(
                authority_signer=signer,
                transport=transport,
            )


def test_probe_rejects_unauthenticated_canned_initialize(staging):
    with pytest.raises(mcp_staging_probe.ProbeError):
        mcp_staging_probe.run_probe(
            authority_signer=CapturingSigner(),
            transport=lambda *_args: initialize_response(),
        )


def test_probe_rejects_wrong_channel_acceptance(staging):
    signer = CapturingSigner()
    call_count = 0

    def transport(*_args):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return rejected_response(401)
        if call_count == 2:
            return initialize_response(request_id=11)
        if call_count == 3:
            return status_response(signer.calls[0][0], request_id=12)
        return status_response(signer.calls[0][0], request_id=13)

    with pytest.raises(mcp_staging_probe.ProbeError):
        mcp_staging_probe.run_probe(
            authority_signer=signer,
            transport=transport,
        )


@pytest.mark.parametrize("empty_status", (401, 403))
def test_probe_rejects_empty_http_wrong_channel_response(staging, empty_status):
    signer = CapturingSigner()
    call_count = 0

    def transport(*_args):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return rejected_response(401)
        if call_count == 2:
            return initialize_response(request_id=11)
        if call_count == 3:
            return status_response(signer.calls[0][0], request_id=12)
        return rejected_response(empty_status)

    with pytest.raises(mcp_staging_probe.ProbeError):
        mcp_staging_probe.run_probe(
            authority_signer=signer,
            transport=transport,
        )


def test_probe_rejects_widened_catalog_or_identity_mismatch(staging):
    overrides = (
        {"allowed_services": ["time", "research"]},
        {"allowed_effects": ["read", "external_read"]},
        {"tenant_id": "another-tenant"},
        {"subject_id": "another-subject"},
        {"session_id": "another-session"},
        {"authority_turn_id": "another-turn"},
        {"subscription_mount_id": "another-mount"},
        {"runner_profile_id": "author"},
        {"plan": "pro"},
        {"scope": "tenant:services tenant:admin"},
        {"available_tool_count": 2},
    )
    for override in overrides:
        signer = CapturingSigner()
        call_count = 0

        def transport(*_args):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return rejected_response(401)
            if call_count == 2:
                return initialize_response(request_id=11)
            return status_response(
                signer.calls[0][0], request_id=12, **override
            )

        with pytest.raises(mcp_staging_probe.ProbeError):
            mcp_staging_probe.run_probe(
                authority_signer=signer,
                transport=transport,
            )


def test_wrong_channel_status_is_never_replaced_by_wrong_channel_initialize(staging):
    signer = CapturingSigner()
    challenge_bearer = None
    challenge_channel = None
    saw_wrong_status = False

    def transport(_url, headers, payload):
        nonlocal challenge_bearer, challenge_channel, saw_wrong_status
        request_id = payload["id"]
        if request_id == 91:
            return rejected_response(401)
        if request_id == 11:
            challenge_bearer = headers["Authorization"]
            challenge_channel = headers["x-leaf-gateway-channel"]
            return initialize_response(request_id=11)
        if (
            payload["method"] == "initialize"
            and headers["Authorization"] == challenge_bearer
            and headers["x-leaf-gateway-channel"] != challenge_channel
        ):
            pytest.fail("the probe attempted a skippable wrong-channel initialize")
        if request_id == 12:
            return status_response(signer.calls[0][0], request_id=12)
        if request_id == 13:
            assert payload["method"] == "tools/call"
            assert headers["Authorization"] == challenge_bearer
            assert headers["x-leaf-gateway-channel"] != challenge_channel
            saw_wrong_status = True
            return wrong_channel_response()
        if request_id == 1:
            return initialize_response()
        return status_response(signer.calls[1][0])

    assert mcp_staging_probe.run_probe(
        authority_signer=signer,
        transport=transport,
    ) == "1"
    assert saw_wrong_status is True


def test_command_output_never_contains_private_probe_material(monkeypatch, capsys):
    monkeypatch.setattr(mcp_staging_probe, "run_probe", lambda: "1")
    assert mcp_staging_probe.main() == 0
    assert capsys.readouterr().out == (
        "tenant_mcp_staging_probe=PASS contract_version=1\n"
    )

    def fail():
        raise RuntimeError("private-bearer private-channel private-claims")

    monkeypatch.setattr(mcp_staging_probe, "run_probe", fail)
    assert mcp_staging_probe.main() == 1
    output = capsys.readouterr().out
    assert output == "tenant_mcp_staging_probe=FAIL\n"
    assert "private" not in output
