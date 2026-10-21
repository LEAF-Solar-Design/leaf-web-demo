"""Read-only app posture probe, usable before platform migrations are applied."""
from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import re
import sys


_SERVER_DIR = str(Path(__file__).resolve().parent)
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)


def validate_postgres_required():
    link = importlib.import_module("platform_link")
    if (os.environ.get("LEAF_RUNTIME_ENV", "").strip().lower() == "production"
            and not link.postgres_required()):
        raise RuntimeError("LEAF_PLATFORM_POSTGRES_REQUIRED")


def auth_live():
    link = importlib.import_module("platform_link")
    live = importlib.import_module("deps").auth_live()
    if link.postgres_required() and not live:
        raise RuntimeError("LEAF_AUTH_LIVE")


def validate_database_url():
    link = importlib.import_module("platform_link")
    if link.postgres_startup_required() and not os.environ.get("DATABASE_URL", "").strip():
        raise RuntimeError("DATABASE_URL")


# Import each dependency inside its own check: an import failure must not hide
# the remaining posture failures. None of these modules mounts app routers.
_CHECKS = (
    (None, "validate_postgres_required"),
    ("platform_link", "postgres_authorities_selected"),
    ("platform_link", "validate_canonical_upload_authority"),
    ("platform_link", "validate_session_annex_authority"),
    (None, "auth_live"),
    (None, "validate_database_url"),
    ("jobs", "validate_store_startup"),
    ("guest_uploads", "upload_store_mode"),
    ("write_loop", "blob_store_mode"),
)


def _schema_status():
    db = importlib.import_module("platform_link").platform_db()
    status = dict(db.schema_status())
    # schema_status compares hashes only for known names. Check the ledger for
    # migrations from a newer/different image as well, without applying any.
    unknown = []
    if db._MIGRATION_LEDGER_TABLE not in status.get("missing", {}):
        expected = {item["name"] for item in db.migration_manifest()}
        with db.get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT name FROM {db._MIGRATION_LEDGER_TABLE} ORDER BY name")
                unknown = sorted(row["name"] for row in cur.fetchall()
                                 if row["name"] not in expected)
    status["unknown_migrations"] = unknown
    return status


def _migrations_dir():
    db = importlib.import_module("platform_link").platform_db()
    return db._PKG_DIR / "migrations"


def _catalog_candidates(status, catalog):
    candidates = {}
    for kind in ("constraints", "indexes", "triggers"):
        key = f"invalid_{kind}"
        for entry in status.get(key, []):
            name, _, reason = entry.partition(":")
            if reason == "missing-or-wrong-relation":
                relation = catalog.get(kind, {}).get(name, {}).get("relation")
                if isinstance(relation, str) and relation:
                    candidates[(key, entry)] = relation
    return candidates


def _catalog_relation_absence(status):
    # No catalog or database lookup is needed for other diagnostic reasons.
    if not any(entry.partition(":")[2] == "missing-or-wrong-relation"
               for kind in ("constraints", "indexes", "triggers")
               for entry in status.get(f"invalid_{kind}", [])):
        return {}, set()
    try:
        db = importlib.import_module("platform_link").platform_db()
        catalog = db.required_catalog_for_selected_authorities(os.environ)
        names = sorted(set(_catalog_candidates(status, catalog).values()))
        absent = set()
        if names:
            with db.get_pool().connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT name FROM unnest(%(names)s::text[]) AS name "
                        "WHERE to_regclass(quote_ident(current_schema()) || '.' || "
                        "quote_ident(name)) IS NULL",
                        {"names": names},
                    )
                    absent = {row["name"] for row in cur.fetchall()}
        return catalog, absent
    except Exception:
        # An unavailable catalog or relation lookup cannot explain a defect.
        return {}, set()


def _unexplained_defects(status, pending, *, catalog=None, absent_relations=None):
    texts = []
    raw_texts = []
    if len(pending) > 64:
        raise ValueError("pending migration limit")
    if pending:
        directory = _migrations_dir()
        for name in pending:
            path = directory / name
            if path.parent != directory:
                raise ValueError("invalid migration name")
            with path.open("rb") as migration:
                limit = 2 * 1024 * 1024
                if os.fstat(migration.fileno()).st_size > limit:
                    raise ValueError("migration size limit")
                contents = migration.read(limit)
                if os.fstat(migration.fileno()).st_size > limit:
                    raise ValueError("migration size limit")
            raw_texts.append(contents.decode("utf-8"))
            texts.append(raw_texts[-1].replace('"', ''))

    explained_catalog = set()
    try:
        for candidate, relation in _catalog_candidates(status, catalog or {}).items():
            if absent_relations is None or relation not in absent_relations:
                continue
            identifier = re.escape(relation)
            create = re.compile(
                r'(?<![A-Za-z0-9_])CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
                r'(?:(?:public|"public")\s*\.\s*)?'
                + r'(?:"' + identifier + r'"|' + identifier + r')'
                + r'(?![A-Za-z0-9_$"])(?!\s*\.)',
                re.IGNORECASE | re.ASCII,
            )
            if any(create.search(text) for text in raw_texts):
                explained_catalog.add(candidate)
    except Exception:
        explained_catalog.clear()

    defects = [(f"{table}.{column}", (table, column))
               for table, columns in status.get("missing", {}).items()
               for column in columns]
    for key, entries in status.items():
        if (key.startswith(("invalid_", "missing_"))
                and key != "missing_migrations" and isinstance(entries, list)):
            # Catalog entries may append a diagnostic reason after the name.
            defects.extend((entry, (entry.split(":", 1)[0],))
                           for entry in entries if (key, entry) not in explained_catalog)
    unexplained = []
    for label, identifiers in defects:
        patterns = [re.compile(r"(?<![A-Za-z0-9_])" + re.escape(identifier.replace('"', ''))
                               + r"(?![A-Za-z0-9_])", re.IGNORECASE | re.ASCII)
                    for identifier in identifiers]
        if not any(all(pattern.search(text) for pattern in patterns) for text in texts):
            unexplained.append(label)
    return sorted(unexplained)


def run(env, schema_status=None) -> int:
    """Emit one credential-free JSON result; pending migrations are permitted."""
    failures = []
    schema = {"pending": [], "drift": [], "unexplained": []}
    previous_env = dict(os.environ)
    previous_path = list(sys.path)

    def failed(name):
        # Never copy exception text: validators/providers can embed arbitrary
        # values, including secrets or URLs, in their diagnostics.
        failures.append({"check": name, "error": "startup validation failed"})

    try:
        os.environ.clear()
        os.environ.update(env)
        # Dependencies may print during import. Only this probe's JSON belongs
        # on stdout, including when a validator fails after printing a secret.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            for module, name in _CHECKS:
                try:
                    function = (getattr(importlib.import_module(module), name)
                                if module else globals()[name])
                    function()
                except Exception:
                    failed(name)
            try:
                status = (schema_status or _schema_status)()
                schema["pending"] = list(status["missing_migrations"])
                schema["drift"] = sorted(set(status["migration_hash_drift"])
                                         | set(status.get("unknown_migrations", [])))
                if schema["pending"]:
                    catalog, absent = _catalog_relation_absence(status)
                    schema["unexplained"] = _unexplained_defects(
                        status, schema["pending"], catalog=catalog, absent_relations=absent)
                if (schema["drift"] or (not schema["pending"] and not status["ok"])
                        or schema["unexplained"]):
                    failed("schema_status")
            except Exception:
                failed("schema_status")
    finally:
        os.environ.clear()
        os.environ.update(previous_env)
        # deps installs sibling import paths; don't leave /app ahead of stdlib.
        sys.path[:] = previous_path
    code = 10 if failures else 0
    print(json.dumps({"probe": "boot", "ok": not failures, "exit": code,
                      "failures": failures, "schema": schema}))
    return code


def main():
    sys.exit(run(dict(os.environ)))


if __name__ == "__main__":
    main()
