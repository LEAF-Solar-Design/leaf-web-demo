"""Freeze gate for the post-launch project repository edit contract.

Run:
    cd server
    python -m pytest tests/test_project_repository_edit_contract_freeze.py -q
"""
from __future__ import annotations

import hashlib
import hmac
import json
import unicodedata
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError


SERVER_DIR = Path(__file__).resolve().parent.parent
ROOT = SERVER_DIR.parent
CONTRACT = (ROOT / "contract" / "PROJECT-REPOSITORY-EDIT.md").read_text(
    encoding="utf-8"
)
CONTRACT_FLAT = " ".join(CONTRACT.split())
SCHEMA_PATH = ROOT / "contract" / "project-repository-edit.v1.schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

STATES = {
    "created",
    "staging",
    "staged",
    "awaiting_confirmation",
    "publishing",
    "published",
    "rejected",
    "conflicted",
    "failed",
    "superseded",
    "rolled_back",
}

STAGED_RECEIPT_FIELDS = {
    "contract",
    "edit_id",
    "state",
    "operation",
    "source_edit_id",
    "actor_binding_id",
    "tenant_id",
    "organization_id",
    "project_id",
    "repo_key",
    "base_commit",
    "staged_head_commit",
    "changed_paths",
    "diff_digest",
    "instruction_digest",
    "idempotency_key",
}

VALID_RECEIPT = {
    "contract": "leaf.project-repository-edit.v1",
    "edit_id": "11111111-1111-4111-8111-111111111111",
    "state": "staged",
    "operation": "edit",
    "source_edit_id": None,
    "actor_binding_id": "22222222-2222-4222-8222-222222222222",
    "tenant_id": "33333333-3333-4333-8333-333333333333",
    "organization_id": "33333333-3333-4333-8333-333333333333",
    "project_id": "44444444-4444-4444-8444-444444444444",
    "repo_key": "55555555-5555-4555-8555-555555555555",
    "base_commit": "a" * 40,
    "staged_head_commit": "b" * 40,
    "changed_paths": ["tools/count/tool.py", "ui/index.html"],
    "diff_digest": "c" * 64,
    "instruction_digest": "d" * 64,
    "idempotency_key": "edit-once",
}


def _validator(definition: str) -> Draft202012Validator:
    return Draft202012Validator(
        {"$ref": f"#/$defs/{definition}", "$defs": SCHEMA["$defs"]},
        format_checker=FormatChecker(),
    )


def _lease_key(receipt: dict[str, object]) -> tuple[object, ...]:
    fields = SCHEMA["$defs"]["repositoryAuthority"]["required"]
    return tuple(receipt[field] for field in fields)


def _semantic_paths(paths: list[str]) -> list[str]:
    if paths != sorted(paths, key=lambda path: path.encode("utf-8")):
        raise ValueError("paths are not sorted by UTF-8 bytes")
    aliases: set[str] = set()
    for path in paths:
        if not unicodedata.is_normalized("NFC", path):
            raise ValueError("path is not Unicode NFC")
        alias = unicodedata.normalize(
            "NFC", unicodedata.normalize("NFC", path).casefold()
        )
        if alias in aliases:
            raise ValueError("path has a Unicode case-fold alias")
        aliases.add(alias)
    return paths


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    parsed: dict[str, object] = {}
    for key, value in pairs:
        if key in parsed:
            raise ValueError(f"duplicate JSON key: {key}")
        parsed[key] = value
    return parsed


def _receipt_digest(receipt_json: str) -> str:
    parsed = json.loads(receipt_json, object_pairs_hook=_reject_duplicate_keys)
    _validator("stagedReceipt").validate(parsed)
    _semantic_paths(parsed["changed_paths"])
    canonical = json.dumps(
        parsed,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def test_schema_is_valid_draft_2020_12():
    Draft202012Validator.check_schema(SCHEMA)
    assert SCHEMA["$id"] == (
        "https://leafautomation.ai/contracts/"
        "project-repository-edit.v1.schema.json"
    )


def test_state_vocabulary_is_frozen():
    assert set(SCHEMA["$defs"]["state"]["enum"]) == STATES
    assert (
        "created -> staging -> staged -> awaiting_confirmation -> publishing -> published"
        in CONTRACT
    )


def test_staged_receipt_has_exact_authority_and_git_fields():
    receipt = SCHEMA["$defs"]["stagedReceipt"]
    assert receipt["additionalProperties"] is False
    assert set(receipt["properties"]) == STAGED_RECEIPT_FIELDS
    assert set(receipt["required"]) == STAGED_RECEIPT_FIELDS
    _validator("stagedReceipt").validate(VALID_RECEIPT)


def test_repository_lease_contention_excludes_actor_but_includes_repo_authority():
    authority = SCHEMA["$defs"]["repositoryAuthority"]
    assert authority["additionalProperties"] is False
    assert set(authority["required"]) == {
        "tenant_id",
        "organization_id",
        "project_id",
        "repo_key",
    }
    assert "actor_binding_id" not in authority["properties"]

    second_actor = {
        **VALID_RECEIPT,
        "actor_binding_id": "77777777-7777-4777-8777-777777777777",
    }
    other_repo = {
        **VALID_RECEIPT,
        "repo_key": "88888888-8888-4888-8888-888888888888",
    }
    assert _lease_key(VALID_RECEIPT) == _lease_key(second_actor)
    assert _lease_key(VALID_RECEIPT) != _lease_key(other_repo)
    assert "Two actors targeting the same repository must contend" in CONTRACT_FLAT
    assert "Read and writer leases contend on this same key" in CONTRACT_FLAT
    assert "Every lease carries a unique generation" in CONTRACT_FLAT
    assert "generation-fence every root resolution" in CONTRACT_FLAT


def test_receipt_refuses_client_filesystem_authority_and_extra_fields():
    assert "repoDir" not in json.dumps(SCHEMA)
    assert "An absolute path is not a wire field" in CONTRACT_FLAT
    assert "`repoDir` MUST NOT be accepted" in CONTRACT_FLAT
    bad = {**VALID_RECEIPT, "repoDir": "C:/tenant-controlled"}
    with pytest.raises(ValidationError):
        _validator("stagedReceipt").validate(bad)


@pytest.mark.parametrize(
    "path",
    [
        "/absolute/file.txt",
        "C:/drive/file.txt",
        "../escape.txt",
        "safe/../escape.txt",
        "./alias.txt",
        "safe//alias.txt",
        ".git/config",
        ".GIT/config",
        "safe/.git/config",
        "safe/.Git/config",
        "safe\\windows.txt",
        "nul\x00byte.txt",
        "directory/",
    ],
)
def test_schema_rejects_lexically_unsafe_changed_paths(path: str):
    bad = {**VALID_RECEIPT, "changed_paths": [path]}
    with pytest.raises(ValidationError):
        _validator("stagedReceipt").validate(bad)


def test_contract_bans_link_gitlink_and_alias_escape_paths():
    for phrase in (
        "symbolic links",
        "Windows junctions",
        "reparse points",
        "Git submodules and gitlinks",
        "mode `160000`",
        "Unicode aliases",
        "changed paths come from the trusted Git diff",
    ):
        assert phrase in CONTRACT


def test_semantic_path_validator_rejects_case_and_unicode_aliases():
    assert _semantic_paths(["A.txt"]) == ["A.txt"]
    with pytest.raises(ValueError, match="not sorted"):
        _semantic_paths(["ui/index.html", "tools/count/tool.py"])
    with pytest.raises(ValueError, match="case-fold alias"):
        _semantic_paths(["A.txt", "a.txt"])

    composed = "caf\u00e9.txt"
    decomposed = "cafe\u0301.txt"
    assert unicodedata.normalize("NFC", decomposed) == composed
    with pytest.raises(ValueError, match="not Unicode NFC"):
        _semantic_paths([decomposed])
    with pytest.raises(ValueError):
        _semantic_paths([composed, decomposed])

    assert "JSON Schema enforces the lexical subset only" in CONTRACT_FLAT
    assert "mandatory semantic validator" in CONTRACT_FLAT


def test_rollbacks_require_a_source_and_edits_forbid_one():
    validator = _validator("stagedReceipt")
    rollback = {
        **VALID_RECEIPT,
        "operation": "rollback",
        "source_edit_id": "66666666-6666-4666-8666-666666666666",
    }
    validator.validate(rollback)

    with pytest.raises(ValidationError):
        validator.validate({**rollback, "source_edit_id": None})
    with pytest.raises(ValidationError):
        validator.validate(
            {
                **VALID_RECEIPT,
                "source_edit_id": "66666666-6666-4666-8666-666666666666",
            }
        )


def test_confirmation_is_exact_ttl_bound_and_one_use():
    confirmation = SCHEMA["$defs"]["confirmationReceipt"]
    assert confirmation["additionalProperties"] is False
    assert {
        "confirmation_id",
        "receipt_digest",
        "approver_binding_id",
        "tenant_id",
        "organization_id",
        "project_id",
        "repo_key",
        "edit_id",
        "issued_at",
        "expires_at",
    }.issubset(confirmation["required"])
    assert "bounded TTL and can be consumed once" in CONTRACT
    assert "It is never a session grant" in CONTRACT_FLAT


def test_receipt_digest_is_jcs_stable_for_key_order_and_whitespace():
    compact = json.dumps(VALID_RECEIPT, ensure_ascii=False, separators=(",", ":"))
    reordered = json.dumps(
        dict(reversed(list(VALID_RECEIPT.items()))),
        ensure_ascii=False,
        indent=4,
    )
    first = _receipt_digest(compact)
    second = _receipt_digest(reordered)
    assert hmac.compare_digest(first.encode("ascii"), second.encode("ascii"))
    assert first == second
    assert "RFC 8785 JSON Canonicalization Scheme" in CONTRACT_FLAT
    assert "compare the lowercase digest bytes in constant time" in CONTRACT_FLAT

    duplicate = compact[:-1] + ',"contract":"leaf.project-repository-edit.v1"}'
    with pytest.raises(ValueError, match="duplicate JSON key"):
        _receipt_digest(duplicate)


def test_receipt_digest_preserves_unicode_and_rejects_non_nfc_paths():
    composed = {**VALID_RECEIPT, "changed_paths": ["caf\u00e9.txt"]}
    decomposed = {**VALID_RECEIPT, "changed_paths": ["cafe\u0301.txt"]}
    composed_digest = _receipt_digest(json.dumps(composed, ensure_ascii=False))
    with pytest.raises(ValueError, match="not Unicode NFC"):
        _receipt_digest(json.dumps(decomposed, ensure_ascii=False))
    assert len(composed_digest) == 64
    assert "JCS preserves Unicode code points and does not normalize strings" in CONTRACT_FLAT


def test_publish_is_receipt_bound_and_expected_old_sha_cas():
    publish = SCHEMA["$defs"]["publishRequest"]
    assert publish["additionalProperties"] is False
    assert set(publish["properties"]) == {
        "receipt",
        "receipt_digest",
        "confirmation_id",
        "expected_main_sha",
        "idempotency_key",
    }
    assert set(publish["required"]) == set(publish["properties"])
    assert "expected main SHA must equal the staged receipt's `base_commit`" in CONTRACT_FLAT
    assert "Git compare-and-swap" in CONTRACT
    assert "Force push, ref deletion" in CONTRACT_FLAT


def test_git_and_database_recovery_are_separate_domains():
    assert "Git and the coordination database are separate transactional domains" in CONTRACT
    assert "A Git update alone never proves a published database state" in CONTRACT_FLAT
    assert "a database pointer alone never proves Git publication" in CONTRACT_FLAT


def test_rollback_is_a_new_inverse_commit_with_confirmation_and_cas():
    assert "Rollback never rewinds main" in CONTRACT
    assert "creates a new isolated rollback edit" in CONTRACT
    assert "needs a fresh one-use confirmation" in CONTRACT
    assert "updates main with expected-old-SHA compare-and-swap" in CONTRACT


def test_project_authority_and_no_existence_oracle_are_frozen():
    assert "loads the project with both organization ID and project ID" in CONTRACT
    assert "explicit `project_repo_edit` entitlement" in CONTRACT
    assert "server-minted repository key" in CONTRACT
    assert "Unknown and foreign organization, project, repository" in CONTRACT
    assert "same 404 status and response shape" in CONTRACT


def test_single_operator_editor_is_explicitly_forbidden():
    assert "`SdkRepoEditor` MUST NOT be mounted or used" in CONTRACT
    assert "single-operator editor" in CONTRACT
    assert "closed repository-relative tool set" in CONTRACT


def test_changed_paths_and_free_text_are_server_derived_and_hash_only():
    changed_paths = SCHEMA["$defs"]["stagedReceipt"]["properties"]["changed_paths"]
    assert changed_paths["uniqueItems"] is True
    assert "sorted by UTF-8 byte order" in CONTRACT
    assert "changed paths come from the trusted Git diff" in CONTRACT
    assert "instruction" not in STAGED_RECEIPT_FIELDS
    assert "instruction_digest" in STAGED_RECEIPT_FIELDS
    assert "Free text and credential values are never persisted" in CONTRACT_FLAT


def test_contract_freeze_does_not_mount_or_enable_the_feature():
    assert "These routes remain unmounted" in CONTRACT
    assert "does not authorize a deployment or a live feature flag" in CONTRACT
