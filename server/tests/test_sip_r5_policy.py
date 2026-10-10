"""In-memory project admission decisions and their catalog projection."""
from contextlib import contextmanager
from copy import deepcopy
import importlib
import json
import sys
from uuid import UUID

import pytest
from fastapi import HTTPException

import deps
import entitlements
import platform_link
import product_capability_availability as availability
import solar_project_context as project
import solar_project_graph as graph
import solar_project_jobs as service
import solar_tools
import tool_validate
from envelopes import ErrorCode, err_envelope
from solar_design_graph import GraphValidationError
from leaf_platform import canonical_jobs as jobs, entitlements as stored
import test_sip_r3a_graph as r3a
import test_sip_r3b_tools as r3b
import test_sip_r4_jobs as r4
import test_w1_design_graph as w1


NINE = frozenset((
    "solar-assign-equipment", "solar-combiners", "solar-feeders", "solar-homeruns",
    "solar-panels-from-drawing", "solar-schedule", "solar-settings",
    "solar-size-strings", "solar-string-add",
))


def policy():
    return importlib.import_module("solar_project_admission")


def setup(monkeypatch):
    s = r3b.memory.__wrapped__(monkeypatch)
    a = r4.admission.__wrapped__(s, monkeypatch)
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    monkeypatch.setattr(deps, "auth_live", lambda: False)
    monkeypatch.setattr(platform_link, "resolve_submission_context", lambda *args:
        {"org_id": s.org, "project_id": s.project, "authority_mode": "postgres_canonical"})
    monkeypatch.setattr(entitlements, "resolve_tier", lambda tenant: "hosted_pro")
    monkeypatch.setattr(entitlements, "resolve_roles", lambda tenant: ((), False))
    monkeypatch.setattr(entitlements, "entitlements_for", lambda *args: {"run_write": True})
    return a


def request(a, params=None, *, tool_name="solar-settings", **changes):
    s = a.s
    tool = solar_tools.trusted_record(tool_name)
    args = {"tool": tool, "tenant": str(s.org), "org_header": str(s.org),
        "project_header": str(s.project), "authorization": None,
        "input_version_id": s.parent.version_id, "drawing_id": s.drawing,
        "params": r3a.prepare(s).request["parameters"] if params is None else params,
        "catalog_digest": deps.catalog_tool_digest(tool), "idempotency_key": "key",
        "checkout_capability": "private-capability", "preview": True}
    args.update(changes)
    return policy().project_tool_admission(**args)


def assert_refusal(result, reason, *, status=409, message="Canonical Solar request was refused.",
                   retryable=False):
    code = ErrorCode.FORBIDDEN if status == 403 else (
        ErrorCode.INTERNAL if status >= 500 else ErrorCode.BAD_PARAMS)
    body = err_envelope(code, message, retryable)
    body["error"]["reason_code"] = reason
    assert result == {"status_code": status, "content": body}


def identity_refusal(status, message):
    code = ErrorCode.UNAUTHENTICATED if status == 401 else ErrorCode.FORBIDDEN
    return {"status_code": status, "content": err_envelope(code, message, False)}


def watch_prepare(monkeypatch, events):
    original = service._validate_new_project_graph_params
    def prepare(*args, **kwargs):
        events.append("prepare")
        return original(*args, **kwargs)
    monkeypatch.setattr(service, "_validate_new_project_graph_params", prepare)


def test_sip_r5_flag_defaults_off(monkeypatch):
    module = policy()
    monkeypatch.setattr(platform_link, "resolve_submission_context", r4.forbidden)
    monkeypatch.setattr(project, "verify_at_admission", r4.forbidden)
    monkeypatch.setattr(platform_link, "platform_db", r4.forbidden)
    monkeypatch.setattr(entitlements, "resolve_tier", r4.forbidden)
    args = dict(org_header=None, project_header=None, authorization=None,
        input_version_id=None, drawing_id=None, params={}, catalog_digest=None,
        idempotency_key=None, checkout_capability=None, preview=True)
    for value in (None, "0", "true", "TRUE", "yes", "on", "2", "1 ", " 1", "01", "", "\t1\n"):
        if value is None:
            monkeypatch.delenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", raising=False)
        else:
            monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", value)
        assert module.project_runs_enabled() is False
        assert_refusal(module.project_tool_admission({}, None, **args),
            "project_execution_disabled", message="Project Solar admission unavailable.")
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    assert module.project_runs_enabled() is True
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "0")
    assert module.project_runs_enabled() is False


def test_sip_r5_exact_supported_identity(monkeypatch):
    a = setup(monkeypatch)
    assert graph.SUPPORTED_TOOLS == NINE and len(solar_tools.entries()) == 53
    calls = []
    monkeypatch.setattr(service, "submit_project_graph_job", lambda *args, **kwargs:
        calls.append((args, kwargs)) or {"admissible": True, "replay": False})
    for name in sorted(NINE):
        tool = solar_tools.trusted_record(name)
        assert entitlements.tool_required_capability(tool) == "run_write"
        before = deepcopy(tool)
        assert request(a, {}, tool_name=name)["status_code"] == 200
        assert calls[-1][0][4] == name
        assert calls[-1][1]["tool_manifest_sha256"] == r3b.manifest(name)
        assert calls[-1][1]["preview"] is True and tool == before
        tool["description"] = "Changed record"
        assert_refusal(request(a, {}, tool_name=name, tool=tool,
            catalog_digest=deps.catalog_tool_digest(tool)), "SIP_R3_TOOL_MANIFEST_MISMATCH",
            message="Project Solar admission unavailable.")
    for name in ("solar-string-multi-add", "solar-pvcase-convert", "solar-commit-solve"):
        assert_refusal(request(a, {}, tool_name=name), "SIP_R3_TOOL_UNSUPPORTED",
            message="Project Solar admission unavailable.")
    assert len(calls) == 9
    r3a.refused("SIP_R3_TOOL_UNSUPPORTED", lambda: graph._tool("solar-string-multi-add", "x"))
    r3a.refused("SIP_R3_TOOL_MANIFEST_MISMATCH", lambda: graph._tool("solar-settings", "x"))
    result = request(a, {}, tenant=str(UUID(int=999)))
    assert result["status_code"] == 409 and len(calls) == 9
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    assert request(a, {})["status_code"] == 409 and len(calls) == 9
    tenant = deps.TenantContext(str(a.s.org), subject="verified-subject")
    assert request(a, {}, tenant=tenant)["status_code"] == 200
    assert calls[-1][0][0] is tenant


def test_sip_r5_preview_success_is_read_only(monkeypatch):
    a = setup(monkeypatch)
    s = a.s
    for embedded in (False, True):
        changes = {}
        if embedded:
            context = r3b.chain(s, 1)[-1][2]
            params = {"expected_rev": 1, "changes": {"num_mppt": 2}}
            r4.at(a, monkeypatch, context, "solar-settings")
            changes["input_version_id"] = context.parent_version_id
        else:
            params = r3a.prepare(s).request["parameters"]
        before = deepcopy((s.blobs, s.versions, s.fingerprints, s.writes, params))
        calls_before = list(s.calls)
        start = a.events.count("insert")
        with monkeypatch.context() as patch:
            patch.setattr(graph, "publish_project_graph_commit", r4.forbidden)
            patch.setattr(s, "put_if_absent_or_verify", r4.forbidden)
            result = request(a, params, **changes)
        assert result == {"status_code": 200, "content": {
            "admissible": True, "replay": False, "error": None, "degraded_mode": False}}
        assert "job_id" not in result["content"]
        assert a.events.count("insert") == start == 0 and a.accepted == []
        assert (s.blobs, s.versions, s.fingerprints, s.writes, params) == before
        assert s.calls == calls_before
    assert a.events.count("entitlement") == 2 and a.events.count("guard") == 2
    assert sorted(r3a.prepare(s).request["parameters"]) == ["changes", "expected_rev", "initialize"]


def test_sip_r5_preview_schema_precedence(monkeypatch):
    a = setup(monkeypatch)
    params = r3a.prepare(a.s).request["parameters"]
    settings = {"expected_rev": 1, "changes": {"num_mppt": 2}}
    tool = solar_tools.trusted_record("solar-settings")
    assert tool_validate.validate_params(tool, settings) == []
    assert tool_validate.validate_params(tool, {**settings, "surprise": 1}) == [
        "<root>: Additional properties are not allowed ('surprise' was unexpected)"]
    assert tool_validate.validate_params(tool, {**settings, "changes": {"surprise": 1}}) == []
    watch_prepare(monkeypatch, a.events)
    bad_seed = deepcopy(params)
    bad_seed["initialize"]["surprise"] = 1
    for candidate, name, code in (({**params, "surprise": 1}, "solar-settings", "INVALID_SETTINGS_REQUEST"),
            (bad_seed, "solar-settings", "INVALID_SEED_REQUEST"),
            (params, "solar-feeders", "INVALID_SEED_REQUEST")):
        before = deepcopy(candidate)
        assert_refusal(request(a, candidate, tool_name=name), code)
        assert candidate == before
    assert "prepare" not in a.events and "entitlement" not in a.events and not a.accepted


def test_sip_r5_preview_preparation_precedence(monkeypatch):
    a = setup(monkeypatch)
    params = r3a.prepare(a.s).request["parameters"]
    original_context = graph.resolve_project_graph_context(a.s.context())
    prepare = service._validate_new_project_graph_params
    watch_prepare(monkeypatch, a.events)
    nested = deepcopy(params)
    nested["changes"]["surprise"] = 1
    source = deepcopy(params)
    source["initialize"]["source_intake_sha256"] = "0" * 64
    for candidate, code in ((nested, "INVALID_SETTINGS_REQUEST"),
            (source, "SOURCE_HASH_MISMATCH"),
            ({"expected_rev": 0, "changes": {"num_mppt": 2}}, "GRAPH_NOT_EMBEDDED")):
        assert_refusal(request(a, candidate), code)
    context = r3b.chain(a.s, 1)[-1][2]
    r4.at(a, monkeypatch, context, "solar-settings")
    for candidate, code in (({"expected_rev": 7, "changes": {"num_mppt": 2}}, "STALE_GRAPH_REVISION"),
            ({"expected_rev": 1, "changes": {"num_mppt": 2}, "cancel": True}, "GRAPH_COMMIT_CANCELLED")):
        assert_refusal(request(a, candidate, input_version_id=context.parent_version_id), code)
    assert a.events.count("prepare") == 5 and "entitlement" not in a.events and not a.accepted
    for candidate, code in ((nested, "INVALID_SETTINGS_REQUEST"),
            (source, "SOURCE_HASH_MISMATCH"),
            ({"expected_rev": 0, "changes": {"num_mppt": 2}}, "GRAPH_NOT_EMBEDDED")):
        with pytest.raises(GraphValidationError) as caught:
            prepare(original_context, "solar-settings", candidate, checkout=a.s.lease,
                tool_manifest_sha256=r3b.manifest("solar-settings"))
        assert caught.value.code == code and str(caught.value) == code + ": <root>"


def test_sip_r5_preview_manual_sizing(monkeypatch):
    a = setup(monkeypatch)
    context = r3b.chain(a.s, 4)[-2][2]
    assert availability.w1_graph_readiness(deepcopy(context.graph))["solar-size-strings"] == {
        "input_ready": False, "input_reason": "project_name_required"}
    r4.at(a, monkeypatch, context, "solar-size-strings")
    params = {"expected_rev": 3, "mode": "manual-global", "confirm": True}
    result = request(a, params, tool_name="solar-size-strings", input_version_id=context.parent_version_id)
    assert result["status_code"] == 200 and result["content"]["admissible"] is True
    assert_refusal(request(a, {**params, "mode": "global"}, tool_name="solar-size-strings",
        input_version_id=context.parent_version_id), "SIP_R3_SERVICE_EVIDENCE_REQUIRED",
        message="Project Solar admission unavailable.")
    assert not a.accepted and a.events.count("entitlement") == 1
    with pytest.raises(project.ProjectContextError) as caught:
        service._validate_new_project_graph_params(context, "solar-size-strings",
            {**params, "mode": "global"}, checkout=a.s.lease,
            tool_manifest_sha256=r3b.manifest("solar-size-strings"))
    assert str(caught.value) == "SIP_R3_SERVICE_EVIDENCE_REQUIRED"


def test_sip_r5_preview_replay(monkeypatch):
    a = setup(monkeypatch)
    s = a.s
    params = r3a.prepare(s).request["parameters"]
    fingerprint = service._submission_fingerprint(s.org, s.project, s.drawing,
        s.parent.version_id, str(s.org), s.actor, "solar-settings", params, r3b.manifest("solar-settings"))
    row = {"job_id": str(s.job), "execution_context": {"schema": jobs.PROJECT_GRAPH_JOB_SCHEMA},
        "submission_fingerprint": fingerprint, "deleted_at": None}
    monkeypatch.setattr(jobs, "get_project_graph_job_by_key", lambda *args, **kwargs: row)
    monkeypatch.setattr(project, "verify_at_admission", r4.forbidden)
    monkeypatch.setattr(service, "_validate_new_project_graph_params", r4.forbidden)
    monkeypatch.setattr(stored, "stored_job_entitlement_verdict", r4.forbidden)
    monkeypatch.setattr(graph, "_tool", r4.forbidden)
    monkeypatch.setattr(s, "get", r4.forbidden)
    assert service.submit_project_graph_job(str(s.org), s.project, s.drawing, s.parent.version_id,
        "solar-settings", params, tool_manifest_sha256=r3b.manifest("solar-settings"),
        checkout_capability=None, idempotency_key="key", preview=True) is row
    result = request(a, params, checkout_capability=None)
    assert result == {"status_code": 200, "content": {
        "admissible": True, "replay": True, "error": None, "degraded_mode": False}}
    submitted = request(a, params, checkout_capability=None, preview=False)
    assert submitted == {"status_code": 202, "content": {
        "job_id": str(s.job), "status": "submitted", "error": None, "degraded_mode": False}}
    assert_refusal(request(a, {**params, "cancel": False}), "SIP_R4_IDEMPOTENCY_CONFLICT",
        message="Project Solar admission unavailable.")
    row["deleted_at"] = "deleted"
    assert_refusal(request(a, params), "SIP_R1_CONTEXT_NOT_FOUND", status=404,
        message="Project Solar admission unavailable.")
    assert not a.accepted and "guard" not in a.events and "entitlement" not in a.events


def test_sip_r5_preview_entitlement_order(monkeypatch):
    a = setup(monkeypatch)
    params = r3a.prepare(a.s).request["parameters"]
    watch_prepare(monkeypatch, a.events)
    denial = entitlements.entitlement_denied_response("run_write", "restricted")
    with monkeypatch.context() as patch:
        patch.setattr(entitlements, "entitlements_for", lambda *args: {"run_write": False})
        patch.setattr(entitlements, "entitlement_denied_response", lambda *args: denial)
        result = request(a, params)
        assert result == {"status_code": 403, "content": json.loads(denial.body)}
        assert not a.events
    stored_calls = []
    def stored_denial(org, kind):
        a.events.append("stored")
        stored_calls.append((org, kind))
        return denial, None
    monkeypatch.setattr(stored, "stored_job_entitlement_verdict", stored_denial)
    for preview in (True, False):
        result = request(a, params, preview=preview)
        assert result == {"status_code": 403, "content": json.loads(denial.body)}
    assert stored_calls == [(a.s.org, "run"), (a.s.org, "run")]
    assert [event for event in a.events if event in ("prepare", "stored")] == [
        "prepare", "stored", "prepare", "stored"]
    assert not a.accepted and "guard" not in a.events
    stored_unavailable = stored.policy_unavailable_response(None, "run")
    monkeypatch.setattr(stored, "stored_job_entitlement_verdict", lambda *args: (stored_unavailable, None))
    for preview in (True, False):
        assert request(a, params, preview=preview) == {
            "status_code": 503, "content": json.loads(stored_unavailable.body)}
    unavailable = entitlements.policy_unavailable_response("run_write", "hosted_pro")
    def unavailable_policy(*args):
        raise entitlements.EntitlementsError("private-marker")
    monkeypatch.setattr(entitlements, "entitlements_for", unavailable_policy)
    assert request(a, params) == {"status_code": 503, "content": json.loads(unavailable.body)}


def test_sip_r5_preview_drain_refusal(monkeypatch):
    a = setup(monkeypatch)
    params = r3a.prepare(a.s).request["parameters"]
    @contextmanager
    def drained():
        a.events.append("drained")
        yield {"refused": True}
    monkeypatch.setattr(project.write_loop, "drawing_mutation_refusal_guard", drained)
    for preview in (True, False):
        assert_refusal(request(a, params, preview=preview), "SIP_R1_WRITES_DRAINED",
            status=503, retryable=True, message="Project Solar admission unavailable.")
    assert a.events.count("entitlement") == a.events.count("drained") == 2
    assert not a.accepted and "insert" not in a.events and not a.s.writes


def test_sip_r5_failure_projection(monkeypatch):
    a = setup(monkeypatch)
    mappings = {
        "SIP_R1_INVALID_BINDING": (400, False), "SIP_R1_CONTEXT_NOT_FOUND": (404, False),
        "SIP_R1_PROJECT_FORBIDDEN": (403, False), "SIP_R1_CANONICAL_AUTHORITY_REQUIRED": (409, False),
        "SIP_R1_INTAKE_PROOF_REQUIRED": (409, False), "SIP_R1_INTAKE_REFERENCE_INVALID": (500, False),
        "SIP_R1_INTAKE_DIGEST_MISMATCH": (500, False), "SIP_R1_INTAKE_INVALID": (500, False),
        "SIP_R1_INTAKE_UNAVAILABLE": (503, True), "SIP_R1_STORE_UNAVAILABLE": (503, True),
        "SIP_R1_CHECKOUT_PARAMS_INVALID": (400, False), "SIP_R1_CHECKOUT_CONFLICT": (409, True),
        "SIP_R1_CHECKOUT_REQUIRED": (409, False), "SIP_R1_CHECKOUT_EXPIRED": (409, False),
        "SIP_R1_CHECKOUT_DENIED": (403, False), "SIP_R1_CHECKOUT_STALE": (409, False),
        "SIP_R1_CHECKOUT_UNAVAILABLE": (503, True), "SIP_R1_FENCE_EXHAUSTED": (409, False),
        "SIP_R1_WRITES_DRAINED": (503, True), "SIP_R1_STALE_VERSION": (409, False),
        "SIP_R1_INTERNAL": (500, False), "SIP_R2_PUBLICATION_PARAMS_INVALID": (400, False),
        "SIP_R2_IDEMPOTENCY_CONFLICT": (409, False), "SIP_R2_PUBLICATION_REQUIRED": (409, False),
        "SIP_R3_TOOL_UNSUPPORTED": (409, False), "SIP_R3_TOOL_MANIFEST_MISMATCH": (409, False),
        "SIP_R3_SERVICE_EVIDENCE_REQUIRED": (409, False), "SIP_R3_PROOF_REJECTED": (500, False),
        "SIP_R4_IDEMPOTENCY_KEY_REQUIRED": (400, False), "SIP_R4_IDEMPOTENCY_CONFLICT": (409, False),
        "SIP_R4_JOB_BINDING_MISMATCH": (409, False),
    }
    assert policy()._FAILURES == mappings
    for preview in (True, False):
        target, name = (service, "submit_project_graph_job") if preview else (
            platform_link, "submit_canonical_graph")
        for reason, (status, retry) in mappings.items():
            def fail(*args, **kwargs):
                raise project.ProjectContextError(reason)
            monkeypatch.setattr(target, name, fail)
            assert_refusal(request(a, {}, preview=preview), reason, status=status,
                retryable=retry, message="Project Solar admission unavailable.")
        for exc, reason, message, status in ((GraphValidationError("INVALID_SETTINGS_REQUEST"),
                "INVALID_SETTINGS_REQUEST", "Canonical Solar request was refused.", 409),
                (RuntimeError("private-marker"), "SIP_R1_INTERNAL", "Project Solar admission unavailable.", 500),
                (project.ProjectContextError("private-marker"), "SIP_R1_INTERNAL",
                 "Project Solar admission unavailable.", 500)):
            def fail(*args, **kwargs):
                raise exc
            monkeypatch.setattr(target, name, fail)
            result = request(a, {}, preview=preview)
            assert_refusal(result, reason, status=status, message=message)
            assert "private-marker" not in json.dumps(result) and "<root>" not in json.dumps(result)
    for message, status in (("X-Project-Id is required with X-Org-Id", 400),
            ("project context does not belong to the verified platform tenant", 409),
            ("private-marker", 409)):
        def bad_context(*args):
            raise ValueError(message)
        monkeypatch.setattr(platform_link, "resolve_submission_context", bad_context)
        result = request(a, {})
        assert result["status_code"] == status
        assert result["content"]["error"]["message"] == (
            "Project Solar admission unavailable." if message == "private-marker" else message)


def test_sip_r5_identity_refusal_projection(monkeypatch):
    a = setup(monkeypatch)
    submitted = []
    monkeypatch.setattr(service, "submit_project_graph_job",
        lambda *args, **kwargs: submitted.append("preview") or {"admissible": True, "replay": False})
    monkeypatch.setattr(platform_link, "submit_canonical_graph",
        lambda *args, **kwargs: submitted.append("submit") or "job")
    unauthenticated = "Authentication is required for project Solar admission."
    forbidden = "Project Solar admission is not permitted for this identity."
    sentences = ("token verified but carries no external subject",
        "verified subject has no active platform identity binding",
        "platform role does not permit mutation")
    assert policy()._IDENTITY_REFUSALS == frozenset(sentences)
    cases = [(401, "missing bearer token (Authorization header)", identity_refusal(401, unauthenticated)),
        (401, "invalid token: private-marker", identity_refusal(401, unauthenticated)),
        (401, {"private-marker": 1}, identity_refusal(401, unauthenticated)),
        (403, "private-marker", identity_refusal(403, forbidden)),
        (403, sentences[2] + " private-marker", identity_refusal(403, forbidden)),
        (403, {"private-marker": 1}, identity_refusal(403, forbidden)),
        (403, ["private-marker"], identity_refusal(403, forbidden)),
        (403, None, identity_refusal(403, forbidden))]
    cases += [(403, sentence, identity_refusal(403, sentence)) for sentence in sentences]
    for preview in (True, False):
        for status, detail, expected in cases:
            def refuse(*args):
                raise HTTPException(status_code=status, detail=detail)
            monkeypatch.setattr(platform_link, "resolve_submission_context", refuse)
            result = request(a, {}, preview=preview)
            assert result == expected
            assert "reason_code" not in result["content"]["error"]
            assert "private-marker" not in json.dumps(result)
        for status in (400, 404, 409, 422, 500, 503):
            def refuse(*args):
                raise HTTPException(status_code=status, detail="private-marker")
            monkeypatch.setattr(platform_link, "resolve_submission_context", refuse)
            result = request(a, {}, preview=preview)
            assert_refusal(result, "SIP_R1_INTERNAL", status=500,
                message="Project Solar admission unavailable.")
            assert "private-marker" not in json.dumps(result)
    assert submitted == [] and not a.accepted and "insert" not in a.events and not a.s.writes


def test_sip_r5_viewer_role_through_resolver(monkeypatch):
    resolver = platform_link.resolve_submission_context
    a = setup(monkeypatch)
    seen = []
    class LiveIdentity:
        @staticmethod
        def auth_live():
            return True
        @staticmethod
        def get_write_org_id(*, x_org_id, authorization):
            seen.append((x_org_id, authorization))
            raise HTTPException(status_code=403, detail="platform role does not permit mutation")
    monkeypatch.setattr(platform_link, "resolve_submission_context", resolver)
    monkeypatch.setattr(platform_link, "_db_configured", lambda: True)
    monkeypatch.setattr(platform_link, "_load_platform", lambda: (None, None, LiveIdentity))
    monkeypatch.setattr(service, "submit_project_graph_job", r4.forbidden)
    monkeypatch.setattr(platform_link, "submit_canonical_graph", r4.forbidden)
    for preview in (True, False):
        result = request(a, {}, preview=preview, authorization="Bearer private-marker")
        assert result == identity_refusal(403, "platform role does not permit mutation")
        assert "private-marker" not in json.dumps(result)
    assert seen == [(None, "Bearer private-marker")] * 2
    assert not a.accepted and "insert" not in a.events and not a.s.writes


def test_sip_r5_catalog_states(monkeypatch):
    a = setup(monkeypatch)
    module = policy()
    tool = solar_tools.trusted_record("solar-settings")
    accepted = request(a)
    invalid = request(a, {"expected_rev": 0, "changes": {"num_mppt": 2}})
    denied = {"status_code": 403, "content": json.loads(
        entitlements.entitlement_denied_response("run_write", "restricted").body)}
    unavailable = {"status_code": 503, "content": json.loads(
        entitlements.policy_unavailable_response("run_write", "hosted_pro").body)}
    stored_unavailable = {"status_code": 503, "content": json.loads(
        stored.policy_unavailable_response(None, "run").body)}
    untrusted = deepcopy(tool)
    untrusted["description"] = "Changed record"
    for record, enabled, result, engine, ready, entitled, checked, reason, ent_reason in (
            (tool, False, accepted, False, False, False, False, "project_execution_disabled", None),
            (solar_tools.trusted_record("solar-string-multi-add"), True, None, False, False,
             False, False, "project_adapter_unavailable", None),
            (untrusted, True, None, False, False, False, False, "project_adapter_unavailable", None),
            (tool, True, None, True, False, False, False, "project_request_required", None),
            (tool, True, accepted, True, True, True, True, None, None),
            (tool, True, invalid, True, False, False, True, "capability_not_ready", None),
            (tool, True, denied, True, False, False, True, "entitlement_required", "entitlement_required"),
            (tool, True, unavailable, True, False, False, True,
             "entitlement_policy_unavailable", "entitlement_policy_unavailable"),
            (tool, True, stored_unavailable, True, False, False, True,
             "entitlement_policy_unavailable", "entitlement_policy_unavailable")):
        before = deepcopy((record, result))
        expected = {"entitled": entitled, "engine_ready": engine, "implemented": True,
            "input_ready": ready, "input_reason": None if ready else reason,
            "entitlement_reason": ent_reason, "engine_reason": None if engine else reason,
            "implementation_reason": None, "refusal_reasons": [reason] if reason else [],
            "runnable": entitled and engine and ready, "admission_checked": checked,
            "admission": {"status_code": result["status_code"],
                          "error": result["content"].get("error")} if checked else None}
        assert module.project_catalog_availability(record, enabled=enabled, admission=result) == expected
        assert (record, result) == before
    with monkeypatch.context() as patch:
        patch.setattr(platform_link, "platform_db", r4.forbidden)
        patch.setattr(project, "verify_at_admission", r4.forbidden)
        assert module.project_catalog_availability(tool, enabled=False)["engine_ready"] is False
        assert module.project_catalog_availability(untrusted, enabled=True)["engine_ready"] is False


def test_sip_r5_legacy_projection_unchanged(monkeypatch):
    legacy = w1.graph.__wrapped__()
    original = deepcopy(legacy)
    tools = solar_tools.trusted_snapshot()
    tools_before = deepcopy(tools)
    manifest = solar_tools.manifest()
    digests = {name: deps.catalog_tool_digest(tool) for name, tool in tools.items()}
    def shared_tables():
        return deepcopy((availability.SOLAR_CAPABILITIES, availability.W1_CAPABILITIES))
    shared_before = shared_tables()
    def snapshot():
        readiness = availability.w1_graph_readiness(deepcopy(legacy))
        states = {name: availability.w1_availability(name, entitled=True, inputs=deepcopy(inputs))
                  for name, inputs in readiness.items()}
        return readiness, states
    before = snapshot()
    assert before[1]["solar-settings"] == {"entitled": True, "engine_ready": True,
        "implemented": True, "input_ready": True, "input_reason": None,
        "entitlement_reason": None, "engine_reason": None, "implementation_reason": None,
        "refusal_reasons": [], "runnable": True}
    assert availability.w1_input_readiness("demo")["solar-settings"] == {
        "input_ready": False, "input_reason": "drawing_context_required"}
    assert availability.w1_input_readiness("demo", "drawing", version=str(UUID(int=3)))[
        "solar-settings"] == {"input_ready": False, "input_reason": "invalid_drawing_context"}
    monkeypatch.delitem(sys.modules, "solar_project_admission", raising=False)
    module = policy()
    a = setup(monkeypatch)
    result = request(a)
    assert result["status_code"] == 200
    for enabled in (False, True):
        for tool in tools.values():
            module.project_catalog_availability(tool, enabled=enabled, admission=deepcopy(result))
    assert snapshot() == before
    assert shared_tables() == shared_before
    assert legacy == original and tools == tools_before
    assert solar_tools.manifest() == manifest and len(manifest) == 53
    assert {name: deps.catalog_tool_digest(tool) for name, tool in solar_tools.trusted_snapshot().items()} == digests
