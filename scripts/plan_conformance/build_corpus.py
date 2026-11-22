"""Build the versioned cross-language plan fixtures, or check committed bytes.

Run from any directory. Modules are loaded by file path: adding the repository
root to sys.path would shadow Python's platform module. Imports do not write
bytecode, so building touches only the requested fixture directory.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


MODEL = load_file("plan_reference_model", Path(__file__).with_name("reference_model.py"))
EMITTER = load_file("plan_corpus_emitter", ROOT / "server/mutation_plan.py")
BASE_SHA256 = "0" * 64
POINTS = [[0.0, 0.0, 0.0], [2.0, 0.0, 0.0], [2.0, 2.0, 0.0], [0.0, 2.0, 0.0]]


def base_intake():
    return {"layers": ["0"], "polylines": [
        {"handle": "A", "layer": "0", "closed": True, "pts": POINTS},
        {"handle": "B", "layer": "0", "closed": False,
         "pts": [[0.0, 0.0, 0.0], [2.0, 0.0, 0.0]]}],
        "circles": [{"handle": "C", "layer": "0", "c": [1, 1, 0], "r": 2}],
        "arcs": [{"handle": "D", "layer": "0", "c": [0, 0, 0], "r": 3,
                  "start_deg": 0, "end_deg": 90}]}


def emitter_inputs():
    """Small fixed canonical mutations; each case isolates one emitted tag."""
    poly = {"kind": "LWPOLYLINE", "layer": "0", "closed": True, "pts": POINTS}
    line = {"kind": "LINE", "layer": "0", "pts": [[1, 2, 0], [4, 6, 0]]}
    circle = {"kind": "CIRCLE", "layer": "0", "c": [3, 4, 0], "r": 2.5}
    arc = {**circle, "kind": "ARC", "start_deg": 30, "end_deg": 120}
    dim = {"kind": "DIMENSION", "layer": "0", "style": "Standard",
           "def1": [0, 0, 0], "def2": [4, 0, 0], "dimline": [0, 2, 0],
           "dimtype": "LINEAR", "rotation": 0}
    leader = {"kind": "MLEADER", "layer": "0", "style": "Standard",
              "pts": [[0, 0, 0], [2, 3, 0]], "text": "Solar panel"}
    return {
        "op-add": {"added": [poly]},
        "op-addopen": {"added": [{**poly, "closed": False, "pts": POINTS[:2]}]},
        "op-addline": {"added": [line]},
        "op-addcircle": {"added": [circle]},
        "op-addarc": {"added": [arc]},
        "op-addinsert": {"added": [{"kind": "INSERT", "layer": "0", "name": "SYMBOL",
                                     "pt": [3, 4, 0], "scale": [1, 2, 1], "rot": 90}]},
        "op-adddimlinear": {"added": [dim]},
        "op-adddimaligned": {"added": [{**dim, "dimtype": "ALIGNED"}]},
        "op-addmleader": {"added": [leader]},
        "op-addblockdef": {"block_defs": [{"name": "PANEL", "base": [1, 1, 0], "members": ["B"]}]},
        "op-blockchild": {"block_defs": [{"name": "PANEL", "base": [1, 1, 0],
                                           "members": [], "children": [line], "order": ["C:0"]}]},
        "op-addgroup": {"added_groups": [{"name": "G1", "members": ["A", "B"]}]},
        "op-removegroup": {"removed_groups": ["G0"]},
        "op-remove": {"removed": ["A"]},
        "op-relayer": {"set_layer": [{"handle": "A", "layer": "Moved"}]},
        "op-setpoints": {"set_points": [{"handle": "A", "closed": True,
                                           "pts": [[0, 0, 0], [4, 0, 0], [4, 4, 0]]}]},
        "op-setcircle": {"set_circle": [{"handle": "C", "c": [3, 4, 0], "r": 2.5}]},
        "op-setarc": {"set_arc": [{"handle": "D", "c": [3, 4, 0], "r": 2.5,
                                     "start_deg": 30, "end_deg": 120}]},
        "op-transform": {"transforms": [{"handle": "A", "dx": 3, "dy": 2, "rotation_deg": 90}]},
        "op-setcolor": {"set_color": [{"handle": "A", "aci": 7}]},
        "op-setlinetype": {"set_linetype": [{"handle": "A", "name": "Continuous"}]},
        "op-setlineweight": {"set_lineweight": [{"handle": "A", "weight": 25}]},
        "v3-utf8-text": {"added": [{**leader, "text": "太陽光パネル — Солнечная панель"}]},
    }


def plan_text(*lines, version=3):
    return (f"LEAF_MUTATION_PLAN|{version}\nBASE_SHA256|{BASE_SHA256}\n"
            + "\n".join(lines) + "\n").encode("utf-8")


def synthetic_inputs():
    malformed = {
        "add": "ADD|0|0,0,0|0|0,0;1,0;1,1",  # zero normal
        "addline": "ADDLINE|0|0,0,0|0,0,0",  # coincident endpoints
        "addcircle": "ADDCIRCLE|0|0,0,0|-1",
        "addarc": "ADDARC|0|0,0,0|1|90|90",
        "addinsert": "ADDINSERT|0|SYMBOL|0,0,0|0|1,0,1",
        "adddim": "ADDDIMLINEAR|0|Standard|0,0,0|1,0,0|bad|0",
        "addmleader": "ADDMLEADER|0|Standard|0,0,0|1,0,0|",
        "addblockdef": "ADDBLOCKDEF|PANEL|0,0,0|C:0",  # undeclared child
        "group": "ADDGROUP|G1|H:A",  # fewer than two members
        "remove": "REMOVE|not-a-handle",
        "relayer": "RELAYER|A|bad/layer",
        "setgeom": "SETPOINTS|A|2|0,0,1|0|0,0;1,0",
        "transform": "TRANSFORM|A|0,0,1|0|0,0;1,0",
        "style": "SETCOLOR|H:A|257",
    }
    cases = {"malformed-" + name: (plan_text(line), "plan_malformed")
             for name, line in malformed.items()}
    cases.update({
        "unknown-op": (plan_text("RUN|anything"), "plan_op_not_supported"),
        "header-invalid": (plan_text("REMOVE|A").replace(b"PLAN|3", b"PLAN|4"), "plan_header_invalid"),
        "non-ascii-v2": (plan_text("ADDMLEADER|0|Standard|0,0,0|1,0,0|太陽", version=2), "plan_non_ascii"),
        "clearmodel-first-v3": (plan_text("CLEARMODEL", "ADDLINE|0|0,0,0|1,0,0"), None),
        "clearmodel-second-v3": (plan_text("REMOVE|A", "CLEARMODEL"), "plan_clearmodel_position"),
        "clearmodel-v2": (plan_text("CLEARMODEL", version=2), "plan_clearmodel_position"),
        "cap-ops-20000": (plan_text(*(["REMOVE|A"] * 20_000)), None),
        "cap-ops-20001": (plan_text(*(["REMOVE|A"] * 20_001)), "plan_too_large"),
    })
    return cases


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def corpus_files():
    """Pure mapping of relative paths to bytes, shared by --check and pytest."""
    files, cases = {}, []
    inputs = {name: (EMITTER.emit_plan(canonical, base_sha256=BASE_SHA256,
                                      base_intake=base_intake()), None, "emit_plan")
              for name, canonical in sorted(emitter_inputs().items())}
    inputs.update({name: (*value, "synthetic") for name, value in synthetic_inputs().items()})
    for name, (data, refusal, source) in sorted(inputs.items()):
        parsed = MODEL.parse_plan(data)
        if refusal is not None:
            if parsed != refusal:
                raise ValueError(f"{name}: expected {refusal}, got {parsed!r}")
            expected = {"refusal": refusal}
        else:
            if isinstance(parsed, str):
                raise ValueError(f"{name}: unexpected refusal {parsed}")
            expected = MODEL.apply_ops(parsed)
        plan_path, expected_path = f"plans/{name}.plan", f"expected/{name}.json"
        files[plan_path], files[expected_path] = data, json_bytes(expected)
        case = {"name": name, "source": source, "plan": plan_path, "expected": expected_path}
        if refusal is None:
            case["operation_counts"] = dict(sorted(Counter(op["op"] for op in parsed).items()))
        else:
            case["refusal"] = refusal
        cases.append(case)
    for count in (500_000, 500_001):
        descriptor = {"generator": "points", "points": count, "per_op": 10_000, "layer": "0"}
        data = MODEL.generate_case(descriptor)
        expected = ({"operations": 50, "points": count} if count == 500_000
                    else {"refusal": "plan_too_large"})
        cases.append({"name": f"cap-points-{count}", "source": "synthetic",
                      "descriptor": descriptor, "sha256": sha256(data), "expected": expected})
    manifest = {
        "schema_version": 1,
        "refusal_codes": sorted(MODEL.REFUSAL_CODES),
        "limits": {"operations": MODEL.MAX_OPERATIONS, "points": MODEL.MAX_POINTS},
        "initial_state": MODEL.initial_state(),
        "cases": sorted(cases, key=lambda case: case["name"]),
        "files": {path: {"sha256": sha256(data)} for path, data in sorted(files.items())},
    }
    files["corpus.manifest.json"] = json_bytes(manifest)
    return files


def build_corpus(out, *, check=False):
    out = Path(out)
    files = corpus_files()
    if check:
        actual = {p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()}
        if actual != files:
            names = sorted(k for k in actual.keys() | files.keys() if actual.get(k) != files.get(k))
            raise ValueError("corpus differs: " + ", ".join(names))
    else:
        for name, data in sorted(files.items()):
            path = out / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    build_corpus(args.out, check=args.check)


if __name__ == "__main__":
    main()
