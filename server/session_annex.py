"""Shared store-mode plumbing for the per-session annex tables.

`server/checkpoints.py` and `server/session_policy.py` each own one table that
0012 never gave a PostgreSQL home. They share a selector, a mode vocabulary, a
PostgreSQL loader and a shadow comparator; only their queries differ. Those
shared parts live here so the two cannot drift apart, and so a third annex table
inherits them for free.

WHY ITS OWN SELECTOR, AND NOT `LEAF_SESSIONS_STORE`. Both were arguable and the
tradeoff is recorded in docs/POSTGRES-CUTOVER.md. The deciding reason is
`platform/db.py._AUTHORITY_SELECTORS`: schema readiness is keyed by selector
VALUE, so reusing the sessions selector would make these two tables required the
moment sessions reaches `dual_write` -- retroactively failing
`assert_schema_current()` for the configuration staging is already deployed on.
A code change must not move the readiness verdict for a running config. The
repository also owns exactly one selector per authority, and the inventory
contract treats a twice-owned selector as an error.

The harmful COMBINATION that sharing a selector would have made unrepresentable
is `sessions=postgres` with `annex=legacy` -- a session that outlives its own
checkpoints, which is the user-visible regression this module exists to prevent.
That is instead declared in `platform/authority-inventory.json` as a selector
dependency, the same mechanism LEAF_UPLOAD_STORE -> LEAF_DRAWING_STORE already
uses, so it is checked at review time rather than in a live request.

MODES, and what `shadow` does NOT do. The vocabulary matches session_store's
five deliberately: production keeps these tables on durable EFS SQLite with real
rows, so its cutover needs dual-write and shadow parity, and a shared vocabulary
keeps a coupled flip readable. Following session_store's own rule, `shadow`
compares READS only. `append_event` performs no post-write comparison there, and
neither do `create_checkpoint` or `set_policy`: in `shadow` mode PostgreSQL is
by definition not receiving the write, so comparing straight after one tests a
target you deliberately did not update.

DUPLICATED LOADER, on purpose. `platform_db()` repeats
`session_store._platform_db`. Reaching into that module to save fifteen lines
would put an import edge on the highest-blast file in this area while its own
flip is mid-flight on staging. Both copies populate the SAME
`sys.modules["leaf_platform"]` entry, so they share one loaded package at
runtime and can only disagree about how it is first created.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

SERVER_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = SERVER_DIR.parent

SELECTOR = "LEAF_SESSION_ANNEX_STORE"

STORE_MODES = {
    "legacy", "dual_write", "dual_write_shadow", "shadow", "postgres",
}
DUAL_WRITE_MODES = {"dual_write", "dual_write_shadow"}
SHADOW_READ_MODES = {"shadow", "dual_write_shadow"}

PG_CHECKPOINTS_TABLE = "app_session_checkpoints"
PG_POLICIES_TABLE = "app_session_policies"


def store_mode() -> str:
    """Return the call-time annex authority mode, rejecting unsafe typos.

    Read per call rather than at import, so a test or an operator can change it
    without reloading the module. ``legacy`` uses SQLite only. ``dual_write``
    mirrors writes while reads stay on SQLite. ``dual_write_shadow`` also
    compares reads. ``shadow`` compares reads but never writes PostgreSQL.
    ``postgres`` is PostgreSQL authority.
    """
    value = os.environ.get(SELECTOR, "legacy").strip().lower()
    if value not in STORE_MODES:
        supported = ", ".join(sorted(STORE_MODES))
        raise RuntimeError(
            f"invalid {SELECTOR} {value!r}; expected one of: {supported}"
        )
    return value


def platform_db():
    """Load the local platform database package without shadowing stdlib platform."""
    loaded = sys.modules.get("leaf_platform")
    if loaded is None:
        pkg_dir = _PROJECT_ROOT / "platform"
        spec = importlib.util.spec_from_file_location(
            "leaf_platform", pkg_dir / "__init__.py",
            submodule_search_locations=[str(pkg_dir)],
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load the Leaf platform database package")
        loaded = importlib.util.module_from_spec(spec)
        sys.modules["leaf_platform"] = loaded
        spec.loader.exec_module(loaded)
    from leaf_platform import db
    return db


def shadow_equal(label: str, legacy: Any, postgres: Any) -> None:
    """Fail closed when a shadow read disagrees with the legacy authority."""
    if legacy != postgres:
        raise RuntimeError(f"{label} shadow mismatch")


def ensure_started() -> None:
    """Refuse to mirror into a schema that is not there.

    Same bound as ``session_store._pg_ensure_started``: this is presence, not
    currency. It does not check migration history, columns, constraints or
    indexes -- ``platform.db.assert_schema_current()`` is what does that, and it
    is what a deploy pre-flight should run.
    """
    db = platform_db()
    with db.cursor() as cur:
        cur.execute(
            f"SELECT to_regclass('{PG_CHECKPOINTS_TABLE}') AS checkpoints,"
            f" to_regclass('{PG_POLICIES_TABLE}') AS policies"
        )
        row = cur.fetchone()
    if not row or not all(row.values()):
        raise RuntimeError(
            "PostgreSQL session annex schema is unavailable;"
            " apply 0029_session_annex.sql"
        )
