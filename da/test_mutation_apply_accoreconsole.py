"""Local AutoCAD canary for the fixed mutation-plan interpreter.

This test is offline and non-billable. It mutates only a temporary copy of the
tracked demo DWG, then re-extracts that copy with a resolved local AutoCAD
console runtime.
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

from apply_lisp import build_apply_scr, build_apply_scr_v3
from console import resolve_accoreconsole as _resolve_accoreconsole
from intake_parse import o2w, parse
from lisp import MUTATION_INSPECT_BLOCKS, build_scr
from mutation_plan import emit_plan, validate_mutations, world_to_ocs



ACCORECONSOLE, CONSOLE_DISCLOSURE = _resolve_accoreconsole()
SOURCE_DWG = PROJECT_ROOT / "data" / "rooftop_demo.dwg"
SOURCE_INTAKE = PROJECT_ROOT / "data" / "rooftop_demo.intake.json"
CONSOLE_SKIP_REASON = f"{CONSOLE_DISCLOSURE}; tracked demo DWG required: {SOURCE_DWG}"


def test_console_resolution_is_disclosed(capsys):
    with capsys.disabled():
        print(f"\nAutoCAD canary: {CONSOLE_DISCLOSURE}", flush=True)
    if ACCORECONSOLE is None:
        assert "no AutoCAD console found" in CONSOLE_DISCLOSURE
    else:
        assert str(ACCORECONSOLE) in CONSOLE_DISCLOSURE


def test_console_resolver_picks_newest_and_ignores_malformed(tmp_path, monkeypatch):
    monkeypatch.delenv("LEAF_ACCORECONSOLE", raising=False)
    for name in ("AutoCAD 2023", "AutoCAD 2025", "AutoCAD 2024",
                 "AutoCAD latest", "AutoCAD 999", "AutoCAD 20260"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "accoreconsole.exe").touch()
    console, _ = _resolve_accoreconsole(tmp_path)
    assert console == tmp_path / "AutoCAD 2025" / "accoreconsole.exe"


def test_console_resolver_override_wins(tmp_path, monkeypatch):
    installed = tmp_path / "AutoCAD 2027"
    installed.mkdir()
    (installed / "accoreconsole.exe").touch()
    override = tmp_path / "override.exe"
    override.touch()
    monkeypatch.setenv("LEAF_ACCORECONSOLE", str(override))
    console, _ = _resolve_accoreconsole(tmp_path)
    assert console == override


def test_console_resolver_missing_override_refuses(tmp_path, monkeypatch):
    installed = tmp_path / "AutoCAD 2025"
    installed.mkdir()
    (installed / "accoreconsole.exe").touch()
    override = tmp_path / "missing.exe"
    monkeypatch.setenv("LEAF_ACCORECONSOLE", str(override))
    with pytest.raises(ValueError) as error:
        _resolve_accoreconsole(tmp_path)
    assert "LEAF_ACCORECONSOLE" in str(error.value)
    assert repr(str(override)) in str(error.value)


def test_console_resolver_empty_scan_explains_skip(tmp_path, monkeypatch):
    monkeypatch.delenv("LEAF_ACCORECONSOLE", raising=False)
    console, reason = _resolve_accoreconsole(tmp_path)
    assert console is None
    assert "LEAF_ACCORECONSOLE" in reason
    assert str(tmp_path / "AutoCAD <year>" / "accoreconsole.exe") in reason
    assert "years seen: none" in reason


def test_engine_canary_contract_is_portable_and_wired():
    assert SOURCE_DWG.exists() and SOURCE_INTAKE.exists()
    assert "TRANSFORM" in build_apply_scr()
    assert "families.txt" in build_scr("families.txt")


def test_width_inspection_preserves_leafextract_digest_and_dimension_tail():
    # Same pre-change pin as test_extract_dxf_activity.py, in this lane's gate.
    assert hashlib.sha256(build_scr().encode()).hexdigest() == (
        "74d54c719d787e7dc1ea42743707657821594ea08100b4d3e0cb211968b29095"
    )
    # build_scr also enforces the console's 1800-character line cap.
    inspection = build_scr(extra_blocks=MUTATION_INSPECT_BLOCKS)
    assert '"PW|"' in inspection and '"PWC|1"' in inspection
    assert '"DS|"' in MUTATION_INSPECT_BLOCKS[-2]
    assert '"DM|"' in MUTATION_INSPECT_BLOCKS[-1]


@pytest.mark.skipif(
    ACCORECONSOLE is None or not SOURCE_DWG.exists(),
    reason=CONSOLE_SKIP_REASON,
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


@pytest.mark.skipif(
    ACCORECONSOLE is None or not SOURCE_DWG.exists(),
    reason=CONSOLE_SKIP_REASON,
)
def test_mutation_inspect_reads_per_vertex_bulges(tmp_path):
    host = tmp_path / "bulges.dwg"
    shutil.copyfile(SOURCE_DWG, host)
    setup = '(entmake (list (cons 0 "LWPOLYLINE") (cons 100 "AcDbEntity") (cons 8 "LEAF_BULGE_CANARY") (cons 100 "AcDbPolyline") (cons 90 4) (cons 70 1) (cons 10 (list 0.0 0.0)) (cons 42 1.0) (cons 10 (list 10.0 0.0)) (cons 42 0.0) (cons 10 (list 10.0 10.0)) (cons 42 0.0) (cons 10 (list 0.0 10.0)) (cons 42 0.0)))\r\n'
    script = tmp_path / "bulges.scr"
    script.write_text(
        setup + build_scr("bulges.txt", extra_blocks=MUTATION_INSPECT_BLOCKS),
        encoding="ascii", newline="")
    result = subprocess.run(
        [str(ACCORECONSOLE), "/i", str(host), "/s", str(script)],
        cwd=tmp_path, capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    intake = parse(tmp_path / "bulges.txt", "canary")
    assert not intake.get("parseErrors"), intake.get("parseErrors")
    curved, = [p for p in intake["polylines"] if p["layer"] == "LEAF_BULGE_CANARY"]
    assert curved["bulges"] == pytest.approx([1.0, 0.0, 0.0, 0.0], rel=0, abs=1e-9)


@pytest.mark.skipif(
    ACCORECONSOLE is None or not SOURCE_DWG.exists(),
    reason=CONSOLE_SKIP_REASON,
)
@pytest.mark.parametrize("widthed", [True, False], ids=["widthed", "clean"])
def test_mutation_inspect_reports_polyline_width_and_coverage(tmp_path, widthed):
    host = tmp_path / "widths.dwg"
    shutil.copyfile(SOURCE_DWG, host)
    # Remove source polylines so the clean round does not depend on demo widths.
    setup = [
        '(progn (setq ss (ssget "_X" (list (cons 0 "LWPOLYLINE") (cons 410 "Model"))) i 0) (if ss (repeat (sslength ss) (entdel (ssname ss i)) (setq i (1+ i)))))',
        '(entmake (list (cons 0 "LWPOLYLINE") (cons 100 "AcDbEntity") (cons 8 "LEAF_WIDTH_CLEAN") (cons 410 "Model") (cons 100 "AcDbPolyline") (cons 90 3) (cons 70 0) (cons 43 0.0) (cons 10 (list 0.0 0.0)) (cons 40 0.0) (cons 41 0.0) (cons 10 (list 10.0 0.0)) (cons 40 0.0) (cons 41 0.0) (cons 10 (list 10.0 10.0))))',
    ]
    if widthed:
        setup.extend([
            '(entmake (list (cons 0 "LWPOLYLINE") (cons 100 "AcDbEntity") (cons 8 "LEAF_WIDTH_CONSTANT") (cons 410 "Model") (cons 100 "AcDbPolyline") (cons 90 3) (cons 70 0) (cons 43 2.5) (cons 10 (list 20.0 0.0)) (cons 10 (list 30.0 0.0)) (cons 10 (list 30.0 10.0))))',
            # AutoCAD drops per-vertex 40/41 when group 43 is present, so a tapered fixture must omit 43.
            '(entmake (list (cons 0 "LWPOLYLINE") (cons 100 "AcDbEntity") (cons 8 "LEAF_WIDTH_TAPERED") (cons 410 "Model") (cons 100 "AcDbPolyline") (cons 90 3) (cons 70 0) (cons 10 (list 40.0 0.0)) (cons 40 0.0) (cons 41 0.0) (cons 10 (list 50.0 0.0)) (cons 40 1.25) (cons 41 0.5) (cons 10 (list 50.0 10.0)) (cons 40 0.0) (cons 41 0.0)))',
        ])
    script = tmp_path / "widths.scr"
    script.write_text(
        "\r\n".join(setup) + "\r\n"
        + build_scr("widths.txt", extra_blocks=MUTATION_INSPECT_BLOCKS),
        encoding="ascii", newline="",
    )
    result = subprocess.run(
        [str(ACCORECONSOLE), "/i", str(host), "/s", str(script)],
        cwd=tmp_path, capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    families = tmp_path / "widths.txt"
    rows = families.read_text().splitlines()
    assert rows.count("PWC|1") == 1
    intake = parse(families, "canary")
    assert not intake.get("parseErrors"), intake.get("parseErrors")
    assert intake["polylineWidthCovered"] is True
    polylines = [p for p in intake["polylines"] if p["layer"].startswith("LEAF_WIDTH_")]
    by_layer = {p["layer"]: p for p in polylines}
    expected_layers = {"LEAF_WIDTH_CLEAN"}
    if widthed:
        expected_layers.update({"LEAF_WIDTH_CONSTANT", "LEAF_WIDTH_TAPERED"})
    assert set(by_layer) == expected_layers
    assert len(polylines) == len(expected_layers)
    assert not by_layer["LEAF_WIDTH_CLEAN"].get("width", False)
    expected_rows = []
    for layer in expected_layers - {"LEAF_WIDTH_CLEAN"}:
        assert by_layer[layer]["width"] is True
        expected_rows.append(f"PW|{by_layer[layer]['handle']}|1")
    assert sorted(row for row in rows if row.startswith("PW|")) == sorted(expected_rows)


def _apply_plan(tmp_path, tag, host, plan_bytes, script_builder=build_apply_scr):
    """Run the fixed interpreter and return its echo-free transcript."""
    work = tmp_path / tag
    work.mkdir()
    (work / "mutation-plan.txt").write_bytes(plan_bytes.replace(b"\n", b"\r\n"))
    (work / "apply.scr").write_text(script_builder(), encoding="ascii", newline="")
    applied = subprocess.run(
        [str(ACCORECONSOLE), "/i", str(host), "/s", str(work / "apply.scr")],
        cwd=work, capture_output=True, text=True, timeout=120, check=False,
    )
    # Redirected accoreconsole output is UTF-16LE read through text mode.
    text = applied.stdout.replace("\x00", "")
    stderr = applied.stderr.replace("\x00", "")
    assert applied.returncode == 0, text + stderr
    # Script echoes contain the marker literals; only printed markers count.
    text = "\n".join(line for line in (text + stderr).splitlines()
                     if not line.strip().startswith("Command:"))
    return work, text


def _run_plan(tmp_path, tag, host, plan_bytes):
    """Apply one plan to `host` with the fixed interpreter, then inspect the
    result with the mutation Activity's inspect variant; returns the parsed
    intake of the output and the output path."""
    work, text = _apply_plan(tmp_path, tag, host, plan_bytes)
    output = work / "output.dwg"
    assert "LEAF-MUTATION-PLAN-INVALID" not in text, text
    assert "LEAF-MUTATION-APPLY-FAILED" not in text, text
    assert output.exists() and output.stat().st_size > 0, text
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
    ACCORECONSOLE is None or not SOURCE_DWG.exists(),
    reason=CONSOLE_SKIP_REASON,
)
@pytest.mark.parametrize("script_builder", [build_apply_scr, build_apply_scr_v3], ids=["v2", "v3"])
def test_invalid_plan_marker_is_detected_without_script_echoes(tmp_path, script_builder):
    host = tmp_path / "host.dwg"
    shutil.copyfile(SOURCE_DWG, host)
    plan = (
        "LEAF_MUTATION_PLAN|9\n"
        f"BASE_SHA256|{hashlib.sha256(host.read_bytes()).hexdigest()}\n"
    ).encode("ascii")
    work, text = _apply_plan(tmp_path, "invalid", host, plan, script_builder)
    assert "LEAF-MUTATION-PLAN-INVALID" in text, text
    after_marker = text.split("LEAF-MUTATION-PLAN-INVALID", 1)[1]
    assert "error: quit / exit abort" in "\n".join(after_marker.splitlines()[:2]), text
    assert "LEAF-MUTATION-APPLY-FAILED" not in text, text
    assert not (work / "output.dwg").exists(), text


@pytest.mark.skipif(
    ACCORECONSOLE is None or not SOURCE_DWG.exists(),
    reason=CONSOLE_SKIP_REASON,
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
