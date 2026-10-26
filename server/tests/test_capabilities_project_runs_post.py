"""Bounded body transport preserves the existing project catalog admission."""
import asyncio
from copy import deepcopy
import json
from uuid import UUID

import pytest
from fastapi import Request

import envelopes
from routers import capabilities as catalog_route
import test_sip_r5_routes as routes
from test_sip_r5_routes import lane  # noqa: F401


MESSAGE = "project_runs must contain valid project admission requests"
EMPTY_BODY = b'{"project_runs":{}}'


def request_parts(lane, params=None, name="solar-settings"):
    request = routes.body(lane, params, name)
    query = {"project_id": str(lane.s.project), "drawing_id": str(lane.s.drawing),
             "input_version_id": request["dwg"]}
    entries = {name: {"params": request["params"],
                      "catalog_digest": request["catalog_digest"],
                      "idempotency_key": "key"}}
    return query, entries


def assert_bad_params(response):
    assert response.status_code == 400
    assert response.json() == envelopes.err_envelope(envelopes.ErrorCode.BAD_PARAMS, MESSAGE, False)


def test_post_matches_existing_get_project_preview(lane):
    query, entries = request_parts(lane)
    before = deepcopy((lane.s.blobs, lane.s.versions, lane.s.fingerprints, lane.s.writes))
    expected = routes.preview(lane)
    response = lane.client.post("/api/capabilities", params=query,
        json={"project_runs": entries}, headers=routes.headers(lane))
    assert response.status_code == expected.status_code == 200
    assert response.json() == expected.json()
    state = routes.states(response)["solar-settings"]
    assert state["runnable"] and state["admission_checked"]
    assert state["admission"] == {"status_code": 200, "error": None}
    assert not lane.a.accepted
    assert (lane.s.blobs, lane.s.versions, lane.s.fingerprints, lane.s.writes) == before


@pytest.mark.parametrize("entries", [
    {"unknown-tool": {"params": {}, "catalog_digest": "digest", "idempotency_key": "key"}},
    {"solar-settings": []},
    {"solar-settings": {"params": {}, "catalog_digest": "digest"}},
], ids=["unknown-tool", "bad-entry", "missing-key"])
def test_invalid_entries_match_get(lane, entries):
    query, _ = request_parts(lane)
    expected = lane.client.get("/api/capabilities",
        params={**query, "project_runs": json.dumps(entries)}, headers=routes.headers(lane))
    response = lane.client.post("/api/capabilities", params=query,
        json={"project_runs": entries}, headers=routes.headers(lane))
    assert_bad_params(expected)
    assert_bad_params(response)
    assert response.json() == expected.json()


@pytest.mark.parametrize("name,fields", [
    ("solar-solve-proposal", {"solve_context": {}}),
    ("solar-solve-proposal", {"solve_context": "context"}),
    ("solar-commit-solve", {"proposal_job_id": "not-a-uuid"}),
], ids=["empty-context", "string-context", "invalid-proposal-id"])
@pytest.mark.parametrize("method", ["POST", "GET"])
def test_malformed_solve_fields_are_bad_params(lane, name, fields, method):
    query, entries = request_parts(lane, params={}, name=name)
    available = lane.client.get("/api/capabilities", params=query, headers=routes.headers(lane))
    assert routes.states(available)[name]["engine_ready"]
    entries[name].update(fields)
    if method == "POST":
        response = lane.client.post("/api/capabilities", params=query,
            json={"project_runs": entries}, headers=routes.headers(lane))
    else:
        response = lane.client.get("/api/capabilities",
            params={**query, "project_runs": json.dumps(entries)}, headers=routes.headers(lane))
    assert_bad_params(response)
    assert not lane.a.accepted


@pytest.mark.parametrize("content", [
    b"not JSON",
    b'\xff{"project_runs":{}}',
    '{"project_runs":{}}'.encode("utf-16"),
    b"[]",
    b"{}",
    b'{"project_runs":{},"extra":true}',
    b'{"project_runs":"{}"}',
    b'{"project_runs":null}',
    b'{"project_runs":{},"project_runs":{}}',
    b'{"project_runs":{"solar-settings":{"params":{"x":NaN},'
    b'"catalog_digest":"digest","idempotency_key":"key"}}}',
    b'{"project_runs":{"solar-settings":{"params":{"x":1e999},'
    b'"catalog_digest":"digest","idempotency_key":"key"}}}',
    EMPTY_BODY + b" " * (262145 - len(EMPTY_BODY)),
], ids=[
    "non-json", "invalid-utf8", "utf16", "array", "missing-project-runs",
    "extra-key", "string-project-runs", "null-project-runs", "duplicate-key",
    "nan", "infinite-float", "oversized",
])
def test_malformed_or_oversized_body_is_bad_params(lane, content):
    query, _ = request_parts(lane)
    response = lane.client.post("/api/capabilities", params=query, content=content,
        headers={**routes.headers(lane), "Content-Type": "application/json"})
    assert_bad_params(response)
    assert not lane.a.accepted


def test_post_accepts_body_at_exact_byte_limit(lane):
    query, _ = request_parts(lane)
    content = EMPTY_BODY
    content += b" " * (262144 - len(content))
    response = lane.client.post("/api/capabilities", params=query, content=content,
        headers={**routes.headers(lane), "Content-Type": "application/json"})
    expected = lane.client.get("/api/capabilities",
        params={**query, "project_runs": "{}"}, headers=routes.headers(lane))
    assert response.status_code == expected.status_code == 200
    assert response.json() == expected.json()


def test_post_accepts_large_panel_groups_without_url_payload(lane, monkeypatch):
    params = {"expected_rev": 3, "groups": [{"name": "Roof", "panel_refs": [
        "leaf:panel:" + str(UUID(int=index + 1)) for index in range(2400)],
        "alignment_tolerance": 0.5, "module_width_along_row": 77,
        "module_height_across_row": 38.5}]}
    query, entries = request_parts(lane, params, "solar-panel-groups")
    content = json.dumps({"project_runs": entries}, indent=5).encode("utf-8")
    assert 190000 < len(content) < 220000
    calls = []
    def admit(tool, tenant, **kwargs):
        calls.append((tool, tenant, kwargs))
        return {"status_code": 200, "content": {"admissible": True}}
    monkeypatch.setattr(routes.policy, "project_tool_admission", admit)
    response = lane.client.post("/api/capabilities", params=query, content=content,
        headers={**routes.headers(lane, Authorization="Bearer preview"),
                 "Content-Type": "application/json"})
    assert "project_runs" not in response.request.url.params
    assert routes.states(response)["solar-panel-groups"]["runnable"]
    assert len(calls) == 1
    tool, tenant, forwarded = calls[0]
    assert tool == lane.tools["solar-panel-groups"] and tenant is lane.tenant
    assert forwarded == {"org_header": str(lane.s.org), "project_header": str(lane.s.project),
        "authorization": "Bearer preview", "input_version_id": query["input_version_id"],
        "drawing_id": query["drawing_id"], "params": entries["solar-panel-groups"]["params"],
        "catalog_digest": entries["solar-panel-groups"]["catalog_digest"],
        "idempotency_key": "key", "checkout_capability": "cap", "preview": True}
    assert not lane.a.accepted


@pytest.mark.parametrize("changes", [
    {"drawing_version": "2"},
    {"project_id": str(UUID(int=999))},
    {"input_version_id": ""},
], ids=["legacy-drawing-version", "project-mismatch", "missing-input-version"])
def test_post_preserves_get_refusals(lane, changes):
    query, entries = request_parts(lane)
    query.update(changes)
    expected = lane.client.get("/api/capabilities",
        params={**query, "project_runs": json.dumps(entries)}, headers=routes.headers(lane))
    response = lane.client.post("/api/capabilities", params=query,
        json={"project_runs": entries}, headers=routes.headers(lane))
    assert response.status_code == expected.status_code
    assert response.json() == expected.json()
    if "input_version_id" in changes:
        state = routes.states(response)["solar-settings"]
        assert not state["runnable"] and state["admission"]["status_code"] == 400
    else:
        assert response.status_code == 400


def test_post_builds_catalog_outside_event_loop(lane, monkeypatch):
    original = catalog_route.catalog.build_catalog
    calls = []
    def build(*args, **kwargs):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(catalog_route.catalog, "build_catalog", build)
    query, entries = request_parts(lane)
    response = lane.client.post("/api/capabilities", params=query,
        json={"project_runs": entries}, headers=routes.headers(lane))
    assert response.status_code == 200
    assert calls == [True]


def test_post_stops_consuming_stream_at_limit(lane):
    chunks = [b" " * 262144, b"x"]
    received = []
    async def receive():
        # A third read fails: the handler must stop at byte 262145 even when
        # Content-Length is absent and the sender has more chunks to send.
        assert len(received) < len(chunks)
        chunk = chunks[len(received)]
        received.append(chunk)
        return {"type": "http.request", "body": chunk, "more_body": True}
    request = Request({"type": "http"}, receive)
    response = asyncio.run(catalog_route.submit_capabilities(request, tenant=lane.tenant))
    assert response.status_code == 400
    assert json.loads(response.body) == envelopes.err_envelope(
        envelopes.ErrorCode.BAD_PARAMS, MESSAGE, False)
    assert len(received) == 2
