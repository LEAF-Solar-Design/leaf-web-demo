"""Validation for deployment-controller identity evidence."""
from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping

_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SERVICES = {"app", "broker", "canonical-worker", "harness", "web"}

RELEASE_IDENTITY_MAX_CHARS = 16384
SOURCE_IDENTITY_STATES = ("built", "adopted", "build_unknown", "unattested")


def release_source_identity(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Reconcile the baked build commit with the receipt-attested release commit.

    No I/O, never raises, O(len(receipt)) with the receipt capped at
    RELEASE_IDENTITY_MAX_CHARS. Returns {"release_source_sha", "source_identity"}.
    Informational only: not digest-bound (see /api/deployment-identity).
    """
    current = os.environ if env is None else env
    baked = current.get("LEAF_SOURCE_SHA", "")
    raw = current.get("LEAF_DEPLOYMENT_IDENTITY", "")
    release = None
    if isinstance(raw, str) and raw and len(raw) <= RELEASE_IDENTITY_MAX_CHARS:
        try:
            release = deployment_identity(current)["source_revision"]
        except (ValueError, TypeError, RecursionError):
            release = None
    if release is None:
        status = "unattested"
    elif not isinstance(baked, str) or not _SOURCE_SHA.fullmatch(baked):
        status = "build_unknown"
    else:
        status = "built" if baked == release else "adopted"
    return {"release_source_sha": release, "source_identity": status}


def deployment_identity(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Read only the deployment controller's immutable runtime receipt.

    This is deliberately not an acceptance-run input. The deployment controller
    writes it into the running app's environment with the five resolved image
    digests after it has selected the task definitions.
    """
    current = os.environ if env is None else env
    raw = current.get("LEAF_DEPLOYMENT_IDENTITY", "")
    try:
        identity = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("deployment identity is unavailable") from exc
    if not isinstance(identity, dict):
        raise ValueError("deployment identity is invalid")
    if identity.get("schema") != "leaf.deployment-identity.v1":
        raise ValueError("deployment identity has an unsupported schema")
    runtime_environment = current.get("LEAF_RUNTIME_ENV", "").strip().lower()
    configured_environment = current.get(
        "LEAF_DEPLOYMENT_ENVIRONMENT", ""
    ).strip().lower()
    if runtime_environment == "production":
        if configured_environment != "production":
            raise ValueError("production deployment environment is not explicit")
        expected_environment = "production"
    else:
        if configured_environment not in {"", "staging"}:
            raise ValueError("non-production deployment environment is invalid")
        expected_environment = "staging"
    if identity.get("environment") != expected_environment:
        raise ValueError("deployment identity environment does not match runtime")
    revision = identity.get("source_revision")
    if not isinstance(revision, str) or not _SOURCE_SHA.fullmatch(revision):
        raise ValueError("deployment identity lacks a full source SHA")
    services = identity.get("services")
    if not isinstance(services, dict) or set(services) != _SERVICES:
        raise ValueError("deployment identity has an incomplete service set")
    sanitized = {}
    for name in sorted(_SERVICES):
        service = services[name]
        if not isinstance(service, dict):
            raise ValueError("deployment identity service is invalid")
        digest = service.get("image_digest")
        if not isinstance(digest, str) or not _IMAGE_DIGEST.fullmatch(digest):
            raise ValueError("deployment identity service digest is invalid")
        if service.get("source_revision") != revision:
            raise ValueError("deployment identity service revision is mixed")
        sanitized[name] = {"image_digest": digest, "source_revision": revision}
    return {
        "schema": "leaf.deployment-identity.v1",
        "environment": expected_environment,
        "source_revision": revision,
        "services": sanitized,
    }
