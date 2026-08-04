"""Measure the REAL overlay_store against PostgreSQL 16, from inside WSL.

This is the term the app-side harness fakes: the actual approve() — anchor
lock, lease check, document CAS, revision insert, audit row, one transaction —
through the real platform package against the real server. Runs in WSL because
the Windows host cannot reach this PostgreSQL over TCP.

Run (WSL):  cd /mnt/c/tmp/t1-decision-89d2209d && \
  DATABASE_URL=postgresql://t1verify:t1pw@127.0.0.1:5432/t1overlay \
  ~/t1verify-venv/bin/python server/measure_overlay_store_pg.py
"""
from __future__ import annotations

import json
import statistics
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))  # repo root: `platform` = the repo package

# The STDLIB platform module is already in sys.modules at interpreter startup,
# so the path insert alone cannot shadow it — evict the cached module so the
# fresh import resolves against the repo package instead.
sys.modules.pop("platform", None)

import psycopg  # noqa: E402

from platform import db, overlay_store  # noqa: E402

TRIALS = 20
URL = "postgresql://t1verify:t1pw@127.0.0.1:5432/t1overlay"

# Schema setup: orgs + the 0028 tables, idempotent.
ORGS = ("CREATE TABLE IF NOT EXISTS orgs (org_id UUID PRIMARY KEY, "
        "name TEXT NOT NULL DEFAULT 't', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
mig = Path("platform/migrations/0028_overlay_tokens.sql").read_text(encoding="utf-8")
with psycopg.connect(URL, autocommit=True) as c:
    c.execute(ORGS)
    c.execute(mig)

approve_ms, propose_ms = [], []
for i in range(TRIALS):
    tenant = str(uuid.uuid4())
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("INSERT INTO orgs (org_id) VALUES (%s)", (tenant,))

    pid = str(uuid.uuid4())
    t0 = time.perf_counter()
    overlay_store.create_proposal(
        proposal_id=pid, tenant_id=tenant, session_id=f"s-{i}",
        tokens={"color.border": "#123456"}, lease_s=900)
    propose_ms.append((time.perf_counter() - t0) * 1000.0)

    t0 = time.perf_counter()
    proposal, doc = overlay_store.approve(
        proposal_id=pid, tenant_id=tenant, actor="op@measure",
        decision_key="k1234567", expected_version=0)
    approve_ms.append((time.perf_counter() - t0) * 1000.0)
    assert proposal["state"] == "approved" and doc["version"] == 1


def stats(xs):
    xs = sorted(xs)
    return {"p50": round(statistics.median(xs), 1),
            "p95": round(xs[min(len(xs) - 1, int(len(xs) * 0.95))], 1),
            "max": round(xs[-1], 1)}


print(json.dumps({"trials": TRIALS,
                  "create_proposal_ms": stats(propose_ms),
                  "approve_ms": stats(approve_ms)}, indent=2))
