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
from pathlib import Path

import pytest

from apply_lisp import build_apply_scr
from intake_parse import o2w, parse
from lisp import build_scr
from mutation_plan import world_to_ocs


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ACCORECONSOLE = Path(
    r"C:\Program Files\Autodesk\AutoCAD 2026\accoreconsole.exe"
)
SOURCE_DWG = PROJECT_ROOT / "data" / "rooftop_demo.dwg"
SOURCE_INTAKE = PROJECT_ROOT / "data" / "rooftop_demo.intake.json"


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

    (tmp_path / "extract.scr").write_text(
        build_scr("families.txt"), encoding="ascii", newline="",
    )
    extracted = subprocess.run(
        [str(ACCORECONSOLE), "/i", str(output), "/s", str(tmp_path / "extract.scr")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    families = tmp_path / "families.txt"
    assert extracted.returncode == 0, extracted.stdout + extracted.stderr
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
    tilted = [
        item for item in intake["polylines"]
        if item["layer"] == "LEAF_TILTED_CANARY"
    ]
    assert len(tilted) == 1
    normal = (0.0, 0.707106781, 0.707106781)
    expected_tilted = [
        [round(value, 3) for value in o2w((x, y, 0.0), normal)]
        for x, y in ((20, 0), (32, 0), (32, 12), (20, 12))
    ]
    assert tilted[0]["pts"] == expected_tilted
    assert len({point[2] for point in tilted[0]["pts"]}) > 1
