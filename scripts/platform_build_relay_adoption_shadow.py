#!/usr/bin/env python3
"""Compile a dormant build, relay, and identity adoption comparison."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping, TextIO

from platform_release_manifest import (
    ContractError as ManifestContractError,
    SERVICES,
    validate_v3_manifest,
)


INPUT_SCHEMA = "leaf.platform-build-relay-adoption-shadow.v1"
RESULT_SCHEMA = "leaf.platform-build-relay-adoption-shadow-result.v1"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


class ContractError(ValueError):
    """Captured evidence violates the frozen adoption-shadow contract."""


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
        raise ContractError("adoption_shadow_json_invalid") from exc
    if not isinstance(value, dict):
        raise ContractError("adoption_shadow_root_invalid")
    return value


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("ascii")).hexdigest()


def _exact(value: Any, keys: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ContractError(code)
    return value


def _pattern(value: Any, pattern: re.Pattern[str], code: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ContractError(code)
    return value


def _boolean(value: Any, code: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(code)
    return value


def _integer(value: Any, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError(code)
    return value


def _nullable_digest(value: Any, code: str) -> str | None:
    return None if value is None else _pattern(value, _DIGEST, code)


def _envelope(value: Any) -> dict[str, Any]:
    result = _exact(
        value,
        {
            "supply_body_sha256",
            "identity_body_sha256",
            "checkpoint_sha256",
            "observed_at",
        },
        "evidence_envelope_invalid",
    )
    observed = result["observed_at"]
    if not isinstance(observed, str) or _RFC3339.fullmatch(observed) is None:
        raise ContractError("evidence_envelope_invalid")
    try:
        parsed = datetime.fromisoformat(observed.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("evidence_envelope_invalid") from exc
    if parsed.tzinfo is None:
        raise ContractError("evidence_envelope_invalid")
    return {
        "supply_body_sha256": _pattern(
            result["supply_body_sha256"], _DIGEST, "evidence_envelope_invalid"
        ),
        "identity_body_sha256": _nullable_digest(
            result["identity_body_sha256"], "evidence_envelope_invalid"
        ),
        "checkpoint_sha256": _pattern(
            result["checkpoint_sha256"], _DIGEST, "evidence_envelope_invalid"
        ),
        "observed_at": observed,
    }


IDENTITY_KEYS = {"schema", "environment", "source_revision", "services"}
IDENTITY_SERVICE_KEYS = {"image_digest", "source_revision"}


def _identity(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    identity = _exact(value, IDENTITY_KEYS, "deployment_identity_invalid")
    if (
        identity["schema"] != "leaf.deployment-identity.v1"
        or identity["environment"] != "staging"
    ):
        raise ContractError("deployment_identity_invalid")
    source = _pattern(
        identity["source_revision"], _SHA, "deployment_identity_invalid"
    )
    services = _exact(
        identity["services"], set(SERVICES), "deployment_identity_invalid"
    )
    normalized: dict[str, dict[str, str]] = {}
    for name in SERVICES:
        service = _exact(
            services[name], IDENTITY_SERVICE_KEYS, "deployment_identity_invalid"
        )
        if service["source_revision"] != source:
            raise ContractError("deployment_identity_invalid")
        normalized[name] = {
            "image_digest": _pattern(
                service["image_digest"], _DIGEST, "deployment_identity_invalid"
            ),
            "source_revision": source,
        }
    return {
        "schema": identity["schema"],
        "environment": identity["environment"],
        "source_revision": source,
        "services": normalized,
    }


SERVICE_KEYS = {
    "predicate_body_sha256",
    "signed_predicate_verified",
    "registry_candidate_digest",
    "live_digest",
    "component_source_exact",
    "runtime_contract_exact",
    "migration_exact",
    "route_stable",
    "health_stable",
}


def _service(value: Any) -> dict[str, Any]:
    service = _exact(value, SERVICE_KEYS, "service_evidence_invalid")
    return {
        "predicate_body_sha256": _pattern(
            service["predicate_body_sha256"], _DIGEST, "service_evidence_invalid"
        ),
        "signed_predicate_verified": _boolean(
            service["signed_predicate_verified"], "service_evidence_invalid"
        ),
        "registry_candidate_digest": _nullable_digest(
            service["registry_candidate_digest"], "service_evidence_invalid"
        ),
        "live_digest": _pattern(
            service["live_digest"], _DIGEST, "service_evidence_invalid"
        ),
        "component_source_exact": _boolean(
            service["component_source_exact"], "service_evidence_invalid"
        ),
        "runtime_contract_exact": _boolean(
            service["runtime_contract_exact"], "service_evidence_invalid"
        ),
        "migration_exact": _boolean(
            service["migration_exact"], "service_evidence_invalid"
        ),
        "route_stable": _boolean(service["route_stable"], "service_evidence_invalid"),
        "health_stable": _boolean(
            service["health_stable"], "service_evidence_invalid"
        ),
    }


def validate_evidence(value: Any) -> dict[str, Any]:
    root = _exact(
        value,
        {
            "schema",
            "envelope",
            "selectors",
            "active_writers",
            "open_markers",
            "manifest",
            "identity",
            "services",
        },
        "adoption_shadow_contract_invalid",
    )
    if root["schema"] != INPUT_SCHEMA:
        raise ContractError("adoption_shadow_contract_invalid")
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
    if not isinstance(root["manifest"], dict):
        raise ContractError("manifest_invalid")
    try:
        validate_v3_manifest(root["manifest"])
    except ManifestContractError as exc:
        raise ContractError("manifest_invalid") from exc
    release_source = root["manifest"]["release_source_revision"]
    release_tree = root["manifest"]["release_source_tree"]
    if any(
        service["producer_source_revision"] != release_source
        or service["producer_source_tree"] != release_tree
        for service in root["manifest"]["services"].values()
    ):
        raise ContractError("manifest_release_binding_mismatch")
    envelope = _envelope(root["envelope"])
    if envelope["supply_body_sha256"] != _canonical_sha256(root["manifest"]):
        raise ContractError("supply_body_hash_mismatch")
    identity = _identity(root["identity"])
    if identity is None:
        if envelope["identity_body_sha256"] is not None:
            raise ContractError("deployment_identity_envelope_invalid")
    elif envelope["identity_body_sha256"] != _canonical_sha256(identity):
        raise ContractError("deployment_identity_envelope_invalid")
    services = _exact(root["services"], set(SERVICES), "services_invalid")
    normalized_services = {name: _service(services[name]) for name in SERVICES}
    for name in SERVICES:
        if (
            normalized_services[name]["predicate_body_sha256"]
            != root["manifest"]["services"][name]["provenance_digest"]
        ):
            raise ContractError("predicate_body_hash_mismatch")
    return {
        "manifest": root["manifest"],
        "identity": identity,
        "envelope": envelope,
        "active_writers": _integer(
            root["active_writers"], "active_writers_invalid"
        ),
        "open_markers": _integer(root["open_markers"], "open_markers_invalid"),
        "services": normalized_services,
    }


def _result_base(evidence: Mapping[str, Any]) -> dict[str, Any]:
    manifest = evidence["manifest"]
    return {
        "schema": RESULT_SCHEMA,
        "release_source_revision": manifest["release_source_revision"],
        "checkpoint_sha256": evidence["envelope"]["checkpoint_sha256"],
        "status": "comparison_ready",
        "code": "shadow_adoption_ready",
        "build_dispositions": {},
        "relay_dispositions": {},
        "projected_digests": {},
        "identity_disposition": None,
        "selector_activation_authorized": False,
        "dispatch_authorized": False,
        "identity_restamp_authorized": False,
        "inferred_savings_seconds": None,
    }


def _blocked(evidence: Mapping[str, Any], code: str) -> dict[str, Any]:
    result = _result_base(evidence)
    result.update(status="blocked", code=code)
    return result


def _identity_exact(evidence: Mapping[str, Any]) -> bool:
    identity = evidence["identity"]
    manifest = evidence["manifest"]
    if identity is None or identity["source_revision"] != manifest["release_source_revision"]:
        return False
    return all(
        identity["services"][name]
        == {
            "image_digest": manifest["services"][name]["image_digest"],
            "source_revision": manifest["release_source_revision"],
        }
        for name in SERVICES
    )


def compare_adoption(value: Any) -> dict[str, Any]:
    evidence = validate_evidence(value)
    if evidence["active_writers"]:
        return _blocked(evidence, "active_writer_present")
    if evidence["open_markers"]:
        return _blocked(evidence, "open_marker_present")

    result = _result_base(evidence)
    manifest = evidence["manifest"]
    build_required = False
    for name in SERVICES:
        candidate = manifest["services"][name]
        observed = evidence["services"][name]
        adopt = (
            observed["signed_predicate_verified"]
            and observed["registry_candidate_digest"] == candidate["image_digest"]
        )
        result["build_dispositions"][name] = (
            "shadow_adopt_build" if adopt else "shadow_build"
        )
        build_required = build_required or not adopt
    if build_required:
        result.update(status="blocked", code="build_required")
        return result

    for name in SERVICES:
        candidate = manifest["services"][name]
        observed = evidence["services"][name]
        skip = (
            observed["live_digest"] == candidate["image_digest"]
            and observed["component_source_exact"]
            and observed["runtime_contract_exact"]
            and observed["migration_exact"]
            and observed["route_stable"]
            and observed["health_stable"]
        )
        result["relay_dispositions"][name] = (
            "shadow_skip_relay" if skip else "shadow_deploy"
        )
        result["projected_digests"][name] = candidate["image_digest"]
    result["identity_disposition"] = (
        "shadow_keep_identity"
        if _identity_exact(evidence)
        else "shadow_identity_restamp"
    )
    return result


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
        result = compare_adoption(evidence)
    except (ContractError, OSError) as exc:
        result = {
            "schema": RESULT_SCHEMA,
            "status": "invalid",
            "code": str(exc),
            "selector_activation_authorized": False,
            "dispatch_authorized": False,
            "identity_restamp_authorized": False,
        }
        print(canonical_json(result))
        return 2
    rendered = canonical_json(result) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8", newline="\n")
    sys.stdout.write(rendered)
    return 78 if result["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
