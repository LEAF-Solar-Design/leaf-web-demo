"""Token-free multi-account grant proxy contract."""

import json
import pytest

from routers.tenant import _status_body


def test_status_whitelists_multi_account_fields_without_secret_material():
    fake_token = "sk-ant-oat01-FAKE-never-return"
    body = _status_body({
        "linked": True,
        "linked_at": "2026-07-25T07:00:00Z",
        "kind": "oauth",
        "active_account_id": "account-a",
        "accounts": [
            {
                "id": "account-a",
                "label": "first@example.com",
                "kind": "oauth",
                "linked_at": "2026-07-25T07:00:00Z",
                "active": True,
                "plan": "team",
                "eligible": True,
                "usage_tokens": 314,
                "cooldown_until": "2026-07-25T07:15:00Z",
                "token": fake_token,
                "path": "/secret/file",
            },
            {"id": "bad", "label": "bad", "kind": "unknown", "token": fake_token},
        ],
        "token": fake_token,
    })

    assert body["active_account_id"] == "account-a"
    assert body["accounts"] == [{
        "id": "account-a",
        "label": "first@example.com",
        "kind": "oauth",
        "linked_at": "2026-07-25T07:00:00Z",
        "active": True,
        "plan": "team",
        "eligible": True,
        "usage_tokens": 314,
        "cooldown_until": "2026-07-25T07:15:00Z",
    }]
    serialized = json.dumps(body)
    assert fake_token not in serialized
    assert "/secret/file" not in serialized


@pytest.mark.parametrize("code", ["authentication_failed", "oauth_org_not_allowed"])
def test_status_surfaces_only_known_refusal_codes_and_iso_timestamp(code):
    body = _status_body({"accounts": [{
        "id": "a", "label": "My account", "kind": "oauth",
        "auth_refused_code": code, "auth_refused_at": "2026-11-25T12:30:00Z",
    }]})
    assert body["accounts"][0]["auth_refused_code"] == code
    assert body["accounts"][0]["auth_refused_at"] == "2026-11-25T12:30:00Z"


@pytest.mark.parametrize("code", ["unknown", "sk-ant-oat01-FAKE-secret", [], {}])
def test_status_drops_unknown_or_nonstring_refusal_codes(code):
    fake_token = "sk-ant-oat01-FAKE-secret"
    body = _status_body({"accounts": [{
        "id": "a", "label": "My account", "kind": "oauth",
        "auth_refused_code": code, "auth_refused_at": fake_token,
    }]})
    assert "auth_refused_code" not in body["accounts"][0]
    assert "auth_refused_at" not in body["accounts"][0]
    assert fake_token not in json.dumps(body)


@pytest.mark.parametrize("stamp", ["sk-ant-oat01-FAKE-secret", "x" * 41, "2026-11-25", [], {}, None])
def test_status_drops_invalid_refusal_timestamp_without_echoing_secret(stamp):
    body = _status_body({"accounts": [{
        "id": "a", "label": "My account", "kind": "oauth",
        "auth_refused_code": "authentication_failed", "auth_refused_at": stamp,
    }]})
    assert body["accounts"][0]["auth_refused_code"] == "authentication_failed"
    assert "auth_refused_at" not in body["accounts"][0]
    assert "sk-ant-oat01-FAKE-secret" not in json.dumps(body)
