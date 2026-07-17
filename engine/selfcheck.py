"""Pure-python mirror of the Lane B engine tools + Result-envelope oracle.

    python engine/selfcheck.py [path-to.intake.json]

Each mock_* function computes the SAME Result envelope (CONTRACT.md section 3)
that the matching DA/LISP tool in engine/tools/ produces on the real engine -
without needing APS. This module is therefore two things at once:

  1. the MOCK BACKEND Lane D / root use before APS Design Automation is wired
     (import mock_count_by_layer / mock_measure_panel_area /
     mock_highlight_panels_near_edge, feed the golden Intake JSON, get an
     envelope); and
  2. the ORACLE the real engine output is checked against (run the same input
     through both and diff).

Binary acceptance: this script prints a valid envelope for all three tools
computed from the real sample intake JSON, and validates each against
engine/envelope_schema.json, exiting 0 only if every check passes.

UNITS ASSUMPTION (measure-panel-area): 1 drawing unit = 1 inch, so
square feet = area_in2 / 144. On the golden sample a panel is 77 x 38.5 units
= 20.57 sqft (a real module footprint), confirming inches.
"""
from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_INTAKE = os.path.normpath(os.path.join(HERE, "..", "data", "rooftop_demo.intake.json"))
SCHEMA_PATH = os.path.join(HERE, "envelope_schema.json")
REGISTRY_PATH = os.path.join(HERE, "registry.json")


# --------------------------------------------------------------------------- #
# geometry helpers (mirror the LISP math exactly)
# --------------------------------------------------------------------------- #
def _shoelace(pts) -> float:
    """Absolute polygon area over [x, y, (z)] vertices."""
    n = len(pts)
    if n < 3:
        return 0.0
    a = 0.0
    for i in range(n):
        x1, y1 = pts[i][0], pts[i][1]
        x2, y2 = pts[(i + 1) % n][0], pts[(i + 1) % n][1]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def _centroid(pts):
    n = len(pts)
    cx = sum(p[0] for p in pts) / n
    cy = sum(p[1] for p in pts) / n
    return cx, cy


def _envelope(tool, result, overlay, t0):
    """Assemble a section-3 envelope. cost stays null (mock; no real APS run)."""
    return {
        "ok": True,
        "tool": tool,
        "version": "1.0.0",
        "result": result,
        "overlay": overlay,
        "timing_ms": round((time.perf_counter() - t0) * 1000, 3),
        "cost": None,
        "error": None,
    }


# --------------------------------------------------------------------------- #
# tool mirrors  (params match registry.json §2)
# --------------------------------------------------------------------------- #
def mock_count_by_layer(intake: dict, params: dict | None = None) -> dict:
    """Mirror of tools/count_by_layer.lsp -> result.counts."""
    t0 = time.perf_counter()
    counts: dict[str, int] = {}
    for coll in ("polylines", "inserts", "faces3d"):
        for e in intake.get(coll, []):
            lay = e.get("layer", "0")
            counts[lay] = counts.get(lay, 0) + 1
    return _envelope("count-by-layer", {"counts": counts}, None, t0)


def mock_measure_panel_area(intake: dict, params: dict | None = None) -> dict:
    """Mirror of tools/measure_panel_area.lsp -> result.area_sqft (+ per layer)."""
    t0 = time.perf_counter()
    total_in2 = 0.0
    by_layer_in2: dict[str, float] = {}
    for pl in intake.get("polylines", []):
        if not pl.get("closed"):
            continue
        a = _shoelace(pl["pts"])
        total_in2 += a
        lay = pl.get("layer", "0")
        by_layer_in2[lay] = by_layer_in2.get(lay, 0.0) + a
    result = {
        "area_sqft": round(total_in2 / 144.0, 3),
        "by_layer_sqft": {k: round(v / 144.0, 3) for k, v in by_layer_in2.items()},
        "units_assumption": "1 drawing unit = 1 inch; sqft = in2 / 144",
    }
    return _envelope("measure-panel-area", result, None, t0)


def mock_highlight_panels_near_edge(intake: dict, params: dict | None = None) -> dict:
    """Mirror of tools/highlight_panels_near_edge.lsp -> overlay.highlight_handles.

    params: {"layer": "Panels", "distance": 200}  (distance in drawing units).
    """
    t0 = time.perf_counter()
    params = params or {}
    layer = params.get("layer", "Panels")
    distance = float(params.get("distance", 200))

    polys = intake.get("polylines", [])
    # drawing extents over ALL polyline vertices
    xs = [p[0] for pl in polys for p in pl["pts"]]
    ys = [p[1] for pl in polys for p in pl["pts"]]
    if not xs:
        result = {"layer": layer, "distance": distance, "matched": 0, "extents": None}
        return _envelope("highlight-panels-near-edge", result,
                         {"highlight_handles": []}, t0)
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)

    handles = []
    for pl in polys:
        if pl.get("layer") != layer or not pl.get("closed"):
            continue
        cx, cy = _centroid(pl["pts"])
        d = min(cx - minx, maxx - cx, cy - miny, maxy - cy)
        if d <= distance:
            handles.append(pl["handle"])

    result = {
        "layer": layer,
        "distance": distance,
        "matched": len(handles),
        "extents": {"min": [round(minx, 3), round(miny, 3)],
                    "max": [round(maxx, 3), round(maxy, 3)]},
    }
    return _envelope("highlight-panels-near-edge", result,
                     {"highlight_handles": handles}, t0)


# registry engine_op -> mirror fn (the mock dispatch table Lane D imports)
MIRRORS = {
    "count_by_layer": mock_count_by_layer,
    "measure_panel_area": mock_measure_panel_area,
    "highlight_panels_near_edge": mock_highlight_panels_near_edge,
}


def run_mock(engine_op: str, intake: dict, params: dict | None = None) -> dict:
    """Single entry point for the mock backend: engine_op -> Result envelope."""
    if engine_op not in MIRRORS:
        return {"ok": False, "tool": engine_op, "version": "1.0.0", "result": {},
                "overlay": None, "timing_ms": 0, "cost": None,
                "error": f"unknown engine_op: {engine_op}"}
    return MIRRORS[engine_op](intake, params)


# --------------------------------------------------------------------------- #
# envelope validation (jsonschema if available, else a structural fallback)
# --------------------------------------------------------------------------- #
def validate_envelope(env: dict) -> list[str]:
    errs: list[str] = []
    try:
        import jsonschema  # type: ignore
        schema = json.load(open(SCHEMA_PATH))
        v = jsonschema.Draft7Validator(schema)
        return [f"{list(e.path)}: {e.message}" for e in v.iter_errors(env)]
    except ImportError:
        pass  # fall through to structural checks

    required = ["ok", "tool", "version", "result", "timing_ms", "error"]
    for k in required:
        if k not in env:
            errs.append(f"missing required key '{k}'")
    if not isinstance(env.get("ok"), bool):
        errs.append("ok must be bool")
    if not isinstance(env.get("tool"), str):
        errs.append("tool must be str")
    if not isinstance(env.get("version"), str):
        errs.append("version must be str")
    if not isinstance(env.get("result"), dict):
        errs.append("result must be object")
    if not isinstance(env.get("timing_ms"), (int, float)):
        errs.append("timing_ms must be number")
    if env.get("overlay") is not None and not isinstance(env.get("overlay"), dict):
        errs.append("overlay must be object or null")
    if env.get("cost") is not None and not isinstance(env.get("cost"), dict):
        errs.append("cost must be object or null")
    if env.get("error") is not None and not isinstance(env.get("error"), str):
        errs.append("error must be str or null")
    extra = set(env) - {"ok", "tool", "version", "result", "overlay",
                        "timing_ms", "cost", "error"}
    if extra:
        errs.append(f"unexpected keys: {sorted(extra)}")
    return errs


# --------------------------------------------------------------------------- #
# main: run all three against the real sample, print + validate
# --------------------------------------------------------------------------- #
def main() -> int:
    intake_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_INTAKE
    intake = json.load(open(intake_path))

    runs = [
        ("count_by_layer", {}),
        ("measure_panel_area", {}),
        ("highlight_panels_near_edge", {"layer": "Panels", "distance": 200}),
    ]

    print(f"# selfcheck against {intake_path}")
    print(f"#   layers={intake.get('layers')} polylines={len(intake.get('polylines', []))} "
          f"inserts={len(intake.get('inserts', []))} faces3d={len(intake.get('faces3d', []))}\n")

    all_ok = True
    for engine_op, params in runs:
        env = run_mock(engine_op, intake, params)
        errs = validate_envelope(env)
        status = "VALID  " if not errs else "INVALID"
        print(f"===== {engine_op}  [{status}] =====")
        print(json.dumps(env, indent=2))
        if errs:
            all_ok = False
            for e in errs:
                print(f"  SCHEMA ERROR: {e}", file=sys.stderr)
        print()

    # cross-check: registry engine_ops all have a mirror, and mirrors are registered
    try:
        reg = json.load(open(REGISTRY_PATH))
        reg_ops = {t["engine_op"] for t in reg["tools"]}
        missing = reg_ops - set(MIRRORS)
        orphan = set(MIRRORS) - reg_ops
        if missing:
            print(f"  REGISTRY ERROR: engine_ops with no mirror: {sorted(missing)}", file=sys.stderr)
            all_ok = False
        if orphan:
            print(f"  REGISTRY ERROR: mirrors not in registry: {sorted(orphan)}", file=sys.stderr)
            all_ok = False
        print(f"# registry: {len(reg['tools'])} tools, engine_ops {sorted(reg_ops)} all mirrored={not missing}")
    except FileNotFoundError:
        print("  WARN: registry.json not found; skipped registry cross-check", file=sys.stderr)

    print(f"\n# RESULT: {'ALL ENVELOPES VALID' if all_ok else 'FAILURES ABOVE'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
