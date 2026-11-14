"""Immutable normalized model inputs and project-scoped ARLO jobs.

The committed example is a synthetic model, not a DWG upload or native CAD
receipt. Its exact request bytes live in arlo_lab_inputs; drawing_versions
provides the existing canonical job's immutable input-version identity.
"""
from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal
from pathlib import Path
import uuid
from typing import Any

from psycopg.types.json import Jsonb

from . import canonical_jobs, project_lifecycle as lifecycle
from .db import connection, run_transaction

CONTRACT = "arlo_lab_example_v1"
REFERENCE_CONTRACT = "arlo_project_reference_v1"
REFERENCE_SOURCE = "project-reference"
MAX_INPUT_BYTES = 1024 * 1024  # arlo_lab_inputs.request_json storage limit
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
           binding_id: uuid.UUID, *, write: bool, lock: bool = True) -> None:
    lifecycle._project_row(cur, org_id, project_id, lock=lock)
    lifecycle._require_project_role(cur, org_id, project_id, binding_id, write=write, lock=lock)
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
    response = {"organization_id": str(row["org_id"]), "project_id": str(row["project_id"]),
            "input_version_id": str(row["input_version_id"]), "example_id": row["example_id"],
            "example_version": row["example_version"], "input_sha256": row["input_sha256"],
            "request": request, "request_canonical_json": canonical_text(request)}
    response.update(parent_id=row["provenance"].get("parent_id"),
                    original_id=row["provenance"].get("original_id", str(row["input_version_id"])))
    if row["example_id"] == REFERENCE_SOURCE:
        response.pop("example_id")
        response.pop("example_version")
        response.update(material_id=row["provenance"]["material_id"],
                        original_sha256=row["provenance"]["original_sha256"])
    return response


def register_input(org_id: uuid.UUID, project_id: uuid.UUID, binding_id: uuid.UUID, *,
                   example_id: str, example_version: str, idempotency_key: str) -> dict:
    key = lifecycle._validate_idempotency_key(idempotency_key)
    request = load_example(example_id, example_version)
    source = {"contract": CONTRACT, "example_id": example_id, "example_version": example_version}
    return _register_input(org_id, project_id, binding_id, key=key, request=request,
                           example_id=example_id, example_version=example_version,
                           source=source, source_kind="synthetic_model")


def register_reference_input(org_id: uuid.UUID, project_id: uuid.UUID, binding_id: uuid.UUID, *,
                             material_id: uuid.UUID, idempotency_key: str) -> dict:
    import project_reference_materials as originals
    from solver_adapters.arlo_design import _validated_input
    from . import project_reference_materials as references

    key = lifecycle._validate_idempotency_key(idempotency_key)
    with connection() as conn, conn.cursor() as cur:
        _scope(cur, org_id, project_id, binding_id, write=True, lock=False)
    material = references.get_material(org_id, project_id, binding_id, material_id)
    if material["media_type"] != "application/json":
        raise ValueError("ARLO input requires a saved JSON reference")
    if not 0 < material["byte_size"] <= MAX_INPUT_BYTES:
        raise ValueError("ARLO input exceeds its size limit")
    # The protected reader verifies exact length and digest against scoped metadata.
    raw = originals.read_content(org_id, project_id, material)
    try:
        request = json.loads(raw.decode("utf-8-sig"))
        # Validate with the execution adapter without replacing original identity labels.
        _validated_input(request, {"org_id": org_id, "project_id": project_id,
                                   "input_version_id": material_id})
        if len(canonical_text(request).encode("utf-8")) > MAX_INPUT_BYTES:
            raise ValueError("normalized ARLO input exceeds its size limit")
    except (UnicodeError, RecursionError, OverflowError):
        raise ValueError("invalid ARLO JSON reference") from None
    source = {"contract": REFERENCE_CONTRACT, "material_id": str(material_id),
              "original_sha256": material["sha256"]}
    # Use the existing origin columns for the source discriminator and identifier.
    return _register_input(org_id, project_id, binding_id, key=key, request=request,
                           example_id=REFERENCE_SOURCE, example_version=str(material_id),
                           source=source, source_kind="project_reference")


def _register_input(org_id, project_id, binding_id, *, key, request, example_id,
                    example_version, source, source_kind, conn=None):
    input_sha = digest(request)
    fingerprint = digest({**source, "input_sha256": input_sha})

    def operation(conn):
        with conn.cursor() as cur:
            # A project row lock serializes registration and project deletion.
            _scope(cur, org_id, project_id, binding_id, write=True)
            cur.execute(
                "SELECT v.import_fingerprint, v.provenance, i.* FROM drawing_versions v "
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
                 "name": (f"ARLO project reference input {version_id}" if source_kind == "project_reference"
                          else f"ARLO synthetic lab input {version_id}")})
            provenance = {**source, "input_sha256": input_sha,
                          "actor_binding_id": str(binding_id), "source_kind": source_kind,
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
            return _input_response({**cur.fetchone(), "provenance": provenance})
    return operation(conn) if conn is not None else run_transaction(operation, isolation="serializable")


def _registered_row(cur, org_id, project_id, version_id):
    cur.execute(
        "SELECT i.*, v.provenance FROM arlo_lab_inputs i "
        "JOIN drawing_versions v ON v.version_id = i.input_version_id "
        "AND v.org_id = i.org_id AND v.project_id = i.project_id "
        "WHERE i.org_id = %(org)s AND i.project_id = %(project)s "
        "AND i.input_version_id = %(version)s AND v.deleted_at IS NULL",
        {"org": org_id, "project": project_id, "version": version_id})
    row = cur.fetchone()
    if row is None:
        raise lifecycle.LifecycleUnavailable("ARLO input not found")
    _input_response(row)  # Verify the immutable bytes before using their lineage.
    return row


def _active_input(cur, org_id, project_id, original_id):
    # Only this project-file pointer changes; input rows and job receipts never do.
    path = f"arlo/inputs/{original_id}/active.json"
    cur.execute(
        "SELECT content FROM project_files WHERE org_id = %(org)s "
        "AND project_id = %(project)s AND path = %(path)s",
        {"org": org_id, "project": project_id, "path": path})
    row = cur.fetchone()
    if row is None:
        return original_id
    try:
        return uuid.UUID(json.loads(row["content"])["input_version_id"])
    except (ValueError, TypeError, KeyError, AttributeError):
        raise lifecycle.LifecycleConflict("ARLO active input reference is invalid.") from None


def read_input(org_id, project_id, binding_id, input_version_id):
    def operation(conn):
        with conn.cursor() as cur:
            _scope(cur, org_id, project_id, binding_id, write=False)
            base = _input_response(_registered_row(cur, org_id, project_id, input_version_id))
            original_id = uuid.UUID(base["original_id"])
            active = _active_input(cur, org_id, project_id, original_id)
            result = _input_response(_registered_row(cur, org_id, project_id, active))
            if result["original_id"] != str(original_id):
                raise lifecycle.LifecycleConflict("ARLO active input lineage mismatch")
            return result
    return run_transaction(operation, isolation="serializable")


def _patch_constraints(request, patch):
    if not isinstance(patch, dict) or not patch or set(patch) - {"requirements", "budget"}:
        raise ValueError("Only requirements and budget constraints can be revised.")
    changed = json.loads(canonical_text(request))
    for group, fields in patch.items():
        if not isinstance(fields, dict) or not fields:
            raise ValueError(f"{group} must contain numeric constraint fields.")
        for name, value in fields.items():
            prior = request.get(group, {}).get(name)
            label = f"{group}.{name}"
            if isinstance(prior, bool) or not isinstance(prior, (int, float)):
                raise ValueError(f"{label} is not an editable numeric constraint.")
            try:
                finite = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise ValueError(f"{label} must be a finite number.")
            positive = group == "budget" or name in {"diameter_m", "max_support_span_m"}
            if value < 0 or (positive and value == 0):
                raise ValueError(f"{label} must be {'greater than zero' if positive else 'nonnegative'}.")
            if group == "budget" and name != "timeout_seconds" and not float(value).is_integer():
                raise ValueError(f"{label} must be a whole number.")
            changed[group][name] = int(value) if group == "budget" and name != "timeout_seconds" else value
    return changed


def revise_input(org_id, project_id, binding_id, input_version_id, *, patch=None,
                 restore=False, idempotency_key):
    """Atomically append a constraint revision (or select the root) and move its pointer."""
    from solver_adapters.arlo_design import _validated_input

    key = lifecycle._validate_idempotency_key(idempotency_key)
    def operation(conn):
        with conn.cursor() as cur:
            _scope(cur, org_id, project_id, binding_id, write=True)
            fingerprint = digest({"input_version_id": str(input_version_id), "patch": patch, "restore": restore})
            parent = _registered_row(cur, org_id, project_id, input_version_id)
            original_id = uuid.UUID(_input_response(parent)["original_id"])
            original = _registered_row(cur, org_id, project_id, original_id)
            replay = lifecycle._receipt_replay(cur, org_id, project_id, "file_put", key, fingerprint)
            if replay is not None:
                return _input_response(_registered_row(cur, org_id, project_id,
                                                       uuid.UUID(replay["input_version_id"])))
            if _active_input(cur, org_id, project_id, original_id) != input_version_id:
                raise lifecycle.LifecycleConflict("The active ARLO input changed. Reopen constraints and try again.")
            if restore:
                result = _input_response(original)
            else:
                request = _patch_constraints(json.loads(parent["request_json"]), patch)
                _validated_input(request, {"org_id": org_id, "project_id": project_id,
                                           "input_version_id": input_version_id})
                if len(canonical_text(request).encode("utf-8")) > MAX_INPUT_BYTES:
                    raise ValueError("Revised ARLO input exceeds its size limit.")
                if request == json.loads(original["request_json"]):
                    result = _input_response(original)
                else:
                    source = {name: parent["provenance"][name] for name in
                              ("contract", "material_id", "original_sha256", "example_id", "example_version")
                              if name in parent["provenance"]}
                    source.update(parent_id=str(input_version_id), original_id=str(original_id))
                    result = _register_input(org_id, project_id, binding_id, key=key, request=request,
                        example_id=parent["example_id"], example_version=parent["example_version"],
                        source=source, source_kind=parent["provenance"]["source_kind"], conn=conn)
            content = canonical_text({"input_version_id": result["input_version_id"]})
            cur.execute(
                "INSERT INTO project_files (file_id,org_id,project_id,path,media_type,content,"
                "content_sha256,created_by_binding_id) VALUES (%(file)s,%(org)s,%(project)s,"
                "%(path)s,'application/json',%(content)s,%(sha)s,%(actor)s) "
                "ON CONFLICT ON CONSTRAINT project_files_scope_path_unique DO UPDATE SET "
                "content=EXCLUDED.content, content_sha256=EXCLUDED.content_sha256, "
                "revision=project_files.revision+1, updated_at=NOW()",
                {"file": uuid.uuid4(), "org": org_id, "project": project_id,
                 "path": f"arlo/inputs/{original_id}/active.json", "content": content,
                 "sha": hashlib.sha256(content.encode()).hexdigest(), "actor": binding_id})
            lifecycle._write_receipt(cur, org_id, project_id, binding_id, "file_put", key,
                                     fingerprint, {"input_version_id": result["input_version_id"]})
            return result
    return run_transaction(operation, isolation="serializable")


def _same_json_value(left: Any, right: Any) -> bool:
    """Compare JSON values without treating number spelling as request content."""
    if type(left) in (int, float) and type(right) in (int, float):
        # Browsers serialize 180.0 as 180. Use exact decimal values, without a
        # tolerance, and exclude bool (a subclass of int in Python).
        return Decimal(str(left)) == Decimal(str(right))
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _same_json_value(value, right[key]) for key, value in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _same_json_value(a, b) for a, b in zip(left, right))
    return left == right


def _execution_input_row(cur, org_id, project_id, version_id):
    cur.execute(
        "SELECT i.*, v.provenance, v.intake_ref, v.oss_object, v.import_fingerprint "
        "FROM arlo_lab_inputs i "
        "JOIN drawing_versions v ON v.version_id = i.input_version_id "
        "AND v.org_id = i.org_id AND v.project_id = i.project_id "
        "JOIN live_projects p ON p.org_id = i.org_id AND p.project_id = i.project_id "
        "WHERE i.org_id = %(org)s AND i.project_id = %(project)s "
        "AND i.input_version_id = %(version)s AND v.deleted_at IS NULL",
        {"org": org_id, "project": project_id, "version": version_id})
    return cur.fetchone()


def _input_provenance_checks(row, version_id):
    provenance = row["provenance"]
    is_object = isinstance(provenance, dict)
    expected = REFERENCE_CONTRACT if row["example_id"] == REFERENCE_SOURCE else CONTRACT
    return {"provenance_is_object": is_object,
            "contract_matches": is_object and provenance.get("contract") == expected,
            "input_sha256_matches": is_object and provenance.get("input_sha256") == row["input_sha256"],
            "intake_ref_matches": row["intake_ref"] == f"arlo_lab_inputs:{version_id}",
            "oss_object_is_null": row["oss_object"] is None}


def _input_provenance_diagnostic(cur, org_id, project_id, version_id):
    # This is an API reader's observation, never an attestation of the worker's
    # imported module. No raw provenance, request, storage ref or path is exposed.
    report = {"reader_context": "api_not_worker",
              "reader_module_file_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    row = _execution_input_row(cur, org_id, project_id, version_id)
    report["registered_input_found"] = row is not None
    if row is not None:
        report["predicates"] = _input_provenance_checks(row, version_id)
        report["value_types"] = {key: type(row[key]).__name__ for key in
                                 ("example_id", "input_sha256", "provenance", "intake_ref", "oss_object")}
        provenance = row["provenance"]
        report["provenance_value_types"] = {key: type(provenance.get(key)).__name__ for key in
                                            ("contract", "input_sha256")} if isinstance(provenance, dict) else {}
    return report


def load_registered_request(job_context: dict, submitted_params: dict) -> dict:
    """Bind canonical submission and execution to the exact persisted input."""
    org_id = uuid.UUID(str(job_context["org_id"]))
    project_id = uuid.UUID(str(job_context["project_id"]))
    version_id = uuid.UUID(str(job_context["input_version_id"]))
    with connection() as conn:
        with conn.cursor() as cur:
            row = _execution_input_row(cur, org_id, project_id, version_id)
    if row is None:
        raise ValueError("registered ARLO model input is unavailable")
    provenance = row["provenance"]
    expected_contract = REFERENCE_CONTRACT if row["example_id"] == REFERENCE_SOURCE else CONTRACT
    if not all(_input_provenance_checks(row, version_id).values()):
        raise ValueError("registered ARLO input provenance mismatch")
    if expected_contract == REFERENCE_CONTRACT:
        source = {"contract": REFERENCE_CONTRACT, "material_id": row["example_version"],
                  "original_sha256": provenance.get("original_sha256")}
        if "parent_id" in provenance:
            source.update(parent_id=provenance["parent_id"], original_id=provenance["original_id"])
        if provenance.get("source_kind") != "project_reference" \
                or provenance.get("material_id") != source["material_id"] \
                or digest({**source, "input_sha256": row["input_sha256"]}) != row["import_fingerprint"]:
            raise ValueError("registered ARLO reference provenance mismatch")
    response = _input_response(row)
    canonical_text(submitted_params)  # Reject non-JSON or non-finite values.
    if not _same_json_value(submitted_params, response["request"]):
        raise ValueError("ARLO request differs from its registered immutable input")
    # The hash-verified stored bytes remain authoritative for execution, even
    # when the browser used an equivalent spelling for a JSON number.
    return response["request"]


def job_view(org_id: uuid.UUID, project_id: uuid.UUID, binding_id: uuid.UUID,
             job_id: uuid.UUID, *, cancel: bool = False, input_diagnostics: bool = False) -> dict:
    if cancel and input_diagnostics:
        raise ValueError("input diagnostics are read-only")
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
            if input_diagnostics:
                view["input_diagnostics"] = _input_provenance_diagnostic(
                    cur, org_id, project_id, row["input_version_id"])
            return {"job": view}
    return run_transaction(operation, isolation="serializable")
