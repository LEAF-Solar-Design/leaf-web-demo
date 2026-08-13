#!/usr/bin/env python3
"""Compare dormant digest and indexed-marker evidence without activation."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Mapping, TextIO


INPUT_SCHEMA = "leaf.platform-convergence-shadow.v1"
RESULT_SCHEMA = "leaf.platform-convergence-shadow-result.v1"
SERVICES = ("web", "app", "broker", "harness", "canonical-worker")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


class ContractError(ValueError):
    """Captured evidence violates the frozen shadow contract."""


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ContractError("duplicate_json_key")
        value[key] = item
    return value


def _constant(_: str) -> None:
    raise ContractError("nonstandard_json_constant")


def load_json(stream: TextIO) -> dict[str, Any]:
    try:
        value = json.load(
            stream, object_pairs_hook=_object, parse_constant=_constant
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("shadow_json_invalid") from exc
    if not isinstance(value, dict):
        raise ContractError("shadow_root_invalid")
    return value


def _exact(value: Any, keys: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ContractError(code)
    return value


def _pattern(value: Any, pattern: re.Pattern[str], code: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ContractError(code)
    return value


def _integer(value: Any, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError(code)
    return value


def _boolean(value: Any, code: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(code)
    return value


def _nullable_digest(value: Any, code: str) -> str | None:
    return None if value is None else _pattern(value, _DIGEST, code)


def _checkpoint(value: Any) -> dict[str, Any]:
    result = _exact(
        value,
        {
            "source_commit",
            "source_tree",
            "supply_sha256",
            "deployment_identity_sha256",
            "checkpoint_sha256",
            "observed_at",
        },
        "checkpoint_invalid",
    )
    observed = result["observed_at"]
    if not isinstance(observed, str) or _RFC3339.fullmatch(observed) is None:
        raise ContractError("checkpoint_invalid")
    try:
        parsed = datetime.fromisoformat(observed.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("checkpoint_invalid") from exc
    if parsed.tzinfo is None:
        raise ContractError("checkpoint_invalid")
    return {
        "source_commit": _pattern(result["source_commit"], _SHA, "checkpoint_invalid"),
        "source_tree": _pattern(result["source_tree"], _SHA, "checkpoint_invalid"),
        "supply_sha256": _pattern(result["supply_sha256"], _DIGEST, "checkpoint_invalid"),
        "deployment_identity_sha256": _pattern(
            result["deployment_identity_sha256"], _DIGEST, "checkpoint_invalid"
        ),
        "checkpoint_sha256": _pattern(
            result["checkpoint_sha256"], _DIGEST, "checkpoint_invalid"
        ),
        "observed_at": observed,
    }


def _marker_result(value: Any, code: str) -> str:
    if value not in {"EMPTY", "OPEN"}:
        raise ContractError(code)
    return value


def _full_scan(value: Any) -> dict[str, Any]:
    result = _exact(
        value,
        {
            "schema",
            "workflow_blob",
            "checkpoint_sha256",
            "result",
            "open_count",
            "open_set_sha256",
            "duration_seconds",
        },
        "full_scan_invalid",
    )
    duration = result["duration_seconds"]
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration <= 0
    ):
        raise ContractError("full_scan_invalid")
    if result["schema"] != "leaf.legacy-marker-census.v1":
        raise ContractError("full_scan_invalid")
    return {
        "schema": result["schema"],
        "workflow_blob": _pattern(result["workflow_blob"], _SHA, "full_scan_invalid"),
        "checkpoint_sha256": _pattern(
            result["checkpoint_sha256"], _DIGEST, "full_scan_invalid"
        ),
        "result": _marker_result(result["result"], "full_scan_invalid"),
        "open_count": _integer(result["open_count"], "full_scan_invalid"),
        "open_set_sha256": _pattern(
            result["open_set_sha256"], _HASH, "full_scan_invalid"
        ),
        "duration_seconds": float(duration),
    }


def _indexed(value: Any) -> dict[str, Any]:
    envelope = _exact(
        value, {"checkpoint_sha256", "receipt"}, "indexed_marker_invalid"
    )
    result = _exact(
        envelope["receipt"],
        {
            "schema",
            "result",
            "strong_consistent",
            "open_count",
            "open_set_sha256",
        },
        "indexed_marker_invalid",
    )
    if result["schema"] != "leaf.staging-marker-ledger-census.v1":
        raise ContractError("indexed_marker_invalid")
    return {
        "schema": result["schema"],
        "checkpoint_sha256": _pattern(
            envelope["checkpoint_sha256"], _DIGEST, "indexed_marker_invalid"
        ),
        "result": _marker_result(result["result"], "indexed_marker_invalid"),
        "strong_consistent": _boolean(
            result["strong_consistent"], "indexed_marker_invalid"
        ),
        "open_count": _integer(result["open_count"], "indexed_marker_invalid"),
        "open_set_sha256": _pattern(
            result["open_set_sha256"], _HASH, "indexed_marker_invalid"
        ),
    }


SERVICE_KEYS = {
    "candidate_digest",
    "live_digest",
    "expected_component_source_sha256",
    "live_component_source_sha256",
    "expected_runtime_contract_sha256",
    "live_runtime_contract_sha256",
    "expected_migration_fingerprint",
    "live_migration_fingerprint",
    "route_stable",
    "health_stable",
}


def _service(value: Any) -> dict[str, Any]:
    result = _exact(value, SERVICE_KEYS, "service_evidence_invalid")
    return {
        "candidate_digest": _pattern(
            result["candidate_digest"], _DIGEST, "service_evidence_invalid"
        ),
        "live_digest": _pattern(
            result["live_digest"], _DIGEST, "service_evidence_invalid"
        ),
        "expected_component_source_sha256": _pattern(
            result["expected_component_source_sha256"],
            _DIGEST,
            "service_evidence_invalid",
        ),
        "live_component_source_sha256": _pattern(
            result["live_component_source_sha256"],
            _DIGEST,
            "service_evidence_invalid",
        ),
        "expected_runtime_contract_sha256": _pattern(
            result["expected_runtime_contract_sha256"],
            _DIGEST,
            "service_evidence_invalid",
        ),
        "live_runtime_contract_sha256": _pattern(
            result["live_runtime_contract_sha256"],
            _DIGEST,
            "service_evidence_invalid",
        ),
        "expected_migration_fingerprint": _nullable_digest(
            result["expected_migration_fingerprint"], "service_evidence_invalid"
        ),
        "live_migration_fingerprint": _nullable_digest(
            result["live_migration_fingerprint"], "service_evidence_invalid"
        ),
        "route_stable": _boolean(result["route_stable"], "service_evidence_invalid"),
        "health_stable": _boolean(result["health_stable"], "service_evidence_invalid"),
    }


def validate_shadow(value: Any) -> dict[str, Any]:
    root = _exact(
        value,
        {"schema", "checkpoint", "selectors", "active_writers", "markers", "services"},
        "shadow_contract_invalid",
    )
    if root["schema"] != INPUT_SCHEMA:
        raise ContractError("shadow_contract_invalid")
    selectors = _exact(
        root["selectors"],
        {"digest_aware_reconcile", "marker_ledger_mode"},
        "selectors_must_remain_dormant",
    )
    if selectors != {
        "digest_aware_reconcile": False,
        "marker_ledger_mode": "disabled",
    }:
        raise ContractError("selectors_must_remain_dormant")
    markers = _exact(root["markers"], {"full_scan", "indexed"}, "markers_invalid")
    services = _exact(root["services"], set(SERVICES), "services_invalid")
    return {
        "schema": INPUT_SCHEMA,
        "checkpoint": _checkpoint(root["checkpoint"]),
        "selectors": selectors,
        "active_writers": _integer(root["active_writers"], "active_writers_invalid"),
        "markers": {
            "full_scan": _full_scan(markers["full_scan"]),
            "indexed": _indexed(markers["indexed"]),
        },
        "services": {name: _service(services[name]) for name in SERVICES},
    }


def _base(evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": RESULT_SCHEMA,
        "source_commit": evidence["checkpoint"]["source_commit"],
        "checkpoint_sha256": evidence["checkpoint"]["checkpoint_sha256"],
        "status": "comparison_ready",
        "code": "shadow_parity",
        "marker_parity": True,
        "dispositions": {},
        "measured_full_scan_seconds": evidence["markers"]["full_scan"][
            "duration_seconds"
        ],
        "inferred_savings_seconds": None,
        "selector_activation_authorized": False,
        "dispatch_authorized": False,
    }


def _blocked(evidence: Mapping[str, Any], code: str) -> dict[str, Any]:
    result = _base(evidence)
    result.update(status="blocked", code=code, marker_parity=False)
    return result


def compare_shadow(value: Any) -> dict[str, Any]:
    evidence = validate_shadow(value)
    if evidence["active_writers"]:
        return _blocked(evidence, "active_writer_present")
    checkpoint = evidence["checkpoint"]["checkpoint_sha256"]
    full = evidence["markers"]["full_scan"]
    indexed = evidence["markers"]["indexed"]
    if (
        not indexed["strong_consistent"]
        or full["checkpoint_sha256"] != checkpoint
        or indexed["checkpoint_sha256"] != checkpoint
        or full["result"] != indexed["result"]
        or full["open_count"] != indexed["open_count"]
        or full["open_set_sha256"] != indexed["open_set_sha256"]
    ):
        return _blocked(evidence, "marker_shadow_mismatch")
    if full["open_count"] or full["result"] != "EMPTY":
        return _blocked(evidence, "open_marker_present")

    result = _base(evidence)
    for name in SERVICES:
        service = evidence["services"][name]
        exact = (
            service["candidate_digest"] == service["live_digest"]
            and service["expected_component_source_sha256"]
            == service["live_component_source_sha256"]
            and service["expected_runtime_contract_sha256"]
            == service["live_runtime_contract_sha256"]
            and service["expected_migration_fingerprint"]
            == service["live_migration_fingerprint"]
            and service["route_stable"]
            and service["health_stable"]
        )
        result["dispositions"][name] = "shadow_skip" if exact else "shadow_deploy"
    return result


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="-")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        if args.input == "-":
            evidence = load_json(sys.stdin)
        else:
            with Path(args.input).open(encoding="utf-8") as handle:
                evidence = load_json(handle)
        result = compare_shadow(evidence)
    except (ContractError, OSError) as exc:
        print(
            canonical_json(
                {
                    "schema": RESULT_SCHEMA,
                    "status": "invalid",
                    "code": str(exc),
                    "selector_activation_authorized": False,
                    "dispatch_authorized": False,
                }
            )
        )
        return 2
    rendered = canonical_json(result) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8", newline="\n")
    sys.stdout.write(rendered)
    return 78 if result["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
