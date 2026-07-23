"""Freeze gate for the leaf.customization.v1 contract.

Run:
    cd server
    python -m pytest tests/test_customization_contract_freeze.py -q
"""
from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator


SERVER_DIR = Path(__file__).resolve().parent.parent
ROOT = SERVER_DIR.parent
CONTRACT = (ROOT / "contract" / "CUSTOMIZATION.md").read_text(encoding="utf-8")
SCHEMA_PATH = ROOT / "contract" / "customization.v1.schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
ADDENDUM = (SERVER_DIR / "CONTRACT-ADDENDUM.md").read_text(encoding="utf-8")

STATES = {
    "created",
    "staging",
    "staged",
    "awaiting_approval",
    "approved",
    "publishing",
    "published",
    "rejected",
    "conflicted",
    "failed",
    "superseded",
    "rolled_back",
}

PUBLISH_FIELDS = {
    "change_set_id",
    "staged_commit",
    "catalog_digest",
    "platform_release",
    "workspace_contract_digest",
    "confirmation_id",
    "idempotency_key",
}


def test_schema_is_valid_draft_2020_12():
    Draft202012Validator.check_schema(SCHEMA)
    assert SCHEMA["$id"] == (
        "https://leafautomation.ai/contracts/customization.v1.schema.json"
    )


def test_workspace_reference_cannot_carry_mutability_policy():
    ref = SCHEMA["$defs"]["workspaceReference"]
    assert ref["additionalProperties"] is False
    assert set(ref["properties"]) == {
        "contract",
        "workspace_contract",
        "desired_platform_release",
    }
    assert set(ref["required"]) == set(ref["properties"])
    assert "paths" not in ref["properties"]
    assert "mutability" not in ref["properties"]
    assert "policy" not in ref["properties"]


def test_change_state_vocabulary_frozen():
    assert set(SCHEMA["$defs"]["state"]["enum"]) == STATES


def test_r6_publish_request_exact_fields_and_route():
    publish = SCHEMA["$defs"]["publishRequest"]
    assert publish["additionalProperties"] is False
    assert set(publish["properties"]) == PUBLISH_FIELDS
    assert set(publish["required"]) == PUBLISH_FIELDS
    assert "`POST /api/author/register`" in CONTRACT
    assert "This is the only R6 publish route" in CONTRACT
    assert "`POST /api/tools/register` is not part of" in CONTRACT


def test_live_legacy_author_is_non_publishing_and_r7_stays_disabled():
    assert "legacy `POST /api/author` cannot publish or persist bytes" in ADDENDUM
    assert "R7 remains disabled" in ADDENDUM
    assert "R7 remains disabled and has no dispatch route" in CONTRACT


def test_git_and_effective_state_authorities_frozen():
    assert "Tenant Git repository" in CONTRACT
    assert "Effective tenant catalog" in CONTRACT
    assert "A Git update never makes bytes effective by itself." in CONTRACT
    assert "coordination store" in CONTRACT


def test_rollout_flags_frozen():
    assert "`LEAF_CUSTOMIZATION_R5_MODE`: `off`, `internal`, or `all`" in CONTRACT
    assert "`LEAF_CUSTOMIZATION_R6_MODE`: `off`, `internal`, or `all`" in CONTRACT
    assert "`LEAF_CUSTOMIZATION_INTERNAL_TENANTS`" in CONTRACT


def test_rollback_contract_restores_catalog_and_runtime():
    assert "prior digest-pinned task definition" in CONTRACT
    assert "prior effective catalog commit and digest" in CONTRACT
    assert "prior effective platform release" in CONTRACT
    assert "idempotent rollback audit event" in CONTRACT
