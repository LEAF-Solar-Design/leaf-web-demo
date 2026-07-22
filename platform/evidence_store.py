"""Build and persist frozen evidence bundles from canonical platform records."""
from __future__ import annotations

import base64
import uuid
from typing import Any, Dict, Mapping, Optional

from psycopg.types.json import Jsonb

from . import evidence
from .db import connection
from .models import new_uuid
from .store import _insert_outbox


def _record_blobs(cur: Any, org_id: uuid.UUID, project_id: uuid.UUID,
                  solve_id: uuid.UUID) -> Dict[str, bytes]:
    cur.execute("SELECT solve_id, payload, hash_algorithm, hash_canonicalization, hash_domain, "
                "hash_value FROM solve_records WHERE org_id = %(org)s AND project_id = %(project)s "
                "AND solve_id = %(solve)s",
                {"org": org_id, "project": project_id, "solve": solve_id})
    solve = cur.fetchone()
    if solve is None:
        raise ValueError("solve is unavailable")
    cur.execute("SELECT operation_id, operation_type, payload, hash_algorithm, hash_canonicalization, "
                "hash_domain, hash_value FROM history_operations WHERE org_id = %(org)s "
                "AND project_id = %(project)s AND payload->>'solveId' = %(solve_text)s "
                "ORDER BY operation_id",
                {"org": org_id, "project": project_id, "solve_text": str(solve_id)})
    history = [dict(row) for row in cur.fetchall()]
    if not history:
        raise ValueError("solve has no canonical history operation")
    cur.execute("SELECT p.snapshot_kind, p.snapshot_id, s.content_sha256, s.source_sha256, "
                "s.review_state, s.provenance FROM solve_snapshot_pins p JOIN platform_snapshots s "
                "ON s.snapshot_id = p.snapshot_id AND s.snapshot_kind = p.snapshot_kind "
                "WHERE p.org_id = %(org)s AND p.project_id = %(project)s AND p.solve_id = %(solve)s "
                "ORDER BY p.snapshot_kind",
                {"org": org_id, "project": project_id, "solve": solve_id})
    pins = [dict(row) for row in cur.fetchall()]
    if len(pins) != 3:
        raise ValueError("solve does not have all required snapshot pins")
    cur.execute("SELECT f.finding_id, f.rule_id, f.payload, f.finding_sha256 "
                "FROM compliance_findings f JOIN compliance_runs r ON r.run_id = f.run_id "
                "WHERE f.org_id = %(org)s AND f.project_id = %(project)s AND r.solve_id = %(solve)s "
                "ORDER BY f.rule_id",
                {"org": org_id, "project": project_id, "solve": solve_id})
    findings = [dict(row) for row in cur.fetchall()]
    if not findings:
        raise ValueError("evidence bundle requires at least one compliance finding")
    cur.execute("SELECT w.waiver_id, w.finding_id, w.reason, e.sequence, e.state, "
                "e.actor_binding_id, e.note, e.created_at FROM compliance_waivers w "
                "JOIN compliance_findings f ON f.finding_id = w.finding_id "
                "JOIN compliance_runs r ON r.run_id = f.run_id "
                "JOIN compliance_waiver_events e ON e.waiver_id = w.waiver_id "
                "WHERE w.org_id = %(org)s AND w.project_id = %(project)s AND r.solve_id = %(solve)s "
                "ORDER BY w.waiver_id, e.sequence",
                {"org": org_id, "project": project_id, "solve": solve_id})
    waivers = [dict(row) for row in cur.fetchall()]
    solve_record = dict(solve)
    return {
        "records/solve.json": evidence.json_blob(solve_record),
        "records/history.json": evidence.json_blob(history),
        "records/snapshot-pins.json": evidence.json_blob(pins),
        "records/compliance-findings.json": evidence.json_blob(findings),
        "records/waiver-events.json": evidence.json_blob(waivers),
    }


def create_bundle(org_id: uuid.UUID, project_id: uuid.UUID, solve_id: uuid.UUID,
                  idempotency_key: str, *, artifacts: Optional[Mapping[str, bytes]] = None) -> Dict[str, Any]:
    if not idempotency_key:
        raise ValueError("Idempotency-Key is required")
    with connection() as conn:
        with conn.cursor() as cur:
            blobs = _record_blobs(cur, org_id, project_id, solve_id)
            for path, content in (artifacts or {}).items():
                blobs[f"artifacts/{path}"] = content
            manifest = evidence.build(blobs, metadata={"orgId": str(org_id),
                "projectId": str(project_id), "solveId": str(solve_id),
                "scope": "record_only" if not artifacts else "records_and_artifacts"})
            bundle_id = new_uuid()
            cur.execute("INSERT INTO evidence_bundles (bundle_id, org_id, project_id, solve_id, "
                        "idempotency_key, root_sha256, manifest) VALUES "
                        "(%(bundle)s, %(org)s, %(project)s, %(solve)s, %(key)s, %(root)s, %(manifest)s) "
                        "ON CONFLICT (org_id, project_id, idempotency_key) DO NOTHING RETURNING bundle_id",
                        {"bundle": bundle_id, "org": org_id, "project": project_id,
                         "solve": solve_id, "key": idempotency_key,
                         "root": manifest["rootSha256"], "manifest": Jsonb(manifest)})
            created = cur.fetchone()
            if created is None:
                cur.execute("SELECT bundle_id, solve_id, root_sha256, manifest FROM evidence_bundles "
                            "WHERE org_id = %(org)s AND project_id = %(project)s "
                            "AND idempotency_key = %(key)s",
                            {"org": org_id, "project": project_id, "key": idempotency_key})
                existing = cur.fetchone()
                if existing["solve_id"] != solve_id or existing["root_sha256"] != manifest["rootSha256"]:
                    raise ValueError("evidence idempotency key exists with different bundle content")
                return {"bundle_id": str(existing["bundle_id"]), "manifest": existing["manifest"]}
            for item in manifest["entries"]:
                cur.execute("INSERT INTO evidence_entries (entry_id, org_id, project_id, bundle_id, "
                            "path, content, size_bytes, content_sha256) VALUES "
                            "(%(id)s, %(org)s, %(project)s, %(bundle)s, %(path)s, %(content)s, "
                            "%(size)s, %(hash)s)",
                            {"id": new_uuid(), "org": org_id, "project": project_id,
                             "bundle": bundle_id, "path": item["path"],
                             "content": blobs[item["path"]], "size": item["size"],
                             "hash": item["sha256"]})
            _insert_outbox(cur, org_id, project_id, "evidence_bundle", bundle_id,
                           "evidence.bundle.frozen", {"bundleId": str(bundle_id),
                                                      "rootSha256": manifest["rootSha256"]})
            return {"bundle_id": str(bundle_id), "manifest": manifest}


def export_bundle(org_id: uuid.UUID, project_id: uuid.UUID, bundle_id: uuid.UUID) -> Dict[str, Any] | None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT manifest FROM evidence_bundles WHERE org_id = %(org)s "
                        "AND project_id = %(project)s AND bundle_id = %(bundle)s",
                        {"org": org_id, "project": project_id, "bundle": bundle_id})
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute("SELECT path, content FROM evidence_entries WHERE org_id = %(org)s "
                        "AND project_id = %(project)s AND bundle_id = %(bundle)s ORDER BY path",
                        {"org": org_id, "project": project_id, "bundle": bundle_id})
            entries = {item["path"]: base64.b64encode(bytes(item["content"])).decode("ascii")
                       for item in cur.fetchall()}
            return {"manifest": row["manifest"], "entriesBase64": entries}


def list_bundles(org_id: uuid.UUID, project_id: uuid.UUID, *, limit: int = 20) -> list[Dict[str, Any]]:
    """Return bounded review summaries without shipping the bundled entry bytes."""
    if limit < 1 or limit > 100:
        raise ValueError("evidence bundle limit must be between 1 and 100")
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT b.bundle_id, b.solve_id, b.root_sha256, b.manifest, b.created_at, "
                "s.signature_id, s.signed_at, "
                "EXISTS (SELECT 1 FROM history_operations h WHERE h.org_id = b.org_id "
                "AND h.project_id = b.project_id AND h.created_at > b.created_at "
                "AND h.operation_type NOT IN "
                "('review.bundle.countersigned', 'evidence.root.delivered')) AS superseded "
                "FROM evidence_bundles b LEFT JOIN LATERAL "
                "(SELECT signature_id, signed_at FROM review_signatures rs "
                "WHERE rs.org_id = b.org_id AND rs.project_id = b.project_id "
                "AND rs.bundle_id = b.bundle_id ORDER BY signed_at DESC LIMIT 1) s ON TRUE "
                "WHERE b.org_id = %(org)s AND b.project_id = %(project)s "
                "ORDER BY b.created_at DESC LIMIT %(limit)s",
                {"org": org_id, "project": project_id, "limit": limit})
            rows = [dict(row) for row in cur.fetchall()]
    from . import signing
    summaries: list[Dict[str, Any]] = []
    for row in rows:
        signature_id = row["signature_id"]
        verification = (signing.verify_signature(org_id, project_id, signature_id)
                        if signature_id is not None else None)
        manifest = row["manifest"]
        state = ("superseded" if row["superseded"] else
                 "signed_valid" if verification and verification["valid"] else
                 "signed_invalid" if verification else "unsigned")
        summaries.append({
            "bundle_id": str(row["bundle_id"]),
            "solve_id": str(row["solve_id"]),
            "root_sha256": row["root_sha256"],
            "created_at": row["created_at"],
            "entry_count": len(manifest.get("entries", [])),
            "scope": manifest.get("metadata", {}).get("scope"),
            "state": state,
            "superseded": bool(row["superseded"]),
            "latest_signature_id": str(signature_id) if signature_id else None,
            "signed_at": row["signed_at"],
            "verification": verification,
        })
    return summaries
