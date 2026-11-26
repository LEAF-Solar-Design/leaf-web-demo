"""Saved engineering comparison over verified version and graph reads."""
from copy import deepcopy
from uuid import uuid4

import pytest

from solar_design_graph import validate_graph
from solar_saved_engineering_comparison import compare
from test_project_solaredge import accept, lane as tracking_lane, report, samples
from test_sip_r3b_tools import INTAKE, embedded
from test_sip_r6_read import lane, refused
from test_solar_ground_graph_codec import b18, ground_graph
from test_solar_ground_topology import topology_of
from test_w1_design_graph import graph


def _save(lane, intake):
    lane.parent = deepcopy(lane.parent)
    lane.parent.version_id = uuid4()
    lane.head = lane.parent.version_id
    lane.set_parent(intake)
    return lane.head


def _save_graph(lane, graph):
    return _save(lane, embedded(validate_graph(graph)))


def _compare(lane, before, after):
    writes = list(lane.writes)
    head = lane.head
    result = compare(lane.tenant, lane.project, lane.drawing, before, after)
    assert lane.writes == writes
    assert lane.head == head
    assert result["before"]["input_version_id"] == str(before)
    assert result["after"]["input_version_id"] == str(after)
    return result, {entry["metric"]: entry for entry in result["metrics"]}


def _amounts(entry):
    return entry["before"], entry["after"], entry["delta"]


def test_exact_roof_versions_preserve_fractional_power_and_signed_delta(lane, graph):
    graph = topology_of(graph)
    graph["frames"][0]["module_power_watts"] = 400.125
    before = _save_graph(lane, graph)
    graph["frames"][0]["module_power_watts"] = 500.25
    after = _save_graph(lane, graph)

    result, totals = _compare(lane, before, after)
    assert result["before"]["graph_sha256"] != result["after"]["graph_sha256"]
    assert result["before"]["intake_sha256"] != result["after"]["intake_sha256"]
    assert list(totals) == ["panels", "optimizers", "inverters", "strings", "kw_dc"]
    assert _amounts(totals["kw_dc"]) == ("1.200375", "1.50075", "0.300375")
    assert _amounts(totals["panels"]) == ("3", "3", "0")
    assert _amounts(totals["strings"]) == ("2", "2", "0")
    assert _amounts(totals["inverters"]) == ("1", "1", "0")
    assert _amounts(totals["optimizers"]) == (None, None, None)
    _, backwards = _compare(lane, after, before)
    assert backwards["kw_dc"]["delta"] == "-0.300375"
    _, same = _compare(lane, after, after)
    assert same["kw_dc"]["delta"] == "0"


def test_counts_compact_ground_modules_without_counting_an_absent_device(lane, graph, b18):
    before = _save_graph(lane, ground_graph(graph, b18, 1))
    after = _save_graph(lane, ground_graph(graph, b18, 2))
    _, totals = _compare(lane, before, after)
    assert _amounts(totals["panels"]) == ("294", "588", "294")
    assert _amounts(totals["kw_dc"]) == ("117.6", "235.2", "117.6")
    assert _amounts(totals["inverters"]) == ("0", "0", "0")
    assert _amounts(totals["strings"]) == ("0", "0", "0")


def test_missing_graph_and_legacy_equipment_keep_each_metric_unavailable(lane, graph):
    before = _save(lane, deepcopy(INTAKE))
    graph["frames"][0]["module_power_watts"] = 0
    after = _save_graph(lane, graph)
    result, totals = _compare(lane, before, after)
    assert result["before"]["graph_sha256"] is None
    assert all(entry["before_reason"] == "saved_graph_unavailable" for entry in totals.values())
    assert _amounts(totals["panels"]) == (None, "3", None)
    assert totals["inverters"]["after_reason"] == "saved_equipment_type_unavailable"
    assert totals["kw_dc"]["after_reason"] == "complete_module_power_unavailable"


def test_uses_only_revision_bound_measured_optimizers(tracking_lane, monkeypatch):
    lane = tracking_lane
    matrices = deepcopy(samples.GRIDS)
    marker = 0
    for matrix in matrices:
        matrix["Optimizers"] = []
        for row, record in enumerate(matrix["Rows"]):
            for column, cell in enumerate(record["Panels"]):
                if cell["Code"] == 1:
                    marker += 1
                    matrix["Optimizers"].append({
                        "id": f"pdf-optimizer-{marker}", "vendor_label": str(marker),
                        "cells": [[row, column]],
                    })
    monkeypatch.setattr("solar_project_solaredge.reports.parse_source", lambda data: deepcopy(matrices))
    _, _, accepted = accept(lane, report(lane))

    _, totals = _compare(lane, lane.ctx.parent_version_id, accepted.parent_version_id)
    assert _amounts(totals["optimizers"]) == (None, "6", None)
    assert totals["optimizers"]["before_reason"]
    assert totals["optimizers"]["after_reason"] is None


@pytest.mark.parametrize("missing_side", ["before", "after"])
def test_missing_version_refuses_whole_comparison(lane, missing_side):
    versions = dict.fromkeys(("before", "after"), lane.head)
    versions[missing_side] = uuid4()
    refused("SIP_R1_CONTEXT_NOT_FOUND", lambda: compare(
        lane.tenant, lane.project, lane.drawing, versions["before"], versions["after"]))
