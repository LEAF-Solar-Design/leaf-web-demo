"""Transactional persistence for deterministic compliance and waiver audit state."""
from __future__ import annotations

import uuid
from typing import Any, Dict, Mapping

from psycopg.types.json import Jsonb

from . import compliance, snapshots
from .db import connection
from .models import canonical_hash, new_uuid
from .store import _insert_outbox


def record_pinned_run(org_id: uuid.UUID, project_id: uuid.UUID, solve_id: uuid.UUID,
                      inputs: Mapping[str, Any]) -> Dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s.content FROM solve_snapshot_pins p JOIN platform_snapshots s "
                "ON s.snapshot_id = p.snapshot_id AND s.snapshot_kind = p.snapshot_kind "
                "WHERE p.org_id = %(org)s AND p.project_id = %(project)s AND p.solve_id = %(solve)s "
                "AND p.snapshot_kind = 'standards'",
                {"org": org_id, "project": project_id, "solve": solve_id})
            row = cur.fetchone()
    if row is None:
        raise ValueError("solve is unavailable or missing its standards pin")
    return record_run(org_id, project_id, solve_id, row["content"], inputs)


def record_run(org_id: uuid.UUID, project_id: uuid.UUID, solve_id: uuid.UUID,
               rule_pack: Mapping[str, Any], inputs: Mapping[str, Any]) -> Dict[str, Any]:
    compliance.validate_rule_pack(rule_pack)
    input_hash = canonical_hash("compliance-input", inputs).value
    pack_hash = canonical_hash("compliance-rule-pack", rule_pack).value
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT p.snapshot_kind, p.snapshot_id, s.content, s.content_sha256 "
                "FROM solve_snapshot_pins p JOIN platform_snapshots s "
                "ON s.snapshot_id = p.snapshot_id AND s.snapshot_kind = p.snapshot_kind "
                "WHERE p.org_id = %(org)s AND p.project_id = %(project)s AND p.solve_id = %(solve)s",
                {"org": org_id, "project": project_id, "solve": solve_id})
            pins = {row["snapshot_kind"]: row for row in cur.fetchall()}
            if not {"standards", "ahj"}.issubset(pins):
                raise ValueError("solve is unavailable or missing standards/AHJ pins")
            if snapshots.sha256(snapshots.canonical_bytes(rule_pack)) != pins["standards"]["content_sha256"]:
                raise ValueError("rule pack does not match the solve's pinned standards snapshot")
            ahj_authority = (pins["ahj"]["content"].get("authority", "unknown")
                             if isinstance(pins["ahj"]["content"], dict) else "unknown")
            if ahj_authority not in {"authoritative", "unknown", "conflicting"}:
                ahj_authority = "unknown"
            findings = compliance.evaluate(
                rule_pack, inputs, standards_snapshot_id=str(pins["standards"]["snapshot_id"]),
                ahj_snapshot_id=str(pins["ahj"]["snapshot_id"]), ahj_authority=ahj_authority)
            run_id = new_uuid()
            cur.execute(
                "INSERT INTO compliance_runs (run_id, org_id, project_id, solve_id, "
                "standards_snapshot_id, ahj_snapshot_id, rule_pack_id, rule_pack_version, "
                "input_sha256, rule_pack_sha256) VALUES "
                "(%(run)s, %(org)s, %(project)s, %(solve)s, %(standards)s, %(ahj)s, "
                "%(pack)s, %(version)s, %(input_hash)s, %(pack_hash)s) ON CONFLICT "
                "(org_id, project_id, solve_id, rule_pack_id, rule_pack_version) DO NOTHING "
                "RETURNING run_id",
                {"run": run_id, "org": org_id, "project": project_id, "solve": solve_id,
                 "standards": pins["standards"]["snapshot_id"],
                 "ahj": pins["ahj"]["snapshot_id"],
                 "pack": rule_pack["pack_id"], "version": rule_pack["version"],
                 "input_hash": input_hash, "pack_hash": pack_hash})
            created = cur.fetchone()
            if created is None:
                cur.execute(
                    "SELECT run_id, input_sha256, rule_pack_sha256 FROM compliance_runs "
                    "WHERE org_id = %(org)s AND project_id = %(project)s AND solve_id = %(solve)s "
                    "AND rule_pack_id = %(pack)s AND rule_pack_version = %(version)s",
                    {"org": org_id, "project": project_id, "solve": solve_id,
                     "pack": rule_pack["pack_id"], "version": rule_pack["version"]})
                existing = cur.fetchone()
                if existing["input_sha256"] != input_hash or existing["rule_pack_sha256"] != pack_hash:
                    raise ValueError("compliance run identity already exists with different inputs")
                run_id = existing["run_id"]
                cur.execute("SELECT finding_id, payload FROM compliance_findings "
                            "WHERE run_id = %(run)s ORDER BY rule_id", {"run": run_id})
                return {"run_id": str(run_id), "findings": [dict(row["payload"],
                        finding_id=str(row["finding_id"])) for row in cur.fetchall()]}
            persisted = []
            for finding in findings:
                finding_id = new_uuid()
                cur.execute(
                    "INSERT INTO compliance_findings (finding_id, org_id, project_id, run_id, "
                    "rule_id, payload, finding_sha256) VALUES "
                    "(%(id)s, %(org)s, %(project)s, %(run)s, %(rule)s, %(payload)s, %(hash)s)",
                    {"id": finding_id, "org": org_id, "project": project_id, "run": run_id,
                     "rule": finding["rule_id"], "payload": Jsonb(finding),
                     "hash": finding["finding_sha256"]})
                persisted.append(dict(finding, finding_id=str(finding_id)))
            _insert_outbox(cur, org_id, project_id, "compliance_run", run_id,
                           "compliance.run.recorded", {"runId": str(run_id),
                                                       "findingCount": len(persisted)})
            return {"run_id": str(run_id), "findings": persisted}


def _binding(cur: Any, org_id: uuid.UUID, binding_id: uuid.UUID) -> str:
    cur.execute("SELECT role FROM identity_bindings WHERE platform_tenant_id = %(org)s "
                "AND binding_id = %(binding)s AND status = 'active'",
                {"org": org_id, "binding": binding_id})
    row = cur.fetchone()
    if row is None:
        raise ValueError("active tenant binding required")
    return row["role"]


def propose_waiver(org_id: uuid.UUID, project_id: uuid.UUID, finding_id: uuid.UUID,
                   binding_id: uuid.UUID, reason: str) -> Dict[str, Any]:
    if not reason or not reason.strip():
        raise ValueError("waiver reason is required")
    with connection() as conn:
        with conn.cursor() as cur:
            if _binding(cur, org_id, binding_id) not in {"owner", "editor"}:
                raise ValueError("owner or editor role required to propose a waiver")
            waiver_id = new_uuid()
            cur.execute(
                "INSERT INTO compliance_waivers (waiver_id, org_id, project_id, finding_id, "
                "requested_by_binding_id, reason) SELECT %(waiver)s, f.org_id, f.project_id, "
                "f.finding_id, %(actor)s, %(reason)s FROM compliance_findings f WHERE "
                "f.org_id = %(org)s AND f.project_id = %(project)s AND f.finding_id = %(finding)s",
                {"waiver": waiver_id, "actor": binding_id, "reason": reason.strip(),
                 "org": org_id, "project": project_id, "finding": finding_id})
            if cur.rowcount != 1:
                raise ValueError("finding is unavailable")
            cur.execute("INSERT INTO compliance_waiver_events (event_id, org_id, project_id, "
                        "waiver_id, sequence, state, actor_binding_id) VALUES "
                        "(%(id)s, %(org)s, %(project)s, %(waiver)s, 1, 'proposed', %(actor)s)",
                        {"id": new_uuid(), "org": org_id, "project": project_id,
                         "waiver": waiver_id, "actor": binding_id})
            _insert_outbox(cur, org_id, project_id, "compliance_waiver", waiver_id,
                           "compliance.waiver.proposed", {"waiverId": str(waiver_id)})
            return {"waiver_id": str(waiver_id), "state": "proposed"}


def transition_waiver(org_id: uuid.UUID, project_id: uuid.UUID, waiver_id: uuid.UUID,
                      binding_id: uuid.UUID, target: str, note: str = "") -> Dict[str, Any]:
    allowed = {"proposed": {"approved", "rejected"}, "approved": {"revoked"}}
    with connection() as conn:
        with conn.cursor() as cur:
            if _binding(cur, org_id, binding_id) != "owner":
                raise ValueError("owner role required for waiver decisions")
            cur.execute("SELECT state, sequence FROM compliance_waiver_events WHERE org_id = %(org)s "
                        "AND project_id = %(project)s AND waiver_id = %(waiver)s "
                        "ORDER BY sequence DESC LIMIT 1 FOR UPDATE",
                        {"org": org_id, "project": project_id, "waiver": waiver_id})
            current = cur.fetchone()
            if current is None:
                raise ValueError("waiver is unavailable")
            if target not in allowed.get(current["state"], set()):
                raise ValueError(f"invalid waiver transition {current['state']} -> {target}")
            sequence = current["sequence"] + 1
            cur.execute("INSERT INTO compliance_waiver_events (event_id, org_id, project_id, "
                        "waiver_id, sequence, state, actor_binding_id, note) VALUES "
                        "(%(id)s, %(org)s, %(project)s, %(waiver)s, %(sequence)s, %(state)s, "
                        "%(actor)s, %(note)s)",
                        {"id": new_uuid(), "org": org_id, "project": project_id,
                         "waiver": waiver_id, "sequence": sequence, "state": target,
                         "actor": binding_id, "note": note})
            _insert_outbox(cur, org_id, project_id, "compliance_waiver", waiver_id,
                           f"compliance.waiver.{target}",
                           {"waiverId": str(waiver_id), "sequence": sequence})
            return {"waiver_id": str(waiver_id), "state": target, "sequence": sequence}


def list_findings(org_id: uuid.UUID, project_id: uuid.UUID, solve_id: uuid.UUID) -> list[Dict[str, Any]]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT f.finding_id, f.payload FROM compliance_findings f JOIN compliance_runs r "
                "ON r.run_id = f.run_id AND r.org_id = f.org_id AND r.project_id = f.project_id "
                "WHERE f.org_id = %(org)s AND f.project_id = %(project)s AND r.solve_id = %(solve)s "
                "ORDER BY f.rule_id",
                {"org": org_id, "project": project_id, "solve": solve_id})
            return [dict(row["payload"], finding_id=str(row["finding_id"])) for row in cur.fetchall()]
