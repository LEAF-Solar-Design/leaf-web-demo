"""Token-free multi-account grant proxy contract."""

import json

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
