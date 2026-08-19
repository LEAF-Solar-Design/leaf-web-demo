"""Static checks: operator_principals grant guards + admin CLI transitions.

The admin tool mutates an authorization roster, so its guarantees are pinned
statically (no live database in this suite): migration 0047 encodes the
attribution invariants at the schema level, the contract documents the
transition rules, and the CLI's transition table can never resurrect a
revoked principal by accident.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MIGRATION = REPO / "platform" / "migrations" / "0047_operator_principals_grant_guards.sql"
CONTRACT = REPO / "contract" / "OPERATOR.md"
SCRIPT = REPO / "scripts" / "operator_principal_admin.py"


def test_grant_guard_migration_exists_and_is_expand_only():
    sql = MIGRATION.read_text(encoding="utf-8")
    # backfill precedes the tighten so the NOT NULL cannot fail on legacy rows
    assert sql.index("UPDATE operator_principals") < sql.index("SET NOT NULL")
    assert "ALTER COLUMN granted_by SET NOT NULL" in sql
    # the audit trail is a schema invariant, not a CLI courtesy
    assert "CHECK (granted_by <> '')" in sql
    assert "CHECK (environment IN ('staging', 'production'))" in sql
    assert "DROP TABLE" not in sql and "DROP COLUMN" not in sql


def test_operator_contract_documents_transition_guards():
    text = CONTRACT.read_text(encoding="utf-8")
    assert "operator_principals" in text
    assert "role_revision" in text
    assert "resume" in text and "un-revoke" in text
    assert "granted_by     TEXT NOT NULL" in text


def test_cli_transition_table_cannot_unrevoke():
    """resume must only apply to suspended rows; revoke must be terminal."""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    transitions = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if getattr(tgt, "id", None) == "_TRANSITIONS":
                    transitions = ast.literal_eval(node.value)
    assert transitions is not None, "_TRANSITIONS table missing from admin CLI"
    assert transitions["resume"]["from"] == ("suspended",)
    assert "revoked" not in transitions["resume"]["from"]
    assert transitions["suspend"]["from"] == ("active",)
    assert transitions["revoke"]["idempotent_on"] == ("revoked",)


def test_cli_grant_requires_grantor_and_never_defaults_scope():
    src = SCRIPT.read_text(encoding="utf-8")
    assert '"--granted-by", required=True' in src
    # scope flags default to None so a bare re-grant cannot reset an existing
    # row's profiles/environment back to defaults
    assert 'grant.add_argument("--profiles", default=None)' in src
    assert 'grant.add_argument("--environment", default=None,' in src
    assert "COALESCE(%s, profiles)" in src
    assert "COALESCE(%s, environment)" in src
    assert "--reactivate" in src
