"""W4g-7c-3s: MLEADER through the server mutation contract v3."""
from __future__ import annotations

import pytest

# Keep the write_loop/mutation_apply cycle safe when this file collects alone.
import write_loop
import mutation_apply
from mutation_plan import emit_plan, uses_v3, validate_mutations


BASE_SHA = "1" * 64
MLEADER_LINE = b"ADDMLEADER|0|Standard|0.000,0.000,0.000|5.000,4.000,0.000|Valve\n"


def _base():
    return {"layers": ["0", "SITE"], "polylines": [], "mlstyles": [{
        "name": "Standard", "textstyle": "Standard", "height": 0.18,
        "arrow": 0.18, "dogleg": 0.36, "gap": 0.09, "segments": 1,
    }]}


def _mleader(**changes):
    return {"handle": "new-leader", "kind": "MLEADER", "layer": "0",
            "style": "Standard", "pts": [[0, 0, 0], [5, 4, 0]],
            "text": "Valve", **changes}


def test_mleader_probe_canonical_form_and_exact_plan():
    canonical = validate_mutations(_base(), {"added": [_mleader(style="standard")]})
    assert canonical == {"added": [_mleader(pts=[[0.0, 0.0, 0.0], [5.0, 4.0, 0.0]])]}
    assert validate_mutations(_base(), canonical) == canonical
    assert uses_v3(canonical)
    assert emit_plan(canonical, base_sha256=BASE_SHA) == (
        b"LEAF_MUTATION_PLAN|3\nBASE_SHA256|" + BASE_SHA.encode() + b"\n" + MLEADER_LINE)


def test_mleader_rounding_and_layer_spelling():
    canonical = validate_mutations(_base(), {"added": [_mleader(
        layer="site", pts=[[0, 0, -0.0004], [5.0004, 4.0004, 0.0004]])]})
    assert canonical["added"] == [_mleader(layer="SITE", pts=[[0, 0, 0], [5, 4, 0]])]


def test_mleader_removal_uses_v3():
    base = {**_base(), "mleaders": [{"handle": "AB", "layer": "0"}]}
    canonical = validate_mutations(base, {"removed": ["AB"]})
    assert canonical == {"removed": ["AB"], "removed_kinds": {"AB": "MULTILEADER"}}
    assert uses_v3(canonical)
    assert emit_plan(canonical, base_sha256=BASE_SHA).endswith(b"REMOVE|AB\n")
    assert validate_mutations(base, canonical) == canonical


@pytest.mark.parametrize("pts", [
    [[0, 0, 0]],
    [[0, 0, 0], [5, 4, 0], [6, 4, 0]],
    [[0, 0, 0], [0.0004, 0.0004, 0]],
    [[0, 0, 0], [5, 4, 0.001]],
    [[float("nan"), 0, 0], [5, 4, 0]],
    [[0, 0, 0], [1_000_000_001, 4, 0]],
])
def test_mleader_point_refusals(pts):
    with pytest.raises(ValueError):
        validate_mutations(_base(), {"added": [_mleader(pts=pts)]})


@pytest.mark.parametrize("text", ["x" * 257, "", "  Valve", "Valve  ",
                                  "Va|lve", "Valve\\P", "Valve%", "Válve",
                                  "Va\nlve", "Va\rlve", 123])
def test_mleader_text_refusals(text):
    with pytest.raises(ValueError):
        validate_mutations(_base(), {"added": [_mleader(text=text)]})


def test_mleader_edge_whitespace_message():
    with pytest.raises(ValueError, match="mleader text carries edge whitespace"):
        validate_mutations(_base(), {"added": [_mleader(text="  Valve")]})


def test_mleader_text_boundary_kept_verbatim():
    text = 'Valve "A" ' + "x" * 246
    assert len(text) == 256
    assert validate_mutations(_base(), {"added": [_mleader(text=text)]})["added"][0]["text"] == text


@pytest.mark.parametrize("style", ["NoSuchStyle", "", "x" * 256, "Bad|Style"])
def test_mleader_style_refusals(style):
    with pytest.raises(ValueError):
        validate_mutations(_base(), {"added": [_mleader(style=style)]})


def test_mleader_style_requires_one_segment():
    base = _base()
    base["mlstyles"][0]["segments"] = 2
    with pytest.raises(ValueError, match="mleader style must take exactly two points in this contract"):
        validate_mutations(base, {"added": [_mleader()]})


def test_mleader_legacy_catalogue_path():
    base = _base()
    del base["mlstyles"]
    assert validate_mutations(base, {"added": [_mleader(style="Custom")]})["added"][0]["style"] == "Custom"
    base["mlstyles"] = []
    with pytest.raises(ValueError, match="not loaded"):
        validate_mutations(base, {"added": [_mleader()]})
    line = {"handle": "line", "kind": "LINE", "layer": "0", "pts": [[0, 0, 0], [1, 0, 0]]}
    canonical = validate_mutations(base, {"added": [line]})
    assert not uses_v3(canonical)
    assert emit_plan(canonical, base_sha256=BASE_SHA) == (
        b"LEAF_MUTATION_PLAN|2\nBASE_SHA256|" + BASE_SHA.encode() + b"\nADDLINE|0|0,0,0|1,0,0\n")


@pytest.mark.parametrize("field,value", [("color", 3), ("linetype", "Continuous"), ("lineweight", 25)])
def test_mleader_add_properties_refused(field, value):
    with pytest.raises(ValueError, match="MLEADER carries no colour, linetype or lineweight in this contract"):
        validate_mutations(_base(), {"added": [_mleader(**{field: value})]})


@pytest.mark.parametrize("op,value", [
    ("set_color", {"aci": 3}), ("set_linetype", {"name": "Continuous"}),
    ("set_lineweight", {"weight": 25}), ("set_layer", {"layer": "SITE"}),
])
def test_mleader_property_target_refused(op, value):
    base = {**_base(), "mleaders": [{"handle": "AB", "layer": "0"}]}
    with pytest.raises(ValueError, match="MLEADER is not a property target in this contract"):
        validate_mutations(base, {op: [{"handle": "AB", **value}]})


def test_mleader_unknown_fields_refused():
    with pytest.raises(ValueError, match="unknown fields"):
        validate_mutations(_base(), {"added": [_mleader(height=0.18)]})


def test_mleader_group_ordinals_refused():
    plan = {"added": [_mleader(handle="z-leader"), _mleader(handle="a-leader")],
            "added_groups": [{"name": "PAIR", "members": [{"add": 0}, {"add": 1}]}]}
    with pytest.raises(ValueError, match="MLEADER is not a group member in this contract"):
        validate_mutations(_base(), plan)


# --- record 3s-1: command interpreter and only-when inspection ---------------

ML_RECORD = (
    "ML|9C76|0|Standard|Standard|0.18000|0.18000|0.36000|1|"
    "0.00000,0.00000,0.00000|5.00000,4.00000,0.00000|"
    "1.00000,0.00000,0.00000|5.45000,4.09147,0.00000|Valve")
MS_RECORD = "MS|Standard|Standard|0.18000|0.18000|0.36000|0.09000|1"


def test_mleader_interpreter_uses_command_and_restores_sysvars():
    import apply_lisp

    script = apply_lisp.build_apply_scr_v3()
    apply = next(line for line in script.splitlines()
                 if line.startswith("(defun leaf-apply-addmleader "))
    assert '(command "_.MLEADER" p1 p2 text)' in apply
    assert "(entmake" not in apply
    assert '(setq before (entlast))' in apply
    assert '(not (equal result before))' in apply and '"MULTILEADER"' in apply
    for variable, saved in (("CLAYER", "oldlayer"), ("CMLEADERSTYLE", "oldstyle")):
        assert f'(getvar "{variable}")' in apply
        assert f'(setvar "{variable}" {saved})' in apply
    assert apply.index('(setvar "CMLEADERSTYLE" oldstyle)') > apply.index('(command "_.MLEADER"')
    assert "ADDMLEADER" not in apply_lisp.build_apply_scr()


def test_mleader_op_validator_pins_closed_fields_text_and_dictionary():
    import apply_lisp

    script = apply_lisp.build_apply_scr_v3()
    op = next(line for line in script.splitlines()
              if line.startswith("(defun leaf-addmleader-op "))
    text_guard = next(line for line in script.splitlines()
                      if line.startswith("(defun leaf-mltext-p "))
    assert "(= (length v) 6)" in op
    assert "(<= (strlen layer) 255)" in op
    assert "(> (strlen style) 0)" in op and "(<= (strlen style) 255)" in op
    assert "(leaf-chars-ok style " in op
    assert "(leaf-point3 (nth 3 v))" in op and "(leaf-point3 (nth 4 v))" in op
    assert "(leaf-mltext-p text)" in op and "(leaf-mlstyle style)" in op
    assert "(> (strlen text) 0)" in text_guard and "(<= (strlen text) 256)" in text_guard
    # 92 is backslash; the range check also refuses CR and LF.
    assert "(list 37 92 124)" in text_guard and "(< c 32)" in text_guard
    assert '(dictsearch (namedobjdict) "ACAD_MLEADERSTYLE")' in script
    assert "(dictsearch (cdr (assoc -1 d)) style)" in script


def test_mleader_dispatch_tracks_created_and_refuses_property_targets():
    import apply_lisp

    script = apply_lisp.build_apply_scr_v3()
    assert '((= (car v) "ADDMLEADER") (leaf-addmleader-op v))' in script
    assert '((= (car op) "ADDMLEADER") (leaf-apply-addmleader op))' in script
    created = next(line for line in script.splitlines()
                   if line.startswith("(defun leaf-apply (op / result)"))
    target = next(line for line in script.splitlines()
                  if line.startswith("(defun leaf-target "))
    remove = next(line for line in script.splitlines()
                  if line.startswith("(defun leaf-remove-op "))
    assert '"ADDMLEADER"' in created and '"MULTILEADER"' in remove
    assert '"MULTILEADER"' not in target


def test_mleader_inspection_precedes_frozen_dimension_tail_and_stays_bounded():
    import apply_lisp
    from lisp import MAX_SCRIPT_LINE_CHARS, MUTATION_INSPECT_BLOCKS, build_scr

    blocks = MUTATION_INSPECT_BLOCKS
    assert '"DS|"' in blocks[-2] and '"DM|"' in blocks[-1]
    ms = next(i for i, line in enumerate(blocks) if '"MS|"' in line)
    ml = next(i for i, line in enumerate(blocks) if '"ML|"' in line)
    assert ms < ml < len(blocks) - 2
    assert '(strcat "MLX|" (cdr (assoc 5 data)))' in blocks[ml]
    assert 'cons 410 "Model"' in blocks[-3]
    for script in (build_scr(extra_blocks=blocks), apply_lisp.build_apply_scr_v3()):
        for line in script.splitlines():
            assert len(line) <= MAX_SCRIPT_LINE_CHARS
            assert line.count("(") == line.count(")"), line


def test_mleader_parse_probe_reports_derived_values():
    import intake_parse

    parsed = intake_parse.parse_text(MS_RECORD + "\n" + ML_RECORD, "probe.dwg")
    assert not parsed.get("parseErrors")
    assert parsed["mlstyles"] == _base()["mlstyles"]
    assert parsed["mleaders"] == [{
        "handle": "9C76", "layer": "0", "style": "Standard", "textstyle": "Standard",
        "height": 0.18, "arrow": 0.18, "dogleg": 0.36, "attachment": 1,
        "pts": [[0, 0, 0], [5, 4, 0]], "landing": [5, 4, 0],
        "dogleg_dir": [1, 0, 0], "textpt": [5.45, 4.091, 0], "text": "Valve",
    }]


def test_mleader_parse_decodes_catalogue_and_entity_percent_escapes():
    import intake_parse

    parsed = intake_parse.parse_text(
        (MS_RECORD + "\n" + ML_RECORD).replace("Standard", "A%257C%7CB")
        .replace("|0|", "|L%7C%25|").replace("|Valve", "|V%7C%25%0D%0A"), "probe.dwg")
    assert not parsed.get("parseErrors")
    assert parsed["mlstyles"][0]["name"] == "A%7C|B"
    assert parsed["mlstyles"][0]["textstyle"] == "A%7C|B"
    leader = parsed["mleaders"][0]
    assert leader["style"] == leader["textstyle"] == "A%7C|B"
    assert leader["layer"] == "L|%"
    assert leader["text"] == "V|%\r\n"


def test_mleader_parse_keys_are_only_when_records_exist():
    import intake_parse

    blank = intake_parse.parse_text("DS|Standard", "probe.dwg")
    assert not {"mlstyles", "mleaders", "mleaders_unsupported"} & blank.keys()
    styles = intake_parse.parse_text(MS_RECORD, "probe.dwg")
    assert "mlstyles" in styles and "mleaders" not in styles
    unsupported = intake_parse.parse_text("MLX|1\nMLX|1", "probe.dwg")
    assert unsupported["mleaders_unsupported"] == 2
    assert "mleaders" not in unsupported and "mlstyles" not in unsupported


@pytest.mark.parametrize("field,value", [
    (1, "ZZ"), (1, ""), (9, ""), (9, "0,0"), (10, "5,4"),
    (11, "1,0"), (12, "5,4,0,1"), (9, "0,0,0;"),
])
def test_mleader_parse_drops_malformed_records_like_dimensions(field, value):
    import intake_parse

    fields = ML_RECORD.split("|")
    fields[field] = value
    parsed = intake_parse.parse_text("|".join(fields), "probe.dwg")
    assert parsed.get("parseErrors")
    assert "mleaders" not in parsed


def test_mleader_parse_preserves_vertex_order_and_rounds_precision():
    import intake_parse

    fields = ML_RECORD.split("|")
    fields[9] = "0.0004,0,0;2.12349,3.0004,0"
    fields[5] = "0.1234567"
    parsed = intake_parse.parse_text("|".join(fields), "probe.dwg")
    assert not parsed.get("parseErrors")
    leader = parsed["mleaders"][0]
    assert leader["pts"] == [[0, 0, 0], [2.123, 3, 0], [5, 4, 0]]
    assert leader["height"] == 0.12346


# --- record 3s-2: DXF catalogue, nested leader contexts and round trip ------

def _mleader_dxf_fixture():
    return {"dwg": "probe.dxf", "layers": ["0"], "polylines": [],
            "memberEvidenceCovered": True, "mlstyles": _base()["mlstyles"],
            "mleaders": [{
                "handle": "9C76", "layer": "0", "style": "Standard",
                "textstyle": "Standard", "height": 0.18, "arrow": 0.18,
                "dogleg": 0.36, "attachment": 1, "pts": [[0, 0, 0], [5, 4, 0]],
                "landing": [5, 4, 0], "dogleg_dir": [1, 0, 0],
                "textpt": [5.45, 4.091, 0], "text": "Valve"}]}


def _dxf_records(raw, kind):
    from dxf_intake import _group_pairs

    pairs = _group_pairs(raw.decode())
    records = []
    for i, pair in enumerate(pairs):
        if pair == (0, kind):
            end = i + 1
            while end < len(pairs) and pairs[end][0] != 0:
                end += 1
            records.append(pairs[i:end])
    return records


def test_mleader_dxf_round_trip_probe_and_second_style():
    from dxf_intake import parse_dxf_bytes
    from intake_dxf import intake_to_dxf

    intake = _mleader_dxf_fixture()
    assert parse_dxf_bytes(intake_to_dxf(intake), source_name="probe.dxf") == intake
    intake["mlstyles"].append({"name": "Two segments", "textstyle": "Notes",
                               "height": 0.12345, "arrow": 0.23456,
                               "dogleg": 0.34567, "gap": 0.04567, "segments": 2})
    assert parse_dxf_bytes(intake_to_dxf(intake), source_name="probe.dxf") == intake


def test_mleader_dxf_probe_group_sequence_and_references():
    from intake_dxf import intake_to_dxf

    raw = intake_to_dxf(_mleader_dxf_fixture())
    ml, = _dxf_records(raw, "MULTILEADER")
    # probe6 entget sequence, expanded from point groups into DXF triples.
    expected = [int(c) for c in (
        "0 330 5 100 67 410 8 100 270 300 40 10 20 30 41 140 145 "
        "174 175 176 177 290 304 11 21 31 340 12 22 32 13 23 33 "
        "42 43 44 45 170 90 171 172 91 141 92 291 292 173 293 142 143 "
        "294 295 296 110 120 130 111 121 131 112 122 132 297 "
        "302 290 291 10 20 30 11 21 31 90 40 304 10 20 30 "
        "91 170 92 340 171 40 341 93 305 271 303 272 273 301 "
        "340 90 170 91 341 171 290 291 41 42 172 343 173 95 174 175 "
        "92 292 93 10 20 30 43 176 293 294 178 179 45 271 272 273 295"
    ).split()]
    assert [code for code, _ in ml] == expected
    context_end = ml.index((301, "}"))
    top = dict(ml[context_end + 1:])
    ms, = _dxf_records(raw, "MLEADERSTYLE")
    ts, = _dxf_records(raw, "STYLE")
    assert top[340] == dict(ms)[5]
    assert top[343] == dict(ts)[5] == dict(ms)[342]
    assert dict(ts)[2] == "Standard" and dict(ts)[3] == "arial.ttf"
    assert dict(ts)[40] == "0.0" and dict(ts)[41] == "1.0"
    assert dict(ms)[173] == "1" and dict(ms)[170] == "2"
    assert [code for code, _ in ms] == [int(c) for c in (
        "0 5 102 330 330 102 330 100 179 170 171 172 90 40 41 173 91 "
        "340 92 290 42 291 43 3 341 44 300 342 174 178 175 176 93 45 "
        "292 297 46 343 94 47 49 140 293 141 294 177 142 295 296 143 "
        "271 272 273 298").split()]
    line_start = ml.index((304, "LEADER_LINE{"))
    line_end = ml.index((305, "}"))
    line = dict(ml[line_start:line_end])
    assert line[340] == line[341] == "0"
    base_start = ml.index((300, "CONTEXT_DATA{")) + 2
    assert ml[base_start:base_start + 3] == [(10, "5.36"), (20, "4.0"), (30, "0.0")]
    dictionaries = _dxf_records(raw, "DICTIONARY")
    root = next(dict(r) for r in dictionaries if dict(r).get(330) == "0")
    assert root[3] == "ACAD_MLEADERSTYLE"
    catalogue = next(dict(r) for r in dictionaries if dict(r)[5] == root[350])
    assert catalogue[3] == "Standard" and catalogue[350] == dict(ms)[5]


@pytest.mark.parametrize("change", ["block", "absent_style", "missing_text", "two_lines"])
def test_mleader_dxf_unsupported_shapes_counted(change):
    from dxf_intake import parse_dxf_bytes
    from intake_dxf import intake_to_dxf

    raw = intake_to_dxf(_mleader_dxf_fixture())
    if change == "block":
        raw = raw.replace(b"172\n2\n343\n", b"172\n1\n343\n")
    elif change == "absent_style":
        style = dict(_dxf_records(raw, "MLEADERSTYLE")[0])[5].encode()
        raw = raw.replace(b"301\n}\n340\n" + style + b"\n",
                          b"301\n}\n340\nDEADBEEF\n")
    elif change == "missing_text":
        raw = raw.replace(b"304\nValve\n", b"")
    else:
        raw = raw.replace(b"305\n}\n", b"305\n}\n304\nLEADER_LINE{\n305\n}\n")
    result = parse_dxf_bytes(raw)
    assert result["mleaders_unsupported"] == 1
    assert "mleaders" not in result
    assert result["mlstyles"] == _base()["mlstyles"]


def test_mleader_dxf_only_when_and_styles_without_entities():
    from dxf_intake import parse_dxf_bytes
    from intake_dxf import intake_to_dxf

    blank = {"dwg": "probe.dxf", "layers": ["0"], "polylines": [],
             "memberEvidenceCovered": True}
    assert parse_dxf_bytes(intake_to_dxf(blank), source_name="probe.dxf") == blank
    styles = {**blank, "mlstyles": _base()["mlstyles"]}
    assert parse_dxf_bytes(intake_to_dxf(styles), source_name="probe.dxf") == styles


def test_mleader_dxf_preserves_vertex_order_and_inspection_precision():
    from dxf_intake import parse_dxf_bytes
    from intake_dxf import intake_to_dxf

    intake = _mleader_dxf_fixture()
    intake["mleaders"][0]["pts"].insert(1, [2.123, 3, 0])
    intake["mleaders"][0]["height"] = 0.12345
    assert parse_dxf_bytes(intake_to_dxf(intake), source_name="probe.dxf") == intake
    raw = intake_to_dxf(intake).replace(b"4.091\n", b"4.09147\n")
    assert parse_dxf_bytes(raw, source_name="probe.dxf") == intake


def test_mleader_dxf_points_share_total_bound(monkeypatch):
    import intake_dxf

    monkeypatch.setattr(intake_dxf, "MAX_POINTS", 1)
    with pytest.raises(intake_dxf.IntakeDxfError, match="points in total"):
        intake_dxf.intake_to_dxf(_mleader_dxf_fixture())


# --- record 3s-3: mock writer, actual-handle verification and upload binding --

@pytest.mark.parametrize("catalogue", [False, True])
def test_mleader_mock_placement_and_catalogue(catalogue):
    base = _base()
    if catalogue:
        base["mlstyles"][0].update(height=0.25, arrow=0.2, textstyle="Notes")
    else:
        del base["mlstyles"]
    result = write_loop.apply_mutations(base, {"added": [_mleader()]})
    expected = _mleader_dxf_fixture()["mleaders"][0]
    expected.update(handle="new-leader", textpt=[5.45, 4.125 if catalogue else 4.09, 0])
    if catalogue:
        expected.update(height=0.25, arrow=0.2, textstyle="Notes")
    assert result["mleaders"] == [expected]
    assert "mleaders" not in base


def test_mleader_mock_removal_and_unchanged_records():
    import copy

    base = _mleader_dxf_fixture()
    before = copy.deepcopy(base)
    plan = validate_mutations(base, {"removed": ["9C76"]})
    result = write_loop.apply_mutations(base, plan)
    assert result["mleaders"] == []
    assert base == before
    assert write_loop.verify_live_mutation_effects(base, result, plan) is None
    with pytest.raises(ValueError, match="removed MLEADER"):
        write_loop.verify_live_mutation_effects(base, before, plan)


def test_mleader_verifier_accepts_console_placement_and_binds_actual_handle():
    import intake_parse

    base = {**_base(), "mleaders": []}
    canonical = validate_mutations(base, {"added": [_mleader()]})
    actual = {**base, "mleaders": intake_parse.parse_text(ML_RECORD, "probe.dwg")["mleaders"]}
    assert actual["mleaders"][0]["textpt"] == [5.45, 4.091, 0]
    assert write_loop.verify_live_mutation_effects(base, actual, canonical) is None
    matched = {}
    write_loop._verify_mleader_effects(base, actual, canonical, matched)
    assert matched == {"new-leader": "9C76"}


@pytest.mark.parametrize("damage", ["vertex", "text", "missing", "extra", "style", "layer"])
def test_mleader_verifier_refuses_wrong_add(damage):
    import copy

    base = {**_base(), "mleaders": []}
    canonical = validate_mutations(base, {"added": [_mleader()]})
    actual = _mleader_dxf_fixture()
    row = actual["mleaders"][0]
    if damage == "vertex":
        row["pts"][0][0] = 0.001
    elif damage in ("text", "style", "layer"):
        row[damage] = "Changed"
    elif damage == "missing":
        actual["mleaders"] = []
    else:
        actual["mleaders"].append({**copy.deepcopy(row), "handle": "AB"})
    with pytest.raises(ValueError, match="MLEADER"):
        write_loop.verify_live_mutation_effects(base, actual, canonical)


@pytest.mark.parametrize("field,value", [("textpt", [5.45, 4.09, 0]),
                                        ("height", 0.2), ("text", "Changed")])
def test_mleader_verifier_unchanged_record_is_exact(field, value):
    import copy

    base = _mleader_dxf_fixture()
    actual = copy.deepcopy(base)
    assert write_loop.verify_live_mutation_effects(base, actual, {}) is None
    actual["mleaders"][0][field] = value
    with pytest.raises(ValueError, match="unchanged MLEADER"):
        write_loop.verify_live_mutation_effects(base, actual, {})


def test_mleader_verifier_legacy_base_proves_only_adds():
    base = _base()
    canonical = validate_mutations(base, {"added": [_mleader()]})
    actual = _mleader_dxf_fixture()
    actual["mleaders"].append({**actual["mleaders"][0], "handle": "AB", "text": "Existing"})
    assert write_loop.verify_live_mutation_effects(base, actual, canonical) is None
    actual["mleaders"] = actual["mleaders"][1:]
    with pytest.raises(ValueError, match="added MLEADER"):
        write_loop.verify_live_mutation_effects(base, actual, canonical)


def test_mleader_verifier_matches_points_at_three_decimals():
    base = {**_base(), "mleaders": []}
    canonical = validate_mutations(base, {"added": [_mleader()]})
    actual = _mleader_dxf_fixture()
    actual["mleaders"][0]["pts"][0][0] = 0.0004
    assert write_loop.verify_live_mutation_effects(base, actual, canonical) is None


@pytest.mark.parametrize("damage", [False, True, "unsupported_add", "unsupported_remove", "bad_style"])
def test_mleader_uploaded_plan_result_binding(tmp_path, monkeypatch, damage):
    import hashlib
    import io
    import json
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import deps
    import store
    from envelopes import install_error_handlers
    from intake_dxf import intake_to_dxf
    from routers import drawings

    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "drawings"))
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.setattr(deps, "APS_LIVE", False)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(drawings.router)
    client = TestClient(app)
    tenant, drawing = "tenant-mleader-save", "mleader-save"
    head = {**_base(), "mleaders": [], "mleaders_unsupported": 0}
    plan = {"added": [_mleader(handle="301")]}
    if damage == "unsupported_remove":
        head = _mleader_dxf_fixture()
        plan = {"removed": ["9C76"]}
    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    source = tmp_path / "base.dwg"
    source.write_bytes(b"AC1032" + b"\x00" * 64)
    store.ingest_drawing(backend, tenant, str(source), drawing_id=drawing)
    write_loop.publish_intake_cache(backend, tenant, drawing, 1, source.read_bytes(), head)
    upload = write_loop.apply_mutations(head, plan)
    if damage == "unsupported_remove":
        upload = _mleader_dxf_fixture()
    upload["mleaders"][0]["textpt"] = [5.45, 4.091, 0]
    if damage is True:
        upload["mleaders"][0]["text"] = "Wrong valve"
    if damage == "unsupported_add":
        upload["mleaders"].append({**upload["mleaders"][0], "handle": "302"})
    data = intake_to_dxf(upload)
    if damage == "unsupported_add":
        # Keep the matching add supported; only the extra entity changes content type.
        start = data.index(b"5\n302\n")
        data = data[:start] + data[start:].replace(b"172\n2\n343\n", b"172\n1\n343\n", 1)
    elif damage == "unsupported_remove":
        data = data.replace(b"172\n2\n343\n", b"172\n1\n343\n")
    elif damage == "bad_style":
        data = data.replace(b"45\n0.18\n", b"45\noops\n")
    checkout = client.post(f"/api/drawings/{drawing}/checkout", headers={"X-Tenant-Id": tenant},
                           json={"holder": "mleader-editor", "ttl_s": 3600})
    assert checkout.status_code == 200, checkout.text
    response = client.post(f"/api/drawings/{drawing}/versions/plan",
        headers={"X-Tenant-Id": tenant,
                 "X-Checkout-Capability": checkout.json()["checkout_capability"]},
        files={"file": ("edited.dxf", io.BytesIO(data), "application/dxf")},
        data={"parent_version": "1", "source_digest": hashlib.sha256(data).hexdigest(),
              "plan": json.dumps({"mutations": plan})})
    assert response.status_code == (422 if damage else 201), response.text
    if damage:
        if damage != "bad_style":
            assert "uploaded DXF does not carry the plan's result" in response.text
        assert store.resolve_version(backend, tenant, drawing, "head")[0] == 1


# --- record 3s-4: correction round one ------------------------------------

@pytest.mark.parametrize("base_count,actual_count", [(0, 1), (2, 1), (1, 1)])
def test_mleader_unsupported_count_is_part_of_effect(base_count, actual_count):
    base = {**_base(), "mleaders": [], "mleaders_unsupported": base_count}
    canonical = validate_mutations(base, {"added": [_mleader()]})
    actual = {**_mleader_dxf_fixture(), "mleaders_unsupported": actual_count}
    if base_count == actual_count:
        assert write_loop.verify_live_mutation_effects(base, actual, canonical) is None
    else:
        with pytest.raises(ValueError, match="unsupported MULTILEADER"):
            write_loop.verify_live_mutation_effects(base, actual, canonical)


def test_mleader_mock_materialises_missing_catalogue_and_reopens():
    from dxf_intake import parse_dxf_bytes
    from intake_dxf import intake_to_dxf

    base = _base()
    del base["mlstyles"]
    result = write_loop.apply_mutations(base, {"added": [_mleader(handle="301", style="Custom")]})
    assert result["mlstyles"] == [{**_base()["mlstyles"][0], "name": "Custom"}]
    reopened = parse_dxf_bytes(intake_to_dxf(result))
    assert reopened["mleaders"] == result["mleaders"]
    assert "mlstyles" not in base


def test_mleader_mock_keeps_existing_catalogue():
    import copy

    base = _base()
    base["mlstyles"][0]["height"] = 0.25
    catalogue = copy.deepcopy(base["mlstyles"])
    result = write_loop.apply_mutations(base, {"added": [_mleader()]})
    assert result["mlstyles"] == catalogue
    assert base["mlstyles"] == catalogue


def test_mleader_existing_group_member_refused():
    base = _mleader_dxf_fixture()
    with pytest.raises(ValueError, match="MLEADER is not a group member in this contract"):
        validate_mutations(base, {"added": [_mleader()], "added_groups": [
            {"name": "PAIR", "members": ["9C76", {"add": 0}]}]})


def test_mleader_lisp_contains_ucs_catch_frozen_guard_and_restore_order():
    import apply_lisp
    import lisp

    script = apply_lisp.build_apply_scr_v3()
    assert "(vl-load-com)" in script
    apply = next(line for line in script.splitlines()
                 if line.startswith("(defun leaf-apply-addmleader "))
    assert "(trans p1 0 1)" in apply and "(trans p2 0 1)" in apply
    assert '(tblsearch "LAYER" layer)' in apply
    assert '(logand 1 (cdr (assoc 70 layerdata)))' in apply
    assert apply.index('(assoc 70 layerdata)') < apply.index('(setvar ')
    assert '(vl-catch-all-apply (function (lambda ()' in apply
    for variable, saved in (("CLAYER", "oldlayer"), ("CMLEADERSTYLE", "oldstyle")):
        assert (apply.index('(command "_.MLEADER"')
                < apply.index(f'(setvar "{variable}" {saved})')
                < apply.index('(vl-catch-all-error-p caught)'))
    row = next(line for line in lisp.MUTATION_INSPECT_BLOCKS
               if line.startswith("(defun leaf-ml-row "))
    assert '(if (and style (= branches 1)' in row
    assert '(write-line (strcat "MLX|" (cdr (assoc 5 data))) f)' in row


@pytest.mark.parametrize("record,index,key", [(MS_RECORD, 3, "mlstyles"), (ML_RECORD, 5, "mleaders")])
@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_mleader_nonfinite_scalars_are_parse_errors(record, index, key, value):
    import intake_parse

    fields = record.split("|")
    fields[index] = value
    result = intake_parse.parse_text("|".join(fields), "probe.dwg")
    assert result.get("parseErrors")
    assert not result.get(key)


@pytest.mark.parametrize("code,value", [(45, "oops"), (45, "nan"), (173, "inf")])
def test_mleader_style_bad_numbers_raise_dxf_parse_error(code, value):
    from dxf_intake import DxfParseError, parse_dxf_bytes
    from intake_dxf import intake_to_dxf

    raw = intake_to_dxf(_mleader_dxf_fixture())
    original = dict(_dxf_records(raw, "MLEADERSTYLE")[0])[code]
    raw = raw.replace(f"{code}\n{original}\n".encode(), f"{code}\n{value}\n".encode())
    with pytest.raises(DxfParseError, match="numeric"):
        parse_dxf_bytes(raw)


@pytest.mark.parametrize("needle,replacement", [
    (b"41\n0.18\n", b"41\nnan\n"),
    (b"140\n0.18\n", b"140\noops\n"),
    (b"40\n0.36\n", b"40\ninf\n"),
    (b"20\n4.0\n", b"20\n-inf\n"),
])
def test_mleader_entity_bad_numbers_raise_dxf_parse_error(needle, replacement):
    from dxf_intake import DxfParseError, parse_dxf_bytes
    from intake_dxf import intake_to_dxf

    raw = intake_to_dxf(_mleader_dxf_fixture())
    assert needle in raw
    with pytest.raises(DxfParseError, match="numeric"):
        parse_dxf_bytes(raw.replace(needle, replacement))


# --- record 3s-5: sidecar scope, raw inventory and layer override ----------

def test_mleader_unsupported_handles_survive_both_extractors():
    import intake_parse
    import lisp
    from dxf_intake import parse_dxf_bytes
    from intake_dxf import intake_to_dxf

    raw = intake_to_dxf(_mleader_dxf_fixture()).replace(
        b"172\n2\n343\n", b"172\n1\n343\n")
    assert parse_dxf_bytes(raw)["mleaders_unsupported_handles"] == ["9C76"]
    parsed = intake_parse.parse_text("MLX|AB\nMLX|9C76\nMLX|1", "probe.dwg")
    assert parsed["mleaders_unsupported"] == 3
    assert parsed["mleaders_unsupported_handles"] == ["AB", "9C76"]
    legacy = intake_parse.parse_text("MLX|1", "probe.dwg")
    assert legacy["mleaders_unsupported"] == 1
    assert "mleaders_unsupported_handles" not in legacy
    assert "mleaders_unsupported_handles" not in intake_parse.parse_text("", "probe.dwg")
    row = next(line for line in lisp.MUTATION_INSPECT_BLOCKS
               if line.startswith("(defun leaf-ml-row "))
    assert '(strcat "MLX|" (cdr (assoc 5 data)))' in row


@pytest.mark.parametrize("damage", ["removed_survives", "inventory_swap", "genuine"])
def test_mleader_raw_inventory_verifies_removal(damage):
    import copy

    base = {**_mleader_dxf_fixture(), "mleaders_unsupported": 1,
            "mleaders_unsupported_handles": ["AB"]}
    # An empty plan is refused by the contract, so the swap case carries an
    # unrelated LINE add; every case derives its actual through the mock
    # writer first, then applies the damage to the unsupported inventory.
    canonical = validate_mutations(
        base, {"added": [{"handle": "301", "kind": "LINE", "layer": "0",
                          "pts": [[0, 0, 0], [1, 0, 0]]}]}
        if damage == "inventory_swap" else {"removed": ["9C76"]})
    actual = write_loop.apply_mutations(copy.deepcopy(base), canonical)
    actual.setdefault("mleaders_unsupported", base["mleaders_unsupported"])
    actual.setdefault("mleaders_unsupported_handles", list(base["mleaders_unsupported_handles"]))
    if damage != "genuine":
        actual["mleaders_unsupported_handles"] = [
            "9C76" if damage == "removed_survives" else "CD"]
        with pytest.raises(ValueError, match=(
                "removed MLEADER" if damage == "removed_survives"
                else "unsupported MULTILEADER inventory changed")):
            write_loop.verify_live_mutation_effects(base, actual, canonical)
    else:
        assert write_loop.verify_live_mutation_effects(base, actual, canonical) is None


def test_mleader_legacy_base_accepts_unrelated_line_and_actual_unsupported():
    base = {"layers": ["0"], "polylines": []}
    canonical = validate_mutations(base, {"added": [{
        "handle": "301", "kind": "LINE", "layer": "0",
        "pts": [[0, 0, 0], [1, 0, 0]]}]})
    actual = write_loop.apply_mutations(base, canonical)
    actual.update(mleaders_unsupported=1, mleaders_unsupported_handles=["AB"])
    assert write_loop.verify_live_mutation_effects(base, actual, canonical) is None


def test_mleader_materialised_catalogue_canonicalises_both_spellings():
    from dxf_intake import parse_dxf_bytes
    from intake_dxf import intake_to_dxf

    base = _base()
    del base["mlstyles"]
    result = write_loop.apply_mutations(base, {"added": [
        _mleader(handle="301", style="Custom"),
        _mleader(handle="302", style="custom")]})
    assert [s["name"] for s in result["mlstyles"]] == ["Custom"]
    assert [e["style"] for e in result["mleaders"]] == ["Custom", "Custom"]
    assert parse_dxf_bytes(intake_to_dxf(result))["mleaders"] == result["mleaders"]


def test_mleader_lisp_saves_pins_and_restores_mleaderlayer():
    import apply_lisp

    apply = next(line for line in apply_lisp.build_apply_scr_v3().splitlines()
                 if line.startswith("(defun leaf-apply-addmleader "))
    assert (apply.index('oldmleaderlayer (getvar "MLEADERLAYER")')
            < apply.index('(vl-catch-all-apply')
            < apply.index('(setvar "MLEADERLAYER" ".")')
            < apply.index('(command "_.MLEADER"')
            < apply.index('(setvar "MLEADERLAYER" oldmleaderlayer)')
            < apply.index('(vl-catch-all-error-p caught)'))


@pytest.mark.parametrize("case", ["empty_sidecar", "intake_sidecar",
                                   "removed_survives", "genuine"])
def test_mleader_save_scope_and_raw_inventory(tmp_path, monkeypatch, case):
    import hashlib
    import io
    import json
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import deps
    import store
    from envelopes import install_error_handlers
    from intake_dxf import intake_to_dxf
    from routers import drawings

    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "drawings"))
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.setattr(deps, "APS_LIVE", False)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(drawings.router)
    client = TestClient(app)
    tenant, drawing = "tenant-mleader-save", "mleader-save"
    head = {**_mleader_dxf_fixture(), "mleaders_unsupported": 1,
            "mleaders_unsupported_handles": ["AB"]}
    plan = {"removed": ["9C76"]}
    upload = _mleader_dxf_fixture()
    if case == "genuine":
        upload["mleaders"][0]["handle"] = "AB"
    data = intake_to_dxf(upload).replace(b"172\n2\n343\n", b"172\n1\n343\n")
    if case.endswith("sidecar"):
        head = _base()
        plan = {} if case == "empty_sidecar" else {"added": [{
            "handle": "301", "kind": "LINE", "layer": "0",
            "pts": [[0, 0, 0], [1, 0, 0]]}]}
        data = intake_to_dxf(write_loop.apply_mutations(head, plan))
    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    source = tmp_path / "base.dwg"
    source.write_bytes(json.dumps(head).encode() if case == "intake_sidecar"
                       else b"AC1032" + b"\x00" * 64)
    store.ingest_drawing(backend, tenant, str(source), drawing_id=drawing)
    write_loop.publish_intake_cache(backend, tenant, drawing, 1, source.read_bytes(), head)
    checkout = client.post(f"/api/drawings/{drawing}/checkout",
        headers={"X-Tenant-Id": tenant}, json={"holder": "editor", "ttl_s": 3600})
    assert checkout.status_code == 200, checkout.text
    response = client.post(f"/api/drawings/{drawing}/versions/plan",
        headers={"X-Tenant-Id": tenant,
                 "X-Checkout-Capability": checkout.json()["checkout_capability"]},
        files={"file": ("edited.dxf", io.BytesIO(data), "application/dxf")},
        data={"parent_version": "1", "source_digest": hashlib.sha256(data).hexdigest(),
              "plan": json.dumps({"mutations": plan})})
    assert response.status_code == (422 if case == "removed_survives" else 201), response.text
    if case.endswith("sidecar"):
        assert response.json()["commit"] == "dxf-sidecar"
    elif case == "removed_survives":
        assert "removed MLEADER" in response.text
        assert store.resolve_version(backend, tenant, drawing, "head")[0] == 1
