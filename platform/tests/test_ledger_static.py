"""Dependency-free proof for canonical hash metadata and migration shape."""
from __future__ import annotations

import hashlib
import importlib.util
import pathlib
import sys


_PKG = pathlib.Path(__file__).resolve().parent.parent
_MODELS = importlib.util.spec_from_file_location("ledger_models", _PKG / "models.py")
assert _MODELS and _MODELS.loader
models = importlib.util.module_from_spec(_MODELS)
sys.modules["ledger_models"] = models
_MODELS.loader.exec_module(models)


def test_domain_separated_hash_is_stable_and_tamper_visible():
    payload = {"z": [3, 2, 1], "a": {"enabled": True}}
    digest = models.canonical_hash("solve-record", payload)
    expected = hashlib.sha256(
        b"leaf:solve-record:v1\0" + models.canonical_json_bytes(payload)
    ).hexdigest()
    assert digest.to_dict() == {
        "algorithm": "sha256", "canonicalization": "RFC8785-JCS",
        "domain": "leaf:solve-record:v1", "value": expected,
    }
    assert models.verify_canonical_hash("solve-record", payload, digest.to_dict())
    assert not models.verify_canonical_hash("solve-record", {"z": [3, 2], "a": {"enabled": True}},
                                            digest.to_dict())
    assert models.canonical_hash("history-operation", payload).value != digest.value


def test_migration_declares_composite_ownership_immutability_and_outbox():
    sql = (_PKG / "migrations" / "0003_canonical_history_ledger.sql").read_text(encoding="utf-8")
    for required in (
        "REFERENCES projects(org_id, project_id)", "identity_bindings",
        "tenant_authority_modes", "project_authority_modes", "history_operations",
        "history_edges", "branch_refs", "solve_records", "outbox_entries",
        "leaf_reject_ledger_mutation", "UNIQUE (org_id, project_id, idempotency_key)",
        "drawing_versions_org_project_version_unique", "jobs_input_version_org_project_fk",
        "jobs_output_version_org_project_fk", "jobs_spine_ref_unique_when_present",
        "ON DELETE CASCADE",
    ):
        assert required in sql


def test_rfc8785_has_no_false_fallback_and_edges_are_composite_scoped():
    models_source = (_PKG / "models.py").read_text(encoding="utf-8")
    sql = (_PKG / "migrations" / "0003_canonical_history_ledger.sql").read_text(encoding="utf-8")
    assert "except ImportError" not in models_source
    assert "REFERENCES history_operations(org_id, project_id, operation_id)" in sql


def test_migration_replaces_unsafe_job_version_foreign_keys_and_allows_offboarding_deletes():
    sql = (_PKG / "migrations" / "0003_canonical_history_ledger.sql").read_text(encoding="utf-8")
    assert "ALTER TABLE jobs DROP CONSTRAINT" in sql
    assert "FOREIGN KEY (org_id, project_id, input_version_id)" in sql
    assert "FOREIGN KEY (org_id, project_id, output_version_id)" in sql
    assert "status = 'offboarding'" in sql
