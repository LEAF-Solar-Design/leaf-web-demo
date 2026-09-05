"""Immutable normalized model inputs and project-scoped ARLO lab jobs.

The committed example is a synthetic model, not a DWG upload or native CAD
receipt. Its exact request bytes live in arlo_lab_inputs; drawing_versions
provides the existing canonical job's immutable input-version identity.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import uuid
from typing import Any

from psycopg.types.json import Jsonb

from . import canonical_jobs, project_lifecycle as lifecycle
from .db import connection, run_transaction

CONTRACT = "arlo_lab_example_v1"
_EXAMPLES = {("feeder-lab-v1", "1"): (
    "feeder-lab-v1.json", "088e7cc50a8148dee695c00f1556c2e57660793caafe02b908d810c0ebc51e32")}
_EXAMPLE_ROOT = Path(__file__).resolve().parent / "examples" / "arlo"


def canonical_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_text(value).encode("utf-8")).hexdigest()


def load_example(example_id: str, example_version: str) -> dict:
    entry = _EXAMPLES.get((example_id, example_version))
    if entry is None:
        raise lifecycle.LifecycleUnavailable("ARLO example version not found")
    raw = (_EXAMPLE_ROOT / entry[0]).read_bytes()
    request = json.loads(raw)
    if digest(request) != entry[1]:
        raise lifecycle.LifecycleConflict("committed ARLO example digest mismatch")
    if not isinstance(request, dict) or request.get("contract") != "arlo_design_request_v1":
        raise ValueError("invalid ARLO example request contract")
    if not isinstance(request.get("scenario"), dict) or not request.get("catalog"):
        raise ValueError("ARLO example requires normalized model and catalog")
    if request.get("budget") != {"beam_width": 8, "max_revision_rounds": 4,
                                 "timeout_seconds": 180.0, "max_proposals": 3}:
        raise ValueError("ARLO example execution budget differs from the lab contract")
    canonical_text(request)
    return request


def _scope(cur: Any, org_id: uuid.UUID, project_id: uuid.UUID,
           binding_id: uuid.UUID, *, write: bool) -> None:
    lifecycle._project_row(cur, org_id, project_id)
    lifecycle._require_project_role(cur, org_id, project_id, binding_id, write=write)
    cur.execute(
        "SELECT COALESCE((SELECT authority_mode FROM live_project_authority_modes "
        "WHERE org_id = %(org)s AND project_id = %(project)s), "
        "(SELECT authority_mode FROM tenant_authority_modes WHERE org_id = %(org)s), "
        "'legacy_sqlite') AS authority_mode", {"org": org_id, "project": project_id})
    if cur.fetchone()["authority_mode"] != "postgres_canonical":
        raise lifecycle.LifecycleConflict("ARLO lab requires canonical project authority")


def _input_response(row: dict) -> dict:
    request = json.loads(row["request_json"])
    if digest(request) != row["input_sha256"]:
        raise lifecycle.LifecycleConflict("stored ARLO input digest mismatch")
    return {"organization_id": str(row["org_id"]), "project_id": str(row["project_id"]),
            "input_version_id": str(row["input_version_id"]), "example_id": row["example_id"],
            "example_version": row["example_version"], "input_sha256": row["input_sha256"],
            "request": request, "request_canonical_json": canonical_text(request)}


def register_input(org_id: uuid.UUID, project_id: uuid.UUID, binding_id: uuid.UUID, *,
                   example_id: str, example_version: str, idempotency_key: str) -> dict:
    key = lifecycle._validate_idempotency_key(idempotency_key)
    request = load_example(example_id, example_version)
    input_sha = digest(request)
    fingerprint = digest({"contract": CONTRACT, "example_id": example_id,
                          "example_version": example_version, "input_sha256": input_sha})

    def operation(conn):
        with conn.cursor() as cur:
            # A project row lock serializes registration and project deletion.
            _scope(cur, org_id, project_id, binding_id, write=True)
            cur.execute(
                "SELECT v.import_fingerprint, i.* FROM drawing_versions v "
                "LEFT JOIN arlo_lab_inputs i ON i.input_version_id = v.version_id "
                "AND i.org_id = v.org_id AND i.project_id = v.project_id "
                "WHERE v.org_id = %(org)s AND v.project_id = %(project)s "
                "AND v.idempotency_key = %(key)s AND v.deleted_at IS NULL",
                {"org": org_id, "project": project_id, "key": key})
            replay = cur.fetchone()
            if replay is not None:
                if replay["import_fingerprint"] != fingerprint or not replay["input_version_id"]:
                    raise lifecycle.LifecycleConflict("input idempotency key has different content")
                return _input_response(replay)
            version_id, drawing_id = uuid.uuid4(), uuid.uuid4()
            cur.execute(
                "INSERT INTO drawing_artifacts (drawing_id, org_id, project_id, name) "
                "VALUES (%(drawing)s, %(org)s, %(project)s, %(name)s)",
                {"drawing": drawing_id, "org": org_id, "project": project_id,
                 "name": f"ARLO synthetic lab input {version_id}"})
            provenance = {"contract": CONTRACT, "example_id": example_id,
                          "example_version": example_version, "input_sha256": input_sha,
                          "actor_binding_id": str(binding_id), "source_kind": "synthetic_model",
                          "native_verified": False}
            cur.execute(
                "INSERT INTO drawing_versions (version_id,drawing_id,org_id,project_id,seq,"
                "oss_object,intake_ref,created_by,provenance,idempotency_key,import_fingerprint) "
                "VALUES (%(version)s,%(drawing)s,%(org)s,%(project)s,1,NULL,%(ref)s,"
                "%(actor)s,%(provenance)s,%(key)s,%(fingerprint)s)",
                {"version": version_id, "drawing": drawing_id, "org": org_id,
                 "project": project_id, "ref": f"arlo_lab_inputs:{version_id}",
                 "actor": str(binding_id), "provenance": Jsonb(provenance),
                 "key": key, "fingerprint": fingerprint})
            cur.execute(
                "INSERT INTO arlo_lab_inputs (input_version_id,org_id,project_id,example_id,"
                "example_version,input_sha256,request_json) VALUES (%(version)s,%(org)s,"
                "%(project)s,%(example)s,%(example_version)s,%(digest)s,%(request)s) RETURNING *",
                {"version": version_id, "org": org_id, "project": project_id,
                 "example": example_id, "example_version": example_version,
                 "digest": input_sha, "request": canonical_text(request)})
            return _input_response(cur.fetchone())
    return run_transaction(operation, isolation="serializable")


def load_registered_request(job_context: dict, submitted_params: dict) -> dict:
    """Bind canonical submission and execution to the exact persisted input."""
    org_id = uuid.UUID(str(job_context["org_id"]))
    project_id = uuid.UUID(str(job_context["project_id"]))
    version_id = uuid.UUID(str(job_context["input_version_id"]))
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT i.*, v.provenance, v.intake_ref, v.oss_object FROM arlo_lab_inputs i "
                "JOIN drawing_versions v ON v.version_id = i.input_version_id "
                "AND v.org_id = i.org_id AND v.project_id = i.project_id "
                "JOIN live_projects p ON p.org_id = i.org_id AND p.project_id = i.project_id "
                "WHERE i.org_id = %(org)s AND i.project_id = %(project)s "
                "AND i.input_version_id = %(version)s AND v.deleted_at IS NULL",
                {"org": org_id, "project": project_id, "version": version_id})
            row = cur.fetchone()
    if row is None:
        raise ValueError("registered ARLO model input is unavailable")
    provenance = row["provenance"]
    if not isinstance(provenance, dict) or provenance.get("contract") != CONTRACT \
            or provenance.get("input_sha256") != row["input_sha256"] \
            or row["intake_ref"] != f"arlo_lab_inputs:{version_id}" or row["oss_object"] is not None:
        raise ValueError("registered ARLO input provenance mismatch")
    response = _input_response(row)
    if digest(submitted_params) != row["input_sha256"]:
        raise ValueError("ARLO request differs from its registered immutable input")
    return response["request"]


def job_view(org_id: uuid.UUID, project_id: uuid.UUID, binding_id: uuid.UUID,
             job_id: uuid.UUID, *, cancel: bool = False) -> dict:
    def operation(conn):
        with conn.cursor() as cur:
            _scope(cur, org_id, project_id, binding_id, write=cancel)
            cur.execute(
                "SELECT * FROM jobs WHERE org_id = %(org)s AND project_id = %(project)s "
                "AND job_id = %(job)s AND tool_name = 'arlo-design' AND deleted_at IS NULL "
                "FOR UPDATE", {"org": org_id, "project": project_id, "job": job_id})
            row = cur.fetchone()
            if row is None:
                raise lifecycle.LifecycleUnavailable("ARLO job not found")
            if cancel and row["status"] in {"queued", "running"}:
                error = {"error_code": "CANCELLED", "message": "Cancelled by project member",
                         "retryable": False}
                provenance = {"actor_binding_id": str(binding_id), "action": "arlo.cancel"}
                cur.execute(
                    "UPDATE jobs SET status='cancelled', error=%(error)s, provenance=%(provenance)s, "
                    "finished_at=NOW(), updated_at=NOW(), lease_owner=NULL, lease_expires_at=NULL, "
                    "terminal_fingerprint=%(fingerprint)s WHERE job_id=%(job)s "
                    "AND org_id=%(org)s AND project_id=%(project)s "
                    "AND status IN ('queued','running') RETURNING *",
                    {"error": Jsonb(error), "provenance": Jsonb(provenance), "job": job_id,
                     "org": org_id, "project": project_id,
                     "fingerprint": digest({"job": str(job_id), "status": "cancelled"})})
                row = cur.fetchone()
            public = canonical_jobs._record(row)
            fields = ("job_id", "org_id", "project_id", "status", "params", "result", "error",
                      "input_version_id", "attempt", "created_at", "updated_at", "started_at",
                      "finished_at", "provenance", "tool_name")
            view = {key: public.get(key) for key in fields}
            result = public.get("result")
            result = result if isinstance(result, dict) else {}
            view["canonical_json"] = {
                "request": canonical_text(public["params"]),
                "solver_input": (canonical_text(result["solver_input"])
                                 if isinstance(result.get("solver_input"), dict) else None),
                "solver_result": (canonical_text(result["solver_result"])
                                  if isinstance(result.get("solver_result"), dict) else None),
            }
            return {"job": view}
    return run_transaction(operation, isolation="serializable")
