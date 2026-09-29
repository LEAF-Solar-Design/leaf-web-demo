"""Project setup, sizing invalidation and ZIP binding on the existing Solar rail."""
import copy
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import product_capability_availability as availability
import solar_local_graph as local
import store
from solar_design_graph import GraphValidationError, entities
from solar_graph_context import resolve_graph_context
from solar_graph_seed import new_empty_graph
from solar_project import (
    SEED_VOC_COLD, apply_project_changes, normalize_project_changes, project_validity,
)
from solar_sizing_client import require_sizing
from solar_solve_results import DERIVED_KINDS
from test_w1_design_graph import app_id, graph  # noqa: F401
from test_w1_local_graph_adapter import held, run as commit_settings
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_seed_product_path import (
    TENANT, UNITS, assert_parent, checkout, product, run as product_run,
    seed_params, upload,
)
from test_w1_sizing_groups import (
    confirm, passing, service, settings, sizing_params,
)
from test_w1_solve_commit import seed


@pytest.fixture
def empty_graph():
    return new_empty_graph(tenant_id=TENANT, drawing_id="solar", source_hash="a" * 64,
                           units=UNITS, created_at="2026-09-17T00:00:00Z")


def project_params(digest):
    params = seed_params(digest)
    del params["changes"]
    params["project_changes"] = {"name": "Roof A", "zip_code": "44224"}
    return params


def test_project_seed_becomes_valid(empty_graph):
    before = copy.deepcopy(empty_graph)
    result = settings.run(empty_graph, {"expected_rev": 0, "project_changes": {
        "name": " Roof A ", "zip_code": "44224"}})
    assert empty_graph == before
    assert before["project"]["validity"] == {"state": "unknown", "reasons": ["seed_defaults"]}
    project = result["project"]
    assert project["name"] == "Roof A" and project["zip_code"] == "44224"
    assert project["latitude"] is None and project["longitude"] is None
    assert project["validity"] == {"state": "valid", "reasons": []}
    assert result["rev"] == 1 and result["parent_rev"] == 0
    assert project["rev"] == 1


def test_project_blank_zip_keeps_sizing_unavailable(empty_graph):
    result = settings.run(empty_graph, {"expected_rev": 0, "project_changes": {
        "name": "Roof A", "zip_code": ""}})
    assert result["project"]["validity"] == {
        "state": "unknown", "reasons": ["project_zip_required"]}
    assert availability.w1_graph_readiness(result)["solar-size-strings"] == {
        "input_ready": False, "input_reason": "valid_settings_required"}


@pytest.mark.parametrize("patch,code", [
    ({"name": "  "}, "PROJECT_NAME_REQUIRED"),
    ({"zip_code": "4422"}, "INVALID_PROJECT_ZIP"),
    ({"latitude": 91, "longitude": 0}, "INVALID_PROJECT_COORDINATES"),
    ({"latitude": 0}, "INVALID_PROJECT_COORDINATES"),
    ({"latitude": None, "longitude": 5}, "INVALID_PROJECT_COORDINATES"),
    ({"latitude": True, "longitude": 0}, "INVALID_PROJECT_COORDINATES"),
    ({"city": "x"}, "INVALID_PROJECT_REQUEST"),
    ({"name": 12}, "INVALID_PROJECT_REQUEST"),
    ({"name": "\ud800"}, "INVALID_PROJECT_REQUEST"),
    ({"zip_code": "\ud800"}, "INVALID_PROJECT_REQUEST"),
    ({}, "INVALID_PROJECT_REQUEST"),
    ({"latitude": float("nan"), "longitude": 0}, "INVALID_PROJECT_COORDINATES"),
])
def test_project_refusals_leave_graph_unchanged(graph, monkeypatch, patch, code):
    before = copy.deepcopy(graph)
    advanced = []

    def refuse_advance(*args):
        advanced.append(args)
        pytest.fail("invalid project patch must not produce a commit candidate")

    monkeypatch.setattr(settings, "advance", refuse_advance)
    with pytest.raises(GraphValidationError) as error:
        settings.run(graph, {"expected_rev": 0, "project_changes": patch})
    assert error.value.code == code
    assert graph == before and advanced == []


def test_project_invalid_patch_never_publishes(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    published = []

    def refuse_publish(*args, **kwargs):
        published.append(args)
        pytest.fail("invalid project patch must not publish")

    monkeypatch.setattr(local, "publish_version", refuse_publish)
    with held(backend) as fence:
        before = copy.deepcopy(store.load_manifest(backend, TENANT, "solar"))
        with pytest.raises(GraphValidationError) as error:
            commit_settings(backend, {"expected_rev": 0, "project_changes": {"name": " "}},
                            fence=fence)
        assert error.value.code == "PROJECT_NAME_REQUIRED"
        assert store.load_manifest(backend, TENANT, "solar") == before
    assert published == []
    assert resolve_graph_context(backend, TENANT, "solar", 1)["graph"] == graph


def test_project_material_edit_invalidates_sizing_and_derived(graph):
    graph["project"].update(latitude=41.1, longitude=-81.4)
    graph["settings"]["extra"]["string_sizing"] = {"fixture": "previous sizing"}
    before = copy.deepcopy(graph)
    result = settings.run(graph, {"expected_rev": 0, "project_changes": {"zip_code": "37601"}})
    assert graph == before
    assert result["project"]["zip_code"] == "37601"
    assert result["project"]["latitude"] is None and result["project"]["longitude"] is None
    assert result["settings"]["global_string_sizing_confirmed"] is False
    assert "string_sizing" not in result["settings"]["extra"]
    targets = [result["settings"]] + result["electrical_zones"]
    assert all(target["voc_cold"] == SEED_VOC_COLD for target in targets)
    assert len({id(target["voc_cold"]) for target in targets}) == len(targets)
    old = {entity["id"]: entity for entity in entities(before)}
    for entity in entities(result):
        if entity["kind"] in DERIVED_KINDS:
            assert entity["validity"] == {"state": "stale", "reasons": ["project_changed"]}
        if entity["kind"] in {"frame", "panel", "zone-el"}:
            assert entity["validity"] == old[entity["id"]]["validity"]
        if entity != old[entity["id"]]:
            assert entity["rev"] == 1
            assert entity["provenance"]["last_writer"] == "solar-settings"
            assert entity["provenance"]["source_rev"] == 0
    assert result["frames"] == before["frames"] and result["panels"] == before["panels"]


def test_project_nonmaterial_edit_preserves_sizing_and_still_commits(graph):
    graph["settings"]["extra"]["string_sizing"] = {"fixture": "preserved"}
    before = copy.deepcopy(graph)
    result = settings.run(graph, {"expected_rev": 0, "project_changes": {
        "name": " " + graph["project"]["name"] + " ", "zip_code": "00000"}})
    assert result["rev"] == 1 and result["parent_rev"] == 0
    assert result["settings"]["global_string_sizing_confirmed"] is True
    assert result["settings"] == before["settings"]
    assert entities(result) == entities(before)
    assert graph == before


def test_project_equal_coordinates_preserve_sizing_and_stored_types(graph, passing, service):
    graph["project"].update(zip_code="44224", latitude=41, longitude=-81)
    confirmed = confirm(graph, sizing_params(graph, passing))["graph"]
    before = copy.deepcopy(confirmed)
    evidence = require_sizing(confirmed)
    result = settings.run(confirmed, {"expected_rev": confirmed["rev"], "project_changes": {
        "latitude": 41.0, "longitude": -81.0}})
    assert require_sizing(result) == evidence
    assert result["settings"]["global_string_sizing_confirmed"] is True
    assert result["settings"]["extra"]["string_sizing"] == evidence
    assert result["project"]["latitude"] == 41
    assert type(result["project"]["latitude"]) is int
    assert result["project"]["longitude"] == -81
    assert type(result["project"]["longitude"]) is int
    assert result["rev"] == before["rev"] + 1
    assert entities(result) == entities(before)
    assert confirmed == before
    assert service == [passing["request"]]


def test_project_validity_repair_invalidates_confirmed_sizing(graph, passing, service):
    graph["project"].update(zip_code="44224", validity={
        "state": "unknown", "reasons": ["imported"]})
    confirmed = confirm(graph, sizing_params(graph, passing))["graph"]
    before = copy.deepcopy(confirmed)
    assert require_sizing(confirmed) == confirmed["settings"]["extra"]["string_sizing"]
    patch = {"name": confirmed["project"]["name"]}
    assert apply_project_changes(copy.deepcopy(confirmed), patch) is True

    result = settings.run(confirmed, {"expected_rev": confirmed["rev"],
                                      "project_changes": patch})
    assert result["project"]["validity"] == {"state": "valid", "reasons": []}
    assert result["settings"]["global_string_sizing_confirmed"] is False
    assert "string_sizing" not in result["settings"]["extra"]
    assert all(target["voc_cold"] == SEED_VOC_COLD
               for target in [result["settings"]] + result["electrical_zones"])
    assert result["project"]["rev"] == result["rev"] == confirmed["rev"] + 1
    assert confirmed == before
    assert service == [passing["request"]]


def test_project_unchanged_validity_preserves_confirmed_sizing(graph, passing, service):
    graph["project"].update(zip_code="44224", validity={"state": "valid", "reasons": []})
    confirmed = confirm(graph, sizing_params(graph, passing))["graph"]
    before = copy.deepcopy(confirmed)
    evidence = require_sizing(confirmed)
    patch = {"name": confirmed["project"]["name"]}
    assert apply_project_changes(copy.deepcopy(confirmed), patch) is False

    result = settings.run(confirmed, {"expected_rev": confirmed["rev"],
                                      "project_changes": patch})
    assert require_sizing(result) == evidence
    assert result["settings"]["global_string_sizing_confirmed"] is True
    assert result["settings"]["extra"]["string_sizing"] == evidence
    assert entities(result) == entities(before)
    assert result["rev"] == confirmed["rev"] + 1
    assert confirmed == before
    assert service == [passing["request"]]


def test_project_settings_numeric_representation_change_advances_entity_rev(graph):
    initial = settings.run(graph, {"expected_rev": graph["rev"],
                                   "changes": {"optimizer_ratio": 1}})
    before = copy.deepcopy(initial)
    assert type(initial["settings"]["optimizer_ratio"]) is int
    result = settings.run(initial, {"expected_rev": initial["rev"],
                                    "changes": {"optimizer_ratio": 1.0}})
    assert result["settings"]["optimizer_ratio"] == 1.0
    assert type(result["settings"]["optimizer_ratio"]) is float
    assert result["settings"]["rev"] == result["rev"] == initial["rev"] + 1
    assert initial == before


@pytest.mark.parametrize("zip_code", ["37601", ""])
def test_project_sizing_zip_mismatch_never_calls_transport(graph, passing, service, zip_code):
    graph["project"]["zip_code"] = zip_code
    before = copy.deepcopy(graph)
    with pytest.raises(GraphValidationError) as error:
        confirm(graph, sizing_params(graph, passing))
    assert error.value.code == "SIZING_PROJECT_MISMATCH"
    assert service == [] and graph == before


def test_project_zip_plus_four_binds_to_five_digit_request(graph, passing, service):
    graph["project"]["zip_code"] = "44224-1234"
    result = confirm(graph, sizing_params(graph, passing))
    assert result["confirmed"] is True
    assert service == [passing["request"]]


def test_project_all_zone_zips_checked_before_first_call(graph, passing, service):
    graph["project"]["zip_code"] = "44224"
    other = copy.deepcopy(graph["electrical_zones"][0])
    other["id"] = app_id("zone-el", 2)
    other["panel_refs"] = [graph["electrical_zones"][0]["panel_refs"].pop()]
    graph["electrical_zones"].append(other)
    params = sizing_params(graph, passing, "zones")
    params["requests"][other["id"]]["zip_code"] = "37601"
    with pytest.raises(GraphValidationError, match="SIZING_PROJECT_MISMATCH"):
        confirm(graph, params)
    assert service == []


def test_project_sizing_cancel_precedes_zip_binding(graph, passing, service):
    graph["project"]["zip_code"] = ""
    before = copy.deepcopy(graph)
    assert confirm(graph, sizing_params(graph, passing, cancel=True))["graph"] == before
    assert graph == before and service == []


def test_project_only_initialization_through_product(product):
    client, backend, tools = product
    drawing_id, digest = upload(client)
    capability = checkout(client, drawing_id)
    response = product_run(client, tools, drawing_id, project_params(digest), capability=capability)
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    context = resolve_graph_context(backend, TENANT, drawing_id, "head")
    assert context["current_head"] == 2 and context["graph"]["rev"] == 1
    assert context["graph"]["project"]["name"] == "Roof A"
    assert context["graph"]["project"]["zip_code"] == "44224"
    assert context["graph"]["project"]["validity"] == {"state": "valid", "reasons": []}


@pytest.mark.parametrize("override,code", [
    ({"cancel": True}, "GRAPH_COMMIT_CANCELLED"),
    ({"expected_rev": 1}, "STALE_GRAPH_REVISION"),
])
def test_project_seed_cancel_or_stale_publishes_nothing(product, override, code):
    client, backend, tools = product
    drawing_id, digest = upload(client)
    capability = checkout(client, drawing_id)
    params = dict(project_params(digest), **override)
    response = product_run(client, tools, drawing_id, params, capability=capability)
    assert response.status_code >= 400, response.text
    assert response.json()["reason_code"] == code
    assert_parent(backend, drawing_id, digest)
    assert len(store.load_manifest(backend, TENANT, drawing_id)["versions"]) == 1


def test_project_readiness_flips_after_setup(empty_graph):
    assert not availability.w1_graph_readiness(empty_graph)["solar-size-strings"]["input_ready"]
    result = settings.run(empty_graph, {"expected_rev": 0, "project_changes": {
        "name": "Roof A", "zip_code": "44224"}})
    assert availability.w1_graph_readiness(result)["solar-size-strings"]["input_ready"] is True


def test_project_settings_schema_accepts_setup_and_rejects_unknown_or_empty():
    tools = json.loads((SERVER / "write_tools.json").read_text(encoding="utf-8"))["tools"]
    schema = next(tool["params"] for tool in tools if tool["name"] == "solar-settings")
    Draft7Validator.check_schema(schema)
    validator = Draft7Validator(schema)
    assert validator.is_valid({"expected_rev": 0, "project_changes": {
        "name": " Roof A ", "zip_code": "44224"}})
    for patch in ({"city": "x"}, {}):
        assert not validator.is_valid({"expected_rev": 0, "project_changes": patch})


def test_project_and_settings_commit_in_one_revision(graph):
    result = settings.run(graph, {"expected_rev": 0, "changes": {"num_mppt": 2},
                                 "project_changes": {"name": "Roof B", "zip_code": "44224"}})
    assert result["settings"]["num_mppt"] == 2
    assert result["project"]["name"] == "Roof B"
    assert result["project"]["validity"] == {"state": "valid", "reasons": []}
    assert result["rev"] == result["settings"]["rev"] == result["project"]["rev"] == 1
    assert result["parent_rev"] == 0


def test_project_cold_reset_matches_seed(empty_graph):
    assert SEED_VOC_COLD == empty_graph["settings"]["voc_cold"]


def test_project_predicate_collects_reasons_in_order():
    project = {"name": " ", "zip_code": "bad", "latitude": None, "longitude": 5}
    before = copy.deepcopy(project)
    assert project_validity(project) == {"state": "unknown", "reasons": [
        "project_name_required", "invalid_project_zip", "invalid_project_coordinates",
        "project_units_required"]}
    assert project == before
    project["zip_code"] = " "
    assert project_validity(project)["reasons"][1] == "project_zip_required"


@pytest.mark.parametrize("patch", [
    None, [], {"name": "x" * 4097}, {"zip_code": "1" * 11}, {"zip_code": 44224},
])
def test_project_patch_shape_and_lengths_are_bounded(patch):
    with pytest.raises(GraphValidationError, match="INVALID_PROJECT_REQUEST"):
        normalize_project_changes(patch)


@pytest.mark.parametrize("latitude,longitude", [(90, 180), (-90, -180), (None, None)])
def test_project_explicit_coordinate_pair_is_preserved(graph, latitude, longitude):
    patch = {"zip_code": "37601", "latitude": latitude, "longitude": longitude}
    if latitude is None:
        graph["project"].update(latitude=41.1, longitude=-81.4)
        patch["zip_code"] = graph["project"]["zip_code"]
    before = copy.deepcopy(patch)
    assert apply_project_changes(graph, patch) is True
    assert patch == before
    assert graph["project"]["latitude"] == latitude
    assert graph["project"]["longitude"] == longitude
    assert graph["project"]["validity"] == {"state": "valid", "reasons": []}
    assert apply_project_changes(graph, patch) is False


def test_project_name_only_edit_keeps_coordinates_and_existing_stale_reason(graph):
    graph["project"].update(latitude=41.1, longitude=-81.4)
    graph["strings"][0]["validity"] = {"state": "stale", "reasons": ["earlier_change"]}
    result = settings.run(graph, {"expected_rev": 0, "project_changes": {"name": "Roof B"}})
    assert result["project"]["name"] == "Roof B"
    assert result["project"]["latitude"] == 41.1 and result["project"]["longitude"] == -81.4
    assert result["strings"][0] == graph["strings"][0]
    assert result["settings"]["global_string_sizing_confirmed"] is False


@pytest.mark.parametrize("extra", [{}, {"changes": {}}, {"changes": {"city": "x"}}])
def test_project_settings_still_requires_valid_change_sets(graph, extra):
    params = {"expected_rev": 0, **extra}
    if extra:
        params["project_changes"] = {"name": "Roof A"}
    with pytest.raises(GraphValidationError, match="INVALID_SETTINGS_REQUEST"):
        settings.run(graph, params)
