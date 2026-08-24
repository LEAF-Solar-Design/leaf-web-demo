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
# Hard bounds on the AWS call. No unbounded I/O on a request path.
_CONNECT_TIMEOUT_SECONDS = 2.0
_READ_TIMEOUT_SECONDS = 4.0
_MAX_ATTEMPTS = 2


class LiveIdentityUnavailable(RuntimeError):
    """Live state could not be read. The endpoint must fail closed on this."""


_lock = threading.Lock()
_cached: Optional[tuple[float, dict[str, str]]] = None
_client = None


def _cluster(env: Mapping[str, str]) -> str:
    return env.get("LEAF_DEPLOYMENT_CLUSTER", "").strip() or DEFAULT_CLUSTER


def _region(env: Mapping[str, str]) -> str:
    return (
        env.get("AWS_REGION", "").strip()
        or env.get("AWS_DEFAULT_REGION", "").strip()
        or "us-east-1"
    )


def _make_client(env: Mapping[str, str]):  # pragma: no cover - needs the real SDK
    try:
        import boto3
        from botocore.config import Config
    except Exception as exc:  # noqa: BLE001
        raise LiveIdentityUnavailable("boto3 is not installed") from exc
    return boto3.client(
        "ecs",
        region_name=_region(env),
        config=Config(
            connect_timeout=_CONNECT_TIMEOUT_SECONDS,
            read_timeout=_READ_TIMEOUT_SECONDS,
            retries={"max_attempts": _MAX_ATTEMPTS, "mode": "standard"},
        ),
    )


def _get_client(env: Mapping[str, str]):
    global _client
    if _client is None:
        _client = _make_client(env)
    return _client


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


def _container_digest(task_definition: dict[str, Any], service: str) -> str:
    """Pull the service container's image reference and reduce it to a digest.

    Only an ``@sha256:`` reference names an immutable image. A tag reference
    (the live app's harness sidecar uses one) does NOT, so it cannot be
    compared against a receipt digest and is refused rather than guessed at.
    """
    name = _SERVICE_CONTAINER[service]
    containers = task_definition.get("containerDefinitions")
    if not isinstance(containers, list):
        raise LiveIdentityUnavailable(f"{service} task definition has no containers")
    matches = [
        item
        for item in containers
        if isinstance(item, dict) and item.get("name") == name
    ]
    if len(matches) != 1:
        raise LiveIdentityUnavailable(
            f"{service} task definition does not carry exactly one {name} container"
        )
    image = matches[0].get("image")
    if not isinstance(image, str) or "@" not in image:
        raise LiveIdentityUnavailable(
            f"{service} runs a tag reference, which does not name an immutable image"
        )
    digest = image.rsplit("@", 1)[1]
    if not _IMAGE_DIGEST.fullmatch(digest):
        raise LiveIdentityUnavailable(f"{service} image digest is malformed")
    return digest


def _read_live_digests(env: Mapping[str, str]) -> dict[str, str]:
    """Map each service to the image digest it is running right now."""
    client = _get_client(env)
    cluster = _cluster(env)
    families = sorted({f for names in _COLOR_FAMILIES.values() for f in names})
    try:
        response = client.describe_services(cluster=cluster, services=families)
    except LiveIdentityUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise LiveIdentityUnavailable(f"could not describe services: {exc}") from exc

    described = {
        item["serviceName"]: item
        for item in response.get("services", [])
        if isinstance(item, dict) and isinstance(item.get("serviceName"), str)
    }

    digests: dict[str, str] = {}
    # Bounded: one describe_task_definition per service, five services, no
    # per-item network call inside a loop over unbounded input.
    for service in SERVICES:
        family = _routed_family(described, service)
        arn = described[family].get("taskDefinition")
        if not isinstance(arn, str) or not arn:
            raise LiveIdentityUnavailable(f"{family} has no task definition")
        try:
            task_definition = client.describe_task_definition(taskDefinition=arn)[
                "taskDefinition"
            ]
        except LiveIdentityUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LiveIdentityUnavailable(
                f"could not describe {family} task definition: {exc}"
            ) from exc
        digests[service] = _container_digest(task_definition, service)
    return digests


def live_digests(
    env: Mapping[str, str] | None = None, *, reader=None
) -> dict[str, str]:
    """Cached, single-flight read of the live digests.

    The lock is held across the read deliberately: a stampede of concurrent
    callers must produce ONE ECS call, not one per caller.
    """
    global _cached
    current = os.environ if env is None else env
    now = time.monotonic()
    with _lock:
        if _cached is not None and now - _cached[0] < _CACHE_TTL_SECONDS:
            return dict(_cached[1])
        read = reader or _read_live_digests
        digests = read(current)
        if set(digests) != set(SERVICES):
            raise LiveIdentityUnavailable("live digest set is incomplete")
        _cached = (now, dict(digests))
        return dict(digests)


def reset_cache() -> None:
    """Drop the cached read. For tests and for an explicit operator refresh."""
    global _cached, _client
    with _lock:
        _cached = None
        _client = None


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
        "derived_from": "live-ecs",
        "services": services,
    }
    # Only a fully verified fleet gets a single top-level source revision. A
    # partial or contradicted receipt must not present one, because a caller
    # reading only this field is the exact failure this module exists to stop.
    if status == "verified":
        result["source_revision"] = next(iter(revisions))
    return result
