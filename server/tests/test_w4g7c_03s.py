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


def test_mleader_ordinal_follows_canonical_added_order():
    plan = {"added": [_mleader(handle="z-leader"), _mleader(handle="a-leader")],
            "added_groups": [{"name": "PAIR", "members": [{"add": 0}, {"add": 1}]}]}
    canonical = validate_mutations(_base(), plan)
    assert [entity["handle"] for entity in canonical["added"]] == ["a-leader", "z-leader"]
    assert canonical["added_groups"][0]["members"] == [{"add": 1}, {"add": 0}]
    assert emit_plan(canonical, base_sha256=BASE_SHA).endswith(b"ADDGROUP|PAIR|A:1;A:0\n")
    assert validate_mutations(_base(), canonical) == canonical
