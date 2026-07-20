#!/usr/bin/env python3
"""Offline pre-flight for the leaf-platform-web presenter kit.

Stdlib only. Self-locating: run it from anywhere, it resolves the repo root
from its own path. It answers exactly one question before you walk on stage:

    will the demo run, and does it still produce the numbers in the runbook?

Every number is RECOMPUTED here from the real intake (web/public/sample.intake.json)
by porting the real mock-engine math (shoelace area, centroid, bbox setback) --
nothing is echoed from a pinned file. The pinned files are then checked AGAINST
the recomputation, so drift in either direction fails.

Exit 0  ->  prints "PREFLIGHT: READY"
Exit 1  ->  prints "PREFLIGHT: NOT-READY <reason>"

Usage:
    python scripts/demo-preflight.py --offline            # default, no network
    python scripts/demo-preflight.py --base https://host  # also pings the host
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Golden numbers the runbook cites. These are ASSERTIONS about what the real
# sample rooftop produces, not inputs -- the values below are compared against
# figures recomputed from the intake a few lines further down.
HERO_PANELS = 2345
EDGE_DISTANCE_IN = 60
SQIN_PER_SQFT = 144.0

DEMO_TOOLS = [
    "count-by-layer",
    "measure-panel-area",
    "highlight-panels-near-edge",
    "delete-marked-panel",
]


class NotReady(Exception):
    pass


def ok(msg):
    print("  ok   " + msg)


# --------------------------------------------------------------------------
# Ported mock-engine math (web/src/mock/geometry.js + mockEngine.js)
# --------------------------------------------------------------------------

def poly_area(pts):
    """Shoelace area (absolute), drawing-unit^2 -- geometry.js polyArea."""
    a = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i][0], pts[i][1]
        x2, y2 = pts[(i + 1) % n][0], pts[(i + 1) % n][1]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def centroid(pts):
    sx = sum(p[0] for p in pts)
    sy = sum(p[1] for p in pts)
    return sx / len(pts), sy / len(pts)


def to_sqft(sqin):
    """mockEngine toSqft: +(sqin / 144).toFixed(1)."""
    return float(format(sqin / SQIN_PER_SQFT, ".1f"))


def count_by_layer(polylines):
    counts = {}
    for pl in polylines:
        counts[pl["layer"]] = counts.get(pl["layer"], 0) + 1
    return counts


def measure_area(polylines):
    total = sum(poly_area(pl["pts"]) for pl in polylines)
    return to_sqft(total)


def highlight_near_edge(polylines, dist):
    xs = [p[0] for pl in polylines for p in pl["pts"]]
    ys = [p[1] for pl in polylines for p in pl["pts"]]
    min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
    near = 0
    for pl in polylines:
        cx, cy = centroid(pl["pts"])
        d = min(cx - min_x, max_x - cx, cy - min_y, max_y - cy)
        if d <= dist:
            near += 1
    return near


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

def check_intake():
    """Recompute the hero number from the real intake."""
    path = ROOT / "web" / "public" / "sample.intake.json"
    if not path.is_file():
        raise NotReady("missing %s" % path.relative_to(ROOT).as_posix())
    try:
        intake = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise NotReady("sample.intake.json is not valid JSON (%s)" % exc)

    polylines = intake.get("polylines") or []
    if not polylines:
        raise NotReady("sample.intake.json has no polylines")

    counts = count_by_layer(polylines)
    panels = counts.get("Panels", 0)
    if panels != HERO_PANELS:
        raise NotReady(
            "hero number drift: Panels=%d recomputed from sample.intake.json, runbook says %d"
            % (panels, HERO_PANELS)
        )
    print("Panels=%d" % panels)
    ok("hero number recomputed from sample.intake.json (layers: %s)"
       % ", ".join("%s=%d" % kv for kv in sorted(counts.items())))
    return polylines


def check_registry():
    path = ROOT / "web" / "src" / "mock" / "registry.json"
    if not path.is_file():
        raise NotReady("missing web/src/mock/registry.json")
    try:
        reg = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise NotReady("registry.json is not valid JSON (%s)" % exc)
    names = {t.get("name") for t in reg.get("tools", [])}
    missing = [t for t in DEMO_TOOLS if t not in names]
    if missing:
        raise NotReady("registry.json is missing demo tool(s): %s" % ", ".join(missing))
    ok("registry.json has all %d demo tools (incl. delete-marked-panel)" % len(DEMO_TOOLS))


def check_mock_author():
    path = ROOT / "web" / "src" / "mock" / "mockAuthor.js"
    if not path.is_file():
        raise NotReady("missing web/src/mock/mockAuthor.js")
    src = path.read_text(encoding="utf-8")

    # (a) the distance-parse regex must still be there -- the authored tool has
    #     to respect the number the prospect typed, not the hardcoded 60.
    if not re.search(r"match\(\s*/\(\\d\+", src):
        raise NotReady("mockAuthor.js no longer parses a distance from the description")
    if "parseDistance" not in src:
        raise NotReady("mockAuthor.js lost parseDistance()")
    ok("mockAuthor.js parses the typed distance (parseDistance + numeric regex)")

    # (b) the preview must not hardcode the false read-only claim on writes.
    m = re.search(r"const\s+preview\s*=(.*?)\n\s*return\s*\{", src, re.S)
    if not m:
        raise NotReady("mockAuthor.js has no preview assignment to inspect")
    preview = m.group(1)
    if "read-only" in preview and "isWrite" not in preview:
        raise NotReady("mockAuthor.js still claims read-only unconditionally on write tools")
    if "undoable" not in preview:
        raise NotReady("mockAuthor.js preview never tells the truth about write tools")
    ok("mockAuthor.js preview is write-aware (no false read-only claim)")


def check_pinned_beats(polylines):
    """Assert the pinned beat file against the freshly recomputed numbers."""
    path = ROOT / "web" / "test" / "demoBeats.expected.json"
    if not path.is_file():
        ok("no pinned demoBeats.expected.json (optional) -- skipped")
        return
    try:
        pinned = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise NotReady("demoBeats.expected.json is not valid JSON (%s)" % exc)

    beats = {b.get("id"): b for b in pinned.get("beats", [])}
    total = len(polylines)
    live = {
        "count": {"counts": {"Panels": count_by_layer(polylines).get("Panels", 0)},
                  "total": total},
        "measure": {"panels": total, "total_area_sqft": measure_area(polylines)},
        "edge": {"distance_in": EDGE_DISTANCE_IN,
                 "panels_near_edge": highlight_near_edge(polylines, EDGE_DISTANCE_IN),
                 "panels_total": total},
        "write": {"panels_before": total, "panels_after": total - 1},
    }
    for bid, computed in live.items():
        beat = beats.get(bid)
        if beat is None:
            raise NotReady("demoBeats.expected.json has no '%s' beat" % bid)
        expect = beat.get("expect") or {}
        for key, value in computed.items():
            if key not in expect:
                continue
            if expect[key] != value:
                raise NotReady(
                    "beat '%s' pinned %s=%r but the engine recomputes %r"
                    % (bid, key, expect[key], value)
                )
    if pinned.get("undo", {}).get("panels_at_head") not in (None, total):
        raise NotReady("undo.panels_at_head disagrees with the recomputed %d" % total)
    ok("demoBeats.expected.json agrees with the recomputed engine "
       "(area=%.1f sqft, near-edge@%din=%d)"
       % (live["measure"]["total_area_sqft"], EDGE_DISTANCE_IN,
          live["edge"]["panels_near_edge"]))


def check_buildable():
    dist = ROOT / "web" / "dist" / "index.html"
    if dist.is_file():
        ok("web/dist exists (prebuilt bundle ready to serve)")
        return
    pkg = ROOT / "web" / "package.json"
    if not pkg.is_file():
        raise NotReady("no web/dist and no web/package.json -- nothing to serve or build")
    try:
        scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts") or {}
    except ValueError as exc:
        raise NotReady("web/package.json is not valid JSON (%s)" % exc)
    if "build" not in scripts:
        raise NotReady("no web/dist and web/package.json has no build script")
    if not (ROOT / "web" / "node_modules").is_dir():
        raise NotReady("no web/dist and web/node_modules is missing -- run npm ci first")
    ok("no web/dist, but `npm run build` is runnable (node_modules present)")


def check_flipbook():
    """The stage-fallback flipbook must be fully self-contained: it is the thing
    you open when the NETWORK is the problem, so a single external URL (script,
    stylesheet, image, font) silently breaks it exactly when it is needed. It
    must also still carry the golden panel count it exists to show."""
    flip = ROOT / "deploy" / "presenter-flipbook.html"
    if not flip.is_file():
        raise NotReady("deploy/presenter-flipbook.html missing")
    text = flip.read_text(encoding="utf-8")
    hit = re.search(r"https?://\S+", text)
    if hit:
        raise NotReady("presenter-flipbook.html references an external URL (%s...) -- "
                       "the offline fallback must be fully self-contained"
                       % hit.group(0)[:60])
    if "2345" not in text:
        raise NotReady("presenter-flipbook.html lost the golden panel count (2345)")
    ok("presenter-flipbook.html self-contained (no external URLs; golden 2345 present)")


def check_base(base):
    """Optional: ping a deployed base URL. Never called on an --offline run."""
    import urllib.request
    import urllib.error
    try:
        with urllib.request.urlopen(base, timeout=10) as resp:
            code = resp.getcode()
    except Exception as exc:  # noqa: BLE001 - any failure is NOT-READY
        raise NotReady("base URL %s unreachable (%s)" % (base, exc))
    if code >= 400:
        raise NotReady("base URL %s returned HTTP %d" % (base, code))
    ok("base URL %s reachable (HTTP %d)" % (base, code))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Offline pre-flight for the demo.")
    ap.add_argument("--offline", action="store_true",
                    help="local files only, no network (default)")
    ap.add_argument("--base", default=None,
                    help="also ping this deployed base URL")
    args = ap.parse_args(argv)

    offline = args.offline or not args.base

    print("Leaf demo pre-flight -- repo %s" % ROOT.as_posix())
    print("mode: %s" % ("offline (local files only)" if offline else "online (--base %s)" % args.base))
    try:
        polylines = check_intake()
        check_registry()
        check_mock_author()
        check_pinned_beats(polylines)
        check_buildable()
        check_flipbook()
        if args.base and not args.offline:
            check_base(args.base)
        elif args.base:
            ok("--base given with --offline: ping skipped by design")
    except NotReady as exc:
        print("PREFLIGHT: NOT-READY %s" % exc)
        return 1
    print("PREFLIGHT: READY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
