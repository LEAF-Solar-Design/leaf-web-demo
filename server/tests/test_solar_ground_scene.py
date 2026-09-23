"""Studio's arrays and PVsyst scene export against the plugin, computed (contract G20).

Covered: LEAFDEFINEARRAY with every default and its refusals, the next free key, the outline
geometry, LEAFLISTARRAYS order and report text, LEAFDELETEARRAY (cancel, unknown key, removal,
the dictionary's case-insensitive match against the outline's ordinal one), the F4 and
millimetre formatting the writers use, ToSpec's frames per row pitch, the plugin's own
PvcExporterTests cases (ground only, one array with a DSM, two arrays, obstacles inside a
footprint, NaN ground, five frames per row pitch), the DAE's obstacles, and the licensed a7
scene: the DAE and PVC are COMPUTED from the committed terrain intake (Studio's own t1 grid,
G13) and the a5 array, and must equal the plugin's files after the G20 normalization. The
expected files are pinned by their normalized SHA-256, line count and raw byte length only;
the captures themselves stay private.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


scene = _load("solar_ground_scene", ROOT / "server" / "solar_ground_scene.py")
terrain = _load("solar_ground_terrain", ROOT / "server" / "solar_ground_terrain.py")

NS = "{http://www.collada.org/2005/11/COLLADASchema}"
INTAKE = ROOT / "docs" / "parity" / "evidence" / "ground" / "terrain" / "intake.json"
# The plugin's a7 files (LEAFEXPORTSCENE on the terrain fixture after a5), pinned without their
# text: SHA-256 of the G20-normalized text, its line count, and the file's byte length as written
# with the two asset stamps below.
A7_DAE = {"sha256": "e1d9e2b506c0a5d601b8028ce42d7ebbb1df99eee73b5eb7f6a2eeda1719715f", "lines": 42, "bytes": 5570}
A7_PVC = {"sha256": "7f25d1f405032a92b1a168ca4aa3e7ce854388caa4514cca1d8ac801030d293f", "lines": 159, "bytes": 9991}
A7_CREATED = "2026-09-23T10:18:06.7121837Z"
A7_MODIFIED = "2026-09-23T10:18:06.7121935Z"


def g20_normalized(text, dae):
    """G20: LF line ends, no BOM, the DAE's <created> and <modified> contents emptied."""
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if dae:
        text = re.sub(r"<(created|modified)>[^<]*</\1>", r"<\1></\1>", text)
    return text


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def a5_record(records=()):
    return scene.define_array(list(records), {"centre_x": 250.0, "centre_y": 250.0})


def spec(**over):
    """An ArraySpec as the plugin tests build it: 10 x 4 m at the origin, 15 deg, south."""
    record = dict(key="array_0", centre_x=0.0, centre_y=0.0, half_x=5.0, half_y=2.0, tilt_deg=15.0,
                  azimuth_deg=0.0, module_width_m=0.992, module_height_m=1.640, orientation=0,
                  modules_x=1, modules_y=1, module_x_spacing_m=0.02, module_y_spacing_m=0.02,
                  row_pitch_m=0.0, module_manufacturer="generic", module_name="generic")
    record.update(over)
    return scene.to_spec(record)


def flat(rows, cols, z=0.0):
    return [z] * (rows * cols)


def pvc_tree(text):
    return ET.fromstring(text.encode("utf-8"))


def geometries_by_material(root, material):
    return sum(1 for g in root.iter(NS + "geometry")
               if any(t.get("material") == material for t in g.iter(NS + "triangles")))


# ----------------------------------------------------------- the array store --

def test_define_array_takes_every_default():
    a = a5_record()
    assert a["key"] == "array_0"
    assert (a["centre_x"], a["centre_y"]) == (250.0, 250.0)
    assert (a["modules_x"], a["modules_y"], a["orientation"]) == (100, 85, 0)
    assert (a["module_width_m"], a["module_height_m"]) == (0.992, 1.640)
    assert (a["module_x_spacing_m"], a["module_y_spacing_m"]) == (0.02, 0.02)
    assert (a["tilt_deg"], a["azimuth_deg"], a["row_pitch_m"]) == (15.0, 0.0, 0.0)
    assert (a["module_manufacturer"], a["module_name"]) == ("generic", "generic")
    # Landscape: 100 x 1.640 + 99 x 0.02 wide, 85 x 0.992 + 84 x 0.02 deep.
    assert a["half_x"] == pytest.approx(82.99, abs=1e-12)
    assert a["half_y"] == pytest.approx(43.0, abs=1e-12)


def test_define_array_portrait_swaps_the_module_sides_and_odd_orientation_is_landscape():
    p = scene.define_array([], {"orientation": 1, "modules_x": 2, "modules_y": 3})
    assert p["half_x"] == pytest.approx((2 * 0.992 + 0.02) / 2)
    assert p["half_y"] == pytest.approx((3 * 1.640 + 2 * 0.02) / 2)
    assert scene.define_array([], {"orientation": 7})["orientation"] == 0
    assert scene.define_array([], {"modules_x": 2.9})["modules_x"] == 2      # (int) truncates


def test_define_array_stores_nothing_under_one_module_and_refuses_negative_sizes():
    assert scene.define_array([], {"modules_x": 0}) is None
    assert scene.define_array([], {"modules_y": 0.5}) is None
    with pytest.raises(scene.SceneInputError):
        scene.define_array([], {"module_width_m": -1.0})
    with pytest.raises(scene.SceneInputError):
        scene.define_array([], {"centre_x": math.nan})
    with pytest.raises(scene.SceneInputError):
        scene.define_array([], {"not_a_prompt": 1.0})
    assert scene.define_array([], {"centre_x": -5.0, "tilt_deg": -10.0})["centre_x"] == -5.0


def test_define_array_reads_project_settings_as_prompt_defaults():
    a = scene.define_array([], None, {"default_tilt_deg": 25.0, "module_manufacturer": "Acme",
                                      "module_name": None})
    assert a["tilt_deg"] == 25.0 and a["module_manufacturer"] == "Acme" and a["module_name"] == "generic"


def test_next_key_takes_the_first_unused_number():
    a0 = a5_record()
    a2 = dict(a0, key="array_2")
    assert scene.next_key([a0, a2]) == "array_1"
    assert scene.define_array([a0, a2])["key"] == "array_1"


def test_outline_is_the_footprint_rotated_about_the_centre():
    def flat_points(points):
        return [v for p in points for v in p]
    a = a5_record()
    assert flat_points(scene.array_outline(a)) == pytest.approx(
        [167.01, 207.0, 332.99, 207.0, 332.99, 293.0, 167.01, 293.0])
    turned = scene.array_outline(dict(a, azimuth_deg=90.0))
    assert flat_points(turned) == pytest.approx([293.0, 167.01, 293.0, 332.99, 207.0, 332.99, 207.0, 167.01])


def test_list_arrays_orders_by_key_and_reports_as_the_plugin_prints():
    a0 = a5_record()
    a1 = dict(a0, key="array_1", orientation=1)
    assert [r["key"] for r in scene.list_arrays([a1, a0])] == ["array_0", "array_1"]
    report = scene.list_report([a0])
    assert report == ["\nLEAFLISTARRAYS: 1 array(s):\n",
                      "  array_0: 100\u00d785=8500 modules (Landscape), 166.0\u00d786.0 m, "
                      "tilt=15.0\u00b0, azim=0.0\u00b0, centre=(250.0, 250.0)\n"]
    assert scene.list_report([]) == ["\nLEAFLISTARRAYS: no arrays defined. Use LEAFDEFINEARRAY.\n"]


def test_delete_array_cancel_unknown_and_removal():
    a0 = a5_record()
    outline = {"key": "array_0", "vertices": scene.array_outline(a0)}
    assert scene.delete_array([], [], "array_0")["status"] == "no_arrays"
    assert scene.delete_array([a0], [outline], ".")["status"] == "cancelled"
    missing = scene.delete_array([a0], [outline], "array_9")
    assert missing["status"] == "not_found" and missing["records"] == [a0]
    gone = scene.delete_array([a0], [outline], " array_0 ")
    assert (gone["status"], gone["key"], gone["records"], gone["outlines"], gone["erased_outlines"]) == \
        ("removed", "array_0", [], [], 1)


def test_delete_array_matches_the_record_ignoring_case_but_the_outline_exactly():
    a0 = a5_record()
    outline = {"key": "array_0", "vertices": scene.array_outline(a0)}
    result = scene.delete_array([a0], [outline], "ARRAY_0")
    assert result["status"] == "removed" and result["records"] == []
    assert result["erased_outlines"] == 0 and len(result["outlines"]) == 1


def test_malformed_records_are_refused():
    a0 = a5_record()
    for bad in (dict(a0, half_x=math.inf), dict(a0, modules_x=1.5), dict(a0, key=""),
                {k: v for k, v in a0.items() if k != "tilt_deg"}, dict(a0, module_name="bad\x01")):
        with pytest.raises(scene.SceneInputError):
            scene.list_arrays([bad])
    with pytest.raises(scene.SceneInputError):
        scene.list_arrays([a0, dict(a0, key="ARRAY_0")])


# ------------------------------------------------------------- formatting --

def test_f4_is_the_exact_value_rounded_half_to_even_with_the_sign_kept():
    assert scene.net_fixed(0.03125, 4) == "0.0312"
    assert scene.net_fixed(0.09375, 4) == "0.0938"
    assert scene.net_fixed(-0.0, 4) == "-0.0000"
    assert scene.net_fixed(-0.00001, 4) == "-0.0000"
    assert scene.net_fixed(250.0, 4) == "250.0000"
    assert scene.net_fixed(2.5, 0) == "2"
    assert scene.net_fixed(165.98, 1) == "166.0"


def test_utc_stamp_has_seven_fractional_digits():
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{7}Z", scene.utc_stamp())


def test_to_spec_rounds_frames_per_row_pitch_half_to_even():
    assert spec()["frames_in_y"] == 1
    assert spec(half_y=10.0, row_pitch_m=4.0)["frames_in_y"] == 5
    assert spec(half_y=10.0, row_pitch_m=8.0)["frames_in_y"] == 2
    assert spec()["maintenance_margin_m"] == 2.0


# ------------------------------------------------ the plugin's exporter cases --

def test_pvc_ground_only_has_one_topography_mesh_and_no_frames():
    text = scene.write_pvc(flat(20, 20), None, 20, 20, -10.0, 10.0, -10.0, 10.0, [])
    root = pvc_tree(text)
    assert geometries_by_material(root, "Material3") == 1
    assert geometries_by_material(root, "Material0") == 0
    assert len(root.findall(f"{NS}library_materials/{NS}material")) == 4


def test_pvc_one_array_with_a_dsm_has_one_frame_and_obstacles():
    rows = cols = 40
    dsm = flat(rows, cols)
    for r in range(30, 36):
        for c in range(30, 36):
            dsm[r * cols + c] = 10.0
    text = scene.write_pvc(flat(rows, cols), dsm, rows, cols, -20.0, 20.0, -20.0, 20.0, [spec()])
    root = pvc_tree(text)
    assert geometries_by_material(root, "Material0") == 1
    assert geometries_by_material(root, "Material2") > 0
    assert geometries_by_material(root, "Material3") == 1


def test_pvc_two_arrays_give_two_frames():
    text = scene.write_pvc(flat(20, 20), None, 20, 20, -30.0, 30.0, -30.0, 30.0,
                           [spec(), spec(key="array_1", centre_x=15.0)])
    assert geometries_by_material(pvc_tree(text), "Material0") == 2


def test_dsm_obstacles_inside_an_array_footprint_are_excluded():
    rows = cols = 40
    dsm = [10.0] * (rows * cols)
    text = scene.write_pvc(flat(rows, cols), dsm, rows, cols, -20.0, 20.0, -20.0, 20.0, [spec()])
    root = pvc_tree(text)
    s = spec()
    for g in root.iter(NS + "geometry"):
        if not g.get("id").startswith("tree_crown_"):
            continue
        vals = [float(v) for v in g.find(f"{NS}mesh/{NS}source/{NS}float_array").text.split()]
        xs, ys = vals[0::3], vals[1::3]
        inside = [(x, y) for x, y in zip(xs, ys) if scene.contains_xy(dict(s, maintenance_margin_m=0.0), x, y)]
        assert not inside, g.get("id")


def test_nan_ground_still_writes_finite_xml():
    rows = cols = 13
    dtm = flat(rows, cols, 1.0)
    dtm[5] = math.nan
    dtm[40] = math.inf
    text = scene.write_pvc(dtm, None, rows, cols, 0.0, 12.0, 0.0, 12.0, [spec(centre_x=6.0, centre_y=6.0)])
    assert "nan" not in text.lower() and "inf" not in text.lower()
    dae = scene.write_collada(dtm, None, rows, cols, 0.0, 12.0, 0.0, 12.0, created=A7_CREATED, modified=A7_MODIFIED)
    assert "nan" not in dae.lower()


def test_frames_in_y_gives_one_frame_per_row():
    text = scene.write_pvc(flat(20, 20), None, 20, 20, -30.0, 30.0, -30.0, 30.0,
                           [spec(half_y=10.0, row_pitch_m=4.0)])
    assert geometries_by_material(pvc_tree(text), "Material0") == 5


def test_dae_adds_the_obstacles_geometry_only_with_a_dsm():
    rows = cols = 16
    plain = scene.write_collada(flat(rows, cols), None, rows, cols, 0.0, 15.0, 0.0, 15.0, ground_stride=4)
    assert 'id="obstacles"' not in plain and 'name="Ground"' in plain
    dsm = [5.0] * (rows * cols)
    with_dsm = scene.write_collada(flat(rows, cols), dsm, rows, cols, 0.0, 15.0, 0.0, 15.0, ground_stride=4)
    root = ET.fromstring(with_dsm.encode("utf-8"))
    assert [n.get("id") for n in root.iter(NS + "node")] == ["ground_node", "obstacles_node"]


def test_writer_text_is_crlf_without_bom_or_trailing_newline():
    text = scene.write_pvc(flat(13, 13), None, 13, 13, 0.0, 12.0, 0.0, 12.0, [spec()])
    assert text.startswith('<?xml version="1.0" encoding="utf-8"?>\r\n<COLLADA version="1.4.1" ')
    assert text.endswith("\r\n</COLLADA>") and "\n" not in text.replace("\r\n", "")
    assert "<module_width>992</module_width>" in text and "<author />" in text


def test_names_are_escaped_as_xml_text_and_empty_names_close_themselves():
    text = scene.write_pvc(flat(13, 13), None, 13, 13, 0.0, 12.0, 0.0, 12.0,
                           [spec(module_manufacturer="A&B <x>", module_name="")])
    assert "<module_manufacturer>A&amp;B &lt;x&gt;</module_manufacturer>" in text
    assert "<module_name />" in text


def test_bad_grids_are_refused():
    with pytest.raises(scene.SceneInputError):
        scene.write_pvc(flat(1, 5), None, 1, 5, 0.0, 1.0, 0.0, 1.0, [])
    with pytest.raises(scene.SceneInputError):
        scene.write_pvc(flat(4, 4)[:-1], None, 4, 4, 0.0, 1.0, 0.0, 1.0, [])
    with pytest.raises(scene.SceneInputError):
        scene.write_pvc(flat(5, 5), None, 5, 5, 0.0, 1.0, 0.0, 1.0, [])       # no triangle at stride 12


def test_export_without_a_terrain_grid_writes_nothing():
    result = scene.export_scene(None, [a5_record()])
    assert not result["succeeded"] and result["dae"] is None and result["pvc"] is None


# ------------------------------------------------------ the licensed a7 scene --

@pytest.fixture(scope="module")
def a7_scene():
    """LEAFEXPORTSCENE after a5, computed: Studio's own t1 grid from the committed intake's
    faces (the a-steps before a7 leave the grid alone) and the a5 array."""
    intake = json.loads(INTAKE.read_text(encoding="utf-8"))
    faces = [{"layer": terrain.PREFERRED_TERRAIN_LAYER, "vertices": corners} for corners in intake["terrain_faces"]]
    mpu = terrain.meters_per_unit_for_keyword(terrain.METERS_KEYWORD)
    result = terrain.topo_from_3d_faces(faces, mpu, terrain.DEFAULT_TARGET_CELLS, [])
    assert result["succeeded"]
    grid = terrain.terrain_interpolator(result["grid"], mpu)
    return scene.export_scene(grid, [a5_record()], created=A7_CREATED, modified=A7_MODIFIED)


def test_a7_scene_shape(a7_scene):
    assert a7_scene["succeeded"] and a7_scene["array_count"] == 1 and not a7_scene["has_dsm"]
    assert (a7_scene["rows"], a7_scene["cols"]) == (90, 150)
    for text in (a7_scene["dae"], a7_scene["pvc"]):
        assert 'count="312"' in text and 'count="104"' in text and 'count="168"' in text
    assert a7_scene["pvc"].count("<frame_parameters>") == 1


def test_a7_dae_reproduces_the_plugin_file(a7_scene):
    text = a7_scene["dae"]
    normalized = g20_normalized(text, dae=True)
    assert normalized.count("\n") + 1 == A7_DAE["lines"]
    assert len(text.encode("utf-8")) == A7_DAE["bytes"]
    assert sha(normalized) == A7_DAE["sha256"]


def test_a7_pvc_reproduces_the_plugin_file(a7_scene):
    text = a7_scene["pvc"]
    normalized = g20_normalized(text, dae=False)
    assert normalized.count("\n") + 1 == A7_PVC["lines"]
    assert len(text.encode("utf-8")) == A7_PVC["bytes"]
    assert sha(normalized) == A7_PVC["sha256"]
