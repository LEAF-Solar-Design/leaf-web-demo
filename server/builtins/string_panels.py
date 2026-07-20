"""Built-in: string-panels. The file IS the tool (CONTRACT section 2).

Serpentine DC string routing over the intake's closed "Panels"-layer polylines:

  1. compute each panel's centroid (WORLD coordinates — the same space as the
     intake polylines; drawing units are inches, matching measure-panel-area's
     units assumption),
  2. cluster panels into BANKS (roof sections): union-find over centroid pairs
     within ``cluster_radius_factor`` x the median nearest-neighbor distance —
     a string never leaps a roof-section gap,
  3. per bank, estimate the row orientation from the panels' longest polyline
     edges (rotated banks band along their own rows, not the world axes),
     rotate into the bank frame, band centroids into rows by frame-y
     (tolerance = half the median rotated bbox height), and order panels
     serpentine (row 0 left->right, row 1 right->left, ...),
  4. chunk each bank's walk into strings of at most
     ``max_modules_per_string``, where that limit comes from the NEC 690.7
     cold-temperature Voc check:

         worst_voc_per_module = voc * (1 + temp_coeff_pct_per_c/100
                                          * (design_min_temp_c - 25))
         max_modules_per_string = floor(max_system_voltage / worst_voc_per_module)

ALL electrical inputs are explicit params with defaults. The defaults model the
"Q.Peak DUO XL-G11.7 / 425" module and are MARKETING-DEMO defaults authored from
public Q.Peak 425-class datasheet values (Voc ~48.5 V, temp coefficient of Voc
~ -0.27 %/degC, -25 degC design minimum, 1000 V max system voltage — the
commercial-rooftop limit, operator-selected 2026-07-20); not engineering
advice, not a stamped design.

Deterministic: identical intake + params -> byte-identical (result, overlay).
No randomness, no timestamps, no environment reads in the solve body.
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Dict, List, Tuple

# MARKETING-DEMO defaults (Q.Peak DUO XL-G11.7 / 425 class, public datasheet
# ballpark) — pending operator blessing. Every value is overridable per-run.
DEFAULTS: Dict[str, Any] = {
    "module_model": "Q.Peak DUO XL-G11.7 / 425",
    "voc": 48.5,                     # volts, STC open-circuit voltage
    "temp_coeff_pct_per_c": -0.27,   # %/degC, Voc temperature coefficient
    "design_min_temp_c": -25.0,      # degC, design minimum ambient
    "max_system_voltage": 1000.0,    # volts (NEC 690.7 commercial-rooftop ceiling)
    "panel_layer": "Panels",         # intake layer carrying the panel outlines
    "cluster_radius_factor": 3.0,    # bank clustering: panels within this
                                     # multiple of the median nearest-neighbor
                                     # distance belong to the same roof section
}

INCHES_PER_FOOT = 12.0


def nec_max_modules(voc: float, temp_coeff_pct_per_c: float,
                    design_min_temp_c: float, max_system_voltage: float
                    ) -> Tuple[int, float]:
    """NEC 690.7 cold-temp string sizing.

    Returns (max_modules_per_string, worst_voc_per_module). The worst-case
    per-module Voc is the STC Voc corrected to the design minimum temperature.
    """
    worst_voc = float(voc) * (
        1.0 + float(temp_coeff_pct_per_c) / 100.0 * (float(design_min_temp_c) - 25.0)
    )
    if worst_voc <= 0:
        raise ValueError(
            f"NEC 690.7 check degenerate: worst-case Voc {worst_voc:.3f} V <= 0 "
            "(check voc / temp_coeff_pct_per_c / design_min_temp_c)")
    max_modules = int(math.floor(float(max_system_voltage) / worst_voc))
    if max_modules < 1:
        raise ValueError(
            f"NEC 690.7 check yields 0 modules per string (worst-case Voc "
            f"{worst_voc:.2f} V exceeds max system voltage {max_system_voltage} V)")
    return max_modules, worst_voc


def _centroid(panel: Dict[str, Any]) -> Tuple[float, float]:
    pts = panel.get("pts") or []
    xs = [pt[0] for pt in pts]
    ys = [pt[1] for pt in pts]
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def _longest_edge_angle(panel: Dict[str, Any]) -> float:
    """Direction of the panel's longest polyline edge, folded into [0, pi) —
    rows run along the module edges, whatever the bank's rotation."""
    pts = panel.get("pts") or []
    best_len, best_ang = 0.0, 0.0
    n = len(pts)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        if length > best_len:
            best_len, best_ang = length, math.atan2(b[1] - a[1], b[0] - a[0])
    return best_ang % math.pi


_GRID_CELL = 200.0  # inches; nearest-neighbor search window (>> module pitch)


def _cluster_banks(cents: List[Tuple[float, float]], radius_factor: float
                   ) -> List[List[int]]:
    """Union-find over centroid pairs within radius_factor x the median
    nearest-neighbor distance. Returns banks as index lists, deterministically
    ordered by (min y, min x) of their members."""
    grid: Dict[Tuple[int, int], List[int]] = {}
    for i, c in enumerate(cents):
        grid.setdefault((int(c[0] // _GRID_CELL), int(c[1] // _GRID_CELL)), []).append(i)

    def neighbors(i: int, radius: float) -> List[int]:
        cx, cy = cents[i]
        out: List[int] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((int(cx // _GRID_CELL) + dx,
                                   int(cy // _GRID_CELL) + dy), []):
                    if j != i and math.hypot(cents[j][0] - cx, cents[j][1] - cy) <= radius:
                        out.append(j)
        return out

    nn = [
        min((math.hypot(cents[j][0] - cents[i][0], cents[j][1] - cents[i][1])
             for j in neighbors(i, _GRID_CELL)), default=float("inf"))
        for i in range(len(cents))
    ]
    finite = [d for d in nn if math.isfinite(d)]
    if not finite:  # every panel isolated — each is its own bank
        return [[i] for i in range(len(cents))]
    radius = statistics.median(finite) * radius_factor

    parent = list(range(len(cents)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(cents)):
        for j in neighbors(i, radius):
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[ri] = rj

    groups: Dict[int, List[int]] = {}
    for i in range(len(cents)):
        groups.setdefault(find(i), []).append(i)
    banks = list(groups.values())
    banks.sort(key=lambda idxs: (min(cents[i][1] for i in idxs),
                                 min(cents[i][0] for i in idxs)))
    return banks


def _bank_serpentine(panels: List[Dict[str, Any]], idxs: List[int],
                     cents: List[Tuple[float, float]]) -> List[int]:
    """Serpentine panel order within one bank, in the bank's own frame:
    rotate by the dominant longest-edge angle (1-degree histogram peak, mean of
    members within +-2 degrees), band rows by frame-y (tol = half the median
    rotated bbox height), even rows left->right, odd rows right->left."""
    angles = [_longest_edge_angle(panels[i]) for i in idxs]
    bins: Dict[int, int] = {}
    for a in angles:
        bins[int(a * 180.0 / math.pi)] = bins.get(int(a * 180.0 / math.pi), 0) + 1
    peak = min((d for d, n in bins.items() if n == max(bins.values())))
    sel = [a for a in angles if abs(a * 180.0 / math.pi - peak) <= 2.0]
    theta = statistics.mean(sel) if sel else 0.0
    ca, sa = math.cos(-theta), math.sin(-theta)

    def rot(pt: Tuple[float, float]) -> Tuple[float, float]:
        return (pt[0] * ca - pt[1] * sa, pt[0] * sa + pt[1] * ca)

    frame = []  # (frame_x, frame_y, rotated bbox height, panel index)
    for i in idxs:
        fx, fy = rot(cents[i])
        rys = [rot((pt[0], pt[1]))[1] for pt in panels[i]["pts"]]
        frame.append((fx, fy, max(rys) - min(rys), i))

    tol = statistics.median(f[2] for f in frame) * 0.5
    rows: List[List[Tuple[float, float, float, int]]] = []
    for f in sorted(frame, key=lambda f: (f[1], f[0])):
        if rows and (f[1] - rows[-1][0][1]) <= tol:
            rows[-1].append(f)
        else:
            rows.append([f])

    ordered: List[int] = []
    for r, row in enumerate(rows):
        row = sorted(row, key=lambda f: (f[0], f[1]))
        ordered.extend(f[3] for f in (row if r % 2 == 0 else row[::-1]))
    return ordered


def run(intake: Dict[str, Any], params: Dict[str, Any]):
    p = dict(DEFAULTS)
    p.update({k: v for k, v in (params or {}).items() if v is not None})

    layer = str(p["panel_layer"])
    panels = [
        pl for pl in (intake or {}).get("polylines", [])
        if pl.get("layer") == layer and pl.get("closed") and pl.get("pts")
    ]
    if not panels:
        raise ValueError(f"no closed polylines found on layer {layer!r}")

    max_modules, worst_voc_per_module = nec_max_modules(
        p["voc"], p["temp_coeff_pct_per_c"], p["design_min_temp_c"],
        p["max_system_voltage"])

    cents = [_centroid(pl) for pl in panels]
    banks = _cluster_banks(cents, float(p["cluster_radius_factor"]))

    strings: List[Dict[str, Any]] = []
    lengths_ft: List[float] = []
    for idxs in banks:
        ordered = _bank_serpentine(panels, idxs, cents)
        for si in range(0, len(ordered), max_modules):
            chunk = [cents[i] for i in ordered[si:si + max_modules]]
            pts = [[round(c[0], 3), round(c[1], 3)] for c in chunk]
            length_units = sum(
                math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(chunk, chunk[1:]))
            length_ft = round(length_units / INCHES_PER_FOOT, 1)
            lengths_ft.append(length_ft)
            strings.append({
                "id": f"S{len(strings) + 1:03d}",
                "modules": len(chunk),
                "pts": pts,      # WORLD coordinates (same space as intake polylines)
                "length_ft": length_ft,
            })

    biggest = max(s["modules"] for s in strings)
    worst_string_voc = round(biggest * worst_voc_per_module, 2)

    # Worst run delta: max deviation from the mean wire length, over FULL
    # strings (modules == max_modules) — partial remainder strings are shorter
    # by construction and would only measure the chunking, not the routing.
    # Falls back to all strings when fewer than two are full; the basis is
    # always named in the stats so the number can't be misread.
    full_lengths = [s["length_ft"] for s in strings if s["modules"] == max_modules]
    basis_lengths, basis = ((full_lengths, "full-strings") if len(full_lengths) >= 2
                            else (lengths_ft, "all-strings"))
    mean_ft = sum(basis_lengths) / len(basis_lengths)
    worst_run_delta_pct = (
        round(max(abs(l - mean_ft) for l in basis_lengths) / mean_ft * 100.0, 2)
        if mean_ft > 0 else 0.0)

    result = {
        "module": {
            "model": p["module_model"],
            "voc": float(p["voc"]),
            "temp_coeff_pct_per_c": float(p["temp_coeff_pct_per_c"]),
            "design_min_temp_c": float(p["design_min_temp_c"]),
            "max_system_voltage": float(p["max_system_voltage"]),
        },
        "electrical": {
            "code_check": "NEC 690.7",
            "max_modules_per_string": max_modules,
            "worst_string_voc": worst_string_voc,
            "pass": worst_string_voc <= float(p["max_system_voltage"]),
        },
        "strings": strings,
        "stats": {
            "panel_count": len(panels),
            "bank_count": len(banks),
            "string_count": len(strings),
            "full_string_count": len(full_lengths),
            "modules_stringed": sum(s["modules"] for s in strings),
            "worst_run_delta_pct": worst_run_delta_pct,
            "worst_run_delta_basis": basis,
            "total_wire_ft": round(sum(lengths_ft), 1),
        },
    }
    overlay = {
        "kind": "polylines",
        "layer": "StringRuns",
        "items": [{"id": s["id"], "pts": s["pts"]} for s in strings],
    }
    return result, overlay
