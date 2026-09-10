"""Offline proofs for the campaign broker's signed developer transport."""
import json

import pytest
import requests
from botocore.credentials import Credentials

from developer_jobs_transport import (
    CHUNK_BYTES, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, DeveloperJobsTransport, TransportError,
)


ENDPOINT = "https://abcdefghij.execute-api.us-east-1.amazonaws.com/v1/developer/jobs"


class Session:
    def __init__(self):
        self.roles = []

    def get_credentials(self):
        return Credentials("TESTACCESS", "test-secret-for-offline-signing", "test-session")

    def client(self, name, **kwargs):
        assert name == "sts"
        assert set(kwargs) == {"region_name", "config"}
        assert kwargs["region_name"] == "us-east-1"
        config = kwargs["config"]
        assert config.connect_timeout == 5
        assert config.read_timeout == 10
        assert config.retries == {"total_max_attempts": 1}
        return self

    def assume_role(self, **kwargs):
        self.roles.append(kwargs)
        return {"Credentials": {"AccessKeyId": "ASSUMEDACCESS", "SecretAccessKey": "offline-assumed-secret",
                                "SessionToken": "offline-assumed-token"}}


class Response:
    def __init__(self, chunks=None, status=200, headers=None, error=None):
        self.status_code = status
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.chunks = chunks if chunks is not None else [b'{"ok":true}']
        self.error = error
        self.closed = False

    def iter_content(self, *, chunk_size):
        assert chunk_size == CHUNK_BYTES
        if self.error:
            raise self.error
        yield from self.chunks

    def close(self):
        self.closed = True


class HTTP:
    def __init__(self, response=None, error=None):
        self.response = response if response is not None else Response()
        self.error = error
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error
        return self.response


def transport(http=None, **kwargs):
    return DeveloperJobsTransport(ENDPOINT, boto3_session=Session(),
                                  http_session=http if http is not None else HTTP(), **kwargs)


@pytest.mark.parametrize("action", ["submit", "inspect", "cancel", "retry", "recover"])
def test_signs_exact_canonical_body_once(action):
    http = HTTP()
    body = {"action": action, "operation_id": "a" * 64, "nested": {"z": 1, "a": "é"}}
    assert transport(http)(body) == {"ok": True}
    assert len(http.calls) == 1
    args, call = http.calls[0]
    assert args == (ENDPOINT,)
    assert call["data"] == json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    assert json.loads(call["data"]) == body
    assert "/us-east-1/execute-api/aws4_request" in call["headers"]["Authorization"]
    assert "Credential=TESTACCESS/" in call["headers"]["Authorization"]
    assert call["headers"]["X-Amz-Security-Token"] == "test-session"
    assert call["timeout"] == (5, 30)
    assert call["allow_redirects"] is False and call["stream"] is True
    assert http.response.closed


@pytest.mark.parametrize("endpoint", [
    ENDPOINT.replace("https:", "http:"),
    ENDPOINT.replace("abcdefghij.execute-api.us-east-1.amazonaws.com", "localhost"),
    ENDPOINT.replace("us-east-1", "us-west-2"),
    ENDPOINT.replace("https://", "https://user:password@"),
    ENDPOINT + "?", ENDPOINT + "#fragment", ENDPOINT + "/",
    ENDPOINT.replace("/v1/", "/v2/"), ENDPOINT.replace(".com/", ".com:443/"),
    ENDPOINT.replace(".com/", ".com.evil.example/"),
])
def test_endpoint_allowlist(endpoint):
    with pytest.raises(TransportError, match="invalid_endpoint"):
        DeveloperJobsTransport(endpoint, boto3_session=Session(), http_session=HTTP())


def test_optional_role_is_assumed_in_process():
    session, http = Session(), HTTP()
    role = "arn:aws:iam::123456789012:role/campaign-broker"
    client = DeveloperJobsTransport(ENDPOINT, role_arn=role, boto3_session=session, http_session=http)
    client({"action": "inspect"})
    assert session.roles == [{"RoleArn": role, "RoleSessionName": "campaign-developer-broker"}]
    assert "Credential=ASSUMEDACCESS/" in http.calls[0][1]["headers"]["Authorization"]


@pytest.mark.parametrize("response,code", [
    (Response(status=302, headers={"Location": "https://evil.example/"}), "redirect_refused"),
    (Response(status=403, chunks=[b'sensitive endpoint error body']), "developer_http_error"),
    (Response(headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)}), "response_too_large"),
    (Response(headers={"Content-Length": "-1"}), "invalid_response_length"),
    (Response(chunks=[b"x" * (CHUNK_BYTES + 1)]), "response_too_large"),
    (Response(chunks=[b"x" * CHUNK_BYTES] * 17), "response_too_large"),
    (Response(chunks=[b'{"x":1,"x":2}']), "invalid_json_response"),
    (Response(chunks=[b'{"nested":{"x":1,"x":2}}']), "invalid_json_response"),
    (Response(chunks=[b'{"x":NaN}']), "invalid_json_response"),
    (Response(chunks=[b"[]"]), "invalid_response_shape"),
    (Response(chunks=[b"null"]), "invalid_response_shape"),
    (Response(headers={"Content-Type": "text/html"}), "non_json_response"),
])
def test_bad_response_sanitized_and_closed(response, code):
    http = HTTP(response)
    with pytest.raises(TransportError) as error:
        transport(http)({"action": "submit"})
    assert error.value.code == code
    assert str(error.value) == code
    assert response.closed and len(http.calls) == 1


@pytest.mark.parametrize("exception,kind", [
    (requests.exceptions.Timeout("sensitive address and headers"), TimeoutError),
    (requests.exceptions.ConnectionError("sensitive address and headers"), ConnectionError),
])
@pytest.mark.parametrize("during_stream", [False, True])
def test_network_outcome_unknown_sanitized_no_retry(exception, kind, during_stream):
    response = Response(error=exception if during_stream else None)
    http = HTTP(response, error=None if during_stream else exception)
    with pytest.raises(kind) as error:
        transport(http)({"action": "cancel"})
    assert "sensitive" not in str(error.value)
    assert len(http.calls) == 1
    if during_stream:
        assert response.closed


def test_invalid_action_makes_no_request():
    http = HTTP()
    with pytest.raises(TransportError):
        transport(http)({"action": "delete_everything"})
    assert not http.calls


def test_missing_credentials_is_sanitized():
    class Missing(Session):
        def get_credentials(self):
            raise RuntimeError("sensitive credential source")
    http = HTTP()
    client = DeveloperJobsTransport(ENDPOINT, boto3_session=Missing(), http_session=http)
    with pytest.raises(TransportError, match="credentials_unavailable"):
        client({"action": "submit"})
    assert not http.calls


def test_default_http_session_ignores_ambient_netrc_and_proxy(monkeypatch):
    http = HTTP()
    http.trust_env = True
    monkeypatch.setattr(requests, "Session", lambda: http)
    DeveloperJobsTransport(ENDPOINT, boto3_session=Session())
    assert http.trust_env is False


def test_request_limit_precedes_credentials_and_http():
    class Untouched(Session):
        def get_credentials(self):
            pytest.fail("oversized request reached credentials")
    http = HTTP()
    client = DeveloperJobsTransport(ENDPOINT, boto3_session=Untouched(), http_session=http)
    with pytest.raises(TransportError) as error:
        client({"action": "submit", "payload": "x" * MAX_REQUEST_BYTES})
    assert error.value.code == "request_too_large"
    assert error.value.status == 413
    assert not http.calls
