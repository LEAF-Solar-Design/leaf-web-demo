"""Confirmed module wattage flows atomically into locally grouped frames."""
import copy
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import solar_sizing_client as cloud
from product_capability_availability import w1_graph_readiness
from solar_design_graph import GraphValidationError
from solar_sizing_power import record_module_power, sizing_power_ready
from solar_solve_results import upstream_basis
from test_w1_design_graph import app_id, graph  # noqa: F401
from test_w1_equipment import case  # noqa: F401
from test_w1_sizing_groups import (  # noqa: F401
    confirm, groups, no_network, passing, service, sizing_params,
)


@pytest.fixture
def source(graph, passing):
    graph["project"]["zip_code"] = passing["request"]["zip_code"]
    graph.update(frames=[], strings=[], inverters=[], routes=[], schedules=[])
    for n, panel in enumerate(graph["panels"], 1):
        panel.update(frame_ref=None, matrix_cell=None, angle=0, centre=[n, 0],
                     assignment={"string_ref": None, "seq": None})
        panel["provenance"]["source_handle"] = f"A{n}"
    return graph


def group_request(graph, memberships=None):
    memberships = memberships or [[panel["id"] for panel in graph["panels"]]]
    return {"expected_rev": graph["rev"], "groups": [
        {"name": f"Group {n}", "panel_refs": refs, "alignment_tolerance": 0.5,
         "module_width_along_row": 1, "module_height_across_row": 2}
        for n, refs in enumerate(memberships, 1)]}


def two_zones(source):
    for n in (4, 5):
        panel = copy.deepcopy(source["panels"][0])
        panel.update(id=app_id("panel", n), centre=[n, 0])
        panel["provenance"]["source_handle"] = f"A{n}"
        source["panels"].append(panel)
    first = source["electrical_zones"][0]
    first.update(module_model="module-A", panel_refs=[p["id"] for p in source["panels"][:2]])
    second = copy.deepcopy(first)
    second.update(id=app_id("zone-el", 2), name="Zone B", module_model="module-B",
                  panel_refs=[p["id"] for p in source["panels"][2:]])
    source["electrical_zones"].append(second)
    return source


def zone_service(monkeypatch, passing, fail_second=False):
    calls = []

    def post(request, grant):
        calls.append(request.module_name)
        if fail_second and request.module_name == "module-B":
            raise cloud.CloudError("cloud_upstream_failure", 502)
        response = copy.deepcopy(passing["response"])
        response["pmp"] = {"module-A": 400, "module-B": 550}[request.module_name]
        return cloud.canonical_bytes(response)

    monkeypatch.setattr(cloud, "post_string_length", post)
    return calls


def test_power_b1_global_group(source, passing, service):
    sized = confirm(source, sizing_params(source, passing))["graph"]
    result = groups.run(sized, group_request(sized))
    assert len(result["frames"]) == 1
    assert len(result["frames"][0]["panel_refs"]) == 3
    assert result["frames"][0]["module_power_watts"] == 595.0
    assert sizing_power_ready(result)


def test_power_b2_zone_groups(source, passing, service, monkeypatch):
    source = two_zones(source)
    calls = zone_service(monkeypatch, passing)
    sized = confirm(source, sizing_params(source, passing, "zones"))["graph"]
    result = groups.run(sized, group_request(
        sized, [zone["panel_refs"] for zone in sized["electrical_zones"]]))
    assert calls == ["module-A", "module-B"]
    assert [len(frame["panel_refs"]) for frame in result["frames"]] == [2, 3]
    assert [frame["module_power_watts"] for frame in result["frames"]] == [400, 550]


def test_power_b3_cross_zone_refuses(source, passing, service, monkeypatch):
    source = two_zones(source)
    zone_service(monkeypatch, passing)
    sized = confirm(source, sizing_params(source, passing, "zones"))["graph"]
    before = copy.deepcopy(sized)
    with pytest.raises(GraphValidationError, match="INVALID_ZONE_COVERAGE"):
        groups.run(sized, group_request(sized))
    assert sized == before


def test_power_global_to_zones_refreshes_existing_frames(source, passing, service, monkeypatch):
    source = two_zones(source)
    sized = confirm(source, sizing_params(source, passing))["graph"]
    refs = [panel["id"] for panel in sized["panels"]]
    # Exercise the original all-five frame, then a spanning and a contained frame
    # together without assigning any panel to two frames.
    for memberships in ([refs], [refs[:3], refs[3:]]):
        grouped = groups.run(sized, group_request(sized, memberships))
        assert grouped["frames"][0]["electrical_zone_ref"] is None
        assert all(frame["module_power_watts"] == 595.0 for frame in grouped["frames"])
        before = copy.deepcopy(grouped)
        zone_service(monkeypatch, passing)
        response = confirm(grouped, sizing_params(grouped, passing, "zones"))
        after = response["graph"]
        assert response["confirmed"] is True
        assert after["rev"] == before["rev"] + 1
        assert [frame["module_power_watts"] for frame in after["frames"]] == (
            [0.0] if len(memberships) == 1 else [0.0, 550])
        for old, new in zip(before["frames"], after["frames"]):
            assert new["rev"] == after["rev"]
            for key in old.keys() - {"module_power_watts", "rev", "provenance"}:
                assert new[key] == old[key]
        assert grouped == before


def test_power_b4_nonpositive_confirmation_is_atomic(source, passing, service):
    sized = confirm(source, sizing_params(source, passing))["graph"]
    grouped = groups.run(sized, group_request(sized))
    before = copy.deepcopy(grouped)
    for power in (0, -1):
        passing["response"]["pmp"] = power
        with pytest.raises(GraphValidationError, match="CLOUD_RESPONSE_INVALID"):
            confirm(grouped, sizing_params(grouped, passing))
        assert grouped == before
        assert grouped["frames"] == before["frames"]


def test_power_b5_resizing_refreshes_existing_frame(source, passing, service):
    sized = confirm(source, sizing_params(source, passing))["graph"]
    before = groups.run(sized, group_request(sized))
    original = copy.deepcopy(before)
    passing["response"]["pmp"] = 600
    after = confirm(before, sizing_params(before, passing))["graph"]
    old, new = before["frames"][0], after["frames"][0]
    assert old["module_power_watts"] == 595.0
    assert new["module_power_watts"] == 600
    assert new["rev"] == after["rev"] == before["rev"] + 1
    for key in old.keys() - {"module_power_watts", "rev", "provenance"}:
        assert new[key] == old[key]
    assert upstream_basis(after) != upstream_basis(before)
    assert before == original


def test_power_b6_fractional_wattage(source, passing, service):
    passing["response"]["pmp"] = 595.5
    sized = confirm(source, sizing_params(source, passing))["graph"]
    result = groups.run(sized, group_request(sized))
    assert result["frames"][0]["module_power_watts"] == 595.5
    record = sized["settings"]["extra"]["string_sizing"]["records"][sized["settings"]["id"]]
    assert record_module_power(record) == 595.5


def test_power_b7_second_zone_failure_is_atomic(source, passing, service, monkeypatch):
    source = two_zones(source)
    zone_service(monkeypatch, passing)
    sized = confirm(source, sizing_params(source, passing, "zones"))["graph"]
    grouped = groups.run(sized, group_request(
        sized, [zone["panel_refs"] for zone in sized["electrical_zones"]]))
    before = copy.deepcopy(grouped)
    calls = zone_service(monkeypatch, passing, fail_second=True)
    with pytest.raises(cloud.CloudError, match="cloud_upstream_failure"):
        confirm(grouped, sizing_params(grouped, passing, "zones"))
    assert calls == ["module-A", "module-B"]
    assert grouped == before
    assert grouped["frames"] == before["frames"]


def test_power_b8_legacy_evidence_refuses(case):
    source, _, _ = case
    source["frames"] = []
    for n, panel in enumerate(source["panels"], 1):
        panel.update(frame_ref=None, matrix_cell=None)
        panel["provenance"]["source_handle"] = f"A{n}"
    cloud.require_sizing(source)
    before = copy.deepcopy(source)
    with pytest.raises(GraphValidationError, match="MODULE_POWER_REQUIRED"):
        groups.run(source, group_request(source))
    assert source == before
    assert w1_graph_readiness(source)["solar-panel-groups"] == {
        "input_ready": False, "input_reason": "module_power_required"}


def test_power_b9_invalid_records(passing):
    for power in (float("nan"), float("inf"), True, "595", 1_000_001):
        record = {"adapter_version": cloud.ADAPTER_VERSION,
                  "response": dict(copy.deepcopy(passing["response"]), pmp=power)}
        with pytest.raises(GraphValidationError, match="MODULE_POWER_REQUIRED"):
            record_module_power(record)
    with pytest.raises(GraphValidationError, match="MODULE_POWER_REQUIRED"):
        record_module_power({"adapter_version": "1.0.0", "response": passing["response"]})
