"""W4g-7b-03s: colour / linetype / lineweight through contract v3, server side."""
from __future__ import annotations

import copy
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "da"))

import apply_lisp
import dxf_intake
import intake_dxf
import intake_parse
import mutation_apply
import write_loop
from mutation_plan import emit_plan, uses_v3, validate_mutations


BASE_SHA = "1" * 64
ACCORECONSOLE = Path(r"C:\Program Files\Autodesk\AutoCAD 2026\accoreconsole.exe")


def _base():
    return {
        "dwg": "upload.dxf", "layers": ["0"],
        "polylines": [
            # A LINE (2-point open polyline, the frozen §1 idiom).
            {"layer": "0", "closed": False, "pts": [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]],
             "xdata": None, "handle": "3B"},
        ],
        "circles": [{"layer": "0", "c": [0.0, 0.0, 0.0], "r": 1.0,
                     "nrm": [0.0, 0.0, 1.0], "handle": "2A"}],
        "arcs": [{"layer": "0", "c": [0.0, 0.0, 0.0], "r": 1.0,
                  "start_deg": 0.0, "end_deg": 90.0, "nrm": [0.0, 0.0, 1.0], "handle": "5D"}],
        "inserts": [],
        # "2A" carries a known (ByLayer) colour; "3B", "5D" carry none: an
        # UNAVAILABLE property is never assumed ByLayer, so a set targeting
        # them is never a no-op just because the new value happens to be 256.
        "properties": {
            "2A": {"aci": 256, "rgb": None, "linetype": "ByLayer", "lineweight": -1},
        },
    }


# --- validate_mutations: canonical form and plan lines -------------------

def test_set_color_canonical_form_and_plan_line():
    canonical = validate_mutations(_base(), {"set_color": [{"handle": "2A", "aci": 1}]})
    assert canonical == {"set_color": [{"handle": "2A", "aci": 1}]}
    assert uses_v3(canonical)
    assert emit_plan(canonical, base_sha256=BASE_SHA) == (
        b"LEAF_MUTATION_PLAN|3\n" + f"BASE_SHA256|{BASE_SHA}\n".encode()
        + b"SETCOLOR|H:2A|1\n")


def test_set_color_accepted_when_true_colour_present_and_mock_drops_rgb():
    base = _base()
    base["properties"]["2A"]["rgb"] = [10, 20, 30]
    canonical = validate_mutations(base, {"set_color": [{"handle": "2A", "aci": 1}]})
    assert canonical == {"set_color": [{"handle": "2A", "aci": 1}]}
    result = write_loop.apply_mutations(base, canonical)
    assert result["properties"]["2A"] == {
        "aci": 1, "rgb": None, "linetype": "ByLayer", "lineweight": -1,
    }


def test_set_color_to_current_aci_is_not_a_no_op_when_true_colour_present():
    base = _base()
    base["properties"]["2A"]["rgb"] = [10, 20, 30]
    canonical = validate_mutations(base, {"set_color": [{"handle": "2A", "aci": 256}]})
    assert canonical == {"set_color": [{"handle": "2A", "aci": 256}]}


def test_set_lineweight_canonical_form_and_plan_line():
    canonical = validate_mutations(_base(), {"set_lineweight": [{"handle": "2A", "weight": 25}]})
    assert canonical == {"set_lineweight": [{"handle": "2A", "weight": 25}]}
    assert emit_plan(canonical, base_sha256=BASE_SHA) == (
        b"LEAF_MUTATION_PLAN|3\n" + f"BASE_SHA256|{BASE_SHA}\n".encode()
        + b"SETLINEWEIGHT|H:2A|25\n")


def test_set_linetype_canonical_form_and_plan_line():
    canonical = validate_mutations(_base(), {"set_linetype": [{"handle": "2A", "name": "Continuous"}]})
    assert canonical == {"set_linetype": [{"handle": "2A", "name": "Continuous"}]}
    assert emit_plan(canonical, base_sha256=BASE_SHA) == (
        b"LEAF_MUTATION_PLAN|3\n" + f"BASE_SHA256|{BASE_SHA}\n".encode()
        + b"SETLINETYPE|H:2A|Continuous\n")


def test_set_linetype_canonicalizes_to_the_heads_spelling_when_it_differs_only_in_case():
    base = _base()
    # "5D" (an arc the head already lists) carries the drawing's own
    # spelling; the request targets "2A" with a different-case spelling of
    # the SAME name, and must lower to the head's spelling, not the input's.
    base["properties"]["5D"] = {"aci": 256, "rgb": None, "linetype": "DASHED", "lineweight": -1}
    canonical = validate_mutations(base, {"set_linetype": [{"handle": "2A", "name": "dashed"}]})
    assert canonical == {"set_linetype": [{"handle": "2A", "name": "DASHED"}]}


def test_set_linetype_head_uppercase_continuous_canonicalizes_and_is_a_noop_against_itself():
    # w4g-7b-03s-d D3: with the standard spellings ordered LAST, a head that
    # already carries the non-standard capitalization "CONTINUOUS" keeps it
    # as the canonical spelling (never the standard "Continuous"), and a
    # request for the same name in any case is a no-op against that handle.
    base = _base()
    base["properties"]["2A"]["linetype"] = "CONTINUOUS"
    with pytest.raises(ValueError, match="set_linetype '2A' is a no-op"):
        validate_mutations(base, {"set_linetype": [{"handle": "2A", "name": "continuous"}]})
    canonical = validate_mutations(
        base, {"set_linetype": [{"handle": "2A", "name": "continuous"}]}, reject_noop=False)
    assert canonical == {"set_linetype": [{"handle": "2A", "name": "CONTINUOUS"}]}


def test_set_linetype_head_mixed_case_continuous_canonicalizes_and_is_a_noop_against_itself():
    base = _base()
    base["properties"]["2A"]["linetype"] = "Continuous"
    with pytest.raises(ValueError, match="set_linetype '2A' is a no-op"):
        validate_mutations(base, {"set_linetype": [{"handle": "2A", "name": "CONTINUOUS"}]})
    canonical = validate_mutations(
        base, {"set_linetype": [{"handle": "2A", "name": "CONTINUOUS"}]}, reject_noop=False)
    assert canonical == {"set_linetype": [{"handle": "2A", "name": "Continuous"}]}


def test_set_linetype_bylayer_admitted_when_no_entity_lists_a_linetype():
    # No entity in the head lists a linetype at all: the standard names are
    # the only known spellings, so "bylayer" canonicalizes to "ByLayer" and
    # is admitted (not a no-op: "linetype" is not in "2A"'s properties).
    base = _base()
    del base["properties"]["2A"]["linetype"]
    canonical = validate_mutations(base, {"set_linetype": [{"handle": "2A", "name": "bylayer"}]})
    assert canonical == {"set_linetype": [{"handle": "2A", "name": "ByLayer"}]}


def test_two_set_color_ops_sort_by_hex_handle_and_read_back():
    base = _base()
    base["properties"]["2A"]["aci"] = 1
    base["properties"]["5D"] = {"aci": 3, "rgb": None, "linetype": "ByLayer", "lineweight": -1}
    canonical = validate_mutations(
        base, {"set_color": [{"handle": "5D", "aci": 5}, {"handle": "2A", "aci": 5}]})
    # 0x2A < 0x5D: sorted ascending by hex handle regardless of input order.
    assert canonical["set_color"] == [{"handle": "2A", "aci": 5}, {"handle": "5D", "aci": 5}]
    result = write_loop.apply_mutations(base, canonical)
    assert result["properties"]["2A"]["aci"] == 5
    assert result["properties"]["5D"]["aci"] == 5


def test_set_color_accepted_when_property_unavailable_is_not_a_no_op():
    canonical = validate_mutations(_base(), {"set_color": [{"handle": "5D", "aci": 256}]})
    assert canonical == {"set_color": [{"handle": "5D", "aci": 256}]}


@pytest.mark.parametrize("mutations,message", [
    ({"set_color": [{"handle": "2A", "aci": 1}, {"handle": "2A", "aci": 2}]},
     "duplicate set_color handle '2A'"),
    ({"set_color": [{"handle": "5D", "aci": 1}], "removed": ["5D"]},
     "property target '5D' is also removed"),
    ({"set_color": [{"handle": "99", "aci": 1}]}, "unknown set_color handle '99'"),
    ({"set_color": [{"handle": "2A", "aci": 257}]}, "color aci must be an integer in 0..256"),
    ({"set_color": [{"handle": "2A", "aci": True}]}, "color aci must be an integer in 0..256"),
    ({"set_lineweight": [{"handle": "2A", "weight": 26}]},
     "lineweight 26 is not a valid enumeration value"),
    ({"set_linetype": [{"handle": "2A", "name": ""}]}, "linetype is not a safe linetype name"),
    ({"set_linetype": [{"handle": "2A", "name": "bad|name"}]}, "linetype is not a safe linetype name"),
    ({"set_color": [{"handle": "2A", "aci": 256}]}, "set_color '2A' is a no-op"),
    ({"set_linetype": [{"handle": "2A", "name": "Dashed"}]},
     "linetype Dashed is not loaded in this drawing"),
])
def test_property_op_refusals(mutations, message):
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        validate_mutations(_base(), mutations)


# --- styled adds -----------------------------------------------------------

def _styled_adds():
    plain = {"handle": "p1", "kind": "LINE", "layer": "0", "pts": [[0, 0], [1, 0]]}
    styled = {"handle": "p2", "kind": "LINE", "layer": "0", "pts": [[0, 0], [2, 0]], "color": 5}
    return plain, styled


def test_styled_add_lowers_to_a_target_at_its_canonical_ordinal():
    base = _base()
    plain, styled = _styled_adds()
    canonical = validate_mutations(base, {"added": [plain, styled]})
    # "p1" < "p2": plain sorts first regardless of style, ordinal 0 and 1.
    assert [entity["handle"] for entity in canonical["added"]] == ["p1", "p2"]
    assert canonical["added"][1]["color"] == 5
    assert "color" not in canonical["added"][0]
    assert uses_v3(canonical)
    assert emit_plan(canonical, base_sha256=BASE_SHA) == (
        b"LEAF_MUTATION_PLAN|3\n" + f"BASE_SHA256|{BASE_SHA}\n".encode()
        + b"ADDLINE|0|0,0,0|1,0,0\nADDLINE|0|0,0,0|2,0,0\nSETCOLOR|A:1|5\n")


def test_styled_add_and_direct_setter_together_order_h_before_a():
    base = _base()
    plain, styled = _styled_adds()
    canonical = validate_mutations(
        base, {"added": [plain, styled], "set_color": [{"handle": "2A", "aci": 9}]})
    assert emit_plan(canonical, base_sha256=BASE_SHA) == (
        b"LEAF_MUTATION_PLAN|3\n" + f"BASE_SHA256|{BASE_SHA}\n".encode()
        + b"ADDLINE|0|0,0,0|1,0,0\nADDLINE|0|0,0,0|2,0,0\n"
        + b"SETCOLOR|H:2A|9\nSETCOLOR|A:1|5\n")


def test_mock_styled_add_sets_properties_keyed_by_the_mapped_handle():
    base = _base()
    plain, styled = _styled_adds()
    canonical = validate_mutations(base, {"added": [plain, styled]})
    result = write_loop.apply_mutations(base, canonical)
    assert result["properties"]["p2"] == {"aci": 5, "rgb": None}
    assert "p1" not in result["properties"]


def test_verify_prefers_the_property_matching_candidate_on_a_geometric_tie():
    # R4: two coincident adds, one styled, re-extracted in the OPPOSITE order
    # from canonical add order ("p1" < "p2" but the output lists "N2" (the
    # one AutoCAD actually painted color 5) before "N1"). Without preferring
    # the property match, the plain add (processed first, by handle) would
    # greedily bind to whichever coincident candidate comes first in the
    # output and steal the styled add's own match.
    base = _base()
    plain = {"handle": "p1", "kind": "LINE", "layer": "0", "pts": [[9, 9], [10, 9]]}
    styled = {"handle": "p2", "kind": "LINE", "layer": "0", "pts": [[9, 9], [10, 9]], "color": 5}
    canonical = validate_mutations(base, {"added": [plain, styled]})
    actual = copy.deepcopy(base)
    actual["polylines"].extend([
        {"layer": "0", "closed": False, "pts": [[9.0, 9.0, 0.0], [10.0, 9.0, 0.0]],
         "xdata": None, "handle": "N2"},
        {"layer": "0", "closed": False, "pts": [[9.0, 9.0, 0.0], [10.0, 9.0, 0.0]],
         "xdata": None, "handle": "N1"},
    ])
    actual["properties"]["N2"] = {"aci": 5, "rgb": None, "linetype": "ByLayer", "lineweight": -1}
    assert write_loop.verify_live_mutation_effects(base, actual, canonical) is None


# --- header rule -------------------------------------------------------

def test_v2_only_plan_bytes_are_unaffected_by_v3_style_support():
    canonical = validate_mutations(_base(), {"added": [
        {"handle": "n1", "kind": "LINE", "layer": "0", "pts": [[0, 0], [3, 4]]},
    ]})
    assert uses_v3(canonical) is False
    assert emit_plan(canonical, base_sha256=BASE_SHA) == (
        b"LEAF_MUTATION_PLAN|2\n" + f"BASE_SHA256|{BASE_SHA}\n".encode()
        + b"ADDLINE|0|0,0,0|3,4,0\n")


# --- write_loop: mock writer round trip through DXF -------------------

def test_intake_dxf_round_trips_62_6_370_for_a_styled_entity_and_omits_untouched():
    intake = {
        "dwg": "x", "layers": ["0"],
        "polylines": [
            {"layer": "0", "closed": False, "pts": [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]],
             "xdata": None, "handle": "2A"},
            {"layer": "0", "closed": False, "pts": [[0.0, 0.0, 0.0], [2.0, 0.0, 0.0]],
             "xdata": None, "handle": "3B"},
        ],
        "properties": {"2A": {"aci": 1, "rgb": None, "linetype": "Continuous", "lineweight": 25}},
    }
    data = intake_dxf.intake_to_dxf(intake)
    back = dxf_intake.parse_dxf_bytes(data)
    assert back["properties"] == {"2A": {"aci": 1, "rgb": None, "linetype": "Continuous", "lineweight": 25}}
    assert "3B" not in back["properties"]


def test_mock_round_trip_for_a_setter_and_a_styled_add_via_dxf():
    # Real (hex) handles here, unlike _styled_adds()'s "p1"/"p2": intake_dxf
    # only preserves a handle verbatim (uppercased) when it is real hex,
    # otherwise it synthesizes a fresh one, same as an added INSERT's temp
    # handle does (test_w4g7b_02s.py's mock/DXF round trip).
    base = _base()
    plain = {"handle": "60", "kind": "LINE", "layer": "0", "pts": [[0, 0], [1, 0]]}
    styled = {"handle": "61", "kind": "LINE", "layer": "0", "pts": [[0, 0], [2, 0]], "color": 5}
    canonical = validate_mutations(
        base, {"added": [plain, styled], "set_color": [{"handle": "2A", "aci": 3}]})
    result = write_loop.apply_mutations(base, canonical)
    data = intake_dxf.intake_to_dxf(result)
    back = dxf_intake.parse_dxf_bytes(data)
    assert back["properties"]["2A"] == {"aci": 3, "rgb": None, "linetype": "ByLayer", "lineweight": -1}
    assert back["properties"]["61"] == {"aci": 5, "rgb": None, "linetype": "ByLayer", "lineweight": -1}
    assert "60" not in back["properties"]
    assert "3B" not in back["properties"]


# --- write_loop: verify_live_mutation_effects ---------------------------

def test_verify_accepts_matching_property_effects():
    base = _base()
    canonical = validate_mutations(base, {"set_color": [{"handle": "2A", "aci": 1}]})
    actual = copy.deepcopy(base)
    actual["properties"]["2A"]["aci"] = 1
    assert write_loop.verify_live_mutation_effects(base, actual, canonical) is None


def test_verify_refuses_a_property_effect_that_was_not_applied():
    base = _base()
    canonical = validate_mutations(base, {"set_color": [{"handle": "2A", "aci": 1}]})
    actual = copy.deepcopy(base)  # aci stays 256
    with pytest.raises(ValueError,
                        match=re.escape("set_color '2A' not applied: expected 1, found 256")):
        write_loop.verify_live_mutation_effects(base, actual, canonical)


def test_verify_refuses_when_true_colour_still_present_after_set_color():
    base = _base()
    canonical = validate_mutations(base, {"set_color": [{"handle": "2A", "aci": 1}]})
    actual = copy.deepcopy(base)
    actual["properties"]["2A"]["aci"] = 1
    actual["properties"]["2A"]["rgb"] = [1, 2, 3]
    with pytest.raises(ValueError, match=re.escape(
            "set_color '2A' not applied: a true colour (420) is still present")):
        write_loop.verify_live_mutation_effects(base, actual, canonical)


def test_verify_reports_unverified_note_for_a_legacy_actual_without_properties():
    base = _base()
    canonical = validate_mutations(base, {"set_color": [{"handle": "2A", "aci": 1}]})
    actual = copy.deepcopy(base)
    actual["properties"]["2A"]["aci"] = 1
    del actual["properties"]
    note = write_loop.verify_live_mutation_effects(base, actual, canonical)
    assert note == ("property effects unverified: the re-extracted output carries "
                     "no properties record")


def test_verify_accepts_a_linetype_read_back_in_a_different_case():
    # R2: tblsearch is case-insensitive, so the LIVE apply can legitimately
    # read back a different case than the plan named; the verifier must not
    # refuse that as "not applied".
    base = _base()
    canonical = validate_mutations(base, {"set_linetype": [{"handle": "2A", "name": "Continuous"}]})
    actual = copy.deepcopy(base)
    actual["properties"]["2A"]["linetype"] = "CONTINUOUS"
    assert write_loop.verify_live_mutation_effects(base, actual, canonical) is None


def test_verify_returns_none_when_the_plan_touches_no_properties():
    base = _base()
    canonical = validate_mutations(base, {"added": [
        {"handle": "plain", "kind": "LINE", "layer": "0", "pts": [[5, 5], [6, 6]]},
    ]})
    expected = write_loop.apply_mutations(base, canonical)
    actual = copy.deepcopy(expected)
    del actual["properties"]
    assert write_loop.verify_live_mutation_effects(base, actual, canonical) is None


# --- da/apply_lisp: the v3 interpreter text -----------------------------

def test_v3_interpreter_carries_the_three_setters_and_v2_stays_frozen():
    script_v3 = mutation_apply.build_apply_scr_v3()
    script_v2 = apply_lisp.build_apply_scr()
    for fn in ("leaf-apply-setcolor", "leaf-apply-setlinetype", "leaf-apply-setlineweight",
               "leaf-target", "leaf-target-p", "leaf-property-op"):
        assert fn in script_v3, fn
        assert fn not in script_v2, fn
    for tag in ("SETCOLOR", "SETLINETYPE", "SETLINEWEIGHT"):
        assert tag in script_v3
        assert tag not in script_v2
    assert "(setq leaf-created nil)" in script_v3
    setcolor = next(
        line for line in script_v3.splitlines() if line.startswith("(defun leaf-apply-setcolor"))
    assert "420" in setcolor and "430" in setcolor
    setlinetype = next(
        line for line in script_v3.splitlines() if line.startswith("(defun leaf-apply-setlinetype"))
    assert '(tblsearch "LTYPE"' in setlinetype
    target = next(
        line for line in script_v3.splitlines() if line.startswith("(defun leaf-target "))
    assert "handent" in target and "leaf-created" in target and "leaf-target-p" in target
    target_p = next(
        line for line in script_v3.splitlines() if line.startswith("(defun leaf-target-p "))
    assert '"H:"' in target_p and '"A:"' in target_p
    assert '(eval ' not in script_v3 and '(read ' not in script_v3
    # Pinned since 81e5d234 (frozen v2 apply script).
    # v2 re-pinned by the invalid-plan fix (apply flag + two-predicate SAVEAS), a da change; the pin still freezes v2 against 7b records.
    assert hashlib.sha256(script_v2.encode("utf-8")).hexdigest() == (
        "30c38a48b69b81412ce25466554503bf029892c0065b1c3dc2867e763d6eab33")


def test_v3_interpreter_dispatches_property_ops_before_add_insert():
    script_v3 = mutation_apply.build_apply_scr_v3()
    parser = next(
        line for line in script_v3.splitlines() if line.startswith("(defun leaf-parse-line"))
    assert parser.index("SETCOLOR") < parser.index("ADDINSERT")


# --- da/lisp: the shared EP inspect block --------------------------------

def test_ep_inspect_block_sits_after_geometry_and_before_the_catalogue():
    from lisp import MUTATION_INSPECT_BLOCKS

    ep = MUTATION_INSPECT_BLOCKS[3]
    assert '"EP|"' in ep
    assert ep.count('"EP|"') == 1
    for kind in ("LINE", "LWPOLYLINE", "CIRCLE", "ARC", "INSERT"):
        assert f'(cons 0 "{kind}")' in ep
    catalogue_index = MUTATION_INSPECT_BLOCKS.index(
        next(block for block in MUTATION_INSPECT_BLOCKS if "leaf-bk-point" in block
             and block.startswith("(defun")))
    assert MUTATION_INSPECT_BLOCKS.index(ep) < catalogue_index


def test_ep_inspect_block_is_shared_by_both_contracts():
    v2 = mutation_apply.activity_spec(2)["settings"]["inspectScript"]
    v3 = mutation_apply.activity_spec(3)["settings"]["inspectScript"]
    assert v2 == v3
    assert '"EP|"' in v2["value"]


def test_intake_parse_reads_an_ep_fixture_line():
    record = "EP|2A|1|10,20,30|Continuous|25"
    parsed = intake_parse.parse_text(record, "test.dwg")
    assert parsed["properties"]["2A"] == {
        "aci": 1, "rgb": [10, 20, 30], "linetype": "Continuous", "lineweight": 25,
    }


def test_intake_parse_decodes_percent_escapes_in_the_ep_linetype_name():
    # R5: da/lisp.py's EP block percent-encodes % | CR LF in the linetype
    # name exactly like a BK block's name; intake_parse must decode it the
    # same way (percent last, so a literal "%7C" round-trips unchanged).
    record = "EP|2A|1|~|A%7Cbar%0D%0Aname%25|25"
    parsed = intake_parse.parse_text(record, "test.dwg")
    assert parsed["properties"]["2A"]["linetype"] == "A|bar\r\nname%"


# --- bounded accoreconsole canary ---------------------------------------

def _console(work, source, script_name, script):
    path = work / script_name
    path.write_text(script, encoding="utf-8", newline="")
    result = subprocess.run(
        [str(ACCORECONSOLE), "/i", str(source), "/s", str(path)],
        cwd=work, capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "LEAF-MUTATION-PLAN-INVALID" not in result.stdout, result.stdout
    assert "LEAF-MUTATION-APPLY-FAILED" not in result.stdout, result.stdout


@pytest.mark.skipif(not ACCORECONSOLE.exists(), reason="local AutoCAD 2026 console is required")
def test_accoreconsole_property_canary_sets_and_verifies_the_three_properties(tmp_path):
    # Same local binary and tracked seed as da/test_mutation_apply_accoreconsole.py.
    # A real, stable handle already in the tracked seed (data/rooftop_demo.intake.json
    # polylines[0]) stands in for "an existing LWPOLYLINE": no entmake seeding needed.
    target_handle = "9462"
    host = tmp_path / "host.dwg"
    shutil.copyfile(ROOT / "data" / "rooftop_demo.dwg", host)

    canonical = validate_mutations(
        {"polylines": [{
            "handle": target_handle, "layer": "Panels", "closed": True,
            "pts": [[14323.816, 2836.126, -25.296], [14400.816, 2836.126, -25.296],
                    [14400.816, 2874.595, -25.296], [14323.816, 2874.595, -25.296]],
            "xdata": None,
        }], "inserts": []},
        {
            "added": [{"handle": "styled-line", "kind": "LINE", "layer": "0",
                       "pts": [[0, 0], [1, 0]], "color": 1}],
            "set_lineweight": [{"handle": target_handle, "weight": 25}],
            "set_linetype": [{"handle": target_handle, "name": "Continuous"}],
        },
    )
    plan = emit_plan(canonical, base_sha256=hashlib.sha256(host.read_bytes()).hexdigest())
    assert b"SETCOLOR|A:0|1\n" in plan
    assert f"SETLINEWEIGHT|H:{target_handle}|25\n".encode() in plan
    assert f"SETLINETYPE|H:{target_handle}|Continuous\n".encode() in plan
    (tmp_path / "mutation-plan.txt").write_bytes(plan.replace(b"\n", b"\r\n"))

    settings = mutation_apply.activity_spec(3)["settings"]
    inspect = settings["inspectScript"]["value"]
    quit_line = '(command "_.QUIT" "_Y")\r\n'
    assert inspect.endswith(quit_line)
    # Extract the unmodified drawing first (renamed output), then continue the
    # SAME session straight into the frozen apply script: it already sets
    # CMDECHO/FILEDIA itself, so no extra setup lines are seeded here.
    before = inspect[:-len(quit_line)].replace("output-intake.txt", "base-intake.txt")
    _console(tmp_path, host, "apply.scr", before + settings["script"]["value"])

    base = intake_parse.parse(tmp_path / "base-intake.txt", "canary")
    assert not base.get("parseErrors"), base.get("parseErrors")
    assert target_handle in base["properties"]

    output = tmp_path / "output.dwg"
    assert output.exists() and output.stat().st_size > 0
    _console(tmp_path, output, "after.scr", inspect)
    families = tmp_path / "output-intake.txt"
    actual = intake_parse.parse(families, "canary")
    assert not actual.get("parseErrors"), actual.get("parseErrors")
    assert actual["properties"][target_handle]["lineweight"] == 25
    assert actual["properties"][target_handle]["linetype"] == "Continuous"

    note = write_loop.verify_live_mutation_effects(base, actual, canonical)
    assert note is None
