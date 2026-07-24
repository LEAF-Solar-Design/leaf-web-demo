"""Clean-room smoke for the containerized canonical worker.

Run inside the canonical-worker image on the compose network. The script writes
only a uniquely named test org/project, submits one real AutoFill solve, waits
for the separately running worker, and verifies the immutable terminal records.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import platform_link
from solver_adapters import autofill


def main() -> None:
    expected_revision = os.environ.get("AUTOFILL_SOLVER_REVISION", "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", expected_revision):
        raise RuntimeError(
            "AUTOFILL_SOLVER_REVISION must identify the exact solver commit in this image")
    descriptor = autofill.descriptor()
    if descriptor["source_revision"] != expected_revision:
        raise AssertionError(
            "canonical worker descriptor does not match its pinned solver revision")

    store, db, _deps = platform_link._load_platform()
    canonical_jobs = platform_link._canonical_jobs_module()
    suffix = uuid.uuid4().hex
    tenant = f"container-smoke-{suffix}"
    idempotency_key = f"container-smoke-{suffix}"

    org = store.create_org(f"Canonical container smoke {suffix}")
    project = store.create_project(org.org_id, "Real worker solve")
    version = store.create_drawing_version(
        org.org_id,
        project.project_id,
        oss_object=f"container-smoke/{suffix}/input.dwg",
        intake_ref=f"container-smoke/{suffix}/input.json",
        created_by="canonical-container-smoke",
    )
    store.set_project_authority_mode(org.org_id, project.project_id, "postgres_canonical")

    params = {
        "groups": [
            {
                "handle": "A",
                "name": "A",
                "count": 25,
                "centroidX": 0.0,
                "centroidY": 0.0,
                "electricalZone": "Z",
                "elevationZone": "",
            },
            {
                "handle": "B",
                "name": "B",
                "count": 15,
                "centroidX": 10.0,
                "centroidY": 0.0,
                "electricalZone": "Z",
                "elevationZone": "",
            },
        ],
        "panelsPerString": 10,
        "options": {
            "drainThreshold": 23,
            "drainDiscount": 0.0,
            "activeGroupPenalty": 10,
            "concentrationBias": 0.15,
            "clusterMarginPitches": 2.0,
        },
    }
    submitted = canonical_jobs.submit_solve_job(
        org.org_id,
        project.project_id,
        tenant,
        "string-autofill-opt",
        params,
        idempotency_key,
        input_version_id=version.version_id,
    )

    deadline = time.monotonic() + 90
    record = None
    while time.monotonic() < deadline:
        record = canonical_jobs.get_job_for_tenant(submitted["job_id"], tenant)
        if record and record["status"] in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.25)

    if not record or record["status"] != "succeeded":
        raise RuntimeError(f"canonical worker did not succeed: {record!r}")

    repeated = canonical_jobs.submit_solve_job(
        org.org_id,
        project.project_id,
        tenant,
        "string-autofill-opt",
        params,
        idempotency_key,
        input_version_id=version.version_id,
    )
    if repeated["job_id"] != record["job_id"]:
        raise AssertionError("idempotent resubmission created another job")

    result = record["result"]
    solve_id = uuid.UUID(result["solve_id"])
    history_id = uuid.UUID(result["history_operation_id"])
    if not store.verify_solve_record(org.org_id, solve_id):
        raise AssertionError("solve record verification failed")
    if not store.verify_history_operation(org.org_id, history_id):
        raise AssertionError("history operation verification failed")

    with db.cursor() as cur:
        counts = {}
        for key, table in (
            ("jobs", "jobs"),
            ("solves", "solve_records"),
            ("history", "history_operations"),
            ("outbox", "outbox_entries"),
            ("solvePins", "solve_snapshot_pins"),
        ):
            cur.execute(
                f"SELECT COUNT(*) AS n FROM {table} "
                "WHERE org_id = %(org)s AND project_id = %(project)s",
                {"org": org.org_id, "project": project.project_id},
            )
            counts[key] = cur.fetchone()["n"]

    expected = {"jobs": 1, "solves": 1, "history": 1, "outbox": 2, "solvePins": 3}
    if counts != expected:
        raise AssertionError(f"unexpected canonical record counts: {counts!r}")

    print(json.dumps({
        "status": "passed",
        "jobId": str(record["job_id"]),
        "attempt": record["attempt"],
        "solveHash": result["solve_hash"],
        "historyHash": result["history_hash"],
        "resultSha256": result["result_sha256"],
        "snapshotKinds": sorted(result["snapshotPins"]),
        "solverRevision": descriptor["source_revision"],
        "counts": counts,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
