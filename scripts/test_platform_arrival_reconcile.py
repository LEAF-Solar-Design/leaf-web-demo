from __future__ import annotations

from collections.abc import Iterator, Mapping
from copy import deepcopy
import importlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import sysconfig

import pytest
import yaml

from platform_arrival_reconcile import (
    FIXTURE_NOW,
    SERVICES,
    ValidatedArrivalBundle,
    reconcile_arrival,
    verify_arrival_bundle,
)
from platform_semantic_eligibility import ContractError, sha256_digest


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "contract" / "platform-arrival-reconciliation.v1.schema.json"
WORKFLOW = ROOT / ".github" / "workflows" / "qualify-platform-arrival-reconciliation.yml"
SCRIPT = ROOT / "scripts" / "platform_arrival_reconcile.py"
PR583 = "e3b709a08dd822e320cd4fb410f63e887aca0357"
PR585 = "ee71607c9d9a8b347f9526ca3ec9509919170d5d"
TREE583 = "d" * 40
TREE585 = "29e560c0f7639d6b8ab0e8381e8da9f8878d81a1"


def digest(label: str) -> str:
    return sha256_digest({"fixture": label})


def _without(value: Mapping[str, object], *keys: str) -> dict:
    excluded = set(keys)
    return {key: deepcopy(item) for key, item in value.items() if key not in excluded}


def _checksum(value: dict) -> None:
    value["checksum"] = sha256_digest(_without(value, "checksum"))


def _seal_supply(value: dict) -> None:
    manifest = value["manifest"]
    manifest["manifest_digest"] = sha256_digest(
        _without(manifest, "manifest_digest", "checksum")
    )
    _checksum(manifest)
    artifact = value["artifact"]
    artifact["content_digest"] = sha256_digest(artifact["content"])
    _checksum(artifact)
    _checksum(value)


def _seal_child(value: dict) -> None:
    value["receipt_digest"] = sha256_digest(
        _without(value, "receipt_digest", "checksum")
    )
    _checksum(value)


def _seal_convergence(value: dict) -> None:
    if value["live_identity"] is not None:
        _checksum(value["live_identity"])
    content = _without(
        value,
        "artifact_content_digest",
        "receipt_digest",
        "checksum",
    )
    value["artifact_content_digest"] = sha256_digest({"artifact": content})
    value["receipt_digest"] = sha256_digest({"receipt": content})
    _checksum(value)


def _seal_stage_receipt(value: dict) -> None:
    value["receipt_digest"] = sha256_digest(
        _without(value, "receipt_digest", "checksum")
    )
    _checksum(value)


def seal_bundle(value: dict) -> dict:
    value = deepcopy(value)
    _checksum(value["arrival"])
    _checksum(value["producer"])
    _seal_supply(value["supply"])
    _checksum(value["relay"])
    for child in value["child_runs"]:
        _seal_child(child)
    _seal_convergence(value["convergence"])
    if value["failed_stage"] is not None:
        _checksum(value["failed_stage"])
    frontier = value["frontier"]
    _seal_supply(frontier["current_supply"])
    for receipt in frontier["prior_successful_stage_receipts"]:
        _seal_stage_receipt(receipt)
    if frontier["arrival"] is not None:
        _checksum(frontier["arrival"])
    _checksum(frontier)
    value["bundle_digest"] = sha256_digest(_without(value, "bundle_digest"))
    return value


def service_rows(label: str) -> list[dict[str, str]]:
    return [
        {"name": name, "image_digest": digest(f"{label}-{name}")}
        for name in SERVICES
    ]


def supply(source: str, tree: str, label: str, artifact_id: int) -> dict:
    rows = service_rows(label)
    manifest = {
        "schema": "leaf.staging-supply-manifest.v1",
        "source_revision": source,
        "source_tree": tree,
        "services": deepcopy(rows),
        "manifest_digest": "",
        "checksum": "",
    }
    manifest["manifest_digest"] = sha256_digest(
        _without(manifest, "manifest_digest", "checksum")
    )
    artifact = {
        "artifact_id": artifact_id,
        "artifact_name": f"staging-supply-set-{source}-attempt-1",
        "content": {
            "manifest_digest": manifest["manifest_digest"],
            "source_revision": source,
            "source_tree": tree,
            "services": deepcopy(rows),
        },
        "content_digest": "",
        "checksum": "",
    }
    value = {"artifact": artifact, "manifest": manifest, "checksum": ""}
    _seal_supply(value)
    return value


def child_run(
    *,
    service: str,
    run_id: int,
    result: str,
    failed_stage: str | None,
    source: str,
    tree: str,
    release_supply: dict,
    relay_run_id: int,
    predecessor: str,
) -> dict:
    value = {
        "service": service,
        "run_id": run_id,
        "relay_run_id": relay_run_id,
        "source_revision": source,
        "source_tree": tree,
        "supply_artifact_id": release_supply["artifact"]["artifact_id"],
        "supply_artifact_content_digest": release_supply["artifact"]["content_digest"],
        "supply_manifest_digest": release_supply["manifest"]["manifest_digest"],
        "predecessor_receipt_digest": predecessor,
        "result": result,
        "failed_stage": failed_stage,
        "receipt_digest": "",
        "checksum": "",
    }
    _seal_child(value)
    return value


def live_identity(source: str, tree: str, release_supply: dict) -> dict:
    rows = [
        {
            "name": row["name"],
            "image_digest": row["image_digest"],
            "task_definition": f"leaf-platform-{row['name']}:{600 + index}",
        }
        for index, row in enumerate(release_supply["manifest"]["services"])
    ]
    value = {
        "source_revision": source,
        "source_tree": tree,
        "supply_manifest_digest": release_supply["manifest"]["manifest_digest"],
        "body_digest": digest(f"identity-{source}"),
        "services": rows,
        "checksum": "",
    }
    _checksum(value)
    return value


def stage_receipt(stage: str, source: str, tree: str, manifest: str) -> dict:
    value = {
        "stage": stage,
        "source_revision": source,
        "source_tree": tree,
        "supply_manifest_digest": manifest,
        "receipt_digest": "",
        "checksum": "",
    }
    _seal_stage_receipt(value)
    return value


def bundle(
    *,
    source: str,
    tree: str,
    pr_number: int,
    sequence: int,
    failed_harness: bool,
) -> dict:
    release_supply = supply(source, tree, f"pr-{pr_number}", 9_180_000_000 + pr_number)
    relay_run_id = 31_696_892_936 if failed_harness else 31_702_568_953
    relay_predecessor = digest(f"relay-predecessor-{pr_number}")
    relay = {
        "repository": "LEAF-Solar-Design/leaf-web-demo",
        "workflow": ".github/workflows/dispatch-staging-deploys.yml",
        "run_id": relay_run_id,
        "source_revision": source,
        "source_tree": tree,
        "supply_artifact_id": release_supply["artifact"]["artifact_id"],
        "supply_artifact_content_digest": release_supply["artifact"]["content_digest"],
        "supply_manifest_digest": release_supply["manifest"]["manifest_digest"],
        "predecessor_receipt_digest": relay_predecessor,
        "conclusion": "failure" if failed_harness else "success",
        "checksum": "",
    }
    _checksum(relay)
    children: list[dict] = []
    predecessor = relay_predecessor
    if failed_harness:
        child_specs = [
            ("web", 31_696_923_082, "success", None),
            ("harness", 31_697_109_132, "failure", "protected_input_validation"),
        ]
    else:
        child_specs = [
            (name, 31_702_570_000 + index, "success", None)
            for index, name in enumerate(SERVICES)
        ]
    for service, run_id, result, failed_stage in child_specs:
        child = child_run(
            service=service,
            run_id=run_id,
            result=result,
            failed_stage=failed_stage,
            source=source,
            tree=tree,
            release_supply=release_supply,
            relay_run_id=relay_run_id,
            predecessor=predecessor,
        )
        children.append(child)
        predecessor = child["receipt_digest"]
    convergence = {
        "artifact_id": 9_182_500_000 + pr_number,
        "artifact_content_digest": "",
        "receipt_digest": "",
        "source_revision": source,
        "source_tree": tree,
        "supply_manifest_digest": release_supply["manifest"]["manifest_digest"],
        "outcome": "failed" if failed_harness else "converged",
        "service_outcomes": [
            {
                "service": item["service"],
                "run_id": item["run_id"],
                "result": item["result"],
                "failed_stage": item["failed_stage"],
                "receipt_digest": item["receipt_digest"],
            }
            for item in children
        ],
        "live_identity": (
            None if failed_harness else live_identity(source, tree, release_supply)
        ),
        "checksum": "",
    }
    _seal_convergence(convergence)
    failed_stage = None
    if failed_harness:
        failed_stage = {
            "service": "harness",
            "run_id": 31_697_109_132,
            "stage": "protected_input_validation",
            "checksum": "",
        }
        _checksum(failed_stage)
    stages = ["build", "web"] if failed_harness else ["build", "relay", "convergence"]
    receipts = [
        stage_receipt(
            stage,
            source,
            tree,
            release_supply["manifest"]["manifest_digest"],
        )
        for stage in stages
    ]
    value = {
        "schema": "leaf.platform-arrival-bundle.v2",
        "version": 2,
        "environment": "staging",
        "topology_version": "leaf.platform-five-service.v1",
        "verifier_version": "leaf.platform-arrival-verifier.v2",
        "selectors": {"arrival_frontier": "UNCONFIGURED"},
        "issued_at_epoch": FIXTURE_NOW - 60,
        "expires_at_epoch": FIXTURE_NOW + 86_400,
        "arrival": {
            "repository": "LEAF-Solar-Design/leaf-web-demo",
            "pr_number": pr_number,
            "sequence": sequence,
            "previous_source_revision": "a" * 40,
            "source_revision": source,
            "source_tree": tree,
            "arrived_at_epoch": FIXTURE_NOW - 60,
            "checksum": "",
        },
        "producer": {
            "repository": "LEAF-Solar-Design/leaf-web-demo",
            "workflow": ".github/workflows/build-platform-images.yml",
            "run_id": 31_696_766_443 if failed_harness else 31_702_420_838,
            "source_revision": source,
            "source_tree": tree,
            "conclusion": "success",
            "checksum": "",
        },
        "supply": release_supply,
        "relay": relay,
        "child_runs": children,
        "convergence": convergence,
        "failed_stage": failed_stage,
        "frontier": {
            "current_sequence": sequence,
            "current_source_revision": source,
            "current_source_tree": tree,
            "current_supply": deepcopy(release_supply),
            "current_supply_manifest_digest": release_supply["manifest"]["manifest_digest"],
            "live_services": deepcopy(release_supply["manifest"]["services"]),
            "prior_successful_stage_receipts": receipts,
            "terminal_receipt_digest": (
                digest(f"prior-terminal-{pr_number}")
                if failed_harness
                else convergence["receipt_digest"]
            ),
            "arrival": None,
            "checksum": "",
        },
        "bundle_digest": "",
    }
    return seal_bundle(value)


def pr583_bundle() -> dict:
    return bundle(
        source=PR583,
        tree=TREE583,
        pr_number=583,
        sequence=583,
        failed_harness=True,
    )


def pr585_bundle() -> dict:
    return bundle(
        source=PR585,
        tree=TREE585,
        pr_number=585,
        sequence=585,
        failed_harness=False,
    )


def evaluate(value: object) -> dict:
    return reconcile_arrival(
        value,
        now_epoch=FIXTURE_NOW,
        fixture_enabled=True,
    )


def jsonschema_module():
    loaded = sys.modules.get("platform")
    if loaded is None or not hasattr(loaded, "python_implementation"):
        path = Path(sysconfig.get_path("stdlib")) / "platform.py"
        spec = importlib.util.spec_from_file_location("platform", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules["platform"] = module
        spec.loader.exec_module(module)
    return importlib.import_module("jsonschema")


def validate_schema(result: dict) -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema = jsonschema_module()
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(result)


class DuplicateKeyMapping(Mapping[str, object]):
    def __init__(self, value: dict[str, object]) -> None:
        self.value = value

    def __getitem__(self, key: str) -> object:
        return self.value[key]

    def __iter__(self) -> Iterator[str]:
        first = next(iter(self.value))
        yield first
        yield first
        yield from list(self.value)[1:]

    def __len__(self) -> int:
        return len(self.value) + 1


def test_pr583_resumes_only_exact_failed_harness_after_green_predecessors():
    value = pr583_bundle()
    result = evaluate(value)
    validate_schema(result)
    assert result["disposition"] == "resume_failed_stage"
    assert result["reason_code"] == "single_failed_child_with_bound_predecessors"
    assert result["resume_service"] == "harness"
    assert result["resume_stage"] == "protected_input_validation"
    assert len(result["prior_stage_receipt_digests"]) == 2
    assert not any(result["authority"].values())


def test_pr585_adopts_exact_five_service_terminal_frontier():
    value = pr585_bundle()
    validated = verify_arrival_bundle(value, now_epoch=FIXTURE_NOW)
    result = evaluate(value)
    validate_schema(result)
    assert isinstance(validated, ValidatedArrivalBundle)
    assert validated.content_digest == value["bundle_digest"]
    assert result["disposition"] == "adopt_frontier"
    assert result["reason_code"] == "exact_arrival_fully_converged"
    assert result["supply_manifest_digest"] == (
        value["supply"]["manifest"]["manifest_digest"]
    )


def test_newer_owned_active_arrival_serializes_without_authority():
    value = pr585_bundle()
    value["frontier"]["arrival"] = {
        "sequence": 586,
        "source_revision": "f" * 40,
        "source_tree": "e" * 40,
        "owner_class": "same_train",
        "state": "active",
        "bundle_digest": digest("newer-active"),
        "checksum": "",
    }
    result = evaluate(seal_bundle(value))
    assert result["disposition"] == "serialize_wait"
    assert result["reason_code"] == "newer_owned_arrival_active"
    assert not any(result["authority"].values())


def test_newer_terminal_arrival_supersedes_without_cancellation():
    value = pr585_bundle()
    newer_supply = supply("f" * 40, "e" * 40, "newer", 9_199_000_001)
    value["frontier"].update(
        current_sequence=586,
        current_source_revision="f" * 40,
        current_source_tree="e" * 40,
        current_supply=newer_supply,
        current_supply_manifest_digest=newer_supply["manifest"]["manifest_digest"],
        live_services=deepcopy(newer_supply["manifest"]["services"]),
        arrival={
            "sequence": 586,
            "source_revision": "f" * 40,
            "source_tree": "e" * 40,
            "owner_class": "external_owner",
            "state": "terminal",
            "bundle_digest": digest("newer-terminal"),
            "checksum": "",
        },
    )
    result = evaluate(seal_bundle(value))
    assert result["disposition"] == "superseded"
    assert result["reason_code"] == "newer_arrival_terminal"
    assert not result["authority"]["cancel"]


def test_r1_manifest_equality_and_foreign_producer_counterexamples_fail():
    value = pr585_bundle()
    value["frontier"]["current_supply_manifest_digest"] = digest("fabricated-equality")
    with pytest.raises(ContractError, match="FRONTIER_MANIFEST_MISMATCH"):
        evaluate(seal_bundle(value))

    value = pr585_bundle()
    value["producer"]["source_revision"] = "f" * 40
    with pytest.raises(ContractError, match="PRODUCER_ARRIVAL_LINEAGE_MISMATCH"):
        evaluate(seal_bundle(value))


def test_swapped_manifest_and_wrong_artifact_fail_after_resealing():
    value = pr585_bundle()
    other = supply(PR585, TREE585, "foreign", 9_199_000_002)
    value["supply"]["manifest"] = other["manifest"]
    with pytest.raises(ContractError, match="SUPPLY_ARTIFACT_MANIFEST_MISMATCH"):
        evaluate(seal_bundle(value))

    value = pr585_bundle()
    value["relay"]["supply_artifact_id"] += 1
    with pytest.raises(ContractError, match="RELAY_LINEAGE_MISMATCH"):
        evaluate(seal_bundle(value))


@pytest.mark.parametrize(
    "target",
    ["producer", "relay", "child", "convergence"],
)
def test_resealed_source_mismatch_at_each_release_layer_is_rejected(target):
    value = pr585_bundle()
    if target == "child":
        value["child_runs"][0]["source_revision"] = "f" * 40
    else:
        value[target]["source_revision"] = "f" * 40
    with pytest.raises(ContractError, match="LINEAGE|MISMATCH"):
        evaluate(seal_bundle(value))


def test_frontier_cross_release_requires_exact_newer_terminal_witness():
    value = pr585_bundle()
    other = supply("f" * 40, "e" * 40, "foreign-frontier", 9_199_000_003)
    value["frontier"].update(
        current_source_revision="f" * 40,
        current_source_tree="e" * 40,
        current_supply=other,
        current_supply_manifest_digest=other["manifest"]["manifest_digest"],
        live_services=deepcopy(other["manifest"]["services"]),
    )
    with pytest.raises(ContractError, match="FRONTIER_LINEAGE_UNEXPLAINED"):
        evaluate(seal_bundle(value))


def test_failed_stage_must_name_the_observed_failed_child():
    value = pr583_bundle()
    value["failed_stage"]["run_id"] += 1
    with pytest.raises(ContractError, match="FAILED_STAGE_NOT_OBSERVED"):
        evaluate(seal_bundle(value))


def test_duplicate_missing_reordered_and_mixed_child_runs_fail():
    value = pr585_bundle()
    value["child_runs"][1]["service"] = value["child_runs"][0]["service"]
    with pytest.raises(ContractError, match="DUPLICATE_CHILD_RUN"):
        evaluate(seal_bundle(value))

    value = pr585_bundle()
    del value["child_runs"][-1]
    value["convergence"]["service_outcomes"] = value["convergence"]["service_outcomes"][:-1]
    with pytest.raises(ContractError, match="CONVERGENCE_INCOMPLETE"):
        evaluate(seal_bundle(value))

    value = pr585_bundle()
    value["child_runs"][0], value["child_runs"][1] = (
        value["child_runs"][1],
        value["child_runs"][0],
    )
    with pytest.raises(
        ContractError,
        match="CHILD_RUN_ORDER_INVALID|CHILD_PREDECESSOR_MISMATCH",
    ):
        evaluate(seal_bundle(value))

    value = pr585_bundle()
    value["child_runs"][1]["predecessor_receipt_digest"] = digest("mixed")
    with pytest.raises(ContractError, match="CHILD_PREDECESSOR_MISMATCH"):
        evaluate(seal_bundle(value))


def test_stale_frontier_and_foreign_later_arrival_block():
    value = pr585_bundle()
    value["frontier"]["current_sequence"] = 584
    result = evaluate(seal_bundle(value))
    assert result["disposition"] == "blocked"
    assert result["reason_code"] == "verified_evidence_incomplete"

    value = pr585_bundle()
    value["frontier"]["arrival"] = {
        "sequence": 586,
        "source_revision": "f" * 40,
        "source_tree": "e" * 40,
        "owner_class": "external_owner",
        "state": "active",
        "bundle_digest": digest("foreign-active"),
        "checksum": "",
    }
    result = evaluate(seal_bundle(value))
    assert result["disposition"] == "blocked"
    assert result["reason_code"] == "newer_arrival_unowned"


def test_fabricated_checksum_extra_expired_and_digest_shortcuts_fail():
    value = pr585_bundle()
    value["producer"]["conclusion"] = "failure"
    with pytest.raises(ContractError, match="PRODUCER_CHECKSUM_MISMATCH"):
        evaluate(value)

    value = pr585_bundle()
    value["raw_log"] = "forbidden"
    with pytest.raises(ContractError, match="ARRIVAL_BUNDLE_INVALID"):
        evaluate(value)

    value = pr585_bundle()
    with pytest.raises(ContractError, match="ARRIVAL_BUNDLE_EXPIRED"):
        reconcile_arrival(
            value,
            now_epoch=value["expires_at_epoch"],
            fixture_enabled=True,
        )

    with pytest.raises(ContractError, match="ARRIVAL_BUNDLE_INVALID"):
        evaluate(pr585_bundle()["bundle_digest"])

    with pytest.raises(ContractError, match="ARRIVAL_BUNDLE_INVALID"):
        evaluate(DuplicateKeyMapping(pr585_bundle()))


def test_validated_bundle_has_no_public_constructor_or_consumer_shortcut():
    with pytest.raises(TypeError, match="no public constructor"):
        ValidatedArrivalBundle()
    validated = verify_arrival_bundle(pr585_bundle(), now_epoch=FIXTURE_NOW)
    with pytest.raises(ContractError, match="ARRIVAL_BUNDLE_INVALID"):
        evaluate(validated)


def test_default_path_closed_result_and_workflow_are_dormant():
    with pytest.raises(ContractError, match="UNCONFIGURED"):
        reconcile_arrival(pr585_bundle(), now_epoch=FIXTURE_NOW)
    result = evaluate(pr585_bundle())
    lowered = json.dumps(result).casefold()
    for token in ("tenant", "subject", "secret", "raw_log", "request_body", "exception"):
        assert token not in lowered
    assert not any(result["authority"].values())

    parsed = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    trigger = parsed.get("on", parsed.get(True))
    assert set(trigger) == {"workflow_dispatch"}
    assert parsed["permissions"] == {"contents": "read"}
    source = WORKFLOW.read_text(encoding="utf-8")
    for forbidden in (
        "aws-actions/configure-aws-credentials",
        "gh workflow run",
        "gh api",
        "workflow_call",
        "schedule:",
        "push:",
        "pull_request:",
        "upload-artifact",
    ):
        assert forbidden not in source
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "workflow-preflight",
            "--shadow-enabled",
            "false",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 78
    preflight = json.loads(completed.stdout)
    assert preflight["state"] == "UNCONFIGURED"
    assert preflight["provider_calls"] == 0
    assert preflight["receipt_published"] is False
    assert preflight["dispatch_authorized"] is False
    assert preflight["cancel_authorized"] is False
    assert preflight["live_mutation_authorized"] is False


def test_product_module_has_no_provider_process_or_network_client():
    source = SCRIPT.read_text(encoding="utf-8")
    for forbidden in (
        "boto3",
        "requests",
        "httpx",
        "urllib",
        "subprocess",
        "socket",
    ):
        assert forbidden not in source
