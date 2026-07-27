"""Validation for deployment-controller identity evidence."""
from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping

_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SERVICES = {"app", "broker", "canonical-worker", "harness", "web"}


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
    if identity.get("environment") != "staging":
        raise ValueError("deployment identity is not staging")
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
        "environment": "staging",
        "source_revision": revision,
        "services": sanitized,
    }
