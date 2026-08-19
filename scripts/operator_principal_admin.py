"""Out-of-band operator principal administration (contract/OPERATOR.md §1).

The ONLY writer of ``operator_principals``. Runs with a direct DATABASE_URL —
never through the app. Every mutation bumps ``role_revision``, so in-flight
approvals and execution authorities bound to the prior revision deny at
redemption.

Transition guards (contract §1.3 — fails closed on anything else):
  grant    -> creates an active row; touching an existing suspended/revoked row
              requires --reactivate, and profiles/environment are rewritten only
              when passed explicitly (a bare re-grant never resets scope).
  suspend  -> active -> suspended only.
  resume   -> suspended -> active only (never un-revokes).
  revoke   -> terminal from active/suspended; idempotent on a revoked row.

Usage (all commands print the resulting row as JSON; non-zero exit on refusal):
  python scripts/operator_principal_admin.py grant   <subject> --granted-by <who> [--profiles a,b] [--environment staging|production] [--reactivate]
  python scripts/operator_principal_admin.py suspend <subject>
  python scripts/operator_principal_admin.py resume  <subject>
  python scripts/operator_principal_admin.py revoke  <subject>
  python scripts/operator_principal_admin.py list
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

# Shared URL sourcing/validation (env + platform/.env.local, malformed-URL
# rejection) — the one DB bootstrap path, not a private re-implementation.
# Loaded by file path: the repo's platform/ package shadows the stdlib name,
# and dependency imports may already have cached stdlib `platform`.
_DB_HELPER = Path(__file__).resolve().parents[1] / "platform" / "db.py"
_spec = importlib.util.spec_from_file_location("_leaf_platform_db", _DB_HELPER)
assert _spec is not None and _spec.loader is not None, _DB_HELPER
_leaf_db = importlib.util.module_from_spec(_spec)
sys.modules["_leaf_platform_db"] = _leaf_db
_spec.loader.exec_module(_leaf_db)
get_database_url = _leaf_db.get_database_url
validate_database_url = _leaf_db.validate_database_url


def _connect() -> psycopg.Connection:
    try:
        url = get_database_url()
        validate_database_url(url)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    # Direct, single-shot connection: this tool must work with a bare operator
    # credential, without the app's pool or its lifecycle.
    return psycopg.connect(url, row_factory=dict_row, prepare_threshold=None)


def _print_row(cur: psycopg.Cursor, subject: str) -> None:
    cur.execute(
        "SELECT subject, role, role_revision, status, profiles, environment,"
        " granted_by, granted_at, updated_at FROM operator_principals"
        " WHERE subject = %s", (subject,))
    row = cur.fetchone()
    if row is None:
        print(json.dumps({"subject": subject, "exists": False}))
        return
    print(json.dumps(dict(row), default=str))


def _refuse(subject: str, current: str | None, why: str) -> int:
    print(json.dumps({"subject": subject, "refused": why,
                      "current_status": current}), file=sys.stderr)
    return 1


def _parse_profiles(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    profiles = [p.strip() for p in raw.split(",") if p.strip()]
    if not profiles:
        print("--profiles must name at least one non-empty profile",
              file=sys.stderr)
        raise SystemExit(2)
    return profiles


def cmd_grant(args: argparse.Namespace) -> int:
    profiles = _parse_profiles(args.profiles)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM operator_principals WHERE subject = %s"
            " FOR UPDATE", (args.subject,))
        row = cur.fetchone()
        if row is None:
            cur.execute(
                """
                INSERT INTO operator_principals
                  (subject, role, role_revision, status, profiles, environment,
                   granted_by, updated_at)
                VALUES (%s, 'operator', 1, 'active', %s, %s, %s, now())
                """,
                (args.subject, profiles or ["default"],
                 args.environment or "staging", args.granted_by))
        else:
            if row["status"] != "active" and not args.reactivate:
                return _refuse(
                    args.subject, row["status"],
                    f"grant over a {row['status']} principal requires"
                    " --reactivate (contract/OPERATOR.md §1.3)")
            # Scope fields change only when passed explicitly: a bare re-grant
            # bumps the revision and re-attributes, never resets scope.
            cur.execute(
                """
                UPDATE operator_principals SET
                  status = 'active',
                  profiles = COALESCE(%s, profiles),
                  environment = COALESCE(%s, environment),
                  granted_by = %s,
                  role_revision = role_revision + 1,
                  updated_at = now()
                WHERE subject = %s
                """,
                (profiles, args.environment, args.granted_by, args.subject))
        _print_row(cur, args.subject)
    return 0


# Legal transitions per contract §1.3; anything else fails closed.
_TRANSITIONS = {
    "suspend": {"from": ("active",), "to": "suspended", "idempotent_on": ()},
    "resume": {"from": ("suspended",), "to": "active", "idempotent_on": ()},
    "revoke": {"from": ("active", "suspended"), "to": "revoked",
               "idempotent_on": ("revoked",)},
}


def _set_status(subject: str, command: str) -> int:
    rule = _TRANSITIONS[command]
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM operator_principals WHERE subject = %s"
            " FOR UPDATE", (subject,))
        row = cur.fetchone()
        if row is None:
            print(json.dumps({"subject": subject, "exists": False}),
                  file=sys.stderr)
            return 1
        current = row["status"]
        if current in rule["idempotent_on"]:
            _print_row(cur, subject)
            return 0
        if current not in rule["from"]:
            return _refuse(
                subject, current,
                f"{command} applies only to {'/'.join(rule['from'])} principals"
                " (contract/OPERATOR.md §1.3)")
        cur.execute(
            "UPDATE operator_principals SET status = %s,"
            " role_revision = role_revision + 1, updated_at = now()"
            " WHERE subject = %s", (rule["to"], subject))
        _print_row(cur, subject)
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT subject, role, role_revision, status, profiles,"
            " environment, granted_by FROM operator_principals ORDER BY subject")
        print(json.dumps({"principals": [dict(r) for r in cur.fetchall()]},
                         default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    grant = sub.add_parser("grant")
    grant.add_argument("subject")
    # None (not "default"/"staging") so an existing row's scope is only
    # rewritten when the operator passes the flag explicitly.
    grant.add_argument("--profiles", default=None)
    grant.add_argument("--environment", default=None,
                       choices=("staging", "production"))
    grant.add_argument("--granted-by", required=True,
                       help="accountable human authorizing this grant"
                            " (schema NOT NULL; contract/OPERATOR.md §1.4)")
    grant.add_argument("--reactivate", action="store_true",
                       help="explicitly allow re-activating a suspended or"
                            " revoked principal")
    grant.set_defaults(func=cmd_grant)

    for name in ("suspend", "resume", "revoke"):
        p = sub.add_parser(name)
        p.add_argument("subject")
        p.set_defaults(func=lambda a, c=name: _set_status(a.subject, c))

    lst = sub.add_parser("list")
    lst.set_defaults(func=cmd_list)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
