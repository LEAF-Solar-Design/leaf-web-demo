"""Finite O02 derivation transport through O01's existing browser custody."""
import json

import httpx
import pytest

from test_integration_browser import LOCAL, PROJECT, context

BASE = f"/api/v1/projects/{PROJECT}/integrations/dataset-derivations"
CAPABILITIES = BASE + "/capabilities"
REQUEST = {"schema_version": 1, "output": {"id": "fixture-counts", "version": 1, "title": "Counts"},
           "inputs": {"primary": {"id": "fixture-input", "version": 1, "digest": "a" * 64}},
           "plan": {"kind": "group_count", "group_by": [], "count": {"as": "count", "label": "Count"},
                    "windows": [{"id": "all", "filters": []}]}}
RESULT = {"reference": {"id": "fixture-counts", "version": 1, "digest": "b" * 64},
          "revision": 1, "rowCount": 1, "status": "complete"}
CAPABILITY_RESULT = {"schema_version": 1, "contract": "dataset-derivation-v1",
                     "operations": ["group_count", "window_count"], "max_inputs": 2,
                     "max_records_per_input": 100000, "max_windows": 4}


def write_headers(context):
    return context["headers"]() + [("Origin", LOCAL), ("Content-Type", "application/json")]


@pytest.mark.parametrize("method,path", [("GET", CAPABILITIES), ("POST", BASE)])
def test_only_exact_derivation_mapping_reaches_canonical_host(context, method, path):
    expected = CAPABILITY_RESULT if method == "GET" else RESULT
    context["respond"] = lambda _: httpx.Response(200, json=expected)
    body = json.dumps(REQUEST).encode() if method == "POST" else None
    response = context["request"](method, path, write_headers(context), body)
    assert response.status_code == 200
    assert response.json() == expected
    [sent] = context["calls"]
    assert sent.url.path == path and not sent.url.query
    assert sent.method == method
    assert sent.content == (body or b"")
    assert "cookie" not in sent.headers


@pytest.mark.parametrize("path", [CAPABILITIES + "?extra=1", BASE + "?scope=foreign"])
def test_derivation_has_no_query_grammar(context, path):
    method = "GET" if "/capabilities" in path else "POST"
    response = context["request"](method, path, write_headers(context),
                                   json.dumps(REQUEST).encode() if method == "POST" else None)
    assert response.status_code == 400
    assert not context["calls"]


@pytest.mark.parametrize("method,path,status", [
    ("POST", CAPABILITIES, 405), ("GET", BASE, 405), ("PUT", BASE, 405),
    ("GET", BASE + "/arbitrary", 404),
])
def test_no_generic_derivation_proxy_or_extra_method(context, method, path, status):
    response = context["request"](method, path, write_headers(context), b"{}" if method != "GET" else None)
    assert response.status_code == status
    assert not context["calls"]


@pytest.mark.parametrize("extra,declared", [(0, None), (1, None), (1, "1")])
def test_derivation_body_cap_checks_both_declared_and_streamed_bytes(context, extra, declared):
    body = json.dumps(REQUEST).encode()
    body += b" " * (163840 - len(body) + extra)
    headers = write_headers(context)
    if declared is not None:
        headers.append(("Content-Length", declared))
    context["respond"] = lambda _: httpx.Response(200, json=RESULT)
    response = context["request"]("POST", BASE, headers, body)
    assert response.status_code == (413 if extra else 200)
    assert len(context["calls"]) == (0 if extra else 1)


@pytest.mark.parametrize("method,path", [("GET", CAPABILITIES), ("POST", BASE)])
def test_derivation_cannot_bypass_joint_proof(context, method, path):
    response = context["request"](method, path, [], b"{}" if method == "POST" else None)
    assert response.status_code in {401, 403}
    assert not context["calls"]


def test_derivation_write_still_requires_local_origin(context):
    response = context["request"]("POST", BASE,
        context["headers"]() + [("Content-Type", "application/json")], json.dumps(REQUEST).encode())
    assert response.status_code == 403
    assert not context["calls"]


def test_canonical_role_refusal_is_preserved_without_write_retry(context):
    context["respond"] = lambda _: httpx.Response(403,
        json={"error": {"code": "WORKSPACE_SCOPE_DENIED", "message": "private details"}})
    response = context["request"]("POST", BASE, write_headers(context), json.dumps(REQUEST).encode())
    assert response.status_code == 403
    assert len(context["calls"]) == 1
    assert "private details" not in response.text
