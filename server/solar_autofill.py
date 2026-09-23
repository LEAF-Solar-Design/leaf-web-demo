"""Pure port of the plugin's LOCAL auto-fill: OptimalPlanSolver plus SnakePanelSelector.

Standard library only, no I/O, no graph, inputs never mutated. AUTOFILL rebalances
panel-group membership so every group holds a FEASIBLE panel count, k strings of
length n-2..n for the string length n, and it does so with the smallest total
disruption. The Branch2025 C# is the authority; every step names the line it
follows (paths relative to the plugin repo root):

* ``is_feasible_count`` / ``find_nearest_feasible``: PanelGroupTradeCalculator.cs:192-228.
* ``solve_targets``: OptimalPlanSolver.SolveTargets (:151-238) with
  ``_build_spatial_clusters`` (:246-299) and ``_merge_nearest`` (:332-362).
* ``_solve_zone``: the bounded-integer-partition DP (:538-682).
* ``realize_trades``: RealizeTrades (:706-825) with ``_build_adjacency_graph``
  (:829-859), ``_shortest_path`` (:863-903) and its MinHeap (:1019-1056).
* ``select_snake_panels``: SnakePanelSelector.Select (SnakePanelSelector.cs:39-292)
  with ``_select_jumper`` (:299-407) and ``_fallback_closest`` (:409-426).
* ``run_solver``: RunSolver (:977-1015) with EstimatePanelDimensions (:933-964)
  and EstimatePitch (:912-926).

Options. AutoFillOrchestrator.RunGroundMount calls ``OptimalPlanSolver.RunSolver
(simGroups, panelsPerString)`` with no options (AutoFillOrchestrator.cs:62), so
SolveOptions.Default is the only configuration this port reproduces: no drain
threshold or discount, no active-group penalty, no concentration bias, no forced
drain mask, no LexMin or greedy solver, no cluster-size split, and
ClusterMarginPitches 2.0. Every one of those knobs is off in the captured run, so
none of them is ported; a caller that needs one is asking for a path the licensed
command does not take.

Grids. SimGroup.BuildGrid (AutoFillSimulator.cs:108) runs in RunSolver, but the
solver path reads only X, Y and Angle: GridRow and GridCol are consumed by the
heuristic strategies this command does not use. ``row`` and ``col`` are therefore
accepted on a panel, carried through, and never read.

GROUP ORDER IS PART OF THE ANSWER, so the caller owns it. The DP keeps the FIRST
target that reaches a cost (strict ``<`` at OptimalPlanSolver.cs:648) and
backtracks from the LAST group (:675), so on a tie the group order decides which
group donates and which receives. The plugin's order is its scan order: AUTOFILL
selects model space (BranchCmd.cs:3631) and AutoCAD returns a select-all in
database order. A command that rebuilds a group's block reference (RemovePanel,
and AutoFill's own Phase 4) gives it a NEW handle at the END of the database, so
a group an earlier edit touched is scanned LAST. Measured on the 2026-09-23
capture: before AUTOFILL the 71-panel group's block was 9D2F, created by the
REMOVEPANEL that made it, above every original group block 9C93..9CFB; after
AUTOFILL the two rebuilt groups read back as 9D44 and 9D51, again at the end
(receipts/w2-autofill-20260923/FINDING.md and bthost-autofill-after-reopen.jsonl).
Pass groups in that order.

Determinism. Every sort here is stable and every tie-break is the plugin's:
receivers by descending demand (OptimalPlanSolver.cs:750), donors by strict-``<``
shortest path (:761), K nearest neighbours by ascending distance (:842), and the
DP's first-target rule. No dict is iterated in a way that depends on hashing.

Cost. The DP as written in C# is O(N x T^2) over the feasible targets, which is
about 29 million inner steps on the captured 11-group, 2317-panel zone. This port
solves the SAME recurrence inside the band the answer can occupy: every state
satisfies dp[i][s] >= |s - prefix_i| by the triangle inequality, so a state whose
cost is at most B has |s - prefix_i| <= B and every chosen target has
|t - c_i| <= B. Doubling B from 0 and accepting the first run whose optimum is at
most B therefore reproduces the unrestricted DP's value AND its choice on every
state the backtrack visits, while visiting O(N x B^2) states. On the captured case
the optimum is 2, so the DP visits at most 11 x 5 x 5 states instead of 29 million.
When B reaches the zone total the band covers everything and the run is the full
DP, so an unbounded answer is still exact.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

# Bounds: every one of these fails closed rather than allocating on a bad input.
MAX_GROUPS = 4096
MAX_PANELS = 262144
MAX_STRING_LENGTH = 4096
# BuildAdjacencyGraph kNearest (OptimalPlanSolver.cs:745).
K_NEAREST = 8
# SolveOptions.ClusterMarginPitches default (OptimalPlanSolver.cs:71).
CLUSTER_MARGIN_PITCHES = 2.0
# BuildSpatialClusters / EstimatePitch fallback when no group has two panels
# (OptimalPlanSolver.cs:279, :925).
FALLBACK_PITCH = 100.0
# EstimatePanelDimensions neighbour window and gates (OptimalPlanSolver.cs:947-955).
_DIMENSION_WINDOW = 30
_DIMENSION_CROSS_TOL = 5.0
_DIMENSION_MIN_GAP = 1.0
# SnakePanelSelector rotation gate and corner band (SnakePanelSelector.cs:53, :318).
_ROTATION_TOL = 0.001
_CORNER_BAND = 0.6
# SnakePanelSelector column-first gate (SnakePanelSelector.cs:135).
_COLUMN_FIRST_MIN_PANELS = 6
# SelectJumper obliqueness thresholds (SnakePanelSelector.cs:353, :357).
_OBLIQUE_FALLBACK = 0.75
_OBLIQUE_PERPENDICULAR = 0.25


class AutofillError(ValueError):
    """Malformed auto-fill input; raised instead of guessing (fails closed)."""


# ------------------------------------------------------------------ primitives


def is_feasible_count(n: int, total_panels: int) -> bool:
    """PanelGroupTradeCalculator.IsFeasibleCount (PanelGroupTradeCalculator.cs:192-206).

    True when some k >= 1 has k*(n-2) <= total <= k*n, i.e. the count splits into
    strings of n, n-1 and n-2. No allocation, at most total/(n-2) steps.
    """
    if n <= 0 or total_panels <= 0:
        return False
    min_string_len = max(n - 2, 1)
    max_k = total_panels // min_string_len + 1
    for k in range(1, max_k + 1):
        if k * min_string_len <= total_panels <= k * n:
            return True
        if k * min_string_len > total_panels:
            break
    return False


def find_nearest_feasible(n: int, current_count: int) -> int:
    """PanelGroupTradeCalculator.FindNearestFeasible (:214-228): ties prefer the lower count."""
    if n <= 0:
        return current_count
    if is_feasible_count(n, current_count):
        return current_count
    for delta in range(1, n + 1):
        if current_count - delta > 0 and is_feasible_count(n, current_count - delta):
            return current_count - delta
        if is_feasible_count(n, current_count + delta):
            return current_count + delta
    return current_count


def _sq_dist(x1: float, y1: float, x2: float, y2: float) -> float:
    """OptimalPlanSolver.SqDist (:966-970)."""
    dx, dy = x1 - x2, y1 - y2
    return dx * dx + dy * dy


class _Panel:
    """AutoFillSimulator.SimPanel (AutoFillSimulator.cs:16-34); x and y are mutable
    because SnakePanelSelector un-rotates the working set in place and restores it."""

    __slots__ = ("id", "x", "y", "angle", "row", "col")

    def __init__(self, panel_id: str, x: float, y: float, angle: float, row, col):
        self.id = panel_id
        self.x = x
        self.y = y
        self.angle = angle
        self.row = row
        self.col = col


class _Group:
    """AutoFillSimulator.SimGroup (AutoFillSimulator.cs:58-102), solver fields only."""

    __slots__ = ("id", "zone", "panels", "cx", "cy")

    def __init__(self, group_id: str, zone: str, panels: list):
        self.id = group_id
        self.zone = zone
        self.panels = panels
        # BranchCmd.cs:3723-3724 seeds the centroid from the members, 0 when empty.
        self.cx = sum(p.x for p in panels) / len(panels) if panels else 0.0
        self.cy = sum(p.y for p in panels) / len(panels) if panels else 0.0

    @property
    def count(self) -> int:
        return len(self.panels)

    def recalc_centroid(self) -> None:
        """SimGroup.RecalcCentroid (:95-102): an emptied group KEEPS its last centroid."""
        if self.panels:
            self.cx = sum(p.x for p in self.panels) / len(self.panels)
            self.cy = sum(p.y for p in self.panels) / len(self.panels)


def _same_zone(a: _Group, b: _Group) -> bool:
    """OptimalPlanSolver.SameZone (:905-907), on the caller's already-joined key."""
    return a.zone == b.zone


# ------------------------------------------------------------------ input read


def _finite(value: Any, label: str) -> float:
    if type(value) not in (int, float) or isinstance(value, bool) or not math.isfinite(value):
        raise AutofillError(f"{label} must be a finite number")
    return float(value)


def _read_groups(groups: Sequence[Mapping[str, Any]]) -> list:
    """Private mutable groups from the caller's mapping; the input is never touched."""
    if isinstance(groups, (str, bytes, Mapping)) or not isinstance(groups, Sequence):
        raise AutofillError("groups must be a sequence of group mappings")
    if len(groups) > MAX_GROUPS:
        raise AutofillError("too many groups")
    built, seen_groups, seen_panels, total = [], set(), set(), 0
    for source in groups:
        if not isinstance(source, Mapping):
            raise AutofillError("each group must be a mapping")
        group_id = source.get("id")
        if not isinstance(group_id, str) or not group_id:
            raise AutofillError("group id must be a nonempty string")
        if group_id in seen_groups:
            raise AutofillError("group ids must be unique")
        seen_groups.add(group_id)
        zone = source.get("zone", "")
        if not isinstance(zone, str):
            raise AutofillError("group zone must be a string")
        members = source.get("panels")
        if isinstance(members, (str, bytes, Mapping)) or not isinstance(members, Sequence):
            raise AutofillError("group panels must be a sequence")
        total += len(members)
        if total > MAX_PANELS:
            raise AutofillError("too many panels")
        panels = []
        for member in members:
            if not isinstance(member, Mapping):
                raise AutofillError("each panel must be a mapping")
            panel_id = member.get("id")
            if not isinstance(panel_id, str) or not panel_id:
                raise AutofillError("panel id must be a nonempty string")
            if panel_id in seen_panels:
                raise AutofillError("a panel may belong to only one group")
            seen_panels.add(panel_id)
            row, col = member.get("row"), member.get("col")
            for label, index in (("panel row", row), ("panel col", col)):
                if index is not None and (type(index) is not int or isinstance(index, bool)):
                    raise AutofillError(f"{label} must be an integer or absent")
            panels.append(_Panel(panel_id, _finite(member.get("x"), "panel x"),
                                 _finite(member.get("y"), "panel y"),
                                 _finite(member.get("angle", 0.0), "panel angle"), row, col))
        built.append(_Group(group_id, zone, panels))
    return built


# ----------------------------------------------------------------- clustering


def _estimate_pitch(groups: Sequence[_Group]) -> float:
    """OptimalPlanSolver.EstimatePitch (:912-926): mean span over sqrt(count)."""
    pitch_sum, pitch_count = 0.0, 0
    for group in groups:
        panels = group.panels
        if len(panels) <= 1:
            continue
        span = max(max(p.x for p in panels) - min(p.x for p in panels),
                   max(p.y for p in panels) - min(p.y for p in panels))
        pitch_sum += span / max(1.0, math.sqrt(len(panels)))
        pitch_count += 1
    return pitch_sum / pitch_count if pitch_count else FALLBACK_PITCH


def _build_spatial_clusters(groups: Sequence[_Group], margin_pitches: float) -> list:
    """BuildSpatialClusters (:249-299): single-linkage union-find on grown bounding boxes.

    Cluster lists keep the caller's group order, exactly as the C# byRoot dictionary
    does (it is filled by ascending index and never has an entry removed), because
    that order is what the DP's tie-break reads.
    """
    n = len(groups)
    if n <= 1:
        return [list(groups)]
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    bbox = [None] * n
    pitch_sum, pitch_count = 0.0, 0
    for i in range(n):
        panels = groups[i].panels
        if not panels:
            bbox[i] = (groups[i].cx, groups[i].cy, groups[i].cx, groups[i].cy)
            continue
        mnx = min(p.x for p in panels)
        mxx = max(p.x for p in panels)
        mny = min(p.y for p in panels)
        mxy = max(p.y for p in panels)
        # The C# names this tuple (minX, minY, maxX, maxY) and fills it in that order.
        bbox[i] = (mnx, mny, mxx, mxy)
        if len(panels) > 1:
            span = max(mxx - mnx, mxy - mny)
            pitch_sum += span / max(1.0, math.sqrt(len(panels)))
            pitch_count += 1
    pitch = pitch_sum / pitch_count if pitch_count else FALLBACK_PITCH
    margin = pitch * margin_pitches

    for i in range(n):
        for j in range(i + 1, n):
            a, b = bbox[i], bbox[j]
            x_overlap = a[0] - margin <= b[2] and b[0] - margin <= a[2]
            y_overlap = a[1] - margin <= b[3] and b[1] - margin <= a[3]
            if x_overlap and y_overlap:
                union(i, j)

    by_root: dict[int, list] = {}
    for i in range(n):
        by_root.setdefault(find(i), []).append(groups[i])
    return list(by_root.values())


def _merge_nearest(clusters: list, infeasibles: list) -> list:
    """MergeNearest (:332-362): fold each infeasible cluster into its nearest sibling."""
    cluster_list = [list(c) for c in clusters]
    for inf_cluster in infeasibles:
        self_idx = next(
            (i for i, c in enumerate(cluster_list)
             if len(c) == len(inf_cluster) and c and inf_cluster and c[0].id == inf_cluster[0].id), -1)
        if self_idx < 0:
            continue
        self_cx = sum(g.cx for g in cluster_list[self_idx]) / len(cluster_list[self_idx])
        self_cy = sum(g.cy for g in cluster_list[self_idx]) / len(cluster_list[self_idx])
        best_other, best_dist = -1, math.inf
        for i, other in enumerate(cluster_list):
            if i == self_idx:
                continue
            cx = sum(g.cx for g in other) / len(other)
            cy = sum(g.cy for g in other) / len(other)
            d = _sq_dist(cx, cy, self_cx, self_cy)
            if d < best_dist:
                best_dist, best_other = d, i
        if best_other < 0:
            continue
        cluster_list[best_other].extend(cluster_list[self_idx])
        cluster_list.pop(self_idx)
    return cluster_list


# ------------------------------------------------------------------- the DP


def _banded_dp(counts: Sequence[int], prefix: Sequence[int], total: int, n: int, bound: int):
    """The SolveZone recurrence restricted to the cost band `bound` (module docstring).

    Returns ``(targets, cost)`` when the optimum is at most `bound`, else None. The
    target loop runs ASCENDING and an update is strict, so the first target reaching
    a cost wins exactly as at OptimalPlanSolver.cs:643-653.
    """
    count = len(counts)
    cost_row: dict[int, int] = {0: 0}
    choices: list[dict[int, int]] = []
    for i in range(1, count + 1):
        current = counts[i - 1]
        low = max(0, prefix[i] - bound)
        high = min(total, prefix[i] + bound)
        # feasibleTargets is [0] then ascending feasible counts (:555-558), clipped
        # to the band: a target outside it costs more than `bound` on its own.
        targets = [0] if current <= bound else []
        targets.extend(t for t in range(max(1, current - bound), min(total, current + bound) + 1)
                       if is_feasible_count(n, t))
        row: dict[int, int] = {}
        pick: dict[int, int] = {}
        for t in targets:
            step = abs(t - current)
            for previous_sum, previous_cost in cost_row.items():
                s = previous_sum + t
                if s < low or s > high:
                    continue
                new_cost = previous_cost + step
                if new_cost < row.get(s, new_cost + 1):
                    row[s] = new_cost
                    pick[s] = t
        if not row:
            return None
        cost_row = row
        choices.append(pick)
    final = cost_row.get(total)
    if final is None or final > bound:
        return None
    targets_out = [0] * count
    remaining = total
    for i in range(count, 0, -1):
        chosen = choices[i - 1].get(remaining)
        if chosen is None:
            # Unreachable: every state on the backtrack path costs at most `final`.
            raise AutofillError("auto-fill DP lost its backtrack state")
        targets_out[i - 1] = chosen
        remaining -= chosen
    return targets_out, final


def _solve_zone(groups: Sequence[_Group], total: int, n: int) -> dict:
    """SolveZone (:538-682) for one cluster: minimum-disruption feasible partition."""
    count = len(groups)
    if count == 0 or total == 0:
        return {"feasible": True, "targets": {g.id: 0 for g in groups},
                "disruption": 0, "diagnostics": []}
    counts = [g.count for g in groups]
    prefix = [0] * (count + 1)
    for i, c in enumerate(counts):
        prefix[i + 1] = prefix[i] + c
    bound = 0
    while True:
        solved = _banded_dp(counts, prefix, total, n, bound)
        if solved is not None:
            targets, cost = solved
            return {"feasible": True,
                    "targets": {groups[i].id: targets[i] for i in range(count)},
                    "disruption": cost, "diagnostics": []}
        if bound >= total:
            break
        bound = 1 if bound == 0 else min(bound * 2, total)
    # No feasible partition exists for this cluster's total: assign each group its
    # nearest feasible count and let the caller report it (:658-667).
    return {"feasible": False,
            "targets": {g.id: find_nearest_feasible(n, g.count) for g in groups},
            "disruption": 0,
            "diagnostics": [f"Zone total {total} cannot be partitioned into feasible counts "
                            f"for n={n} across {count} groups. Assigning nearest feasible "
                            f"per group (non-conserving)."]}


def solve_targets(groups: Sequence[_Group], panels_per_string: int) -> dict:
    """SolveTargets (:154-238): per zone, per spatial cluster, merging on infeasibility."""
    result = {"feasible": True, "targets": {}, "disruption": 0, "diagnostics": []}
    if not groups:
        return result
    # LINQ GroupBy keeps first-appearance key order and source order inside a key.
    zones: dict[str, list] = {}
    for group in groups:
        zones.setdefault(group.zone, []).append(group)
    for zone_groups in zones.values():
        clusters = _build_spatial_clusters(zone_groups, CLUSTER_MARGIN_PITCHES)
        safety = len(zone_groups) + 2
        solved = None
        while safety > 0:
            safety -= 1
            attempts = [(cluster, _solve_zone(cluster, sum(g.count for g in cluster),
                                              panels_per_string))
                        for cluster in clusters]
            infeasibles = [cluster for cluster, attempt in attempts if not attempt["feasible"]]
            if not infeasibles:
                solved = attempts
                break
            if len(clusters) == 1:
                zone_total = sum(g.count for g in zone_groups)
                result["feasible"] = False
                result["diagnostics"].append(
                    f"Zone total {zone_total} has no feasible partition across "
                    f"{len(zone_groups)} groups for n={panels_per_string}.")
                for group in zone_groups:
                    result["targets"][group.id] = find_nearest_feasible(
                        panels_per_string, group.count)
                break
            clusters = _merge_nearest(clusters, infeasibles)
        if solved is not None:
            for _, attempt in solved:
                result["targets"].update(attempt["targets"])
                result["disruption"] += attempt["disruption"]
            if len(clusters) > 1:
                result["diagnostics"].append(
                    f"Zone solved as {len(clusters)} spatial cluster(s); "
                    f"trades confined within each cluster.")
    return result


# ------------------------------------------------------- panel dimensions, graph


def estimate_panel_dimensions(groups: Sequence[_Group]) -> tuple:
    """EstimatePanelDimensions (:933-964): the smallest same-row and same-column gaps.

    Bounded: each group is sorted once and each panel looks at the next 29 panels,
    so this is O(P log P) over the drawing, never a pairwise scan.
    """
    h_pitch = math.inf
    v_pitch = math.inf
    for group in groups:
        panels = group.panels
        if len(panels) < 2:
            continue
        ordered = sorted(panels, key=lambda p: (p.x, p.y))
        for i in range(len(ordered)):
            for j in range(i + 1, min(len(ordered), i + _DIMENSION_WINDOW)):
                dx = abs(ordered[j].x - ordered[i].x)
                dy = abs(ordered[j].y - ordered[i].y)
                if dy < _DIMENSION_CROSS_TOL and dx > _DIMENSION_MIN_GAP and dx < h_pitch:
                    h_pitch = dx
                if dx < _DIMENSION_CROSS_TOL and dy > _DIMENSION_MIN_GAP and dy < v_pitch:
                    v_pitch = dy
    fallback = _estimate_pitch(groups)
    if h_pitch == math.inf:
        h_pitch = fallback
    if v_pitch == math.inf:
        v_pitch = fallback
    return h_pitch, v_pitch


def _build_adjacency_graph(groups: Sequence[_Group], k_nearest: int) -> dict:
    """BuildAdjacencyGraph (:829-859): K nearest same-zone neighbours, symmetrised."""
    adjacency: dict[str, list] = {}
    for a in groups:
        neighbours = sorted(
            ((b, math.sqrt(_sq_dist(a.cx, a.cy, b.cx, b.cy))) for b in groups
             if b is not a and _same_zone(a, b)),
            key=lambda pair: pair[1])[:k_nearest]
        adjacency[a.id] = [(b.id, dist) for b, dist in neighbours]
    for a in groups:
        for to_id, dist in list(adjacency[a.id]):
            if not any(other == a.id for other, _ in adjacency[to_id]):
                adjacency[to_id].append((a.id, dist))
    return adjacency


class _MinHeap:
    """The plugin's own binary min-heap (OptimalPlanSolver.cs:1019-1056).

    Ported rather than replaced by heapq because the dequeue order of EQUAL
    priorities is part of Dijkstra's answer here, and heapq breaks those ties
    differently.
    """

    __slots__ = ("_h",)

    def __init__(self):
        self._h: list = []

    def __len__(self) -> int:
        return len(self._h)

    def enqueue(self, element, priority: float) -> None:
        self._h.append((element, priority))
        i = len(self._h) - 1
        while i > 0:
            parent = (i - 1) >> 1
            if self._h[parent][1] <= self._h[i][1]:
                break
            self._h[parent], self._h[i] = self._h[i], self._h[parent]
            i = parent

    def dequeue(self):
        top = self._h[0][0]
        last = len(self._h) - 1
        self._h[0] = self._h[last]
        self._h.pop()
        last -= 1
        i = 0
        while True:
            left, right, smallest = 2 * i + 1, 2 * i + 2, i
            if left <= last and self._h[left][1] < self._h[smallest][1]:
                smallest = left
            if right <= last and self._h[right][1] < self._h[smallest][1]:
                smallest = right
            if smallest == i:
                break
            self._h[smallest], self._h[i] = self._h[i], self._h[smallest]
            i = smallest
        return top


def _shortest_path(adjacency: dict, start_id: str, end_id: str) -> tuple:
    """ShortestPath (:863-903): Dijkstra, returning (handle path, length) or (None, inf)."""
    if start_id == end_id:
        return [start_id], 0.0
    dist = {start_id: 0.0}
    previous: dict[str, str] = {}
    queue = _MinHeap()
    queue.enqueue(start_id, 0.0)
    visited: set[str] = set()
    while len(queue) > 0:
        u = queue.dequeue()
        if u in visited:
            continue
        visited.add(u)
        if u == end_id:
            break
        edges = adjacency.get(u)
        if edges is None:
            continue
        du = dist[u]
        for v, w in edges:
            if v in visited:
                continue
            alt = du + w
            if v not in dist or alt < dist[v]:
                dist[v] = alt
                previous[v] = u
                queue.enqueue(v, alt)
    if end_id not in dist:
        return None, math.inf
    path = [end_id]
    while path[-1] != start_id:
        step = previous.get(path[-1])
        if step is None:
            return None, math.inf
        path.append(step)
    path.reverse()
    return path, dist[end_id]


# ------------------------------------------------------------- panel selection


def _fallback_closest(donor_panels: Sequence[_Panel], receiver_panels: Sequence[_Panel]) -> _Panel:
    """SnakePanelSelector.FallbackClosest (:409-426): first donor panel wins a tie."""
    best = donor_panels[0]
    best_sq = math.inf
    for p in donor_panels:
        min_sq = math.inf
        for r in receiver_panels:
            d = _sq_dist(p.x, p.y, r.x, r.y)
            if d < min_sq:
                min_sq = d
        if min_sq < best_sq:
            best_sq = min_sq
            best = p
    return best


def _select_jumper(donor_panels: Sequence[_Panel], receiver_panels: Sequence[_Panel],
                   pw: float, ph: float) -> _Panel:
    """SelectJumper (SnakePanelSelector.cs:299-407): the donor panel that bridges to the receiver."""
    src_cx = sum(p.x for p in donor_panels) / len(donor_panels)
    src_cy = sum(p.y for p in donor_panels) / len(donor_panels)
    dst_cx = sum(p.x for p in receiver_panels) / len(receiver_panels)
    dst_cy = sum(p.y for p in receiver_panels) / len(receiver_panels)

    s_min_x = min(p.x for p in donor_panels)
    s_max_x = max(p.x for p in donor_panels)
    s_min_y = min(p.y for p in donor_panels)
    s_max_y = max(p.y for p in donor_panels)
    dx, dy = dst_cx - src_cx, dst_cy - src_cy

    if abs(dx) > abs(dy):
        target = s_max_x if dx > 0 else s_min_x
        edge = [p for p in donor_panels if abs(p.x - target) < pw * _CORNER_BAND]
    else:
        target = s_max_y if dy > 0 else s_min_y
        edge = [p for p in donor_panels if abs(p.y - target) < ph * _CORNER_BAND]

    if not edge:
        return _fallback_closest(donor_panels, receiver_panels)

    if abs(dx) > abs(dy):
        e_min_y = min(p.y for p in edge)
        e_max_y = max(p.y for p in edge)
        edge_corners = [p for p in edge
                        if abs(p.y - e_min_y) < ph * _CORNER_BAND
                        or abs(p.y - e_max_y) < ph * _CORNER_BAND]
    else:
        e_min_x = min(p.x for p in edge)
        e_max_x = max(p.x for p in edge)
        edge_corners = [p for p in edge
                        if abs(p.x - e_min_x) < pw * _CORNER_BAND
                        or abs(p.x - e_max_x) < pw * _CORNER_BAND]
    if not edge_corners:
        edge_corners = edge

    d_min_x = min(p.x for p in receiver_panels)
    d_max_x = max(p.x for p in receiver_panels)
    d_min_y = min(p.y for p in receiver_panels)
    d_max_y = max(p.y for p in receiver_panels)

    primary = max(abs(dx), abs(dy))
    cross_axis = min(abs(dx), abs(dy))
    obliqueness = cross_axis / primary if primary > 0 else 0.0

    if obliqueness >= _OBLIQUE_FALLBACK:
        return _fallback_closest(donor_panels, receiver_panels)

    if obliqueness < _OBLIQUE_PERPENDICULAR:
        # Near-perpendicular: the receiver's donor-facing edge corners.
        if abs(dx) > abs(dy):
            r_target = d_min_x if dx > 0 else d_max_x
            r_edge = [p for p in receiver_panels if abs(p.x - r_target) < pw * _CORNER_BAND]
        else:
            r_target = d_min_y if dy > 0 else d_max_y
            r_edge = [p for p in receiver_panels if abs(p.y - r_target) < ph * _CORNER_BAND]
        if not r_edge:
            r_edge = list(receiver_panels)
        if abs(dx) > abs(dy):
            re_min_y = min(p.y for p in r_edge)
            re_max_y = max(p.y for p in r_edge)
            targets = [p for p in r_edge
                       if abs(p.y - re_min_y) < ph * _CORNER_BAND
                       or abs(p.y - re_max_y) < ph * _CORNER_BAND]
        else:
            re_min_x = min(p.x for p in r_edge)
            re_max_x = max(p.x for p in r_edge)
            targets = [p for p in r_edge
                       if abs(p.x - re_min_x) < pw * _CORNER_BAND
                       or abs(p.x - re_max_x) < pw * _CORNER_BAND]
        if not targets:
            targets = r_edge
    else:
        # Moderate obliqueness: the receiver's bounding-box corners.
        targets = [p for p in receiver_panels
                   if (abs(p.x - d_min_x) < pw * _CORNER_BAND
                       or abs(p.x - d_max_x) < pw * _CORNER_BAND)
                   and (abs(p.y - d_min_y) < ph * _CORNER_BAND
                        or abs(p.y - d_max_y) < ph * _CORNER_BAND)]
        if not targets:
            targets = list(receiver_panels)

    best = edge_corners[0]
    best_dist = math.inf
    for ec in edge_corners:
        for t in targets:
            d = _sq_dist(ec.x, ec.y, t.x, t.y)
            if d < best_dist:
                best_dist = d
                best = ec
    return best


def select_snake_panels(donor_panels: Sequence[_Panel], receiver_panels, count: int,
                        panel_width: float, panel_height: float) -> list:
    """SnakePanelSelector.Select (:39-292): jumper first, then a serpentine walk.

    The result is result[0] = jumper and result[1:] = the snake. It returns fewer
    than `count` only when the donor has fewer panels.
    """
    result: list = []
    if not donor_panels or count <= 0:
        return result
    if panel_width <= 0:
        panel_width = 1.0
    if panel_height <= 0:
        panel_height = 1.0
    desired_count = min(count, len(donor_panels))

    angle = donor_panels[0].angle
    rotated = abs(angle) > _ROTATION_TOL
    saved = None
    if rotated:
        # The walk is grid-shaped, so a rotated array is un-rotated about the
        # combined centroid, walked, and put back (:54-71, :277-289).
        all_panels = list(donor_panels)
        if receiver_panels:
            all_panels.extend(receiver_panels)
        cx = sum(p.x for p in all_panels) / len(all_panels)
        cy = sum(p.y for p in all_panels) / len(all_panels)
        cos, sin = math.cos(-angle), math.sin(-angle)
        saved = {}
        for p in all_panels:
            saved[p.id] = (p.x, p.y)
            dx, dy = p.x - cx, p.y - cy
            p.x = cx + dx * cos - dy * sin
            p.y = cy + dx * sin + dy * cos

    try:
        if receiver_panels:
            jumper = _select_jumper(donor_panels, receiver_panels, panel_width, panel_height)
        else:
            cx = sum(p.x for p in donor_panels) / len(donor_panels)
            cy = sum(p.y for p in donor_panels) / len(donor_panels)
            jumper = min(donor_panels, key=lambda p: _sq_dist(p.x, p.y, cx, cy))

        # ALWAYS row = Y, col = X (:88-98). C# Math.Round and Python round both
        # break a .5 tie to even, so the bucketing matches cell for cell.
        min_x = min(p.x for p in donor_panels)
        min_y = min(p.y for p in donor_panels)

        def cell_of(p: _Panel) -> tuple:
            return (int(round((p.y - min_y) / panel_height)),
                    int(round((p.x - min_x) / panel_width)))

        grid: dict[tuple, _Panel] = {}
        for p in donor_panels:
            grid[cell_of(p)] = p
        j_row, j_col = cell_of(jumper)

        row_step = 1
        if receiver_panels:
            nearest_r = min(receiver_panels,
                            key=lambda r: _sq_dist(r.x, r.y, jumper.x, jumper.y))
            row_step = 1 if jumper.y >= nearest_r.y else -1

        cols_in_j_row = sorted(col for row, col in grid if row == j_row)
        left_cols = [c for c in cols_in_j_row if c < j_col]
        right_cols = [c for c in cols_in_j_row if c > j_col]
        start_dir = 1 if len(right_cols) >= len(left_cols) else -1

        all_cols = {col for _, col in grid}
        all_rows = {row for row, _ in grid}
        at_row_edge = j_row == min(all_rows) or j_row == max(all_rows)
        at_col_edge = j_col == min(cols_in_j_row) or j_col == max(cols_in_j_row)
        start_panels = len(right_cols) if start_dir > 0 else len(left_cols)
        nr_col = j_col
        if receiver_panels:
            nearest_r = min(receiver_panels,
                            key=lambda r: _sq_dist(r.x, r.y, jumper.x, jumper.y))
            nr_col = int(round((nearest_r.x - min_x) / panel_width))
        recv_outside_grid = nr_col < min(all_cols) or nr_col > max(all_cols)
        col_first = (at_row_edge and at_col_edge
                     and start_panels >= _COLUMN_FIRST_MIN_PANELS and recv_outside_grid)

        result.append(jumper)
        moved = {jumper.id}

        def append_row(row: int, cols_order) -> None:
            for c in cols_order:
                if len(result) >= count:
                    return
                p = grid.get((row, c))
                if p is not None and p.id not in moved:
                    result.append(p)
                    moved.add(p.id)

        if row_step > 0:
            rows_away = sorted(r for r in all_rows if r > j_row)
            rows_toward = sorted((r for r in all_rows if r < j_row), reverse=True)
        else:
            rows_away = sorted((r for r in all_rows if r < j_row), reverse=True)
            rows_toward = sorted(r for r in all_rows if r > j_row)

        if col_first:
            if rows_away:
                p = grid.get((rows_away[0], j_col))
                if p is not None and p.id not in moved:
                    result.append(p)
                    moved.add(p.id)
            col_dir = -1 if j_col == max(all_cols) else 1
            direction = col_dir
            for row in rows_away:
                if len(result) >= count:
                    break
                cols = sorted((col for r, col in grid if r == row),
                              key=lambda c: -c if direction < 0 else c)
                append_row(row, cols)
                direction = -direction
            if len(result) < count:
                if start_dir > 0:
                    append_row(j_row, right_cols)
                else:
                    append_row(j_row, reversed(left_cols))
                if len(result) < count:
                    if start_dir > 0:
                        append_row(j_row, reversed(left_cols))
                    else:
                        append_row(j_row, right_cols)
            if len(result) < count:
                direction = col_dir
                for row in rows_toward:
                    if len(result) >= count:
                        break
                    cols = sorted((col for r, col in grid if r == row),
                                  key=lambda c: -c if direction < 0 else c)
                    append_row(row, cols)
                    direction = -direction
        else:
            if start_dir > 0:
                append_row(j_row, right_cols)
            else:
                append_row(j_row, reversed(left_cols))

            direction = -start_dir
            for row in rows_away:
                if len(result) >= count:
                    break
                cols = sorted((col for r, col in grid if r == row),
                              key=lambda c: -c if direction < 0 else c)
                append_row(row, cols)
                direction = -direction

            if len(result) < count:
                if start_dir > 0:
                    append_row(j_row, reversed(left_cols))
                else:
                    append_row(j_row, right_cols)

            if len(result) < count:
                direction = -start_dir
                for row in rows_toward:
                    if len(result) >= count:
                        break
                    cols = sorted((col for r, col in grid if r == row),
                                  key=lambda c: -c if direction < 0 else c)
                    append_row(row, cols)
                    direction = -direction

        if len(result) < desired_count:
            # Never under-fill a move the donor can satisfy (:260-272).
            short = desired_count - len(result)
            for p in sorted((p for p in donor_panels if p.id not in moved),
                            key=lambda p: _sq_dist(p.x, p.y, jumper.x, jumper.y))[:short]:
                result.append(p)
                moved.add(p.id)
        if len(result) > desired_count:
            result = result[:desired_count]
    finally:
        if saved is not None:
            all_panels = list(donor_panels)
            if receiver_panels:
                all_panels.extend(receiver_panels)
            for p in all_panels:
                original = saved.get(p.id)
                if original is not None:
                    p.x, p.y = original
    return result


# ------------------------------------------------------------------- the trades


def realize_trades(groups: Sequence[_Group], targets: Mapping[str, int],
                   panel_width: float, panel_height: float) -> list:
    """RealizeTrades (:723-825): one local correction per edge of each donor-receiver chain.

    Mutates the groups in place, exactly as the C# does, and returns the corrections
    in the order the plugin makes them.
    """
    use_snake = panel_width > 0 and panel_height > 0
    corrections: list = []
    supply: dict[str, int] = {}
    demand: dict[str, int] = {}
    donors: list = []
    receivers: list = []
    for g in groups:
        target = targets.get(g.id, g.count)
        delta = target - g.count
        if delta < 0:
            donors.append(g)
            supply[g.id] = -delta
        elif delta > 0:
            receivers.append(g)
            demand[g.id] = delta

    by_id = {g.id: g for g in groups}
    adjacency = _build_adjacency_graph(groups, K_NEAREST)

    # OrderByDescending is stable, so equal demands keep the caller's group order.
    for recv in sorted(receivers, key=lambda r: -demand[r.id]):
        while demand[recv.id] > 0:
            best_donor = None
            best_path = None
            best_len = math.inf
            for d in donors:
                if supply[d.id] <= 0 or not _same_zone(d, recv):
                    continue
                path, length = _shortest_path(adjacency, d.id, recv.id)
                if path is None:
                    continue
                if length < best_len:
                    best_len, best_donor, best_path = length, d, path
            if best_donor is None:
                break

            transfer = min(supply[best_donor.id], demand[recv.id])
            transfer = min(transfer, len(best_donor.panels))
            if transfer <= 0:
                break

            for i in range(len(best_path) - 1):
                u = by_id[best_path[i]]
                v = by_id[best_path[i + 1]]
                moving = min(transfer, len(u.panels))
                if moving <= 0:
                    break
                if use_snake:
                    panels_to_move = select_snake_panels(u.panels, v.panels, moving,
                                                         panel_width, panel_height)
                    if len(panels_to_move) < moving:
                        already = {p.id for p in panels_to_move}
                        panels_to_move.extend(
                            sorted((p for p in u.panels if p.id not in already),
                                   key=lambda p: _sq_dist(p.x, p.y, v.cx, v.cy))
                            [:moving - len(panels_to_move)])
                else:
                    panels_to_move = sorted(
                        u.panels, key=lambda p: _sq_dist(p.x, p.y, v.cx, v.cy))[:moving]
                move_set = {p.id for p in panels_to_move}
                u.panels = [p for p in u.panels if p.id not in move_set]
                v.panels.extend(panels_to_move)
                u.recalc_centroid()
                v.recalc_centroid()

                # The distance the plugin reports is measured AFTER both centroids move.
                edge_dist = math.sqrt(_sq_dist(u.cx, u.cy, v.cx, v.cy))
                chain = ("direct" if len(best_path) == 2
                         else f"hop {i + 1}/{len(best_path) - 1} of chain "
                              f"{best_donor.id}->{recv.id}")
                corrections.append({
                    "from": u.id, "to": v.id,
                    "panels": [p.id for p in panels_to_move],
                    "distance": edge_dist, "chain": chain,
                })

            supply[best_donor.id] -= transfer
            demand[recv.id] -= transfer

    return corrections


def run_solver(groups: Sequence[_Group], panels_per_string: int) -> dict:
    """RunSolver (:981-1015): solve targets, realize trades, report the plan.

    BuildGrid (:987) is deliberately not ported: GridRow and GridCol are read only
    by the heuristic strategies AUTOFILL does not run on this path.
    """
    solver = solve_targets(groups, panels_per_string)
    panel_width, panel_height = estimate_panel_dimensions(groups)
    corrections = realize_trades(groups, solver["targets"], panel_width, panel_height)
    violations = list(solver["diagnostics"])
    valid = True
    for g in groups:
        if g.count > 0 and not is_feasible_count(panels_per_string, g.count):
            valid = False
            violations.append(f"Group {g.id} count {g.count} infeasible after solver.")
    return {
        "phase": "OptimalSolver", "valid": valid, "violations": violations,
        "targets": dict(solver["targets"]), "feasible": solver["feasible"],
        "disruption": solver["disruption"], "corrections": corrections,
        "total_moved": sum(len(c["panels"]) for c in corrections),
        "panel_width": panel_width, "panel_height": panel_height,
        "membership": {g.id: [p.id for p in g.panels] for g in groups},
        "counts": {g.id: g.count for g in groups},
    }


def autofill(groups: Sequence[Mapping[str, Any]], panels_per_string: int) -> dict:
    """The LOCAL auto-fill answer for one drawing: corrections in plugin order.

    `groups` is ``[{"id", "zone", "panels": [{"id", "x", "y", "angle", "row", "col"}]}]``
    in the plugin's SCAN ORDER (module docstring), and `panels_per_string` is
    ``mSettings.NumPanelsInSequence`` (BranchCmd.cs:3617). Pure: the caller's
    mappings are never mutated.
    """
    if type(panels_per_string) is not int or isinstance(panels_per_string, bool) \
            or not 1 <= panels_per_string <= MAX_STRING_LENGTH:
        raise AutofillError("panels per string must be a positive integer")
    return run_solver(_read_groups(groups), panels_per_string)
