from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from cat_oracle import FIXTURE_DIR, _read_pbm, evaluate_cat


def entity(handle: str, x: float, y: float, *, size: float = 1.0) -> dict:
    return {
        "handle": handle,
        "layer": "PANELS",
        "closed": True,
        "pts": [[x, y, 7.0], [x + size, y, 7.0], [x + size, y + size, 7.0], [x, y + size, 7.0]],
        "xdata": {"panel": handle},
        "metadata": {"kind": "roof-panel"},
    }


def case_for(template: str) -> tuple[dict, dict, list[str]]:
    pixels = sorted(_read_pbm(FIXTURE_DIR / f"{template}.pbm"), key=lambda p: (p[1], p[0]))
    handles = [f"P{i:04d}" for i in range(len(pixels))]
    before = {"dwg": "cat.dwg", "layers": ["PANELS"], "polylines": []}
    after = copy.deepcopy(before)
    width = 64
    before["polylines"] = [entity(handle, i % width, i // width) for i, handle in enumerate(handles)]
    after["polylines"] = [entity(handle, x, y) for handle, (x, y) in zip(handles, pixels)]
    return before, after, handles


@pytest.mark.parametrize("template", ["sitting-v1", "standing-v1", "curled-v1"])
def test_frozen_cat_templates_pass(template):
    before, after, handles = case_for(template)
    report = evaluate_cat(before, after, handles)
    assert report["verdict"] == "pass", report
    assert report["template"] == template
    assert report["metrics"]["iou"] == 1.0
    assert report["metrics"]["outline_chamfer_px"] == 0.0
    assert report["metrics"]["components_4"] == 1
    assert report["metrics"]["overlap_pixels"] == 0
    assert set(report["metrics"]["region_recall"].values()) == {1.0}
    assert report["schema"] == "leaf.cat-oracle.v1"
    assert len(report["thresholds_hash"]) == 64
    assert report["selected_handles"] == handles
    assert report["selected_handle_count_before"] == len(handles)
    assert report["selected_handle_count_after"] == len(handles)
    assert report["identity_ok"] is True
    assert report["geometry_ok"] is True
    assert report["unselected_digest_equal"] is True
    assert report["metrics"]["overlap_pixel_fraction"] == 0.0
    assert report["input_sha256"] == report["input_hashes"]["before"]
    assert report["output_sha256"] == report["input_hashes"]["after"]
    assert len(report["template_sha256"]) == 64
    assert set(report["alignment"]) == {"scale_x", "scale_y", "dx", "dy", "reflected"}


def test_same_count_rectangle_fails_and_calibration_intervals_separate():
    positive_scores = []
    negative_scores = []
    for template in ("sitting-v1", "standing-v1", "curled-v1"):
        before, after, handles = case_for(template)
        positive_scores.append(evaluate_cat(before, after, handles)["metrics"]["iou"])
        rectangle = copy.deepcopy(before)
        negative = evaluate_cat(before, rectangle, handles)
        assert negative["verdict"] == "fail"
        negative_scores.append(negative["metrics"]["iou"])
    thresholds = json.loads((FIXTURE_DIR / "thresholds.json").read_text(encoding="utf-8"))
    assert max(negative_scores) < thresholds["min_iou"] <= min(positive_scores), (
        "calibration intervals do not separate; fail closed",
        negative_scores,
        positive_scores,
    )


def test_disconnected_tail_or_shape_fails():
    before, after, handles = case_for("sitting-v1")
    moved = copy.deepcopy(after)
    tail_handle = handles[-1]
    panel = next(item for item in moved["polylines"] if item["handle"] == tail_handle)
    for point in panel["pts"]:
        point[0] += 30
        point[1] += 20
    report = evaluate_cat(before, moved, handles)
    assert report["verdict"] == "fail"
    assert "not_one_4_connected_component" in report["reasons"]


def test_overlap_or_duplicate_geometry_fails():
    before, after, handles = case_for("standing-v1")
    changed = copy.deepcopy(after)
    changed["polylines"][1]["pts"] = copy.deepcopy(changed["polylines"][0]["pts"])
    report = evaluate_cat(before, changed, handles)
    assert report["verdict"] == "fail"
    assert "selected_polygons_overlap" in report["reasons"]


def test_resized_panel_fails_identity_invariant():
    before, after, handles = case_for("curled-v1")
    changed = copy.deepcopy(after)
    x, y, z = changed["polylines"][0]["pts"][2]
    changed["polylines"][0]["pts"][2] = [x + 0.25, y, z]
    report = evaluate_cat(before, changed, handles)
    assert report["verdict"] == "fail"
    assert report["reasons"][0].startswith("invalid_evidence:")


def test_missing_selected_handle_fails_closed():
    before, after, handles = case_for("sitting-v1")
    after["polylines"] = after["polylines"][1:]
    report = evaluate_cat(before, after, handles)
    assert report["verdict"] == "fail"
    assert "missing selected handles" in report["reasons"][0]


def test_duplicate_selected_handle_fails_closed():
    before, after, handles = case_for("sitting-v1")
    report = evaluate_cat(before, after, handles + [handles[0]])
    assert report["verdict"] == "fail"
    assert "selected_handles contains duplicates" in report["reasons"][0]


def test_boolean_coordinate_fails_closed():
    malformed = entity("A", 0, 0)
    malformed["pts"][0][0] = True
    report = evaluate_cat({"polylines": [malformed]}, {"polylines": [copy.deepcopy(malformed)]}, ["A"])
    assert report["verdict"] == "fail"
    assert "boolean point coordinate" in report["reasons"][0]


def test_collinear_self_contact_fails_closed():
    malformed = entity("A", 0, 0)
    malformed["pts"] = [[0, 0, 7], [2, 0, 7], [1, 0, 7], [1, 1, 7]]
    report = evaluate_cat({"polylines": [malformed]}, {"polylines": [copy.deepcopy(malformed)]}, ["A"])
    assert report["verdict"] == "fail"
    assert "polygon is not simple" in report["reasons"][0]


def test_corner_touch_is_not_four_connected():
    handles = ["A", "B"]
    before = {"polylines": [entity("A", 0, 0), entity("B", 1, 0)]}
    after = {"polylines": [entity("A", 20, 20), entity("B", 21, 21)]}
    report = evaluate_cat(before, after, handles)
    assert report["verdict"] == "fail"
    assert report["metrics"]["components_4"] == 2
    assert "not_one_4_connected_component" in report["reasons"]


def test_unselected_canonical_json_change_fails():
    before, after, handles = case_for("standing-v1")
    before["polylines"].append(entity("KEEP", -10, -10))
    after["polylines"].append(entity("KEEP", -10, -10))
    after["polylines"][-1]["metadata"]["kind"] = "changed"
    report = evaluate_cat(before, after, handles)
    assert report["verdict"] == "fail"
    assert "unselected intake changed" in report["reasons"][0]


def test_top_level_intake_change_fails():
    before, after, handles = case_for("standing-v1")
    after["dwg"] = "different.dwg"
    after["layers"].append("UNAUTHORIZED")
    report = evaluate_cat(before, after, handles)
    assert report["verdict"] == "fail"
    assert report["unselected_digest_equal"] is False
    assert report["identity_ok"] is False
    assert "unselected intake changed" in report["reasons"][0]


def test_reflected_alignment_reports_actual_affine_transform():
    before, after, handles = case_for("standing-v1")
    mirrored = copy.deepcopy(after)
    for panel in mirrored["polylines"]:
        for point in panel["pts"]:
            point[0] = 96.0 - point[0]
        panel["pts"].reverse()  # preserve signed area after reflection
    report = evaluate_cat(before, mirrored, handles)
    assert report["verdict"] == "pass", report
    alignment = report["alignment"]
    assert alignment["reflected"] is True
    assert alignment["scale_x"] < 0 < alignment["scale_y"]
    assert alignment["scale_x"] * 96.0 + alignment["dx"] == pytest.approx(0.0)


def test_fixture_hash_tamper_fails_closed(tmp_path: Path):
    for source in FIXTURE_DIR.iterdir():
        (tmp_path / source.name).write_bytes(source.read_bytes())
    with (tmp_path / "sitting-v1.pbm").open("ab") as stream:
        stream.write(b"\n")
    before, after, handles = case_for("sitting-v1")
    report = evaluate_cat(before, after, handles, fixture_dir=tmp_path)
    assert report["verdict"] == "fail"
    assert "fixture hash mismatch" in report["reasons"][0]


def test_nonseparating_calibration_fails_closed(tmp_path: Path):
    for source in FIXTURE_DIR.iterdir():
        (tmp_path / source.name).write_bytes(source.read_bytes())
    calibration_path = tmp_path / "calibration.json"
    calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
    calibration["separating_intervals"]["iou"]["negative_max"] = 0.99
    calibration_path.write_text(json.dumps(calibration, indent=2) + "\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sha256"]["calibration.json"] = hashlib.sha256(calibration_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    before, after, handles = case_for("sitting-v1")
    report = evaluate_cat(before, after, handles, fixture_dir=tmp_path)
    assert report["verdict"] == "fail"
    assert "strictly inside frozen calibration intervals" in report["reasons"][0]
