"""Pure content validation for the existing Mushy host-enrollment lifecycle.

This checks supplied content only. It grants no authority and produces no host
evidence; the owning lifecycle supplies the persisted readback.
"""

import hashlib
import json
import re
import uuid


CONSTANTS = {
    "schema": "leaf.campaign-capability.v1",
    "capability": "campaign.host-enrollment",
    "tool_name": "campaign-host-enrollment",
    "profile_selector": "campaign-default-v1",
}
IDS = {"org_id", "project_id", "campaign_id", "enrollment_id", "link_id"}
KEYS = set(CONSTANTS) | IDS | {
    "tenant_id", "change_set_id", "catalog_commit", "effective_catalog_digest",
    "tool_manifest_sha256", "tool_source_sha256",
}


def _uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("invalid capability UUID")
    return value


def _hex(value, length=64, prefix=""):
    return isinstance(value, str) and re.fullmatch(
        prefix + "[0-9a-f]{%d}" % length, value
    ) is not None


def _context(value):
    if not isinstance(value, dict) or set(value) != KEYS:
        raise ValueError("invalid capability context")
    if any(value[key] != expected for key, expected in CONSTANTS.items()):
        raise ValueError("invalid capability identity")
    for key in IDS:
        _uuid(value[key])
    for key, maximum in (("tenant_id", 32768), ("change_set_id", 200)):
        text = value[key]
        if (not isinstance(text, str) or not 1 <= len(text) <= maximum
                or any(ord(c) < 32 or ord(c) == 127 for c in text)):
            raise ValueError("invalid capability token")
    if (not _hex(value["catalog_commit"], 40)
            or not _hex(value["effective_catalog_digest"])
            or not _hex(value["tool_manifest_sha256"], prefix="sha256:")
            or not _hex(value["tool_source_sha256"])):
        raise ValueError("invalid capability digest")
    return dict(value)


def _readback(value):
    if not isinstance(value, dict) or set(value) != {
        "config_identity_before", "config_identity_after", "readback_sha256", "reason",
    }:
        raise ValueError("invalid host readback")
    if ((value["config_identity_before"] is not None
         and not _hex(value["config_identity_before"]))
            or not _hex(value["config_identity_after"])
            or not _hex(value["readback_sha256"])
            or not isinstance(value["reason"], str)
            or value["reason"] not in {"verified", "already_applied"}):
        raise ValueError("invalid successful host readback")
    return dict(value)


def run(intake, params):
    """Validate the closed intake without changing it or performing host actions."""
    if not isinstance(params, dict) or params:
        raise ValueError("invalid validator params")
    if not isinstance(intake, dict) or set(intake) != {
        "schema", "job_id", "operation_id", "input_sha256",
        "capability_provenance", "host_readback",
    }:
        raise ValueError("invalid host validation intake")
    if intake["schema"] != "leaf.campaign-host-validation.v1":
        raise ValueError("invalid host validation schema")
    job_id = _uuid(intake["job_id"])
    operation_id = _uuid(intake["operation_id"])
    context = _context(intake["capability_provenance"])
    if not _hex(intake["input_sha256"]):
        raise ValueError("invalid input digest")
    body = {
        "schema": "leaf.campaign-host-operation.v1",
        "job_id": job_id,
        "context": context,
    }
    input_digest = hashlib.sha256(json.dumps(
        body, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()
    if input_digest != intake["input_sha256"]:
        raise ValueError("host input digest mismatch")
    readback = _readback(intake["host_readback"])
    return {
        "verified": True,
        "operation_id": operation_id,
        "input_sha256": input_digest,
        "readback_sha256": readback["readback_sha256"],
    }
