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
from lisp import build_scr
from mutation_plan import world_to_ocs


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
    families = tmp_path / "output-intake.txt"
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert output.exists() and output.stat().st_size > 0, (
        applied.stdout + applied.stderr
    )
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
