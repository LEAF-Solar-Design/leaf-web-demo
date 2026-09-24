"""Studio's lightweight cabling engine: a literal port of the plugin's cabling studio (LEAFLITEPLACE,
LEAFCABLEVIEW, LEAFLITEFREE; contract G36).

Sources (Branch2025, LeafSolarDesign.Core/LightweightCabling, cited file:line):
  LightweightCablingEngine.cs  Place (:26-151), BuildFindings (:156-214), BuildFeederPaths (:226-255),
                               NearestLaneX (:257-267), DedupePath (:269-284), DeriveRows (:291-346),
                               RoutedDist (:351-369), GroupSameRowFirst (:377-457), NearestLaneSide
                               (:460-470), BuildHomerunPaths (:476-516), ConsolidateLaneAware (:519-618),
                               SmallestSku (:861-866), Cell (:868)
  CablingSolver.cs             FilterPhantomInverters (:31-51), AssignFeeders (:53-90), SwapOptimize (:92-146)
  LaneDeriver.cs               DeriveVerticalLaneXs (:26-84), ExtentsFromStrings (:86-92)
  LightweightCablingOptions.cs every default (:15-103), SkuAcKw
The engine works in drawing units exactly as the plugin does: its options are named in feet and applied to
the drawing's own coordinates, whatever the drawing unit is.

Free inverter placement (LEAFLITEFREE's k-means, gear pull and road snap) is not ported: `place` refuses it
by name. Pure and bounded: O(n) grids for the neighbour searches, the plugin's own pass caps elsewhere.
"""
from __future__ import annotations

import math

MAX_STRINGS = 100_000
MAX_INVERTERS = 10_000
ROW_MIN_FILL = 8                                             # LightweightCablingEngine.cs:371
SKU_CATALOG = (8, 12, 16, 20, 24, 28, 32)                   # :861
PHANTOM_STACK_RADIUS = 100.0                                 # CablingSolver.cs:39
SWAP_MAX_PASSES = 60                                         # :120
SWAP_EPSILON = 1e-6                                          # :136
DEDUPE_EPSILON = 1e-6                                        # LightweightCablingEngine.cs:271
LONG_FEEDER = 1800.0                                         # :206


class LiteCablingError(ValueError):
    """A malformed request; the engine refuses rather than guessing."""


class LiteCablingNotPortedError(LiteCablingError):
    """A path of the plugin engine this port does not carry."""


def default_options():
    """LightweightCablingOptions.CreateDefault (LightweightCablingOptions.cs:15-103)."""
    return {"FillCap": 16, "Reach": 60.0, "MaxHomerunFt": 150.0, "DcAcMax": 1.29, "FreeInverters": False,
            "InvSku": "C", "FreeDcAc": 1.21, "BandTol": 1.02, "CapPerInverter": 36, "Inv2Enabled": False,
            "Inv2AcKw": 2520.0, "Inv2Floor": 0.9, "RowAwareRouting": True, "RowXTolFt": 4.0, "RowYGapFt": 36.0,
            "CrossPenaltyFt": 8.0, "RackHalfWidthFt": 8.0, "FeederTailBias": True, "DirectFeeders": False,
            "MvWeight": 2.0, "RoadClearanceFt": 40.0, "LaneBucketFt": 1.0, "MinLaneWidthFt": 25.0,
            "StringKw": 28 * 550 / 1000.0}


def sku_ac_kw(options):
    return {"A": 5040.0, "B": 2520.0, "C": 3150.0}.get(options["InvSku"], 3150.0)


def _dist(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2)


def _cell(value, cell):
    return math.floor(value / cell)


def _average(values):
    return sum(values) / len(values)


# ------------------------------------------------------------------------------------------------ lanes --

def extents_from_strings(strings):
    """LaneDeriver.ExtentsFromStrings (:86-92): each string's X span."""
    return [(min(a[0], b[0]), max(a[0], b[0])) for a, b in strings]


def derive_vertical_lane_xs(extents, bucket, min_lane_width):
    """LaneDeriver.DeriveVerticalLaneXs (:26-84)."""
    lanes = []
    if not extents or bucket <= 0:
        return lanes
    lo = min(e[0] for e in extents)
    hi = max(e[1] for e in extents)
    if hi <= lo:
        return lanes
    n = int((hi - lo) / bucket) + 2
    if n > 50_000_000:
        raise LiteCablingError("the lane bucket grid is too large")
    occupied = bytearray(n)
    for min_x, max_x in extents:
        a = max(0, int((min_x - lo) / bucket))
        b = min(n - 1, int((max_x - lo) / bucket))
        if a <= b:
            occupied[a:b + 1] = b"\x01" * (b - a + 1)
    bands, index = [], 0
    while index < n:
        if occupied[index]:
            j = index
            while j < n and occupied[j]:
                j += 1
            bands.append((index, j - 1))
            index = j
        else:
            index += 1
    if not bands:
        return lanes
    for k in range(1, len(bands)):
        gap_lo, gap_hi = bands[k - 1][1] + 1, bands[k][0] - 1
        if (gap_hi - gap_lo + 1) * bucket < min_lane_width:
            continue
        lanes.append(lo + (gap_lo + gap_hi) / 2.0 * bucket)
    first_width = (bands[0][1] - bands[0][0] + 1) * bucket
    edge = max(bucket, first_width * 0.25)
    lanes.insert(0, lo + bands[0][0] * bucket - edge)
    lanes.append(lo + bands[-1][1] * bucket + edge)
    return lanes


def nearest_lane_x(lanes, combiner_x, inverter_x):
    """NearestLaneX (:257-267)."""
    if not lanes:
        return inverter_x
    best, best_d = lanes[0], abs(lanes[0] - combiner_x)
    for lane in lanes[1:]:
        d = abs(lane - combiner_x)
        if d < best_d:
            best_d, best = d, lane
    return best


def nearest_lane_side(x, lanes):
    """NearestLaneSide (:460-470): +1 right, -1 left."""
    if not lanes:
        return 1
    best, best_d = lanes[0], abs(lanes[0] - x)
    for lane in lanes[1:]:
        d = abs(lane - x)
        if d < best_d:
            best_d, best = d, lane
    return 1 if best >= x else -1


def dedupe_path(raw):
    """DedupePath (:269-284): consecutive duplicates, then collinear interior vertices, removed."""
    pts = []
    for p in raw:
        if not pts or _dist(pts[-1], p) > DEDUPE_EPSILON:
            pts.append(p)
    i = 1
    while i < len(pts) - 1:
        a, b, c = pts[i - 1], pts[i], pts[i + 1]
        cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if abs(cross) < DEDUPE_EPSILON:
            del pts[i]
        else:
            i += 1
    return pts


def build_feeder_paths(assignments, cb_pos, inv_pos, lanes, direct):
    """BuildFeederPaths (:226-255): [(cb, inv, points)] in assignment order."""
    paths = []
    for cb, inv in assignments:
        if cb not in cb_pos or inv not in inv_pos:
            continue
        c, i = cb_pos[cb], inv_pos[inv]
        if direct:
            points = [c, i]
        else:
            bus = nearest_lane_x(lanes, c[0], i[0])
            points = dedupe_path([c, (bus, c[1]), (bus, i[1]), i])
        if len(points) >= 2:
            paths.append((cb, inv, points))
    return paths


# ------------------------------------------------------------------------------------------- row-aware --

def derive_rows(pts, x_tol, y_gap):
    """DeriveRows (:291-346): (rows, s2row); a row is {id, x, members, end0, end1}."""
    n = len(pts)
    cell = 36.0 if y_gap <= 0 else y_gap
    grid = {}
    for i, (x, y) in enumerate(pts):
        grid.setdefault((_cell(x, cell), _cell(y, cell)), []).append(i)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, (ix, iy) in enumerate(pts):
        gx, gy = _cell(ix, cell), _cell(iy, cell)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((gx + dx, gy + dy), ()):
                    if j > i and abs(pts[j][0] - ix) <= x_tol and abs(pts[j][1] - iy) <= y_gap:
                        ra, rb = find(i), find(j)
                        if ra != rb:
                            parent[ra] = rb
    comps = {}
    for i in range(n):
        comps.setdefault(find(i), []).append(i)
    s2row = [0] * n
    rows = []
    for members in comps.values():
        members.sort(key=lambda m: pts[m][1])
        x_mean = _average([pts[m][0] for m in members])
        rid = len(rows)
        for m in members:
            s2row[m] = rid
        rows.append({"id": rid, "x": x_mean, "members": members, "end0": (x_mean, pts[members[0]][1]),
                     "end1": (x_mean, pts[members[-1]][1])})
    return rows, s2row


def routed_dist(i, j, pts, rows, s2row, cross_penalty):
    """RoutedDist (:351-369)."""
    ri, rj = s2row[i], s2row[j]
    cyi = pts[i][1]
    if ri == rj:
        return abs(cyi - pts[j][1])
    a, b = rows[ri], rows[rj]
    cyj = pts[j][1]
    best = math.inf
    for ea, eb in ((a["end0"], b["end0"]), (a["end1"], b["end1"])):
        d = abs(cyi - ea[1]) + abs(ea[0] - eb[0]) + abs(ea[1] - eb[1]) + abs(eb[1] - cyj) + cross_penalty
        if d < best:
            best = d
    return best


def group_same_row_first(pts, rows, s2row, lanes, string_ids, cap, reach, cross_penalty, rack_half):
    """GroupSameRowFirst (:377-457): [{number, location, string_ids, n, row_id, x_row, side, pure}]."""
    boxes, box_row = [], []
    for r in rows:
        members = r["members"]
        for k in range(0, len(members), cap):
            boxes.append(list(members[k:k + cap]))
            box_row.append(r["id"])
    alive = [True] * len(boxes)
    by_row = {}
    for bi, rid in enumerate(box_row):
        by_row.setdefault(rid, []).append(bi)

    def box_y(bi):
        return _average([pts[s][1] for s in boxes[bi]])

    for bi in range(len(boxes)):
        if not alive[bi] or len(boxes[bi]) >= ROW_MIN_FILL:
            continue
        best, best_d = -1, math.inf
        for bj in by_row[box_row[bi]]:
            if bj == bi or not alive[bj] or len(boxes[bj]) + len(boxes[bi]) > cap + 4:
                continue
            d = abs(box_y(bi) - box_y(bj))
            if d < best_d:
                best_d, best = d, bj
        if best < 0:
            for bj in range(len(boxes)):
                if bj == bi or not alive[bj] or box_row[bj] == box_row[bi]:
                    continue
                if len(boxes[bj]) + len(boxes[bi]) > cap + 4:
                    continue
                d = routed_dist(boxes[bi][0], boxes[bj][0], pts, rows, s2row, cross_penalty)
                if d <= reach and d < best_d:
                    best_d, best = d, bj
        if best >= 0:
            boxes[best].extend(boxes[bi])
            alive[bi] = False
    out, number = [], 1
    for bi, members in enumerate(boxes):
        if not alive[bi]:
            continue
        counts = {}
        for s in members:
            counts[s2row[s]] = counts.get(s2row[s], 0) + 1
        dom, dom_count = next(iter(counts)), -1
        for rid, count in counts.items():
            if count > dom_count:
                dom_count, dom = count, rid
        x_row = rows[dom]["x"]
        side = nearest_lane_side(x_row, lanes)
        ys = sorted(pts[s][1] for s in members)
        out.append({"number": number, "location": (x_row + side * rack_half, ys[len(ys) // 2]),
                    "string_ids": [string_ids[s] for s in members], "n": len(members), "row_id": dom,
                    "x_row": x_row, "side": side, "pure": len(counts) == 1})
        number += 1
    return out


def build_homerun_paths(combiners, pts, rows, s2row):
    """BuildHomerunPaths (:476-516): [(string id, combiner number, points)] (string ids index `pts`)."""
    paths = []
    for c in combiners:
        cbx, cby = c["location"]
        x_row = c["x_row"]
        for sid in c["string_ids"]:
            si = pts[sid]
            if s2row[sid] == c["row_id"]:
                pp = dedupe_path([si, (x_row, cby), (cbx, cby)])
            else:
                a = rows[s2row[sid]]
                pp, best = None, math.inf
                for end in (a["end0"], a["end1"]):
                    road = end[1]
                    d = abs(si[1] - road) + abs(a["x"] - x_row) + abs(road - cby)
                    if d < best:
                        best = d
                        pp = dedupe_path([si, (a["x"], road), (x_row, road), (x_row, cby), (cbx, cby)])
            if pp is not None and len(pp) >= 2:
                paths.append((sid, c["number"], pp))
    return paths


# ------------------------------------------------------------------------------------ euclidean grouping --

def consolidate_lane_aware(pts, target, reach, max_homerun):
    """ConsolidateLaneAware (:519-618): [(medoid position, [string index])]."""
    n = len(pts)
    cell = 60.0 if reach <= 0 else reach
    grid = {}
    for i, (x, y) in enumerate(pts):
        grid.setdefault((_cell(x, cell), _cell(y, cell)), []).append(i)

    def neighbors(i):
        gx, gy = _cell(pts[i][0], cell), _cell(pts[i][1], cell)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                yield from grid.get((gx + dx, gy + dy), ())

    band = 24.0

    def row_band(i):                                         # (long)Math.Round: banker's rounding, as Python's
        return int(round(pts[i][1] / band))

    order = sorted(range(n), key=lambda i: (row_band(i), pts[i][0] if row_band(i) % 2 == 0 else -pts[i][0]))
    assigned = [False] * n
    s2g = [-1] * n
    groups = []
    for seed in order:
        if assigned[seed]:
            continue
        assigned[seed] = True
        g = [seed]
        s2g[seed] = len(groups)
        cx, cy = pts[seed]
        cand = []                                            # HashSet<int>: first-inserted order on ties
        seen = set()
        for j in neighbors(seed):
            if not assigned[j] and _dist(pts[seed], pts[j]) <= reach and j not in seen:
                seen.add(j)
                cand.append(j)
        while len(g) < target and cand:
            best, best_d = -1, math.inf
            for j in cand:
                d = (pts[j][0] - cx) ** 2 + (pts[j][1] - cy) ** 2
                if d < best_d:
                    best_d, best = d, j
            if best < 0:
                break
            cand.remove(best)
            seen.discard(best)
            if assigned[best]:
                continue
            if max_homerun > 0 and math.sqrt(best_d) > max_homerun:
                break
            assigned[best] = True
            g.append(best)
            s2g[best] = len(groups)
            cx = _average([pts[k][0] for k in g])
            cy = _average([pts[k][1] for k in g])
            for j in neighbors(best):
                if not assigned[j] and _dist(pts[best], pts[j]) <= reach and j not in seen:
                    seen.add(j)
                    cand.append(j)
        groups.append(g)
    minimum = max(4, target // 3)
    for gi, g in enumerate(groups):
        if not g or len(g) >= minimum:
            continue
        best_g, best_d = -1, math.inf
        for s in g:
            for j in neighbors(s):
                og = s2g[j]
                if og < 0 or og == gi or not groups[og] or len(groups[og]) + len(g) > target + 4:
                    continue
                d = _dist(pts[s], pts[j])
                if d < best_d:
                    best_d, best_g = d, og
        if best_g >= 0:
            for s in g:
                s2g[s] = best_g
            groups[best_g].extend(g)
            groups[gi] = []
    result = []
    for g in groups:
        if not g:
            continue
        cx = _average([pts[i][0] for i in g])
        cy = _average([pts[i][1] for i in g])
        medoid, md = g[0], math.inf
        for i in g:
            d = (pts[i][0] - cx) ** 2 + (pts[i][1] - cy) ** 2
            if d < md:
                md, medoid = d, i
        result.append((pts[medoid], g))
    return result


# ------------------------------------------------------------------------------------------- solver --

def filter_phantom_inverters(cbs, invs):
    """FilterPhantomInverters (CablingSolver.cs:31-51): (kept, dropped); items are (number, position)."""
    if not cbs or len(invs) < 4:
        return list(invs), []
    near = [min(_dist(i[1], c[1]) for c in cbs) for i in invs]
    median = sorted(near)[len(near) // 2]
    threshold = max(median * 4.0, 1.0)
    kept, dropped = [], []
    for index, inv in enumerate(invs):
        far = near[index] > threshold
        stacked = far and any(j != index and _dist(inv[1], other[1]) < PHANTOM_STACK_RADIUS
                              for j, other in enumerate(invs))
        (dropped if far and stacked else kept).append(inv)
    return kept, dropped


def assign_feeders(cbs, invs, cap_override=0, weight_cap=0, tail_bias=False):
    """AssignFeeders (CablingSolver.cs:53-90) then SwapOptimize: ({cb: inv}, total distance). `cbs` are
    (number, position, weight) and `invs` (number, position)."""
    if not cbs or not invs:
        return {}, 0.0
    n, m = len(cbs), len(invs)
    max_per = cap_override if cap_override > 0 else (n + m - 1) // m
    load = {num: 0 for num, _ in invs}
    load_w = {num: 0 for num, _ in invs}

    def nearest(c):
        return min(_dist(c[1], i[1]) for i in invs)
    # OrderBy and OrderByDescending are stable sorts: equal keys keep the list order.
    order = sorted(cbs, key=lambda c: -nearest(c)) if tail_bias else sorted(cbs, key=nearest)
    assign = {}
    for num, pos, weight in order:
        best, best_d = None, math.inf
        for inv_num, inv_pos in invs:
            if load[inv_num] < max_per and (weight_cap <= 0 or load_w[inv_num] + weight <= weight_cap):
                d = _dist(pos, inv_pos)
                if d < best_d:
                    best_d, best = d, inv_num
        if best is None:
            key = (lambda i: load_w[i[0]]) if weight_cap > 0 else (lambda i: load[i[0]])
            best = min(invs, key=key)[0]                     # OrderBy(...).First(): the first minimum
        assign[num] = best
        load[best] += 1
        load_w[best] += weight
    swap_optimize(cbs, invs, assign, weight_cap, tail_bias)
    positions = dict(invs)
    total = sum(_dist(pos, positions[assign[num]]) for num, pos, _ in cbs)
    return assign, total


def swap_optimize(cbs, invs, assign, weight_cap=0, tail_bias=False):
    """SwapOptimize (CablingSolver.cs:92-146), in place."""
    n, m = len(cbs), len(invs)
    if n < 2 or m < 2:
        return
    inv_index = {num: j for j, (num, _) in enumerate(invs)}
    dist = [[_dist(cbs[i][1], invs[j][1]) for j in range(m)] for i in range(n)]
    a = [inv_index[assign[cbs[i][0]]] for i in range(n)]
    w = [cbs[i][2] for i in range(n)]
    load_w = [0] * m
    for i in range(n):
        load_w[a[i]] += w[i]
    improved, passes = True, 0
    while improved and passes < SWAP_MAX_PASSES:
        improved = False
        passes += 1
        for i in range(n):
            ai = a[i]
            di = dist[i]
            for k in range(i + 1, n):
                bk = a[k]
                if bk == ai:
                    continue
                dk = dist[k]
                if tail_bias:
                    delta = (di[bk] * di[bk] + dk[ai] * dk[ai]) - (di[ai] * di[ai] + dk[bk] * dk[bk])
                else:
                    delta = (di[bk] + dk[ai]) - (di[ai] + dk[bk])
                if delta >= -SWAP_EPSILON:
                    continue
                if weight_cap > 0:
                    if load_w[ai] - w[i] + w[k] > weight_cap or load_w[bk] - w[k] + w[i] > weight_cap:
                        continue
                    load_w[ai] += w[k] - w[i]
                    load_w[bk] += w[i] - w[k]
                a[i], a[k] = bk, ai
                ai = bk
                improved = True
    for i in range(n):
        assign[cbs[i][0]] = invs[a[i]][0]


def smallest_sku(strings_per_box):
    """SmallestSku (:861-866)."""
    for sku in SKU_CATALOG:
        if sku >= strings_per_box:
            return sku
    return SKU_CATALOG[-1]


# -------------------------------------------------------------------------------------------- place --

def place(strings, existing_inverters, rack_extents=None, options=None):
    """LightweightCablingEngine.Place (:26-151). `strings`: [(A, B)] endpoint pairs, their list index the string
    id; `existing_inverters`: [(number, position)] in the L2 registration order; `rack_extents`: [(min x, max x)]
    or None. Returns the result as a dict (success, warnings, findings, combiners, inverters, assignments,
    lanes, homerun_paths, total_feeder)."""
    opt = default_options() if options is None else options
    result = {"success": False, "warnings": [], "findings": [], "combiners": [], "inverters": [],
              "assignments": [], "lanes": [], "homerun_paths": None, "total_feeder": 0.0}
    if not isinstance(strings, list) or len(strings) > MAX_STRINGS:
        raise LiteCablingError("the strings are not a bounded list")
    if not isinstance(existing_inverters, list) or len(existing_inverters) > MAX_INVERTERS:
        raise LiteCablingError("the inverters are not a bounded list")
    if not strings:
        result["warnings"].append("No strings supplied.")
        return result
    string_pts = [a for a, _ in strings]
    extents = rack_extents if rack_extents else extents_from_strings(strings)
    lanes = derive_vertical_lane_xs(extents, opt["LaneBucketFt"], opt["MinLaneWidthFt"])
    ids = list(range(len(strings)))

    rows = s2row = None
    if opt["RowAwareRouting"]:
        rows, s2row = derive_rows(string_pts, opt["RowXTolFt"], opt["RowYGapFt"])
        combiners = group_same_row_first(string_pts, rows, s2row, lanes, ids, opt["FillCap"], opt["Reach"],
                                         opt["CrossPenaltyFt"], opt["RackHalfWidthFt"])
    else:
        combiners = [{"number": k, "location": pos, "string_ids": [ids[i] for i in g], "n": len(g)}
                     for k, (pos, g) in enumerate(consolidate_lane_aware(string_pts, opt["FillCap"], opt["Reach"],
                                                                         opt["MaxHomerunFt"]), 1)]

    if opt["FreeInverters"]:
        raise LiteCablingNotPortedError("free inverter placement (LEAFLITEFREE)")
    raw = [(num, pos) for num, pos in existing_inverters]
    solver_cbs = [(c["number"], c["location"]) for c in combiners]
    inv_nodes, dropped = filter_phantom_inverters(solver_cbs, raw)
    if dropped:
        result["warnings"].append(f"Dropped {len(dropped)} phantom inverter(s).")
    result["inverters"] = [{"number": num, "location": pos, "is_new": False, "from_number": num, "load": 0,
                            "spec": "A"} for num, pos in inv_nodes]
    if not inv_nodes:
        result["warnings"].append("No inverters available to assign feeders to.")
        return result

    ac_kw = sku_ac_kw(opt)
    weight_cap = math.floor(ac_kw * opt["DcAcMax"] / opt["StringKw"])
    assign, total = assign_feeders([(c["number"], c["location"], c["n"]) for c in combiners], inv_nodes,
                                   cap_override=opt["CapPerInverter"], weight_cap=weight_cap,
                                   tail_bias=opt["FeederTailBias"])
    result["total_feeder"] = total
    for c in combiners:
        if c["number"] in assign:
            c["inverter"] = assign[c["number"]]
            result["assignments"].append((c["number"], assign[c["number"]]))
        c["sku"] = smallest_sku(c["n"])
    by_number = {c["number"]: c for c in combiners}
    loads = {}
    for cb, inv in result["assignments"]:
        loads[inv] = loads.get(inv, 0) + by_number[cb]["n"]
    for inv in result["inverters"]:
        inv["load"] = loads.get(inv["number"], 0)
    if opt["Inv2Enabled"] and opt["Inv2AcKw"] > 0:
        for inv in result["inverters"]:
            if inv["load"] <= 0:
                continue
            if inv["load"] * opt["StringKw"] / ac_kw < opt["Inv2Floor"] and \
                    inv["load"] * opt["StringKw"] / opt["Inv2AcKw"] <= opt["DcAcMax"]:
                inv["spec"] = "B"
    result["combiners"] = combiners
    result["lanes"] = lanes
    if opt["RowAwareRouting"] and rows is not None:
        result["homerun_paths"] = build_homerun_paths(combiners, string_pts, rows, s2row)
    result["findings"] = findings(result, inv_nodes, weight_cap, opt)
    result["success"] = True
    return result


def _net(value, digits):
    """String.Format "{0:F<digits>}" on a double (fixed, half away from zero at the shown digits)."""
    from decimal import ROUND_HALF_UP, Decimal
    return str(Decimal(repr(value)).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP))


def _thousands(value):
    """"{0:N0}": thousands separators, no decimals, half away from zero."""
    from decimal import ROUND_HALF_UP, Decimal
    return f"{int(Decimal(repr(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP)):,}"


def findings(result, inv_nodes, weight_cap, opt):
    """BuildFindings (:156-214): the command-line findings, in order."""
    f = []
    ac_kw = sku_ac_kw(opt)
    loaded = [inv for inv in result["inverters"] if inv["load"] > 0]
    if loaded:
        lmin, lmax = min(i["load"] for i in loaded), max(i["load"] for i in loaded)
        dcacs = [i["load"] * opt["StringKw"] / (opt["Inv2AcKw"] if i["spec"] == "B" else ac_kw) for i in loaded]
        f.append(f"strings/inverter {lmin}-{lmax} (DC:AC {_net(min(dcacs), 2)}-{_net(max(dcacs), 2)} at each "
                 "unit's rating)")
        n_b = sum(1 for i in result["inverters"] if i["spec"] == "B")
        if n_b > 0:
            n_a = len(loaded) - n_b
            f.append(f"re-spec'd {n_b} underused inverter(s) (DC:AC < {_net(opt['Inv2Floor'], 2)} at the "
                     f"{_net(ac_kw / 1000.0, 2)} MW rating) to spec-B {_net(opt['Inv2AcKw'] / 1000.0, 2)} MW - fleet: "
                     f"{n_a}x A @ {_net(ac_kw / 1000.0, 2)} MW + {n_b}x B @ {_net(opt['Inv2AcKw'] / 1000.0, 2)} MW.")
        cap_b = math.floor(opt["Inv2AcKw"] * opt["DcAcMax"] / opt["StringKw"])
        over = sum(1 for i in result["inverters"] if i["load"] > (cap_b if i["spec"] == "B" else weight_cap))
        if over > 0:
            f.append(f"WARN: {over} inverter(s) exceed the {weight_cap}-string cap (DC:AC > {_net(opt['DcAcMax'], 2)})"
                     " - not enough inverter capacity where the array is dense.")
        if lmin > 0 and lmax > 2 * lmin:
            f.append(f"WARN: heavy load imbalance ({lmin} vs {lmax} strings) - inverter positions don't match the "
                     "array distribution (LEAFLITEFREE would rebalance).")
        idle = sum(1 for i in result["inverters"] if i["load"] == 0)
        if idle > 0:
            f.append(f"WARN: {idle} inverter(s) received no combiners (idle) - stacked duplicates or outside the "
                     "array.")
    per_inv = {}
    for _, inv in result["assignments"]:
        per_inv[inv] = per_inv.get(inv, 0) + 1
    most = max(per_inv.values(), default=0)
    if most > opt["CapPerInverter"]:
        f.append(f"WARN: an inverter has {most} combiners (DC-input cap {opt['CapPerInverter']}).")
    positions = dict(inv_nodes)
    lengths = sorted(_dist(c["location"], positions[c["inverter"]]) for c in result["combiners"]
                     if c.get("inverter") in positions)
    if lengths:
        median, longest = lengths[len(lengths) // 2], lengths[-1]
        n_long = sum(1 for length in lengths if length > LONG_FEEDER)
        if n_long > 0:
            f.append(f"WARN: {n_long} feeder(s) over 1,800 ft (max {_thousands(longest)} ft, median "
                     f"{_thousands(median)} ft) - distant combiners; consider relocating those inverters "
                     "(LEAFLITEFREE).")
    if len(result["lanes"]) <= 2:
        f.append("no interior land lanes derived - feeders fall back to direct orthogonal runs.")
    return f


def printed(line):
    """ReportFindings (LightweightCablingCommands.cs): a WARN finding prints as "! ...", others as "- ..."."""
    return "! " + line[5:].lstrip() if line.startswith("WARN:") else "- " + line
