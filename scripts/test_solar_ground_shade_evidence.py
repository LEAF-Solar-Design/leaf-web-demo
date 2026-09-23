"""Studio's G23 shade evidence (b8, b9, b10, b11).

A small synthetic terrain intake (a plane rising east, authored here) runs the terrain chain and
the four b-steps so every row kind is covered quickly: shade-marker rows with their frame
references, the replaced markers' `removed` row, the file rows (chunks, and the per-panel
table's digest), the report rows, the read-only rule, the parameters' answers, and refusals.

The terrain fixture itself is COMPUTED from the committed intake: Studio's own chain to a13
(G13), then b8 to b11. Its outcomes must equal what the plugin committed and printed: 1,197
green markers, the three LEAFSHADESIM exports at the captured byte sizes (all zero: nothing on
this site blocks a panel 1.5 m above the terrain), a clear-sky energy-weighted annual loss of
97.2% (97.24%, time-weighted 98.13%) over 1,434 rows with the captured first and last rows,
the compare's replaced markers and terrain-only scene at the captured byte sizes, and the
explained panel 1,196 with 0/216 angles blocked and 0.00% shade. The captures stay private; only
these figures are pinned. The plugin traced b8 on a GPU; the CPU trace is the reference (G23:
a GPU raster is a host quantity), and on this site both find nothing blocked.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
import re
from pathlib import Path
import sys

import pytest


def _load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sev = _load("solar_ground_shade_evidence")
ev = sev.ev
compare = sev.compare
shade = sev.shade
REVISION = "0123456789abcdef0123456789abcdef01234567"
INTAKE = Path(__file__).resolve().parent.parent / "docs" / "parity" / "evidence" / "ground" / "terrain" / "intake.json"
GREEN = 2068030                          # (31, 142, 62), bin 0 of the loss gradient
# Plugin storage names that must never reach public evidence (G23: neutral names only). They are kept as
# hash prefixes so this public file does not itself carry the plugin's storage layout.
STORAGE_TOKEN_HASHES = frozenset({"01a7f9c03f21b6fb", "25b2b9dd66a0495a", "585bd046617883d5", "7819f17e097194c2", "843202d80c59cf3f", "c1f05caff395ebab", "ee7244362b3f52b8"})
_UPPER_WORD = re.compile(r"[A-Z][A-Z0-9_]{3,}")


def storage_tokens_in(text):
    """Every upper-case word in `text` whose hash prefix is a plugin storage name, plus any ACAD_ name."""
    found = {w for w in _UPPER_WORD.findall(text) if hashlib.sha256(w.encode()).hexdigest()[:16] in STORAGE_TOKEN_HASHES}
    return found | {w for w in _UPPER_WORD.findall(text) if w.startswith("ACAD_")}


PRESET = {"Name": "Small", "ModuleLengthM": 2.0, "ModuleWidthM": 1.0, "Rows": 1, "Columns": 2,
          "HorizontalGapM": 0.0, "VerticalGapM": 0.0, "ColorIndex": 5, "PileTemplateName": "Two",
          "MaxSlopePercent": 80.0, "MaxCrossAxisSlopePct": 5.0,
          "Piling": {"MinPileLengthM": 3.0, "MaxPileLengthM": 6.0}}
TEMPLATE = {"Name": "Two", "HorizontalPoleCount": 1, "VerticalPoleCount": 2, "PileDiameterM": 0.2,
            "PileRevealM": 1.0, "PileEmbedmentM": 1.5}
BOUNDARY = [[0.0, 0.0], [3.0, 0.0], [3.0, 8.0], [0.0, 8.0]]


def terrain_intake():
    """A 30 x 9 m plane rising 0.3 m per metre east, as 3 x 3 m faces."""
    faces = []
    for i in range(10):
        for j in range(3):
            x0, x1, y0, y1 = 3.0 * i, 3.0 * i + 3.0, 3.0 * j, 3.0 * j + 3.0
            faces.append([[x0, y0, 0.3 * x0], [x1, y0, 0.3 * x1], [x1, y1, 0.3 * x1], [x0, y1, 0.3 * x0]])
    return {"units": "m", "boundary": deepcopy(BOUNDARY), "active_preset": deepcopy(PRESET),
            "pile_template": deepcopy(TEMPLATE), "terrain_faces": faces}


def synthetic_state():
    """Studio's t7 state of the synthetic intake (no tracker blocks, no arrays)."""
    intake = terrain_intake()
    return sev.with_shade_state(sev.scene_ev.terrain_state(intake)), intake


@pytest.fixture(scope="module")
def synthetic():
    state, intake = synthetic_state()
    docs, state = sev.run_steps(intake, REVISION, state=state)
    return docs, state, intake


def rows_of(doc, row_type=None):
    return [r for r in doc["after"]["rows"] if row_type is None or r["type"] == row_type]


def reports(doc):
    return {r["name"]: r["value"] for r in rows_of(doc, "report")}


def files(doc):
    return {r["role"]: r for r in rows_of(doc, "file")}


def text_of(row):
    return "".join(row["chunks"])


# --------------------------------------------------------------- synthetic --

def test_every_step_in_order_with_its_capability_and_answers(synthetic):
    docs, _, intake = synthetic
    assert list(docs) == ["b8", "b9", "b10", "b11"]
    for step, capability, operation in sev.B_STEPS:
        doc = docs[step]
        assert doc["provenance"] == {"side": "studio", "fixture_kind": "terrain", "step": step,
                                     "capability": capability, "operation": operation}
        assert doc["parameters"]["answers"] == list(sev.ANSWERS[step])
        assert doc["parameters"]["active_preset"] == intake["active_preset"]["Name"]
        assert doc["after"]["source_revision"] == step
        compare.validate_evidence(doc, "exports")
    assert docs["b11"]["parameters"]["answers"] == ["point:first-tracker-centre"]


def test_b8_markers_point_at_their_frames(synthetic):
    docs, state, _ = synthetic
    frames = ev.frame_ids(state["frames"])
    assert frames
    markers = rows_of(docs["b8"], "shade-marker")
    assert len(markers) == len(frames)
    frame_ids = {fid for fid, _, _ in frames}
    assert {m["panel"]["entity_id"] for m in markers} == frame_ids
    assert set(docs["b8"]["entity_mapping"]) >= frame_ids
    assert [m["id"]["entity_id"] for m in markers] == [f"shade-marker-{n}" for n in range(1, len(markers) + 1)]
    for m in markers:
        xs = [p["value"][0] for p in m["vertices"]]
        ys = [p["value"][1] for p in m["vertices"]]
        assert max(xs) - min(xs) == pytest.approx(4.0) and max(ys) - min(ys) == pytest.approx(4.0)
    assert not rows_of(docs["b8"], "removed")


def test_b8_files_and_reports(synthetic):
    docs, state, _ = synthetic
    n = len(state["frames"])
    f = files(docs["b8"])
    assert set(f) == {"shade-azal-matrix", "shade-sam", "shade-per-panel"}
    angles = shade.select_profile(n)["angles"]
    assert f["shade-sam"]["lines"] == 1 + len(angles)
    assert f["shade-per-panel"]["lines"] == 1 + n * len(angles)
    # G24: the representation follows the size of the normalized text, never the role.
    pp = f["shade-per-panel"]
    if "sha256" in pp:
        assert "chunks" not in pp and len(pp["sha256"]) == 64 and pp["chars"] > sev.G24_DIGEST_THRESHOLD
    else:
        assert len("".join(pp["chunks"])) <= sev.G24_DIGEST_THRESHOLD
    assert text_of(f["shade-azal-matrix"]).startswith("Altitude\\Azimuth,0,10,20,")
    r = reports(docs["b8"])
    assert r["panel-samples"] == n and r["panels-tinted"] == n and r["profile"] == "full"
    assert r["sun-angles"] == len(angles) and r["ray-tests"] == n * len(angles)
    assert r["clearance-shift"]["value"] == 1.5 and r["raw-median-clearance"]["value"] == 0.0
    assert r["target-clearance"] == {"kind": "length", "value": 1.5, "unit": "m"}
    assert r["surface-rows"] * r["surface-cols"] == r["surface-cells"] > 0
    # The plane rises 0.3 m per metre east: low eastern sun is blocked.
    assert r["shading-loss-percent"] > 0.0
    assert "replaced-markers" not in r
    assert not {"panels", "estimated-probes", "heatmap-panels", "median-clearance", "clearance-target"} & set(r)


def test_b9_is_read_only_and_writes_the_table(synthetic):
    docs, state, _ = synthetic
    doc = docs["b9"]
    assert not rows_of(doc, "unexpected-change") and not rows_of(doc, "shade-marker")
    table = text_of(files(doc)["shade-csv"])
    lines = table.split("\n")
    assert lines[0] == "LEAFSHADE Shade Analysis" and lines[-1] == ""
    assert len(lines) - 1 == 7 + len(state["frames"])
    r = reports(doc)
    assert r["tracker-rows"] == len(state["frames"]) and r["simulated-hours"] == 8760
    assert set(r) == {"tracker-rows", "shading-loss-percent", "simulated-hours"}


def test_b10_replaces_the_markers_and_exports_the_scene(synthetic):
    docs, state, _ = synthetic
    doc = docs["b10"]
    n = len(state["frames"])
    assert [(r["of"], r["count"]) for r in rows_of(doc, "removed")] == [("shade-marker", n)]
    assert len(rows_of(doc, "shade-marker")) == n
    assert set(files(doc)) == {"shade-azal-matrix", "shade-sam", "shade-per-panel", "scene-dae", "scene-pvc"}
    r = reports(doc)
    assert r["replaced-markers"] == n and r["scene-arrays"] == 0
    assert "<created></created>" in text_of(files(doc)["scene-dae"])
    b8, b10 = files(docs["b8"]), files(doc)
    for role in ("shade-azal-matrix", "shade-sam"):
        assert b8[role]["chunks"] == b10[role]["chunks"]
    assert (b8["shade-per-panel"].get("sha256"), b8["shade-per-panel"].get("chunks")) == (b10["shade-per-panel"].get("sha256"), b10["shade-per-panel"].get("chunks"))


def test_b11_explains_the_last_frame(synthetic):
    docs, state, _ = synthetic
    doc = docs["b11"]
    r = reports(doc)
    assert r["pick-distance"]["value"] == 0.0 and r["panels"] == len(state["frames"])
    assert r["sun-angles"] == len(shade.select_profile(len(state["frames"]))["angles"])
    assert 0 <= r["blocked-angles"] <= r["sun-angles"]
    assert not {"panel-index", "angle-count"} & set(r)
    assert not rows_of(doc, "unexpected-change") and not rows_of(doc, "file")


def test_a_read_only_step_reports_a_state_change(monkeypatch):
    state, intake = synthetic_state()
    real = shade.annual_shade

    def mutating(ents, mpu):
        state["shade_heatmap"].append({"vertices": [(0, 0), (1, 0), (1, 1), (0, 1)]})
        return real(ents, mpu)
    monkeypatch.setattr(shade, "annual_shade", mutating)
    rows = sev.step_rows("b9", state, intake)
    assert [r["id"] for r in rows if r["type"] == "unexpected-change"] == ["unexpected-change-1"]


def test_no_storage_strings_in_any_document(synthetic):
    docs, _, _ = synthetic
    for doc in docs.values():
        text = ev._serialize(doc)
        assert not storage_tokens_in(text)


def test_refusals():
    state, intake = synthetic_state()
    with pytest.raises(sev.EvidenceError):
        sev.step_rows("b7", state, intake)
    with pytest.raises(sev.EvidenceError):
        sev.run_steps(intake, REVISION, only="b12", state=state)
    with pytest.raises(sev.EvidenceError):
        sev.with_shade_state({"grid": None})
    with pytest.raises(sev.EvidenceError):
        sev.build_document(intake, "b8", "shade-sim", "shade-sim", [], "not-a-revision")
    with pytest.raises(sev.EvidenceError):
        sev.step_explain(state, ())
    with pytest.raises(sev.EvidenceError):
        sev.first_tracker_centre([{"type": "INSERT", "axis_start": (0, 0), "axis_end": (1, 0)}])
    with pytest.raises(sev.EvidenceError):
        sev.normalize_text("x", "bom-csv")


def test_the_cli_writes_documents_and_files(tmp_path, monkeypatch):
    intake_path = tmp_path / "intake.json"
    intake_path.write_text(json.dumps(terrain_intake()), encoding="utf-8")
    monkeypatch.setattr(ev, "fixture_revision", lambda path: REVISION)
    monkeypatch.setattr(sev, "studio_state", lambda intake: synthetic_state()[0])
    assert sev.main(["--intake", str(intake_path), "--out-dir", str(tmp_path / "out"), "--step", "b9",
                     "--files-dir", str(tmp_path / "files")]) == 0
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["b9.json"]
    written = (tmp_path / "files" / "studio-b9-shade.csv").read_bytes()
    assert written.startswith(b"\xef\xbb\xbfLEAFSHADE Shade Analysis\r\n")


# ---------------------------------------------------- the terrain fixture --

@pytest.fixture(scope="module")
def site():
    intake = compare.load_evidence(INTAKE)
    state = sev.studio_state(intake)
    docs = {}
    raw = {}
    for step, capability, operation in sev.B_STEPS:
        rows = sev.step_rows(step, state, intake)
        docs[step] = sev.build_document(intake, step, capability, operation, rows, REVISION)
        raw[step] = dict(state["shade_files"])
    return docs, raw


def _raw_size(text):
    return len(text.encode("utf-8"))


def _m(value):
    return {"kind": "length", "value": value, "unit": "m"}


_SIM = {"clearance-shift": _m(1.5), "max-ray": _m(400.0), "panel-samples": 1197, "panels-tinted": 1197,
        "profile": "balanced", "raw-median-clearance": _m(0.0), "ray-step": _m(3.0), "ray-tests": 258552,
        "shading-loss-percent": 0.0, "sun-angles": 216, "surface-cells": 13500, "surface-cols": 150,
        "surface-rows": 90, "target-clearance": _m(1.5)}
# Contract G25's report table: per step, every report name the plugin printed on
# the terrain capture and the value it printed. Test data only; the producer computes every value.
G25 = {
    "b8": _SIM,
    "b9": {"shading-loss-percent": 97.2, "simulated-hours": 8760, "tracker-rows": 1434},
    "b10": dict(_SIM, **{"replaced-markers": 1197, "scene-arrays": 0, "scene-grid-cols": 150,
                         "scene-grid-rows": 90}),
    "b11": {"blocked-angles": 0, "clearance-shift": _m(1.5), "max-ray": _m(400.0), "panels": 1197,
            "pick-distance": _m(0.0), "profile": "balanced", "raw-median-clearance": _m(0.0),
            "ray-step": _m(3.0), "shading-loss-percent": 0.0, "sun-angles": 216, "target-clearance": _m(1.5)},
}


def _shape(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, dict):
        return f"quantity:{value['kind']}:{value['unit']}"
    return type(value).__name__


def assert_g25(doc, step):
    """The step's report rows are exactly G25's names, each with G25's shape and the captured value
    (a length within the comparator's 1 mm)."""
    rows = rows_of(doc, "report")
    assert [r["id"]["entity_id"] for r in rows] == [f"report-{name}" for name in sorted(G25[step])]
    got = reports(doc)
    for name, want in G25[step].items():
        assert _shape(got[name]) == _shape(want), name
        if isinstance(want, dict):
            assert got[name]["value"] == pytest.approx(want["value"], abs=1e-3), name
        else:
            assert got[name] == want, name


def test_site_report_rows_are_the_g25_table(site):
    docs, _ = site
    assert set(G25) == set(sev.STEP_IDS)
    for step in sev.STEP_IDS:
        assert_g25(docs[step], step)


def test_site_b8_matches_the_committed_outcome(site):
    docs, raw = site
    doc = docs["b8"]
    markers = rows_of(doc, "shade-marker")
    assert len(markers) == 1197
    assert {m["color"] for m in markers} == {GREEN}
    assert all(m["panel"] is not None for m in markers)
    assert not rows_of(doc, "removed")
    assert_g25(doc, "b8")
    sizes = {role: _raw_size(text) for role, text in raw["b8"].items()}
    assert sizes == {"shade-azal-matrix": 1653, "shade-sam": 3211, "shade-per-panel": 4816417}
    assert files(doc)["shade-per-panel"]["lines"] == 258553


def test_site_b9_annual_loss_and_table(site):
    docs, raw = site
    doc = docs["b9"]
    assert_g25(doc, "b9")
    assert not rows_of(doc, "unexpected-change")
    written = raw["b9"]["shade-csv"]
    assert _raw_size(written) == 32496
    lines = text_of(files(doc)["shade-csv"]).split("\n")
    assert len(lines) - 1 == 1441
    assert lines[3] == "Clear-sky energy-weighted annual shading loss (%),97.24"
    assert lines[4] == "Time-weighted annual shading loss (%),98.13"
    assert lines[7:10] == ["Row 0,44.55,51.22", "Row 1,57.32,68.83", "Row 2,62.69,73.87"]
    assert lines[-4:-1] == ["Row 1431,100.00,100.00", "Row 1432,99.96,99.98", "Row 1433,99.41,99.69"]


def test_site_b10_replaces_markers_and_exports_the_terrain_scene(site):
    docs, raw = site
    doc = docs["b10"]
    assert [(x["of"], x["count"]) for x in rows_of(doc, "removed")] == [("shade-marker", 1197)]
    assert len(rows_of(doc, "shade-marker")) == 1197
    assert_g25(doc, "b10")
    sizes = {role: _raw_size(text) for role, text in raw["b10"].items()}
    assert sizes == {"shade-azal-matrix": 1653, "shade-sam": 3211, "shade-per-panel": 4816417,
                     "scene-dae": 5570, "scene-pvc": 8079}
    assert files(docs["b8"])["shade-per-panel"]["sha256"] == files(doc)["shade-per-panel"]["sha256"]


def test_site_b11_explains_panel_1196_unblocked(site):
    docs, _ = site
    doc = docs["b11"]
    assert_g25(doc, "b11")
    assert not rows_of(doc, "unexpected-change")


def test_site_documents_carry_no_storage_strings(site):
    docs, _ = site
    for doc in docs.values():
        text = ev._serialize(doc)
        assert not storage_tokens_in(text)


def test_g24_large_file_is_carried_by_its_digest():
    nl = chr(10)
    big = "PanelIndex,AzimuthDeg" + nl + "".join("%d,%d%s" % (i, i % 360, nl) for i in range(150000))
    assert len(big) > sev.G24_DIGEST_THRESHOLD
    row = sev.file_rows({"shade-per-panel": big})[0]
    assert "chunks" not in row and row["chars"] == len(big) and len(row["sha256"]) == 64
    assert "".join(row["head"]).count(nl) == 40
