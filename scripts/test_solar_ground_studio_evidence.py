"""Studio ground evidence (contract v6, G10 to G17) from small synthetic intakes.

Every intake is authored in this file: no capture, no docs/parity fixture, no network,
so the collected count is the same on every runner and nothing skips except the one
CLI case that needs a git executable to commit its intake.

Covered: every row kind (frame, pile-set, marker in all three roles, terrain-grid,
terrain-mesh, off-grid-face, removed), G12 id and vertex ordering, G16 pile-set
grouping and the terrain-mesh row, the G17 shapes (every coordinate quantity is one
point, pile sets split by length, elevations as lengths), G13 chaining (each step
computed from Studio's own prior state), the frozen comparator accepting every
document against itself, the comparator's document bounds at the terrain fixture's
own t1 and t4 sizes (exact node counts) and at the largest grid that fits, and
refusal of malformed intakes.
"""
from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


def _load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ev = _load("solar_ground_studio_evidence")
compare = ev.compare
REVISION = "0123456789abcdef0123456789abcdef01234567"

# 1 x 4 m frames (Portrait, one module across, two along at 2 m), a 3 x 8 m boundary:
# two rows of three frames. Two piles per frame at (x + 0.5, y + 1) and (x + 0.5, y + 3);
# the pile window starts at 3 m so every 2.5 m pile is out of range.
PRESET = {"Name": "Small", "ModuleLengthM": 2.0, "ModuleWidthM": 1.0, "Rows": 1, "Columns": 2,
          "HorizontalGapM": 0.0, "VerticalGapM": 0.0, "ColorIndex": 5, "PileTemplateName": "Two",
          "MaxSlopePercent": 80.0, "MaxCrossAxisSlopePct": 5.0,
          "Piling": {"MinPileLengthM": 3.0, "MaxPileLengthM": 6.0}}
TEMPLATE = {"Name": "Two", "HorizontalPoleCount": 1, "VerticalPoleCount": 2, "PileDiameterM": 0.2,
            "PileRevealM": 1.0, "PileEmbedmentM": 1.5}
BOUNDARY = [[0.0, 0.0], [3.0, 0.0], [3.0, 8.0], [0.0, 8.0]]


def generate_intake():
    return {"units": "m", "boundary": deepcopy(BOUNDARY), "active_preset": deepcopy(PRESET),
            "pile_template": deepcopy(TEMPLATE)}


def terrain_intake():
    """A 30 x 9 m plane rising 0.3 m per metre east, as 3 x 3 m faces: a 45 x 150 grid."""
    faces = []
    for i in range(10):
        for j in range(3):
            x0, x1, y0, y1 = 3.0 * i, 3.0 * i + 3.0, 3.0 * j, 3.0 * j + 3.0
            faces.append([[x0, y0, 0.3 * x0], [x1, y0, 0.3 * x1], [x1, y1, 0.3 * x1], [x0, y1, 0.3 * x0]])
    return dict(generate_intake(), terrain_faces=faces)


@pytest.fixture(scope="module")
def gen_docs():
    return ev.run_scenario(generate_intake(), "generate", REVISION)


@pytest.fixture(scope="module")
def terrain_docs():
    return ev.run_scenario(terrain_intake(), "terrain", REVISION)


def rows_of(doc, row_type=None):
    return [r for r in doc["after"]["rows"] if row_type is None or r["type"] == row_type]


def pts(quantities):
    """The points of a list of coordinate quantities."""
    return [q["value"] for q in quantities]


def bbox(row):
    return row["bbox"]["min"]["value"] + row["bbox"]["max"]["value"]


def nodes(value):
    """The comparator's _bounded count: every dict, list and scalar is one node."""
    if isinstance(value, dict):
        return 1 + sum(nodes(v) for v in value.values())
    if isinstance(value, list):
        return 1 + sum(nodes(v) for v in value)
    return 1


def coordinate_quantities(value):
    """Every {"kind": "coordinate", ...} quantity anywhere inside value."""
    if isinstance(value, dict):
        if value.get("kind") == "coordinate":
            yield value
        for v in value.values():
            yield from coordinate_quantities(v)
    elif isinstance(value, list):
        for v in value:
            yield from coordinate_quantities(v)


# ------------------------------------------------------------ row kinds --

def test_generate_scenario_emits_every_step_in_order(gen_docs):
    assert list(gen_docs) == ["g1", "g2", "g3", "g4", "g5", "g6"]
    assert [d["provenance"]["capability"] for d in gen_docs.values()] == [
        "frame-generate", "frame-collision-detect", "piling-generate", "pile-length-range-check",
        "frame-generate", "frame-collision-detect"]
    for step, doc in gen_docs.items():
        assert doc["after"]["source_revision"] == step and doc["after"]["format"] == "ground-v1"


def test_frame_rows_ids_cells_colour_and_vertex_order(gen_docs):
    frames = rows_of(gen_docs["g1"], "frame")
    assert [r["id"]["entity_id"] for r in frames] == [f"frame-{n}" for n in range(1, 7)]
    assert [(r["row_index"], r["col_index"]) for r in frames] == [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2)]
    assert {r["color_index"] for r in frames} == {5}
    assert all((r["quantity"], r["unit"]) == (1, "each") for r in frames)
    # G17: four [x, y] point quantities, not a flat list.
    assert pts(frames[1]["vertices"]) == [[1.0, 0.0], [2.0, 0.0], [2.0, 4.0], [1.0, 4.0]]
    assert all(len(r["vertices"]) == 4 for r in frames)
    assert all(q["kind"] == "coordinate" and q["unit"] == "m" and len(q["value"]) == 2
               for q in frames[1]["vertices"])


def test_canonical_corners_start_low_and_run_counter_clockwise():
    clockwise_from_top = [(1, 4), (2, 4), (2, 0), (1, 0)]
    assert ev.canonical_corners(clockwise_from_top) == [(1.0, 0.0), (2.0, 0.0), (2.0, 4.0), (1.0, 4.0)]
    # A tie on y is broken by x; values within 1e-6 of each other tie.
    assert ev.canonical_corners([(5, 1e-9), (5, 3), (0, 3), (0, 0)])[0] == (0.0, 0.0)


def test_pile_sets_group_by_frame_in_frame_id_order(gen_docs):
    sets = rows_of(gen_docs["g3"], "pile-set")
    assert [r["frame"] for r in sets] == [{"entity_id": f"frame-{n}"} for n in range(1, 7)]
    assert [r["id"]["entity_id"] for r in sets] == [f"pile-set-{n}" for n in range(1, 7)]
    first = sets[0]
    assert first["count"] == 2
    assert first["diameter"] == {"kind": "length", "value": 0.2, "unit": "m"}
    # G17: [x, y, z_bottom] points plus ONE length (reveal 1.0 + embedment 1.5).
    assert first["length"] == {"kind": "length", "value": 2.5, "unit": "m"}
    assert [p for q in pts(first["piles"]) for p in q] == pytest.approx([0.5, 1.0, -1.5, 0.5, 3.0, -1.5])
    assert all(len(q["value"]) == 3 for r in sets for q in r["piles"])
    assert pts(sets[4]["piles"])[0][:2] == pytest.approx([1.5, 5.0])
    assert rows_of(gen_docs["g3"], "removed") == []


def _pile(x, y, bottom=0.0, top=1.0, diameter=0.2):
    return {"cylinder": {"center_x": x, "center_y": y, "bottom_z": bottom, "top_z": top},
            "pile": {"diameter_m": diameter}}


def test_piles_outside_every_frame_form_the_last_null_set():
    live = [{"vertices": [(0, 0), (1, 0), (1, 4), (0, 4)]}]
    rows = ev.pile_set_rows([_pile(9, 9), _pile(0.5, 3), _pile(9, 1), _pile(0.5, 1)], live)
    assert [(r["frame"], r["count"]) for r in rows] == [("frame-1", 2), (None, 2)]
    assert pts(rows[1]["piles"])[0] == [9.0, 1.0, 0.0]
    with pytest.raises(ev.EvidenceError, match="diameters"):
        ev.pile_set_rows([_pile(0.5, 1), _pile(0.5, 3, diameter=0.3)], live)


def test_a_frames_piles_split_into_one_set_per_distinct_length():
    live = [{"vertices": [(0, 0), (1, 0), (1, 4), (0, 4)]}, {"vertices": [(2, 0), (3, 0), (3, 4), (2, 4)]}]
    piles = [_pile(0.5, 1, -2.0, 1.0), _pile(0.5, 2, -1.0, 1.0), _pile(0.5, 3, -1.0, 1.0),
             # float noise in top - bottom (0.1 + 2.0 - 0.1) is still the length 2.0
             _pile(2.5, 1, 0.1, 0.1 + 2.0), _pile(2.5, 3, 0.0, 2.0), _pile(9, 9, 0.0, 5.0)]
    rows = ev.pile_set_rows(piles, live)
    assert [(r["id"], r["frame"], r["length"]["value"], r["count"]) for r in rows] == [
        ("pile-set-1", "frame-1", 2.0, 2), ("pile-set-2", "frame-1", 3.0, 1),
        ("pile-set-3", "frame-2", 2.0, 2), ("pile-set-4", None, 5.0, 1)]
    assert pts(rows[0]["piles"]) == [[0.5, 2.0, -1.0], [0.5, 3.0, -1.0]]
    assert pts(rows[1]["piles"]) == [[0.5, 1.0, -2.0]]
    assert all(r["length"]["kind"] == "length" and r["length"]["unit"] == "m" for r in rows)


def test_markers_carry_role_and_bbox(gen_docs, terrain_docs):
    assert rows_of(gen_docs["g2"]) == []
    collisions = rows_of(gen_docs["g6"], "marker")
    assert len(collisions) == 6 and {r["role"] for r in collisions} == {"collision"}
    # G17: bbox is {min, max}, each one [x, y] point.
    assert collisions[0]["bbox"] == {"min": {"kind": "coordinate", "value": [0.0, 0.0], "unit": "m"},
                                     "max": {"kind": "coordinate", "value": [1.0, 4.0], "unit": "m"}}
    ranged = rows_of(gen_docs["g4"], "marker")
    assert len(ranged) == 12 and {r["role"] for r in ranged} == {"pile-range"}
    assert bbox(ranged[0]) == pytest.approx([0.4, 0.9, 0.6, 1.1])
    slope = rows_of(terrain_docs["t6"], "marker")
    assert slope and {r["role"] for r in slope} == {"slope"}
    centres = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for b in (bbox(r) for r in slope)]
    assert centres == sorted(centres, key=lambda c: (round(c[1], 6), round(c[0], 6)))


def test_terrain_grid_and_mesh_rows(terrain_docs):
    t1 = terrain_docs["t1"]
    assert [r["type"] for r in rows_of(t1)] == ["terrain-grid", "terrain-mesh"]
    grid, mesh = rows_of(t1)
    assert (grid["rows"], grid["cols"]) == (45, 150)
    # G17: extent is {min, max} [x, y] points; elevations are length quantities.
    extent = {"min": {"kind": "coordinate", "value": [0.0, 0.0], "unit": "m"},
              "max": {"kind": "coordinate", "value": [30.0, 9.0], "unit": "m"}}
    assert grid["extent"] == extent
    assert len(grid["elevations"]) == 45 * 150
    assert all(set(q) == {"kind", "value", "unit"} and (q["kind"], q["unit"]) == ("length", "m")
               and type(q["value"]) is float for q in grid["elevations"])
    assert grid["elevations"][0]["value"] == pytest.approx(0.0)
    assert (mesh["rows"], mesh["cols"], mesh["extent"]) == (45, 150, extent)
    assert len(mesh["cell_colors"]) == 44 * 149
    assert set(mesh["cell_colors"]) <= {0x00C800, 0xFFC800, 0xDC0000}


def test_removed_rows_count_erased_entities(terrain_docs):
    t2 = rows_of(terrain_docs["t2"])
    assert [r["type"] for r in t2] == ["removed", "terrain-mesh"]
    assert (t2[0]["of"], t2[0]["count"]) == ("terrain-mesh", 44 * 149)
    t7 = rows_of(terrain_docs["t7"])
    assert [(r["type"], r["of"], r["count"]) for r in t7] == [
        ("removed", "marker", len(rows_of(terrain_docs["t6"], "marker")))]
    assert ev.removed_rows({"pile-set": 0, "marker": 3, "frame": 1}) == [
        {"id": "removed-1", "type": "removed", "quantity": 1, "unit": "each", "of": "frame", "count": 1},
        {"id": "removed-2", "type": "removed", "quantity": 1, "unit": "each", "of": "marker", "count": 3}]


def test_off_grid_face_is_four_3d_points():
    corners = [[0, 0, 1], [3, 0, 1.5], [3, 3, 2], [0, 3, 1.25]]
    row = ev.off_grid_face_row(1, corners, 3)
    assert (row["id"], row["type"], row["color_index"]) == ("off-grid-face-1", "off-grid-face", 3)
    assert pts(row["vertices"]) == [[0.0, 0.0, 1.0], [3.0, 0.0, 1.5], [3.0, 3.0, 2.0], [0.0, 3.0, 1.25]]
    doc = ev.build_document(terrain_intake(), "terrain", "t2", "terrain-mesh-render", "mesh", [row], REVISION)
    assert compare.compare(doc, doc, "exports", capability="terrain-mesh-render")["verdict"] == "pass"
    with pytest.raises(ev.EvidenceError):
        ev.off_grid_face_row(1, corners[:3], 3)
    with pytest.raises(ev.EvidenceError):
        ev.off_grid_face_row(1, [c[:2] for c in corners], 3)


def test_point_is_exactly_one_point_of_two_or_three_numbers():
    assert ev.point([1, 2]) == {"kind": "coordinate", "value": [1.0, 2.0], "unit": "m"}
    assert ev.point((1, 2, 3))["value"] == [1.0, 2.0, 3.0]
    for bad in ([1.0], [1, 2, 3, 4], [1.0, float("nan")], [True, 1.0], [1.0, "2"]):
        with pytest.raises(ev.EvidenceError):
            ev.point(bad)


def test_every_coordinate_in_every_document_is_one_point(gen_docs, terrain_docs):
    found = 0
    for docs in (gen_docs, terrain_docs):
        for step, doc in docs.items():
            for q in coordinate_quantities(doc):
                found += 1
                assert set(q) == {"kind", "value", "unit"} and q["unit"] == "m", step
                assert isinstance(q["value"], list) and len(q["value"]) in (2, 3), step
                assert all(type(v) in (int, float) for v in q["value"]), step
            # No row carries a bare number list except the mesh's colour integers.
            for row in doc["after"]["rows"]:
                for key, value in row.items():
                    if isinstance(value, list) and key != "cell_colors":
                        assert all(isinstance(v, dict) for v in value), (step, key)
    assert found > 0


# ------------------------------------------------------------- chaining --

def test_steps_chain_from_studio_state(gen_docs, terrain_docs):
    # g5 draws the same six frames again, and only because g1's frames are still in the
    # state does g6 find six full overlaps.
    assert rows_of(gen_docs["g5"]) == rows_of(gen_docs["g1"])
    assert len(rows_of(gen_docs["g6"], "marker")) == 6
    # t3 generates on the grid t1 committed and t4 drapes onto it. The committed grid is
    # the IDW resample of the faces (LandXmlImporter.ResampleToGrid), not the analytic
    # plane, so the expected ground comes from the same sampler the piling engine uses:
    # bottom = ground - embedment (LeafPilingCommand.cs:1290-1411, DrawPiles).
    state = ev.new_state()
    ev.step_terrain_import(state, {"intake": terrain_intake()})
    assert [q["value"] for q in rows_of(terrain_docs["t1"], "terrain-grid")[0]["elevations"]] == [
        float(z) for z in state["grid"]["elevations"]]
    ground = ev._terrain_z(state)
    frames = rows_of(terrain_docs["t3"], "frame")
    sets = rows_of(terrain_docs["t4"], "pile-set")
    assert frames and len(sets) == len(frames)
    assert sets[0]["length"]["value"] == pytest.approx(2.5)
    x, y, bottom = pts(sets[0]["piles"])[0]
    assert bottom == ground(x, y) - 1.5
    assert len(rows_of(terrain_docs["t5"], "marker")) == sum(r["count"] for r in sets)


def test_one_step_equals_the_same_step_of_a_full_run(gen_docs):
    only = ev.run_scenario(generate_intake(), "generate", REVISION, only="g6")
    assert list(only) == ["g6"] and only["g6"] == gen_docs["g6"]


# ------------------------------------------------------ document contract --

def test_document_fields_and_hashes(gen_docs):
    intake = generate_intake()
    doc = gen_docs["g1"]
    assert set(doc) == compare.EVIDENCE_KEYS
    assert doc["fixture_sha256"] == compare.semantic_hash(intake)
    assert doc["parameters"] == {"units_keyword": "Meters", "grid_cells_long_axis": 150, "active_preset": "Small"}
    assert doc["input_sha256"] == compare.semantic_hash(
        {"fixture_sha256": doc["fixture_sha256"], "parameters": doc["parameters"]})
    assert doc["output_sha256"] == compare.semantic_hash(doc["after"])
    assert doc["entity_mapping"] == {f"frame-{n}": f"frame-{n}" for n in range(1, 7)}
    assert (doc["revision"], doc["units"], doc["state"], doc["survived_reopen"]) == (REVISION, "m", "committed", True)
    assert doc["versions"]["schema"] == compare.SCHEMA
    assert (doc["versions"]["catalog"], doc["versions"]["solver"]) == ("none", "none")
    assert gen_docs["g2"]["entity_mapping"] == {}


def test_comparator_accepts_every_document_against_itself(gen_docs, terrain_docs):
    for docs in (gen_docs, terrain_docs):
        for doc in docs.values():
            result = compare.compare(doc, doc, "exports", capability=doc["versions"]["capability"])
            assert result["verdict"] == "pass", result["diffs"]


def test_comparator_reports_a_moved_frame(gen_docs):
    moved = deepcopy(gen_docs["g1"])
    moved["after"]["rows"][0]["vertices"][0]["value"][0] += 0.01
    moved["output_sha256"] = compare.semantic_hash(moved["after"])
    result = compare.compare(gen_docs["g1"], moved, "exports", capability="frame-generate")
    assert result["verdict"] == "fail"
    assert result["diffs"] == ["after/rows/0/vertices/0: quantity differs"]


def _grid_doc(rows, cols):
    grid = {"elevations": [float(i % 7) for i in range(rows * cols)], "rows": rows, "cols": cols,
            "x_min": 0.0, "x_max": 500.0, "y_min": 0.0, "y_max": 300.0}
    mesh = ev.terrain.draw_grid_mesh(grid["elevations"], rows, cols, 0.0, 500.0, 0.0, 300.0, 1.0)
    out = [ev.terrain_grid_row(grid), ev.terrain_mesh_row(grid, mesh)]
    return ev.build_document(terrain_intake(), "terrain", "t1", "terrain-import", "topo-import", out, REVISION)


def test_an_empty_document_is_66_nodes(gen_docs):
    # The fixed part of every document; the size comments in the module build on it.
    assert rows_of(gen_docs["g2"]) == [] and nodes(gen_docs["g2"]) == 66


def test_terrain_fixture_t1_grid_fits_the_comparator_bounds():
    # The terrain fixture's t1 is a 90 x 150 grid plus its mesh: 66 + 2 + 22 + 4 * 13,500
    # + 22 + 89 * 149 = 67,373 nodes.
    doc = _grid_doc(90, 150)
    assert nodes(doc) == 67_373 <= compare.MAX_NODES
    assert len(ev._serialize(doc).encode("utf-8")) <= compare.MAX_BYTES


def test_terrain_fixture_t4_piles_fit_the_comparator_bounds():
    # The terrain fixture's t4: 1197 frames, 8 draped piles each (9576), one length per
    # frame. 66 + 1197 * 2 (mapping: its pile sets and the frames they reference) + 1197 * 18 + 9576 * 7 = 91,038 nodes.
    live, piles = [], []
    for i in range(1197):
        x, y, handle = 2.0 * (i % 63), 5.0 * (i // 63), f"{i + 1:X}"
        live.append({"vertices": [(x, y), (x + 1, y), (x + 1, y + 4), (x, y + 4)], "handle": handle})
        for j in range(8):
            bottom = -1.5 + 0.001 * j
            piles.append({"cylinder": {"center_x": x + 0.5, "center_y": y + 0.25 + 0.5 * j,
                                       "bottom_z": bottom, "top_z": bottom + 2.5},
                          "pile": {"diameter_m": 0.2, "source_tracker": handle}})
    rows = ev.pile_set_rows(piles, live)
    assert len(rows) == 1197 and sum(r["count"] for r in rows) == 9576
    doc = ev.build_document(terrain_intake(), "terrain", "t4", "piling-generate", "piling", rows, REVISION)
    assert nodes(doc) == 91_038 <= compare.MAX_NODES
    assert len(ev._serialize(doc).encode("utf-8")) <= compare.MAX_BYTES


def test_largest_grid_that_fits_and_one_row_more_is_refused():
    # A 150-cell import holds at most 133 x 150 under the node bound (99,580 nodes);
    # 134 x 150 is 100,329 and the comparator refuses it.
    doc = _grid_doc(133, 150)
    assert nodes(doc) == 99_580
    assert len(ev._serialize(doc).encode("utf-8")) <= compare.MAX_BYTES
    with pytest.raises(ev.EvidenceError, match="refused by the comparator"):
        _grid_doc(134, 150)


# ------------------------------------------------------------ refusals --

def _without(key):
    intake = generate_intake()
    del intake[key]
    return intake


MALFORMED = [
    ("missing preset", lambda: _without("active_preset"), "generate"),
    ("extra key", lambda: dict(generate_intake(), handles=[]), "generate"),
    ("feet", lambda: dict(generate_intake(), units="ft"), "generate"),
    ("two-vertex boundary", lambda: dict(generate_intake(), boundary=BOUNDARY[:2]), "generate"),
    ("nan vertex", lambda: dict(generate_intake(), boundary=BOUNDARY[:3] + [[0.0, float("nan")]]), "generate"),
    ("unnamed preset", lambda: dict(generate_intake(), active_preset={"Rows": 1}), "generate"),
    ("faces on generate", lambda: terrain_intake(), "generate"),
    ("no faces on terrain", lambda: generate_intake(), "terrain"),
    ("three-corner face", lambda: dict(terrain_intake(), terrain_faces=[[[0, 0, 0], [1, 0, 0], [1, 1, 0]]]),
     "terrain"),
    ("not an object", lambda: [generate_intake()], "generate"),
    ("unknown kind", lambda: generate_intake(), "roof"),
]


@pytest.mark.parametrize("label,make,kind", MALFORMED, ids=[m[0] for m in MALFORMED])
def test_malformed_intake_is_refused(label, make, kind):
    with pytest.raises(ev.EvidenceError):
        ev.run_scenario(make(), kind, REVISION)


def test_unknown_step_and_bad_revision_are_refused():
    with pytest.raises(ev.EvidenceError, match="not in the generate scenario"):
        ev.run_scenario(generate_intake(), "generate", REVISION, only="t1")
    with pytest.raises(ev.EvidenceError, match="revision"):
        ev.run_scenario(generate_intake(), "generate", "HEAD")


# ------------------------------------------------------------------ CLI --

def test_cli_refuses_an_untracked_intake(tmp_path):
    intake = tmp_path / "intake.json"
    intake.write_text(json.dumps(generate_intake()), encoding="utf-8")
    out = tmp_path / "out"
    assert ev.main(["--intake", str(intake), "--kind", "generate", "--out-dir", str(out)]) == 2
    assert not out.exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="needs a git executable to commit the intake")
def test_cli_writes_one_document_per_step_from_a_committed_intake(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    intake = repo / "intake.json"
    intake.write_text(json.dumps(generate_intake()), encoding="utf-8")

    def git(*args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                        "-c", "commit.gpgsign=false", *args], cwd=repo, check=True, capture_output=True,
                       timeout=30)
    git("init", "-q")
    git("add", "intake.json")
    git("commit", "-q", "--no-verify", "-m", "intake")
    out = tmp_path / "out"
    assert ev.main(["--intake", str(intake), "--kind", "generate", "--out-dir", str(out)]) == 0
    assert sorted(p.name for p in out.iterdir()) == [f"g{n}.json" for n in range(1, 7)]
    g1 = json.loads((out / "g1.json").read_text(encoding="utf-8"))
    assert len(g1["revision"]) == 40 and g1["fixture_sha256"] == compare.semantic_hash(generate_intake())
    assert ev.main(["--intake", str(intake), "--kind", "generate", "--out-dir", str(tmp_path / "one"),
                    "--step", "g3"]) == 0
    assert [p.name for p in (tmp_path / "one").iterdir()] == ["g3.json"]


def test_pile_step_mapping_covers_the_frames_its_pile_sets_reference(gen_docs):
    # G8: a piling step's rows point at frames drawn in an earlier step; those ids are referenced,
    # so they belong in the mapping even though no frame row appears in this step.
    doc = gen_docs["g3"]
    ref = lambda value: value["entity_id"] if isinstance(value, dict) else value
    referenced = {ref(row["frame"]) for row in doc["after"]["rows"] if row.get("frame")}
    assert referenced and referenced <= set(doc["entity_mapping"])
    assert set(doc["entity_mapping"]) == referenced | {ref(row["id"]) for row in doc["after"]["rows"]}


def test_every_document_carries_the_ledger_capability_version(gen_docs):
    # The gate rejects a receipt whose studio capability_version differs from the ledger's ("0").
    assert {d["versions"]["capability"] for d in gen_docs.values()} == {"0"}
