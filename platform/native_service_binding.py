"""The single operator-approved staging service identity; no general grant API.

App image entry (WORKDIR /app/server):
    python -P /app/platform/native_service_binding.py --mode plan
Like server/app.py, the script registers the sibling package as leaf_platform,
leaving the standard-library platform module unshadowed.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone
from uuid import UUID, uuid4

SUBJECT = "hGErLFTFnqJDpg40ZfW06mNAq8aGHUf7@clients"
AUTHORITY = "auth0"
ORG = UUID("1b60bef6-b488-4e18-ac45-4e4afecc30ff")
PROJECT = UUID("ba40e811-d2b8-4707-ad55-6fc11b160d0c")
TENANT_ROLE = "read_only"
PROJECT_ROLE = "editor"
PROVENANCE = "native-service-binding:operator-approved-20261030"
PREFIX = "LEAF_NATIVE_SERVICE_BINDING_RECEIPT "


class Refused(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default error echoes arbitrary operator input to stderr.
        raise Refused("arguments")


def parse_mode(argv):
    parser = _Parser(add_help=False, allow_abbrev=False)
    parser.add_argument("--mode", choices=("plan", "apply", "revoke"), default="plan")
    args = list(argv)
    if len(args) not in (0, 2) or (args and args[0] != "--mode"):
        raise Refused("arguments")
    return parser.parse_args(args).mode


def receipt(mode, outcome, reason=None, binding=None, membership=None):
    return {
        "schema": "leaf.native-service-binding.v1", "mode": mode,
        "outcome": outcome, "reason": reason,
        "binding_id": str(binding["binding_id"]) if binding else None,
        "platform_user_id_present": bool(binding and binding["platform_user_id"]),
        "tenant_role": binding["role"] if binding else None,
        "membership_id": str(membership["membership_id"]) if membership else None,
        "project_role": membership["role"] if membership else None,
        "org_id": str(ORG), "project_id": str(PROJECT),
        "subject_suffix": "@clients", "time": datetime.now(timezone.utc).isoformat(),
    }


def _store():
    from . import store
    return store


def _binding(cur):
    cur.execute(
        "SELECT binding_id, platform_tenant_id, platform_user_id, role, status "
        "FROM identity_bindings WHERE external_authority = %s "
        "AND external_subject = %s AND status = 'active' FOR UPDATE",
        (AUTHORITY, SUBJECT),
    )
    row = cur.fetchone()
    if row and (row["platform_tenant_id"] != ORG or row["role"] != TENANT_ROLE):
        raise Refused("binding_drift")
    cur.execute(
        "SELECT binding_id FROM identity_bindings WHERE external_subject = %s "
        "AND status = 'active' AND platform_tenant_id <> %s FOR UPDATE", (SUBJECT, ORG),
    )
    if cur.fetchone():
        raise Refused("binding_drift")
    if row:
        cur.execute(
            "SELECT binding_id FROM identity_bindings WHERE platform_user_id = %s "
            "AND binding_id <> %s AND status = 'active' FOR UPDATE",
            (row["platform_user_id"], row["binding_id"]),
        )
        if cur.fetchone():
            raise Refused("binding_drift")
    return row


def _membership(cur, binding):
    if binding is None:
        return None
    cur.execute(
        "SELECT membership_id, org_id, project_id, role, status, invited_by_binding_id "
        "FROM project_member_bindings WHERE binding_id = %s AND status = 'active' "
        "FOR UPDATE", (binding["binding_id"],),
    )
    rows = cur.fetchall()
    if any(row["project_id"] != PROJECT or row["org_id"] != ORG for row in rows):
        raise Refused("other_project")
    if any(row["role"] != PROJECT_ROLE
           or row["invited_by_binding_id"] != binding["binding_id"]
           for row in rows) or len(rows) > 1:
        raise Refused("membership_drift")
    return rows[0] if rows else None


def _audit(cur, binding, membership, action):
    # Existing immutable membership lifecycle ledger, with service self provenance.
    payload = json.dumps({"provenance": PROVENANCE,
                          "membership_id": str(membership["membership_id"]),
                          "role": PROJECT_ROLE}, sort_keys=True)
    cur.execute(
        "INSERT INTO project_lifecycle_receipts "
        "(receipt_id, org_id, project_id, action, actor_binding_id, idempotency_key, "
        "input_digest, result_json) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
        (uuid4(), ORG, PROJECT, action, binding["binding_id"],
         f"{PROVENANCE}:{uuid4()}", hashlib.sha256(payload.encode()).hexdigest(), payload),
    )


def execute(mode):
    if os.environ.get("LEAF_RUNTIME_ENV") != "staging":
        raise Refused("environment")
    if mode not in {"plan", "apply", "revoke"}:
        raise Refused("arguments")
    store = _store()
    with store.cursor() as cur:
        cur.execute("SELECT org_id, status FROM orgs WHERE org_id = %s FOR UPDATE", (ORG,))
        org = cur.fetchone()
        if org is None or org["status"] != "active":
            raise Refused("org_inactive")
        cur.execute("SELECT org_id, status, deleted_at FROM projects "
                    "WHERE project_id = %s AND deleted_at IS NULL FOR UPDATE", (PROJECT,))
        project = cur.fetchone()
        if (project is None or project["org_id"] != ORG
                or project["status"] != "active" or project["deleted_at"] is not None):
            raise Refused("project")
        binding = _binding(cur)
        membership = _membership(cur, binding)
        if mode != "revoke" and binding is not None and membership is None:
            cur.execute(
                "SELECT membership_id FROM project_member_bindings "
                "WHERE binding_id = %s AND org_id = %s AND project_id = %s "
                "AND status <> 'active' FOR UPDATE",
                (binding["binding_id"], ORG, PROJECT),
            )
            if cur.fetchone():
                raise Refused("membership_revoked")
        if mode == "plan":
            return receipt(mode, "unchanged" if binding and membership else "would_create",
                           binding=binding, membership=membership)
        if mode == "revoke":
            if binding is None:
                return receipt(mode, "unchanged")
            if membership:
                cur.execute("UPDATE project_member_bindings SET status = 'revoked', "
                            "revoked_at = NOW() WHERE membership_id = %s AND org_id = %s "
                            "AND project_id = %s AND binding_id = %s AND role = %s "
                            "AND invited_by_binding_id = %s "
                            "AND status = 'active' RETURNING membership_id, role, status",
                            (membership["membership_id"], ORG, PROJECT,
                             binding["binding_id"], PROJECT_ROLE, binding["binding_id"]))
                membership = cur.fetchone()
                if membership is None or membership["status"] != "revoked":
                    raise Refused("readback")
                _audit(cur, binding, membership, "member_revoked")
            cur.execute("UPDATE identity_bindings SET status = 'revoked', revoked_at = NOW() "
                        "WHERE binding_id = %s AND platform_tenant_id = %s "
                        "AND external_authority = %s AND external_subject = %s "
                        "AND role = %s AND status = 'active' "
                        "RETURNING binding_id, platform_user_id, role, status",
                        (binding["binding_id"], ORG, AUTHORITY, SUBJECT, TENANT_ROLE))
            binding = cur.fetchone()
            if binding is None or binding["status"] != "revoked":
                raise Refused("readback")
            return receipt(mode, "revoked", binding=binding, membership=membership)
        if binding and membership:
            return receipt(mode, "unchanged", binding=binding, membership=membership)
        if binding is None:
            store._create_identity_binding_on_cursor(
                cur, ORG, AUTHORITY, SUBJECT, None, role=TENANT_ROLE, require_new=True)
            binding = _binding(cur)
            if binding is None:
                raise Refused("readback")
        if membership is None:
            cur.execute("INSERT INTO project_member_bindings "
                        "(membership_id, org_id, project_id, binding_id, role, invited_by_binding_id) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (uuid4(), ORG, PROJECT, binding["binding_id"], PROJECT_ROLE,
                         binding["binding_id"]))
            membership = _membership(cur, binding)
            if membership is None:
                raise Refused("readback")
            _audit(cur, binding, membership, "member_invited")
        return receipt(mode, "created", binding=binding, membership=membership)


def main(argv=None):
    mode = "plan"
    try:
        mode = parse_mode(sys.argv[1:] if argv is None else argv)
        result = execute(mode)
    except Refused as exc:
        result = receipt(mode, "refused", str(exc))
    except Exception:
        # Connection failures and DB exceptions can contain credentials or SQL rows.
        result = receipt(mode, "refused", "database")
    print(PREFIX + json.dumps(result, sort_keys=True))
    return 2 if result["outcome"] == "refused" else 0


if __name__ == "__main__":
    pkg = Path(__file__).resolve().parent
    if "leaf_platform" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "leaf_platform", pkg / "__init__.py", submodule_search_locations=[str(pkg)])
        module = importlib.util.module_from_spec(spec)
        sys.modules["leaf_platform"] = module
        spec.loader.exec_module(module)
    __package__ = "leaf_platform"
    raise SystemExit(main())
