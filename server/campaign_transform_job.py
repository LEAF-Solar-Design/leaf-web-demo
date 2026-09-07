"""Published records-to-CSV tools under the campaign completion job lease."""
from __future__ import annotations

import hashlib
import math
import re
import time
import uuid

from envelopes import ErrorCode, err_envelope, ok_envelope

CONSTANTS = {
    "schema": "leaf.campaign-transform.v1",
    "capability": "campaign.records-to-csv",
    "recipe_id": "json-records-to-csv",
    "recipe_version": 1,
    "tool_name": "campaign-records-to-csv",
}
IDS = {"org_id", "project_id", "campaign_id", "release_id", "binding_id"}
KEYS = set(CONSTANTS) | IDS | {
    "tenant_id", "contract_version", "change_set_id", "catalog_commit",
    "effective_catalog_digest", "tool_manifest_sha256", "tool_source_sha256", "input_sha256",
}


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def validate_context(value):
    if not isinstance(value, dict) or set(value) != KEYS:
        raise ValueError("invalid completion context")
    if any(value[k] != v for k, v in CONSTANTS.items()):
        raise ValueError("invalid completion identity")
    for key in ("recipe_version", "contract_version"):
        if type(value[key]) is not int or value[key] < 1:
            raise ValueError("invalid completion version")
    for key in IDS:
        if not isinstance(value[key], str) or str(uuid.UUID(value[key])) != value[key]:
            raise ValueError("invalid completion UUID")
    for key, cap in (("tenant_id", 32768), ("change_set_id", 200)):
        text = value[key]
        if (not isinstance(text, str) or not 1 <= len(text) <= cap
                or any(ord(c) < 32 or ord(c) == 127 for c in text)):
            raise ValueError("invalid completion token")
    for key in ("catalog_commit", "effective_catalog_digest", "tool_manifest_sha256",
                "tool_source_sha256", "input_sha256"):
        prefix = "sha256:" if key == "tool_manifest_sha256" else ""
        size = 40 if key == "catalog_commit" else 64
        if not isinstance(value[key], str) or re.fullmatch(prefix + "[0-9a-f]{%d}" % size, value[key]) is None:
            raise ValueError("invalid completion digest")
    return dict(value)


def execution_context(execution):
    if not isinstance(execution, dict):
        raise ValueError("invalid durable execution context")
    if "completion_provenance" not in execution:
        return None
    if "capability_provenance" in execution or "plan" in execution:
        raise ValueError("completion execution kinds conflict")
    return validate_context(execution["completion_provenance"])


def record_context(execution, record):
    context = execution_context(execution)
    if context is not None and any(context[key] != record.get(column) for key, column in (
        ("tenant_id", "tenant_id"), ("org_id", "org_id"),
        ("project_id", "project_id"), ("tool_name", "tool"),
    )):
        raise ValueError("durable completion job scope mismatch")
    return context


def input_bytes(context, params):
    import campaign_web_tool_static as static

    if not isinstance(params, dict) or set(params) != {"source_json"} or not isinstance(params["source_json"], str):
        raise ValueError("invalid completion params")
    raw = params["source_json"].encode("utf-8")
    if _sha(raw) != context["input_sha256"]:
        raise ValueError("completion input changed")
    static.expected_output(raw)
    return raw


def check_authority(context):
    from leaf_platform import campaigns, campaign_release

    scope = campaigns._scope(context["org_id"], context["project_id"])
    with campaigns._cursor() as cur:
        campaigns._principal(cur, scope, uuid.UUID(context["binding_id"]))
    campaign = campaigns.get_campaign(context["org_id"], context["project_id"], context["campaign_id"])
    if campaign is None or campaign.get("tenant_id") != context["tenant_id"]:
        raise ValueError("completion campaign authority changed")
    snapshot = campaign_release.get_release(context["org_id"], context["project_id"],
                                            context["campaign_id"], context["release_id"])
    release = snapshot.get("release") if isinstance(snapshot, dict) else None
    if (not isinstance(release, dict) or release.get("status") != "active"
            or release.get("contract_version") != context["contract_version"]
            or any(str(release.get(key)) != context[key] for key in ("org_id", "project_id", "campaign_id", "release_id"))):
        raise ValueError("completion release authority changed")


def published_tool(context, supplied):
    import customization_service
    import deps
    from customization_models import ChangeState

    tenant = context["tenant_id"]
    if customization_service.effective_catalog_pin(tenant) != {
            k: context[k] for k in ("catalog_commit", "effective_catalog_digest")}:
        raise ValueError("published catalog changed")
    service = customization_service.CustomizationService.configured()
    pin = service.store.get_effective_catalog(tenant_id=tenant)
    change = service.store.get_change_set(tenant_id=tenant, change_set_id=context["change_set_id"])
    if (pin is None or pin.tenant_id != tenant or pin.change_set_id != context["change_set_id"]
            or pin.catalog_commit != context["catalog_commit"] or pin.catalog_digest != context["effective_catalog_digest"]
            or change.tenant_id != tenant or change.change_set_id != context["change_set_id"]
            or change.state != ChangeState.PUBLISHED or change.staged_commit != context["catalog_commit"]
            or change.catalog_digest != context["effective_catalog_digest"]):
        raise ValueError("published change set changed")
    winners = [(tool, source) for tool, source in deps.effective_tools_with_provenance(tenant)
               if tool.get("name") == context["tool_name"]]
    if (len(winners) != 1 or winners[0][1] != deps.TOOL_SOURCE_TENANT_REPO
            or not isinstance(supplied, dict) or supplied.get("name") != context["tool_name"]):
        raise ValueError("published tool source changed")
    tool = winners[0][0]
    # A published catalog is cumulative. Its latest change may have added a
    # different tool while retaining this exact selected tenant tool.
    if any(deps.catalog_tool_digest(t) != context["tool_manifest_sha256"]
           for t in (tool, supplied)):
        raise ValueError("published tool manifest changed")
    return tool


def capture_source(context, tool):
    import tool_loader

    local = tool_loader.resolve_local_file(tool, context["tenant_id"])
    contained = tool_loader._contained_published_path(local, context["tenant_id"]) if local is not None else None
    if contained is None:
        raise ValueError("published source is unavailable")
    limit = tool_loader._SANDBOX_LIMITS["source_bytes"]
    with open(contained, "rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("published source exceeds bound")
    source = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    if _sha(source.encode("utf-8")) != context["tool_source_sha256"]:
        raise ValueError("published source changed")
    return source


def validate_result(context, params, result):
    import campaign_web_tool_static as static

    raw = input_bytes(context, params)
    if not isinstance(result, dict) or set(result) != {"csv"} or not isinstance(result["csv"], str):
        raise ValueError("invalid authored CSV result")
    actual = result["csv"].encode("utf-8")
    if len(actual) > static.MAX_OUTPUT_BYTES or actual != static.expected_output(raw):
        raise ValueError("authored CSV does not match records")
    return {"input_sha256": _sha(raw), "tool_source_sha256": context["tool_source_sha256"],
            "output_sha256": _sha(actual)}


def validate_terminal(context, tool, params, result, provenance):
    context = validate_context(context)
    if (not isinstance(provenance, dict) or set(provenance) != {
            "attempt", "execution_path", "completion_provenance", "input_sha256", "tool_source_sha256", "output_sha256"}
            or provenance["completion_provenance"] != context):
        raise ValueError("completion terminal identity mismatch")
    evidence = validate_result(context, params, result)
    if any(provenance.get(k) != v for k, v in evidence.items()):
        raise ValueError("completion terminal evidence mismatch")
    check_authority(context)
    published_tool(context, tool)


def run(job_id, completion_provenance, tool, params, heartbeat, cancelled, deadline):
    import jobs
    import tool_loader

    started = time.monotonic()

    def guard(budget=0):
        if cancelled() or not heartbeat() or cancelled():
            raise ValueError("completion attempt is no longer owned")
        if not math.isfinite(deadline) or time.monotonic() + budget >= deadline:
            raise ValueError("completion deadline exhausted")

    try:
        context = validate_context(completion_provenance)
        guard()
        input_bytes(context, params)
        check_authority(context)
        published = published_tool(context, tool)
        source = capture_source(context, published)
        timeout = tool_loader._sandbox_timeout_s()
        if not math.isfinite(timeout) or timeout <= 0 or timeout + 1 >= min(jobs.lease_duration_s(), jobs.heartbeat_stale_s()):
            raise ValueError("sandbox exceeds owning lease budget")
        guard(timeout + 1)
        env = tool_loader.run_tool_dynamic(published, {}, params, False,
                                           tenant_id=context["tenant_id"], test_source=source)
        guard()
        if not isinstance(env, dict) or env.get("ok") is not True:
            raise ValueError("authored tool did not execute successfully")
        actual = env.get("result")
        validate_result(context, params, actual)
        check_authority(context)
        published_tool(context, tool)
        return ok_envelope(tool["name"], tool.get("version", ""), actual, None,
                           int((time.monotonic() - started) * 1000))
    except Exception:
        return err_envelope(ErrorCode.INTERNAL, "Completion transform could not be verified", False,
                            tool=tool.get("name"), version=tool.get("version"),
                            timing_ms=int((time.monotonic() - started) * 1000))
