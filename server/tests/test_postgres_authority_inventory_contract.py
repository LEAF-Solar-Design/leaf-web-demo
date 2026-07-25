"""Contract checks for the machine-readable PostgreSQL authority inventory."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[2]
INVENTORY_PATH = REPO_ROOT / "platform" / "authority-inventory.json"

EXPECTED_MIGRATIONS = [f"{number:04d}" for number in range(1, 20)]
EXPECTED_SELECTOR_DEFAULTS = {
    "tenant_authority_modes.authority_mode": "legacy_sqlite",
    "project_authority_modes.authority_mode": "legacy_sqlite",
    "LEAF_JOBS_STORE": "legacy",
    "LEAF_CALLBACK_REPLAY_STORE": "legacy",
    "LEAF_SESSIONS_STORE": "legacy",
    "LEAF_AGENT_STORE": "legacy",
    "LEAF_BROKER_STORE": "legacy",
    "LEAF_GUEST_CAP_STORE": "memory",
    "LEAF_DRAWING_STORE": "legacy",
    "LEAF_UPLOAD_STORE": "legacy",
    "LEAF_BLOB_STORE": "legacy",
    "LEAF_HARNESS_SESSION_STORE": "file",
    "LEAF_GRANT_STORE": "file",
    "LEAF_HARNESS_AUTHORING_MODE": "disabled",
    "LEAF_CUSTOMIZATION_R5_MODE": "off",
    "LEAF_CUSTOMIZATION_R6_MODE": "off",
}
REQUIRED_COVERAGE = {
    "jobs",
    "sessions_approvals",
    "agent_state",
    "broker_tenants_ledger",
    "guest_caps",
    "drawing_metadata",
    "upload_metadata",
    "harness_sessions_grants",
    "customization",
    "tenant_repositories_leases",
}
REQUIRED_AUTHORITY_FIELDS = {
    "id",
    "coverage",
    "selectors",
    "postgres_tables",
    "legacy_source",
    "backfill",
    "parity",
    "cutover_modes",
    "rollback_mode",
    "current_selection",
}


def _load_inventory() -> dict:
    return json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))


def _declared_postgres_tables() -> set[str]:
    sql = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((REPO_ROOT / "platform" / "migrations").glob("*.sql"))
    )
    return set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+([a-z0-9_]+)", sql, re.I))


def _inventory_errors(inventory: dict) -> list[str]:
    errors: list[str] = []
    authorities = inventory.get("authorities")
    if not isinstance(authorities, list) or not authorities:
        return ["authorities must be a non-empty list"]

    coverage: set[str] = set()
    selectors: dict[str, str] = {}
    ids: set[str] = set()
    declared_tables = _declared_postgres_tables()

    for authority in authorities:
        authority_id = authority.get("id", "<missing>")
        missing_fields = REQUIRED_AUTHORITY_FIELDS - set(authority)
        if missing_fields:
            errors.append(f"{authority_id}: missing fields {sorted(missing_fields)}")
            continue
        if authority_id in ids:
            errors.append(f"duplicate authority id {authority_id}")
        ids.add(authority_id)
        coverage.update(authority["coverage"])

        for selector in authority["selectors"]:
            name = selector.get("name")
            if name in selectors:
                errors.append(f"selector {name} is owned more than once")
            selectors[name] = selector.get("repository_default")
            if not selector.get("allowed_modes"):
                errors.append(f"{name}: allowed_modes is empty")
            if selector.get("repository_default") not in selector.get("allowed_modes", []):
                errors.append(f"{name}: repository_default is not an allowed mode")

        missing_tables = set(authority["postgres_tables"]) - declared_tables
        if missing_tables:
            errors.append(f"{authority_id}: unknown PostgreSQL tables {sorted(missing_tables)}")
        if not authority["legacy_source"]:
            errors.append(f"{authority_id}: legacy_source is empty")
        if not authority["cutover_modes"]:
            errors.append(f"{authority_id}: cutover_modes is empty")
        if not authority["rollback_mode"].strip():
            errors.append(f"{authority_id}: rollback_mode is empty")

        for phase in ("backfill", "parity"):
            claim = authority[phase]
            if set(claim) != {"command", "status", "note"}:
                errors.append(f"{authority_id}: {phase} must contain command, status, and note")
            if not claim.get("status") or not claim.get("note"):
                errors.append(f"{authority_id}: {phase} lacks status or note")
            if claim.get("status") == "complete" and not claim.get("command"):
                errors.append(f"{authority_id}: {phase} claims complete without a command")

        selections = authority["current_selection"]
        if set(selections) != {"staging", "production"}:
            errors.append(f"{authority_id}: selections must cover staging and production")
        for environment, selection in selections.items():
            if set(selection) != {"value", "status", "evidence"}:
                errors.append(f"{authority_id}: malformed {environment} selection")
            if not selection.get("evidence"):
                errors.append(f"{authority_id}: {environment} selection lacks evidence")
            if selection.get("status") == "unknown" and selection.get("value") is not None:
                errors.append(f"{authority_id}: unknown {environment} selection has a value")
            if selection.get("status") == "verified" and selection.get("value") is None:
                errors.append(f"{authority_id}: verified {environment} selection lacks a value")

    if selectors != EXPECTED_SELECTOR_DEFAULTS:
        errors.append(
            "selector coverage/default mismatch: "
            f"expected={EXPECTED_SELECTOR_DEFAULTS!r}, actual={selectors!r}"
        )
    missing_coverage = REQUIRED_COVERAGE - coverage
    if missing_coverage:
        errors.append(f"missing authority coverage {sorted(missing_coverage)}")

    scope = inventory.get("scope", {})
    if scope.get("migration_ids") != EXPECTED_MIGRATIONS:
        errors.append("scope migration_ids must be exactly 0001 through 0019")
    if scope.get("completeness") == "complete":
        unresolved = [
            authority["id"]
            for authority in authorities
            if authority["backfill"]["status"] != "complete"
            or authority["parity"]["status"] != "complete"
            or any(
                selection["status"] != "verified"
                for selection in authority["current_selection"].values()
            )
        ]
        if unresolved:
            errors.append(f"false complete inventory claim; unresolved authorities: {unresolved}")
    elif scope.get("completeness") != "partial":
        errors.append("scope completeness must be partial or supported complete")
    if not scope.get("reason"):
        errors.append("partial inventory must state its reason")

    return errors


def test_inventory_is_complete_about_its_known_gaps_and_schema() -> None:
    inventory = _load_inventory()
    assert _inventory_errors(inventory) == []

    migration_ids = [
        path.name.split("_", 1)[0]
        for path in sorted((REPO_ROOT / "platform" / "migrations").glob("*.sql"))
    ]
    assert migration_ids == EXPECTED_MIGRATIONS


def test_contract_rejects_missing_selector_and_domain_coverage() -> None:
    inventory = _load_inventory()
    incomplete = deepcopy(inventory)
    incomplete["authorities"] = [
        authority
        for authority in incomplete["authorities"]
        if authority["id"] not in {
            "async_jobs", "customization_r5", "customization_r6",
        }
    ]

    errors = _inventory_errors(incomplete)
    assert any("selector coverage/default mismatch" in error for error in errors)
    assert any("missing authority coverage" in error for error in errors)


def test_contract_rejects_false_complete_claims() -> None:
    inventory = _load_inventory()

    false_inventory_claim = deepcopy(inventory)
    false_inventory_claim["scope"]["completeness"] = "complete"
    assert any(
        "false complete inventory claim" in error
        for error in _inventory_errors(false_inventory_claim)
    )

    false_backfill_claim = deepcopy(inventory)
    false_backfill_claim["authorities"][0]["backfill"]["status"] = "complete"
    false_backfill_claim["authorities"][0]["backfill"]["command"] = None
    assert any(
        "backfill claims complete without a command" in error
        for error in _inventory_errors(false_backfill_claim)
    )
