#!/usr/bin/env python3
"""Dependency-light validator for the executor benchmark contract."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = ROOT / "scenario-manifest.json"
SCHEMA_PATH = ROOT / "result-schema.json"
FIXTURE_PATH = ROOT / "fixtures" / "synthetic-result.json"
FORBIDDEN_INSTANT_DEPENDENCIES = ("redis", "s3", "docker_ecs", "broker", "database")


class ContractError(ValueError):
    """A benchmark document does not meet the frozen contract."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"{path}: cannot read JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{path}: top level must be an object")
    return value


def require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ContractError(f"{where}: missing {key}")
    return mapping[key]


def nearest_rank(samples: list[float], percentile: int) -> float:
    if not samples:
        raise ContractError("percentiles need at least one successful sample")
    ordered = sorted(samples)
    index = max(math.ceil(percentile / 100 * len(ordered)), 1) - 1
    return ordered[index]


def validate_manifest(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if manifest.get("contract_version") != "executor-bench.v1":
        raise ContractError("manifest: unsupported contract_version")
    scenarios = require(manifest, "scenarios", "manifest")
    if not isinstance(scenarios, list) or len(scenarios) != 7:
        raise ContractError("manifest: exactly seven scenarios are required")
    by_id: dict[str, dict[str, Any]] = {}
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            raise ContractError("manifest: every scenario must be an object")
        scenario_id = require(scenario, "id", "manifest scenario")
        if not isinstance(scenario_id, str) or scenario_id in by_id:
            raise ContractError("manifest: scenario ids must be unique strings")
        require(scenario, "description", f"manifest scenario {scenario_id}")
        require(scenario, "acceptance", f"manifest scenario {scenario_id}")
        by_id[scenario_id] = scenario
    expected = {
        "prepare_before_first_call", "warm_invocation_startup", "trivial_cloud_rpc",
        "concurrent_sessions_per_host", "code_reload", "host_drain", "batch_handoff",
    }
    if set(by_id) != expected:
        raise ContractError("manifest: scenario set does not match executor-bench.v1")
    if by_id["warm_invocation_startup"]["acceptance"].get("p95_lt_ms") != 10:
        raise ContractError("manifest: warm startup p95 target must be under 10 ms")
    rpc = by_id["trivial_cloud_rpc"]
    if rpc["acceptance"].get("p99_lt_ms") != 100 or rpc.get("report_percentiles") != [50, 95, 99]:
        raise ContractError("manifest: trivial RPC must report p50/p95/p99 and target p99 under 100 ms")
    if rpc.get("expected_planning_range_ms") != {"min": 20, "max": 100}:
        raise ContractError("manifest: trivial RPC planning range must be 20 to 100 ms")
    instant = require(require(manifest, "shared_requirements", "manifest"), "instant_path", "manifest shared_requirements")
    if instant.get("docker_start_delta_must_equal") != 0:
        raise ContractError("manifest: instant calls must prohibit Docker starts")
    if tuple(instant.get("forbidden_per_call_dependencies", [])) != FORBIDDEN_INSTANT_DEPENDENCIES:
        raise ContractError("manifest: instant dependency proof set changed")
    return by_id


def validate_schema(schema: dict[str, Any]) -> None:
    if schema.get("title") != "Executor benchmark result":
        raise ContractError("schema: unexpected title")
    required = set(require(schema, "required", "schema"))
    needed = {"contract_version", "evidence_kind", "synthetic", "run", "scenario_results", "overall_acceptance"}
    if not needed <= required:
        raise ContractError("schema: missing top-level required fields")
    run_required = set(schema["properties"]["run"]["required"])
    context = {"environment", "source_sha", "host_shape", "concurrency", "payload_bytes", "code_digest", "warm_cold_classification", "sample_count", "failures"}
    if not context <= run_required:
        raise ContractError("schema: missing required run context")


def validate_percentiles(result: dict[str, Any], where: str) -> None:
    samples = require(result, "latency_samples_ms", where)
    count = require(result, "sample_count", where)
    if not isinstance(samples, list) or not samples or not all(isinstance(x, (int, float)) and x >= 0 for x in samples):
        raise ContractError(f"{where}: latency_samples_ms must contain non-negative numbers")
    if count != len(samples):
        raise ContractError(f"{where}: sample_count must equal successful latency sample count")
    recorded = require(result, "percentiles_ms", where)
    if not isinstance(recorded, dict):
        raise ContractError(f"{where}: percentiles_ms must be an object")
    for percentile in (50, 95, 99):
        key = f"p{percentile}"
        if key not in recorded:
            raise ContractError(f"{where}: missing {key}")
        if recorded[key] != nearest_rank(samples, percentile):
            raise ContractError(f"{where}: {key} does not use nearest-rank percentile")


def validate_instant_proof(result: dict[str, Any], where: str) -> None:
    proof = require(result, "instant_path_proof", where)
    if not isinstance(proof, dict):
        raise ContractError(f"{where}: instant_path_proof must be an object")
    if not proof.get("applicable"):
        return
    if proof.get("docker_start_delta") != 0:
        raise ContractError(f"{where}: instant call started Docker or a container")
    operations = proof.get("dependency_operations")
    if not isinstance(operations, dict):
        raise ContractError(f"{where}: missing dependency operation counters")
    for dependency in FORBIDDEN_INSTANT_DEPENDENCIES:
        if operations.get(dependency) != 0:
            raise ContractError(f"{where}: instant path used {dependency}")
    if proof.get("sentinel_test_passed") is not True:
        raise ContractError(f"{where}: fail-fast dependency sentinel did not pass")
    if not isinstance(proof.get("trace_request_id"), str) or not proof["trace_request_id"]:
        raise ContractError(f"{where}: missing request trace identifier")


def computed_pass(scenario_id: str, result: dict[str, Any], scenario: dict[str, Any]) -> bool:
    if result.get("failures"):
        return False
    acceptance = scenario["acceptance"]
    if scenario_id == "prepare_before_first_call":
        events = result.get("event_timeline")
        required_events = scenario["required_events_in_order"]
        if (not isinstance(events, list) or not all(isinstance(event, dict) for event in events)
                or [event.get("event") for event in events] != required_events):
            return False
        timestamps = [event.get("monotonic_ms") for event in events]
        if not all(isinstance(timestamp, (int, float)) for timestamp in timestamps):
            return False
        return (result.get("preparation_before_first_call") is True
                and all(before < after for before, after in zip(timestamps, timestamps[1:])))
    if scenario_id == "warm_invocation_startup":
        return result["percentiles_ms"]["p95"] < acceptance["p95_lt_ms"]
    if scenario_id == "trivial_cloud_rpc":
        return result["percentiles_ms"]["p99"] < acceptance["p99_lt_ms"]
    if scenario_id == "concurrent_sessions_per_host":
        return (result.get("isolation_breach") is False
                and result.get("tested_concurrency_levels") == scenario["required_concurrency_levels"])
    if scenario_id == "code_reload":
        return result.get("stale_digest_execution") is False and result.get("recovery_reported") is True
    if scenario_id == "host_drain":
        return result.get("assignment_after_drain") is False and result.get("all_sessions_terminal") is True
    if scenario_id == "batch_handoff":
        return (result.get("http_status") == 202 and isinstance(result.get("job_id"), str)
                and bool(result["job_id"]) and result.get("inline_terminal_result") is False)
    raise ContractError(f"unknown scenario {scenario_id}")


def validate_result(result: dict[str, Any], scenarios: dict[str, dict[str, Any]]) -> None:
    if result.get("contract_version") != "executor-bench.v1":
        raise ContractError("result: unsupported contract_version")
    synthetic = result.get("synthetic")
    evidence_kind = result.get("evidence_kind")
    if not isinstance(synthetic, bool) or (synthetic != (evidence_kind == "synthetic_fixture")):
        raise ContractError("result: synthetic and evidence_kind disagree")
    run = require(result, "run", "result")
    if not isinstance(run, dict):
        raise ContractError("result: run must be an object")
    for field in ("environment", "source_sha", "host_shape", "concurrency", "payload_bytes", "code_digest", "warm_cold_classification", "sample_count", "failures"):
        require(run, field, "result.run")
    if not isinstance(run["environment"], str) or not run["environment"]:
        raise ContractError("result.run: environment must be a non-empty string")
    if not isinstance(run["source_sha"], str) or not re.fullmatch(r"[0-9a-f]{7,64}", run["source_sha"]):
        raise ContractError("result.run: source_sha must be a 7 to 64 digit lowercase hexadecimal SHA")
    host_shape = run["host_shape"]
    if not isinstance(host_shape, dict) or not all(key in host_shape for key in ("provider", "instance_type", "cpu", "memory_mib")):
        raise ContractError("result.run: host_shape must name provider, instance type, CPU, and memory")
    if not isinstance(run["concurrency"], int) or run["concurrency"] < 1:
        raise ContractError("result.run: concurrency must be a positive integer")
    if not isinstance(run["payload_bytes"], int) or run["payload_bytes"] < 0:
        raise ContractError("result.run: payload_bytes must be a non-negative integer")
    if not isinstance(run["code_digest"], str) or not run["code_digest"]:
        raise ContractError("result.run: code_digest must be a non-empty string")
    if run["warm_cold_classification"] not in {"warm", "cold", "mixed", "batch"}:
        raise ContractError("result.run: invalid warm/cold classification")
    if not isinstance(run["sample_count"], int) or run["sample_count"] < 1 or not isinstance(run["failures"], list):
        raise ContractError("result.run: sample_count and failures are invalid")
    results = require(result, "scenario_results", "result")
    if not isinstance(results, list) or len(results) != len(scenarios):
        raise ContractError("result: one result is required for each scenario")
    seen: set[str] = set()
    all_pass = True
    for scenario_result in results:
        if not isinstance(scenario_result, dict):
            raise ContractError("result: scenario result must be an object")
        scenario_id = require(scenario_result, "scenario_id", "scenario result")
        if scenario_id not in scenarios or scenario_id in seen:
            raise ContractError(f"result: invalid or duplicate scenario {scenario_id}")
        seen.add(scenario_id)
        where = f"scenario {scenario_id}"
        if not isinstance(scenario_result.get("failures"), list):
            raise ContractError(f"{where}: failures must be an array")
        validate_percentiles(scenario_result, where)
        validate_instant_proof(scenario_result, where)
        passed = computed_pass(scenario_id, scenario_result, scenarios[scenario_id])
        if scenario_id == "prepare_before_first_call" and not passed:
            raise ContractError(f"{where}: preparation timeline must strictly precede the first call")
        all_pass = all_pass and passed
        verdict = require(require(scenario_result, "acceptance", where), "verdict", where)
        expected_verdict = "NOT_EVALUATED" if synthetic else ("PASS" if passed else "FAIL")
        if verdict != expected_verdict:
            raise ContractError(f"{where}: verdict must be {expected_verdict}")
    if seen != set(scenarios):
        raise ContractError("result: missing benchmark scenario")
    overall = require(require(result, "overall_acceptance", "result"), "verdict", "result.overall_acceptance")
    expected_overall = "NOT_EVALUATED" if synthetic else ("PASS" if all_pass else "FAIL")
    if overall != expected_overall:
        raise ContractError(f"result: overall verdict must be {expected_overall}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, default=FIXTURE_PATH, help="benchmark result JSON to validate")
    args = parser.parse_args(argv)
    try:
        scenarios = validate_manifest(load_json(MANIFEST_PATH))
        validate_schema(load_json(SCHEMA_PATH))
        validate_result(load_json(args.result), scenarios)
    except ContractError as exc:
        print(f"FAIL: {exc}")
        return 1
    print(f"PASS: executor benchmark contract is valid ({args.result})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
