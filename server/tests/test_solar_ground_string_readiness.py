"""String creation is ready on a converted Ground design.

A converted Ground frame stores its panels as one compact slot block (codec
leaf.solar-ground-slots.v1) and the graph's `panels` list stays empty. solar-string-add and
solar-string-multi-add run on those slot panels, so their catalog readiness must not ask for panel
rows. Both tools declare a readiness hook (builtins/solar_string_add.py input_readiness); the shared
`panels` facet is unchanged for the one other tool that declares it, whose import reads panel rows.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import product_capability_availability as availability  # noqa: E402
import solar_tools  # noqa: E402
from solar_design_graph import GraphValidationError  # noqa: E402
from test_solar_ground_admission import _run  # noqa: E402  (runs a builtin on a copy, unchanged)
from test_solar_ground_equipment import (  # noqa: E402,F401  (fixtures and the converted chain)
    converted, equip, graph, pinned, service, sized, slots, strung,
)

ADD = "solar-string-add"
MULTI = "solar-string-multi-add"
ACCEPT = "solar-solaredge-accept"
TOOLS = (ADD, MULTI)
READY = {"input_ready": True, "input_reason": None}
NO_PANELS = {"input_ready": False, "input_reason": "panels_required"}


def hook(name):
    declaration = solar_tools.get(name)
    return availability._load_readiness_builtin(declaration["builtin"]).input_readiness


def test_both_string_creation_tools_declare_a_readiness_hook():
    for name in TOOLS:
        assert solar_tools.get(name)["readiness"] == {"kind": "hook"}
    # The shared facet stays where its reader needs panel rows.
    assert solar_tools.get(ACCEPT)["readiness"] == {"kind": "facets", "facets": ["frames", "panels"]}


def test_converted_ground_design_is_ready_for_string_creation(graph, service, pinned):
    base = sized(converted(graph))
    assert base["panels"] == [] and all("ground_slots" in frame for frame in base["frames"])
    for name in TOOLS:
        assert availability.w1_local_commit_inputs(base)[name] == READY
        assert availability.w1_graph_readiness(base)[name] == READY
    # Still ready once strings and equipment exist: a drafter can add another string.
    equipped = equip(strung(copy.deepcopy(base), pinned))
    assert equipped["strings"] and equipped["inverters"]
    for name in TOOLS:
        assert availability.w1_local_commit_inputs(equipped)[name] == READY
        assert availability.w1_graph_readiness(equipped)[name] == READY


def test_converted_before_sizing_is_ready_too(graph, service):
    base = converted(graph)
    for name in TOOLS:
        assert availability.w1_local_commit_inputs(base)[name] == READY


def test_rooftop_readiness_is_unchanged(graph):
    assert graph["panels"] and not any("ground_slots" in frame for frame in graph["frames"])
    for name in TOOLS:
        assert availability.w1_local_commit_inputs(graph)[name] == READY
        # No panel rows and no slot block: the same reason as before.
        assert availability.w1_local_commit_inputs(dict(graph, panels=[]))[name] == NO_PANELS
        assert availability.w1_local_commit_inputs(dict(graph, panels=[], frames=[]))[name] == NO_PANELS


def test_unresolved_units_answer_before_the_hook(graph, service):
    base = converted(graph)
    broken = copy.deepcopy(base)
    broken["project"]["units"]["meters_per_unit"] = 2.0
    for name in TOOLS:
        assert availability.w1_local_commit_inputs(broken)[name] == {
            "input_ready": False, "input_reason": "unresolved_units"}


def test_the_hook_reads_frame_keys_only(graph, service):
    base = converted(graph)
    # A block is never decoded by readiness: an undecodable block still reads as panels present.
    # The run decodes it and refuses; a stored graph was validated before it was published.
    broken = copy.deepcopy(base)
    for frame in broken["frames"]:
        frame["ground_slots"] = {"codec": "not-decoded"}
    for name in TOOLS:
        assert hook(name)(broken) == READY
    # One frame with a block is enough; frames without one do not count.
    mixed = copy.deepcopy(base)
    for frame in mixed["frames"][1:]:
        del frame["ground_slots"]
    stripped = copy.deepcopy(base)
    for frame in stripped["frames"]:
        del frame["ground_slots"]
    for name in TOOLS:
        assert hook(name)(mixed) == READY
        assert hook(name)(stripped) == NO_PANELS


def test_multi_add_uses_the_single_add_rule(graph, service):
    base = converted(graph)
    cases = [graph, dict(graph, panels=[]), dict(graph, panels=[], frames=[]), base]
    for case in cases:
        assert hook(MULTI)(case) == hook(ADD)(case)
    # The hook never changes the graph it reads.
    before = copy.deepcopy(base)
    hook(ADD)(base)
    hook(MULTI)(base)
    assert base == before


def test_the_panels_facet_still_refuses_a_compact_design_for_the_solaredge_accept(graph, service):
    base = converted(graph)
    assert availability.w1_local_commit_inputs(base)[ACCEPT] == NO_PANELS
    assert availability.w1_local_commit_inputs(graph)[ACCEPT] == READY


@pytest.mark.parametrize("name, request_for, added", [
    (ADD, lambda panels: {"operation": "add-string", "expected_rev": 2,
                          "ordered_panel_refs": panels[:3]}, 1),
    (MULTI, lambda panels: {"operation": "add-strings", "expected_rev": 2, "string_length": 3,
                            "ordered_panel_refs": panels}, 2),
])
def test_readiness_and_the_run_agree_on_the_converted_chain(
        name, request_for, added, graph, service, monkeypatch):
    # Each tool's own builtin runs on the graph its readiness called ready.
    base = sized(converted(graph))
    assert availability.w1_local_commit_inputs(base)[name] == READY
    out = _run(name, base, request_for(slots(base)), monkeypatch)
    assert out["rev"] == base["rev"] + 1
    assert len(out["strings"]) == len(base["strings"]) + added


def test_readiness_is_an_existence_probe_not_a_run_forecast(graph, service, pinned, monkeypatch):
    # Readiness asks whether the design holds panels to string, as the panels facet always did on a
    # Rooftop design. It does not forecast a run: on a fully strung design both tools stay ready and a
    # request naming an assigned slot is refused by the run, with its own code.
    strung_graph = strung(sized(converted(graph)), pinned)
    panels = slots(strung_graph)
    for name in TOOLS:
        assert availability.w1_local_commit_inputs(strung_graph)[name] == READY
    with pytest.raises(GraphValidationError) as refused:
        _run(ADD, strung_graph, {"operation": "add-string", "expected_rev": strung_graph["rev"],
                                 "ordered_panel_refs": panels[:1]}, monkeypatch)
    assert refused.value.code == "PANEL_ALREADY_ASSIGNED"
