"""Deterministic Level 0 oracle for the panel-to-cat litmus test.

The oracle is deliberately independent of rendering libraries. It validates
drawing invariants, normalizes the selected geometry into frozen 96 by 96
silhouette masks, and reports reproducible geometry metrics.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

SCHEMA = "leaf.cat-oracle.v1"
REGIONS = ("head", "ear_left", "ear_right", "body", "leg_left", "leg_right", "tail")
FIXTURE_DIR = Path(__file__).resolve().parent / "tests" / "fixtures" / "cat_oracle"
EPSILON = 1e-6


class OracleInputError(ValueError):
    """The evidence is malformed or violates a drawing invariant."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _entities(intake: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    if not isinstance(intake, dict) or not isinstance(intake.get("polylines"), list):
        raise OracleInputError("intake.polylines must be a list")
    entities = intake["polylines"]
    by_handle: dict[str, dict[str, Any]] = {}
    for entity in entities:
        if not isinstance(entity, dict) or not isinstance(entity.get("handle"), str) or not entity["handle"]:
            raise OracleInputError("each polyline needs a nonempty string handle")
        handle = entity["handle"]
        if handle in by_handle:
            raise OracleInputError(f"duplicate handle: {handle}")
        by_handle[handle] = entity
    return entities, by_handle


def _points(entity: dict[str, Any]) -> list[tuple[float, float, float]]:
    raw = entity.get("pts")
    if entity.get("closed") is not True or not isinstance(raw, list) or len(raw) < 3:
        raise OracleInputError(f"{entity.get('handle')}: polygon must be closed with at least three points")
    points: list[tuple[float, float, float]] = []
    for point in raw:
        if not isinstance(point, list) or len(point) != 3:
            raise OracleInputError(f"{entity.get('handle')}: each point must be [x,y,z]")
        if any(isinstance(value, bool) for value in point):
            raise OracleInputError(f"{entity.get('handle')}: boolean point coordinate")
        try:
            xyz = tuple(float(v) for v in point)
        except (TypeError, ValueError) as exc:
            raise OracleInputError(f"{entity.get('handle')}: nonnumeric point") from exc
        if not all(math.isfinite(v) for v in xyz):
            raise OracleInputError(f"{entity.get('handle')}: nonfinite point")
        points.append(xyz)
    if abs(_signed_area(points)) <= EPSILON:
        raise OracleInputError(f"{entity.get('handle')}: degenerate polygon")
    if not _is_simple(points):
        raise OracleInputError(f"{entity.get('handle')}: polygon is not simple")
    return points


def _signed_area(points: list[tuple[float, float, float]]) -> float:
    return 0.5 * sum(
        points[i][0] * points[(i + 1) % len(points)][1]
        - points[(i + 1) % len(points)][0] * points[i][1]
        for i in range(len(points))
    )


def _orient(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _proper_intersection(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float], d: tuple[float, float]) -> bool:
    ab_c, ab_d = _orient(a, b, c), _orient(a, b, d)
    cd_a, cd_b = _orient(c, d, a), _orient(c, d, b)
    return ((ab_c > EPSILON and ab_d < -EPSILON) or (ab_c < -EPSILON and ab_d > EPSILON)) and (
        (cd_a > EPSILON and cd_b < -EPSILON) or (cd_a < -EPSILON and cd_b > EPSILON)
    )


def _segments_intersect(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float], d: tuple[float, float]) -> bool:
    """Include endpoint and collinear contact for self-intersection checks."""
    if _proper_intersection(a, b, c, d):
        return True

    def on_segment(p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]) -> bool:
        return (
            abs(_orient(p, q, r)) <= EPSILON
            and min(p[0], q[0]) - EPSILON <= r[0] <= max(p[0], q[0]) + EPSILON
            and min(p[1], q[1]) - EPSILON <= r[1] <= max(p[1], q[1]) + EPSILON
        )

    return any((
        on_segment(a, b, c),
        on_segment(a, b, d),
        on_segment(c, d, a),
        on_segment(c, d, b),
    ))


def _is_simple(points: list[tuple[float, float, float]]) -> bool:
    n = len(points)
    xy = [(p[0], p[1]) for p in points]
    for i in range(n):
        for j in range(i + 1, n):
            if j in (i, (i + 1) % n) or i == (j + 1) % n:
                continue
            if _segments_intersect(xy[i], xy[(i + 1) % n], xy[j], xy[(j + 1) % n]):
                return False
    return True


def _pairwise(points: list[tuple[float, float, float]]) -> list[float]:
    return [math.dist(points[i], points[j]) for i in range(len(points)) for j in range(i + 1, len(points))]


def _point_inside(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    x, y = point
    inside = False
    j = len(polygon) - 1
    for i, (xi, yi) in enumerate(polygon):
        xj, yj = polygon[j]
        if (yi > y) != (yj > y):
            cross_x = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < cross_x:
                inside = not inside
        j = i
    return inside


def _positive_overlap(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> bool:
    for i in range(len(a)):
        for j in range(len(b)):
            if _proper_intersection(a[i], a[(i + 1) % len(a)], b[j], b[(j + 1) % len(b)]):
                return True
    # Boundary contact is legal. A tiny move toward each polygon centroid tests
    # whether a vertex lies in the other's interior.
    for source, target in ((a, b), (b, a)):
        cx = sum(p[0] for p in source) / len(source)
        cy = sum(p[1] for p in source) / len(source)
        for x, y in source:
            probe = (x + (cx - x) * 1e-8, y + (cy - y) * 1e-8)
            if _point_inside(probe, target):
                return True
    return False


def _any_overlap(polygons: list[list[tuple[float, float]]]) -> bool:
    indexed = sorted(
        ((min(x for x, _ in polygon), max(x for x, _ in polygon), min(y for _, y in polygon), max(y for _, y in polygon), polygon)
         for polygon in polygons),
        key=lambda item: item[0],
    )
    active: list[tuple[float, float, float, float, list[tuple[float, float]]]] = []
    for current in indexed:
        xmin, _, ymin, ymax, polygon = current
        active = [item for item in active if item[1] >= xmin - EPSILON]
        for other in active:
            if other[3] < ymin - EPSILON or ymax < other[2] - EPSILON:
                continue
            if _positive_overlap(polygon, other[4]):
                return True
        active.append(current)
    return False


def _read_pbm(path: Path) -> frozenset[tuple[int, int]]:
    tokens = []
    for line in path.read_text(encoding="ascii").splitlines():
        tokens.extend(line.split("#", 1)[0].split())
    if len(tokens) < 3 or tokens[0] != "P1" or tokens[1:3] != ["96", "96"]:
        raise OracleInputError(f"invalid 96x96 P1 fixture: {path.name}")
    bits = tokens[3:]
    if len(bits) != 96 * 96 or any(bit not in ("0", "1") for bit in bits):
        raise OracleInputError(f"invalid PBM pixels: {path.name}")
    return frozenset((index % 96, index // 96) for index, bit in enumerate(bits) if bit == "1")


def _fixture_bytes(path: Path) -> bytes:
    """Canonicalize tracked text fixture line endings before hashing."""
    return path.read_bytes().replace(b"\r\n", b"\n")


def _load_fixtures(fixture_dir: Path) -> tuple[dict[str, dict[str, frozenset[tuple[int, int]]]], dict[str, Any], str]:
    manifest_path = fixture_dir / "manifest.json"
    thresholds_path = fixture_dir / "thresholds.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    thresholds = json.loads(thresholds_path.read_text(encoding="utf-8"))
    for filename, expected in manifest["sha256"].items():
        actual = hashlib.sha256(_fixture_bytes(fixture_dir / filename)).hexdigest()
        if actual != expected:
            raise OracleInputError(f"fixture hash mismatch: {filename}")
    calibration = json.loads((fixture_dir / "calibration.json").read_text(encoding="utf-8"))
    intervals = calibration["separating_intervals"]
    checks = (
        intervals["iou"]["negative_max"] < thresholds["min_iou"] < intervals["iou"]["positive_min"],
        intervals["chamfer_px"]["positive_max"] < thresholds["max_chamfer_px"] < intervals["chamfer_px"]["negative_min"],
        intervals["minimum_region_recall"]["negative_max"] < thresholds["min_region_recall"] < intervals["minimum_region_recall"]["positive_min"],
    )
    if not all(checks):
        raise OracleInputError("thresholds do not lie strictly inside frozen calibration intervals")
    loaded: dict[str, dict[str, frozenset[tuple[int, int]]]] = {}
    for name in manifest["templates"]:
        loaded[name] = {"silhouette": _read_pbm(fixture_dir / f"{name}.pbm")}
        for region in REGIONS:
            loaded[name][region] = _read_pbm(fixture_dir / f"{name}.{region}.pbm")
    return loaded, thresholds, hashlib.sha256(_fixture_bytes(thresholds_path)).hexdigest()


def _boundary(mask: frozenset[tuple[int, int]]) -> frozenset[tuple[int, int]]:
    return frozenset(
        p for p in mask
        if any((p[0] + dx, p[1] + dy) not in mask for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
    )


def _chamfer(a: frozenset[tuple[int, int]], b: frozenset[tuple[int, int]]) -> float:
    aa, bb = _boundary(a), _boundary(b)
    if not aa or not bb:
        return math.inf
    def directed(source: Iterable[tuple[int, int]], target: Iterable[tuple[int, int]]) -> float:
        target_list = list(target)
        return sum(min(math.hypot(x - u, y - v) for u, v in target_list) for x, y in source) / len(source)
    return (directed(aa, bb) + directed(bb, aa)) / 2.0


def _components(mask: frozenset[tuple[int, int]]) -> int:
    remaining = set(mask)
    count = 0
    while remaining:
        count += 1
        stack = [remaining.pop()]
        while stack:
            x, y = stack.pop()
            for q in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if q in remaining:
                    remaining.remove(q)
                    stack.append(q)
    return count


def _rasterize(polygons: list[list[tuple[float, float]]], template: frozenset[tuple[int, int]], reflected: bool) -> tuple[frozenset[tuple[int, int]], int, dict[str, float]]:
    sx0 = min(x for polygon in polygons for x, _ in polygon)
    sx1 = max(x for polygon in polygons for x, _ in polygon)
    sy0 = min(y for polygon in polygons for _, y in polygon)
    sy1 = max(y for polygon in polygons for _, y in polygon)
    tx0, tx1 = min(x for x, _ in template), max(x for x, _ in template) + 1
    ty0, ty1 = min(y for _, y in template), max(y for _, y in template) + 1
    if sx1 - sx0 <= EPSILON or sy1 - sy0 <= EPSILON:
        raise OracleInputError("selected geometry has a degenerate extent")
    scale = min((tx1 - tx0) / (sx1 - sx0), (ty1 - ty0) / (sy1 - sy0))
    scx, scy = (sx0 + sx1) / 2, (sy0 + sy1) / 2
    tcx, tcy = (tx0 + tx1) / 2, (ty0 + ty1) / 2
    transformed = []
    for polygon in polygons:
        current = []
        for x, y in polygon:
            nx = (x - scx) * scale
            if reflected:
                nx = -nx
            current.append((nx + tcx, (y - scy) * scale + tcy))
        transformed.append(current)
    counts: dict[tuple[int, int], int] = {}
    for polygon in transformed:
        px0 = max(0, math.floor(min(x for x, _ in polygon)))
        px1 = min(95, math.ceil(max(x for x, _ in polygon)))
        py0 = max(0, math.floor(min(y for _, y in polygon)))
        py1 = min(95, math.ceil(max(y for _, y in polygon)))
        for y in range(py0, py1 + 1):
            for x in range(px0, px1 + 1):
                if _point_inside((x + 0.5, y + 0.5), polygon):
                    counts[(x, y)] = counts.get((x, y), 0) + 1
    mask = frozenset(counts)
    overlap_pixels = sum(1 for value in counts.values() if value > 1)
    scale_x = -scale if reflected else scale
    return mask, overlap_pixels, {
        "scale_x": scale_x,
        "scale_y": scale,
        "dx": tcx - scx * scale_x,
        "dy": tcy - scy * scale,
        "reflected": reflected,
    }


def evaluate_cat(before_intake: dict[str, Any], after_intake: dict[str, Any], selected_handles: list[str], *, fixture_dir: Path = FIXTURE_DIR) -> dict[str, Any]:
    """Evaluate one rearrangement and return a stable, fail-closed report."""
    reasons: list[str] = []
    base_report = {
        "schema": SCHEMA,
        "selected_handles": list(selected_handles) if isinstance(selected_handles, list) else [],
        "selected_handle_count_before": 0,
        "selected_handle_count_after": 0,
        "identity_ok": False,
        "geometry_ok": False,
        "unselected_digest_equal": False,
        "input_hashes": {"before": _sha(before_intake), "after": _sha(after_intake), "selected_handles": _sha(selected_handles)},
        "input_sha256": _sha(before_intake),
        "output_sha256": _sha(after_intake),
    }
    try:
        if not isinstance(selected_handles, list) or not selected_handles or any(not isinstance(h, str) for h in selected_handles):
            raise OracleInputError("selected_handles must be a nonempty string list")
        if len(set(selected_handles)) != len(selected_handles):
            raise OracleInputError("selected_handles contains duplicates")
        before_entities, before = _entities(before_intake)
        after_entities, after = _entities(after_intake)
        selected = set(selected_handles)
        base_report["selected_handle_count_before"] = sum(handle in before for handle in selected_handles)
        base_report["selected_handle_count_after"] = sum(handle in after for handle in selected_handles)
        unknown = selected - before.keys()
        missing = selected - after.keys()
        if unknown:
            raise OracleInputError(f"unknown selected handles: {sorted(unknown)}")
        if missing:
            raise OracleInputError(f"missing selected handles: {sorted(missing)}")
        if set(before) != set(after):
            raise OracleInputError("entity handles changed")
        before_top = {key: value for key, value in before_intake.items() if key != "polylines"}
        after_top = {key: value for key, value in after_intake.items() if key != "polylines"}
        before_unselected = [e for e in before_entities if e["handle"] not in selected]
        after_unselected = [e for e in after_entities if e["handle"] not in selected]
        unselected_equal = _canonical(before_top) == _canonical(after_top) and _canonical(before_unselected) == _canonical(after_unselected)
        base_report["unselected_digest_equal"] = unselected_equal
        if not unselected_equal:
            raise OracleInputError("unselected intake changed")
        polygons: list[list[tuple[float, float]]] = []
        for handle in selected_handles:
            b, a = before[handle], after[handle]
            bp, ap = _points(b), _points(a)
            for field in ("handle", "layer", "closed", "xdata", "metadata"):
                if _canonical(b.get(field)) != _canonical(a.get(field)):
                    raise OracleInputError(f"{handle}: {field} changed")
            if len(bp) != len(ap) or any(abs(bp[i][2] - ap[i][2]) > EPSILON for i in range(len(bp))):
                raise OracleInputError(f"{handle}: vertex count or Z changed")
            if abs(_signed_area(bp) - _signed_area(ap)) > EPSILON:
                raise OracleInputError(f"{handle}: signed area changed")
            if any(abs(x - y) > EPSILON for x, y in zip(_pairwise(bp), _pairwise(ap))):
                raise OracleInputError(f"{handle}: pairwise distances changed")
            polygons.append([(p[0], p[1]) for p in ap])
        base_report["identity_ok"] = True
        base_report["geometry_ok"] = True
        exact_overlap = _any_overlap(polygons)
        fixtures, thresholds, thresholds_hash = _load_fixtures(fixture_dir)
        candidates = []
        for template_name in sorted(fixtures):
            template = fixtures[template_name]["silhouette"]
            for reflected in (False, True):
                mask, overlap_pixels, alignment = _rasterize(polygons, template, reflected)
                intersection = len(mask & template)
                union = len(mask | template)
                iou = intersection / union if union else 0.0
                region_recall = {
                    region: len(mask & fixtures[template_name][region]) / max(1, len(fixtures[template_name][region]))
                    for region in REGIONS
                }
                candidates.append({
                    "template": template_name, "reflected": reflected, "mask": mask,
                    "iou": iou, "chamfer": _chamfer(mask, template),
                    "region_recall": region_recall, "components_4": _components(mask),
                    "overlap_pixels": overlap_pixels, "alignment": alignment,
                })
        best = min(candidates, key=lambda c: (-c["iou"], c["chamfer"], c["template"], c["reflected"]))
        if best["iou"] < thresholds["min_iou"]:
            reasons.append("iou_below_threshold")
        if best["chamfer"] > thresholds["max_chamfer_px"]:
            reasons.append("chamfer_above_threshold")
        if best["components_4"] != 1:
            reasons.append("not_one_4_connected_component")
        if exact_overlap or best["overlap_pixels"] > thresholds["max_overlap_pixels"]:
            reasons.append("selected_polygons_overlap")
        low_regions = sorted(k for k, value in best["region_recall"].items() if value < thresholds["min_region_recall"])
        if low_regions:
            reasons.append("region_recall_below_threshold:" + ",".join(low_regions))
        report = {
            **base_report,
            "fixture_manifest_hash": hashlib.sha256(_fixture_bytes(fixture_dir / "manifest.json")).hexdigest(),
            "thresholds_hash": thresholds_hash,
            "template_sha256": hashlib.sha256(_fixture_bytes(fixture_dir / f"{best['template']}.pbm")).hexdigest(),
            "template": best["template"], "alignment": best["alignment"],
            "metrics": {"iou": best["iou"], "outline_chamfer_px": best["chamfer"], "region_recall": best["region_recall"], "components_4": best["components_4"], "overlap_pixels": best["overlap_pixels"], "overlap_pixel_fraction": best["overlap_pixels"] / max(1, len(best["mask"]))},
            "verdict": "pass" if not reasons else "fail", "reasons": reasons,
        }
        return report
    except (OracleInputError, KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {**base_report, "verdict": "fail", "reasons": [f"invalid_evidence:{exc}"]}
