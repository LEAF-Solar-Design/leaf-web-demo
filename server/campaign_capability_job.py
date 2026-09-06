"""ReciPDF host validation under the existing async job's attempt and lease."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid

from envelopes import ErrorCode, err_envelope, ok_envelope

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
STAGES = ["apply", "activate", "readback"]
POLL_S = 0.25


def _uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("invalid capability UUID")
    return value


def _hex(value, length=64, prefix=""):
    return isinstance(value, str) and re.fullmatch(prefix + "[0-9a-f]{%d}" % length, value) is not None


def validate_context(value):
    """Copy the closed wire context; presence with malformed data never means ordinary."""
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


def execution_context(execution):
    if not isinstance(execution, dict):
        raise ValueError("invalid durable execution context")
    if "capability_provenance" not in execution:
        return None
    return validate_context(execution["capability_provenance"])


def record_context(execution, record):
    context = execution_context(execution)
    if context is not None and any(context[key] != record.get(column) for key, column in (
        ("tenant_id", "tenant_id"), ("org_id", "org_id"),
        ("project_id", "project_id"), ("tool_name", "tool"),
    )):
        raise ValueError("durable capability job scope mismatch")
    return context


def input_sha256(job_id, context):
    body = {"schema": "leaf.campaign-host-operation.v1",
            "job_id": _uuid(job_id), "context": validate_context(context)}
    return hashlib.sha256(json.dumps(
        body, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def validate_readback(value):
    if not isinstance(value, dict) or set(value) != {
        "config_identity_before", "config_identity_after", "readback_sha256", "reason",
    }:
        raise ValueError("invalid host readback")
    if ((value["config_identity_before"] is not None
         and not _hex(value["config_identity_before"]))
            or not _hex(value["config_identity_after"])
            or not _hex(value["readback_sha256"])
            or value["reason"] not in {"verified", "already_applied"}):
        raise ValueError("invalid successful host readback")
    return dict(value)


def validate_operation(job_id, context, operation):
    if not isinstance(operation, dict):
        raise ValueError("missing host operation")
    _uuid(operation.get("operation_id"))
    expected = {"job_id": _uuid(job_id), "input_sha256": input_sha256(job_id, context),
                **{key: context[key] for key in ("enrollment_id", "link_id", "profile_selector")}}
    if any(operation.get(key) != value for key, value in expected.items()):
        raise ValueError("host operation scope mismatch")
    if operation.get("outcome") != "succeeded":
        return None
    if operation.get("stage") != "readback" or operation.get("completed_stages") != STAGES:
        raise ValueError("host operation stages incomplete")
    evidence = operation.get("stage_evidence")
    if not isinstance(evidence, dict) or set(evidence) != set(STAGES):
        raise ValueError("host operation evidence incomplete")
    for stage in STAGES:
        entry = evidence[stage]
        if not isinstance(entry, dict) or entry.get("outcome") != "succeeded":
            raise ValueError("host operation stage failed")
        validate_readback(entry.get("evidence"))
    return validate_readback(evidence["readback"]["evidence"])


def stored_readback(job_id, context):
    """Only the actual store read supplies receipt evidence. No operation creation."""
    from leaf_platform import campaign_capabilities
    operation = campaign_capabilities.read_operation(job_id, context)
    return validate_operation(job_id, context, operation)


def validate_result(value, operation_id, input_digest, readback):
    expected = {"verified": True, "operation_id": operation_id,
                "input_sha256": input_digest, "readback_sha256": readback["readback_sha256"]}
    if (not isinstance(value, dict) or set(value) != set(expected)
            or value.get("verified") is not True or value != expected):
        raise ValueError("published validator result mismatch")
    return dict(expected)


def _published_tool(context, supplied):
    import customization_service
    import deps

    pin = customization_service.effective_catalog_pin(context["tenant_id"])
    if pin != {key: context[key] for key in ("catalog_commit", "effective_catalog_digest")}:
        raise ValueError("published catalog changed")
    winners = [(tool, source) for tool, source in
               deps.effective_tools_with_provenance(context["tenant_id"])
               if tool.get("name") == context["tool_name"]]
    if len(winners) != 1 or winners[0][1] != deps.TOOL_SOURCE_TENANT_REPO:
        raise ValueError("published tool source changed")
    tool = winners[0][0]
    if (supplied.get("name") != context["tool_name"]
            or deps.catalog_tool_digest(tool) != context["tool_manifest_sha256"]
            or deps.catalog_tool_digest(supplied) != context["tool_manifest_sha256"]):
        raise ValueError("published tool manifest changed")
    return tool


def _capture(context, tool):
    import tool_loader

    local = tool_loader.resolve_local_file(tool, context["tenant_id"])
    contained = (tool_loader._contained_published_path(local, context["tenant_id"])
                 if local is not None else None)
    if contained is None:
        raise ValueError("published source is unavailable")
    limit = tool_loader._SANDBOX_LIMITS["source_bytes"]
    with open(contained, "rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("published source exceeds bound")
    source = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    if hashlib.sha256(source.encode("utf-8")).hexdigest() != context["tool_source_sha256"]:
        raise ValueError("published source changed")
    return source


class _Stopped(Exception):
    pass


class _Deadline(Exception):
    pass


def run(job_id, capability_provenance, tool, params, heartbeat, cancelled, deadline):
    """Wait synchronously in the owning attempt, then validate captured source."""
    import tool_loader
    from leaf_platform import campaign_capabilities

    started = time.monotonic()

    def guard(budget=0):
        if cancelled() or not heartbeat() or cancelled():
            raise _Stopped()
        if not math.isfinite(deadline) or time.monotonic() + budget >= deadline:
            raise _Deadline()

    def failure(message, retryable=False, code=ErrorCode.INTERNAL):
        return err_envelope(code, message, retryable, tool=tool.get("name"),
                            version=tool.get("version"),
                            timing_ms=int((time.monotonic() - started) * 1000))

    try:
        context = validate_context(capability_provenance)
        guard()
        published = _published_tool(context, tool)
        if not isinstance(params, dict) or tool_loader.validate_params(published, params):
            return failure("invalid capability validator params", code=ErrorCode.BAD_PARAMS)
        source = _capture(context, published)
        guard()
        campaign_capabilities.ensure_operation(job_id, context)
        while True:
            guard()
            operation = campaign_capabilities.read_operation(job_id, context)
            readback = validate_operation(job_id, context, operation)
            if readback is not None:
                break
            if operation.get("outcome") in {"held", "failed"}:
                return failure("host operation " + operation["outcome"])
            if operation.get("outcome") is not None:
                return failure("invalid host operation outcome")
            time.sleep(min(POLL_S, max(0, deadline - time.monotonic())))
        guard()
        _published_tool(context, tool)
        # Recheck drift but execute the original capture, never a second file read.
        # The immutable manifest/source pin above remains the authority for this capture.
        timeout = tool_loader._sandbox_timeout_s()
        if not math.isfinite(timeout) or timeout <= 0:
            return failure("invalid validator timeout")
        import jobs
        if timeout + 1 >= min(jobs.lease_duration_s(), jobs.heartbeat_stale_s()):
            return failure("validator exceeds owning lease budget")
        guard(timeout + 1)
        intake = {"schema": "leaf.campaign-host-validation.v1", "job_id": job_id,
                  "operation_id": operation["operation_id"],
                  "input_sha256": operation["input_sha256"],
                  "capability_provenance": context, "host_readback": readback}
        kind, value = tool_loader._run_source_in_sandbox(
            source, "campaign-host-enrollment.py", intake, params)
        guard()
        if kind != "ok":
            return failure("published validator infrastructure failed" if kind == "infra_error"
                           else "published validator failed", retryable=kind == "infra_error")
        result = validate_result(value, operation["operation_id"], operation["input_sha256"], readback)
        return ok_envelope(tool["name"], tool.get("version", ""), result, None,
                           int((time.monotonic() - started) * 1000))
    except _Stopped:
        return failure("capability attempt no longer owns job")
    except _Deadline:
        return failure("capability attempt deadline exceeded", True, ErrorCode.TIMEOUT)
    except (ValueError, OSError, TypeError, KeyError):
        return failure("capability publication or host proof rejected")
    except Exception as exc:
        # Store/service errors never expose claims, source, SQL or raw stderr.
        from psycopg import OperationalError
        retryable = isinstance(exc, OperationalError) or getattr(exc, "status_code", None) == 503
        return failure("capability authority unavailable" if retryable
                       else "capability authority rejected operation", retryable)
