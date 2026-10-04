"""Read-only CPU terrain shade at recorded native frame centres."""
from copy import deepcopy
import math

from leaf_cloud_client import canonical_bytes
import solar_ground_shade as shade
from solar_design_graph import GraphValidationError
from solar_physical_analysis import hydrate_physical_document

TOOL = "solar-physical-shade"
OUTPUT_SCHEMA = "leaf.solar-physical-shade.v1"
INVALID = "INVALID_PHYSICAL_SHADE_REQUEST"
MATURITY = "preview"
READS_PHYSICAL_HEAD = True
MAX_HEAD_BYTES = 8_192
MAX_OUTPUT_BYTES = 65_536


def _number(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def _project(sim, frame_count):
    """Validate the kernel's result before projecting any public fields."""
    if type(sim) is not dict or sim.get("succeeded") is not True:
        raise ValueError()
    panels, weighted = sim["panels"], sim["result"]["weighted_per_panel"]
    # The projection indexes both lists, so only exact lists are admitted (a dict would pass its keys).
    if type(panels) is not list or type(weighted) is not list:
        raise ValueError()
    n = len(panels)
    if not 1 <= n <= 20_000 or len(weighted) != n:
        raise ValueError()
    indices = [panel["entity"] for panel in panels]
    if (any(type(i) is not int or not 0 <= i < frame_count for i in indices)
            or indices != sorted(set(indices))
            or any(not _number(value, 0, 1) for value in weighted)
            or not _number(sim["mean_shade"], 0, 1)
            or not _number(sim["binding"]["shift_m"], -2e12, 2e12)):
        raise ValueError()
    p = sim["profile"]
    expected = shade.select_profile(n)
    if (p["name"] != expected["name"] or len(p["angles"]) != len(expected["angles"])
            or not _number(p["ray_step_m"], 1, 10)
            or not _number(p["max_ray_m"], 400, 400)
            or p["ray_step_m"] != expected["ray_step_m"]):
        raise ValueError()
    estimated = n * len(p["angles"]) * math.ceil(p["max_ray_m"] / p["ray_step_m"])
    if (estimated > 56_160_000 or type(p["estimated_samples"]) is not int
            or p["estimated_samples"] != estimated):
        raise ValueError()
    surface = sim["surface"]
    if (type(surface) is not dict or set(surface) != {"rows", "cols", "cells"}
            or any(type(surface[k]) is not int for k in surface)
            or not 2 <= surface["rows"] <= 300 or not 2 <= surface["cols"] <= 300
            or surface["cells"] != surface["rows"] * surface["cols"]):
        raise ValueError()
    return {"sample_count": n, "mean_shade": sim["mean_shade"],
            "datum_shift_m": sim["binding"]["shift_m"],
            "profile": {"name": p["name"], "angle_count": len(p["angles"]),
                        "ray_step_m": p["ray_step_m"], "max_ray_m": p["max_ray_m"],
                        "estimated_samples": estimated}, "surface": deepcopy(surface),
            "frames": [{"sample_index": i, "frame_index": indices[i], "shade": weighted[i]}
                       for i in range(min(n, 200))], "frames_omitted": max(0, n - 200)}


def run(graph, params, physical_head=None):
    if type(params) is not dict or params:
        raise GraphValidationError(INVALID)
    if physical_head is None:
        raise GraphValidationError("PHYSICAL_SHADE_HEAD_REQUIRED")
    if (type(physical_head) is not dict or type(physical_head.get("head")) is not dict
            or type(physical_head.get("document")) is not dict):
        raise GraphValidationError("PHYSICAL_SHADE_INPUT_INVALID")
    inputs = hydrate_physical_document(physical_head["document"])
    head = deepcopy(physical_head["head"])
    try:
        head_size = len(canonical_bytes(head))
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise GraphValidationError("PHYSICAL_SHADE_INPUT_INVALID") from None
    if head_size > MAX_HEAD_BYTES:
        raise GraphValidationError("PHYSICAL_SHADE_OUTPUT_LIMIT_EXCEEDED")
    try:
        sim = shade.shade_sim(inputs["frames"], inputs["dtm"], inputs["meters_per_unit"],
                              settings={}, existing_heatmap=0)
        summary = _project(sim, len(inputs["frames"]))
    except Exception:
        raise GraphValidationError("PHYSICAL_SHADE_KERNEL_FAILED") from None
    report = {"schema": OUTPUT_SCHEMA, "maturity": MATURITY,
              "scope": "cpu-terrain-native-frame-centres", "head": head,
              "units": {"drawing_units": physical_head["document"]["units"]["drawing_units"],
                        "meters_per_unit": inputs["meters_per_unit"]},
              "settings": {"mode": "defaults", "target_clearance_m": 1.5,
                           "profile_selection": "automatic"}, **summary}
    while len(canonical_bytes(report)) > MAX_OUTPUT_BYTES:
        if not report["frames"]:
            raise GraphValidationError("PHYSICAL_SHADE_OUTPUT_LIMIT_EXCEEDED")
        report["frames"].pop()
        report["frames_omitted"] += 1
    return report
