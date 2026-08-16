"""Changed-surface acceptance for the dormant P8 repository edit contract."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest


SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import project_repository_edit_contract as contract  # noqa: E402


TENANT_ID = "11111111-1111-4111-8111-111111111111"
ORG_ID = "22222222-2222-4222-8222-222222222222"
PROJECT_ID = "33333333-3333-4333-8333-333333333333"
REPO_KEY = "44444444-4444-4444-8444-444444444444"
EDIT_ID = "55555555-5555-4555-8555-555555555555"
SOURCE_EDIT_ID = "66666666-6666-4666-8666-666666666666"
ACTOR_ID = "77777777-7777-4777-8777-777777777777"
LEASE_ID = "88888888-8888-4888-8888-888888888888"
CONFIRMATION_ID = "99999999-9999-4999-8999-999999999999"
APPROVER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _staged(**overrides):
    value = {
        "contract": contract.STAGED_RECEIPT_CONTRACT,
        "edit_id": EDIT_ID,
        "state": "staged",
        "operation": "edit",
        "source_edit_id": None,
        "actor_binding_id": ACTOR_ID,
        "tenant_id": TENANT_ID,
        "organization_id": ORG_ID,
        "project_id": PROJECT_ID,
        "repo_key": REPO_KEY,
        "writer_lease_id": LEASE_ID,
        "writer_lease_generation": 7,
        "base_commit": "1" * 40,
        "staged_head_commit": "2" * 40,
        "staged_tree": "3" * 40,
        "changed_paths": ["docs/readme.md", "src/caf\u00e9.py"],
        "diff_digest": "4" * 64,
        "instruction_digest": "5" * 64,
        "idempotency_key": "stage-edit-1",
    }
    value.update(overrides)
    return value


def _confirmation(receipt, **overrides):
    value = {
        "contract": contract.CONFIRMATION_CONTRACT,
        "confirmation_id": CONFIRMATION_ID,
        "receipt_digest": contract.staged_receipt_digest(receipt),
        "approver_binding_id": APPROVER_ID,
        "tenant_id": TENANT_ID,
        "organization_id": ORG_ID,
        "project_id": PROJECT_ID,
        "repo_key": REPO_KEY,
        "edit_id": EDIT_ID,
        "writer_lease_id": LEASE_ID,
        "writer_lease_generation": 7,
        "staged_tree": "3" * 40,
        "issued_at": "2026-08-14T07:00:00Z",
        "expires_at": "2026-08-14T07:05:00Z",
    }
    value.update(overrides)
    return value


def test_edit_receipt_is_closed_canonical_and_jcs_stable():
    raw = _staged()
    receipt = contract.parse_staged_receipt(raw)
    assert receipt.authority_key == contract.RepositoryAuthorityKey(
        TENANT_ID, ORG_ID, PROJECT_ID, REPO_KEY)
    assert receipt.writer_witness == contract.WriterLeaseWitness(LEASE_ID, 7)
    assert receipt.staged_tree == "3" * 40

    compact = json.dumps(raw, separators=(",", ":"))
    reordered = json.dumps(dict(reversed(list(raw.items()))), indent=2)
    first = contract.parse_staged_receipt_json(compact)
    second = contract.parse_staged_receipt_json(reordered)
    assert contract.staged_receipt_digest(first) == contract.staged_receipt_digest(second)


def test_rollback_requires_and_preserves_its_source_edit():
    receipt = contract.parse_staged_receipt(
        _staged(operation="rollback", source_edit_id=SOURCE_EDIT_ID),
    )
    assert receipt.operation == "rollback"
    assert receipt.source_edit_id == SOURCE_EDIT_ID

    with pytest.raises(contract.ContractValidationError, match="source_edit_id"):
        contract.parse_staged_receipt(_staged(operation="rollback"))
    with pytest.raises(contract.ContractValidationError, match="source_edit_id"):
        contract.parse_staged_receipt(_staged(source_edit_id=SOURCE_EDIT_ID))


@pytest.mark.parametrize(
    "field",
    ["writer_lease_id", "writer_lease_generation", "staged_tree"],
)
def test_historical_pr546_missing_writer_witness_is_rejected(field):
    raw = _staged()
    raw.pop(field)
    with pytest.raises(contract.ContractValidationError, match="closed staged receipt"):
        contract.parse_staged_receipt(raw)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("writer_lease_id", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        ("writer_lease_generation", 8),
        ("staged_tree", "6" * 40),
    ],
)
def test_confirmation_refuses_any_frozen_writer_witness_drift(field, replacement):
    receipt = contract.parse_staged_receipt(_staged())
    confirmation_raw = _confirmation(receipt)
    confirmation = contract.parse_confirmation(confirmation_raw)

    changed_receipt = contract.parse_staged_receipt(_staged(**{field: replacement}))
    with pytest.raises(contract.ContractValidationError, match="receipt digest"):
        contract.require_confirmation_binding(confirmation, changed_receipt)

    confirmation_raw[field] = replacement
    changed_confirmation = contract.parse_confirmation(confirmation_raw)
    with pytest.raises(contract.ContractValidationError, match="does not bind"):
        contract.require_confirmation_binding(changed_confirmation, receipt)


def test_confirmation_binds_the_exact_receipt_and_is_ttl_bounded():
    receipt = contract.parse_staged_receipt(_staged())
    confirmation = contract.parse_confirmation(_confirmation(receipt))
    contract.require_confirmation_binding(confirmation, receipt)
    assert confirmation.confirmation_id == CONFIRMATION_ID

    wrong_digest = _confirmation(receipt, receipt_digest="f" * 64)
    with pytest.raises(contract.ContractValidationError, match="receipt digest"):
        contract.require_confirmation_binding(
            contract.parse_confirmation(wrong_digest), receipt)

    with pytest.raises(contract.ContractValidationError, match="expires_at"):
        contract.parse_confirmation(_confirmation(
            receipt, expires_at="2026-08-14T06:59:59Z"))


@pytest.mark.parametrize("target", ["staged", "confirmation"])
def test_receipts_reject_extra_fields(target):
    receipt = contract.parse_staged_receipt(_staged())
    raw = _staged() if target == "staged" else _confirmation(receipt)
    raw["repo_dir"] = "C:/tenant/repo"
    parser = (contract.parse_staged_receipt if target == "staged"
              else contract.parse_confirmation)
    with pytest.raises(contract.ContractValidationError, match="closed"):
        parser(raw)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("tenant_id", "not-a-uuid", "tenant_id"),
        ("writer_lease_generation", 0, "writer_lease_generation"),
        ("writer_lease_generation", True, "writer_lease_generation"),
        ("base_commit", "A" * 40, "base_commit"),
        ("staged_tree", "3" * 39, "staged_tree"),
        ("diff_digest", "g" * 64, "diff_digest"),
    ],
)
def test_identity_generation_and_git_fields_are_strict(field, value, message):
    with pytest.raises(contract.ContractValidationError, match=message):
        contract.parse_staged_receipt(_staged(**{field: value}))


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/src/file.py",
        "C:/src/file.py",
        "src\\file.py",
        "src//file.py",
        "src/./file.py",
        "src/../file.py",
        "src/.git/config",
        "src/.GIT/file.py",
        "src/file.py/",
        "src/\x00file.py",
        "src/cafe\u0301.py",
    ],
)
def test_unsafe_or_noncanonical_changed_paths_are_rejected(path):
    with pytest.raises(contract.ContractValidationError, match="changed_paths"):
        contract.parse_staged_receipt(_staged(changed_paths=[path]))


def test_paths_must_be_utf8_sorted_unique_and_case_alias_free():
    bad_sets = [
        ["z.py", "a.py"],
        ["a.py", "a.py"],
        ["SRC/file.py", "src/file.py"],
        ["caf\u00e9.py", "CAF\u00c9.py"],
    ]
    for paths in bad_sets:
        with pytest.raises(contract.ContractValidationError, match="changed_paths"):
            contract.parse_staged_receipt(_staged(changed_paths=paths))


def test_strict_json_rejects_duplicate_keys_invalid_utf8_and_surrogates():
    valid = json.dumps(_staged())
    duplicate = valid[:-1] + ',"edit_id":"' + SOURCE_EDIT_ID + '"}'
    with pytest.raises(contract.ContractValidationError, match="duplicate JSON key"):
        contract.parse_staged_receipt_json(duplicate)
    with pytest.raises(contract.ContractValidationError, match="UTF-8"):
        contract.parse_staged_receipt_json(b"{\xff}")

    surrogate = copy.deepcopy(_staged())
    surrogate["changed_paths"] = ["src/\ud800.py"]
    with pytest.raises(contract.ContractValidationError, match="changed_paths"):
        contract.parse_staged_receipt(surrogate)


def test_digest_comparison_is_strict_and_constant_time(monkeypatch):
    receipt = contract.parse_staged_receipt(_staged())
    digest = contract.staged_receipt_digest(receipt)
    calls = []
    original = contract.hmac.compare_digest

    def spy(left, right):
        calls.append((left, right))
        return original(left, right)

    monkeypatch.setattr(contract.hmac, "compare_digest", spy)
    assert contract.receipt_digest_matches(receipt, digest) is True
    assert contract.receipt_digest_matches(receipt, "0" * 64) is False
    assert len(calls) == 2
    assert all(isinstance(value, bytes) for call in calls for value in call)


def test_source_surface_remains_pure_and_dormant():
    source = (SERVER_DIR / "project_repository_edit_contract.py").read_text(
        encoding="utf-8")
    for forbidden in (
        "fastapi", "sqlalchemy", "psycopg", "subprocess", "os.environ",
        "SdkRepoEditor", "repo_dir", "git ",
    ):
        assert forbidden not in source
