"""Out-of-band operator principal administration (contract/OPERATOR.md §1).

The ONLY writer of ``operator_principals``. Runs with a direct DATABASE_URL —
never through the app. Every mutation bumps ``role_revision``, so in-flight
approvals and execution authorities bound to the prior revision deny at
redemption.

Usage (all commands print the resulting row as JSON):
  python scripts/operator_principal_admin.py grant   <subject> [--profiles default] [--environment staging] [--granted-by <who>]
  python scripts/operator_principal_admin.py suspend <subject>
  python scripts/operator_principal_admin.py resume  <subject>
  python scripts/operator_principal_admin.py revoke  <subject>
  python scripts/operator_principal_admin.py list
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg


def _connect() -> psycopg.Connection:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required (direct DB credential; the app "
              "never runs this tool)", file=sys.stderr)
        raise SystemExit(2)
    return psycopg.connect(url)


def _row_json(cur, subject: str) -> str:
    cur.execute(
        "SELECT subject, role, role_revision, status, profiles, environment,"
        " granted_by, granted_at, updated_at FROM operator_principals"
        " WHERE subject = %s", (subject,))
    row = cur.fetchone()
    if row is None:
        return json.dumps({"subject": subject, "exists": False})
    keys = ("subject", "role", "role_revision", "status", "profiles",
            "environment", "granted_by", "granted_at", "updated_at")
    return json.dumps({k: (str(v) if k.endswith("_at") else v)
                       for k, v in zip(keys, row)}, default=str)


def cmd_grant(args: argparse.Namespace) -> int:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO operator_principals
              (subject, role, role_revision, status, profiles, environment,
               granted_by, updated_at)
            VALUES (%s, 'operator', 1, 'active', %s, %s, %s, now())
            ON CONFLICT (subject) DO UPDATE SET
              status = 'active',
              profiles = EXCLUDED.profiles,
              environment = EXCLUDED.environment,
              granted_by = EXCLUDED.granted_by,
              role_revision = operator_principals.role_revision + 1,
              updated_at = now()
            """,
            (args.subject, args.profiles.split(","), args.environment,
             args.granted_by))
        print(_row_json(cur, args.subject))
    return 0


def _set_status(subject: str, status: str) -> int:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE operator_principals SET status = %s,"
            " role_revision = role_revision + 1, updated_at = now()"
            " WHERE subject = %s", (status, subject))
        if cur.rowcount == 0:
            print(json.dumps({"subject": subject, "exists": False}))
            return 1
        print(_row_json(cur, subject))
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT subject, role, role_revision, status, profiles,"
            " environment FROM operator_principals ORDER BY subject")
        rows = [{"subject": r[0], "role": r[1], "role_revision": r[2],
                 "status": r[3], "profiles": r[4], "environment": r[5]}
                for r in cur.fetchall()]
        print(json.dumps({"principals": rows}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    grant = sub.add_parser("grant")
    grant.add_argument("subject")
    grant.add_argument("--profiles", default="default")
    grant.add_argument("--environment", default="staging",
                       choices=("staging", "production"))
    grant.add_argument("--granted-by", default=None)
    grant.set_defaults(func=cmd_grant)

    for name, status in (("suspend", "suspended"), ("resume", "active"),
                         ("revoke", "revoked")):
        p = sub.add_parser(name)
        p.add_argument("subject")
        p.set_defaults(func=lambda a, s=status: _set_status(a.subject, s))

    lst = sub.add_parser("list")
    lst.set_defaults(func=cmd_list)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
