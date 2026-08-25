"""Derive the five-service deployment identity from what is RUNNING right now.

Why this module exists
----------------------
``LEAF_DEPLOYMENT_IDENTITY`` is a SNAPSHOT written into one task definition by
whichever deploy stamped it. Consumers read it as a LIVE assertion about five
services. A snapshot in a per-service task definition can never be both durable
and truthful, because four of the five services it describes can change without
that task definition changing. Every observed symptom follows from that one
root cause:

* a forward or rollback deploy strips it (deploy-leaf-platform-staging.yml) and
  the endpoint 503s, because a ONE-service deploy cannot honestly re-derive a
  FIVE-service receipt;
* a configuration deploy stamps it, and it is truthful only at that instant;
* the single-variable config renderers carry it forward byte-identical, so it
  is never re-checked.

Measured live on 2026-08-24, staging ``leaf-platform-app-alt:139``: the receipt
certified web ``sha256:1d0460ed...`` while ``leaf-platform-web:252`` was running
``sha256:a12a9d7c...``. Broker, canonical-worker and harness still matched. A
consumer spot-checking one service would have read that receipt as healthy.
Three of five entries true is the DANGEROUS shape, not the safe one.

The rule this module enforces
-----------------------------
Live digests, read from ECS at request time, are the ONLY source of truth for
what is running. The stored receipt is consulted for exactly one thing: it maps
an image digest to the source commit that produced it. And it is consulted for
a service ONLY when the receipt's digest for that service EQUALS the live
digest.

That single rule is what makes staleness impossible rather than merely
unlikely. A stale receipt entry cannot make the response wrong, because the
digest comparison rejects it before its ``source_revision`` is ever read. A
stale entry goes INERT; it never goes FALSE. There is no code path in which a
receipt that disagrees with live state contributes a value to the response.

Fails closed. If live state cannot be read, this raises and the endpoint 503s.
It never falls back to the stored receipt, because "the receipt is all we have"
is precisely the situation in which the receipt is least trustworthy.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Mapping, Optional

SCHEMA = "leaf.deployment-identity.v1"
SERVICES = ("app", "broker", "canonical-worker", "harness", "web")
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

# app and web run a blue/green color pair; the other three are single-family.
# The routed color is the one actually serving, which is what "running" means
# for this endpoint. Resolving it from ECS is the reason this cannot be a
# static map.
_COLOR_FAMILIES: dict[str, tuple[str, ...]] = {
    "app": ("leaf-platform-app", "leaf-platform-app-alt"),
    "web": ("leaf-platform-web", "leaf-platform-web-alt"),
    "broker": ("leaf-platform-broker",),
    "canonical-worker": ("leaf-platform-canonical-worker",),
    "harness": ("leaf-platform-harness",),
}

# The container inside each task definition whose image IS the service. A task
# definition may carry init/sidecar containers (the live app carries
# init-drawing-mutations-fence and init-ios-provider-files), and picking the
# wrong one would compare an unrelated digest.
_SERVICE_CONTAINER = {
    "app": "leaf-platform-app",
    "broker": "leaf-platform-broker",
    "canonical-worker": "leaf-platform-canonical-worker",
    "harness": "leaf-platform-harness",
    "web": "leaf-platform-web",
}

DEFAULT_CLUSTER = "leaf-automation-staging"
# Bounded so a burst of callers cannot turn a page refresh into an ECS API
# rate-limit event. Short enough that a deploy is reflected within one cycle.
_CACHE_TTL_SECONDS = 15.0
# The sidecar file (C1-C4 of the sidecar design review): the operator egress
# guard rightly denies this process the ECS control plane, so a sibling
# container in the same task performs the read and publishes it here. The
# reader fails closed on absence, staleness, malformed content, or an
# explicit unavailable state; it never falls back to the stored receipt.
_SIDECAR_FILE_DEFAULT = "/run/leaf-identity/current.json"
_SIDECAR_SCHEMA = "leaf.live-identity-collector.v1"
# 2 * the collector's 20s poll + 5s slack (C4). One constant, derived.
_SIDECAR_MAX_AGE_SECONDS = 45.0
# Bounded read (C9): seven families' descriptions fit well inside this.
_SIDECAR_MAX_BYTES = 1024 * 1024
# Reject future-dated documents beyond small clock skew (C10).
_SIDECAR_MAX_FUTURE_SKEW_SECONDS = 5.0
# Hard bounds on the AWS call. No unbounded I/O on a request path.
_CONNECT_TIMEOUT_SECONDS = 2.0
_READ_TIMEOUT_SECONDS = 4.0
_MAX_ATTEMPTS = 2
# DescribeTasks accepts at most 100 identifiers per call.
_MAX_TASKS_PER_SERVICE = 100


class LiveIdentityUnavailable(RuntimeError):
    """Live state could not be read. The endpoint must fail closed on this."""


_lock = threading.Lock()
_cached: Optional[tuple[float, dict[str, str]]] = None


def _cluster(env: Mapping[str, str]) -> str:
    return env.get("LEAF_DEPLOYMENT_CLUSTER", "").strip() or DEFAULT_CLUSTER


def _region(env: Mapping[str, str]) -> str:
    return (
        env.get("AWS_REGION", "").strip()
        or env.get("AWS_DEFAULT_REGION", "").strip()
        or "us-east-1"
    )


def _routed_family(descriptions: dict[str, dict[str, Any]], service: str) -> str:
    """Return the one family that is actually serving this service.

    Ambiguity is an error, not a coin flip. Two active colors means a flip is
    in progress and no single answer is true; zero active colors means nothing
    is serving. Both fail closed rather than pick one.
    """
    families = _COLOR_FAMILIES[service]
    active = [
        family
        for family in families
        if (descriptions.get(family) or {}).get("desiredCount", 0) >= 1
        and (descriptions.get(family) or {}).get("runningCount", 0) >= 1
    ]
    if len(active) == 1:
        return active[0]
    if not active:
        raise LiveIdentityUnavailable(f"no active {service} service is running")
    raise LiveIdentityUnavailable(
        f"{service} has {len(active)} active colors; the routed image is ambiguous"
    )


def _service_container_digest(task: dict[str, Any], service: str) -> str:
    """Pull the RUNNING container's resolved image digest out of one task.

    ``containers[].imageDigest`` is what the container actually resolved and
    pulled, which is the honest answer to "what is running". A task definition
    only records what was INTENDED, and the two can differ while a roll is in
    flight. Verified live 2026-08-24: leaf-platform-web's running task reported
    sha256:87e6dd97 minutes after its service still described revision 252
    pinning sha256:a12a9d7c, because the service rolled to 253 underneath.
    """
    name = _SERVICE_CONTAINER[service]
    containers = task.get("containers")
    if not isinstance(containers, list):
        raise LiveIdentityUnavailable(f"{service} task reports no containers")
    matches = [
        item
        for item in containers
        if isinstance(item, dict) and item.get("name") == name
    ]
    if len(matches) != 1:
        raise LiveIdentityUnavailable(
            f"{service} task does not carry exactly one {name} container"
        )
    digest = matches[0].get("imageDigest")
    if not isinstance(digest, str) or not _IMAGE_DIGEST.fullmatch(digest):
        # Absent for a non-ECR registry, and unusable if malformed. Either way
        # there is no honest digest to report, so refuse rather than guess.
        raise LiveIdentityUnavailable(
            f"{service} running container reports no usable image digest"
        )
    return digest


def _sidecar_path(env: Mapping[str, str]) -> str:
    return env.get("LEAF_IDENTITY_SIDECAR_FILE", "").strip() or _SIDECAR_FILE_DEFAULT


def _read_sidecar_document(env: Mapping[str, str]) -> tuple[dict[str, Any], float]:
    """Read, bound, validate, and age-check the collector's document.

    Fails closed on every anomaly. The unavailable state carries the
    collector's own reason so the 503 detail names the real cause (C1).
    """
    import datetime

    path = _sidecar_path(env)
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_SIDECAR_MAX_BYTES + 1)
    except FileNotFoundError as exc:
        raise LiveIdentityUnavailable(
            "identity collector file is absent; the sidecar has not published"
        ) from exc
    except OSError as exc:
        raise LiveIdentityUnavailable(
            f"identity collector file unreadable: {type(exc).__name__}"
        ) from exc
    if len(raw) > _SIDECAR_MAX_BYTES:
        raise LiveIdentityUnavailable("identity collector file exceeds its size bound")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise LiveIdentityUnavailable("identity collector file is not valid JSON") from exc
    if not isinstance(document, dict) or document.get("schema") != _SIDECAR_SCHEMA:
        raise LiveIdentityUnavailable("identity collector document schema differs")
    observed_raw = document.get("observed_at")
    if not isinstance(observed_raw, str):
        raise LiveIdentityUnavailable("identity collector document carries no observation time")
    try:
        observed = datetime.datetime.fromisoformat(observed_raw)
    except ValueError as exc:
        raise LiveIdentityUnavailable("identity collector observation time is invalid") from exc
    if observed.tzinfo is None:
        raise LiveIdentityUnavailable("identity collector observation time is not timezone-aware")
    age = (datetime.datetime.now(datetime.timezone.utc) - observed).total_seconds()
    if age < -_SIDECAR_MAX_FUTURE_SKEW_SECONDS:
        raise LiveIdentityUnavailable("identity collector document is future-dated")
    if age > _SIDECAR_MAX_AGE_SECONDS:
        raise LiveIdentityUnavailable(
            f"identity collector document is stale ({age:.0f}s old, bound {_SIDECAR_MAX_AGE_SECONDS:.0f}s)"
        )
    if document.get("state") != "ok":
        reason = str(document.get("reason", ""))[:300]
        raise LiveIdentityUnavailable(
            f"identity collector reports unavailable: {reason or 'no reason recorded'}"
        )
    return document, age


def _read_live_digests(env: Mapping[str, str]) -> dict[str, str]:
    digests, _ = _read_live_digests_with_meta(env)
    return digests


def _read_live_digests_with_meta(
    env: Mapping[str, str],
) -> tuple[dict[str, str], dict[str, Any]]:
    """Map each service to the digest its RUNNING tasks resolved, per the
    sidecar's bounded, age-checked observation. All interpretation (routed
    color, mid-roll refusal, digest extraction) stays in this process; the
    sidecar only collects."""
    document, age = _read_sidecar_document(env)
    services_payload = document.get("describe_services")
    if not isinstance(services_payload, dict):
        raise LiveIdentityUnavailable("identity collector document lacks service descriptions")
    described = {
        item["serviceName"]: item
        for item in services_payload.get("services", [])
        if isinstance(item, dict) and isinstance(item.get("serviceName"), str)
    }
    tasks_by_family = document.get("tasks")
    if not isinstance(tasks_by_family, dict):
        raise LiveIdentityUnavailable("identity collector document lacks task descriptions")

    digests: dict[str, str] = {}
    for service in SERVICES:
        family = _routed_family(described, service)
        family_payload = tasks_by_family.get(family)
        if not isinstance(family_payload, dict):
            raise LiveIdentityUnavailable(f"{family} has no running tasks")
        tasks = family_payload.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise LiveIdentityUnavailable(f"{family} returned no task detail")
        if len(tasks) > _MAX_TASKS_PER_SERVICE:
            raise LiveIdentityUnavailable(
                f"{family} runs {len(tasks)} tasks, more than this endpoint reads"
            )
        observed = {_service_container_digest(task, service) for task in tasks}
        if len(observed) != 1:
            raise LiveIdentityUnavailable(
                f"{service} is running {len(observed)} different image digests; "
                "the fleet is mid-roll and has no single running identity"
            )
        digests[service] = observed.pop()
    meta = {"observed_at": document["observed_at"], "age_seconds": round(age, 1)}
    return digests, meta


def live_digests(
    env: Mapping[str, str] | None = None, *, reader=None
) -> dict[str, str]:
    """Cached, single-flight read of the live digests.

    The lock is held across the read deliberately: a stampede of concurrent
    callers must produce ONE ECS call, not one per caller.
    """
    global _cached
    current = os.environ if env is None else env
    if reader is None:
        # The sidecar file path is uncached (C3): a local bounded read has no
        # rate limit to protect, and stacking the 15s cache on the sidecar's
        # own poll interval would widen the true staleness bound to 75s.
        digests = _read_live_digests(current)
        if set(digests) != set(SERVICES):
            raise LiveIdentityUnavailable("live digest set is incomplete")
        return digests
    now = time.monotonic()
    with _lock:
        if _cached is not None and now - _cached[0] < _CACHE_TTL_SECONDS:
            return dict(_cached[1])
        digests = reader(current)
        if set(digests) != set(SERVICES):
            raise LiveIdentityUnavailable("live digest set is incomplete")
        _cached = (now, dict(digests))
        return dict(digests)


def reset_cache() -> None:
    """Drop the cached read. For tests and for an explicit operator refresh."""
    global _cached
    with _lock:
        _cached = None


def _receipt(env: Mapping[str, str]) -> Optional[dict[str, Any]]:
    """Parse the stored receipt, or None if it is absent or unusable.

    An unparseable receipt is treated exactly like an absent one. It is only
    ever a source of provenance, never of truth, so there is nothing to fail
    on: the response is already fully determined by the live digests.
    """
    raw = env.get("LEAF_DEPLOYMENT_IDENTITY", "")
    if not raw:
        return None
    try:
        document = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        return None
    services = document.get("services")
    if not isinstance(services, dict):
        return None
    return document


def _expected_environment(env: Mapping[str, str]) -> str:
    runtime = env.get("LEAF_RUNTIME_ENV", "").strip().lower()
    configured = env.get("LEAF_DEPLOYMENT_ENVIRONMENT", "").strip().lower()
    if runtime == "production":
        if configured != "production":
            raise LiveIdentityUnavailable(
                "production deployment environment is not explicit"
            )
        return "production"
    if configured not in {"", "staging"}:
        raise LiveIdentityUnavailable("non-production deployment environment is invalid")
    return "staging"


def live_deployment_identity(
    env: Mapping[str, str] | None = None, *, reader=None
) -> dict[str, Any]:
    """Return the identity of what is RUNNING, with honest attestation status.

    ``status`` is the field a convergence gate must read:

    * ``verified``   -- every service's live digest is attested by the receipt
                        AND all five attest to one source revision.
    * ``mismatch``   -- a receipt is present and disagrees with live state for
                        at least one service. NEVER reported as success.
    * ``unattested`` -- no usable receipt, or it covers only some services. The
                        digests are still true; the commit mapping is unknown.

    A caller that requires convergence must require ``status == "verified"``.
    ``mismatch`` and ``unattested`` are both non-answers, and they are
    distinguishable so an operator can tell "the stamp is wrong" from "there is
    no stamp".
    """
    current = os.environ if env is None else env
    environment = _expected_environment(current)
    meta: Optional[dict[str, Any]] = None
    if reader is None:
        digests, meta = _read_live_digests_with_meta(current)
        if set(digests) != set(SERVICES):
            raise LiveIdentityUnavailable("live digest set is incomplete")
    else:
        digests = live_digests(current, reader=reader)
    document = _receipt(current)

    receipt_services: Mapping[str, Any] = {}
    receipt_environment_ok = True
    if document is not None:
        raw_services = document.get("services")
        if isinstance(raw_services, dict):
            receipt_services = raw_services
        # A receipt stamped for another environment attests nothing here.
        if document.get("environment") != environment:
            receipt_environment_ok = False

    services: dict[str, Any] = {}
    revisions: set[str] = set()
    attested_count = 0
    mismatched = False

    for name in SERVICES:
        live_digest = digests[name]
        entry: dict[str, Any] = {"image_digest": live_digest, "attested": False}
        claimed = receipt_services.get(name) if receipt_environment_ok else None
        if isinstance(claimed, dict):
            claimed_digest = claimed.get("image_digest")
            revision = claimed.get("source_revision")
            if claimed_digest == live_digest:
                # The ONLY branch that reads a value out of the receipt, and it
                # is guarded by digest equality with live state.
                if isinstance(revision, str) and _SOURCE_SHA.fullmatch(revision):
                    entry["source_revision"] = revision
                    entry["attested"] = True
                    revisions.add(revision)
                    attested_count += 1
            elif isinstance(claimed_digest, str):
                # The receipt makes a claim about this service that live state
                # refutes. Record it so the response says so out loud.
                entry["receipt_claims_digest"] = claimed_digest
                mismatched = True
        services[name] = entry

    if mismatched:
        status = "mismatch"
    elif attested_count == len(SERVICES) and len(revisions) == 1:
        status = "verified"
    else:
        status = "unattested"

    result: dict[str, Any] = {
        "schema": SCHEMA,
        "environment": environment,
        "status": status,
        # "live-ecs-sidecar" is the honest name for the real mechanism (C2);
        # injected readers keep the historical label so their consumers can
        # tell recorded fixtures from the deployed read path.
        "derived_from": "live-ecs" if meta is None else "live-ecs-sidecar",
        "services": services,
    }
    if meta is not None:
        # Rollback-shaped convergence gates need the observation time: a
        # stale-but-matching revision must be distinguishable from a fresh
        # one (design review R2).
        result["observed_at"] = meta["observed_at"]
        result["age_seconds"] = meta["age_seconds"]
    # Only a fully verified fleet gets a single top-level source revision. A
    # partial or contradicted receipt must not present one, because a caller
    # reading only this field is the exact failure this module exists to stop.
    if status == "verified":
        result["source_revision"] = next(iter(revisions))
    return result
