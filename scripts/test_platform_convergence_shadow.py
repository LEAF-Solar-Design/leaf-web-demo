from __future__ import annotations

from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys

import pytest

from platform_convergence_shadow import (
    ContractError,
    SERVICES,
    compare_shadow,
    load_json,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "platform_convergence_shadow.py"
SCHEMA = ROOT / "contract" / "platform-convergence-shadow.v1.schema.json"
WORKFLOW = ROOT / ".github" / "workflows" / "qualify-platform-convergence-shadow.yml"
DIGEST = "sha256:" + "a" * 64
EMPTY_SET = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
CHECKPOINT = "sha256:" + "b" * 64


def _service(index: int) -> dict[str, object]:
    image = "sha256:" + format(index, "064x")
    component = "sha256:" + format(index + 10, "064x")
    runtime = "sha256:" + format(index + 20, "064x")
    migration = "sha256:" + format(index + 30, "064x") if index == 2 else None
    return {
        "candidate_digest": image,
        "live_digest": image,
        "expected_component_source_sha256": component,
        "live_component_source_sha256": component,
        "expected_runtime_contract_sha256": runtime,
        "live_runtime_contract_sha256": runtime,
        "expected_migration_fingerprint": migration,
        "live_migration_fingerprint": migration,
        "route_stable": True,
        "health_stable": True,
    }


def _evidence() -> dict[str, object]:
    return {
        "schema": "leaf.platform-convergence-shadow.v1",
        "checkpoint": {
            "source_commit": "1" * 40,
            "source_tree": "2" * 40,
            "supply_sha256": "sha256:" + "3" * 64,
            "deployment_identity_sha256": "sha256:" + "4" * 64,
            "checkpoint_sha256": CHECKPOINT,
            "observed_at": "2026-08-13T08:00:00Z",
        },
        "selectors": {
            "digest_aware_reconcile": False,
            "marker_ledger_mode": "disabled",
        },
        "active_writers": 0,
        "markers": {
            "full_scan": {
                "schema": "leaf.legacy-marker-census.v1",
                "workflow_blob": "5" * 40,
                "checkpoint_sha256": CHECKPOINT,
                "result": "EMPTY",
                "open_count": 0,
                "open_set_sha256": EMPTY_SET,
                "duration_seconds": 395.6,
            },
            "indexed": {
                "checkpoint_sha256": CHECKPOINT,
                "receipt": {
                    "schema": "leaf.staging-marker-ledger-census.v1",
                    "result": "EMPTY",
                    "strong_consistent": True,
                    "open_count": 0,
                    "open_set_sha256": EMPTY_SET,
                },
            },
        },
        "services": {
            name: _service(index) for index, name in enumerate(SERVICES, start=1)
        },
    }


def test_exact_parity_yields_five_shadow_skips_without_authority() -> None:
    result = compare_shadow(_evidence())

    assert result == {
        "schema": "leaf.platform-convergence-shadow-result.v1",
        "source_commit": "1" * 40,
        "checkpoint_sha256": CHECKPOINT,
        "status": "comparison_ready",
        "code": "shadow_parity",
        "marker_parity": True,
        "dispositions": {name: "shadow_skip" for name in SERVICES},
        "measured_full_scan_seconds": 395.6,
        "inferred_savings_seconds": None,
        "selector_activation_authorized": False,
        "dispatch_authorized": False,
    }


def test_stale_tail_yields_only_three_ordered_shadow_deploys() -> None:
    evidence = _evidence()
    services = evidence["services"]
    assert isinstance(services, dict)
    for name in ("broker", "harness", "canonical-worker"):
        services[name]["live_digest"] = DIGEST

    result = compare_shadow(evidence)

    assert list(result["dispositions"]) == list(SERVICES)
    assert result["dispositions"] == {
        "web": "shadow_skip",
        "app": "shadow_skip",
        "broker": "shadow_deploy",
        "harness": "shadow_deploy",
        "canonical-worker": "shadow_deploy",
    }


@pytest.mark.parametrize(
    ("service", "field", "value"),
    [
        ("web", "live_digest", DIGEST),
        ("broker", "live_component_source_sha256", DIGEST),
        ("harness", "live_runtime_contract_sha256", DIGEST),
        ("app", "live_migration_fingerprint", DIGEST),
        ("canonical-worker", "route_stable", False),
        ("web", "health_stable", False),
    ],
)
def test_any_surface_drift_prevents_skip(
    service: str, field: str, value: object
) -> None:
    evidence = _evidence()
    services = evidence["services"]
    assert isinstance(services, dict)
    services[service][field] = value

    result = compare_shadow(evidence)

    assert result["dispositions"][service] == "shadow_deploy"
    assert sum(item == "shadow_deploy" for item in result["dispositions"].values()) == 1


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("indexed", "open_count", 1),
        ("indexed", "result", "OPEN"),
        ("indexed", "open_set_sha256", "a" * 64),
        ("indexed", "checkpoint_sha256", DIGEST),
        ("indexed", "strong_consistent", False),
        ("full_scan", "checkpoint_sha256", DIGEST),
    ],
)
def test_marker_disagreement_blocks(
    section: str, field: str, value: object
) -> None:
    evidence = _evidence()
    markers = evidence["markers"]
    assert isinstance(markers, dict)
    target = markers[section]
    if section == "indexed" and field != "checkpoint_sha256":
        target = target["receipt"]
    target[field] = value

    result = compare_shadow(evidence)

    assert result["status"] == "blocked"
    assert result["code"] == "marker_shadow_mismatch"
    assert result["dispositions"] == {}
    assert result["dispatch_authorized"] is False


def test_matched_open_markers_block() -> None:
    evidence = _evidence()
    markers = evidence["markers"]
    assert isinstance(markers, dict)
    markers["full_scan"].update(
        result="OPEN", open_count=1, open_set_sha256="a" * 64
    )
    markers["indexed"]["receipt"].update(
        result="OPEN", open_count=1, open_set_sha256="a" * 64
    )

    result = compare_shadow(evidence)

    assert (result["status"], result["code"]) == (
        "blocked",
        "open_marker_present",
    )


def test_active_writer_blocks_before_dispositions() -> None:
    evidence = _evidence()
    evidence["active_writers"] = 1

    result = compare_shadow(evidence)

    assert result["code"] == "active_writer_present"
    assert result["dispositions"] == {}


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
        compare_shadow(evidence)


@pytest.mark.parametrize(
    "observed_at",
    [
        "2026-08-13 08:00:00+00:00",
        "2026-W33-4T08:00:00+00:00",
        "2026-08-13T08:00:00",
        "2026-08-13T08:00Z",
        "2026-13-13T08:00:00Z",
    ],
)
def test_non_rfc3339_observation_time_is_rejected(observed_at: str) -> None:
    evidence = _evidence()
    evidence["checkpoint"]["observed_at"] = observed_at

    with pytest.raises(ContractError, match="checkpoint_invalid"):
        compare_shadow(evidence)


def test_duplicate_json_key_is_rejected() -> None:
    with pytest.raises(ContractError, match="duplicate_json_key"):
        load_json(StringIO('{"schema":"first","schema":"second"}'))


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonstandard_json_constants_are_rejected(constant: str) -> None:
    with pytest.raises(ContractError, match="nonstandard_json_constant"):
        load_json(StringIO('{"duration":' + constant + "}"))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(raw_marker={"body": "forbidden"}),
        lambda value: value["checkpoint"].update(checkpoint_sha256="bad"),
        lambda value: value["services"].pop("web"),
        lambda value: value["markers"]["full_scan"].update(duration_seconds=True),
        lambda value: value["markers"]["full_scan"].update(duration_seconds=0),
        lambda value: value["markers"]["full_scan"].update(duration_seconds=float("nan")),
        lambda value: value["markers"]["full_scan"].update(duration_seconds=float("inf")),
        lambda value: value["markers"]["indexed"]["receipt"].update(raw_rows=[]),
    ],
)
def test_invalid_or_raw_evidence_is_rejected(mutate) -> None:
    evidence = _evidence()
    mutate(evidence)

    with pytest.raises(ContractError):
        compare_shadow(evidence)


def test_cli_emits_only_closed_non_authoritative_result(tmp_path: Path) -> None:
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(_evidence()), encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--input", str(input_path)],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )

    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    assert result["selector_activation_authorized"] is False
    assert result["dispatch_authorized"] is False
    rendered = completed.stdout.lower()
    for forbidden in ("token", "secret", "tenant", "account", "task_arn", "marker_body"):
        assert forbidden not in rendered


def test_schema_is_closed_and_matches_runtime_fixture() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["additionalProperties"] is False
    assert schema["properties"]["selectors"]["properties"] == {
        "digest_aware_reconcile": {"const": False},
        "marker_ledger_mode": {"const": "disabled"},
    }
    assert schema["$defs"]["service"]["additionalProperties"] is False
    assert compare_shadow(deepcopy(_evidence()))["status"] == "comparison_ready"


def test_workflow_is_manual_read_only_and_cannot_dispatch_or_activate() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    lowered = text.lower()

    assert "workflow_dispatch:" in text
    assert "contents: read" in text
    assert '"selector_activation_authorized"] is false' in lowered
    assert '"dispatch_authorized"] is false' in lowered
    assert "selectors remain off" in lowered
    assert "no provider was queried" in lowered
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
        "gh workflow",
        "repository_dispatch",
        "workflow_dispatches",
        "curl ",
    ):
        assert forbidden not in lowered
