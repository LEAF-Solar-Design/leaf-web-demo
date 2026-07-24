"""
Guest/account drawing uploads (§19): endpoint validation, guest minting,
rate caps, marker lifecycle, honest status, policy endpoint — all in-process
(TestClient), APS_LIVE=0, isolated stores per test.

THE test that matters most here is
test_upload_dxf_happy_path_serves_their_geometry: an uploaded drawing must
render THE USER'S coordinates and provably NOT the cached rooftop_demo intake
(the fabrication trap this whole lane exists to close).

Run:  cd server && python -m pytest tests/test_guest_uploads.py -q
"""
from __future__ import annotations

import io
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app as app_module
import deps
import guest_uploads
import platform_link
import write_loop
from routers import uploads as uploads_router

# A distinctive DXF nothing in the repo shares coordinates with. Rooftop demo
# coordinates live around 14000-15500; these are nowhere near.
DXF_BYTES = (
    "0\nSECTION\n2\nENTITIES\n"
    "0\nLWPOLYLINE\n5\nABCD\n8\nPanels\n70\n1\n"
    "10\n111.25\n20\n222.5\n10\n333.75\n20\n444.0\n10\n555.5\n20\n666.25\n"
    "0\nENDSEC\n0\nEOF\n"
).encode("utf-8")
DISTINCTIVE_COORD = 111.25
ROOFTOP_COORD = 14323.816  # first polyline x of data/rooftop_demo.intake.json


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_GUEST_STORE_DIR", str(tmp_path / "guest"))
    monkeypatch.setenv("LEAF_UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.delenv("LEAF_GUEST_RETENTION_HOURS", raising=False)
    guest_uploads._reset_rate_state()
    # Deterministic tests: run extraction inline instead of a thread.
    monkeypatch.setattr(
        guest_uploads, "start_extraction_thread",
        lambda tenant_id, drawing_id, ext: guest_uploads.run_extraction(
            tenant_id, drawing_id, ext))
    return TestClient(app_module.app)


def _upload(client, data=DXF_BYTES, name="mine.dxf", headers=None):
    return client.post("/api/drawings/upload",
                       files={"file": (name, io.BytesIO(data))},
                       headers=headers or {})


def test_live_account_upload_uses_active_binding_not_stale_claims(monkeypatch):
    import auth
    import tenancy

    canonical = "f49766b5-1e5a-4e67-a10f-4e3a9b576266"
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    monkeypatch.setattr(
        auth, "verify_platform_token", lambda authorization: {"sub": "auth0|bound"})
    monkeypatch.setattr(
        auth, "extract_tenant_claims", lambda payload: {
            "tenant_id": "stale-claim-tenant",
            "org_id": "stale-claim-org",
            "tier": "demo",
        })
    monkeypatch.setattr(
        deps, "resolve_active_platform_tenant_authority",
        lambda subject: (canonical, "restricted"))
    monkeypatch.setattr(
        tenancy, "get_store", lambda: SimpleNamespace(
            resolve_workspace=lambda tenant_id: None))

    tenant, kind, minted = uploads_router._resolve_upload_identity(
        "forged-header-tenant", "Bearer verified", "stale-guest-session")
    assert str(tenant) == canonical
    assert tenant.org_id == canonical
    assert tenant.tier == "restricted"
    assert tenant.subject == "auth0|bound"
    assert kind == "account"
    assert minted is False


def test_active_binding_resolution_fails_closed_for_unbound_subject(monkeypatch):
    monkeypatch.setattr(
        platform_link, "platform_store", lambda: SimpleNamespace(
            resolve_active_identity_binding=lambda authority, subject: None))
    with pytest.raises(HTTPException) as exc:
        deps.resolve_active_platform_tenant_id("auth0|unbound")
    assert exc.value.status_code == 403


def test_active_binding_resolution_returns_server_owned_tenant(monkeypatch):
    binding = SimpleNamespace(platform_tenant_id="canonical-org")
    org = SimpleNamespace(tier="hosted_pro", status="active")
    monkeypatch.setattr(
        platform_link, "platform_store", lambda: SimpleNamespace(
            resolve_active_identity_binding=lambda authority, subject: binding,
            get_org=lambda org_id: org))
    assert deps.resolve_active_platform_tenant_id(
        "auth0|foreign-claim") == "canonical-org"
    assert deps.resolve_active_platform_tenant_authority(
        "auth0|foreign-claim") == ("canonical-org", "hosted_pro")


def test_upload_dxf_happy_path_serves_their_geometry(client):
    r = _upload(client)
    assert r.status_code == 202
    body = r.json()
    assert body["error"] is None
    assert body["tenant_kind"] == "guest"
    assert body["tenant_id"].startswith("guest-")
    assert body["drawing_id"].startswith("u-")
    assert body["retention_expires_at"]  # stamped for guests
    assert body["status"] == "extracting"

    tenant, did = body["tenant_id"], body["drawing_id"]
    s = client.get(f"/api/drawings/{did}/upload-status",
                   headers={"X-Tenant-Id": tenant})
    assert s.status_code == 200
    assert s.json()["status"] == "ready"

    i = client.get(f"/api/drawings/{did}/intake", headers={"X-Tenant-Id": tenant})
    assert i.status_code == 200
    intake = i.json()["intake"]
    coords = [c for p in intake["polylines"] for pt in p["pts"] for c in pt]
    assert DISTINCTIVE_COORD in coords, "their geometry must be served"
    assert ROOFTOP_COORD not in coords, "the cached demo intake must NEVER leak in"
    assert intake["layers"] == ["Panels"]
    assert intake["polylines"][0]["handle"] == "ABCD"
    assert intake["polylines"][0]["closed"] is True


def test_upload_account_tenant_no_retention(client):
    r = _upload(client, headers={"X-Tenant-Id": "acme-solar"})
    assert r.status_code == 202
    body = r.json()
    assert body["tenant_kind"] == "account"
    assert str(uuid.UUID(body["drawing_id"])) == body["drawing_id"]
    assert body["tenant_id"] == "acme-solar"
    assert body["retention_expires_at"] is None
    assert body["guest_session"] is None
    status = client.get(
        f"/api/drawings/{body['drawing_id']}/upload-status",
        headers={"X-Tenant-Id": "acme-solar"},
    ).json()
    assert status["status"] == "ready"
    assert status["extracted_version"] == 1


def test_guest_and_account_upload_id_mints_keep_distinct_syntax():
    guest_id = guest_uploads.new_upload_drawing_id()
    assert guest_id.startswith("u-")
    assert len(guest_id) == 12
    int(guest_id[2:], 16)

    account_id = guest_uploads.new_account_upload_drawing_id()
    assert str(uuid.UUID(account_id)) == account_id


def test_upload_oversize_413(client, monkeypatch):
    monkeypatch.setenv("LEAF_UPLOAD_MAX_BYTES", "64")
    r = _upload(client, data=b"0\nSECTION\n" + b"x" * 100 + b"\nENTITIES\n")
    assert r.status_code == 413
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"


def test_upload_bad_extension_400(client):
    r = _upload(client, name="mine.pdf")
    assert r.status_code == 400
    assert "dwg" in r.json()["error"]["message"].lower()


def test_upload_empty_400(client):
    r = _upload(client, data=b"")
    assert r.status_code == 400


def test_upload_fake_dwg_magic_400(client):
    r = _upload(client, data=b"this is not a dwg at all", name="mine.dwg")
    assert r.status_code == 400
    assert "AC1" in r.json()["error"]["message"]


def test_guest_rate_limit_per_ip(client, monkeypatch):
    monkeypatch.setenv("LEAF_GUEST_UPLOADS_PER_IP_PER_DAY", "2")
    assert _upload(client).status_code == 202
    assert _upload(client).status_code == 202
    r = _upload(client)
    assert r.status_code == 429
    assert r.json()["error"]["error_code"] == "quota_exceeded"
    # accounts are NOT rate-capped by the guest limiter
    assert _upload(client, headers={"X-Tenant-Id": "acme-solar"}).status_code == 202


def test_guest_rate_limit_global(client, monkeypatch):
    monkeypatch.setenv("LEAF_GUEST_UPLOADS_PER_DAY", "1")
    assert _upload(client).status_code == 202
    r = _upload(client)
    assert r.status_code == 429


def test_upload_status_unknown_404(client):
    r = client.get("/api/drawings/u-doesnotexist/upload-status",
                   headers={"X-Tenant-Id": "guest-nobody"})
    assert r.status_code == 404


def test_upload_status_timeout_becomes_failed(client, monkeypatch):
    r = _upload(client)
    tenant, did = r.json()["tenant_id"], r.json()["drawing_id"]
    backend = write_loop.backend_for_tenant(tenant)
    marker = guest_uploads.read_marker(backend, tenant, did)
    marker["status"] = "extracting"
    marker["uploaded_at"] = "2020-01-01T00:00:00+00:00"
    guest_uploads.write_marker(backend, tenant, did, marker)
    s = client.get(f"/api/drawings/{did}/upload-status",
                   headers={"X-Tenant-Id": tenant})
    assert s.json()["status"] == "failed"
    assert s.json()["error"]["error_code"] == "TIMEOUT"
    # persisted, not just computed: a second read agrees without recomputation
    marker2 = guest_uploads.read_marker(backend, tenant, did)
    assert marker2["status"] == "failed"


def test_dwg_at_aps_live_0_fails_honestly(client):
    r = _upload(client, data=b"AC1032rest-of-a-dwg-file", name="real.dwg")
    assert r.status_code == 202
    tenant, did = r.json()["tenant_id"], r.json()["drawing_id"]
    s = client.get(f"/api/drawings/{did}/upload-status",
                   headers={"X-Tenant-Id": tenant})
    assert s.json()["status"] == "failed"
    assert s.json()["error"]["error_code"] == "APS_UNAVAILABLE"
    # and the intake read fails CLOSED — no demo bootstrap
    i = client.get(f"/api/drawings/{did}/intake", headers={"X-Tenant-Id": tenant})
    assert i.status_code == 404


def test_marker_records_content_identity(client):
    r = _upload(client)
    tenant, did = r.json()["tenant_id"], r.json()["drawing_id"]
    backend = write_loop.backend_for_tenant(tenant)
    marker = guest_uploads.read_marker(backend, tenant, did)
    assert marker["bytes"] == len(DXF_BYTES)
    assert marker["filename"] == "mine.dxf"
    import hashlib
    assert marker["content_sha256"] == hashlib.sha256(DXF_BYTES).hexdigest()
    intake_ref = write_loop.intake_cache_key(tenant, did, 1)
    assert marker["intake_ref"] == intake_ref
    assert marker["intake_sha256"] == hashlib.sha256(
        backend.get(intake_ref)).hexdigest()


def test_policy_endpoint_reads_the_one_constant(client, monkeypatch):
    monkeypatch.setenv("LEAF_GUEST_RETENTION_HOURS", "5.5")
    r = client.get("/api/site/guest-upload-policy")
    assert r.status_code == 200
    body = r.json()
    assert body["retention_hours"] == 5.5
    assert body["accepted"] == [".dwg", ".dxf"]
    assert body["enabled"] is True
    assert body["extract_live"] is False


def test_disabled_flag_503_and_policy_reports_it(client, monkeypatch):
    monkeypatch.setenv("LEAF_GUEST_UPLOADS_ENABLED", "0")
    assert _upload(client).status_code == 503
    assert client.get("/api/site/guest-upload-policy").json()["enabled"] is False


def test_purge_daemon_starts_even_when_uploads_disabled(monkeypatch):
    """Round-1 MAJOR: disabling NEW uploads never strands already-stamped
    retention promises — the daemon runs regardless of the enable flag."""
    monkeypatch.setenv("LEAF_GUEST_UPLOADS_ENABLED", "0")
    thread = guest_uploads.start_purge_daemon()
    assert thread is not None and thread.is_alive()


def test_invalid_upload_does_not_consume_guest_quota(client, monkeypatch):
    """Round-1 MAJOR: garbage requests must not drain the shared daily pool —
    quota is counted only after validation passes."""
    monkeypatch.setenv("LEAF_GUEST_UPLOADS_PER_IP_PER_DAY", "1")
    monkeypatch.setenv("LEAF_GUEST_UPLOADS_PER_DAY", "1")
    assert _upload(client, name="junk.pdf").status_code == 400
    assert _upload(client, data=b"not a dwg", name="junk.dwg").status_code == 400
    # the one real slot is still available
    assert _upload(client).status_code == 202


def test_oversize_request_precheck_rejects_declared_length(client, monkeypatch):
    monkeypatch.setenv("LEAF_UPLOAD_MAX_BYTES", "64")
    big = b"0\nSECTION\n" + b"x" * 200_000
    r = _upload(client, data=big)
    assert r.status_code == 413
    assert "upload cap" in r.json()["error"]["message"]


def test_store_representation_dxf_intake_blob_plus_cache(client):
    """Rounds 1+2: a .dxf upload's v1 blob is the intake JSON (the demo
    drawing's own mock representation — raw DXF bytes under the store's
    immutable `.dwg` version key would be a mislabeled HostDwg on the first
    live write), and the parsed intake also sits at the cache sibling
    write_loop.read_intake prefers."""
    import store
    r = _upload(client)
    tenant, did = r.json()["tenant_id"], r.json()["drawing_id"]
    backend = write_loop.backend_for_tenant(tenant)
    vkey = store.drawing_version_key(tenant, did, 1)
    blob = json.loads(backend.get(vkey).decode("utf-8"))
    assert blob["layers"] == ["Panels"], "dxf v1 blob must be the intake JSON"
    ckey = write_loop.intake_cache_key(tenant, did, 1)
    assert backend.exists(ckey), "parsed intake must sit at the cache sibling"
    assert json.loads(backend.get(ckey).decode("utf-8"))["layers"] == ["Panels"]


def test_store_representation_dwg_raw_bytes_v1(client, monkeypatch):
    """A .dwg upload's v1 blob is the user's RAW DWG bytes — what a later
    live write signs and sends to APS as HostDwg. Extraction is mocked at
    the broker seam (the live path); the ingest branch under test is real."""
    import deps
    import store
    dwg_bytes = b"AC1032" + b"\x00" * 64
    fake_intake = {"dwg": "real.dwg", "layers": ["L1"],
                   "polylines": [{"layer": "L1", "closed": True,
                                  "pts": [[1, 2, 0], [3, 4, 0]],
                                  "xdata": None, "handle": "AA"}]}
    monkeypatch.setattr(deps, "APS_LIVE", True)
    monkeypatch.setattr(guest_uploads, "_extract_via_broker",
                        lambda tenant_id, drawing_id: fake_intake)
    r = _upload(client, data=dwg_bytes, name="real.dwg")
    assert r.status_code == 202
    tenant, did = r.json()["tenant_id"], r.json()["drawing_id"]
    backend = write_loop.backend_for_tenant(tenant)
    assert backend.get(store.drawing_version_key(tenant, did, 1)) == dwg_bytes
    cached = json.loads(backend.get(
        write_loop.intake_cache_key(tenant, did, 1)).decode("utf-8"))
    assert cached == fake_intake
    # and the intake read serves the CACHE (their geometry), not the raw blob
    i = client.get(f"/api/drawings/{did}/intake", headers={"X-Tenant-Id": tenant})
    assert i.status_code == 200
    assert i.json()["intake"] == fake_intake


def test_chunked_oversized_body_hits_the_asgi_wall(client, monkeypatch):
    """Round-2 MAJOR: a LENGTH-LESS (chunked) oversized body must be stopped
    by the byte-counting ASGI middleware before multipart parsing can spool
    it to disk — no Content-Length header to pre-check."""
    monkeypatch.setenv("LEAF_UPLOAD_MAX_BYTES", "1024")
    boundary = "x-test-boundary"

    def chunks():
        yield (f"--{boundary}\r\ncontent-disposition: form-data; "
               f"name=\"file\"; filename=\"big.dxf\"\r\n\r\n").encode()
        for _ in range(2100):  # ~2.1 MB, far past cap + slack
            yield b"y" * 1024
        yield f"\r\n--{boundary}--\r\n".encode()

    r = client.post("/api/drawings/upload", content=chunks(),
                    headers={"content-type": f"multipart/form-data; boundary={boundary}"})
    # The abort raised by the counting receive() lands inside starlette's
    # multipart parser, whose broad except answers 400; a clean 413 comes
    # only from the declared-length path. EITHER status proves the wall —
    # the bounded spool is the security property, so also prove no drawing
    # was ever staged or marked.
    assert r.status_code in (400, 413)
    assert not any(guest_uploads.uploads_dir().glob("*")), \
        "nothing may reach staging past the byte wall"


def test_compose_stack_shares_the_uploads_staging_volume():
    """Round-2 BLOCKER guard: the app stages uploads and the broker extracts
    them — in the compose stack that only works through a SHARED volume with
    a matching LEAF_UPLOADS_DIR in BOTH services. String-level lockstep check
    so the wiring cannot silently drift out of the compose file."""
    compose = (Path(__file__).resolve().parent.parent.parent
               / "docker-compose.yml").read_text(encoding="utf-8")
    assert compose.count("LEAF_UPLOADS_DIR: /data/uploads") == 2, \
        "both app and broker must point at the shared staging dir"
    assert compose.count("- leaf-uploads:/data/uploads") == 2, \
        "both app and broker must mount the shared uploads volume"
    assert "\n  leaf-uploads:" in compose, "the named volume must be declared"


def test_session_route_serves_uploaded_drawing_not_demo(client):
    """Round-1 BLOCKER (session half): offline GET /api/session?dwg=<upload>
    must serve THEIR intake (or 404), never the cached rooftop demo."""
    r = _upload(client)
    tenant, did = r.json()["tenant_id"], r.json()["drawing_id"]
    s = client.get(f"/api/session?dwg={did}", headers={"X-Tenant-Id": tenant})
    assert s.status_code == 200
    coords = [c for p in s.json()["intake"]["polylines"] for pt in p["pts"] for c in pt]
    assert DISTINCTIVE_COORD in coords
    assert ROOFTOP_COORD not in coords
    # unknown drawing under a guest tenant: honest 404, no bootstrap
    missing = client.get("/api/session?dwg=u-missing", headers={"X-Tenant-Id": tenant})
    assert missing.status_code == 404
    # regression: the default dwg still serves the cached demo intake
    default = client.get("/api/session", headers={"X-Tenant-Id": "acme-solar"})
    assert default.status_code == 200
    default_coords = [c for p in default.json()["intake"]["polylines"] for pt in p["pts"] for c in pt]
    assert ROOFTOP_COORD in default_coords


def test_guest_same_bytes_reupload_returns_same_receipt_and_quota_once(client, monkeypatch):
    """§19 idempotent guest uploads (FE round-3 MAJOR, receipt recovery): the
    guest drawing id is content-derived, so re-posting the SAME bytes returns
    the SAME drawing — and does NOT consume quota again. Proven with a 1/day
    IP cap: the identical re-upload still succeeds; different bytes then 429."""
    monkeypatch.setenv("LEAF_GUEST_UPLOADS_PER_IP_PER_DAY", "1")
    guest_uploads._reset_rate_state()

    first = _upload(client)
    assert first.status_code == 202
    tenant, did = first.json()["tenant_id"], first.json()["drawing_id"]

    again = _upload(client, headers={"X-Tenant-Id": tenant})
    assert again.status_code == 202, again.text
    assert again.json()["drawing_id"] == did          # same drawing, not a copy
    assert again.json()["tenant_id"] == tenant
    assert again.json()["status"] == "ready"          # echoes the CURRENT state

    other = DXF_BYTES.replace(b"111.25", b"999.99")
    blocked = _upload(client, data=other, headers={"X-Tenant-Id": tenant})
    assert blocked.status_code == 429                 # cap was 1: dedupe was free

    # Same bytes as a DIFFERENT (fresh) guest still mints its own drawing.
    fresh = _upload(client)
    assert fresh.status_code == 429                   # ...but the cap stops it here


def test_guest_failed_upload_retry_replaces_failure(client, monkeypatch):
    """A terminally FAILED attempt is not deduped: re-uploading the same
    bytes reuses the derived id and replaces the failure with a fresh
    extraction. The failure is produced the real way (extraction raises, so
    no manifest exists) — that is the shape the retry path keys on."""
    import dxf_intake
    real_parse = dxf_intake.parse_dxf_file
    calls = {"n": 0}

    def flaky_parse(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise guest_uploads._ExtractError("INTERNAL", "transient boom",
                                              retryable=True)
        return real_parse(*args, **kwargs)

    monkeypatch.setattr(dxf_intake, "parse_dxf_file", flaky_parse)

    first = _upload(client)
    assert first.status_code == 202
    tenant, did = first.json()["tenant_id"], first.json()["drawing_id"]
    backend = write_loop.backend_for_tenant(tenant, aps_live=False, da=None)
    assert guest_uploads.read_marker(backend, tenant, did)["status"] == "failed"

    retry = _upload(client, headers={"X-Tenant-Id": tenant})
    assert retry.status_code == 202
    assert retry.json()["drawing_id"] == did
    assert guest_uploads.read_marker(backend, tenant, did)["status"] == "ready"


def test_account_same_bytes_reupload_mints_new_drawing(client):
    """Account uploads keep random ids: two intentional uploads of one file
    stay two independent drawings (dedupe is a GUEST posture only)."""
    a = _upload(client, headers={"X-Tenant-Id": "acme-solar"})
    b = _upload(client, headers={"X-Tenant-Id": "acme-solar"})
    assert a.status_code == 202 and b.status_code == 202
    assert a.json()["drawing_id"] != b.json()["drawing_id"]


def test_guest_failed_retry_wipes_partial_ingest_residue(client, monkeypatch):
    """Round-5 MAJOR: a failed ingest can leave partial residue (a v1 blob
    without a manifest would wedge the derived id in ingest's immutable-
    version refusal forever). The retry must WIPE the drawing dir and start
    clean under the same derived id."""
    import store
    import dxf_intake
    real_parse = dxf_intake.parse_dxf_file
    calls = {"n": 0}

    def flaky_parse(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise guest_uploads._ExtractError("INTERNAL", "boom", retryable=True)
        return real_parse(*args, **kwargs)

    monkeypatch.setattr(dxf_intake, "parse_dxf_file", flaky_parse)
    first = _upload(client)
    tenant, did = first.json()["tenant_id"], first.json()["drawing_id"]
    backend = write_loop.backend_for_tenant(tenant, aps_live=False, da=None)
    assert guest_uploads.read_marker(backend, tenant, did)["status"] == "failed"

    # Plant the worst residue shape: a v1 version blob with NO manifest.
    backend.put(store.drawing_version_key(tenant, did, 1), b"partial-ingest-residue")

    retry = _upload(client, headers={"X-Tenant-Id": tenant})
    assert retry.status_code == 202
    assert retry.json()["drawing_id"] == did
    marker = guest_uploads.read_marker(backend, tenant, did)
    assert marker["status"] == "ready", marker.get("error")
    intake = client.get(f"/api/drawings/{did}/intake",
                        headers={"X-Tenant-Id": tenant})
    assert intake.status_code == 200


def test_stale_worker_cannot_overwrite_replaced_attempt(client, monkeypatch):
    """Round-5 MAJOR: a timed-out attempt's worker thread that is STILL
    RUNNING when the retry replaces the marker must fence itself out — the
    locked tail re-checks the attempt token, not mere marker existence."""
    import dxf_intake
    real_parse = dxf_intake.parse_dxf_file
    state = {"old_worker_ran": False}

    def parse_with_midflight_replacement(*args, **kwargs):
        intake = real_parse(*args, **kwargs)
        if not state["old_worker_ran"]:
            state["old_worker_ran"] = True
            tenant, did = state["tenant"], state["did"]
            backend = write_loop.backend_for_tenant(tenant, aps_live=False, da=None)
            # The attempt times out (status_view's persistence, simulated
            # here with the marker this worker owns)...
            marker = guest_uploads.read_marker(backend, tenant, did)
            guest_uploads._mark_failed(backend, tenant, did, marker,
                                       "TIMEOUT", "budget exceeded", retryable=True)
            # ...and the user's retry replaces the attempt and completes
            # fully (inline extraction) before the old worker resumes.
            retry = _upload(client, headers={"X-Tenant-Id": tenant})
            assert retry.status_code == 202
            assert retry.json()["drawing_id"] == did
            state["new_attempt"] = guest_uploads.read_marker(
                backend, tenant, did)["attempt"]
        return intake

    # First upload: capture ids BEFORE extraction runs, so run the thread
    # manually instead of inline.
    monkeypatch.setattr(guest_uploads, "start_extraction_thread", lambda *a, **k: None)
    monkeypatch.setattr(dxf_intake, "parse_dxf_file", parse_with_midflight_replacement)
    first = _upload(client)
    tenant, did = first.json()["tenant_id"], first.json()["drawing_id"]
    state["tenant"], state["did"] = tenant, did
    # Re-enable inline extraction for the RETRY inside the wrapper.
    monkeypatch.setattr(
        guest_uploads, "start_extraction_thread",
        lambda t, d, e: guest_uploads.run_extraction(t, d, e))

    guest_uploads.run_extraction(tenant, did, ".dxf")  # the OLD worker

    backend = write_loop.backend_for_tenant(tenant, aps_live=False, da=None)
    final = guest_uploads.read_marker(backend, tenant, did)
    assert state["old_worker_ran"]
    assert final["attempt"] == state["new_attempt"], \
        "the old worker must not overwrite the replacement attempt"
    assert final["status"] == "ready"


def test_mark_failed_never_demotes_a_terminal_state(client):
    """Round-6 MAJOR: a committed ready must never be overwritten by a late
    failure — a twin worker's error or a stale status snapshot arrives with
    a matching attempt token, and the status guard alone must stop it."""
    first = _upload(client)
    tenant, did = first.json()["tenant_id"], first.json()["drawing_id"]
    backend = write_loop.backend_for_tenant(tenant, aps_live=False, da=None)
    ready = guest_uploads.read_marker(backend, tenant, did)
    assert ready["status"] == "ready"

    stale_snapshot = dict(ready, status="extracting")  # same attempt token
    wrote = guest_uploads._mark_failed(backend, tenant, did, stale_snapshot,
                                       "TIMEOUT", "late twin failure",
                                       retryable=True)
    assert wrote is False
    assert guest_uploads.read_marker(backend, tenant, did)["status"] == "ready"


def test_failed_retry_wipe_failure_is_honest_and_recoverable(client, monkeypatch):
    """Round-6 MAJOR: if the residue wipe cannot be verified, the retry must
    fail honestly (500, retryable) — never fall through to a random id or a
    wedged ingest — and the surviving failed marker must route a LATER retry
    straight back into the replace path."""
    import dxf_intake
    # Round-7 MAJOR scenario: a per-IP cap of exactly 2 proves the failed
    # wipe consumes NO slot — slot 1 is the failed extraction, slot 2 must
    # still be available for the successful recovery retry.
    monkeypatch.setenv("LEAF_GUEST_UPLOADS_PER_IP_PER_DAY", "2")
    guest_uploads._reset_rate_state()
    real_parse = dxf_intake.parse_dxf_file
    calls = {"n": 0}

    def flaky_parse(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise guest_uploads._ExtractError("INTERNAL", "boom", retryable=True)
        return real_parse(*args, **kwargs)

    monkeypatch.setattr(dxf_intake, "parse_dxf_file", flaky_parse)
    first = _upload(client)
    tenant, did = first.json()["tenant_id"], first.json()["drawing_id"]
    backend = write_loop.backend_for_tenant(tenant, aps_live=False, da=None)
    assert guest_uploads.read_marker(backend, tenant, did)["status"] == "failed"

    real_wipe = guest_uploads.wipe_failed_attempt_residue
    monkeypatch.setattr(guest_uploads, "wipe_failed_attempt_residue",
                        lambda *a: False)
    blocked = _upload(client, headers={"X-Tenant-Id": tenant})
    assert blocked.status_code == 500
    assert blocked.json()["error"]["retryable"] is True
    assert guest_uploads.read_marker(backend, tenant, did)["status"] == "failed"

    monkeypatch.setattr(guest_uploads, "wipe_failed_attempt_residue", real_wipe)
    monkeypatch.setattr(dxf_intake, "parse_dxf_file", real_parse)
    retry = _upload(client, headers={"X-Tenant-Id": tenant})
    assert retry.status_code == 202
    assert retry.json()["drawing_id"] == did
    assert guest_uploads.read_marker(backend, tenant, did)["status"] == "ready"


def test_timeout_view_lost_race_reports_one_coherent_snapshot(client, monkeypatch):
    """Round-7 MINOR: when the TIMEOUT persist loses to a replacement
    attempt, the status view must serve the NEW marker wholesale — never the
    new status spliced onto the stale snapshot's filename and times."""
    r = _upload(client)
    tenant, did = r.json()["tenant_id"], r.json()["drawing_id"]
    backend = write_loop.backend_for_tenant(tenant)
    stale = guest_uploads.read_marker(backend, tenant, did)
    stale["status"] = "extracting"
    stale["uploaded_at"] = "2020-01-01T00:00:00+00:00"
    guest_uploads.write_marker(backend, tenant, did, stale)

    replacement = dict(stale, attempt="ffffffffffffffff", status="ready",
                       filename="replacement.dxf",
                       uploaded_at="2026-01-01T00:00:00+00:00")
    real_mark = guest_uploads._mark_failed

    def swap_then_mark(backend_, tenant_, did_, marker_, *args, **kwargs):
        guest_uploads.write_marker(backend_, tenant_, did_, replacement)
        return real_mark(backend_, tenant_, did_, marker_, *args, **kwargs)

    monkeypatch.setattr(guest_uploads, "_mark_failed", swap_then_mark)
    view = guest_uploads.status_view(backend, tenant, did)
    assert view["status"] == "ready"
    assert view["filename"] == "replacement.dxf"
    assert view["uploaded_at"] == "2026-01-01T00:00:00+00:00"


def test_quota_rejected_retry_cannot_destroy_readable_data(client, monkeypatch):
    """Round-8 MAJOR: a failed attempt can leave a COMMITTED manifest that
    intake still serves (ingest succeeded, the cache write did not). A
    quota-rejected retry must NOT delete that readable data — the visible
    wipe runs only after the quota charge, as part of a paid replacement."""
    real_cache_key = write_loop.intake_cache_key

    def broken_cache_key(*args, **kwargs):
        raise ValueError("cache write breaks AFTER ingest committed the manifest")

    monkeypatch.setattr(write_loop, "intake_cache_key", broken_cache_key)
    first = _upload(client)
    assert first.status_code == 202
    tenant, did = first.json()["tenant_id"], first.json()["drawing_id"]
    backend = write_loop.backend_for_tenant(tenant, aps_live=False, da=None)
    assert guest_uploads.read_marker(backend, tenant, did)["status"] == "failed"
    import store
    assert backend.exists(store.manifest_key(tenant, did)), \
        "precondition: the failed attempt left a committed manifest"
    monkeypatch.setattr(write_loop, "intake_cache_key", real_cache_key)
    assert client.get(f"/api/drawings/{did}/intake",
                      headers={"X-Tenant-Id": tenant}).status_code == 200, \
        "precondition: the manifest is API-visible despite the failed marker"

    # Exhaust the quota, then retry: 429 — and the data must SURVIVE.
    monkeypatch.setenv("LEAF_GUEST_UPLOADS_PER_IP_PER_DAY", "1")
    blocked = _upload(client, headers={"X-Tenant-Id": tenant})
    assert blocked.status_code == 429
    assert backend.exists(store.manifest_key(tenant, did))
    assert client.get(f"/api/drawings/{did}/intake",
                      headers={"X-Tenant-Id": tenant}).status_code == 200

    # With quota available the paid replacement proceeds under the same id.
    monkeypatch.setenv("LEAF_GUEST_UPLOADS_PER_IP_PER_DAY", "10")
    retry = _upload(client, headers={"X-Tenant-Id": tenant})
    assert retry.status_code == 202
    assert retry.json()["drawing_id"] == did
    assert guest_uploads.read_marker(backend, tenant, did)["status"] == "ready"


def test_visible_wipe_failure_refunds_the_quota_charge(client, monkeypatch):
    """Round-8 companion: when the PAID visible-manifest wipe fails, the
    charge is refunded — proven with a cap of exactly 2: slot 1 is the
    failed extraction, the failed-wipe 500 must not hold slot 2."""
    real_cache_key = write_loop.intake_cache_key
    monkeypatch.setenv("LEAF_GUEST_UPLOADS_PER_IP_PER_DAY", "2")
    guest_uploads._reset_rate_state()
    monkeypatch.setattr(write_loop, "intake_cache_key",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    first = _upload(client)
    tenant, did = first.json()["tenant_id"], first.json()["drawing_id"]
    backend = write_loop.backend_for_tenant(tenant, aps_live=False, da=None)
    assert guest_uploads.read_marker(backend, tenant, did)["status"] == "failed"
    monkeypatch.setattr(write_loop, "intake_cache_key", real_cache_key)

    real_wipe = guest_uploads.wipe_failed_attempt_residue
    monkeypatch.setattr(guest_uploads, "wipe_failed_attempt_residue",
                        lambda *a: False)
    blocked = _upload(client, headers={"X-Tenant-Id": tenant})
    assert blocked.status_code == 500
    assert blocked.json()["error"]["retryable"] is True

    monkeypatch.setattr(guest_uploads, "wipe_failed_attempt_residue", real_wipe)
    retry = _upload(client, headers={"X-Tenant-Id": tenant})
    assert retry.status_code == 202, "the refunded slot must still be available"
    assert retry.json()["drawing_id"] == did
    assert guest_uploads.read_marker(backend, tenant, did)["status"] == "ready"


def test_raising_wipe_still_refunds_the_paid_charge(client, monkeypatch):
    """Round-9 MAJOR: the wipe's failure can be an OSError (e.g. iterdir on
    the residue root), not just a False return. The containment inside
    wipe_failed_attempt_residue must convert it to False so the paid-path
    refund still runs — cap-2 proof: the recovery retry gets the slot."""
    real_cache_key = write_loop.intake_cache_key
    monkeypatch.setenv("LEAF_GUEST_UPLOADS_PER_IP_PER_DAY", "2")
    guest_uploads._reset_rate_state()
    monkeypatch.setattr(write_loop, "intake_cache_key",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    first = _upload(client)
    tenant, did = first.json()["tenant_id"], first.json()["drawing_id"]
    backend = write_loop.backend_for_tenant(tenant, aps_live=False, da=None)
    assert guest_uploads.read_marker(backend, tenant, did)["status"] == "failed"
    monkeypatch.setattr(write_loop, "intake_cache_key", real_cache_key)

    class _BoomDir:
        def is_dir(self):
            return True

        def iterdir(self):
            raise OSError("residue enumeration failed")

    real_dir = guest_uploads.guest_drawing_dir
    monkeypatch.setattr(guest_uploads, "guest_drawing_dir",
                        lambda *a: _BoomDir())
    blocked = _upload(client, headers={"X-Tenant-Id": tenant})
    assert blocked.status_code == 500
    assert blocked.json()["error"]["retryable"] is True

    monkeypatch.setattr(guest_uploads, "guest_drawing_dir", real_dir)
    retry = _upload(client, headers={"X-Tenant-Id": tenant})
    assert retry.status_code == 202, "the refunded slot must still be available"
    assert retry.json()["drawing_id"] == did
    assert guest_uploads.read_marker(backend, tenant, did)["status"] == "ready"
