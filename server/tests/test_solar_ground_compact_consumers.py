"""Compact Ground frames reach the solve-results consumers: coverage, sync_assignments, correction,
export currency and settings, each equal to the same call on the graph's expansion."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))
from test_w1_design_graph import app_id, entity, graph  # noqa: E402,F401
from test_solar_ground_graph_codec import b18, canon, canon_sha, ground_graph, slot_ids, strung  # noqa: E402,F401
import solar_design_graph as sdg  # noqa: E402
import solar_ground_graph_codec as codec  # noqa: E402
import solar_solve_results as ssr  # noqa: E402

MISSING = "leaf:panel:00000000-0000-4000-8000-0000000000aa"
W1_SHA = "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3"
EMPTY = {"duplicate_panel_refs": [], "unassigned_panel_refs": []}


def _builtin(name):
    spec = importlib.util.spec_from_file_location(name, SERVER / "builtins" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


settings = _builtin("solar_settings")
correct = _builtin("solar_correct_string")


def lsha(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")).hexdigest()


def _code(fn, *args, **kwargs):
    sdg._reset_validation_caches()
    with pytest.raises(sdg.GraphValidationError) as error:
        fn(*args, **kwargs)
    return error.value.code


def _never_expand(*args, **kwargs):
    raise AssertionError("a compact consumer must not expand")


def routed(w1, site):
    """strung plus one start homerun (S1 to the inverter) and one schedule over S1..S3."""
    g = strung(w1, site)
    refs = [app_id("string", n) for n in (1, 2, 3)]
    g["routes"] = [entity("route", 1, route_kind="start homerun", points=[[0, 0], [5, 0]], from_ref=refs[0],
                          to_ref=app_id("inverter", 1), wire_gauge="10 AWG", length_ft=16.4042,
                          point_units="m", length_units="ft")]
    g["schedules"] = [entity("schedule", 1, rows=[["S1", 3], ["S2", 4], ["S3", 2]],
                             headers=["Circuit", "Modules"], insertion_point=[10, 10], layer="LEAF-SCHEDULES",
                             source_rev=0, source_refs=refs, column_units=[None, "count"])]
    return g


def full_tracker(w1, site):
    """One compact b18 tracker (294 slots) carrying fourteen 21-slot strings, no inverter."""
    g = ground_graph(w1, site, 1)
    ids = slot_ids(g, 0, 294)
    for n in range(1, 15):
        refs = ids[21 * (n - 1):21 * n]
        g["strings"].append(entity(
            "string", n, circuit_tag=f"S{n}", circuit_kind="String", ordered_panel_refs=list(refs),
            module_count=21, from_ref=refs[0], to_ref=refs[-1], tag_text_ref=None, wire_gauge="10 AWG",
            length_ft=10, route=[[0, 0], [1, 0]], inverter_ref=None))
    return g


def test_ground_compact_consumers_plain_graph_never_imports_the_codec(graph, monkeypatch):
    w1 = deepcopy(graph)
    monkeypatch.setitem(sys.modules, "solar_ground_graph_codec", None)
    assert ssr.slot_panel_ids(w1) == ()
    assert ssr.coverage(w1) == EMPTY
    synced = deepcopy(w1)
    assert ssr.sync_assignments(synced) == EMPTY
    assert canon_sha(synced) == canon_sha(w1) == W1_SHA


def test_ground_compact_consumers_slot_ids_follow_frame_and_slot_order(graph, b18):
    s = strung(graph, b18)
    ids = ssr.slot_panel_ids(s)
    assert type(ids) is tuple and len(ids) == 588
    assert ids == tuple(slot_ids(s, 0, 294) + slot_ids(s, 1, 294))
    assert lsha(list(ids)) == "78d6e26c5c939a80d3a0dfa25adc5414c79e9615f2ae21d0aad6446680cc7ec0"


@pytest.mark.parametrize("with_strings,unassigned,digest", [
    (False, 588, "397ec20125659c60078f3ccfac984bcb6b1279484d6c53936beaecdb0804c783"),
    (True, 579, "9f1189f2fc2ca0094646458bbd5ccf4ba09a2827afe6b01b956062e921bb1d16"),
], ids=["unstrung", "strung"])
def test_ground_compact_consumers_coverage_matches_expansion(graph, b18, with_strings, unassigned, digest):
    g = strung(graph, b18) if with_strings else ground_graph(graph, b18, 2)
    report = ssr.coverage(g)
    assert report == ssr.coverage(codec.expand_graph(g))
    assert report["duplicate_panel_refs"] == []
    assert len(report["unassigned_panel_refs"]) == unassigned
    assert lsha(report) == digest


def test_ground_compact_consumers_sync_is_a_no_op_on_a_synced_compact_graph(graph, b18):
    s = strung(graph, b18)
    synced = deepcopy(s)
    assert ssr.sync_assignments(synced) == ssr.coverage(s)
    assert canon(synced) == canon(s)


def test_ground_compact_consumers_sync_matches_expansion(graph, b18):
    s = strung(graph, b18)
    b = slot_ids(s, 1, 294)
    moved = deepcopy(s)
    moved["strings"][2]["ordered_panel_refs"] = b[10:13]
    report = ssr.sync_assignments(moved)
    assert moved["strings"][2]["module_count"] == 3
    assert len(report["unassigned_panel_refs"]) == 578
    assert canon_sha(moved) == "56d3c48dd99ce4842a00edb61b50be06c7f58ecd1a943e55897c3a039e111999"
    reference = deepcopy(s)
    reference["strings"][2].update(ordered_panel_refs=b[10:13], module_count=3)
    expanded = codec.expand_graph(reference)
    ssr.sync_assignments(expanded)
    assert canon_sha(expanded) == "2505bc7312b417f27c6eb821eb190894105caccf2e2bf0efc66e89205659274c"
    assert canon(codec.expand_graph(moved)) == canon(expanded)


@pytest.mark.parametrize("name,code", [
    ("missing-panel", "MISSING_PANEL"),
    ("duplicate-membership", "DUPLICATE_PANEL_MEMBERSHIP"),
    ("non-base64-row", "INVALID_GROUND_SLOTS"),
], ids=["missing-panel", "duplicate-membership", "non-base64-row"])
def test_ground_compact_consumers_sync_refusals(graph, b18, name, code):
    g = deepcopy(strung(graph, b18))
    if name == "missing-panel":
        g["strings"][2]["ordered_panel_refs"][0] = MISSING
    elif name == "duplicate-membership":
        g["strings"][2]["ordered_panel_refs"][0] = slot_ids(g, 0, 294)[0]
    else:
        block = g["frames"][0]["ground_slots"]
        block["panel_ids"] = "!" + block["panel_ids"][1:]
        assert _code(ssr.coverage, deepcopy(g)) == code
    assert _code(ssr.sync_assignments, g) == code


@pytest.mark.parametrize("name,expected", [
    ("strung", "SOLAR_OUTPUT_NOT_CURRENT"),
    ("full-tracker", None),
    ("full-tracker-minus-one-slot", "SOLAR_OUTPUT_NOT_CURRENT"),
], ids=["strung", "full-tracker", "full-tracker-minus-one-slot"])
def test_ground_compact_consumers_export_currency(graph, b18, name, expected):
    if name == "strung":
        g = strung(graph, b18)
    else:
        g = full_tracker(graph, b18)
        assert canon_sha(g) == "01d7085d35a383890def1def34eaeabd11081cc1dc9098d896d5d8cde1c3c98d"
        if name == "full-tracker-minus-one-slot":
            last = g["strings"][13]
            last["ordered_panel_refs"] = last["ordered_panel_refs"][:20]
            last.update(module_count=20, to_ref=last["ordered_panel_refs"][-1])
            assert ssr.coverage(g) == {"duplicate_panel_refs": [],
                                       "unassigned_panel_refs": [slot_ids(g, 0, 294)[293]]}
        else:
            assert ssr.coverage(g) == EMPTY
    for candidate in (g, codec.expand_graph(g)):
        if expected is None:
            sdg._reset_validation_caches()
            assert ssr.require_current_export(candidate) == candidate
        else:
            assert _code(ssr.require_current_export, candidate) == expected


def _membership(refs):
    return {"expected_rev": 0, "memberships": [{"string_ref": app_id("string", 3), "ordered_panel_refs": refs}]}


def test_ground_compact_consumers_correction_strings_slot_panels(graph, b18):
    s = strung(graph, b18)
    before = deepcopy(s)
    params = _membership(slot_ids(s, 1, 294)[10:13])
    sdg._reset_validation_caches()
    result = correct.run(s, deepcopy(params))
    assert s == before
    assert result["rev"] == 1
    assert [x["module_count"] for x in result["strings"]] == [3, 4, 3]
    assert [x["validity"] for x in result["strings"]] == [
        {"state": "valid", "reasons": []}, {"state": "valid", "reasons": []},
        {"state": "stale", "reasons": ["upstream_corrected"]}]
    assert [x["validity"] for x in result["inverters"]] == [{"state": "valid", "reasons": []}]
    assert result["extra"]["solve_coverage"] == ssr.coverage(result)
    assert len(result["extra"]["solve_coverage"]["unassigned_panel_refs"]) == 578
    sdg._reset_validation_caches()
    assert sdg.validate_graph(result) == result
    assert lsha(result["strings"]) == "1376022bb9b95ccca7b4efb7368e1f47307ae13e411888884a3e9d3edb8ac7cb"
    assert canon_sha(result) == "0042d57aa066a04d67d11bd441504f4f1ea981eb6ec9f5ee83beb611aa73a30f"
    sdg._reset_validation_caches()
    reference = correct.run(codec.expand_graph(s), deepcopy(params))
    for key in ("strings", "inverters", "settings", "project"):
        assert result[key] == reference[key]
    assert result["extra"]["solve_coverage"] == reference["extra"]["solve_coverage"]
    # Slot views are derived, not stored: the compact frame and its slots keep their revs.
    expanded = codec.expand_graph(result)
    assert sorted(key for key in reference if reference[key] != expanded[key]) == ["frames", "panels"]
    assert [f["rev"] for f in result["frames"]] == [0, 0]
    assert [f["rev"] for f in reference["frames"]] == [0, 1]


@pytest.mark.parametrize("name,code", [
    ("missing-panel", "MISSING_PANEL"),
    ("duplicate-membership", "DUPLICATE_PANEL_MEMBERSHIP"),
], ids=["missing-panel", "duplicate-membership"])
def test_ground_compact_consumers_correction_refusals_leave_the_graph(graph, b18, name, code):
    s = strung(graph, b18)
    before = deepcopy(s)
    ref = MISSING if name == "missing-panel" else slot_ids(s, 0, 294)[0]
    assert _code(correct.run, s, _membership([ref])) == code
    assert s == before


@pytest.mark.parametrize("params,digest,stale,reasons", [
    ({"expected_rev": 0, "changes": {"string_layer": "LEAF-STRINGS-2"}},
     "8f017690fcdc9187ecca4f69b445195a2c5fc5c8731dc41b94833aedd74f89e6",
     [app_id("route", 1), app_id("schedule", 1)], ["settings_changed"]),
    ({"expected_rev": 0, "changes": {"num_mppt": 3}},
     "f7c7722039b34643aa75ecfedcdc88ed82a8e053b62a47d53d6641a7632b521f",
     [app_id("route", 1), app_id("schedule", 1)], ["settings_changed"]),
    ({"expected_rev": 0, "changes": {"string_number": 7}},
     "c3b53caa95ee5116fb5c18528fa1c305bc4d3b2f8ff2c4ae7279c793fd136187", [], []),
    ({"expected_rev": 0, "project_changes": {"name": "Renamed site"}},
     "08fe686b1fd2fa632abe2006801da01e11ebc797f94bcb80780abc1d0a57be4a",
     sorted([app_id("inverter", 1), app_id("route", 1), app_id("schedule", 1),
             app_id("string", 1), app_id("string", 2), app_id("string", 3)]), ["project_changed"]),
], ids=["string-layer", "num-mppt", "string-number", "project-name"])
def test_ground_compact_consumers_settings_match_expansion(graph, b18, params, digest, stale, reasons):
    g = routed(graph, b18)
    assert canon_sha(g) == "add428f2ffb632062515860bf061b7d9eb64a52e07ebd5ac729d2d2ce2ce50df"
    sdg._reset_validation_caches()
    result = settings.run(deepcopy(g), deepcopy(params))
    sdg._reset_validation_caches()
    reference = settings.run(codec.expand_graph(g), deepcopy(params))
    assert canon(codec.expand_graph(result)) == canon(reference)
    assert canon_sha(result) == digest
    kinds = ("strings", "inverters", "routes", "schedules", "frames")
    assert sorted(x["id"] for k in kinds for x in result[k] if x["validity"]["state"] == "stale") == stale
    assert sorted({r for k in kinds for x in result[k] for r in x["validity"]["reasons"]}) == reasons
    assert [f["ground_slots"]["panel"]["validity"] for f in result["frames"]] == [
        {"state": "valid", "reasons": []}] * 2


def test_ground_compact_consumers_solve_binding_refuses_a_compact_frame(graph, b18):
    s = strung(graph, b18)
    table = codec.decode_slots(s["frames"][0]["ground_slots"])
    request = json.loads((SERVER / "tests/fixtures/w1_stringer_request_4x6.json").read_text(encoding="utf-8"))
    request["grid"]["Rows"] = [{"Panels": [
        {"Code": 1, "Id": ref, "Seq": 0, "InverterId": -1, "StringInputNumber": 0,
         "X": table.centres[2 * c], "Y": table.centres[2 * c + 1], "Angle": table.angle}
        for c, ref in enumerate(table.ids)]}]
    before = deepcopy(s)
    assert _code(ssr.bind_request, s, request, expected_rev=0, frame_ref=s["frames"][0]["id"],
                 tenant_id="fixture-tenant", job_id="fixture-job") == "SOLVE_GRID_MISMATCH"
    assert s == before


def test_ground_compact_consumers_b18_site(graph, b18, monkeypatch):
    g = ground_graph(graph, b18, 237)
    ids = slot_ids(g, 0, 294)
    g["strings"].append(entity(
        "string", 1, circuit_tag="S1", circuit_kind="String", ordered_panel_refs=ids[0:21], module_count=21,
        from_ref=ids[0], to_ref=ids[20], tag_text_ref=None, wire_gauge="10 AWG", length_ft=10,
        route=[[0, 0], [1, 0]], inverter_ref=None))
    monkeypatch.setattr(codec, "expand_graph", _never_expand)
    assert len(ssr.slot_panel_ids(g)) == 69678
    report = ssr.coverage(g)
    assert report["duplicate_panel_refs"] == [] and len(report["unassigned_panel_refs"]) == 69657
    assert ssr.sync_assignments(deepcopy(g)) == report
    assert _code(ssr.require_current_export, g) == "SOLAR_OUTPUT_NOT_CURRENT"
    sdg._reset_validation_caches()
    result = correct.run(g, {"expected_rev": 0, "memberships": [
        {"string_ref": app_id("string", 1), "ordered_panel_refs": ids[0:22]}]})
    assert result["rev"] == 1 and result["strings"][0]["module_count"] == 22
    assert len(result["extra"]["solve_coverage"]["unassigned_panel_refs"]) == 69656
    assert len(canon(result)) == 6835511


@pytest.mark.parametrize("name,params,differing,unassigned,digest", [
    ("solar_string_delete",
     {"operation": "delete-strings", "expected_rev": 0, "string_refs": [app_id("string", 3)]},
     ["frames", "panels"], 581,
     "75e29f994fb0f6c5737c27232eb82658cdcce97b84ec0742e1956b8f7ab4a603"),
    ("solar_string_flip",
     {"operation": "flip-string", "expected_rev": 0, "string_ref": app_id("string", 3)},
     ["frames", "panels"], 579,
     "dbec0310d3db6d3b038e5096cd0ddbd113c79d94fb149777b223e2a367744e9e"),
    ("solar_string_swap",
     {"operation": "swap-strings", "expected_rev": 0,
      "string_refs": [app_id("string", 1), app_id("string", 2)]},
     ["frames"], 579,
     "b423803b7767d5ce4bb9be1c11ea864d9bc52865b65b744498bee33018b1cc45"),
], ids=["delete", "flip", "swap"])
def test_ground_compact_consumers_string_edits_match_expansion(
        graph, b18, name, params, differing, unassigned, digest):
    tool = _builtin(name)
    s = strung(graph, b18)
    before = deepcopy(s)
    sdg._reset_validation_caches()
    result = tool.run(s, deepcopy(params))
    assert s == before
    assert result["rev"] == 1
    sdg._reset_validation_caches()
    assert sdg.validate_graph(result) == result
    sdg._reset_validation_caches()
    reference = tool.run(codec.expand_graph(deepcopy(s)), deepcopy(params))
    for key in ("strings", "inverters", "routes", "schedules", "settings", "project"):
        assert result[key] == reference[key]
    assert result["extra"]["solve_coverage"] == reference["extra"]["solve_coverage"]
    expanded = codec.expand_graph(result)
    assert sorted(key for key in reference if reference[key] != expanded[key]) == differing
    assert len(result["extra"]["solve_coverage"]["unassigned_panel_refs"]) == unassigned
    assert canon_sha(result) == digest


@pytest.mark.parametrize("name", [
    "solar_string_add", "solar_string_multi_add", "solar_string_midpoint",
], ids=["add", "multi_add", "midpoint"])
def test_ground_compact_consumers_panel_lookup_tools_refuse_slot_panels(graph, b18, name):
    s = strung(graph, b18)
    s["settings"]["panels_in_sequence"] = 21
    free = slot_ids(s, 1, 294)[100:103]
    before = deepcopy(s)
    if name == "solar_string_add":
        params = {"operation": "add-string", "expected_rev": 0, "ordered_panel_refs": list(free)}
    elif name == "solar_string_multi_add":
        params = {"operation": "add-strings", "expected_rev": 0,
                  "ordered_panel_refs": list(free), "string_length": 3}
    else:
        params = {"operation": "add-midpoint-string", "expected_rev": 0,
                  "start_panel_ref": free[0], "end_panel_ref": free[2]}
    assert _code(_builtin(name).run, s, params) == "MISSING_PANEL"
    assert s == before


def test_ground_compact_consumers_module_import_never_loads_the_codec():
    program = (
        "import sys\n"
        "sys.modules['solar_ground_graph_codec'] = None\n"
        "import solar_solve_results\n"
        "assert sys.modules['solar_ground_graph_codec'] is None\n"
        "print('OK')\n"
    )
    result = subprocess.run([sys.executable, "-c", program], cwd=str(SERVER),
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
