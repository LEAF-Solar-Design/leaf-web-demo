"""W4g-7b: existing block INSERTs through the server's contract v3 path."""
from __future__ import annotations

import copy
import hashlib
import math
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
from mutation_plan import canonical_json_bytes, emit_plan, uses_v3, validate_mutations


BASE_SHA = "1" * 64
INSERT_LINE = b"ADDINSERT|0|Fixture|10.000,20.000,0.000|90.000000|2.0000,3.0000,1.0000\n"
ACCORECONSOLE = Path(r"C:\Program Files\Autodesk\AutoCAD 2026\accoreconsole.exe")


def _base():
    return {
        "dwg": "upload.dxf", "layers": ["0"], "polylines": [], "inserts": [],
        "blocks": {
            "Fixture": {
                "base": [0.0, 0.0, 0.0], "count": 1, "complete": True,
                "children": [{"kind": "LINE", "layer": "0",
                              "pts": [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]]}],
            },
        },
    }


def _add(**changes):
    return {
        "handle": "new-insert", "kind": "INSERT", "name": "Fixture", "layer": "0",
        "pt": [10, 20, 0], "rot": 90, "scale": [2, 3, 1], **changes,
    }


def _insert(handle="2A"):
    # The intake's existing shape is x/y/z and nrm, not the plan's pt.
    return {
        "name": "Fixture", "layer": "0", "x": 10.0, "y": 20.0, "z": 0.0,
        "rot": 1.570796, "scale": [2.0, 3.0, 1.0], "nrm": [0.0, 0.0, 1.0],
        "handle": handle,
    }


def _actual():
    return {**_base(), "inserts": [_insert()]}


@pytest.mark.parametrize("rotation", [90, 450, -270, 3690])
def test_insert_canonical_form_normalizes_degrees(rotation):
    canonical = validate_mutations(_base(), {"added": [_add(rot=rotation)]})
    assert canonical == {"added": [{
        "kind": "INSERT", "name": "Fixture", "handle": "new-insert", "layer": "0",
        "pt": [10.0, 20.0, 0.0], "rot": 90.0, "scale": [2.0, 3.0, 1.0],
    }]}
    assert uses_v3(canonical)
    assert validate_mutations(_base(), canonical) == canonical


def test_insert_quantizes_all_numbers_and_keeps_mirrored_scales():
    canonical = validate_mutations(_base(), {"added": [_add(
        pt=[10.12349, 20.98761, -0.00001], rot=450.12345678,
        scale=[-2.123456, 3.987654, 1.000001],
    )]})
    entity = canonical["added"][0]
    assert entity["pt"] == [10.123, 20.988, 0.0]
    assert entity["rot"] == 90.123457
    assert entity["scale"] == [-2.1235, 3.9877, 1.0]
    assert b"-0.0" not in canonical_json_bytes(canonical)
    wrapped = validate_mutations(_base(), {"added": [_add(rot=359.9999999)]})
    assert wrapped["added"][0]["rot"] == 0.0


@pytest.mark.parametrize("definition,message", [
    (None, "block Fixture is not defined in this drawing"),
    ({"complete": False}, "block Fixture is incomplete in this drawing"),
    ({"complete": True, "baseUnknown": True}, "block Fixture is incomplete in this drawing"),
    ({"count": 2}, "block Fixture is incomplete in this drawing"),
    ({"count": 0}, "block Fixture is incomplete in this drawing"),
])
def test_insert_refuses_unavailable_definition(definition, message):
    base = _base()
    if definition is None:
        base["blocks"] = {}
    else:
        base["blocks"]["Fixture"].update(definition)
    with pytest.raises(ValueError, match=f"^{message}$"):
        validate_mutations(base, {"added": [_add()]})


@pytest.mark.parametrize("changes,message", [
    ({"scale": [0, 1, 1]}, "non-zero"),
    ({"scale": [1, 0, 1]}, "non-zero"),
    ({"scale": [1, 1, -0.0]}, "non-zero"),
    ({"scale": [0.000001, 1, 1]}, "non-zero"),
    ({"scale": [1e10, 1, 1]}, "supported range"),
    ({"scale": [1, float("nan"), 1]}, "finite"),
    ({"scale": [1, 1]}, "three components"),
    ({"pt": [1, 2]}, "three components"),
    ({"pt": [1, 2, float("inf")]}, "finite"),
    ({"pt": [1e10, 2, 3]}, "supported range"),
    ({"rot": float("nan")}, "finite"),
    ({"rot": True}, "must be a number"),
    ({"name": "*U1"}, "system or anonymous"),
    ({"name": "*Model_Space"}, "system or anonymous"),
    ({"name": "bad|name"}, "safe block name"),
    ({"name": "bad\rname"}, "safe block name"),
    ({"name": "bad\nname"}, "safe block name"),
    ({"name": ""}, "safe block name"),
    ({"name": "x" * 256}, "safe block name"),
    ({"layer": "bad|layer"}, "safe layer name"),
    ({"pts": [[0, 0], [1, 1]]}, "unknown fields"),
])
def test_insert_refuses_invalid_fields(changes, message):
    with pytest.raises(ValueError, match=message):
        validate_mutations(_base(), {"added": [_add(**changes)]})


def test_dimension_remains_disabled():
    with pytest.raises(ValueError, match="^contract v3 is not enabled on this deployment$"):
        validate_mutations(_base(), {"added": [_add(kind="DIMENSION")]})


def test_insert_plan_line_is_byte_exact_and_add_order_is_canonical():
    canonical = validate_mutations(_base(), {"added": [_add()]})
    # Existing CIRCLE/ARC fields use .12g; only INSERT uses fixed 3/6/4
    # decimals, as specified by its coordinate/angle/scale reading.
    assert emit_plan(canonical, base_sha256=BASE_SHA) == (
        b"LEAF_MUTATION_PLAN|3\n" + f"BASE_SHA256|{BASE_SHA}\n".encode() + INSERT_LINE)
    first = _add(handle="z", pt=[1, 2, 3])
    second = _add(handle="a")
    left = validate_mutations(_base(), {"added": [first, second]})
    right = validate_mutations(_base(), {"added": [second, first]})
    assert left["added"] == sorted(left["added"], key=canonical_json_bytes)
    assert emit_plan(left, base_sha256=BASE_SHA) == emit_plan(right, base_sha256=BASE_SHA)


def test_v2_only_plan_bytes_are_frozen():
    canonical = validate_mutations(_base(), {"added": [
        {"handle": "n1", "kind": "LINE", "layer": "0", "pts": [[0, 0], [3, 4]]},
        {"handle": "n2", "kind": "CIRCLE", "layer": "0", "c": [1, 2], "r": 0.5},
    ]})
    assert uses_v3(canonical) is False
    assert emit_plan(canonical, base_sha256=BASE_SHA) == (
        b"LEAF_MUTATION_PLAN|2\n" + f"BASE_SHA256|{BASE_SHA}\n".encode()
        + b"ADDCIRCLE|0|1,2,0|0.5\nADDLINE|0|0,0,0|3,4,0\n")


def test_mock_insert_uses_intake_shape_and_dxf_maps_its_temporary_handle():
    base = _base()
    before = copy.deepcopy(base)
    result = write_loop.apply_mutations(base, {"added": [_add()]})
    assert result["inserts"] == [_insert("new-insert")]
    assert base == before
    # Like other mock adds, the temp handle persists until DXF synthesis,
    # whose existing fresh-handle allocator maps it to the first free hex.
    parsed = dxf_intake.parse_dxf_bytes(intake_dxf.intake_to_dxf(result))
    assert parsed["inserts"] == [_insert("100")]
    assert parsed["blocks"] == base["blocks"]
    result["inserts"][0]["handle"] = "A1"
    assert dxf_intake.parse_dxf_bytes(intake_dxf.intake_to_dxf(result)) == result


def test_verifier_binds_added_insert_to_actual_handle(monkeypatch):
    canonical = validate_mutations(_base(), {"added": [_add()]})
    expected = write_loop.apply_mutations(_base(), canonical)
    monkeypatch.setattr(write_loop, "apply_mutations", lambda *_: expected)
    write_loop.verify_live_mutation_effects(_base(), _actual(), canonical)
    assert expected["inserts"][0]["handle"] == "2A"
    assert canonical["added"][0]["handle"] == "new-insert"


@pytest.mark.parametrize("changes", [
    {"rot": round(math.radians(91), 6)},
    {"rot": math.radians(90) - 2e-5}, {"rot": math.radians(90) + 2e-5},
    {"name": "fixture"}, {"layer": "Other"},
    {"x": 10.01}, {"scale": [2.01, 3.0, 1.0]}, {"nrm": [0.0, 0.0, -1.0]},
])
def test_verifier_refuses_added_insert_with_wrong_name_or_geometry(changes):
    actual = _actual()
    actual["inserts"][0].update(changes)
    canonical = validate_mutations(_base(), {"added": [_add()]})
    with pytest.raises(ValueError, match="^added INSERT Fixture not found in output$"):
        write_loop.verify_live_mutation_effects(_base(), actual, canonical)


def test_verifier_refuses_missing_insert_and_cannot_reuse_one_match():
    canonical = validate_mutations(_base(), {"added": [_add()]})
    with pytest.raises(ValueError, match="^added INSERT Fixture not found in output$"):
        write_loop.verify_live_mutation_effects(_base(), _base(), canonical)
    canonical = validate_mutations(_base(), {"added": [_add(), _add(handle="second")]})
    with pytest.raises(ValueError, match="^added INSERT Fixture not found in output$"):
        write_loop.verify_live_mutation_effects(_base(), _actual(), canonical)


def test_verifier_accepts_insert_reading_tolerances_and_preserves_unchanged_inserts():
    base = _base()
    base["inserts"] = [_insert("A1")]
    actual = _actual()
    actual["inserts"].append(_insert("A1"))
    actual["inserts"][0].update(x=10.0004, rot=1.5707965, scale=[2.00004, 3.0, 1.0])
    canonical = validate_mutations(base, {"added": [_add()]})
    write_loop.verify_live_mutation_effects(base, actual, canonical)
    actual["inserts"][1]["x"] += 1
    with pytest.raises(ValueError, match="unchanged INSERT"):
        write_loop.verify_live_mutation_effects(base, actual, canonical)


@pytest.mark.parametrize("name", ["Caf\u00e9", "Cafe\t", "Cafe\x7f"])
def test_insert_refuses_names_outside_printable_ascii(name):
    base = _base()
    base["blocks"][name] = base["blocks"].pop("Fixture")
    with pytest.raises(ValueError, match="^block names outside printable ASCII are not carried in this round$"):
        validate_mutations(base, {"added": [_add(name=name)]})


def test_insert_accepts_printable_ascii_name():
    base = _base()
    base["blocks"]["Cafe"] = base["blocks"].pop("Fixture")
    assert validate_mutations(base, {"added": [_add(name="Cafe")]})["added"][0]["name"] == "Cafe"


def test_eof_truncated_catalogue_is_incomplete_and_refused():
    parsed = intake_parse.parse_text(
        "BK|Fixture|0,0,0|2|1\nBKE|Fixture|LINE|0,0,0|1,0,0|0", "test.dwg")
    assert parsed["blocks"]["Fixture"]["complete"] is False
    assert len(parsed["blocks"]["Fixture"]["children"]) == 1
    with pytest.raises(ValueError, match="^block Fixture is incomplete in this drawing$"):
        validate_mutations(parsed, {"added": [_add()]})


@pytest.mark.parametrize("delta", [-1e-5, 1e-5])
def test_added_insert_accepts_one_rotation_reading_quantum(delta):
    actual = _actual()
    actual["inserts"][0]["rot"] = math.radians(90) + delta
    canonical = validate_mutations(_base(), {"added": [_add()]})
    write_loop.verify_live_mutation_effects(_base(), actual, canonical)


def test_legacy_and_v3_keep_the_same_unchanged_insert_rotation_reading():
    from lisp import build_scr

    legacy = build_scr()
    v3 = mutation_apply.activity_spec(3)["settings"]["inspectScript"]["value"]
    reading = '(rtos (cond (rot rot)(T 0.0)) 2 5)'
    assert reading in legacy and reading in v3
    assert '(rtos (cond (rot rot)(T 0.0)) 2 6)' not in v3
    record = "IN|Fixture|0|10,20,0|{rot}|0,0,1|2,3,1|A1"
    base = _base()
    base["inserts"] = intake_parse.parse_text(
        record.format(rot=f"{1.2345646:.5f}"), "test.dwg")["inserts"]
    actual = _actual()
    actual["inserts"] += copy.deepcopy(base["inserts"])
    canonical = validate_mutations(base, {"added": [_add()]})
    write_loop.verify_live_mutation_effects(base, actual, canonical)


@pytest.mark.parametrize("kind", ["INSERT", "LINE", "CIRCLE", "ARC"])
def test_added_entities_match_nearest_unconsumed_candidate(kind, monkeypatch):
    adds = []
    for index in range(3):
        x = index * 0.001
        common = {"handle": f"add-{index}", "kind": kind, "layer": "0"}
        if kind == "INSERT":
            entity = _add(**common, pt=[x, 0, 0])
        elif kind == "LINE":
            entity = {**common, "pts": [[x, 0, 0], [x, 1, 0]]}
        else:
            entity = {**common, "c": [x, 0, 0], "r": 1}
            if kind == "ARC":
                entity.update(start_deg=0, end_deg=90)
        adds.append(entity)
    base = _base()
    canonical = validate_mutations(base, {"added": adds})
    expected = write_loop.apply_mutations(base, canonical)
    actual = copy.deepcopy(expected)
    field = {"INSERT": "inserts", "LINE": "polylines", "CIRCLE": "circles", "ARC": "arcs"}[kind]
    actual[field].reverse()
    for index, entity in enumerate(actual[field]):
        entity["handle"] = f"A{index}"
    monkeypatch.setattr(write_loop, "apply_mutations", lambda *_: expected)
    write_loop.verify_live_mutation_effects(base, actual, canonical)
    if kind == "INSERT":
        assert [entity["handle"] for entity in expected[field]] == ["A2", "A1", "A0"]


def test_v3_interpreter_guards_insert_and_keeps_the_v2_script_snapshot():
    script = mutation_apply.build_apply_scr_v3()
    parser = next(line for line in script.splitlines() if line.startswith("(defun leaf-addinsert-op "))
    apply = next(line for line in script.splitlines() if line.startswith("(defun leaf-apply-addinsert "))
    assert '(= (length v) 6)' in parser
    assert '(tblsearch "BLOCK" name)' in parser
    assert '(leaf-number (nth 4 v))' in parser
    assert '(leaf-point3 (nth 3 v))' in parser
    assert '(leaf-point3 (nth 5 v))' in parser
    assert apply.index('(tblsearch "BLOCK" name)') < apply.index("(leaf-ensure-layer layer)")
    assert apply.index("(leaf-ensure-layer layer)") < apply.index("(entmakex ")
    assert '(cons 50 (/ (* rot pi) 180.0))' in apply
    for field in ('(cons 2 name)', '(cons 10 pt)', '(cons 41 sx)', '(cons 42 sy)', '(cons 43 sz)'):
        assert field in apply
    assert '(eval ' not in script and '(read ' not in script
    v2 = apply_lisp.build_apply_scr()
    assert "ADDINSERT" not in v2
    # Captured from build_apply_scr's frozen literal lines at 81e5d234.
    assert hashlib.sha256(v2.encode("utf-8")).hexdigest() == (
        "a7ed0bb7dbd8266404574b523550d8103318981c47a927a6ad4c9daab07f6c35")


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
def test_accoreconsole_insert_canary_reopens_and_verifies_the_output(tmp_path):
    # Same local binary and tracked seed as da/test_mutation_apply_accoreconsole.py.
    host = tmp_path / "host.dwg"
    shutil.copyfile(ROOT / "data" / "rooftop_demo.dwg", host)
    # Seed the definition before the plan is read, in the same console session.
    # Let the production apply script provide the final SAVEAS and QUIT lines.
    setup = "\r\n".join([
        '(setvar "CMDECHO" 0)',
        '(setvar "FILEDIA" 0)',
        '(entmake (list (cons 0 "BLOCK") (cons 2 "Fixture") (cons 70 0) (cons 10 (list 0.0 0.0 0.0))))',
        '(entmake (list (cons 0 "LINE") (cons 8 "0") (cons 10 (list 0.0 0.0 0.0)) (cons 11 (list 1.0 0.0 0.0))))',
        '(entmake (list (cons 0 "ENDBLK")))',
        "",
    ])
    settings = mutation_apply.activity_spec(3)["settings"]
    inspect = settings["inspectScript"]["value"]
    quit_line = '(command "_.QUIT" "_Y")\r\n'
    assert inspect.endswith(quit_line)
    before = inspect[:-len(quit_line)].replace("output-intake.txt", "base-intake.txt")
    canonical = validate_mutations(_base(), {"added": [_add()]})
    plan = emit_plan(canonical, base_sha256=hashlib.sha256(host.read_bytes()).hexdigest())
    assert plan.endswith(INSERT_LINE)
    # Match the working v2 canary's on-disk plan and script framing.
    (tmp_path / "mutation-plan.txt").write_bytes(plan.replace(b"\n", b"\r\n"))
    _console(tmp_path, host, "apply.scr", setup + before + settings["script"]["value"])
    base = intake_parse.parse(tmp_path / "base-intake.txt", "canary")
    assert not base.get("parseErrors"), base.get("parseErrors")
    assert base["blocks"]["Fixture"]["complete"] is True
    assert validate_mutations(base, {"added": [_add()]}) == canonical
    output = tmp_path / "output.dwg"
    assert output.exists() and output.stat().st_size > 0
    _console(tmp_path, output, "after.scr", inspect)
    families = tmp_path / "output-intake.txt"
    assert "IN|Fixture|0|" in families.read_text(encoding="utf-8")
    actual = intake_parse.parse(families, "canary")
    assert not actual.get("parseErrors"), actual.get("parseErrors")
    added, = [entity for entity in actual["inserts"] if entity["name"] == "Fixture"]
    assert added == {**_insert(added["handle"]), "rot": 1.5708}
    assert added["handle"] not in {entity["handle"] for entity in base.get("inserts", [])}
    write_loop.verify_live_mutation_effects(base, actual, canonical)
