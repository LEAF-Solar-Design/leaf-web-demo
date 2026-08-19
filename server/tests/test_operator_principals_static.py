"""Static checks: the operator_principals schema + admin CLI transition guards.

The admin tool mutates an authorization roster, so its guarantees are pinned
statically (no live database in this suite): the migration exists and encodes
the contract's invariants at the schema level, and the CLI's transition table
can never resurrect a revoked principal by accident.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MIGRATION = REPO / "platform" / "migrations" / "0020_operator_principals.sql"
CONTRACT = REPO / "contract" / "OPERATOR.md"
SCRIPT = REPO / "scripts" / "operator_principal_admin.py"


def test_operator_principals_migration_exists_and_is_guarded():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS operator_principals" in sql
    assert "subject       TEXT PRIMARY KEY" in sql
    assert "role_revision INTEGER NOT NULL DEFAULT 1" in sql
    assert "CHECK (status IN ('active', 'suspended', 'revoked'))" in sql
    assert "CHECK (environment IN ('staging', 'production'))" in sql
    # the audit trail is a schema invariant, not a CLI courtesy
    assert "granted_by    TEXT NOT NULL CHECK (granted_by <> '')" in sql


def test_operator_contract_document_exists():
    text = CONTRACT.read_text(encoding="utf-8")
    assert "operator_principals" in text
    assert "role_revision" in text
    assert "resume" in text and "never un-revoke" in text


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
