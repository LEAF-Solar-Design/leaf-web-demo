import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from platform_surface_receipt import ContractError, verify_surface_result
from platform_release_manifest import _canonical_sha256

DIGESTS = {"app": "sha256:" + "1" * 64}


def _write_surface_artifact(
    root: Path, *, outcome: str = "skipped", mutate=None
) -> tuple[Path, dict, dict]:
    digest = DIGESTS["app"]
    convergence = "convergence-12345678"
    snapshot = {
        "task_definition": "arn:aws:ecs:us-east-1:123:task-definition/app:1",
        "deployment_id": "ecs-svc/123",
        "desired": 1,
        "running": 1,
        "pending": 0,
        "route_hash": "1" * 64,
        "marker_census_sha256": "2" * 64,
    }
    if outcome == "skipped":
        receipt_name = "surface-skip-receipt.json"
        receipt = {
            "schema": "leaf.staging-surface-skip.v1",
            "release_source_revision": "a" * 40,
            "release_source_tree": "b" * 40,
            "supply_artifact": {
                "name": "supply",
                "id": 123,
                "sha256": "3" * 64,
                "producer_run": 456,
                "producer_attempt": 1,
            },
            "service": "app",
            "convergence_id": convergence,
            "candidate_image_digest": digest,
            "live_image_digest": digest,
            "component_producer_source_revision": "c" * 40,
            "component_producer_source_tree": "d" * 40,
            "surface_fingerprint": "4" * 64,
            "recipe_fingerprint": "5" * 64,
            "candidate_runtime_contract_sha256": "6" * 64,
            "live_runtime_contract_sha256": "6" * 64,
            "migration_fingerprint": "unchanged",
            "provenance": {
                "subject": "oci://app",
                "digest": digest,
                "workflow": "build.yml",
                "blob": "e" * 40,
                "run": 789,
                "attempt": 1,
            },
            "routed": {
                "color": "primary",
                "service": "app-blue",
                "primary_task_definition": snapshot["task_definition"],
                "route_priorities": [50, 51],
                "route_hash": snapshot["route_hash"],
                "desired": 1,
                "running": 1,
                "pending": 0,
                "healthy": True,
            },
            "before": dict(snapshot),
            "after": dict(snapshot),
            "aws_mutation_count": 0,
            "identity_stamped": False,
            "rollback_invoked": False,
            "migration_result": "unchanged",
            "lock_name": "leaf-platform-staging-ecs-mutation",
            "github_run_id": "12345",
            "started_at": "2026-08-11T00:00:00Z",
            "finished_at": "2026-08-11T00:00:01Z",
        }
        mutations = 0
    else:
        receipt_name = "surface-deploy-receipt.json"
        receipt = {
            "schema": "leaf.staging-surface-deploy.v1",
            "service": "app",
            "convergence_id": convergence,
            "candidate_image_digest": digest,
            "terminal_image_digest": digest,
            "terminal_task_definition": snapshot["task_definition"],
            "github_run_id": "12345",
            "aws_mutation_count": 1,
            "identity_stamped": False,
            "rollback_invoked": False,
        }
        mutations = 1
    if mutate is not None:
        mutate(receipt)
    producer_bytes = subprocess.run(
        ["jq", "-cS", "."], input=json.dumps(receipt).encode(),
        stdout=subprocess.PIPE, check=True,
    ).stdout.replace(b"\r\n", b"\n")
    receipt_hash = hashlib.sha256(producer_bytes).hexdigest()
    result = {
        "schema": "leaf.staging-surface-result.v1",
        "release_source_revision": "a" * 40,
        "service": "app",
        "convergence_id": convergence,
        "outcome": outcome,
        "candidate_image_digest": digest,
        "terminal_image_digest": digest,
        "terraform_workflow_blob": "f" * 40,
        "surface_receipt_sha256": receipt_hash,
        "aws_mutation_count": mutations,
    }
    root.mkdir()
    (root / receipt_name).write_text(json.dumps(receipt), encoding="utf-8")
    (root / "surface-result.json").write_text(json.dumps(result), encoding="utf-8")
    return root, result, receipt


def _verify_surface_artifact(root: Path) -> dict:
    return verify_surface_result(
        root,
        service="app",
        convergence_id="convergence-12345678",
        release_source_revision="a" * 40,
        candidate_digest=DIGESTS["app"],
        terraform_workflow_blob="f" * 40,
    )


@pytest.mark.parametrize("outcome", ["skipped", "deployed"])
def test_surface_result_verifies_exact_receipt_and_hash(tmp_path: Path, outcome: str):
    root, result, _ = _write_surface_artifact(tmp_path / "artifact", outcome=outcome)
    assert _verify_surface_artifact(root) == result


def test_surface_result_rejects_forged_receipt_hash(tmp_path: Path):
    root, _, _ = _write_surface_artifact(tmp_path / "artifact")
    result_path = root / "surface-result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["surface_receipt_sha256"] = "0" * 64
    result_path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(ContractError, match="bind its receipt"):
        _verify_surface_artifact(root)


def test_surface_result_rejects_missing_or_extra_receipt_files(tmp_path: Path):
    missing, _, _ = _write_surface_artifact(tmp_path / "missing")
    (missing / "surface-skip-receipt.json").unlink()
    with pytest.raises(ContractError, match="file set"):
        _verify_surface_artifact(missing)

    extra, _, _ = _write_surface_artifact(tmp_path / "extra")
    (extra / "untrusted.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ContractError, match="file set"):
        _verify_surface_artifact(extra)


def test_surface_skip_receipt_rejects_drift_even_with_matching_hash(tmp_path: Path):
    def drift(receipt: dict) -> None:
        receipt["after"]["deployment_id"] = "ecs-svc/drift"

    root, _, _ = _write_surface_artifact(tmp_path / "artifact", mutate=drift)
    with pytest.raises(ContractError, match="changed routed state"):
        _verify_surface_artifact(root)


def test_surface_hash_uses_producer_newline_without_changing_manifest_hash(tmp_path):
    root, result, receipt = _write_surface_artifact(tmp_path / "artifact", outcome="deployed")
    expected_manifest = hashlib.sha256(json.dumps(
        receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode()).hexdigest()
    assert _canonical_sha256(receipt) == expected_manifest
    assert result["surface_receipt_sha256"] != expected_manifest
    assert _verify_surface_artifact(root) == result


def test_surface_receipt_cli_accepts_current_producer(tmp_path):
    root, _, _ = _write_surface_artifact(tmp_path / "artifact", outcome="deployed")
    import sys
    result = subprocess.run([
        sys.executable, str(Path(__file__).with_name("platform_surface_receipt.py")),
        "--directory", str(root), "--service", "app",
        "--convergence-id", "convergence-12345678",
        "--release-source-revision", "a" * 40,
        "--candidate-digest", DIGESTS["app"], "--terraform-workflow-blob", "f" * 40,
    ], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
