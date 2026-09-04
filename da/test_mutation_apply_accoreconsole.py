"""Local AutoCAD canary for the fixed mutation-plan interpreter.

This test is offline and non-billable. It mutates only a temporary copy of the
tracked demo DWG, then re-extracts that copy with the same local AutoCAD 2026
console runtime used to build the proven APS scripts.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT / "server"))

from apply_lisp import build_apply_scr
from intake_parse import o2w, parse
from lisp import MUTATION_INSPECT_BLOCKS, build_scr
from mutation_plan import emit_plan, validate_mutations, world_to_ocs


ACCORECONSOLE = Path(
    r"C:\Program Files\Autodesk\AutoCAD 2026\accoreconsole.exe"
)
SOURCE_DWG = PROJECT_ROOT / "data" / "rooftop_demo.dwg"
SOURCE_INTAKE = PROJECT_ROOT / "data" / "rooftop_demo.intake.json"


def test_engine_canary_contract_is_portable_and_wired():
    assert SOURCE_DWG.exists() and SOURCE_INTAKE.exists()
    assert "TRANSFORM" in build_apply_scr()
    assert "families.txt" in build_scr("families.txt")


@pytest.mark.skipif(
    not ACCORECONSOLE.exists() or not SOURCE_DWG.exists(),
    reason="local AutoCAD 2026 console and tracked demo DWG are required",
)
def test_fixed_plan_removes_and_adds_then_reextracts(tmp_path):
    source_intake = json.loads(SOURCE_INTAKE.read_text(encoding="utf-8"))
    removed_handle = source_intake["polylines"][0]["handle"]
    transformed_source = source_intake["polylines"][1]
    transformed_handle = transformed_source["handle"]
    transformed_target = [
        [point[0] + 5.0, point[1] + 7.0, point[2]]
        for point in transformed_source["pts"]
    ]
    lowered_transform = world_to_ocs(transformed_target)
    transform_normal = ",".join(
        format(value, ".12g") for value in lowered_transform["normal"])
    transform_vertices = ";".join(
        ",".join(format(value, ".12g") for value in point)
        for point in lowered_transform["points"]
    )
    host = tmp_path / "host.dwg"
    shutil.copyfile(SOURCE_DWG, host)

    plan = "\r\n".join([
        "LEAF_MUTATION_PLAN|1",
        f"BASE_SHA256|{hashlib.sha256(host.read_bytes()).hexdigest()}",
        f"REMOVE|{removed_handle}",
        (
            f"TRANSFORM|{transformed_handle}|{transform_normal}|"
            f"{format(lowered_transform['elevation'], '.12g')}|"
            f"{transform_vertices}"
        ),
        "ADD|LEAF_APPLY_CANARY|0,0,1|0|0,0;12,0;12,12;0,12",
        "ADD|LEAF_ROUNDING_CANARY|0,0,1|0|10.0625,20.0625;12.0625,20.0625;12.0625,22.0625;10.0625,22.0625",
        "ADD|LEAF_DECIMAL_CANARY|0,0,1|0|10.0005,20.0005;12.0005,20.0005;12.0005,22.0005;10.0005,22.0005",
        "ADD|LEAF_NEGATIVE_CANARY|0,0,1|0|-10.0625,-20.0625;-12.0625,-20.0625;-12.0625,-22.0625;-10.0625,-22.0625",
        "ADD|LEAF_EXPONENT_CANARY|0,0,1|0|3e-05,0;1,0;1,1;0,1",
        (
            "ADD|LEAF_TILTED_CANARY|0,0.707106781,0.707106781|0|"
            "20,0;32,0;32,12;20,12"
        ),
        "",
    ])
    (tmp_path / "mutation-plan.txt").write_text(
        plan, encoding="ascii", newline="",
    )
    (tmp_path / "apply.scr").write_text(
        build_apply_scr(), encoding="ascii", newline="",
    )

    applied = subprocess.run(
        [str(ACCORECONSOLE), "/i", str(host), "/s", str(tmp_path / "apply.scr")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    output = tmp_path / "output.dwg"
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert output.exists() and output.stat().st_size > 0, (
        applied.stdout + applied.stderr
    )
    (tmp_path / "inspect.scr").write_text(
        build_scr("output-intake.txt"), encoding="ascii", newline="",
    )
    inspected = subprocess.run(
        [
            str(ACCORECONSOLE), "/i", str(output),
            "/s", str(tmp_path / "inspect.scr"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    families = tmp_path / "output-intake.txt"
    assert inspected.returncode == 0, inspected.stdout + inspected.stderr
    assert families.exists() and families.stat().st_size > 0

    intake = parse(families, "canary")
    handles = {item["handle"] for item in intake["polylines"]}
    assert removed_handle not in handles
    transformed = next(
        item for item in intake["polylines"]
        if item["handle"] == transformed_handle
    )
    assert transformed["layer"] == transformed_source["layer"]
    for actual_point, expected_point in zip(
        transformed["pts"], transformed_target
    ):
        assert actual_point == pytest.approx(expected_point, abs=0.000501)
    assert transformed["closed"] == transformed_source["closed"]
    assert transformed["xdata"] == transformed_source["xdata"]
    added = [
        item for item in intake["polylines"]
        if item["layer"] == "LEAF_APPLY_CANARY"
    ]
    assert len(added) == 1
    assert added[0]["closed"] is True
    assert added[0]["pts"] == [
        [0.0, 0.0, 0.0],
        [12.0, 0.0, 0.0],
        [12.0, 12.0, 0.0],
        [0.0, 12.0, 0.0],
    ]
    rounded = [
        item for item in intake["polylines"]
        if item["layer"] == "LEAF_ROUNDING_CANARY"
    ]
    assert len(rounded) == 1
    assert rounded[0]["pts"] == [
        [10.063, 20.063, 0.0],
        [12.063, 20.063, 0.0],
        [12.063, 22.063, 0.0],
        [10.063, 22.063, 0.0],
    ]
    decimal = [
        item for item in intake["polylines"]
        if item["layer"] == "LEAF_DECIMAL_CANARY"
    ]
    assert len(decimal) == 1
    assert decimal[0]["pts"] == [
        [10.001, 20.001, 0.0],
        [12.001, 20.001, 0.0],
        [12.001, 22.001, 0.0],
        [10.001, 22.001, 0.0],
    ]
    negative = [
        item for item in intake["polylines"]
        if item["layer"] == "LEAF_NEGATIVE_CANARY"
    ]
    assert len(negative) == 1
    assert negative[0]["pts"] == [
        [-10.063, -20.063, 0.0],
        [-12.063, -20.063, 0.0],
        [-12.063, -22.063, 0.0],
        [-10.063, -22.063, 0.0],
    ]
    exponent = [
        item for item in intake["polylines"]
        if item["layer"] == "LEAF_EXPONENT_CANARY"
    ]
    assert len(exponent) == 1
    assert exponent[0]["pts"] == [
        [0.0, 0.0, 0.0], [1.0, 0.0, 0.0],
        [1.0, 1.0, 0.0], [0.0, 1.0, 0.0],
    ]
    tilted = [
        item for item in intake["polylines"]
        if item["layer"] == "LEAF_TILTED_CANARY"
    ]
    assert len(tilted) == 1
    normal = (0.0, 0.707107, 0.707107)
    expected_tilted = [
        [round(value, 3) for value in o2w((x, y, 0.0), normal)]
        for x, y in ((20, 0), (32, 0), (32, 12), (20, 12))
    ]
    assert tilted[0]["pts"] == expected_tilted
    assert len({point[2] for point in tilted[0]["pts"]}) > 1


def _run_plan(tmp_path, tag, host, plan_bytes):
    """Apply one plan to `host` with the fixed interpreter, then inspect the
    result with the mutation Activity's inspect variant; returns the parsed
    intake of the output and the output path."""
    work = tmp_path / tag
    work.mkdir()
    (work / "mutation-plan.txt").write_bytes(plan_bytes.replace(b"\n", b"\r\n"))
    (work / "apply.scr").write_text(build_apply_scr(), encoding="ascii", newline="")
    applied = subprocess.run(
        [str(ACCORECONSOLE), "/i", str(host), "/s", str(work / "apply.scr")],
        cwd=work, capture_output=True, text=True, timeout=120, check=False,
    )
    output = work / "output.dwg"
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "LEAF-MUTATION-PLAN-INVALID" not in applied.stdout, applied.stdout
    assert "LEAF-MUTATION-APPLY-FAILED" not in applied.stdout, applied.stdout
    assert output.exists() and output.stat().st_size > 0, applied.stdout + applied.stderr
    (work / "inspect.scr").write_text(
        build_scr("output-intake.txt", extra_blocks=MUTATION_INSPECT_BLOCKS),
        encoding="ascii", newline="")
    inspected = subprocess.run(
        [str(ACCORECONSOLE), "/i", str(output), "/s", str(work / "inspect.scr")],
        cwd=work, capture_output=True, text=True, timeout=120, check=False,
    )
    families = work / "output-intake.txt"
    assert inspected.returncode == 0, inspected.stdout + inspected.stderr
    assert families.exists() and families.stat().st_size > 0
    intake = parse(families, "canary")
    assert not intake.get("parseErrors"), intake.get("parseErrors")
    return intake, output


@pytest.mark.skipif(
    not ACCORECONSOLE.exists() or not SOURCE_DWG.exists(),
    reason="local AutoCAD 2026 console and tracked demo DWG are required",
)
def test_v2_plan_applies_every_new_line_and_the_server_verifies_the_effects(tmp_path):
    """W4g-3a: the contract v2 end to end on a REAL drawing. Round 1 adds a
    LINE, a CIRCLE, an ARC and an open polyline, relayers one existing
    polyline and replaces another's vertices; round 2 (on round 1's output,
    whose inspection is round 2's base intake) moves the circle and the arc,
    re-points the line, relayers the circle and removes the open polyline.
    Every round's effects are checked by the server's own verifier over the
    inspection, the same call the live write makes before publishing."""
    import write_loop

    source_intake = json.loads(SOURCE_INTAKE.read_text(encoding="utf-8"))
    base = copy_intake = json.loads(json.dumps(source_intake))
    relayer_src = base["polylines"][2]
    repoint_src = base["polylines"][3]
    new_points = [[p[0] + 100.0, p[1] + 100.0, p[2]] for p in repoint_src["pts"]]
    host = tmp_path / "host.dwg"
    shutil.copyfile(SOURCE_DWG, host)
    round1 = validate_mutations(base, {
        "added": [
            {"handle": "n1", "kind": "LINE", "layer": "LEAF_V2_LINE", "pts": [[1, 2], [4, 6]]},
            {"handle": "n2", "kind": "CIRCLE", "layer": "LEAF_V2_CIRCLE", "c": [10, 10], "r": 3},
            {"handle": "n3", "kind": "ARC", "layer": "LEAF_V2_ARC", "c": [20, 0], "r": 2, "start_deg": 30, "end_deg": 120},
            {"handle": "n4", "layer": "LEAF_V2_OPEN", "closed": False, "pts": [[0, 0], [5, 5], [10, 0]]},
        ],
        "set_layer": [{"handle": relayer_src["handle"], "layer": "LEAF_V2_RELAYER"}],
        "set_points": [{"handle": repoint_src["handle"], "pts": new_points}],
    })
    plan1 = emit_plan(round1, base_sha256=hashlib.sha256(host.read_bytes()).hexdigest(), base_intake=base)
    assert plan1.startswith(b"LEAF_MUTATION_PLAN|2\n")
    actual1, output1 = _run_plan(tmp_path, "round1", host, plan1)
    write_loop.verify_live_mutation_effects(
        {**copy_intake, "dwg": "canary"}, actual1, round1)
    by_layer = {}
    for field in ("polylines", "circles", "arcs"):
        for entity in actual1.get(field, []):
            by_layer.setdefault(entity["layer"], []).append((field, entity))
    (kind, line), = by_layer["LEAF_V2_LINE"]
    assert kind == "polylines" and line["closed"] is False
    assert line["pts"] == [[1.0, 2.0, 0.0], [4.0, 6.0, 0.0]]
    (kind, circle), = by_layer["LEAF_V2_CIRCLE"]
    assert kind == "circles" and circle["c"] == [10.0, 10.0, 0.0] and circle["r"] == 3.0
    (kind, arc), = by_layer["LEAF_V2_ARC"]
    assert kind == "arcs" and arc["c"] == [20.0, 0.0, 0.0] and arc["r"] == 2.0
    assert arc["start_deg"] == pytest.approx(30.0, abs=1e-5) and arc["end_deg"] == pytest.approx(120.0, abs=1e-5)
    (kind, opened), = by_layer["LEAF_V2_OPEN"]
    assert kind == "polylines" and opened["closed"] is False
    assert opened["pts"] == [[0.0, 0.0, 0.0], [5.0, 5.0, 0.0], [10.0, 0.0, 0.0]]
    relayered = next(p for p in actual1["polylines"] if p["handle"] == relayer_src["handle"])
    assert relayered["layer"] == "LEAF_V2_RELAYER" and relayered["pts"] == relayer_src["pts"]
    repointed = next(p for p in actual1["polylines"] if p["handle"] == repoint_src["handle"])
    assert repointed["closed"] == repoint_src["closed"]
    for actual_point, expected_point in zip(repointed["pts"], new_points):
        assert actual_point == pytest.approx(expected_point, abs=0.000501)

    round2 = validate_mutations(actual1, {
        "set_circle": [{"handle": circle["handle"], "c": [11, 12], "r": 4}],
        "set_arc": [{"handle": arc["handle"], "c": [21, 1], "r": 2.5, "start_deg": 40, "end_deg": 200}],
        "set_points": [{"handle": line["handle"], "pts": [[2, 3], [7, 8]]}],
        "set_layer": [{"handle": circle["handle"], "layer": "LEAF_V2_RELAYER2"}],
        "removed": [opened["handle"]],
    })
    plan2 = emit_plan(round2, base_sha256=hashlib.sha256(output1.read_bytes()).hexdigest(), base_intake=actual1)
    actual2, _ = _run_plan(tmp_path, "round2", output1, plan2)
    write_loop.verify_live_mutation_effects(actual1, actual2, round2)
    moved = next(c for c in actual2["circles"] if c["handle"] == circle["handle"])
    assert moved["c"] == [11.0, 12.0, 0.0] and moved["r"] == 4.0 and moved["layer"] == "LEAF_V2_RELAYER2"
    swept = next(a for a in actual2["arcs"] if a["handle"] == arc["handle"])
    assert swept["c"] == [21.0, 1.0, 0.0] and swept["r"] == 2.5
    assert swept["start_deg"] == pytest.approx(40.0, abs=1e-5) and swept["end_deg"] == pytest.approx(200.0, abs=1e-5)
    repointed_line = next(p for p in actual2["polylines"] if p["handle"] == line["handle"])
    assert repointed_line["pts"] == [[2.0, 3.0, 0.0], [7.0, 8.0, 0.0]]
    assert all(p["handle"] != opened["handle"] for p in actual2["polylines"])
