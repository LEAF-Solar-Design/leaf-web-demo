"""Offline, byte-exact plan vectors shared with the Branch implementation."""
from collections import Counter
from copy import deepcopy
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures/plan_conformance"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


BUILDER = _load("conformance_builder", ROOT / "scripts/plan_conformance/build_corpus.py")
MODEL = _load("conformance_reference", ROOT / "scripts/plan_conformance/reference_model.py")
EMITTER = _load("conformance_emitter", ROOT / "server/mutation_plan.py")
MANIFEST = json.loads((FIXTURES / "corpus.manifest.json").read_bytes())


def _tree(directory):
    return {p.relative_to(directory).as_posix(): p.read_bytes()
            for p in directory.rglob("*") if p.is_file()}


@pytest.mark.parametrize("case", MANIFEST["cases"], ids=lambda case: case["name"])
def test_corpus_case(case):
    generated = "descriptor" in case
    data = (MODEL.generate_case(case["descriptor"]) if generated
            else (FIXTURES / case["plan"]).read_bytes())
    expected = (case["expected"] if generated
                else json.loads((FIXTURES / case["expected"]).read_bytes()))
    parsed = MODEL.parse_plan(data)
    if "refusal" in expected:
        assert parsed == expected["refusal"]
        assert parsed in MANIFEST["refusal_codes"]
        return
    assert isinstance(parsed, list), parsed
    if generated:
        assert len(parsed) == expected["operations"]
        assert sum(len(op["args"][-1].split(";")) for op in parsed) == expected["points"]
    else:
        assert MODEL.apply_ops(parsed, MANIFEST["initial_state"]) == expected
        assert dict(Counter(op["op"] for op in parsed)) == case["operation_counts"]
    header, base = data.split(b"\n", 2)[:2]
    assert MODEL.serialize_plan(parsed, version=int(header[-1:]),
                                base_sha256=base.decode().split("|")[1]) == data
    if case["source"] == "emit_plan":
        canonical = BUILDER.emitter_inputs()[case["name"]]
        assert EMITTER.emit_plan(canonical, base_sha256=BUILDER.BASE_SHA256,
                                 base_intake=BUILDER.base_intake()) == data


def test_rebuild_byte_identical(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    BUILDER.build_corpus(first)
    BUILDER.build_corpus(second)
    assert _tree(first) == _tree(second) == _tree(FIXTURES)
    BUILDER.build_corpus(FIXTURES, check=True)


def test_manifest_hashes_and_layout():
    files = _tree(FIXTURES)
    assert set(files) == set(MANIFEST["files"]) | {"corpus.manifest.json"}
    assert len(files) == 91
    assert sum(map(len, files.values())) < 2 * 1024 * 1024
    for path, entry in MANIFEST["files"].items():
        assert hashlib.sha256(files[path]).hexdigest() == entry["sha256"], path
        assert b"\r" not in files[path], path
    assert MANIFEST["refusal_codes"] == sorted(MODEL.REFUSAL_CODES)
    assert MANIFEST["limits"] == {"operations": 20_000, "points": 500_000}
    assert MANIFEST["initial_state"] == MODEL.initial_state()
    for case in MANIFEST["cases"]:
        assert case["source"] in ("emit_plan", "synthetic")
        if "descriptor" in case:
            assert hashlib.sha256(MODEL.generate_case(case["descriptor"])).hexdigest() == case["sha256"]
        else:
            assert case["plan"] == f"plans/{case['name']}.plan"
            assert case["expected"] == f"expected/{case['name']}.json"


def test_emitter_coverage():
    cases = {case["name"]: case for case in MANIFEST["cases"]}
    assert len(cases) == len(MANIFEST["cases"]) == 47
    assert len(MODEL.OP_NAMES) == 22
    for tag in MODEL.OP_NAMES:
        case = cases["op-" + tag.lower()]
        assert case["source"] == "emit_plan"
        assert case["operation_counts"][tag] == 1
        assert set(case["operation_counts"]) == ({tag, "ADDBLOCKDEF"} if tag == "BLOCKCHILD" else {tag})
    assert len([case for case in cases.values() if case["source"] == "emit_plan"]) == 23
    assert {case["refusal"] for case in cases.values() if "refusal" in case} == set(MODEL.REFUSAL_CODES)
    text = (FIXTURES / cases["v3-utf8-text"]["plan"]).read_bytes()
    assert text.startswith(b"LEAF_MUTATION_PLAN|3\n")
    assert "太陽光" in text.decode() and "Солнечная" in text.decode()


def test_reference_effects():
    # Independent semantic anchors keep regenerated goldens from concealing a
    # change to the oracle. Check geometry, ordering, copy semantics and ids.
    def state(name):
        data = (FIXTURES / f"plans/{name}.plan").read_bytes()
        return MODEL.apply_ops(MODEL.parse_plan(data))
    def entity(name, key):
        return next(e for e in state(name)["entities"] if e["id"] == key)
    assert entity("op-add", "A:0")["points"] == [[0, 0], [2, 0], [2, 2], [0, 2]]
    assert entity("op-add", "A:0")["closed"] is True
    assert entity("op-addopen", "A:0")["closed"] is False
    assert entity("op-addline", "A:0")["points"] == [[1, 2, 0], [4, 6, 0]]
    assert entity("op-addcircle", "A:0")["radius"] == 2.5
    assert entity("op-addarc", "A:0")["start"] == math.pi / 6
    assert entity("op-addinsert", "A:0")["rotation"] == math.pi / 2
    assert entity("op-addinsert", "A:0")["scale"] == [1, 2, 1]
    assert entity("op-adddimlinear", "A:0")["dimtype"] == "LINEAR"
    assert entity("op-adddimaligned", "A:0")["dimtype"] == "ALIGNED"
    assert entity("op-addmleader", "A:0")["text"] == "Solar panel"
    assert entity("op-relayer", "H:A")["layer"] == "Moved"
    assert entity("op-setpoints", "H:A")["points"] == [[0, 0], [4, 0], [4, 4]]
    assert entity("op-setcircle", "H:C")["center"] == [3, 4, 0]
    assert entity("op-setarc", "H:D")["end"] == math.radians(120)
    assert entity("op-transform", "H:A")["points"] == [[5, 2], [5, 4], [3, 4], [3, 2]]
    assert entity("op-setcolor", "H:A")["color"] == 7
    assert entity("op-setlinetype", "H:A")["linetype"] == "Continuous"
    assert entity("op-setlineweight", "H:A")["lineweight"] == 25
    assert [e["id"] for e in state("op-remove")["entities"]] == ["H:B", "H:C", "H:D", "H:E"]
    assert state("op-addgroup")["groups"][-1] == {"name": "G1", "members": ["H:A", "H:B"]}
    assert state("op-removegroup")["groups"] == []
    assert len(state("op-removegroup")["entities"]) == 5
    block = state("op-addblockdef")
    assert block["blocks"][-1]["entities"][0]["points"] == [[0, 0, 0], [2, 0, 0]]
    assert len(block["entities"]) == 5  # copying a block member does not consume it
    assert state("op-blockchild")["blocks"][-1]["entities"][0]["kind"] == "LINE"
    cleared = state("clearmodel-first-v3")
    assert [e["id"] for e in cleared["entities"]] == ["H:E", "A:0"]
    assert len(cleared["blocks"]) == 1
    seed = deepcopy(MODEL.initial_state())
    seed["entities"][0]["bulges"] = [1, 0, 0, 0]
    seed["entities"][0]["true_color"] = 123
    operations = [{"op": "SETCOLOR", "args": ["H:A", "256"]}]
    changed = MODEL.apply_ops(operations, seed)
    assert changed["entities"][0]["color"] == 256
    assert "true_color" not in changed["entities"][0]
    assert seed["entities"][0]["true_color"] == 123
    transformed = MODEL.parse_plan((FIXTURES / "plans/op-transform.plan").read_bytes())
    assert MODEL.apply_ops(transformed, seed)["entities"][0]["bulges"] == [1, 0, 0, 0]
    resized = MODEL.parse_plan((FIXTURES / "plans/op-setpoints.plan").read_bytes())
    assert "bulges" not in MODEL.apply_ops(resized, seed)["entities"][0]


def test_parser_validation():
    def parse(line, version=3):
        return MODEL.parse_plan(BUILDER.plan_text(line, version=version))
    assert MODEL.parse_plan(b"") == "plan_header_invalid"
    assert MODEL.parse_plan(b"LEAF_MUTATION_PLAN|3\nBASE_SHA256|bad\nREMOVE|A\n") == "plan_header_invalid"
    assert parse("ADDCIRCLE|0|0,0,0|NaN") == "plan_malformed"
    assert parse("ADD|0|0,0,1|0|0,0;1,0") == "plan_malformed"
    assert parse("ADDOPEN|0|0,0,1|0|0,0;1,0|extra") == "plan_malformed"
    assert parse("SETLINEWEIGHT|H:A|1") == "plan_malformed"
    assert parse("SETCOLOR|A:0|1.5") == "plan_malformed"
    assert parse("CLEARMODEL|extra") == "plan_malformed"
    assert parse("CLEARMODEL", version=1) == "plan_clearmodel_position"
    assert parse("ADDMLEADER|0|Standard|0,0,0|1,0,0|太陽", version=1) == "plan_non_ascii"
    assert MODEL.parse_plan(BUILDER.plan_text("REMOVE|A") + b"\xff\n") == "plan_malformed"
    for tag in MODEL.OP_NAMES:
        assert parse(tag + "|*") == "plan_malformed", tag
    # The emitter permits ! in group names; * is explicitly forbidden.
    # Give ADDGROUP valid members so its name validation is exercised too.
    for tag, suffix in (("ADDGROUP", "|H:A;H:B"), ("REMOVEGROUP", "")):
        assert isinstance(parse(tag + "|!" + suffix), list)
        for name in ("", " leading", "trailing ", "x" * 256, "太陽",
                     *'<>/\\":;?*|,=`'):
            assert parse(tag + "|" + name + suffix) == "plan_malformed", (tag, name)
