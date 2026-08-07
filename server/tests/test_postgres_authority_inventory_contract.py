"""Contract checks for the machine-readable PostgreSQL authority inventory."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[2]
INVENTORY_PATH = REPO_ROOT / "platform" / "authority-inventory.json"

EXPECTED_MIGRATIONS = [f"{number:04d}" for number in range(1, 29)]
EXPECTED_SELECTOR_DEFAULTS = {
    "tenant_authority_modes.authority_mode": "legacy_sqlite",
    "project_authority_modes.authority_mode": "legacy_sqlite",
    "LEAF_JOBS_STORE": "legacy",
    "LEAF_CALLBACK_REPLAY_STORE": "legacy",
    "LEAF_SESSIONS_STORE": "legacy",
    "LEAF_AGENT_STORE": "legacy",
    "LEAF_BROKER_STORE": "legacy",
    "LEAF_GUEST_CAP_STORE": "memory",
    "LEAF_AUTHOR_QUOTA_STORE": "memory",
    "LEAF_DRAWING_STORE": "legacy",
    "LEAF_UPLOAD_STORE": "legacy",
    "LEAF_BLOB_STORE": "legacy",
    "LEAF_HARNESS_SESSION_STORE": "file",
    "LEAF_GRANT_STORE": "file",
    "LEAF_HARNESS_AUTHORING_MODE": "disabled",
    "LEAF_CUSTOMIZATION_R5_MODE": "off",
    "LEAF_CUSTOMIZATION_R6_MODE": "off",
    "LEAF_CUSTOMIZATION_STORE": "sqlite",
}
EXPECTED_SELECTOR_DEPENDENCIES = [
    {
        "when": {"name": "LEAF_UPLOAD_STORE", "value": "postgres"},
        "requires": {"name": "LEAF_DRAWING_STORE", "value": "postgres"},
    },
]
REQUIRED_COVERAGE = {
    "jobs",
    "sessions_approvals",
    "agent_state",
    "broker_tenants_ledger",
    "guest_caps",
    "author_quota",
    "drawing_metadata",
    "upload_metadata",
    "harness_sessions_grants",
    "customization",
    "tenant_repositories_leases",
}
# A selection status is a claim about HOW the value was established, so an
# unrecognized one is a silent downgrade rather than a typo: "measurd" used to
# pass every check while reading, to a human, like a measurement.
#   unknown          - nothing recorded; value must be null.
#   measured         - the selector was OBSERVED set to this value.
#   unset_defaulting - the selector was observed ABSENT and the value is the
#                      repository default the code resolves in its place. It is
#                      an inference from absence, never an observed setting.
#   verified         - measured AND reconciled; the only status a "complete"
#                      inventory claim accepts.
KNOWN_SELECTION_STATUSES = {"unknown", "measured", "unset_defaulting", "verified"}
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
REQUIRED_RUNTIME_TABLES_BY_SELECTOR = {
    "LEAF_JOBS_STORE": {"async_jobs", "async_job_terminal_conflicts"},
    "LEAF_CALLBACK_REPLAY_STORE": {"callback_consumed_nonces"},
    "LEAF_SESSIONS_STORE": {"app_sessions", "app_session_events", "app_approvals"},
    "LEAF_AGENT_STORE": {
        "agent_approvals", "agent_session_grants", "agent_rate_counters",
        "agent_fleet_state", "agent_gate_audit_events", "agent_tenant_state",
        "agent_usage_turns",
    },
    "LEAF_BROKER_STORE": {
        "broker_tenants", "broker_usage_ledger", "broker_run_admissions",
        "broker_aps_slots", "broker_admission_resolution_audit",
    },
    "LEAF_GUEST_CAP_STORE": {"guest_upload_counters"},
    "LEAF_AUTHOR_QUOTA_STORE": {
        "author_quota_counters", "author_quota_attempts",
    },
    "LEAF_DRAWING_STORE": {"drawing_store_manifests", "drawing_store_versions"},
    "LEAF_UPLOAD_STORE": {
        "drawing_store_manifests", "drawing_store_versions",
        "drawing_upload_attempts", "drawing_purge_receipts",
    },
    "LEAF_HARNESS_SESSION_STORE": {
        "harness_sessions", "harness_turns", "harness_events",
        "harness_confirmations", "harness_usage", "harness_tenant_repo_leases",
    },
    "LEAF_CUSTOMIZATION_STORE": {
        "customization_change_sets",
        "effective_catalogs",
        "customization_audit_events",
        "customization_confirmations",
        "customization_publication_requests",
        "customization_deployment_snapshots",
        "customization_deployment_audit",
    },
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
            missing_runtime_tables = REQUIRED_RUNTIME_TABLES_BY_SELECTOR.get(
                name, set()) - set(authority["postgres_tables"])
            if missing_runtime_tables:
                errors.append(
                    f"{name}: missing runtime PostgreSQL tables "
                    f"{sorted(missing_runtime_tables)}"
                )

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
            if selection.get("status") not in KNOWN_SELECTION_STATUSES:
                errors.append(
                    f"{authority_id}: unknown {environment} selection status "
                    f"{selection.get('status')!r}"
                )
            if selection.get("status") == "unknown" and selection.get("value") is not None:
                errors.append(f"{authority_id}: unknown {environment} selection has a value")
            if selection.get("status") == "verified" and selection.get("value") is None:
                errors.append(f"{authority_id}: verified {environment} selection lacks a value")
            if selection.get("status") == "unset_defaulting":
                # The value IS the resolved default, so it must be present --
                # otherwise the record is indistinguishable from "unknown" and
                # nothing downstream can scope a backfill from it.
                if selection.get("value") is None:
                    errors.append(
                        f"{authority_id}: unset_defaulting {environment} selection "
                        "lacks the defaulted value"
                    )
                # The whole point of the status is that a later reader cannot
                # mistake an inferred default for an observed setting, and that
                # only holds if the evidence says the selector was absent.
                if "absent" not in (selection.get("evidence") or "").lower():
                    errors.append(
                        f"{authority_id}: unset_defaulting {environment} selection "
                        "evidence does not state that the selector is absent"
                    )

    if selectors != EXPECTED_SELECTOR_DEFAULTS:
        errors.append(
            "selector coverage/default mismatch: "
            f"expected={EXPECTED_SELECTOR_DEFAULTS!r}, actual={selectors!r}"
        )
    dependencies = inventory.get("selector_dependencies")
    if not isinstance(dependencies, list):
        errors.append("selector_dependencies must be a list")
    else:
        normalized_dependencies = []
        for dependency in dependencies:
            if set(dependency) != {"when", "requires", "reason"}:
                errors.append(
                    "selector dependency must contain when, requires, and reason")
                continue
            when = dependency["when"]
            requires = dependency["requires"]
            if set(when) != {"name", "value"} or set(requires) != {"name", "value"}:
                errors.append("selector dependency endpoints must contain name and value")
                continue
            if when["name"] not in selectors or requires["name"] not in selectors:
                errors.append("selector dependency references an unknown selector")
            if not dependency["reason"].strip():
                errors.append("selector dependency lacks a reason")
            normalized_dependencies.append({"when": when, "requires": requires})
        if normalized_dependencies != EXPECTED_SELECTOR_DEPENDENCIES:
            errors.append(
                "selector dependency mismatch: "
                f"expected={EXPECTED_SELECTOR_DEPENDENCIES!r}, "
                f"actual={normalized_dependencies!r}"
            )
    missing_coverage = REQUIRED_COVERAGE - coverage
    if missing_coverage:
        errors.append(f"missing authority coverage {sorted(missing_coverage)}")

    scope = inventory.get("scope", {})
    if scope.get("migration_ids") != EXPECTED_MIGRATIONS:
        errors.append("scope migration_ids must be exactly 0001 through 0025")
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


def test_inferred_production_defaults_cannot_pose_as_observed_settings() -> None:
    """The fields this lane closed, and the rule that keeps them honest.

    All three selectors are ABSENT from the live production task definition, so
    `legacy` is the repository default the code resolves, NOT a value anything
    in production was measured to say. `measured` would overstate exactly that.

    This pins the recorded distinction. It does NOT detect live drift: a later
    task-definition revision can set any of these and no test here would notice,
    which is why each evidence string names the revision it was read from.
    """
    inventory = _load_inventory()
    entries = {authority["id"]: authority for authority in inventory["authorities"]}

    for authority_id in ("app_sessions_and_approvals", "callback_replay_nonces",
                         "async_jobs"):
        production = entries[authority_id]["current_selection"]["production"]
        assert production["value"] == "legacy", authority_id
        assert production["status"] == "unset_defaulting", authority_id
        assert "leaf-automation-production-platform:98" in production["evidence"]
        assert "ABSENT" in production["evidence"], authority_id

    # Each branch of the rule fails the contract when violated, so none of the
    # three assertions above is resting on a check that never fires.
    typo = deepcopy(inventory)
    typo["authorities"][0]["current_selection"]["production"]["status"] = "measurd"
    assert any(
        "unknown production selection status" in error
        for error in _inventory_errors(typo)
    )

    valueless = deepcopy(inventory)
    entry = next(
        authority for authority in valueless["authorities"]
        if authority["id"] == "app_sessions_and_approvals"
    )
    entry["current_selection"]["production"]["value"] = None
    assert any(
        "unset_defaulting production selection lacks the defaulted value" in error
        for error in _inventory_errors(valueless)
    )

    vague = deepcopy(inventory)
    entry = next(
        authority for authority in vague["authorities"]
        if authority["id"] == "app_sessions_and_approvals"
    )
    entry["current_selection"]["production"]["evidence"] = "Production is legacy."
    assert any(
        "evidence does not state that the selector is absent" in error
        for error in _inventory_errors(vague)
    )


def test_contract_rejects_upload_inventory_without_shared_drawing_tables() -> None:
    inventory = _load_inventory()
    incomplete = deepcopy(inventory)
    upload = next(
        authority for authority in incomplete["authorities"]
        if authority["id"] == "upload_metadata"
    )
    upload["postgres_tables"] = [
        table for table in upload["postgres_tables"]
        if table not in {"drawing_store_manifests", "drawing_store_versions"}
    ]

    errors = _inventory_errors(incomplete)

    assert any(
        "LEAF_UPLOAD_STORE: missing runtime PostgreSQL tables" in error
        and "drawing_store_manifests" in error
        and "drawing_store_versions" in error
        for error in errors
    )


def test_contract_requires_postgres_drawing_authority_for_postgres_uploads() -> None:
    inventory = _load_inventory()
    assert inventory["selector_dependencies"] == [
        {
            **EXPECTED_SELECTOR_DEPENDENCIES[0],
            "reason": (
                "The PostgreSQL upload lifecycle creates and advances drawing "
                "manifests and versions, so upload and drawing metadata must "
                "share one PostgreSQL authority."
            ),
        }
    ]


def test_contract_rejects_missing_or_weakened_upload_dependency() -> None:
    inventory = _load_inventory()

    missing = deepcopy(inventory)
    missing["selector_dependencies"] = []
    assert any(
        "selector dependency mismatch" in error
        for error in _inventory_errors(missing)
    )

    weakened = deepcopy(inventory)
    weakened["selector_dependencies"][0]["requires"]["value"] = "legacy"
    assert any(
        "selector dependency mismatch" in error
        for error in _inventory_errors(weakened)
    )
