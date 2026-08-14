"""Changed-surface server binding checks for P8 durable edit state."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
PLATFORM = ROOT / "platform"
SERVER = ROOT / "server"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))
if "leaf_platform" not in sys.modules:
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "leaf_platform", PLATFORM / "__init__.py",
        submodule_search_locations=[str(PLATFORM)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["leaf_platform"] = module
    spec.loader.exec_module(module)

import project_repository_edit_contract as contract  # noqa: E402
import project_repository_edit_state as state  # noqa: E402


IDS = {
    "tenant": "11111111-1111-4111-8111-111111111111",
    "organization": "11111111-1111-4111-8111-111111111111",
    "project": "22222222-2222-4222-8222-222222222222",
    "repo": "33333333-3333-4333-8333-333333333333",
    "edit": "44444444-4444-4444-8444-444444444444",
    "actor": "55555555-5555-4555-8555-555555555555",
    "lease": "66666666-6666-4666-8666-666666666666",
    "confirmation": "77777777-7777-4777-8777-777777777777",
    "approver": "88888888-8888-4888-8888-888888888888",
}


def _raw():
    return {
        "contract": contract.STAGED_RECEIPT_CONTRACT, "edit_id": IDS["edit"],
        "state": "staged", "operation": "edit", "source_edit_id": None,
        "actor_binding_id": IDS["actor"], "tenant_id": IDS["tenant"],
        "organization_id": IDS["organization"], "project_id": IDS["project"],
        "repo_key": IDS["repo"], "writer_lease_id": IDS["lease"],
        "writer_lease_generation": 4, "base_commit": "1" * 40,
        "staged_head_commit": "2" * 40, "staged_tree": "3" * 40,
        "changed_paths": ["src/a.py"], "diff_digest": "4" * 64,
        "instruction_digest": "5" * 64, "idempotency_key": "stage-key",
    }


def _resolved():
    return {"tenant_id": IDS["tenant"], "organization_id": IDS["organization"],
            "project_id": IDS["project"], "repo_key": IDS["repo"]}


def test_record_parses_unit1_and_passes_exact_witnesses(monkeypatch):
    raw = _raw()
    receipt = contract.parse_staged_receipt(raw)
    digest = contract.staged_receipt_digest(receipt)
    monkeypatch.setattr(state.platform_store, "resolve_project_repository_authority",
                        lambda *args: _resolved())
    captured = {}
    monkeypatch.setattr(state.repository_edit_store, "record_staged",
                        lambda value, value_digest, **kwargs:
                        captured.update(receipt=value, digest=value_digest, **kwargs) or {"state": "staged"})
    assert state.record_staged(raw, digest, expected_version=0,
                               transition_key="stage-key") == {"state": "staged"}
    assert captured["receipt"].writer_witness.writer_lease_id == IDS["lease"]
    assert captured["receipt"].writer_witness.writer_lease_generation == 4
    assert captured["receipt"].staged_tree == "3" * 40


@pytest.mark.parametrize("field", ["tenant_id", "organization_id", "project_id", "repo_key"])
def test_any_authority_substitution_fails_before_store(monkeypatch, field):
    raw = _raw()
    resolved = _resolved()
    resolved[field] = "99999999-9999-4999-8999-999999999999"
    monkeypatch.setattr(state.platform_store, "resolve_project_repository_authority",
                        lambda *args: resolved)
    called = []
    monkeypatch.setattr(state.repository_edit_store, "record_staged",
                        lambda *args, **kwargs: called.append(True))
    receipt = contract.parse_staged_receipt(raw)
    with pytest.raises(state.RepositoryEditStateError, match="authority_mismatch"):
        state.record_staged(raw, contract.staged_receipt_digest(receipt),
                            expected_version=0, transition_key="stage-key")
    assert called == []


def test_confirmation_drift_fails_before_persistence(monkeypatch):
    raw = _raw()
    receipt = contract.parse_staged_receipt(raw)
    confirmation = {
        "contract": contract.CONFIRMATION_CONTRACT,
        "confirmation_id": IDS["confirmation"],
        "receipt_digest": contract.staged_receipt_digest(receipt),
        "approver_binding_id": IDS["approver"], "tenant_id": IDS["tenant"],
        "organization_id": IDS["organization"], "project_id": IDS["project"],
        "repo_key": IDS["repo"], "edit_id": IDS["edit"],
        "writer_lease_id": IDS["lease"], "writer_lease_generation": 4,
        "staged_tree": "3" * 40, "issued_at": "2026-08-14T12:00:00Z",
        "expires_at": "2026-08-14T12:05:00Z",
    }
    monkeypatch.setattr(state.platform_store, "resolve_project_repository_authority",
                        lambda *args: _resolved())
    called = []
    monkeypatch.setattr(state.repository_edit_store, "put_confirmation",
                        lambda *args, **kwargs: called.append(True))
    changed = copy.deepcopy(confirmation)
    changed["staged_tree"] = "9" * 40
    with pytest.raises(state.RepositoryEditStateError, match="confirmation_binding_mismatch"):
        state.put_confirmation(raw, changed, expected_edit_version=2,
                               transition_key="confirm-key")
    assert called == []


def test_module_remains_dormant_and_has_no_git_or_route_surface():
    source = (Path(__file__).resolve().parent.parent /
              "project_repository_edit_state.py").read_text(encoding="utf-8")
    for forbidden in ("fastapi", "APIRouter", "subprocess", "git ", "os.environ", "repo_dir"):
        assert forbidden not in source
