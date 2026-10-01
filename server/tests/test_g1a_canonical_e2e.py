"""Production-shaped G1A walkthrough against PostgreSQL and the real AutoFill solver."""
from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"),
                                reason="G1A walkthrough requires PostgreSQL")


@pytest.fixture
def isolated_database(monkeypatch):
    """Run the walkthrough in its own throwaway database on the caller's server.

    Suites sharing the gate database must never see this test's org, job or
    identity binding, so the caller's database is never written: a fresh
    database is created, DATABASE_URL points the in-process store and the worker
    subprocesses at it, and it is dropped on teardown even when the test fails.
    """
    import psycopg
    from psycopg import sql

    caller_url = os.environ["DATABASE_URL"]
    name = f"g1a_e2e_{uuid.uuid4().hex[:12]}"
    parts = urlsplit(caller_url)
    isolated_url = urlunsplit(parts._replace(path=f"/{name}"))

    with psycopg.connect(caller_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        monkeypatch.setenv("DATABASE_URL", isolated_url)
        from app import app  # noqa: F401  (sets up the leaf_platform alias)
        from leaf_platform import db as platform_db
        platform_db.reset_pool()
        try:
            yield isolated_url
        finally:
            platform_db.reset_pool()
    finally:
        with psycopg.connect(caller_url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
                          .format(sql.Identifier(name)))


def test_production_shaped_authenticated_restart_safe_real_solve(monkeypatch, isolated_database):
    from test_wave5 import AUD, ISS, NS, _JWKS_FILE, bearer
    from app import app
    from leaf_platform import canonical_jobs, db as platform_db, store
    from leaf_platform.db import cursor

    platform_db.apply_migration()
    # Tier comes from the org, not the token claim; the solve needs hosted_pro.
    org = store.create_org("G1A production-shaped", tier="hosted_pro")
    project = store.create_project(org.org_id, "AutoFill solve")
    version = store.create_drawing_version(
        org.org_id, project.project_id, oss_object="g1a/roof.dwg",
        intake_ref="g1a/roof-intake.json", created_by="g1a-test")
    # The server resolves the token's verified `sub` (test_wave5.mint signs
    # auth0|wave5|<tier>) under authority auth0.
    subject = "auth0|wave5|hosted_pro"
    store.create_identity_binding(org.org_id, "auth0", subject, role="owner")
    store.set_project_authority_mode(org.org_id, project.project_id, "postgres_canonical")

    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_AUTH0_ISSUER", ISS)
    monkeypatch.setenv("LEAF_AUTH0_AUDIENCE", AUD)
    monkeypatch.setenv("LEAF_TENANT_CLAIM_NS", NS)
    monkeypatch.setenv("LEAF_AUTH0_JWKS_FILE", str(_JWKS_FILE))

    params = {
        "groups": [
            {"handle": "A", "name": "A", "count": 25, "centroidX": 0.0,
             "centroidY": 0.0, "electricalZone": "Z", "elevationZone": ""},
            {"handle": "B", "name": "B", "count": 15, "centroidX": 10.0,
             "centroidY": 0.0, "electricalZone": "Z", "elevationZone": ""},
        ],
        "panelsPerString": 10,
        "options": {"drainThreshold": 23, "drainDiscount": 0.0,
                    "activeGroupPenalty": 10, "concentrationBias": 0.15,
                    "clusterMarginPitches": 2.0},
    }
    headers = {**bearer("hosted_pro", str(org.org_id)),
               "X-Org-Id": str(org.org_id), "X-Project-Id": str(project.project_id),
               "Idempotency-Key": "g1a-real-solve-1"}
    with TestClient(app, raise_server_exceptions=True) as client:
        # A run must echo the catalog digest the tool list issued, as a client does
        # after confirming the tool.
        listed = client.get("/api/tools", headers=headers)
        assert listed.status_code == 200, listed.text
        catalog_digest = next(t["catalog_digest"] for t in listed.json()["tools"]
                              if t.get("name") == "string-autofill-opt")
        run_body = {"tool": "string-autofill-opt", "params": params,
                    "dwg": str(version.version_id), "catalog_digest": catalog_digest}
        submitted = client.post("/api/run", headers=headers, json=run_body)
        assert submitted.status_code == 202, submitted.text
        job_id = submitted.json()["job_id"]
        assert client.get(f"/api/jobs/{job_id}", headers=headers).json()["status"] == "submitted"
        confused = client.post(
            "/api/run",
            headers={**bearer("hosted_pro", str(uuid.uuid4())),
                     "X-Org-Id": str(org.org_id),
                     "X-Project-Id": str(project.project_id),
                     "Idempotency-Key": "g1a-confused-deputy"},
            json=run_body,
        )
        assert confused.status_code == 409
        assert "conflicts with the active platform binding" in confused.text

    # A first worker process claims and dies. After its short lease expires, a
    # distinct worker must reclaim attempt 2 and produce the sole terminal result.
    crashed = subprocess.run(
        [sys.executable, "-c",
         "import platform_link; "
         "j=platform_link._canonical_jobs_module().claim_next('crashed-worker', lease_seconds=1); "
         "assert j and j['attempt'] == 1"],
        cwd=str(SERVER_DIR), env=dict(os.environ), capture_output=True, text=True, timeout=30)
    assert crashed.returncode == 0, crashed.stderr
    time.sleep(1.2)

    # The recovery worker proves the API process owns no solver execution state
    # and that the durable claim survives navigation/process boundaries.
    worker = subprocess.run(
        [sys.executable, str(SERVER_DIR / "canonical_worker.py"), "--once"],
        cwd=str(SERVER_DIR), env=dict(os.environ), capture_output=True, text=True, timeout=90)
    assert worker.returncode == 0, worker.stderr

    with TestClient(app, raise_server_exceptions=True) as reconnected:
        completed = reconnected.get(f"/api/jobs/{job_id}", headers=headers)
        assert completed.status_code == 200, completed.text
        record = completed.json()
        assert record["status"] == "complete"
        assert record["attempt"] == 2
        assert record["result"]["solver_result"]["groupTargets"] == {"A": 40, "B": 0}
        assert record["result"]["result_sha256"] == (
            "525e2d417d916ab896ab25525352783302c98f6f436631777731a4c08bb1ed59")
        assert record["result"]["solve_hash"]
        assert record["result"]["history_hash"]
        assert set(record["result"]["snapshotPins"]) == {"catalog", "standards", "ahj"}
        assert record["execution_context"]["capability_state"] == "connected_degraded"
        assert record["provenance"]["solver_revision"]
        replay = reconnected.post("/api/run", headers=headers, json=run_body)
        assert replay.status_code == 202
        assert replay.json()["job_id"] == job_id

        denied = reconnected.post(
            "/api/run", headers={**bearer("restricted", str(org.org_id)),
                                  "X-Org-Id": str(org.org_id),
                                  "X-Project-Id": str(project.project_id),
                                  "Idempotency-Key": "g1a-denied"},
            json=run_body)
        assert denied.status_code == 403

    saved = canonical_jobs.get_job_for_tenant(uuid.UUID(job_id), str(org.org_id))
    assert store.verify_solve_record(org.org_id, uuid.UUID(saved["result"]["solve_id"]))
    assert store.verify_history_operation(
        org.org_id, uuid.UUID(saved["result"]["history_operation_id"]))
    with cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM solve_records WHERE project_id = %(project)s",
                    {"project": project.project_id})
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT COUNT(*) AS n FROM history_operations WHERE project_id = %(project)s",
                    {"project": project.project_id})
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT COUNT(*) AS n FROM outbox_entries WHERE project_id = %(project)s",
                    {"project": project.project_id})
        assert cur.fetchone()["n"] == 2
    platform_db.reset_pool()
