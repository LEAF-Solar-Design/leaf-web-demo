from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
import subprocess
import sys

import pytest

from platform_build_relay_adoption_shadow import (
    ContractError,
    SERVICES,
    _canonical_sha256,
    compare_adoption,
    load_json,
)
from platform_release_manifest import REPOSITORIES, build_v3_manifest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "platform_build_relay_adoption_shadow.py"
SCHEMA = ROOT / "contract" / "platform-build-relay-adoption-shadow.v1.schema.json"
WORKFLOW = (
    ROOT / ".github" / "workflows" / "qualify-platform-build-relay-adoption-shadow.yml"
)
SOURCE = "1" * 40
TREE = "2" * 40
CHECKPOINT = "sha256:" + "3" * 64


def _manifest(disposition: str = "reused") -> dict:
    services = {}
    for index, name in enumerate(SERVICES, start=1):
        digest = "sha256:" + format(index, "064x")
        service = {
            "repository": REPOSITORIES[name],
            "image_digest": digest,
            "immutable_lookup_tag": "surface-v1-" + format(index + 10, "064x"),
            "producer_source_revision": SOURCE,
            "producer_source_tree": TREE,
            "surface_fingerprint": format(index + 20, "064x"),
            "recipe_fingerprint": format(index + 30, "064x"),
            "producer_workflow_path": ".github/workflows/build-platform-images.yml",
            "producer_workflow_blob": "4" * 40,
            "producer_run_id": 123456,
            "producer_run_attempt": 1,
            "provenance_subject": (
                "807034087062.dkr.ecr.us-east-1.amazonaws.com/"
                f"{REPOSITORIES[name]}"
            ),
            "provenance_digest": "sha256:" + format(index + 40, "064x"),
            "build_disposition": disposition,
        }
        if name == "canonical-worker":
            service["solver_provenance"] = {
                "solver_source_revision": "5" * 40,
                "solver_source_sha256": "6" * 64,
            }
        if name == "web":
            service["artifact_sha256"] = "7" * 64
        services[name] = service
    return build_v3_manifest(SOURCE, TREE, "123456", "1", services)


def _identity(manifest: dict) -> dict:
    return {
        "schema": "leaf.deployment-identity.v1",
        "environment": "staging",
        "source_revision": SOURCE,
        "services": {
            name: {
                "image_digest": manifest["services"][name]["image_digest"],
                "source_revision": SOURCE,
            }
            for name in SERVICES
        },
    }


def _evidence() -> dict:
    manifest = _manifest()
    identity = _identity(manifest)
    return {
        "schema": "leaf.platform-build-relay-adoption-shadow.v1",
        "envelope": {
            "supply_body_sha256": _canonical_sha256(manifest),
            "identity_body_sha256": _canonical_sha256(identity),
            "checkpoint_sha256": CHECKPOINT,
            "observed_at": "2026-08-13T08:00:00Z",
        },
        "selectors": {
            "digest_aware_reconcile": False,
            "marker_ledger_mode": "disabled",
        },
        "active_writers": 0,
        "open_markers": 0,
        "manifest": manifest,
        "identity": identity,
        "services": {
            name: {
                "predicate_body_sha256": manifest["services"][name][
                    "provenance_digest"
                ],
                "signed_predicate_verified": True,
                "registry_candidate_digest": manifest["services"][name][
                    "image_digest"
                ],
                "live_digest": manifest["services"][name]["image_digest"],
                "component_source_exact": True,
                "runtime_contract_exact": True,
                "migration_exact": True,
                "route_stable": True,
                "health_stable": True,
            }
            for name in SERVICES
        },
    }


def _rehash(evidence: dict) -> None:
    evidence["envelope"]["supply_body_sha256"] = _canonical_sha256(
        evidence["manifest"]
    )
    evidence["envelope"]["identity_body_sha256"] = (
        None if evidence["identity"] is None else _canonical_sha256(evidence["identity"])
    )


def test_five_verified_candidates_and_live_surfaces_are_fully_skipped() -> None:
    result = compare_adoption(_evidence())

    assert result["status"] == "comparison_ready"
    assert result["build_dispositions"] == {
        name: "shadow_adopt_build" for name in SERVICES
    }
    assert result["relay_dispositions"] == {
        name: "shadow_skip_relay" for name in SERVICES
    }
    assert result["identity_disposition"] == "shadow_keep_identity"
    assert result["projected_digests"] == {
        name: _evidence()["manifest"]["services"][name]["image_digest"]
        for name in SERVICES
    }
    assert result["selector_activation_authorized"] is False
    assert result["dispatch_authorized"] is False
    assert result["identity_restamp_authorized"] is False
    assert result["inferred_savings_seconds"] is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("signed_predicate_verified", False),
        ("registry_candidate_digest", None),
        ("registry_candidate_digest", "sha256:" + "f" * 64),
    ],
)
def test_unproven_candidate_requires_build_and_blocks_relay(
    field: str, value: object
) -> None:
    evidence = _evidence()
    evidence["services"]["broker"][field] = value

    result = compare_adoption(evidence)

    assert (result["status"], result["code"]) == ("blocked", "build_required")
    assert result["build_dispositions"]["broker"] == "shadow_build"
    assert result["relay_dispositions"] == {}
    assert result["identity_disposition"] is None


def test_manifest_history_does_not_override_current_verified_adoption() -> None:
    evidence = _evidence()
    evidence["manifest"] = _manifest("built")
    evidence["identity"] = _identity(evidence["manifest"])
    for name in SERVICES:
        evidence["services"][name]["predicate_body_sha256"] = evidence["manifest"][
            "services"
        ][name]["provenance_digest"]
        evidence["services"][name]["registry_candidate_digest"] = evidence[
            "manifest"
        ]["services"][name]["image_digest"]
    _rehash(evidence)

    result = compare_adoption(evidence)

    assert result["status"] == "comparison_ready"
    assert set(result["build_dispositions"].values()) == {"shadow_adopt_build"}


def test_predicate_body_hash_drift_fails_closed_before_decisions() -> None:
    evidence = _evidence()
    evidence["services"]["broker"]["predicate_body_sha256"] = (
        "sha256:" + "e" * 64
    )

    with pytest.raises(ContractError, match="predicate_body_hash_mismatch"):
        compare_adoption(evidence)


def test_stale_tail_projects_three_deploys_and_shadow_restamp() -> None:
    evidence = _evidence()
    for name in ("broker", "harness", "canonical-worker"):
        evidence["services"][name]["live_digest"] = "sha256:" + "f" * 64
    evidence["identity"] = None
    evidence["envelope"]["identity_body_sha256"] = None

    result = compare_adoption(evidence)

    assert list(result["relay_dispositions"]) == list(SERVICES)
    assert result["relay_dispositions"] == {
        "app": "shadow_skip_relay",
        "broker": "shadow_deploy",
        "canonical-worker": "shadow_deploy",
        "harness": "shadow_deploy",
        "web": "shadow_skip_relay",
    }
    assert result["identity_disposition"] == "shadow_identity_restamp"
    assert result["projected_digests"] == {
        name: evidence["manifest"]["services"][name]["image_digest"]
        for name in SERVICES
    }


@pytest.mark.parametrize(
    "field",
    [
        "component_source_exact",
        "runtime_contract_exact",
        "migration_exact",
        "route_stable",
        "health_stable",
    ],
)
def test_any_runtime_surface_drift_prevents_relay_skip(field: str) -> None:
    evidence = _evidence()
    evidence["services"]["app"][field] = False

    result = compare_adoption(evidence)

    assert result["relay_dispositions"]["app"] == "shadow_deploy"


@pytest.mark.parametrize("shape", ["absent", "source", "digest", "decorated"])
def test_nonexact_identity_cannot_be_kept(shape: str) -> None:
    evidence = _evidence()
    if shape == "absent":
        evidence["identity"] = None
        evidence["envelope"]["identity_body_sha256"] = None
    elif shape == "source":
        evidence["identity"]["source_revision"] = "9" * 40
        for service in evidence["identity"]["services"].values():
            service["source_revision"] = "9" * 40
        _rehash(evidence)
    elif shape == "digest":
        evidence["identity"]["services"]["web"]["image_digest"] = (
            "sha256:" + "9" * 64
        )
        _rehash(evidence)
    else:
        evidence["identity"]["body_sha256"] = "sha256:" + "9" * 64
        evidence["envelope"]["identity_body_sha256"] = _canonical_sha256(
            evidence["identity"]
        )

    if shape == "decorated":
        with pytest.raises(ContractError, match="deployment_identity_invalid"):
            compare_adoption(evidence)
    else:
        assert compare_adoption(evidence)["identity_disposition"] == (
            "shadow_identity_restamp"
        )


def test_identity_hash_mismatch_and_null_pair_fail_closed() -> None:
    evidence = _evidence()
    evidence["envelope"]["identity_body_sha256"] = "sha256:" + "9" * 64
    with pytest.raises(ContractError, match="deployment_identity_envelope_invalid"):
        compare_adoption(evidence)

    evidence = _evidence()
    evidence["identity"] = None
    with pytest.raises(ContractError, match="deployment_identity_envelope_invalid"):
        compare_adoption(evidence)


def test_supply_hash_and_canonical_manifest_drift_fail_closed() -> None:
    evidence = _evidence()
    evidence["envelope"]["supply_body_sha256"] = "sha256:" + "9" * 64
    with pytest.raises(ContractError, match="supply_body_hash_mismatch"):
        compare_adoption(evidence)

    evidence = _evidence()
    evidence["manifest"]["services"]["web"]["repository"] = "wrong"
    _rehash(evidence)
    with pytest.raises(ContractError, match="manifest_invalid"):
        compare_adoption(evidence)


@pytest.mark.parametrize(
    ("field", "value"),
    [("producer_source_revision", "8" * 40), ("producer_source_tree", "9" * 40)],
)
def test_service_producer_must_bind_release_source_and_tree(
    field: str, value: str
) -> None:
    evidence = _evidence()
    evidence["manifest"]["services"]["broker"][field] = value
    _rehash(evidence)

    with pytest.raises(ContractError, match="manifest_release_binding_mismatch"):
        compare_adoption(evidence)


@pytest.mark.parametrize(
    ("field", "code"),
    [("active_writers", "active_writer_present"), ("open_markers", "open_marker_present")],
)
def test_writer_or_marker_blocks(field: str, code: str) -> None:
    evidence = _evidence()
    evidence[field] = 1

    result = compare_adoption(evidence)

    assert (result["status"], result["code"]) == ("blocked", code)
    assert result["build_dispositions"] == {}


@pytest.mark.parametrize(
    "selectors",
    [
        {"digest_aware_reconcile": True, "marker_ledger_mode": "disabled"},
        {"digest_aware_reconcile": False, "marker_ledger_mode": "shadow"},
    ],
)
def test_enabled_selector_is_rejected(selectors: dict[str, object]) -> None:
    evidence = _evidence()
    evidence["selectors"] = selectors
    with pytest.raises(ContractError, match="selectors_must_remain_dormant"):
        compare_adoption(evidence)


@pytest.mark.parametrize(
    "observed_at",
    ["2026-08-13 08:00:00+00:00", "2026-W33-4T08:00:00+00:00", "bad"],
)
def test_non_rfc3339_observation_is_rejected(observed_at: str) -> None:
    evidence = _evidence()
    evidence["envelope"]["observed_at"] = observed_at
    with pytest.raises(ContractError, match="evidence_envelope_invalid"):
        compare_adoption(evidence)


def test_duplicate_keys_constants_extra_keys_and_missing_services_reject() -> None:
    with pytest.raises(ContractError, match="duplicate_json_key"):
        load_json(StringIO('{"schema":"a","schema":"b"}'))
    with pytest.raises(ContractError, match="nonstandard_json_constant"):
        load_json(StringIO('{"value":NaN}'))

    evidence = _evidence()
    evidence["raw_registry_body"] = {}
    with pytest.raises(ContractError):
        compare_adoption(evidence)
    evidence = _evidence()
    evidence["services"].pop("web")
    with pytest.raises(ContractError):
        compare_adoption(evidence)


def test_cli_output_is_closed_and_non_authoritative(tmp_path: Path) -> None:
    path = tmp_path / "input.json"
    path.write_text(json.dumps(_evidence()), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--input", str(path)],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    assert result["selector_activation_authorized"] is False
    assert result["dispatch_authorized"] is False
    assert result["identity_restamp_authorized"] is False
    assert "secret" not in completed.stdout.lower()
    assert "account" not in completed.stdout.lower()


def test_schema_and_workflow_freeze_the_dormant_boundary() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["identity"]["additionalProperties"] is False
    assert schema["$defs"]["service"]["additionalProperties"] is False

    text = WORKFLOW.read_text(encoding="utf-8")
    lowered = text.lower()
    assert "workflow_dispatch:" in text
    assert "contents: read" in text
    assert '"selector_activation_authorized"] is false' in lowered
    assert '"dispatch_authorized"] is false' in lowered
    assert '"identity_restamp_authorized"] is false' in lowered
    for forbidden in (
        "schedule:",
        "push:",
        "pull_request:",
        "workflow_call:",
        "workflow_run:",
        "id-token:",
        "secrets.",
        "configure-aws-credentials",
        "aws ",
        "docker ",
        "gh workflow",
        "repository_dispatch",
        "curl ",
    ):
        assert forbidden not in lowered
