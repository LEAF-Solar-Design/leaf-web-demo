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
#   unknown  - nothing recorded; value must be null.
#   measured - the environment was OBSERVED selecting this value.
#   verified - measured AND reconciled; the only status a "complete" inventory
#              claim accepts.
#   measured_no_override - the environment sets no override and the value is
#              the repository default its image ships. Still a measurement, but
#              NOT a selection anyone made for that environment, which is the
#              distinction a cutover plan needs. Establishing it requires
#              reading BOTH layers: a Docker image ENV stays in the process
#              environment when the deployment supplies no override, so an
#              absent task-definition variable does not by itself mean the
#              code's own fallback is what runs.
KNOWN_SELECTION_STATUSES = {"unknown", "measured", "measured_no_override", "verified"}
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
            if selection.get("status") == "measured_no_override":
                # The value IS the default, so it must be present -- otherwise
                # the record is indistinguishable from "unknown" and nothing
                # downstream can scope a backfill from it.
                if selection.get("value") is None:
                    errors.append(
                        f"{authority_id}: measured_no_override {environment} "
                        "selection lacks the defaulted value"
                    )
                # STRUCTURAL, not prose. An earlier draft required the evidence
                # to contain the word "absent", which evidence stating the
                # OPPOSITE ("is not absent; it is set to postgres") satisfied
                # just as well. The claim "no override, so this is the shipped
                # default" is only coherent when the value IS that default, and
                # that is checkable against the selector the authority declares.
                #
                # Exactly ONE selector, deliberately. A draft of this rule
                # accepted the default of ANY selector the authority owns, which
                # customization_r5 defeats: it owns LEAF_CUSTOMIZATION_R5_MODE
                # (default "off") and LEAF_CUSTOMIZATION_STORE (default
                # "sqlite"), so a record claiming the R5 mode shipped as
                # "sqlite" passed. The deeper problem is that `value` is a
                # single field with no way to name which selector it describes,
                # so for a multi-selector authority it has no defined referent
                # at all. Rather than guess, refuse the status there: such an
                # authority needs a per-selector selection shape first.
                elif len(authority["selectors"]) != 1:
                    errors.append(
                        f"{authority_id}: measured_no_override {environment} "
                        "selection is ambiguous because the authority owns "
                        f"{len(authority['selectors'])} selectors and the "
                        "selection cannot name which one it describes"
                    )
                elif selection["value"] != authority["selectors"][0].get(
                        "repository_default"):
                    errors.append(
                        f"{authority_id}: measured_no_override {environment} "
                        f"value {selection['value']!r} is not the "
                        "repository_default of its selector "
                        f"{authority['selectors'][0].get('name')!r}"
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


def test_shipped_defaults_cannot_pose_as_production_selections() -> None:
    """The fields this lane closed, and the rule that keeps them honest.

    Production sets no override for any of these three, and the images it pins
    bake all three to `legacy`. So the value is measured, but it is the shipped
    default rather than a selection anyone made for production, and plain
    `measured` would read as the latter.

    Establishing that needed BOTH layers. Task-definition absence alone does not
    imply the code fallback runs, because an image ENV survives an absent
    override, so each evidence string names the image digest as well as the
    task-definition revision.

    This pins the recorded distinction. It does NOT detect live drift: a later
    revision or image can change any of these and no test here would notice.
    """
    inventory = _load_inventory()
    entries = {authority["id"]: authority for authority in inventory["authorities"]}

    for authority_id in ("app_sessions_and_approvals", "callback_replay_nonces",
                         "async_jobs"):
        production = entries[authority_id]["current_selection"]["production"]
        assert production["value"] == "legacy", authority_id
        assert production["status"] == "measured_no_override", authority_id
        evidence = production["evidence"]
        # Both layers, or the record does not support its own status.
        assert "leaf-automation-production-platform:98" in evidence, authority_id
        assert "@sha256:" in evidence, authority_id

    # Every branch fails the contract when violated, so none of the assertions
    # above rests on a check that never fires.
    typo = deepcopy(inventory)
    typo["authorities"][0]["current_selection"]["production"]["status"] = "measurd"
    assert any(
        "unknown production selection status" in error
        for error in _inventory_errors(typo)
    )

    valueless = deepcopy(inventory)
    _production_of(valueless, "app_sessions_and_approvals")["value"] = None
    assert any(
        "measured_no_override production selection lacks the defaulted value" in error
        for error in _inventory_errors(valueless)
    )

    # The regression that retired the previous rule: it only required the
    # evidence to contain "absent", so evidence asserting the OPPOSITE passed.
    # The replacement is structural, so the prose cannot buy its way out.
    contradictory = deepcopy(inventory)
    selection = _production_of(contradictory, "app_sessions_and_approvals")
    selection["value"] = "postgres"
    selection["evidence"] = (
        "LEAF_SESSIONS_STORE is not absent; it is explicitly set to postgres."
    )
    assert any(
        "is not the repository_default of its selector" in error
        for error in _inventory_errors(contradictory)
    )

    # The regression that retired the FIRST structural draft: it accepted the
    # default of any selector the authority owned. customization_r5 owns
    # LEAF_CUSTOMIZATION_R5_MODE (default "off") and LEAF_CUSTOMIZATION_STORE
    # (default "sqlite"), so a record claiming the R5 mode shipped as "sqlite"
    # borrowed the sibling's default and passed with zero errors.
    borrowed = deepcopy(inventory)
    selection = _production_of(borrowed, "customization_r5")
    selection["status"] = "measured_no_override"
    selection["value"] = "sqlite"
    selection["evidence"] = "Forged: borrows the sibling selector's default."
    assert any(
        "is ambiguous because the authority owns 2 selectors" in error
        for error in _inventory_errors(borrowed)
    )


def _production_of(inventory: dict, authority_id: str) -> dict:
    authority = next(
        item for item in inventory["authorities"] if item["id"] == authority_id
    )
    return authority["current_selection"]["production"]


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
