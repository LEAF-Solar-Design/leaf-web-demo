"""W4g-7b-04s: non-associative LINEAR and ALIGNED dimensions through contract v3, server side."""
from __future__ import annotations

import copy
import hashlib
import math
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
ALIGNED_LINE = (
    b"ADDDIMALIGNED|0|Standard|0.000,0.000,0.000|3.000,4.000,0.000|1.080,5.440,0.000\n")
LINEAR_LINE_R0 = (
    b"ADDDIMLINEAR|0|Standard|0.000,0.000,0.000|3.000,4.000,0.000|3.000,6.000,0.000|0.000000\n")


def _base():
    return {"dwg": "upload.dxf", "layers": ["0"], "polylines": [], "dimstyles": ["Standard"]}


def _linear(**changes):
    entity = {
        "handle": "new-dim", "kind": "DIMENSION", "dimtype": "LINEAR", "layer": "0",
        "def1": [0, 0, 0], "def2": [3, 4, 0], "dimline": [1.5, 6, 0], "rotation": 0,
        "style": "Standard",
    }
    entity.update(changes)
    return entity


def _aligned(**changes):
    entity = {
        "handle": "new-dim", "kind": "DIMENSION", "dimtype": "ALIGNED", "layer": "0",
        "def1": [0, 0, 0], "def2": [3, 4, 0], "dimline": [1.5, 6, 0], "style": "Standard",
    }
    entity.update(changes)
    return entity


# --- validate_mutations: canonical form and measurement ---------------------

def test_aligned_canonical_form_and_measurement():
    canonical = validate_mutations(_base(), {"added": [_aligned()]})
    assert canonical == {"added": [{
        "handle": "new-dim", "kind": "DIMENSION", "dimtype": "ALIGNED", "layer": "0",
        "def1": [0.0, 0.0, 0.0], "def2": [3.0, 4.0, 0.0], "dimline": [1.08, 5.44, 0.0],
        "style": "Standard", "measurement": 5.0,
    }]}
    assert uses_v3(canonical)
    assert validate_mutations(_base(), canonical) == canonical


@pytest.mark.parametrize("rotation,canonical_rotation,measurement", [
    (0, 0.0, 3.0), (90, 90.0, 4.0), (450, 90.0, 4.0),
])
def test_linear_canonical_form_normalizes_rotation_and_computes_measurement(
        rotation, canonical_rotation, measurement):
    canonical = validate_mutations(_base(), {"added": [_linear(rotation=rotation)]})
    entity = canonical["added"][0]
    assert entity["rotation"] == canonical_rotation
    assert entity["measurement"] == measurement
    assert entity["dimtype"] == "LINEAR"
    assert uses_v3(canonical)
    assert validate_mutations(_base(), canonical) == canonical


def test_linear_measurement_is_the_projection_never_the_endpoint_distance():
    # rotation 0 projects onto the x axis (3.0), never the 5.0 endpoint distance.
    canonical = validate_mutations(_base(), {"added": [_linear(rotation=0)]})
    assert canonical["added"][0]["measurement"] == 3.0
    assert canonical["added"][0]["measurement"] != 5.0


def test_dimline_placement_does_not_affect_measurement():
    canonical = validate_mutations(_base(), {"added": [_linear(dimline=[1.5, 9, 0])]})
    assert canonical["added"][0]["measurement"] == 3.0
    assert canonical["added"][0]["dimline"] == [3.0, 9.0, 0.0]


def test_dimline_is_canonicalized_to_def2_projected_onto_the_dimension_line():
    # AutoCAD stores group 10 as def2 projected onto the dimension line
    # through the given point, never the raw point the caller sent.
    aligned = validate_mutations(_base(), {"added": [_aligned()]})
    assert aligned["added"][0]["dimline"] == [1.08, 5.44, 0.0]
    linear_r0 = validate_mutations(_base(), {"added": [_linear(rotation=0)]})
    assert linear_r0["added"][0]["dimline"] == [3.0, 6.0, 0.0]
    linear_r90 = validate_mutations(_base(), {"added": [_linear(rotation=90)]})
    assert linear_r90["added"][0]["dimline"] == [1.5, 4.0, 0.0]
    assert linear_r90["added"][0]["measurement"] == 4.0


# --- F7: dimline canonicalization is a fixed point under 3-dp rounding ------

@pytest.mark.parametrize("build", [
    lambda: _aligned(),
    lambda: _linear(rotation=0),
    lambda: _linear(rotation=90),
    lambda: _linear(dimline=[1.5, 9, 0]),
])
def test_dimline_canonicalization_is_a_fixed_point_for_the_hand_derived_cases(build):
    once = validate_mutations(_base(), {"added": [build()]})
    twice = validate_mutations(_base(), once)
    assert twice == once


def test_dimline_canonicalization_is_a_fixed_point_off_axis_22_5_degrees():
    # At 22.5 deg the re-projection of the rounded canonical point moves by
    # up to 6.04e-4 (more than the 5e-4 half-quantum), so a naive re-round
    # would move it by 0.001 on a second validate. A supplied dimline that
    # already lies within tolerance of its own projection is kept unchanged.
    once = validate_mutations(_base(), {"added": [
        _linear(rotation=22.5, dimline=[1.3, 2.7, 0])]})
    twice = validate_mutations(_base(), once)
    assert twice == once


def test_a_far_dimline_point_still_projects():
    canonical = validate_mutations(_base(), {"added": [_linear(dimline=[100, 200, 0])]})
    assert canonical["added"][0]["dimline"] == [3.0, 200.0, 0.0]


# --- F5: a disagreeing supplied measurement is refused, never fail-open -----

@pytest.mark.parametrize("measurement", [3.0, 3.0004])
def test_an_agreeing_supplied_measurement_is_accepted_and_stays_the_computed_value(measurement):
    canonical = validate_mutations(_base(), {"added": [_linear(measurement=measurement)]})
    assert canonical["added"][0]["measurement"] == 3.0


def test_an_absent_measurement_is_accepted():
    canonical = validate_mutations(_base(), {"added": [_linear()]})
    assert canonical["added"][0]["measurement"] == 3.0


def test_a_disagreeing_supplied_measurement_is_refused():
    with pytest.raises(
            ValueError,
            match=r"^dimension measurement 999\.0 disagrees with the definition points \(3\.0\)$"):
        validate_mutations(_base(), {"added": [_linear(measurement=999)]})


# --- F9: the planar contract, z must be 0 on def1/def2/dimline --------------

def test_def2_with_nonzero_z_is_refused():
    with pytest.raises(
            ValueError, match=r"^dimension points must lie in the XY plane \(z = 0\) in this round$"):
        validate_mutations(_base(), {"added": [_aligned(def2=[3, 4, 5])]})


def test_dimline_with_nonzero_z_is_refused():
    with pytest.raises(
            ValueError, match=r"^dimension points must lie in the XY plane \(z = 0\) in this round$"):
        validate_mutations(_base(), {"added": [_aligned(dimline=[1.5, 6, 1])]})


# --- refusals ----------------------------------------------------------------

@pytest.mark.parametrize("changes,message", [
    ({"def2": [0, 0, 0]}, "dimension definition points coincide"),
    ({"dimtype": "ANGULAR"}, "added DIMENSION 'new-dim' has an unsupported dimtype"),
])
def test_linear_refusals(changes, message):
    with pytest.raises(ValueError, match=f"^{message}$"):
        validate_mutations(_base(), {"added": [_linear(**changes)]})


def test_aligned_refuses_a_rotation_field():
    with pytest.raises(ValueError, match="^rotation is only valid for LINEAR$"):
        validate_mutations(_base(), {"added": [_aligned(rotation=30)]})


def test_unknown_style_is_refused_when_the_dimstyles_catalogue_is_present():
    with pytest.raises(ValueError, match="^dimstyle Fancy is not loaded in this drawing$"):
        validate_mutations(_base(), {"added": [_linear(style="Fancy")]})


def test_intake_without_a_dimstyles_list_admits_only_standard():
    base = _base()
    del base["dimstyles"]
    canonical = validate_mutations(base, {"added": [_linear()]})
    assert canonical["added"][0]["style"] == "Standard"
    with pytest.raises(ValueError, match="^dimstyle Other is not loaded in this drawing$"):
        validate_mutations(base, {"added": [_linear(style="Other")]})


def test_linear_requires_an_explicit_rotation_field():
    add = _linear()
    del add["rotation"]
    with pytest.raises(ValueError, match="^added DIMENSION 'new-dim' LINEAR requires rotation$"):
        validate_mutations(_base(), {"added": [add]})


# --- plan lines: byte-exact, header, v2-only frozen --------------------------

def test_aligned_plan_line_is_byte_exact():
    canonical = validate_mutations(_base(), {"added": [_aligned()]})
    assert emit_plan(canonical, base_sha256=BASE_SHA) == (
        b"LEAF_MUTATION_PLAN|3\n" + f"BASE_SHA256|{BASE_SHA}\n".encode() + ALIGNED_LINE)


def test_linear_plan_line_is_byte_exact():
    canonical = validate_mutations(_base(), {"added": [_linear()]})
    assert emit_plan(canonical, base_sha256=BASE_SHA) == (
        b"LEAF_MUTATION_PLAN|3\n" + f"BASE_SHA256|{BASE_SHA}\n".encode() + LINEAR_LINE_R0)


def test_v2_only_plan_bytes_are_frozen():
    canonical = validate_mutations(_base(), {"added": [
        {"handle": "n1", "kind": "LINE", "layer": "0", "pts": [[0, 0], [3, 4]]},
    ]})
    assert uses_v3(canonical) is False
    assert emit_plan(canonical, base_sha256=BASE_SHA) == (
        b"LEAF_MUTATION_PLAN|2\n" + f"BASE_SHA256|{BASE_SHA}\n".encode()
        + b"ADDLINE|0|0,0,0|3,4,0\n")


# --- removal and property setters (03s) --------------------------------------

def _with_existing_dimension(handle="2A"):
    base = _base()
    base["dimensions"] = [{
        "type": "LINEAR", "p1": [0.0, 0.0, 0.0], "p2": [3.0, 4.0, 0.0],
        "dimline": [1.5, 6.0, 0.0], "rotation_deg": 0.0, "style": "Standard",
        "nrm": [0.0, 0.0, 1.0], "measurement": 3.0, "handle": handle,
    }]
    return base


def test_existing_dimension_handle_is_admitted_in_the_removal_set():
    base = _with_existing_dimension()
    canonical = validate_mutations(base, {"removed": ["2A"]})
    assert canonical == {"removed": ["2A"], "removed_kinds": {"2A": "DIMENSION"}}
    assert uses_v3(canonical) is True  # forces the v3 interpreter: REMOVE needs DIMENSION support
    assert emit_plan(canonical, base_sha256=BASE_SHA) == (
        b"LEAF_MUTATION_PLAN|3\n" + f"BASE_SHA256|{BASE_SHA}\n".encode() + b"REMOVE|2A\n")


def test_existing_dimension_handle_takes_property_setters():
    base = _with_existing_dimension()
    canonical = validate_mutations(base, {"set_color": [{"handle": "2A", "aci": 1}]})
    assert canonical == {"set_color": [{"handle": "2A", "aci": 1}]}
    assert uses_v3(canonical) is True
    # F1: a set_color target of kind DIMENSION verifies end to end through the
    # mock writer and the live verifier, exercising the EP inspect block's
    # DIMENSION coverage (da/lisp.py) rather than just canonical validation.
    result = write_loop.apply_mutations(base, canonical)
    assert result["properties"]["2A"] == {"aci": 1, "rgb": None}
    actual = copy.deepcopy(base)
    actual["properties"] = {
        "2A": {"aci": 1, "rgb": None, "linetype": "ByLayer", "lineweight": -1}}
    assert write_loop.verify_live_mutation_effects(base, actual, canonical) is None


# --- mock writer + DXF round trip --------------------------------------------

def test_mock_dimension_uses_intake_shape_and_dxf_maps_its_temporary_handle():
    base = _base()
    before = copy.deepcopy(base)
    result = write_loop.apply_mutations(base, {"added": [_linear()]})
    expected = {
        "type": "LINEAR", "p1": [0.0, 0.0, 0.0], "p2": [3.0, 4.0, 0.0],
        "dimline": [3.0, 6.0, 0.0], "rotation_deg": 0.0, "style": "Standard",
        "nrm": [0.0, 0.0, 1.0], "measurement": 3.0, "handle": "new-dim",
    }
    assert result["dimensions"] == [expected]
    assert base == before
    parsed = dxf_intake.parse_dxf_bytes(intake_dxf.intake_to_dxf(result))
    assert parsed["dimensions"] == [{**expected, "handle": "100"}]
    assert parsed["dimstyles"] == ["Standard"]


def test_mock_removed_dimension_drops_it():
    base = _with_existing_dimension()
    canonical = validate_mutations(base, {"removed": ["2A"]})
    result = write_loop.apply_mutations(base, canonical)
    assert result.get("dimensions", []) == []


# --- verifier: matched with the actual handle, wrong/missing refused --------

def test_verifier_binds_added_dimension_to_its_actual_handle():
    canonical = validate_mutations(_base(), {"added": [_linear()]})
    expected = write_loop.apply_mutations(_base(), canonical)
    matched_handles = {}
    write_loop._verify_dimension_effects(_base(), expected, canonical, matched_handles)
    assert matched_handles == {"new-dim": "new-dim"}
    actual = copy.deepcopy(_base())
    actual["dimensions"] = [{**expected["dimensions"][0], "handle": "2A"}]
    matched_handles = {}
    write_loop._verify_dimension_effects(_base(), actual, canonical, matched_handles)
    assert matched_handles == {"new-dim": "2A"}


def test_verifier_refuses_a_wrong_measurement_outside_the_tolerance():
    canonical = validate_mutations(_base(), {"added": [_linear()]})
    actual = copy.deepcopy(_base())
    actual["dimensions"] = [{
        "type": "LINEAR", "p1": [0.0, 0.0, 0.0], "p2": [3.0, 4.0, 0.0],
        "dimline": [3.0, 6.0, 0.0], "rotation_deg": 0.0, "style": "Standard",
        "nrm": [0.0, 0.0, 1.0], "measurement": 4.999, "handle": "2A",
    }]
    with pytest.raises(ValueError, match="unexpected measurement"):
        write_loop._verify_dimension_effects(_base(), actual, canonical, {})


def test_verifier_refuses_a_dimlfac_scaled_measurement_never_the_geometric_one():
    # F8: da/lisp.py's DM record is ALWAYS the geometric measurement (never
    # DXF group 42, which a dimstyle's DIMLFAC scales for on-screen display),
    # so a scaled display value never reaches this verifier — it refuses an
    # actual record carrying one exactly like any other wrong measurement.
    canonical = validate_mutations(_base(), {"added": [_linear()]})  # geometric measurement 3.0
    actual = copy.deepcopy(_base())
    actual["dimensions"] = [{
        "type": "LINEAR", "p1": [0.0, 0.0, 0.0], "p2": [3.0, 4.0, 0.0],
        "dimline": [3.0, 6.0, 0.0], "rotation_deg": 0.0, "style": "Standard",
        "nrm": [0.0, 0.0, 1.0], "measurement": 6.0,  # DIMLFAC=2 scaled display value
        "handle": "2A",
    }]
    with pytest.raises(ValueError, match="unexpected measurement"):
        write_loop._verify_dimension_effects(_base(), actual, canonical, {})


def test_verifier_refuses_a_missing_added_dimension():
    canonical = validate_mutations(_base(), {"added": [_linear()]})
    with pytest.raises(ValueError, match="^added DIMENSION 'new-dim' not found in output$"):
        write_loop._verify_dimension_effects(_base(), _base(), canonical, {})


def test_verifier_preserves_unchanged_dimensions_and_refuses_a_drift():
    base = _with_existing_dimension()
    actual = copy.deepcopy(base)
    write_loop._verify_dimension_effects(base, actual, {}, {})
    actual["dimensions"][0]["p2"] = [9.0, 9.0, 0.0]
    with pytest.raises(ValueError, match="unexpected output geometry"):
        write_loop._verify_dimension_effects(base, actual, {}, {})


def test_full_verify_live_mutation_effects_covers_dimensions():
    canonical = validate_mutations(_base(), {"added": [_linear()]})
    expected = write_loop.apply_mutations(_base(), canonical)
    write_loop.verify_live_mutation_effects(_base(), expected, canonical)


# --- F2: a legacy base (no `dimensions` key) never emitted DM rows ----------

def test_legacy_base_with_no_dimensions_key_verifies_only_the_added_dimension():
    # base's LeafExtract-shaped intake carries NO `dimensions` key at all (a
    # live write's stored base, unlike the mutation-inspect `actual`), so an
    # ALREADY-PRESENT dimension the actual reports must be ignored rather
    # than read as an unexpected extra entity.
    base = _base()
    assert "dimensions" not in base
    canonical = validate_mutations(base, {"added": [_linear()]})
    actual = copy.deepcopy(base)
    actual["dimensions"] = [
        {"type": "ALIGNED", "p1": [0.0, 0.0, 0.0], "p2": [5.0, 0.0, 0.0],
         "dimline": [2.5, 3.0, 0.0], "rotation_deg": 0.0, "style": "Standard",
         "nrm": [0.0, 0.0, 1.0], "measurement": 5.0, "handle": "pre-existing"},
        {"type": "LINEAR", "p1": [0.0, 0.0, 0.0], "p2": [3.0, 4.0, 0.0],
         "dimline": [3.0, 6.0, 0.0], "rotation_deg": 0.0, "style": "Standard",
         "nrm": [0.0, 0.0, 1.0], "measurement": 3.0, "handle": "new-handle"},
    ]
    matched_handles = {}
    write_loop._verify_dimension_effects(base, actual, canonical, matched_handles)
    assert matched_handles == {"new-dim": "new-handle"}


def test_base_carrying_the_dimensions_key_still_refuses_a_missing_handle():
    # The strict comparison stays once the base DOES carry the key: an
    # unchanged dimension's handle absent from the actual is refused exactly
    # as before, legacy tolerance never applies here.
    base = _with_existing_dimension(handle="2A")
    actual = copy.deepcopy(base)
    actual["dimensions"] = []
    with pytest.raises(ValueError, match="^unchanged handle '2A' is missing from output$"):
        write_loop._verify_dimension_effects(base, actual, {}, {})


# --- da/apply_lisp: the v3 interpreter text ----------------------------------

def test_v3_interpreter_carries_both_dimension_functions_and_freezes_v2():
    script_v3 = mutation_apply.build_apply_scr_v3()
    script_v2 = apply_lisp.build_apply_scr()
    for fn in ("leaf-apply-adddimlinear", "leaf-apply-adddimaligned",
               "leaf-adddimlinear-op", "leaf-adddimaligned-op"):
        assert fn in script_v3, fn
        assert fn not in script_v2, fn
    for tag in ("ADDDIMLINEAR", "ADDDIMALIGNED"):
        assert tag in script_v3 and tag not in script_v2
    linear_apply = next(
        line for line in script_v3.splitlines()
        if line.startswith("(defun leaf-apply-adddimlinear "))
    aligned_apply = next(
        line for line in script_v3.splitlines()
        if line.startswith("(defun leaf-apply-adddimaligned "))
    assert '(tblsearch "DIMSTYLE" style)' in linear_apply
    assert '(tblsearch "DIMSTYLE" style)' in aligned_apply
    assert "(cons 2 " not in linear_apply and "(cons 2 " not in aligned_apply
    assert "(cons 70 32)" in linear_apply
    assert "(cons 70 33)" in aligned_apply
    assert "(cons 50 (leaf-deg2rad rot))" in linear_apply
    assert "(cons 50" not in aligned_apply
    assert "AcDbRotatedDimension" in linear_apply
    assert "AcDbRotatedDimension" not in aligned_apply
    assert "AcDbAlignedDimension" in linear_apply and "AcDbAlignedDimension" in aligned_apply
    for line in script_v3.splitlines():
        assert line.count("(") == line.count(")"), line[:80]
    assert '(eval ' not in script_v3 and '(read ' not in script_v3
    # Pinned since 81e5d234 (frozen v2 apply script).
    assert hashlib.sha256(script_v2.encode("utf-8")).hexdigest() == (
        "a7ed0bb7dbd8266404574b523550d8103318981c47a927a6ad4c9daab07f6c35")


def test_v3_remove_op_admits_dimension_and_v2_stays_frozen():
    script_v3 = mutation_apply.build_apply_scr_v3()
    script_v2 = apply_lisp.build_apply_scr()
    remove_v3 = next(
        line for line in script_v3.splitlines() if line.startswith("(defun leaf-remove-op "))
    remove_v2 = next(
        line for line in script_v2.splitlines() if line.startswith("(defun leaf-remove-op "))
    assert '(list "LWPOLYLINE" "LINE" "CIRCLE" "ARC" "DIMENSION")' in remove_v3
    assert '(list "LWPOLYLINE" "LINE" "CIRCLE" "ARC")' in remove_v2
    assert "DIMENSION" not in remove_v2


def test_v3_leaf_target_admits_dimension_for_property_ops():
    script_v3 = mutation_apply.build_apply_scr_v3()
    target = next(
        line for line in script_v3.splitlines() if line.startswith("(defun leaf-target "))
    assert '"DIMENSION"' in target


def test_v3_created_list_tracks_added_dimensions_for_ordinal_style_targets():
    script_v3 = mutation_apply.build_apply_scr_v3()
    dispatcher = next(
        line for line in script_v3.splitlines()
        if line.startswith('(defun leaf-apply (op / result)'))
    assert '"ADDDIMLINEAR"' in dispatcher and '"ADDDIMALIGNED"' in dispatcher


# --- da/lisp: the DS / DM / DMX inspect blocks -------------------------------

def test_ds_and_dm_inspect_blocks_sit_after_geometry_and_the_bk_catalogue():
    from lisp import MUTATION_INSPECT_BLOCKS

    ds = MUTATION_INSPECT_BLOCKS[-2]
    dm = MUTATION_INSPECT_BLOCKS[-1]
    assert '"DS|"' in ds
    assert '"DM|"' in dm and '"DMX|"' in dm
    ar_index = next(i for i, b in enumerate(MUTATION_INSPECT_BLOCKS) if '"AR|"' in b)
    bk_index = next(i for i, b in enumerate(MUTATION_INSPECT_BLOCKS) if '"BK|"' in b)
    assert ar_index < bk_index < len(MUTATION_INSPECT_BLOCKS) - 2
    for line in (ds, dm):
        assert line.count("(") == line.count(")"), line[:80]


def test_ds_and_dm_blocks_are_shared_by_both_contracts():
    v2 = mutation_apply.activity_spec(2)["settings"]["inspectScript"]
    v3 = mutation_apply.activity_spec(3)["settings"]["inspectScript"]
    assert v2 == v3
    assert '"DS|"' in v2["value"] and '"DM|"' in v2["value"]


def test_intake_parse_reads_a_ds_and_dmx_fixture_line():
    parsed = intake_parse.parse_text("DS|Standard\nDMX|2A|2", "test.dwg")
    assert parsed["dimstyles"] == ["Standard"]
    assert parsed["dimensions_unsupported"] == 1
    assert "dimensions" not in parsed


def test_dm_block_reports_def_points_as_wcs_never_ocs_transformed():
    # F3 (opus round-one read of PR #1119): DIMENSION groups 13/14/10 are WCS
    # per the DXF spec (only 11/12/16 are OCS), so the DM block must report
    # entget's p1/p2/dl exactly, with no `trans` (arbitrary-axis) call.
    from lisp import MUTATION_INSPECT_BLOCKS

    dm = MUTATION_INSPECT_BLOCKS[-1]
    assert "(setq wp1 p1 wp2 p2 wdl dl)" in dm
    assert "(trans p1" not in dm and "(trans p2" not in dm and "(trans dl" not in dm


def test_dm_style_decodes_percent_escapes_the_same_way_as_ds():
    # F6: da/lisp.py's DM emitter percent-encodes the style with the same
    # leaf-bk-encode helper DS uses; the parser must decode both identically
    # (percent last) so a style named "A%B" round-trips to the same string.
    parsed = intake_parse.parse_text(
        "DS|A%25B\n"
        "DM|linear|0,0,0|3,4,0|1.5,6,0|0|A%25B|0,0,1|3|2A",
        "test.dwg")
    assert parsed["dimstyles"] == ["A%B"]
    assert parsed["dimensions"][0]["style"] == "A%B"


# --- bounded accoreconsole canary --------------------------------------------

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
def test_accoreconsole_dimension_canary_reopens_and_verifies_the_output(tmp_path):
    # Same local binary and tracked seed as da/test_mutation_apply_accoreconsole.py.
    host = tmp_path / "host.dwg"
    shutil.copyfile(ROOT / "data" / "rooftop_demo.dwg", host)

    canonical = validate_mutations(_base(), {"added": [
        _linear(handle="lin-dim", rotation=0),
        _aligned(handle="align-dim"),
    ]})
    plan = emit_plan(canonical, base_sha256=hashlib.sha256(host.read_bytes()).hexdigest())
    assert LINEAR_LINE_R0 in plan and ALIGNED_LINE in plan

    settings = mutation_apply.activity_spec(3)["settings"]
    inspect = settings["inspectScript"]["value"]
    quit_line = '(command "_.QUIT" "_Y")\r\n'
    assert inspect.endswith(quit_line)
    before = inspect[:-len(quit_line)].replace("output-intake.txt", "base-intake.txt")
    (tmp_path / "mutation-plan.txt").write_bytes(plan.replace(b"\n", b"\r\n"))
    _console(tmp_path, host, "apply.scr", before + settings["script"]["value"])

    base = intake_parse.parse(tmp_path / "base-intake.txt", "canary")
    assert not base.get("parseErrors"), base.get("parseErrors")
    assert "Standard" in base.get("dimstyles", [])

    output = tmp_path / "output.dwg"
    assert output.exists() and output.stat().st_size > 0
    _console(tmp_path, output, "after.scr", inspect)
    families = tmp_path / "output-intake.txt"
    actual = intake_parse.parse(families, "canary")
    assert not actual.get("parseErrors"), actual.get("parseErrors")
    assert "Standard" in actual.get("dimstyles", [])

    added = {d["type"]: d for d in actual["dimensions"] if d["handle"] not in
             {e["handle"] for e in base.get("dimensions", [])}}
    assert set(added) == {"LINEAR", "ALIGNED"}
    assert f'{added["LINEAR"]["measurement"]:.3f}' == "3.000"
    assert f'{added["ALIGNED"]["measurement"]:.3f}' == "5.000"

    note = write_loop.verify_live_mutation_effects(base, actual, canonical)
    assert note is None
