"""One terrain or CPU native-frame shade CSV bound to the supplied physical head."""
from copy import deepcopy
import math
import re

from leaf_cloud_client import canonical_bytes
import solar_artifacts
import solar_ground_analysis as analysis
import solar_ground_shade as shade
import solar_ground_terrain_adapter as terrain_adapter
import solar_physical_state as ps
from solar_design_graph import GraphValidationError
from solar_physical_analysis import hydrate_physical_document

TOOL = "solar-physical-export"
OUTPUT_SCHEMA = "leaf.solar-physical-export.v1"
INVALID = "INVALID_PHYSICAL_EXPORT_REQUEST"
MATURITY = "preview"
READS_PHYSICAL_HEAD = True
# Kept equal to MAX_HEAD_BYTES in builtins/solar_physical_shade.py.
MAX_HEAD_BYTES = 8_192
FILENAMES = {"terrain-csv": "terrain.csv", "shade-azal-matrix": "shade-azal-matrix.csv",
             "shade-sam": "shade-sam.csv", "shade-per-panel": "shade-per-panel.csv"}
_HEX = re.compile(r"[0-9a-f]{64}")
_CSV_NUMBER = re.compile(r"-?[0-9]+\.[0-9]{3}")
_TERRAIN_CODES = {"TERRAIN_FRAME_UNSUPPORTED": "PHYSICAL_SHADE_FRAME_UNSUPPORTED",
                  "TERRAIN_GRID_MISSING": "PHYSICAL_SHADE_GRID_MISSING",
                  "TERRAIN_GRID_INVALID": "PHYSICAL_SHADE_GRID_INVALID",
                  "TERRAIN_GRID_TOO_LARGE": "PHYSICAL_SHADE_INPUT_LIMIT_EXCEEDED"}


def _check_terrain_csv(content, cells):
    """The serializer's exact shape or nothing (solar_ground_analysis.terrain_csv_text): the UTF-8 BOM,
    the X,Y,Z header, one row per grid node in order, each three finite F3 numbers, every line ending
    CRLF. O(nodes); 90,000 nodes measured at 143 ms."""
    if (type(content) is not bytes or not content.startswith(b"\xef\xbb\xbfX,Y,Z\r\n")
            or not content.endswith(b"\r\n")):
        raise ValueError()
    lines = content[3:].decode("utf-8", errors="strict").split("\r\n")
    if lines[0] != "X,Y,Z" or lines[-1] != "" or len(lines) != cells + 2:
        raise ValueError()
    for line in lines[1:-1]:
        fields = line.split(",")
        if len(fields) != 3 or not all(
                _CSV_NUMBER.fullmatch(field) and math.isfinite(float(field)) for field in fields):
            raise ValueError()


def _terrain(document):
    if (type(document) is not dict or type(document.get("state")) is not dict
            or type(document.get("units")) is not dict):
        raise GraphValidationError("PHYSICAL_SHADE_INPUT_INVALID")
    units = document["units"]
    unit, mpu = units.get("drawing_units"), units.get("meters_per_unit")
    if (type(unit) is not str or unit not in ps.UNITS or type(mpu) is not float
            or mpu != ps.UNITS[unit]):
        raise GraphValidationError("PHYSICAL_STATE_UNITS_UNSUPPORTED")
    try:
        ps._check_envelope(document)
    except ps.PhysicalStateError as exc:
        code = (exc.code if exc.code == "PHYSICAL_STATE_UNITS_UNSUPPORTED"
                else "PHYSICAL_SHADE_INPUT_INVALID")
        raise GraphValidationError(code) from None
    try:
        terrain_adapter.document_frame(document)
    except terrain_adapter.TerrainAdapterError as exc:
        raise GraphValidationError(_TERRAIN_CODES[exc.code]) from None
    except (LookupError, TypeError, AttributeError, ValueError, OverflowError):
        raise GraphValidationError("PHYSICAL_SHADE_INPUT_INVALID") from None
    try:
        return terrain_adapter.document_grid(document)
    except terrain_adapter.TerrainAdapterError as exc:
        raise GraphValidationError(_TERRAIN_CODES[exc.code]) from None
    except (LookupError, TypeError, AttributeError, ValueError, OverflowError):
        raise GraphValidationError("PHYSICAL_SHADE_GRID_INVALID") from None


def _number(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def _shade_summary(sim, frame_count, grid):
    if type(sim) is not dict or sim.get("succeeded") is not True:
        raise ValueError()
    panels, weighted = sim["panels"], sim["result"]["weighted_per_panel"]
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
    profile = sim["profile"]
    expected = shade.select_profile(n)
    if (type(profile) is not dict or profile != expected
            or not _number(profile["ray_step_m"], 1, 10)
            or not _number(profile["max_ray_m"], 400, 400)
            or type(profile["estimated_samples"]) is not int
            or profile["estimated_samples"] > 56_160_000
            or type(sim["surface"]) is not dict or sim["surface"] != grid
            or any(type(v) is not int for v in sim["surface"].values())):
        raise ValueError()
    return {"sample_count": n, "profile": profile["name"],
            "mean_shade": sim["mean_shade"], "datum_shift_m": sim["binding"]["shift_m"]}


def run(graph, params, physical_head=None):
    if (type(params) is not dict or set(params) != {"expected_head", "format"}
            or type(params["expected_head"]) is not str or not _HEX.fullmatch(params["expected_head"])
            or type(params["format"]) is not str or params["format"] not in FILENAMES):
        raise GraphValidationError(INVALID)
    if physical_head is None:
        raise GraphValidationError("PHYSICAL_SHADE_HEAD_REQUIRED")
    if (type(physical_head) is not dict or type(physical_head.get("head")) is not dict
            or type(physical_head.get("document")) is not dict):
        raise GraphValidationError("PHYSICAL_SHADE_INPUT_INVALID")
    head = physical_head["head"]
    if (type(head.get("state")) is not dict
            or type(head["state"].get("artifact_id")) is not str
            or not _HEX.fullmatch(head["state"]["artifact_id"])):
        raise GraphValidationError("PHYSICAL_SHADE_INPUT_INVALID")
    try:
        head_size = len(canonical_bytes(head))
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise GraphValidationError("PHYSICAL_SHADE_INPUT_INVALID") from None
    if head_size > MAX_HEAD_BYTES:
        raise GraphValidationError("PHYSICAL_SHADE_OUTPUT_LIMIT_EXCEEDED")
    if params["expected_head"] != head["state"]["artifact_id"]:
        raise GraphValidationError("PHYSICAL_EXPORT_HEAD_MOVED")
    document = physical_head["document"]
    fmt = params["format"]
    if fmt == "terrain-csv":
        grid = _terrain(document)
        settings = None
        values = dict(sample_count=None, profile=None, mean_shade=None, datum_shift_m=None)
        try:
            content = analysis.terrain_csv_file_bytes(grid, document["units"]["meters_per_unit"])
            _check_terrain_csv(content, grid["rows"] * grid["cols"])
        except Exception:
            raise GraphValidationError("PHYSICAL_EXPORT_KERNEL_FAILED") from None
    else:
        inputs = hydrate_physical_document(document)
        grid = terrain_adapter.document_grid(document)
        settings = {"mode": "defaults", "target_clearance_m": 1.5, "profile_selection": "automatic"}
        try:
            sim = shade.shade_sim(inputs["frames"], inputs["dtm"], inputs["meters_per_unit"],
                                  {}, existing_heatmap=0)
            dimensions = {"rows": grid["rows"], "cols": grid["cols"],
                          "cells": grid["rows"] * grid["cols"]}
            values = _shade_summary(sim, len(inputs["frames"]), dimensions)
            selected = sim["files"][fmt]
            if type(selected) is not str or not selected:
                raise ValueError()
            content = selected.encode("utf-8")
        except Exception:
            raise GraphValidationError("PHYSICAL_SHADE_KERNEL_FAILED") from None
    summary = {"schema": OUTPUT_SCHEMA, "maturity": MATURITY,
               "scope": "terrain-nodes" if fmt == "terrain-csv" else "cpu-terrain-native-frame-centres",
               "head": deepcopy(head), "format": fmt,
               "units": {key: document["units"][key] for key in ("drawing_units", "meters_per_unit")},
               "grid": {"rows": grid["rows"], "cols": grid["cols"], "cells": grid["rows"] * grid["cols"]},
               "settings": settings, **values}
    return solar_artifacts.ArtifactOutput(summary=summary, media_type="text/csv",
                                         filename=FILENAMES[fmt], content=content)
