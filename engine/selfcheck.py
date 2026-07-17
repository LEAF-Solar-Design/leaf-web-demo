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
PROJECT_ROOT = os.path.dirname(HERE)
SERVER_DIR = os.path.join(PROJECT_ROOT, "server")
DEFAULT_INTAKE = os.path.normpath(os.path.join(HERE, "..", "data", "rooftop_demo.intake.json"))
SCHEMA_PATH = os.path.join(HERE, "envelope_schema.json")
REGISTRY_PATH = os.path.join(HERE, "registry.json")
AUTHORED_STORE = os.path.join(SERVER_DIR, "authored_tools.json")

# Make server-side authored tool files importable if they import shared helpers
# (server/ has no `platform/` shadow, so this insert is safe; PROJECT_ROOT is
# intentionally NOT inserted here to avoid shadowing the stdlib `platform`).
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)


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
    except Exception:  # noqa: BLE001  (ImportError, or the repo-root platform-shadow)
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
# --------------------------------------------------------------------------- #
# effective-registry cross-check (dynamic-loader era): each tool's referenced
# FILE must exist AND its envelope must schema-validate. A hand-written MIRROR is
# NO LONGER required — the tool FILE is the source of truth, so a newly authored
# tool no longer reddens this gate.
# --------------------------------------------------------------------------- #
def _load_effective_registry() -> list:
    tools: list = []
    try:
        tools += json.load(open(REGISTRY_PATH)).get("tools", [])
    except FileNotFoundError:
        pass
    try:
        with open(AUTHORED_STORE, encoding="utf-8") as fh:
            tools += json.load(fh).get("tools", [])
    except FileNotFoundError:
        pass
    return tools


def _referenced_file(tool: dict):
    """Resolve a tool's referenced source file to an existing path, or None."""
    entry = tool.get("entry")
    if entry:
        for cand in (entry, os.path.join(SERVER_DIR, entry)):
            if os.path.isfile(cand):
                return cand
    script = tool.get("script")
    if script:
        for cand in (script, os.path.join(HERE, script), os.path.join(PROJECT_ROOT, script)):
            if os.path.isfile(cand):
                return cand
    return None


def _load_py_run(path: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"leaf_selfcheck_{abs(hash(path)) & 0xFFFFFFFF}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return getattr(mod, "run", None)


def _envelope_for(tool: dict, intake: dict):
    """Produce a §3 envelope to validate: run_mock for a built-in that HAS a
    mirror, else load and run the tool's own .py FILE (the FILE is the tool)."""
    engine_op = tool.get("engine_op", "")
    params = dict(tool.get("default_params", {}) or {})
    if engine_op in MIRRORS:
        return run_mock(engine_op, intake, params)
    ref = _referenced_file(tool)
    if ref and ref.endswith(".py"):
        run = _load_py_run(ref)
        if run is None:
            return None
        t0 = time.perf_counter()
        ret = run(intake, params)
        if isinstance(ret, tuple) and len(ret) == 2:
            result, overlay = ret
        elif isinstance(ret, dict):
            result, overlay = ret, None
        else:
            result, overlay = {"value": ret}, None
        return _envelope(tool.get("name", engine_op), result, overlay, t0)
    return None


def main() -> int:
    intake_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_INTAKE
    intake = json.load(open(intake_path))

    print(f"# selfcheck against {intake_path}")
    print(f"#   layers={intake.get('layers')} polylines={len(intake.get('polylines', []))} "
          f"inserts={len(intake.get('inserts', []))} faces3d={len(intake.get('faces3d', []))}\n")

    all_ok = True

    # (A) OPTIONAL golden-value regression for the 3 built-in mirrors — never a
    #     gate that reddens on a new tool; kept as a built-ins-only sanity print.
    golden = [
        ("count_by_layer", {}),
        ("measure_panel_area", {}),
        ("highlight_panels_near_edge", {"layer": "Panels", "distance": 200}),
    ]
    for engine_op, params in golden:
        env = run_mock(engine_op, intake, params)
        errs = validate_envelope(env)
        print(f"===== [mirror] {engine_op}  [{'VALID  ' if not errs else 'INVALID'}] =====")
        print(json.dumps(env, indent=2))
        if errs:
            all_ok = False
            for e in errs:
                print(f"  SCHEMA ERROR: {e}", file=sys.stderr)
        print()

    # (B) EFFECTIVE-REGISTRY cross-check (THE gate): every registry + authored
    #     tool's referenced FILE exists AND its envelope schema-validates. No
    #     MIRRORS membership required.
    effective = _load_effective_registry()
    print(f"# effective registry: {len(effective)} tools "
          f"({sorted(str(t.get('name')) for t in effective)})")
    for tool in effective:
        name = tool.get("name")
        ref = _referenced_file(tool)
        if ref is None:
            print(f"  REGISTRY ERROR: tool {name!r} references no existing file "
                  f"(entry={tool.get('entry')!r} script={tool.get('script')!r})", file=sys.stderr)
            all_ok = False
            continue
        env = _envelope_for(tool, intake)
        if env is None:
            print(f"  REGISTRY ERROR: tool {name!r} produced no envelope to validate "
                  f"(ref={ref})", file=sys.stderr)
            all_ok = False
            continue
        errs = validate_envelope(env)
        via = "mirror" if tool.get("engine_op") in MIRRORS else "file"
        try:
            rel = os.path.relpath(ref, PROJECT_ROOT)
        except ValueError:
            rel = ref
        print(f"  [{'OK ' if not errs else 'BAD'}] {name}  (via {via}: {rel})")
        if errs:
            all_ok = False
            for e in errs:
                print(f"    SCHEMA ERROR: {e}", file=sys.stderr)

    print(f"\n# RESULT: {'ALL CHECKS PASS' if all_ok else 'FAILURES ABOVE'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
