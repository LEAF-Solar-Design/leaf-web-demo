from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import threading

import pytest
import platform_semantic_qualification as qualification_module

from platform_semantic_eligibility import (
    ContractError,
    SERVICES,
    attach_integrity,
    fixture_signature_verifier,
    fixture_signer,
)
from platform_semantic_qualification import (
    KNOWN_SURFACES,
    QualificationLease,
    StageReceiptJournal,
    evaluate_fixture,
    run_fixture_qualification,
)


NOW = datetime(2026, 8, 12, 16, 0, tzinfo=UTC)
PRODUCER = "github.com/LEAF-Solar-Design/leaf-web-demo/.github/workflows/build-platform-images.yml"


def manifest() -> dict:
    unsigned = {
        "schema": "platform-qualification-manifest.v1",
        "repository": "LEAF-Solar-Design/leaf-web-demo",
        "source_commit": "a" * 40,
        "source_tree": "b" * 40,
        "services": [
            {
                "name": name,
                "image_digest": "sha256:" + format(index, "064x"),
                "provenance": "adopted" if index < 3 else "full_build",
                "entrypoint": ["/leaf", name],
            }
            for index, name in enumerate(SERVICES, start=1)
        ],
        "config_contract_digest": "sha256:" + "c" * 64,
        "image_aliases": [],
        "producer": {
            "identity": PRODUCER,
            "workflow": ".github/workflows/build-platform-images.yml",
            "run_id": 1,
            "attempt": 1,
        },
        "deployment_identity": {
            "schema": "leaf.deployment-identity.v1",
            "value": "sha256:" + "d" * 64,
        },
        "supported_deployment_path": "both",
        "verifier_version": "v1",
        "topology_version": "v1",
    }
    return attach_integrity(unsigned, fixture_signer)


def surface_counts() -> dict[str, int]:
    return {name: 0 for name in KNOWN_SURFACES}


def fixture() -> dict:
    return {
        "deployment_identity_count": 5,
        "deployment_identity_value": "sha256:" + "d" * 64,
        "app_to_harness_classification": "reached",
        "generic_removal_claimed": False,
        "ordinary_authoring_claimed": True,
        "lease_recovered": True,
        "publication_terminal_states": ["auto_published", "explicitly_approved"],
        "closed_projection_operator_authority": True,
        "closed_projection_tenant_markers": [],
        "tenants": {
            "tenant-a": {
                "marker": "tenant-a-marker",
                "observed_markers": ["tenant-a-marker"],
                "upload_status": "ready",
            },
            "tenant-b": {
                "marker": "tenant-b-marker",
                "observed_markers": ["tenant-b-marker"],
                "upload_status": "ready",
            },
        },
        "cleanup_pre": surface_counts(),
        "cleanup_post": surface_counts(),
        "rollback": {
            "restored": True,
            "images_rebuilt": False,
            "service_definitions_mutated": False,
        },
    }


def run(tmp_path: Path, **overrides):
    arguments = {
        "manifest": manifest(),
        "fixture": fixture(),
        "output_dir": tmp_path,
        "expected_producer": PRODUCER,
        "verifier_version": "v1",
        "topology_version": "v1",
        "signature_verifier": fixture_signature_verifier,
        "receipt_signer": fixture_signer,
        "allow_fixture_receipt": True,
        "now": NOW,
    }
    arguments.update(overrides)
    return run_fixture_qualification(**arguments)


def test_positive_production_shaped_fixture_emits_exactly_one_receipt(tmp_path: Path):
    first = run(tmp_path)
    second = run(tmp_path)

    assert first == second
    assert first["schema"] == "leaf.platform-semantic-eligibility.v1"
    assert len(first["assertions"]) == 8
    assert list(tmp_path.glob("semantic-eligibility.json")) == [tmp_path / "semantic-eligibility.json"]
    journal = json.loads((tmp_path / "stage-receipts.json").read_text(encoding="utf-8"))
    assert [entry["stage_id"] for entry in journal["receipts"]] == ["manifest", "semantic", "receipt"]


def test_default_path_is_unconfigured_and_writes_nothing(tmp_path: Path):
    with pytest.raises(ContractError, match="UNCONFIGURED"):
        run(tmp_path, allow_fixture_receipt=False)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda value: value.update(app_to_harness_classification="intercepted"), "HARNESS_AUTH_CLOSED"),
        (lambda value: value.update(generic_removal_claimed=True), "REMOVAL_WORKER_FENCE_FAILED"),
        (lambda value: value.update(ordinary_authoring_claimed=False), "AUTHORING_RECOVERY_FAILED"),
        (lambda value: value.update(lease_recovered=False), "AUTHORING_RECOVERY_FAILED"),
        (lambda value: value.update(publication_terminal_states=["staging"]), "PUBLICATION_STATE_INVALID"),
        (lambda value: value.update(closed_projection_tenant_markers=["tenant-a-marker"]), "CLOSED_PROJECTION_INVALID"),
        (lambda value: value["rollback"].update(images_rebuilt=True), "ROLLBACK_INVALID"),
    ],
)
def test_observed_failure_shapes_fail_closed_before_receipt(tmp_path: Path, mutate, code):
    value = fixture()
    mutate(value)
    with pytest.raises(ContractError, match=code):
        run(tmp_path, fixture=value)
    assert not (tmp_path / "semantic-eligibility.json").exists()


def test_cross_tenant_marker_swap_and_disclosure_fail_closed(tmp_path: Path):
    swapped = fixture()
    swapped["tenants"]["tenant-b"]["observed_markers"] = ["tenant-a-marker"]
    with pytest.raises(ContractError, match="TENANT_ISOLATION_FAILED"):
        run(tmp_path / "swap", fixture=swapped)

    duplicate = fixture()
    duplicate["tenants"]["tenant-b"]["marker"] = "tenant-a-marker"
    duplicate["tenants"]["tenant-b"]["observed_markers"] = ["tenant-a-marker"]
    with pytest.raises(ContractError, match="TENANT_ISOLATION_FAILED"):
        run(tmp_path / "duplicate", fixture=duplicate)


def test_unknown_or_known_cleanup_residue_blocks_receipt(tmp_path: Path):
    unknown = fixture()
    unknown["cleanup_post"]["unknown_surface"] = 1
    with pytest.raises(ContractError, match="CLEANUP_CENSUS_INVALID"):
        run(tmp_path / "unknown", fixture=unknown)

    known = fixture()
    known["cleanup_post"]["leases"] = 1
    with pytest.raises(ContractError, match="CLEANUP_RESIDUE"):
        run(tmp_path / "known", fixture=known)


def test_missing_deployment_identity_fails_before_stage_mutation(tmp_path: Path):
    value = manifest()
    value["deployment_identity"]["value"] = ""
    value.pop("signature")
    value.pop("payload_digest")
    value = attach_integrity(value, fixture_signer)
    with pytest.raises(ContractError):
        run(tmp_path, manifest=value)
    assert not (tmp_path / "stage-receipts.json").exists()


def test_kill_after_durable_stage_resumes_without_repeating_completed_mutation(
    tmp_path: Path, monkeypatch,
):
    with pytest.raises(ContractError, match="FIXTURE_STOP"):
        run(tmp_path, stop_after_stage="semantic")

    def repeated_semantic_mutation(*_args, **_kwargs):
        raise AssertionError("the completed semantic mutation ran again")

    monkeypatch.setattr(qualification_module, "evaluate_fixture", repeated_semantic_mutation)

    receipt = run(tmp_path)

    assert receipt["schema"] == "leaf.platform-semantic-eligibility.v1"
    journal = json.loads((tmp_path / "stage-receipts.json").read_text(encoding="utf-8"))
    assert [entry["stage_id"] for entry in journal["receipts"]].count("semantic") == 1


def test_recovered_semantic_cache_is_bound_to_durable_stage_receipt(tmp_path: Path):
    with pytest.raises(ContractError, match="FIXTURE_STOP"):
        run(tmp_path, stop_after_stage="semantic")
    cache_path = tmp_path / "semantic-stage.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    cache["assertions"][0]["evidence_digest"] = "sha256:" + "0" * 64
    cache_path.write_text(json.dumps(cache), encoding="utf-8")

    with pytest.raises(ContractError, match="STAGE_CACHE_INVALID"):
        run(tmp_path)
    assert not (tmp_path / "semantic-eligibility.json").exists()


def test_exact_completed_qualification_replays_byte_stable_receipt(tmp_path: Path):
    first = run(tmp_path, now=NOW)
    first_bytes = (tmp_path / "semantic-eligibility.json").read_bytes()

    replay = run(tmp_path, now=NOW.replace(hour=17))

    assert replay == first
    assert (tmp_path / "semantic-eligibility.json").read_bytes() == first_bytes


def test_stage_journal_serializes_concurrent_readers_and_runs_mutation_once(tmp_path: Path):
    lease = QualificationLease("a" * 40, "sha256:" + "b" * 64, "fixture-only")
    journal = StageReceiptJournal(tmp_path / "journal.json", lease)
    calls = 0
    calls_lock = threading.Lock()
    recovered: list[bool] = []

    def mutation():
        nonlocal calls
        with calls_lock:
            calls += 1
        return ["mutation-1"], "verified"

    def worker():
        _receipt, was_recovered = journal.run_stage("stage-a", mutation, now=NOW)
        recovered.append(was_recovered)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls == 1
    assert recovered.count(False) == 1
    assert recovered.count(True) == 3


def test_stage_journal_rejects_foreign_lease_and_tampering(tmp_path: Path):
    path = tmp_path / "journal.json"
    lease = QualificationLease("a" * 40, "sha256:" + "b" * 64, "owner-a")
    StageReceiptJournal(path, lease).run_stage("stage-a", lambda: (["one"], "done"), now=NOW)

    foreign = QualificationLease("a" * 40, "sha256:" + "b" * 64, "owner-b")
    with pytest.raises(ContractError, match="STAGE_LEASE_MISMATCH"):
        StageReceiptJournal(path, foreign).run_stage("stage-a", lambda: (["two"], "done"), now=NOW)

    value = json.loads(path.read_text(encoding="utf-8"))
    value["receipts"][0]["written_after"] = "tampered"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ContractError, match="STAGE_DIGEST_INVALID"):
        StageReceiptJournal(path, lease).run_stage("stage-b", lambda: (["two"], "done"), now=NOW)


def test_fixture_contract_rejects_extra_fields():
    value = fixture()
    value["new_surface"] = "not frozen"
    with pytest.raises(ContractError, match="FIXTURE_INVALID"):
        evaluate_fixture(value, manifest())
