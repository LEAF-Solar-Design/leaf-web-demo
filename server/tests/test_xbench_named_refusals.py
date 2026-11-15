"""X02 refusals name the failed input and the action needed to continue."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

import deps
import platform_link
import solar_project_admission as admission
import solar_project_context as project
import solar_project_jobs as service
import tool_loader
import tools_fallback as fallback
from envelopes import ErrorCode
from routers import jobs as route
from solar_design_graph import GraphValidationError
from solar_graph_seed import (new_empty_graph, resolve_seed_context, seed_entity_id,
                              seed_units, validate_seed_request)
import test_project_scoped_read_tool as reads
from test_project_scoped_read_tool import lane as read_lane, memory  # noqa: F401
import test_sip_r5_policy as policy


def units():
    return {"drawing_units": "m", "wcs_to_ucs": [1, 0, 0, 0, 0, 1, 0, 0,
                                                0, 0, 1, 0, 0, 0, 0, 1],
            "elevation_datum": "local", "crs": None}


def seed_request():
    return {"schema_version": 1, "source_intake_sha256": "b" * 64, "units": units()}


def assert_layer_refusal(body, name):
    assert body["ok"] is False and body["result"] is None and body["overlay"] is None
    error = body["error"]
    assert error["error_code"] == ErrorCode.BAD_PARAMS
    assert error["reason_code"] == "LAYER_NOT_FOUND"
    assert error["message"] == f'Layer "{name}" is not in this drawing.'
    assert error["retryable"] is False
    assert error["next_action"] == "Choose a layer from the drawing's layer list."


def test_absent_layer_is_a_named_refusal():
    intake = {"layers": ["Panels"], "polylines": [{"layer": "Panels"}]}
    before = deepcopy(intake)
    assert_layer_refusal(fallback.op_count_by_layer(intake, {"layer": "Missing"}), "Missing")
    assert intake == before


@pytest.mark.parametrize("name,display", [
    ("x" * 250, "x" * 200),
    ("bad\n\r\t\0\x1b\x7f\x85\u202elayer", "badlayer"),
    ("\n" * 205 + "Z" * 205, "Z" * 200),
    ("不存在", "不存在"),
])
def test_layer_name_is_bounded_and_controls_are_removed(name, display):
    assert_layer_refusal(fallback.run_op("count_by_layer", {}, {"layer": name}), display)


@pytest.mark.parametrize("layers", [["Empty"], [{"name": "Empty"}]])
def test_existing_empty_layer_still_counts_zero(layers):
    assert fallback.op_count_by_layer({"layers": layers}, {"layer": "Empty"}) == (
        {"counts": {"Empty": 0}, "total": 0}, None)


@pytest.mark.parametrize("kind", ["polylines", "inserts", "faces3d"])
def test_entities_establish_layer_presence_without_table_entry(kind):
    assert fallback.op_count_by_layer({kind: [{"layer": "Present"}]}, {"layer": "Present"}) == (
        {"counts": {"Present": 1}, "total": 1}, None)


@pytest.mark.parametrize("params", [{}, {"layer": ""}])
def test_all_layer_counts_are_unchanged(params):
    intake = {"layers": ["Empty"], "polylines": [{"layer": "A"}, {"layer": "A"}],
              "inserts": [{"layer": "B"}], "faces3d": [{"layer": "A"}]}
    assert fallback.op_count_by_layer(intake, params) == (
        {"counts": {"A": 3, "B": 1}, "total": 4}, None)


def test_builtin_loader_preserves_named_error(monkeypatch):
    monkeypatch.setenv("LEAF_TOOL_SANDBOX_PROVIDER", "off")
    tool = deepcopy(fallback.DEFAULT_TOOLS[0])
    body = tool_loader.run_tool_dynamic(tool, {}, {"layer": "Missing"}, aps_live=False)
    assert_layer_refusal(body, "Missing")


@pytest.mark.parametrize("wait", [0, 1])
def test_api_run_canonical_read_refuses_absent_layer(read_lane, monkeypatch, wait):
    tool = deepcopy(fallback.DEFAULT_TOOLS[0])
    monkeypatch.setattr(deps, "find_tool", lambda *_: tool)
    response = reads.request(read_lane, tool=tool, params={"layer": "Missing"}, wait=wait)
    assert response.status_code == 404, response.text
    assert_layer_refusal(response.json(), "Missing")
    assert response.headers["cache-control"] == "no-store"


def test_api_run_legacy_wait_preserves_reason_and_action(read_lane, monkeypatch):
    tool = deepcopy(fallback.DEFAULT_TOOLS[0])
    monkeypatch.setattr(deps, "find_tool", lambda *_: tool)
    monkeypatch.setattr(route, "_checkout_identity", lambda *_: ("anonymous", None))
    monkeypatch.setattr(platform_link, "resolve_submission_context", lambda *_: None)
    monkeypatch.setattr(deps, "APS_LIVE", False)
    monkeypatch.setattr(route.jobs, "submit_job", lambda *args, **kwargs: "failed-count")
    body = fallback.op_count_by_layer({}, {"layer": "Missing"})
    monkeypatch.setattr(route.jobs, "wait_for_terminal", lambda *args, **kwargs: {
        "job_id": "failed-count", "status": "failed", "tool": "count-by-layer",
        "error": body["error"], "elapsed_ms": 0})
    response = reads.request(read_lane, tool=tool, params={"layer": "Missing"}, headers={}, wait=1)
    assert response.status_code == 404, response.text
    assert_layer_refusal(response.json(), "Missing")


@pytest.mark.parametrize("field,value", [
    ("drawing_units", "unknown"), ("drawing_units", None),
    ("wcs_to_ucs", []), ("wcs_to_ucs", [True] * 16),
    ("wcs_to_ucs", [float("nan")] * 16), ("wcs_to_ucs", [2**65] * 16),
    ("elevation_datum", ""), ("elevation_datum", "\ud800"),
    ("crs", 7), ("crs", "\ud800"),
])
def test_seed_units_carries_offending_field(field, value):
    request = units()
    request[field] = value
    with pytest.raises(GraphValidationError) as caught:
        seed_units(request)
    assert (caught.value.code, caught.value.path) == ("INVALID_SEED_REQUEST", "units." + field)


@pytest.mark.parametrize("value", [None, [], {}, {**units(), "extra": 1}])
def test_seed_units_shape_names_units(value):
    with pytest.raises(GraphValidationError) as caught:
        seed_units(value)
    assert (caught.value.code, caught.value.path) == ("INVALID_SEED_REQUEST", "units")


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 2),
    ("source_intake_sha256", "B" * 64), ("source_intake_sha256", None),
    ("units", []),
])
def test_seed_request_carries_offending_field(field, value):
    request = seed_request()
    request[field] = value
    with pytest.raises(GraphValidationError) as caught:
        validate_seed_request(request)
    assert (caught.value.code, caught.value.path) == ("INVALID_SEED_REQUEST", field)


@pytest.mark.parametrize("field", ["schema_version", "source_intake_sha256", "units"])
def test_missing_seed_field_is_named(field):
    request = seed_request()
    del request[field]
    with pytest.raises(GraphValidationError) as caught:
        validate_seed_request(request)
    assert caught.value.path == field and caught.value.code == "INVALID_SEED_REQUEST"


@pytest.mark.parametrize("field", ["tenant_id", "drawing_id"])
def test_seed_identifier_refusals_name_identifier(field):
    ids = {"tenant_id": "fixture-tenant", "drawing_id": "solar"}
    ids[field] = ""
    with pytest.raises(GraphValidationError) as caught:
        seed_entity_id(**ids, kind="project")
    assert (caught.value.code, caught.value.path) == ("INVALID_SEED_REQUEST", field)
    with pytest.raises(GraphValidationError) as caught:
        new_empty_graph(**ids, source_hash="b" * 64, units=units(), created_at="2026-11-15")
    assert (caught.value.code, caught.value.path) == ("INVALID_SEED_REQUEST", field)


@pytest.mark.parametrize("field,path", [("source_hash", "source_intake_sha256"),
                                        ("created_at", "created_at")])
def test_new_graph_invalid_metadata_names_field(field, path):
    args = dict(tenant_id="fixture-tenant", drawing_id="solar", source_hash="b" * 64,
                units=units(), created_at="2026-11-15")
    args[field] = ""
    with pytest.raises(GraphValidationError) as caught:
        new_empty_graph(**args)
    assert (caught.value.code, caught.value.path) == ("INVALID_SEED_REQUEST", path)


def test_seed_context_invalid_hash_names_field():
    with pytest.raises(GraphValidationError) as caught:
        resolve_seed_context(None, "fixture-tenant", "solar", 1, source_intake_sha256="bad")
    assert (caught.value.code, caught.value.path) == ("INVALID_SEED_REQUEST", "source_intake_sha256")


@pytest.mark.parametrize("path", ["units.drawing_units", "units.wcs_to_ucs",
    "units.elevation_datum", "units.crs", "units", "schema_version",
    "source_intake_sha256", "tenant_id", "drawing_id"])
def test_seed_message_names_each_known_field(path):
    result = admission._refusal("INVALID_SEED_REQUEST", path=path)
    assert result["status_code"] == 409
    assert result["content"]["error"]["message"] == f"Solar setup was refused: {path} is not valid."
    assert result["content"]["error"]["next_action"] == "Correct the Solar setup field, then retry."


@pytest.mark.parametrize("exception,message,action", [
    (GraphValidationError("PANEL_LAYER_FILTER_REQUIRED"),
     "No panel layer filter is set (panel_layer_contains). Set it in Solar settings to the layer that holds your panels.",
     "Set panel_layer_contains in Solar settings to the layer that holds your panels."),
    (GraphValidationError("NO_PANELS_RECOGNISED"),
     "No panels matched the panel layer filter panel_layer_contains.",
     "Choose a panel_layer_contains filter in Solar settings that matches your panel layer."),
    (GraphValidationError("INVALID_SEED_REQUEST", "units.drawing_units"),
     "Solar setup was refused: units.drawing_units is not valid.",
     "Correct the Solar setup field, then retry."),
    (GraphValidationError("INVALID_SEED_REQUEST"), "Solar setup was refused.",
     "Correct the Solar setup field, then retry."),
    (GraphValidationError("UNKNOWN_UNITS", "project.units"),
     "The drawing units in project.units are not recognised.",
     "Set recognised drawing units in Solar settings, then retry."),
    (project.ProjectContextError("SIP_R1_CHECKOUT_EXPIRED"),
     "Your drawing checkout expired. Take the checkout again, then retry.",
     "Take the drawing checkout again, then retry."),
    (project.ProjectContextError("SIP_R1_CHECKOUT_REQUIRED"),
     "This change needs a drawing checkout. Take the checkout, then retry.",
     "Take the drawing checkout, then retry."),
])
@pytest.mark.parametrize("preview", [True, False])
def test_admission_messages_name_cause_and_action(monkeypatch, exception, message, action, preview):
    a = policy.setup(monkeypatch)
    def fail(*args, **kwargs):
        raise exception
    target, name = (service, "submit_project_graph_job") if preview else (
        platform_link, "submit_canonical_graph")
    monkeypatch.setattr(target, name, fail)
    # A missing graph is a supported diagnostic fallback for NO_PANELS_RECOGNISED.
    monkeypatch.setattr(project, "resolve_context", lambda *args, **kwargs: SimpleNamespace(intake={}))
    result = policy.request(a, {}, preview=preview)
    error = result["content"]["error"]
    assert result["status_code"] == 409 and result["content"]["ok"] is False
    assert error["reason_code"] == getattr(exception, "code", getattr(exception, "reason_code", None))
    assert error["message"] == message
    assert error["next_action"] == action and len(action) < 200
    assert error["retryable"] is False


@pytest.mark.parametrize("value,display", [("Roof-PV", "Roof-PV"),
    ("PV\n\0\x7f\x85\u202e" + "x" * 130, "PV" + "x" * 118)])
def test_admission_no_panels_names_filter_from_exact_parent(monkeypatch, value, display):
    a = policy.setup(monkeypatch)
    def fail(*args, **kwargs):
        raise GraphValidationError("NO_PANELS_RECOGNISED", "private-exception-marker")
    monkeypatch.setattr(service, "submit_project_graph_job", fail)
    lookups = []
    def resolve(*args, **kwargs):
        lookups.append((args, kwargs))
        return SimpleNamespace(intake={"solar_design_graph": {
            "settings": {"panel_layer_contains": value}}})
    monkeypatch.setattr(project, "resolve_context", resolve)
    result = policy.request(a, {"panel_layer_contains": "untrusted-filter"})
    assert result["status_code"] == 409
    error = result["content"]["error"]
    assert error["reason_code"] == "NO_PANELS_RECOGNISED"
    assert error["message"] == f'No panels matched the panel layer filter panel_layer_contains="{display}".'
    assert lookups == [((str(a.s.org), a.s.project, a.s.parent.version_id),
                        {"drawing_id": a.s.drawing, "write": False})]


def test_admission_seed_unknown_units_names_field(monkeypatch):
    a = policy.setup(monkeypatch)
    params = policy.r3a.prepare(a.s).request["parameters"]
    params["initialize"]["units"]["drawing_units"] = "unknown"
    result = policy.request(a, params)
    assert result["status_code"] == 409
    assert result["content"]["error"]["reason_code"] == "INVALID_SEED_REQUEST"
    assert result["content"]["error"]["message"] == "Solar setup was refused: units.drawing_units is not valid."


@pytest.mark.parametrize("panel_filter,reason,message", [
    ("", "PANEL_LAYER_FILTER_REQUIRED",
     "No panel layer filter is set (panel_layer_contains). Set it in Solar settings to the layer that holds your panels."),
    ("Roof-PV", "NO_PANELS_RECOGNISED",
     'No panels matched the panel layer filter panel_layer_contains="Roof-PV".'),
])
def test_real_panel_import_admission_names_graph_filter(monkeypatch, panel_filter, reason, message):
    a = policy.setup(monkeypatch)
    intake = json.loads(policy.r3a.prepare(a.s).output_intake_bytes)
    graph = intake["solar_design_graph"]
    graph["settings"]["panel_layer_contains"] = panel_filter
    intake["solar_design_graph_sha256"] = policy.r3a.local.digest(graph)
    a.s.set_parent(intake)
    before = deepcopy((a.s.blobs, a.s.versions, a.s.writes))
    result = policy.request(a, {"expected_rev": graph["rev"]}, tool_name="solar-panels-from-drawing")
    assert result["status_code"] == 409
    assert result["content"]["error"]["reason_code"] == reason
    assert result["content"]["error"]["message"] == message
    assert (a.s.blobs, a.s.versions, a.s.writes) == before and not a.accepted


def test_filter_diagnostic_failure_keeps_original_refusal(monkeypatch):
    a = policy.setup(monkeypatch)
    def fail(*args, **kwargs):
        raise GraphValidationError("NO_PANELS_RECOGNISED")
    def unavailable(*args, **kwargs):
        raise RuntimeError("token-verifier-secret")
    monkeypatch.setattr(service, "submit_project_graph_job", fail)
    monkeypatch.setattr(project, "resolve_context", unavailable)
    result = policy.request(a, {})
    assert result["status_code"] == 409
    assert result["content"]["error"]["reason_code"] == "NO_PANELS_RECOGNISED"
    assert result["content"]["error"]["message"] == "No panels matched the panel layer filter panel_layer_contains."
    assert "token-verifier-secret" not in str(result)


@pytest.mark.parametrize("exception,message,status", [
    (GraphValidationError("INVALID_SETTINGS_REQUEST", "token-verifier-secret"),
     "Canonical Solar request was refused.", 409),
    (GraphValidationError("INVALID_SEED_REQUEST", "token-verifier-secret"),
     "Solar setup was refused.", 409),
    (project.ProjectContextError("SIP_R1_CHECKOUT_DENIED"),
     "Project Solar admission unavailable.", 403),
])
def test_admission_unlisted_reasons_and_unknown_paths_do_not_echo_text(monkeypatch, exception, message, status):
    a = policy.setup(monkeypatch)
    def fail(*args, **kwargs):
        raise exception
    monkeypatch.setattr(service, "submit_project_graph_job", fail)
    result = policy.request(a, {})
    assert result["status_code"] == status
    assert result["content"]["error"]["message"] == message
    assert "token-verifier-secret" not in str(result)
