"""The real-service author path must not disguise harness failures."""

import requests
from fastapi.testclient import TestClient

from app import app


def test_live_harness_timeout_is_honest_and_does_not_template(monkeypatch):
    observed = {}

    def timeout(*_args, **kwargs):
        observed["timeout"] = kwargs["timeout"]
        raise requests.Timeout("authoring took too long")

    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.invalid")
    monkeypatch.setenv("LEAF_AUTHOR_TIMEOUT_S", "300")
    monkeypatch.setenv("LEAF_AUTHOR_TEMPLATE_FALLBACK", "0")
    monkeypatch.setattr(requests, "post", timeout)

    response = TestClient(app).post(
        "/api/author",
        json={"description": "rearrange panels into a sitting cat"},
        headers={"X-Tenant-Id": "demo-tenant"},
    )

    assert observed["timeout"] == 300
    assert response.status_code == 504
    body = response.json()
    assert body["source"] == "harness_unavailable"
    assert body["tool"] is None
    assert body["error"]["error_code"] == "TIMEOUT"
    assert "templated" not in response.text.lower()
