"""Out-of-band operator identity-binding seed (contract/OPERATOR.md provisioning gap).

The operator's own verified JWT needs an ACTIVE ``identity_bindings`` row so
``server/deps.py``'s ``resolve_active_platform_tenant_authority`` resolves a
platform tenant for it (contract/OPERATOR.md ties operator authority to
``operator_principals``, but the *tenant* leg of ``require_tenant`` still
requires this binding regardless). Staging readiness additionally asserts
``role == 'read_only'`` (diagnose-leaf-platform-staging-db.yml,
bind-leaf-platform-staging-synthetic-user-pair.yml) as a deliberate
least-privilege posture: the operator principal should not also be a tenant
owner. The runtime itself never inspects this role
(server/operator_deps.py checks only the operator_principals row).

Nothing provisions this binding for a subject like the operator:
- first-login auto-provision (``ensure_org_with_identity``) always writes
  role='owner';
- the pair route (``bind_identity_pair`` / POST .../identity-bindings/pair)
  takes exactly two subjects and is scoped to the synthetic tenant-A/B pair.

This script is the third, out-of-band writer, scoped to a single subject at
a time. Runs with a direct DATABASE_URL -- never through the app, matching
scripts/operator_principal_admin.py's convention.

Idempotent: an existing ACTIVE binding for the subject is reported, never
mutated. Changing an existing binding's role is a separate, explicit
decision this tool does not make (there is no UPDATE path here).

This script does NOT touch Auth0. After a successful `seed`, the printed
org_id must be set as Auth0 app_metadata.leaf_platform_tenant_id on that
subject out of band, or the verified JWT still won't carry the
`https://leafdesign.ai/tenant_id` claim server/auth.py's
extract_tenant_claims requires.

Usage (prints JSON):
  python scripts/operator_identity_binding_seed.py seed <subject> [--org-name NAME] [--tier hosted_starter]
  python scripts/operator_identity_binding_seed.py show <subject>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid

import psycopg
from psycopg.errors import UniqueViolation

_SUBJECT_RE = re.compile(r"^auth0\|[A-Za-z0-9._~+\-]{1,249}$")


def _connect() -> psycopg.Connection:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required (direct DB credential; the app "
              "never runs this tool)", file=sys.stderr)
        raise SystemExit(2)
    return psycopg.connect(url)


def _validate_subject(subject: str) -> str:
    if not _SUBJECT_RE.fullmatch(subject):
        raise SystemExit("a canonical Auth0 subject (auth0|...) is required")
    return subject


def _row_json(row) -> dict:
    keys = ("binding_id", "external_authority", "external_subject", "platform_tenant_id",
            "platform_user_id", "role", "status", "created_at")
    return {k: str(v) for k, v in zip(keys, row)}


def _existing_active_binding(cur, subject: str):
    cur.execute(
        "SELECT binding_id, external_authority, external_subject, platform_tenant_id, "
        "platform_user_id, role, status, created_at FROM identity_bindings "
        "WHERE external_authority = 'auth0' AND external_subject = %s AND status = 'active'",
        (subject,))
    return cur.fetchone()


def cmd_show(args: argparse.Namespace) -> int:
    subject = _validate_subject(args.subject)
    with _connect() as conn, conn.cursor() as cur:
        row = _existing_active_binding(cur, subject)
        print(json.dumps(
            {"subject": subject, "exists": row is not None,
             **({"binding": _row_json(row)} if row else {})},
            default=str))
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    subject = _validate_subject(args.subject)
    with _connect() as conn, conn.cursor() as cur:
        existing = _existing_active_binding(cur, subject)
        if existing is not None:
            print(json.dumps(
                {"subject": subject, "created": False, "binding": _row_json(existing),
                 "note": "an active binding already exists; role was NOT changed"},
                default=str))
            return 0

        org_id, binding_id, user_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        try:
            cur.execute(
                "INSERT INTO orgs (org_id, name, tier) VALUES (%(org_id)s, %(name)s, %(tier)s) "
                "RETURNING org_id, name, tier, status, created_at",
                {"org_id": org_id, "name": args.org_name, "tier": args.tier})
            org_row = cur.fetchone()
            cur.execute(
                "INSERT INTO identity_bindings (binding_id, external_authority, external_subject, "
                "platform_tenant_id, platform_user_id, role) VALUES "
                "(%(binding_id)s, 'auth0', %(subject)s, %(org_id)s, %(user_id)s, 'read_only') "
                "RETURNING binding_id, external_authority, external_subject, platform_tenant_id, "
                "platform_user_id, role, status, created_at",
                {"binding_id": binding_id, "subject": subject, "org_id": org_id, "user_id": user_id})
            binding_row = cur.fetchone()
        except UniqueViolation:
            conn.rollback()
            with conn.cursor() as cur2:
                existing = _existing_active_binding(cur2, subject)
            print(json.dumps(
                {"subject": subject, "created": False,
                 "binding": _row_json(existing) if existing else None,
                 "note": "a concurrent writer created this binding first; role was NOT changed"},
                default=str))
            return 0

    print(json.dumps({
        "subject": subject, "created": True,
        "org": {"org_id": str(org_id), "name": org_row[1], "tier": org_row[2]},
        "binding": _row_json(binding_row),
        "next_step": (
            f"set Auth0 app_metadata.leaf_platform_tenant_id = \"{org_id}\" on this subject "
            "out of band (e.g. auth0 api patch \"users/<user_id>\" --data "
            f"'{{\"app_metadata\":{{\"leaf_platform_tenant_id\":\"{org_id}\"}}}}'); "
            "this script does not touch Auth0"),
    }, default=str))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed")
    seed.add_argument("subject")
    seed.add_argument("--org-name", default="Staging Operator")
    seed.add_argument("--tier", default="hosted_starter")
    seed.set_defaults(func=cmd_seed)

    show = sub.add_parser("show")
    show.add_argument("subject")
    show.set_defaults(func=cmd_show)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
