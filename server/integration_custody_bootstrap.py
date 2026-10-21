"""Fixed-purpose, separately authenticated Studio custody bootstrap.

Only the two packaged custody migrations are admitted. This module is a one-shot
operator CLI, never a web-process pool or credential resolver.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
from hashlib import sha256
import json
import os
from pathlib import Path


MIGRATIONS = (
    "0079_integration_turn_grants.sql",
    "0080_integration_turn_grant_action_discovery.sql",
)
RUNTIME_ROLE = "leaf_integration_grant_app"
_ROLE_COLUMNS = ("rolcanlogin,rolsuper,rolbypassrls,rolcreaterole,rolcreatedb,rolreplication,"
                 "rolinherit,rolconnlimit,rolvaliduntil,rolconfig")


class BootstrapRefused(Exception):
    """A fixed, public receipt code; never contains database error text."""


def _require(condition, code):
    if not condition:
        raise BootstrapRefused(code)


def _configuration(environ):
    names = ("MASTER_USERNAME", "MASTER_PASSWORD", "APP_USERNAME", "APP_PASSWORD",
             "DB_HOST", "DB_NAME")
    _require(all(environ.get(name) for name in names), "CONFIGURATION_INVALID")
    port = environ.get("DB_PORT", "5432")
    _require(port.isascii() and port.isdecimal() and 1 <= int(port) <= 65535,
             "CONFIGURATION_INVALID")
    sslmode = environ.get("DB_SSLMODE", "require")
    _require(sslmode in ("require", "verify-ca", "verify-full") or (
        sslmode == "disable" and environ["DB_HOST"] in ("127.0.0.1", "::1", "localhost")),
        "CONFIGURATION_INVALID")
    _require(environ["MASTER_USERNAME"] != environ["APP_USERNAME"], "IDENTITIES_UNSAFE")
    # Keyword parameters prevent credentials or host values from becoming DSN options.
    common = dict(host=environ["DB_HOST"], dbname=environ["DB_NAME"], port=int(port),
                  sslmode=sslmode, connect_timeout=5,
                  options="-c statement_timeout=10000 -c lock_timeout=3000 "
                          "-c idle_in_transaction_session_timeout=15000 "
                          "-c search_path=leaf_platform,public")
    return common


def _role(conn, name):
    return conn.execute("SELECT " + _ROLE_COLUMNS + " FROM pg_roles WHERE rolname=%s",
                        (name,)).fetchone()


def _member(conn, member, owner):
    return conn.execute("SELECT pg_has_role(%s, %s, 'MEMBER') AS member",
                        (member, owner)).fetchone()["member"]


def _runtime_role(conn, master):
    role = _role(conn, RUNTIME_ROLE)
    if role is None:
        return
    _require(not any(role[key] for key in (
        "rolcanlogin", "rolsuper", "rolbypassrls", "rolcreaterole", "rolcreatedb",
        "rolreplication")), "RUNTIME_ROLE_UNSAFE")
    _require(not _member(conn, RUNTIME_ROLE, master), "RUNTIME_ROLE_UNSAFE")
    elevated = conn.execute("""SELECT EXISTS (
        SELECT 1 FROM pg_roles WHERE (rolsuper OR rolbypassrls OR rolcreaterole
          OR rolcreatedb OR rolreplication OR rolcanlogin)
          AND pg_has_role(%s, oid, 'MEMBER')) AS unsafe""", (RUNTIME_ROLE,)).fetchone()
    _require(not elevated["unsafe"], "RUNTIME_ROLE_UNSAFE")


def _ledger(app, steps):
    drift = False
    for step in steps:
        row = app.execute("SELECT sha256 FROM leaf_platform.leaf_schema_migrations "
                          "WHERE name=%s", (step["name"],)).fetchone()
        step["state"] = ("pending" if row is None else
                         "applied" if row["sha256"] == step["sha256"] else "drift")
        drift = drift or step["state"] == "drift"
    _require(not drift, "MIGRATION_HASH_DRIFT")
    _require(steps[0]["state"] == "applied" or steps[1]["state"] == "pending",
             "MIGRATION_ORDER_UNSAFE")


def _preflight(master, app, environ, steps):
    for conn, expected in ((master, environ["MASTER_USERNAME"]), (app, environ["APP_USERNAME"])):
        row = conn.execute("SELECT current_database() AS db, current_user AS identity, "
                           "session_user AS login").fetchone()
        _require(row == dict(db=environ["DB_NAME"], identity=expected, login=expected),
                 "IDENTITIES_UNSAFE")
    master_name, app_name = environ["MASTER_USERNAME"], environ["APP_USERNAME"]
    role = _role(app, app_name)
    _require(role and role["rolcanlogin"] and not any(role[key] for key in (
        "rolsuper", "rolbypassrls", "rolcreaterole", "rolcreatedb", "rolreplication")),
        "APP_ROLE_UNSAFE")
    _require(not _member(app, app_name, master_name), "OWNER_MEMBERSHIP_UNSAFE")
    ownership = app.execute("""SELECT n.nspowner = r.oid AS schema_owned,
        c.relowner = r.oid AND c.relkind='r' AS ledger_owned
        FROM pg_namespace n JOIN pg_class c ON c.relnamespace=n.oid
        CROSS JOIN pg_roles r WHERE n.nspname='leaf_platform'
        AND c.relname='leaf_schema_migrations' AND r.rolname=current_user""").fetchone()
    _require(ownership and all(ownership.values()), "APP_LEDGER_REQUIRED")
    _ledger(app, steps)
    table = app.execute("""SELECT r.rolname AS owner FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_roles r ON r.oid=c.relowner
        WHERE n.nspname='public' AND c.relname='integration_turn_grants'""").fetchone()
    _require(not table or (steps[0]["state"] == "applied" and table["owner"] == master_name),
             "CUSTODY_OWNERSHIP_UNSAFE")
    _require(table or steps[0]["state"] == "pending", "CUSTODY_TABLE_MISSING")
    function = app.execute("""SELECT r.rolname AS owner FROM pg_proc p
        JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_roles r ON r.oid=p.proowner
        WHERE n.nspname='public' AND p.proname='guard_integration_turn_grant'
        AND p.pronargs=0""").fetchone()
    _require(not function or (table and function["owner"] == master_name),
             "CUSTODY_OWNERSHIP_UNSAFE")
    _require(not table or function, "CUSTODY_FUNCTION_MISSING")
    _runtime_role(app, master_name)
    return role


def _access(app, master_name):
    """Record effective access, so cleanup never revokes an original privilege."""
    needed = []
    for target, privileges in (("SCHEMA leaf_platform", ("USAGE", "CREATE")),
                               ("TABLE leaf_platform.leaf_schema_migrations", ("SELECT", "INSERT"))):
        function = "has_schema_privilege" if target.startswith("SCHEMA") else "has_table_privilege"
        object_name = target.split(" ", 1)[1]
        for privilege in privileges:
            exists = app.execute("SELECT " + function + "(%s,%s,%s) AS allowed",
                                 (master_name, object_name, privilege)).fetchone()["allowed"]
            if not exists:
                needed.append((target, privilege))
    return needed


def _acl(app, master_name, target, privilege, verb):
    from psycopg import sql
    # target/privilege/verb are exclusively fixed internal constants.
    app.execute(sql.SQL(verb + " " + privilege + " ON " + target
                        + (" TO " if verb == "GRANT" else " FROM ") + "{}").format(
                            sql.Identifier(master_name)))


class _MasterPool:
    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def connection(self):
        # The canonical executor retains its lock, ledger and transaction boundary.
        with self.conn.transaction():
            yield self.conn


def _verify(app, master_name, original_role, steps):
    from leaf_platform.integration_turn_grant_store import _GUARD
    _ledger(app, steps)
    _require(all(step["state"] == "applied" for step in steps), "MIGRATIONS_INCOMPLETE")
    _require(_role(app, app.info.user) == original_role, "APP_ROLE_CHANGED")
    _require(not _member(app, app.info.user, master_name), "OWNER_MEMBERSHIP_UNSAFE")
    _runtime_role(app, master_name)
    row = app.execute(_GUARD).fetchone()
    _require(row and row["safe"] is True, "RUNTIME_GUARD_UNSAFE")
    privileges = app.execute("""SELECT has_schema_privilege(current_user,'public','USAGE') AS usage,
        has_table_privilege(current_user,'public.integration_turn_grants','SELECT') AS sel,
        has_table_privilege(current_user,'public.integration_turn_grants','INSERT') AS ins,
        has_table_privilege(current_user,'public.integration_turn_grants','UPDATE') AS upd,
        has_table_privilege(current_user,'public.integration_turn_grants','DELETE') AS del,
        has_table_privilege(current_user,'public.integration_turn_grants','TRUNCATE') AS trunc""").fetchone()
    _require(all(privileges[key] for key in ("usage", "sel", "ins", "upd"))
             and not privileges["del"] and not privileges["trunc"], "RUNTIME_PRIVILEGES_UNSAFE")


def bootstrap(mode="inspect", environ=None):
    """Return a sanitized receipt, including committed partial migration progress."""
    receipt = dict(mode=mode, ok=False, ready=False, steps=[])
    try:
        _require(mode in ("inspect", "apply"), "CONFIGURATION_INVALID")
        environ = os.environ if environ is None else environ
        common = _configuration(environ)
        import psycopg
        from psycopg.rows import dict_row
        from server.platform_link import platform_db
        db = platform_db()
        paths = [Path(__file__).resolve().parents[1] / "platform" / "migrations" / name
                 for name in MIGRATIONS]
        receipt["steps"] = [dict(name=path.name, sha256=sha256(path.read_bytes()).hexdigest(),
                                 state="unchecked") for path in paths]
        def connect(kind):
            return psycopg.connect(**common, user=environ[kind + "_USERNAME"],
                                   password=environ[kind + "_PASSWORD"],
                                   autocommit=True, row_factory=dict_row)
        with ExitStack() as stack:
            master = stack.enter_context(connect("MASTER"))
            app = stack.enter_context(connect("APP"))
            original_role = _preflight(master, app, environ, receipt["steps"])
            if mode == "apply":
                added = []
                previous_pool = db._pool
                try:
                    if any(step["state"] == "pending" for step in receipt["steps"]):
                        for target, privilege in _access(app, environ["MASTER_USERNAME"]):
                            _acl(app, environ["MASTER_USERNAME"], target, privilege, "GRANT")
                            added.append((target, privilege))
                    db._pool = _MasterPool(master)
                    for path, step in zip(paths, receipt["steps"]):
                        if step["state"] == "pending":
                            db.apply_migration(path)
                            step["state"] = "applied"
                            _ledger(app, receipt["steps"])
                    from psycopg import sql
                    if not _member(app, environ["APP_USERNAME"], RUNTIME_ROLE):
                        master.execute(sql.SQL("GRANT leaf_integration_grant_app TO {}").format(
                            sql.Identifier(environ["APP_USERNAME"])))
                finally:
                    db._pool = previous_pool
                    # Try every cleanup even if an earlier REVOKE fails.
                    cleanup_failed = False
                    for target, privilege in reversed(added):
                        try:
                            _acl(app, environ["MASTER_USERNAME"], target, privilege, "REVOKE")
                        except Exception:
                            cleanup_failed = True
                    _require(not cleanup_failed, "TEMPORARY_ACCESS_CLEANUP_FAILED")
            if all(step["state"] == "applied" for step in receipt["steps"]):
                with connect("APP") as fresh:
                    _preflight(master, fresh, environ, receipt["steps"])
                    _verify(fresh, environ["MASTER_USERNAME"], original_role, receipt["steps"])
                receipt["ready"] = True
            receipt["ok"] = True
    except BootstrapRefused as exc:
        receipt["error"] = str(exc)
    except Exception:
        receipt["error"] = "BOOTSTRAP_DATABASE_OR_PACKAGE_FAILURE"
    return receipt


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise BootstrapRefused("ARGUMENTS_INVALID")


def main(argv=None):
    try:
        parser = _Parser(description=__doc__, add_help=False, allow_abbrev=False)
        parser.add_argument("--mode", choices=("inspect", "apply"), default="inspect")
        args = parser.parse_args(argv)
        receipt = bootstrap(args.mode)
    except Exception:
        receipt = dict(ok=False, ready=False, error="ARGUMENTS_INVALID", steps=[])
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
