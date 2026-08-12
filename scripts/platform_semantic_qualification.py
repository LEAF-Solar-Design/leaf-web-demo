"""Fixture-only semantic qualification and durable local stage receipts.

S1 deliberately has no live topology adapter.  Its command-line preflight always
fails ``UNCONFIGURED``.  Tests may opt into the deterministic fixture adapter to
exercise the frozen contracts without granting deployment authority.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import time
from typing import Any

from platform_semantic_eligibility import (
    ASSERTION_IDS,
    ContractError,
    SignatureBuilder,
    SignatureVerifier,
    attach_integrity,
    canonical_json_bytes,
    sha256_digest,
    validate_eligibility_receipt,
    validate_manifest,
)


STAGE_SCHEMA = "platform-stage-receipt.v1"
KNOWN_SURFACES = (
    "candidate_tasks",
    "catalog_pointers",
    "leases",
    "markers",
    "temporary_identities",
    "uploaded_artifacts",
)
FIXTURE_KEYS = {
    "deployment_identity_count",
    "deployment_identity_value",
    "app_to_harness_classification",
    "generic_removal_claimed",
    "ordinary_authoring_claimed",
    "lease_recovered",
    "publication_terminal_states",
    "closed_projection_operator_authority",
    "closed_projection_tenant_markers",
    "tenants",
    "cleanup_pre",
    "cleanup_post",
    "rollback",
}


@dataclass(frozen=True)
class QualificationLease:
    source_commit: str
    manifest_digest: str
    owner: str

    def as_dict(self) -> dict[str, str]:
        return {
            "source_commit": self.source_commit,
            "manifest_digest": self.manifest_digest,
            "owner": self.owner,
        }


@contextmanager
def _exclusive_lock(path: Path, timeout_seconds: float = 5.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    deadline = time.monotonic() + timeout_seconds
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(descriptor, str(os.getpid()).encode("ascii"))
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise ContractError("STAGE_LOCK_TIMEOUT")
            time.sleep(0.01)
    try:
        yield
    finally:
        os.close(descriptor)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json_bytes(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class StageReceiptJournal:
    """A hash-chained local receipt journal keyed by one immutable lease."""

    def __init__(self, path: Path, lease: QualificationLease) -> None:
        self.path = path
        self.lease = lease

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError("STAGE_RECEIPT_CORRUPT") from exc
        if not isinstance(value, dict) or set(value) != {"schema", "receipts"} or value["schema"] != "platform-stage-journal.v1":
            raise ContractError("STAGE_RECEIPT_CORRUPT")
        receipts = value["receipts"]
        if not isinstance(receipts, list):
            raise ContractError("STAGE_RECEIPT_CORRUPT")
        prior = None
        seen: set[str] = set()
        for receipt in receipts:
            self._validate_receipt(receipt, prior)
            if receipt["stage_id"] in seen:
                raise ContractError("STAGE_RECEIPT_DUPLICATE")
            seen.add(receipt["stage_id"])
            prior = receipt["payload_digest"]
        return receipts

    def _validate_receipt(self, receipt: Any, prior: str | None) -> None:
        keys = {
            "schema",
            "qualification_lease",
            "stage_id",
            "stage_version",
            "completed_mutations",
            "written_after",
            "prior_stage_receipt_digest",
            "timestamp",
            "payload_digest",
        }
        if not isinstance(receipt, dict) or set(receipt) != keys:
            raise ContractError("STAGE_RECEIPT_INVALID")
        if receipt["schema"] != STAGE_SCHEMA or receipt["qualification_lease"] != self.lease.as_dict():
            raise ContractError("STAGE_LEASE_MISMATCH")
        if receipt["prior_stage_receipt_digest"] != prior:
            raise ContractError("STAGE_CHAIN_INVALID")
        if receipt["payload_digest"] != sha256_digest({key: value for key, value in receipt.items() if key != "payload_digest"}):
            raise ContractError("STAGE_DIGEST_INVALID")
        if not isinstance(receipt["completed_mutations"], list) or len(receipt["completed_mutations"]) != len(set(receipt["completed_mutations"])):
            raise ContractError("STAGE_RECEIPT_INVALID")

    def run_stage(
        self,
        stage_id: str,
        mutation: Callable[[], tuple[list[str], str]],
        *,
        stage_version: str = "v1",
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if not stage_id or not stage_version:
            raise ContractError("STAGE_ID_INVALID")
        with _exclusive_lock(self.path):
            receipts = self._load()
            for receipt in receipts:
                if receipt["stage_id"] == stage_id:
                    if receipt["stage_version"] != stage_version:
                        raise ContractError("STAGE_VERSION_MISMATCH")
                    return deepcopy(receipt), True
            completed_mutations, written_after = mutation()
            if not isinstance(completed_mutations, list) or not all(isinstance(item, str) and item for item in completed_mutations):
                raise ContractError("MUTATION_KEYS_INVALID")
            if len(completed_mutations) != len(set(completed_mutations)) or not written_after:
                raise ContractError("MUTATION_KEYS_INVALID")
            receipt: dict[str, Any] = {
                "schema": STAGE_SCHEMA,
                "qualification_lease": self.lease.as_dict(),
                "stage_id": stage_id,
                "stage_version": stage_version,
                "completed_mutations": completed_mutations,
                "written_after": written_after,
                "prior_stage_receipt_digest": receipts[-1]["payload_digest"] if receipts else None,
                "timestamp": (now or datetime.now(UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z"),
            }
            receipt["payload_digest"] = sha256_digest(receipt)
            self._validate_receipt(receipt, receipt["prior_stage_receipt_digest"])
            receipts.append(receipt)
            _atomic_json(self.path, {"schema": "platform-stage-journal.v1", "receipts": receipts})
            return deepcopy(receipt), False


def _exact_mapping(value: Any, keys: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ContractError(code)
    return dict(value)


def _surface_snapshot(value: Any) -> dict[str, int]:
    expected = set(KNOWN_SURFACES)
    mapping = _exact_mapping(value, expected, "CLEANUP_CENSUS_INVALID")
    if any(not isinstance(count, int) or isinstance(count, bool) or count < 0 for count in mapping.values()):
        raise ContractError("CLEANUP_CENSUS_INVALID")
    return {key: mapping[key] for key in KNOWN_SURFACES}


def evaluate_fixture(fixture: Mapping[str, Any], manifest: Mapping[str, Any]) -> tuple[list[dict[str, Any]], str]:
    value = _exact_mapping(fixture, FIXTURE_KEYS, "FIXTURE_INVALID")
    if value["deployment_identity_count"] != 5 or value["deployment_identity_value"] != manifest["deployment_identity"]["value"]:
        raise ContractError("DEPLOYMENT_IDENTITY_ASSERTION_FAILED")
    if value["app_to_harness_classification"] != "reached":
        raise ContractError("HARNESS_AUTH_CLOSED")
    if value["generic_removal_claimed"] is not False:
        raise ContractError("REMOVAL_WORKER_FENCE_FAILED")
    if value["ordinary_authoring_claimed"] is not True or value["lease_recovered"] is not True:
        raise ContractError("AUTHORING_RECOVERY_FAILED")
    states = value["publication_terminal_states"]
    if not isinstance(states, list) or set(states) != {"auto_published", "explicitly_approved"} or len(states) != 2:
        raise ContractError("PUBLICATION_STATE_INVALID")
    if value["closed_projection_operator_authority"] is not True or value["closed_projection_tenant_markers"] != []:
        raise ContractError("CLOSED_PROJECTION_INVALID")
    tenants = _exact_mapping(value["tenants"], {"tenant-a", "tenant-b"}, "TENANT_FIXTURE_INVALID")
    markers: dict[str, str] = {}
    for tenant, raw in tenants.items():
        entry = _exact_mapping(raw, {"marker", "observed_markers", "upload_status"}, "TENANT_FIXTURE_INVALID")
        marker = entry["marker"]
        if not isinstance(marker, str) or len(marker) < 8 or entry["upload_status"] != "ready" or entry["observed_markers"] != [marker]:
            raise ContractError("TENANT_ISOLATION_FAILED")
        markers[tenant] = marker
    if markers["tenant-a"] == markers["tenant-b"]:
        raise ContractError("TENANT_ISOLATION_FAILED")
    pre = _surface_snapshot(value["cleanup_pre"])
    post = _surface_snapshot(value["cleanup_post"])
    if pre != post:
        raise ContractError("CLEANUP_RESIDUE")
    rollback = _exact_mapping(value["rollback"], {"restored", "images_rebuilt", "service_definitions_mutated"}, "ROLLBACK_INVALID")
    if rollback != {"restored": True, "images_rebuilt": False, "service_definitions_mutated": False}:
        raise ContractError("ROLLBACK_INVALID")

    evidence = {
        "deployment_identity": {"count": 5, "value": value["deployment_identity_value"]},
        "app_to_harness_auth": {"classification": "reached"},
        "removal_worker_fence": {"generic_removal_claimed": False},
        "ordinary_authoring_recovery": {"claimed": True, "lease_recovered": True},
        "publication_terminal_state": {"states": sorted(states)},
        "closed_projection": {"operator_authority": True, "tenant_markers": []},
        "tenant_upload_isolation": {"tenants": tenants},
        "rollback_cleanup": {"rollback": rollback, "pre": pre, "post": post},
    }
    assertions = [
        {"id": assertion_id, "result": True, "evidence_digest": sha256_digest(evidence[assertion_id])}
        for assertion_id in ASSERTION_IDS
    ]
    cleanup_digest = sha256_digest({"surfaces": pre, "delta": {key: 0 for key in KNOWN_SURFACES}, "allowlist_version": "v1"})
    return assertions, cleanup_digest


def run_fixture_qualification(
    *,
    manifest: Mapping[str, Any],
    fixture: Mapping[str, Any],
    output_dir: Path,
    expected_producer: str,
    verifier_version: str,
    topology_version: str,
    signature_verifier: SignatureVerifier | None,
    receipt_signer: SignatureBuilder | None,
    allow_fixture_receipt: bool = False,
    now: datetime | None = None,
    stop_after_stage: str | None = None,
) -> dict[str, Any]:
    if not allow_fixture_receipt or signature_verifier is None or receipt_signer is None:
        raise ContractError("UNCONFIGURED")
    validated_manifest = validate_manifest(
        manifest,
        expected_producer=expected_producer,
        expected_verifier_version=verifier_version,
        expected_topology_version=topology_version,
        signature_verifier=signature_verifier,
    )
    lease = QualificationLease(
        source_commit=validated_manifest["source_commit"],
        manifest_digest=validated_manifest["payload_digest"],
        owner="fixture-only",
    )
    journal = StageReceiptJournal(output_dir / "stage-receipts.json", lease)
    assertions: list[dict[str, Any]] = []
    cleanup_digest = ""

    journal.run_stage("manifest", lambda: (["manifest-validated"], "manifest_signature_and_identity_verified"), now=now)
    if stop_after_stage == "manifest":
        raise ContractError("FIXTURE_STOP")

    def semantic_stage() -> tuple[list[str], str]:
        nonlocal assertions, cleanup_digest
        assertions, cleanup_digest = evaluate_fixture(fixture, validated_manifest)
        cache = {"assertions": assertions, "cleanup_census_digest": cleanup_digest}
        _atomic_json(output_dir / "semantic-stage.json", cache)
        return [sha256_digest(cache)], "all_eight_assertions_and_cleanup_verified"

    semantic_receipt, recovered = journal.run_stage("semantic", semantic_stage, now=now)
    if recovered:
        try:
            cache = json.loads((output_dir / "semantic-stage.json").read_text(encoding="utf-8"))
            assertions = cache["assertions"]
            cleanup_digest = cache["cleanup_census_digest"]
        except (OSError, KeyError, json.JSONDecodeError, TypeError) as exc:
            raise ContractError("STAGE_CACHE_MISSING") from exc
        if semantic_receipt["completed_mutations"] != [sha256_digest(cache)]:
            raise ContractError("STAGE_CACHE_INVALID")
    if stop_after_stage == "semantic":
        raise ContractError("FIXTURE_STOP")

    receipt_path = output_dir / "semantic-eligibility.json"
    if receipt_path.exists():
        try:
            existing = json.loads(receipt_path.read_text(encoding="utf-8"))
            existing_issued_at = datetime.fromisoformat(existing["issued_at"].replace("Z", "+00:00"))
        except (OSError, KeyError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ContractError("RECEIPT_CONFLICT") from exc
        existing = validate_eligibility_receipt(
            existing,
            manifest_digest=validated_manifest["payload_digest"],
            expected_producer=expected_producer,
            expected_verifier_version=verifier_version,
            expected_topology_version=topology_version,
            signature_verifier=signature_verifier,
            now=existing_issued_at,
        )
        journal.run_stage("receipt", lambda: ([existing["payload_digest"]], "receipt_validated_and_persisted"), now=now)
        return existing

    observed = (now or datetime.now(UTC)).astimezone(UTC)
    unsigned = {
        "schema": "leaf.platform-semantic-eligibility.v1",
        "manifest_digest": validated_manifest["payload_digest"],
        "producer_identity": expected_producer,
        "assertions": assertions,
        "verifier_version": verifier_version,
        "topology_version": topology_version,
        "rollback_result": deepcopy(dict(fixture["rollback"])),
        "cleanup_census_digest": cleanup_digest,
        "issued_at": observed.isoformat().replace("+00:00", "Z"),
        "expires_at": (observed + timedelta(minutes=15)).isoformat().replace("+00:00", "Z"),
    }
    receipt = attach_integrity(unsigned, receipt_signer)
    receipt = validate_eligibility_receipt(
        receipt,
        manifest_digest=validated_manifest["payload_digest"],
        expected_producer=expected_producer,
        expected_verifier_version=verifier_version,
        expected_topology_version=topology_version,
        signature_verifier=signature_verifier,
        now=observed,
    )
    _atomic_json(receipt_path, receipt)
    journal.run_stage("receipt", lambda: ([receipt["payload_digest"]], "receipt_validated_and_persisted"), now=now)
    return receipt


def workflow_preflight(*, shadow_enabled: bool) -> dict[str, Any]:
    """The S1 workflow never has signing or terminal-P6 authority."""

    return {
        "schema": "leaf.platform-semantic-qualification-preflight.v1",
        "state": "UNCONFIGURED",
        "shadow_enabled": bool(shadow_enabled),
        "producer_signing_configured": False,
        "terminal_p6_verifier_configured": False,
        "deployment_effect": False,
        "receipt_published": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("workflow-preflight")
    preflight.add_argument("--shadow-enabled", choices=("true", "false"), required=True)
    args = parser.parse_args()
    if args.command == "workflow-preflight":
        result = workflow_preflight(shadow_enabled=args.shadow_enabled == "true")
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 78
    return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ContractError",
    "KNOWN_SURFACES",
    "QualificationLease",
    "StageReceiptJournal",
    "evaluate_fixture",
    "run_fixture_qualification",
    "workflow_preflight",
]
