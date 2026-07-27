"""Live-PostgreSQL gate: schema_status() must report ok with every authority
selector routed at `postgres`.

WHY THIS FILE EXISTS. Two CI paths each looked like they covered this, and
neither executed it:

  * `.github/workflows/test-gate.yml` is deliberately hermetic (its own comment
    says DATABASE_URL is unset on purpose), and the `platform` suite in
    scripts/run-all-gates.py is db_gated=True, so it SKIPS there.
  * `.github/workflows/upload-authority-postgres.yml` does run a live
    `postgres:16` service and does list platform/db.py in its `paths:` trigger,
    but every one of its steps ran a named server/ test file, and none of them
    called schema_status() with an authority selector set to postgres.

Trigger coverage is not step coverage. PR #187 carried three green checks
(postgres-authority, replay, run-all-gates) while its committed platform/db.py
returned ok=false against a real PostgreSQL 16: broker_usage_ledger_immutable
and broker_admission_resolution_audit_immutable both came back
definition-mismatch. assert_schema_current() raises on ok=false, so that tree
would have failed closed at startup for any LEAF_BROKER_STORE=postgres deploy.

This file is executed by the `Run platform authority schema_status gate` step
of upload-authority-postgres.yml, which supplies the live database that makes
the assertion real. Under the hermetic gate its conftest skips collection, and
that is the gap, not a second bug: the assertion below is only meaningful with
a reachable PostgreSQL.
"""
from __future__ import annotations

import pytest

from leaf_platform import db

# Keys schema_status() returns that describe the database rather than a defect.
_METADATA_KEYS = frozenset({
    "ok",
    "database",
    "schema",
    "migration_count",
    "applied_migration_count",
})

SELECTORS = sorted(db._AUTHORITY_SELECTORS)


def _defects(status):
    """Every non-metadata field carrying a value, whatever it happens to be named.

    schema_status()'s defect keys are NOT stable across revisions of db.py: this
    tree reports missing_constraints / missing_indexes / missing_triggers, while
    the PR #187 revision reports invalid_constraints / invalid_indexes /
    invalid_triggers for the same class of failure. Naming the keys explicitly
    would make this gate silently vacuous the day the key set moves again, so
    report whatever non-metadata field is populated.
    """
    return {
        key: value
        for key, value in sorted(status.items())
        if key not in _METADATA_KEYS and value
    }


def _describe(label, status):
    return f"{label}: schema_status() ok={status['ok']} defects={_defects(status)}"


def _select_only(monkeypatch, selectors):
    """Route exactly `selectors` at postgres and clear every other authority."""
    for name in SELECTORS:
        monkeypatch.delenv(name, raising=False)
    for name in selectors:
        monkeypatch.setenv(name, "postgres")


def test_every_authority_selector_offers_a_postgres_value():
    """Guard the sweep below against passing vacuously.

    Every assertion in this file is only worth its runtime if SELECTORS is
    non-empty and each entry actually routes at the literal "postgres" value
    the tests set. A selector that dropped its postgres mapping would otherwise
    turn its own case into a no-op that still reports green.
    """
    assert SELECTORS, "_AUTHORITY_SELECTORS is empty: this gate proves nothing"
    without_postgres = [
        name for name in SELECTORS if "postgres" not in db._AUTHORITY_SELECTORS[name]
    ]
    assert not without_postgres, (
        "selectors with no postgres value, so setting them to postgres selects "
        f"no authority: {without_postgres}"
    )


def test_postgres_selection_requires_real_catalog_objects(monkeypatch):
    """Guard against an empty required-catalog making ok=true meaningless.

    schema_status() reports ok when nothing required is missing. If selecting
    every authority required zero constraints, indexes, or triggers, the ok
    assertions would hold on a database with none of them. Prove the contract
    set is populated before trusting the verdict built from it.
    """
    _select_only(monkeypatch, SELECTORS)
    catalog = db.required_catalog_for_selected_authorities()
    empty = sorted(kind for kind, names in catalog.items() if not names)
    assert not empty, f"no {empty} required with every authority at postgres"

    required_columns = db.required_columns_for_selected_authorities()
    for table in ("broker_usage_ledger", "broker_admission_resolution_audit"):
        assert required_columns.get(table), (
            f"{table} carries no required columns with every authority at postgres"
        )


def test_schema_status_ok_with_every_authority_on_postgres(monkeypatch):
    """The union case: every LEAF_*_STORE at postgres against a live database."""
    _select_only(monkeypatch, SELECTORS)
    status = db.schema_status()
    assert status["ok"] is True, _describe("every selector=postgres", status)


@pytest.mark.parametrize("selector", SELECTORS)
def test_schema_status_ok_for_each_authority_on_postgres(monkeypatch, selector):
    """One selector at a time, so a failure names the authority that broke.

    The union case above fails on the first defect from any authority; these
    localize it. PR #187's tree fails exactly LEAF_BROKER_STORE here.
    """
    _select_only(monkeypatch, [selector])
    status = db.schema_status()
    assert status["ok"] is True, _describe(f"{selector}=postgres", status)


def test_assert_schema_current_does_not_fail_closed_on_postgres(monkeypatch):
    """The real startup path, not just the read-only report.

    assert_schema_current() is what a LEAF_*_STORE=postgres deployment calls; it
    raises RuntimeError on ok=false. Asserting schema_status() alone would leave
    the raise itself unexercised.
    """
    _select_only(monkeypatch, SELECTORS)
    status = db.assert_schema_current()
    assert status["ok"] is True, _describe("assert_schema_current", status)
