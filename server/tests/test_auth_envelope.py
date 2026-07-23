"""Frozen HTTP status and envelope vocabulary for platform authentication."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

import tenancy
from envelopes import DEFAULT_HTTP_STATUS, ErrorCode, error_response
from test_auth import _ENV, _client, bearer, mint


@pytest.fixture(autouse=True)
def live_auth(monkeypatch):
    for name, value in _ENV.items():
        monkeypatch.setenv(name, value)
    tenancy.reset_store()
    yield
    tenancy.reset_store()


def _auth_header(case: str):
    if case == "missing":
        return None, None
    if case == "malformed":
        value = "Basic sensitive-malformed-header"
        return value, value
    if case == "expired":
        token = mint(exp_delta=-30)
    elif case == "wrong_issuer":
        token = mint(iss="https://wrong-issuer.example/")
    elif case == "wrong_audience":
        token = mint(aud="https://wrong-audience.example")
    else:  # pragma: no cover
        raise AssertionError(case)
    return bearer(token), token


@pytest.mark.parametrize(
    "case", ["missing", "malformed", "expired", "wrong_issuer", "wrong_audience"])
def test_unauthenticated_matrix_has_exact_status_code_and_no_echo(case, capsys, caplog):
    header, sensitive = _auth_header(case)
    headers = {"Authorization": header} if header is not None else {}
    response = _client().get("/api/session", headers=headers)
    body = response.json()
    assert response.status_code == 401
    assert body["error"]["error_code"] == ErrorCode.UNAUTHENTICATED
    assert body["error"]["retryable"] is False
    assert body["degraded_mode"] is False
    captured = capsys.readouterr()
    observable = (
        json.dumps(body) + captured.out + captured.err
        + "".join(record.getMessage() for record in caplog.records)
    )
    if sensitive:
        assert sensitive not in observable


def test_insufficient_authorization_is_forbidden_without_token_echo(capsys, caplog):
    token = mint(include_tenant=False)
    response = _client().get(
        "/api/session", headers={"Authorization": bearer(token)})
    body = response.json()
    assert response.status_code == 403
    assert body["error"]["error_code"] == ErrorCode.FORBIDDEN
    assert body["error"]["retryable"] is False
    captured = capsys.readouterr()
    observable = (
        json.dumps(body) + captured.out + captured.err
        + "".join(record.getMessage() for record in caplog.records)
    )
    assert token not in observable


def test_frozen_codes_schema_and_grant_entitlement_semantics():
    assert ErrorCode.UNAUTHENTICATED in ErrorCode.ALL
    assert ErrorCode.FORBIDDEN in ErrorCode.ALL
    assert DEFAULT_HTTP_STATUS[ErrorCode.UNAUTHENTICATED] == 401
    assert DEFAULT_HTTP_STATUS[ErrorCode.FORBIDDEN] == 403
    assert DEFAULT_HTTP_STATUS[ErrorCode.GRANT_REQUIRED] == 401
    assert DEFAULT_HTTP_STATUS[ErrorCode.ENTITLEMENT_REQUIRED] == 403

    schema = json.loads(
        (Path(__file__).resolve().parent.parent /
         "envelope_schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    for code, status in (
            (ErrorCode.UNAUTHENTICATED, 401),
            (ErrorCode.FORBIDDEN, 403),
            (ErrorCode.GRANT_REQUIRED, 401),
            (ErrorCode.ENTITLEMENT_REQUIRED, 403)):
        response = error_response(code, "safe", retryable=False)
        assert response.status_code == status
        validator.validate(json.loads(response.body))
