"""Slice 12a: GET /api/receipts reads receipts that exist and fabricates none.

The load-bearing properties:

  1. NO FABRICATED ROWS. Every row this endpoint emits traces to a fixture
     artifact, a fixture reconciler document, or a fixture receipt.json. With no
     source configured the answer is an EMPTY row list plus an honest
     `source_unavailable` entry naming the missing environment variable -- never
     a placeholder row and never a silent empty.
  2. NO CREDENTIAL LEAK. The token never appears in a URL, a response body, or
     an unavailable detail. Asserted against the whole serialized response.
  3. NO UNAUTHENTICATED FALLBACK. With the token unset the reader must not make
     the HTTP call at all.
  4. BOUNDED. Oversize bodies are refused rather than truncated; row and field
     caps hold; the reconciler read is cached for 60 s.
  5. FAIL CLOSED on a malformed scope: 422, never a best-effort read.
  6. TENANT SCOPED: a `job:` scope for another tenant's job answers 404, the
     same no-existence-leak answer an unknown job gets.

Run:
    cd server
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_receipts_endpoint.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import receipts_read as rr  # noqa: E402

FAKE_TOKEN = "ghp-fixture-token-never-real"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (rr.ENV_REPO, rr.ENV_TOKEN, rr.ENV_API_ROOT,
                 rr.ENV_RECONCILER_URL, rr.ENV_JOB_RECEIPT_DIR):
        monkeypatch.delenv(name, raising=False)
    rr.reset_reconciler_cache()
    yield
    rr.reset_reconciler_cache()


def _artifact(name, *, sha="a" * 40, run_id=4242, created="2026-09-01T10:00:00Z", size=3072):
    return {
        "name": name,
        "size_in_bytes": size,
        "created_at": created,
        "expired": False,
        "archive_download_url": f"https://api.github.com/artifacts/{name}/zip",
        "workflow_run": {"id": run_id, "head_sha": sha, "head_branch": "main"},
    }


def _configure_github(monkeypatch):
    monkeypatch.setenv(rr.ENV_REPO, "LEAF-Solar-Design/leaf-web-demo")
    monkeypatch.setenv(rr.ENV_TOKEN, FAKE_TOKEN)


def _stub_get_json(monkeypatch, responses, calls=None):
    """Replace the ONE bounded HTTP primitive; assert no credential in the URL."""
    def fake(url, *, headers, cap):
        if calls is not None:
            calls.append((url, dict(headers), cap))
        assert FAKE_TOKEN not in url, "the token must never reach a URL"
        for match, payload in responses.items():
            if match in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise OSError("no fixture for this url")

    monkeypatch.setattr(rr, "_get_json", fake)


# --------------------------------------------------------------------------- #
# 1. scope parsing fails closed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("scope", [
    "pr:988", "tree:" + "a" * 40, "job:job-1", "train",
])
def test_valid_scopes_parse(scope):
    kind, _ = rr.parse_scope(scope)
    assert kind in {"pr", "tree", "job", "train"}


@pytest.mark.parametrize("bad", [
    "", "pr:", "pr:0", "pr:-1", "pr:abc", "tree:", "tree:zzz", "tree:" + "a" * 65,
    "job:", "job:../etc", "job:a/b", "trains", "PR:1", None, 7, ["pr:1"],
    "pr:1" + chr(0), "x" * 300,
])
def test_malformed_scopes_fail_closed(bad):
    with pytest.raises(rr.ReceiptsError):
        rr.parse_scope(bad)


# --------------------------------------------------------------------------- #
# 2. every artifact kind, from a fixture
# --------------------------------------------------------------------------- #
def test_prewarm_relay_receipt_row_from_a_fixture_artifact(monkeypatch):
    _configure_github(monkeypatch)
    name = "prewarm-relay-receipt-pr-988"
    _stub_get_json(monkeypatch, {"actions/artifacts": {"artifacts": [_artifact(name)]}})

    body = rr.read_receipts("pr:988")
    assert body["contract"] == rr.CONTRACT
    assert body["unavailable"] == []
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert set(row) == {"kind", "ref", "at", "sha", "summary", "url"}
    assert row["kind"] == "prewarm-relay"
    assert row["ref"] == "pr:988"
    assert row["sha"] == "a" * 40
    assert name in row["summary"]
    assert row["url"].endswith("/actions/runs/4242")


def test_tree_scope_reads_both_the_gate_proof_and_the_supply_set(monkeypatch):
    _configure_github(monkeypatch)
    tree = "b" * 40
    _stub_get_json(monkeypatch, {
        f"gate-proof-{tree}": {"artifacts": [
            _artifact(f"gate-proof-{tree}", created="2026-08-30T08:00:00Z")]},
        f"spec-v3-supply-set-{tree}": {"artifacts": [
            _artifact(f"spec-v3-supply-set-{tree}", created="2026-08-30T09:00:00Z")]},
    })
    body = rr.read_receipts(f"tree:{tree}")
    kinds = [row["kind"] for row in body["rows"]]
    assert kinds == ["supply-set", "gate-proof"], "newest first"
    assert body["unavailable"] == []


def test_an_absent_artifact_yields_no_row_and_no_fabrication(monkeypatch):
    _configure_github(monkeypatch)
    _stub_get_json(monkeypatch, {"actions/artifacts": {"artifacts": []}})
    body = rr.read_receipts("pr:1")
    assert body["rows"] == []
    assert body["unavailable"] == []


def test_reconciler_latest_json_row(monkeypatch):
    monkeypatch.setenv(rr.ENV_RECONCILER_URL,
                       "https://raw.githubusercontent.com/o/leaf-plan/main/"
                       "receipt-inbox/product-progress/latest.json")
    _stub_get_json(monkeypatch, {"latest.json": {
        "receipts": [
            {"at": "2026-09-02T12:00:00Z", "sha": "c" * 40,
             "summary": "studio-standardization reconciled", "url": "https://example.com/r"},
        ]
    }})
    body = rr.read_receipts("train")
    assert body["unavailable"] == []
    assert body["rows"][0]["kind"] == "reconciler"
    assert body["rows"][0]["summary"] == "studio-standardization reconciled"


def test_job_receipt_json_beside_a_job(monkeypatch, tmp_path):
    monkeypatch.setenv(rr.ENV_JOB_RECEIPT_DIR, str(tmp_path))
    job_dir = tmp_path / "job-77"
    job_dir.mkdir()
    (job_dir / "receipt.json").write_text(json.dumps({
        "at": "2026-09-03T01:02:03Z", "sha": "d" * 40, "summary": "count-by-layer finished",
        "url": "https://example.com/job/77",
    }), encoding="utf-8")

    body = rr.read_receipts("job:job-77")
    assert body["unavailable"] == []
    assert body["rows"] == [{
        "kind": "job", "ref": "job:job-77", "at": "2026-09-03T01:02:03Z",
        "sha": "d" * 40, "summary": "count-by-layer finished",
        "url": "https://example.com/job/77",
    }]


def test_a_job_with_no_receipt_yet_is_an_empty_answer_not_an_error(monkeypatch, tmp_path):
    """Slice 11 writes these; until it does, absence is the normal answer."""
    monkeypatch.setenv(rr.ENV_JOB_RECEIPT_DIR, str(tmp_path))
    body = rr.read_receipts("job:job-77")
    assert body["rows"] == []
    assert body["unavailable"] == []


def test_a_malformed_job_receipt_is_skipped_with_an_honest_reason(monkeypatch, tmp_path):
    monkeypatch.setenv(rr.ENV_JOB_RECEIPT_DIR, str(tmp_path))
    job_dir = tmp_path / "job-77"
    job_dir.mkdir()
    (job_dir / "receipt.json").write_text("{not json", encoding="utf-8")
    body = rr.read_receipts("job:job-77")
    assert body["rows"] == []
    assert body["unavailable"][0]["reason"] == rr.REASON_UNREADABLE


# --------------------------------------------------------------------------- #
# 3. no credential -> honest empty, and NO http call at all
# --------------------------------------------------------------------------- #
def test_missing_token_returns_an_honest_empty_list_and_makes_no_call(monkeypatch):
    monkeypatch.setenv(rr.ENV_REPO, "LEAF-Solar-Design/leaf-web-demo")
    called = []

    def must_not_run(*args, **kwargs):
        called.append(args)
        raise AssertionError("an unauthenticated GitHub call must never be made")

    monkeypatch.setattr(rr, "_get_json", must_not_run)

    body = rr.read_receipts("pr:988")
    assert called == []
    assert body["rows"] == []
    assert len(body["unavailable"]) == 1
    entry = body["unavailable"][0]
    assert entry["source"] == "github-artifacts"
    assert entry["reason"] == rr.REASON_NO_CREDENTIAL
    # The refusal names the exact variable an operator must set.
    assert rr.ENV_TOKEN in entry["detail"]


def test_missing_reconciler_url_names_its_environment_variable(monkeypatch):
    body = rr.read_receipts("train")
    assert body["rows"] == []
    assert body["unavailable"][0]["reason"] == rr.REASON_NO_CREDENTIAL
    assert rr.ENV_RECONCILER_URL in body["unavailable"][0]["detail"]


def test_a_non_https_reconciler_url_is_refused(monkeypatch):
    monkeypatch.setenv(rr.ENV_RECONCILER_URL, "http://raw.example.com/latest.json")
    called = []
    monkeypatch.setattr(rr, "_get_json", lambda *a, **k: called.append(a))
    body = rr.read_receipts("train")
    assert called == []
    assert body["rows"] == []


@pytest.mark.parametrize("slug,token", [
    ("not-a-slug", FAKE_TOKEN),
    ("a/b", "token with spaces"),
    ("a/b", ""),
    ("", FAKE_TOKEN),
])
def test_an_unusable_credential_is_source_unavailable_not_a_call(monkeypatch, slug, token):
    monkeypatch.setenv(rr.ENV_REPO, slug)
    monkeypatch.setenv(rr.ENV_TOKEN, token)
    monkeypatch.setattr(rr, "_get_json", lambda *a, **k: pytest.fail("must not call"))
    assert rr.github_credentials() == rr.REASON_NO_CREDENTIAL
    body = rr.read_receipts("pr:1")
    assert body["rows"] == []
    assert body["unavailable"][0]["reason"] == rr.REASON_NO_CREDENTIAL


# --------------------------------------------------------------------------- #
# 4. the token never reaches a response, a URL, or a detail
# --------------------------------------------------------------------------- #
def test_the_token_travels_only_in_the_authorization_header(monkeypatch):
    _configure_github(monkeypatch)
    calls = []
    _stub_get_json(monkeypatch, {"actions/artifacts": {"artifacts": [
        _artifact("prewarm-relay-receipt-pr-5")]}}, calls=calls)

    body = rr.read_receipts("pr:5")
    assert len(calls) == 1
    url, headers, cap = calls[0]
    assert FAKE_TOKEN not in url
    assert headers["Authorization"] == f"Bearer {FAKE_TOKEN}"
    assert cap == rr.MAX_ARTIFACT_BYTES
    assert FAKE_TOKEN not in json.dumps(body)


def test_an_unreachable_api_reports_a_reason_without_quoting_the_request(monkeypatch):
    _configure_github(monkeypatch)
    _stub_get_json(monkeypatch, {"actions/artifacts": OSError(
        f"HTTP 401 for https://api.github.com/x?token={FAKE_TOKEN}")})
    body = rr.read_receipts("pr:5")
    assert body["rows"] == []
    assert body["unavailable"][0]["reason"] == rr.REASON_UNREACHABLE
    assert FAKE_TOKEN not in json.dumps(body)


# --------------------------------------------------------------------------- #
# 5. bounds
# --------------------------------------------------------------------------- #
def test_rows_are_capped(monkeypatch):
    _configure_github(monkeypatch)
    many = [_artifact("prewarm-relay-receipt-pr-9", created=f"2026-09-01T10:{i:02d}:00Z")
            for i in range(rr.MAX_ROWS + 40)]
    _stub_get_json(monkeypatch, {"actions/artifacts": {"artifacts": many}})
    body = rr.read_receipts("pr:9")
    assert len(body["rows"]) <= rr.MAX_ROWS


def test_a_field_is_truncated_not_echoed_whole(monkeypatch):
    _configure_github(monkeypatch)
    art = _artifact("x" * 5000)
    _stub_get_json(monkeypatch, {"actions/artifacts": {"artifacts": [art]}})
    body = rr.read_receipts("pr:9")
    assert all(len(value) <= rr.MAX_FIELD for value in body["rows"][0].values()), \
        "every rendered field is bounded, including the composed summary"


def test_an_oversize_body_is_refused_rather_than_truncated(monkeypatch):
    """The cap is enforced by reading cap+1, so an oversize body is DETECTED."""
    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def read(self, n):
            return self._payload[:n]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        rr.urllib.request, "urlopen",
        lambda request, timeout=None: FakeResponse(b"[" + b"0," * 400000 + b"0]"),
    )
    with pytest.raises(ValueError):
        rr._get_json("https://example.com/x", headers={}, cap=1024)


def test_an_unexpected_artifact_shape_is_unreadable_not_a_crash(monkeypatch):
    _configure_github(monkeypatch)
    _stub_get_json(monkeypatch, {"actions/artifacts": {"artifacts": "nope"}})
    body = rr.read_receipts("pr:9")
    assert body["rows"] == []
    assert body["unavailable"][0]["reason"] == rr.REASON_UNREADABLE


def test_the_reconciler_read_is_cached_for_sixty_seconds(monkeypatch):
    monkeypatch.setenv(rr.ENV_RECONCILER_URL, "https://raw.example.com/latest.json")
    hits = []

    def fake(url, *, headers, cap):
        hits.append(url)
        return {"receipts": [{"at": "2026-09-02T12:00:00Z", "summary": "one"}]}

    monkeypatch.setattr(rr, "_get_json", fake)
    assert rr.RECONCILER_CACHE_SECONDS == 60.0
    rr.read_receipts("train")
    rr.read_receipts("train")
    rr.read_receipts("train")
    assert len(hits) == 1, "the 60 s cache must not re-read on every request"


# --------------------------------------------------------------------------- #
# 6. the endpoint
# --------------------------------------------------------------------------- #
def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from envelopes import install_error_handlers
    import routers.change_to_live as router_module

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router_module.router)
    return TestClient(app, raise_server_exceptions=False)


def test_endpoint_returns_rows_and_the_envelope(monkeypatch):
    _configure_github(monkeypatch)
    _stub_get_json(monkeypatch, {"actions/artifacts": {"artifacts": [
        _artifact("prewarm-relay-receipt-pr-988")]}})
    resp = _client().get("/api/receipts", params={"scope": "pr:988"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["contract"] == rr.CONTRACT
    assert body["scope"] == "pr:988"
    assert len(body["rows"]) == 1
    assert "degraded_mode" in body


def test_endpoint_is_honest_when_no_source_is_configured():
    resp = _client().get("/api/receipts", params={"scope": "pr:988"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["rows"] == []
    assert body["unavailable"][0]["reason"] == rr.REASON_NO_CREDENTIAL


@pytest.mark.parametrize("bad", ["", "pr:0", "tree:zz", "job:a/b", "nonsense"])
def test_endpoint_fails_closed_on_a_malformed_scope(bad):
    assert _client().get("/api/receipts", params={"scope": bad}).status_code == 422


def test_endpoint_requires_a_scope():
    assert _client().get("/api/receipts").status_code == 422


def test_a_job_scope_for_another_tenants_job_is_404(monkeypatch, tmp_path):
    """The same no-existence-leak answer GET /api/jobs/{id} gives (F8)."""
    monkeypatch.setenv(rr.ENV_JOB_RECEIPT_DIR, str(tmp_path))
    job_dir = tmp_path / "job-77"
    job_dir.mkdir()
    (job_dir / "receipt.json").write_text(json.dumps({"summary": "leaked"}), encoding="utf-8")

    import routers.jobs as jobs_router
    monkeypatch.setattr(jobs_router, "_job_for_tenant", lambda job_id, tenant_id: None)

    resp = _client().get("/api/receipts", params={"scope": "job:job-77"})
    assert resp.status_code == 404
    assert "leaked" not in resp.text


def test_a_job_scope_for_the_calling_tenants_job_is_served(monkeypatch, tmp_path):
    monkeypatch.setenv(rr.ENV_JOB_RECEIPT_DIR, str(tmp_path))
    job_dir = tmp_path / "job-77"
    job_dir.mkdir()
    (job_dir / "receipt.json").write_text(
        json.dumps({"summary": "mine", "at": "2026-09-03T00:00:00Z"}), encoding="utf-8")

    import deps
    import routers.jobs as jobs_router
    tenant_id = deps.DEFAULT_TENANT
    monkeypatch.setattr(
        jobs_router, "_job_for_tenant",
        lambda job_id, tid: {"job_id": job_id, "tenant_id": tid},
    )
    monkeypatch.setattr(jobs_router, "_bound_tenant_id", lambda tenant: str(tenant_id))

    resp = _client().get("/api/receipts", params={"scope": "job:job-77"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["rows"][0]["summary"] == "mine"
