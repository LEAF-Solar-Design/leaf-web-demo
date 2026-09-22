"""Pure standard-library port of Branch2025's PrepareIndicesForRetry piece generation.

Grids use the plugin's Sequences, Rows/Panels and optional RowIndices fields.
No solving, restitching, I/O, or fixture-dependent behavior occurs here. Enumeration
order, retained blank rows, and the different cropping rules are intentional.
"""
from collections import Counter, deque
from copy import deepcopy
from itertools import combinations, islice, product
from math import ceil, log2, sqrt


STRING_DIRS = ((0, 1), (0, -1), (1, 0), (-1, 0))
CONNECTIVITY_DIRS = STRING_DIRS + ((1, 1), (1, -1), (-1, 1), (-1, -1))
SPLIT_THRESHOLD = 200
MAX_DIMENSION_THRESHOLD = 30
MIN_PANELS_PER_PIECE = 10
MAX_PIECES_WITH_JOGS = 5


def panel_positions(grid):
    """ExtractPanels, GridSplitter.cs; insertion order is row-major."""
    return {(r, c): cell for r, row in enumerate(grid.get("Rows", []))
            for c, cell in enumerate(row.get("Panels") or []) if cell["Code"] == 1}


def count_panels(grid):
    """CountPanels, GridSplitter.cs."""
    return len(panel_positions(grid))


def count_non_empty_rows(grid):
    """CountNonEmptyRows, GridSplitHelper.cs."""
    return len({r for r, c in panel_positions(grid)})


def count_non_empty_columns(grid):
    """CountNonEmptyColumns, GridSplitHelper.cs."""
    return len({c for r, c in panel_positions(grid)})


def exceeds_dimension_limit(grid):
    """ExceedsDimensionLimit, GridSplitHelper.cs."""
    return max(count_non_empty_rows(grid), count_non_empty_columns(grid)) > 30


def needs_splitting(grid):
    """NeedsSplitting, GridSplitHelper.cs; uses raw rows and first-row width."""
    rows = grid.get("Rows") or []
    return (count_panels(grid) >= 200 or len(rows) > 30
            or bool(rows and len(rows[0].get("Panels") or []) > 30))


def get_max_pieces_for_panel_count(panel_count):
    """GetMaxPiecesForPanelCount, GridSplitHelper.cs."""
    return 1 if panel_count < 100 else 2 if panel_count < 200 else 4 if panel_count < 400 else 8


def compute_sequences_for_panel_count(panel_count, preferred_string_len):
    """ComputeSequencesForPanelCount, GridSplitter.cs (balanced, not greedy)."""
    preferred = preferred_string_len if preferred_string_len > 0 else 15
    if panel_count <= 0:
        return [preferred, preferred - 1, 0, 0]
    num_strings = max(1, ceil(panel_count / preferred))
    first = ceil(panel_count / num_strings)
    second = first - 1
    if second <= 0:
        return [first, first, num_strings, 0]
    first_qty = panel_count - num_strings * second
    return [first, second, first_qty, num_strings - first_qty]


def expand_flat_sequences(sequences):
    """ExpandFlatSequences, GridSplitter.cs."""
    if sequences is None or len(sequences) < 4:
        return []
    a, b, qa, qb = sequences[:4]
    return [a] * max(0, qa) + [b] * max(0, qb)


def sizes_to_flat_sequences(sizes):
    """SizesToFlatSequences, GridSplitter.cs."""
    counts = Counter(sizes)
    lengths = sorted(counts, reverse=True)
    if not lengths:
        return [0, 0, 0, 0]
    a = lengths[0]
    if len(lengths) == 1:
        return [a, a - 1, counts[a], 0]
    b = lengths[1]
    return [a, b, counts[a], counts[b]]


def find_size_partitions(sizes, target_a, target_b):
    """FindSizePartitions/FindSizePartitionsStatic, GridSplitter.cs; stable DFS order."""
    sizes = sorted(sizes, reverse=True)
    subsets = []

    def visit(index, current, total):
        if total == target_a:
            subsets.append(current[:])
            return
        if total > target_a or index >= len(sizes):
            return
        current.append(sizes[index])
        visit(index + 1, current, total + sizes[index])
        current.pop()
        next_index = index + 1
        while next_index < len(sizes) and sizes[next_index] == sizes[index]:
            next_index += 1
        visit(next_index, current, total)

    visit(0, [], 0)
    results = []
    for a in subsets:
        b = sizes[:]
        for size in a:
            b.remove(size)
        if sum(b) == target_b:
            results.append((a, b))
    return sorted(results, key=lambda pair: abs(len(pair[0]) - len(pair[1])))


def calculate_complexity(grid):
    """CalculateComplexity, ShapeComplexityCalculator.cs (internal surface/volume)."""
    rows = grid.get("Rows") or []
    width = max((len(row.get("Panels") or []) for row in rows), default=0)
    positions = panel_positions(grid)
    if not positions:
        return 0.0
    edges = 0
    for r, c in positions:
        for dr, dc in STRING_DIRS:
            nr, nc = r + dr, c + dc
            if 0 <= nr < len(rows) and 0 <= nc < width:
                cells = rows[nr].get("Panels") or []
                if nc >= len(cells) or cells[nc]["Code"] == 0:
                    edges += 1
    return edges / len(positions)


def should_use_carving(grid):
    """ShouldUseCarving, ShapeComplexityCalculator.cs; CarvingThreshold is 0.0."""
    complexity = calculate_complexity(grid)
    return not (count_panels(grid) > 140 and complexity < 0.35) and complexity >= 0.0


def _neighbors(panel, region, directions=CONNECTIVITY_DIRS):
    r, c = panel
    return [(r + dr, c + dc) for dr, dc in directions if (r + dr, c + dc) in region]


def connected_components(region, directions=CONNECTIVITY_DIRS):
    """GetConnectedComponents, GridSplitter.cs; preserves BFS component order."""
    visited = set()
    components = []
    for start in region:
        if start in visited:
            continue
        component = {}
        queue = deque([start])
        while queue:
            p = queue.popleft()
            if p in visited:
                continue
            visited.add(p)
            component[p] = None
            queue.extend(n for n in _neighbors(p, region, directions) if n not in visited)
        components.append(component)
    return components


def is_connected(region, allow_diagonal=True):
    """IsRegionConnected/IsConnected, GridSplitter.cs and ColumnCarvingStrategy.cs."""
    return len(connected_components(region, CONNECTIVITY_DIRS if allow_diagonal else STRING_DIRS)) <= 1


def count_peninsulas(region):
    """FindPeninsulas/FindArticulationPoints, GridSplitter.cs, including root handling."""
    if len(region) <= 2:
        return 0
    disc, low, parent, children, gateways = {}, {}, {}, Counter(), {}
    for start in region:
        if start in disc:
            continue
        stack = [(start, 0, True)]
        while stack:
            u, index, first = stack.pop()
            if first:
                disc[u] = low[u] = len(disc)
            neighbors = _neighbors(u, region)
            descended = False
            for i in range(index, len(neighbors)):
                v = neighbors[i]
                if v not in disc:
                    children[u] += 1
                    parent[v] = u
                    stack.append((u, i + 1, False))
                    stack.append((v, 0, True))
                    descended = True
                    break
                if v != parent.get(u):
                    low[u] = min(low[u], disc[v])
            if not descended and u in parent:
                par = parent[u]
                low[par] = min(low[par], low[u])
                # C# also adds DFS roots here; do not substitute textbook Tarjan.
                if low[u] >= disc[par]:
                    gateways[par] = None
        if children[start] > 1:
            gateways[start] = None
    peninsulas = []
    for gateway in gateways:
        components = connected_components({p: None for p in region if p != gateway})
        if len(components) < 2:
            continue
        largest = max(map(len, components))
        num_largest = sum(len(c) == largest for c in components)
        for component in components:
            if len(component) == largest and num_largest == 1:
                continue
            cells = set(component)
            if any(cells <= old or old <= cells for old in peninsulas):
                continue
            peninsulas.append(cells | {gateway})
    return len(peninsulas)


def _blank():
    return {"Code": 0, "Id": "", "Seq": 0}


def _row_indices(grid):
    indices = grid.get("RowIndices")
    return list(indices) if indices is not None else list(range(len(grid["Rows"])))


def _piece(grid, rows, sequences, indices=None, carving=False):
    result = {"Sequences": list(sequences), "Rows": rows}
    for name in ("Dwgname", "Modify"):
        if name in grid:
            result[name] = deepcopy(grid[name])
    if not carving:
        result["RowIndices"] = _row_indices(grid) if indices is None else indices
    return result


def _region_piece(grid, region, sequences, crop_columns=False, crop_rows=False, carving=False):
    positions = panel_positions(grid)
    rlo, rhi = (min(r for r, c in region), max(r for r, c in region)) if crop_rows else (0, len(grid["Rows"]) - 1)
    clo, chi = (min(c for r, c in region), max(c for r, c in region)) if crop_columns else (0, 0)
    rows = []
    for r in range(rlo, rhi + 1):
        columns = range(clo, chi + 1) if crop_columns else range(len(grid["Rows"][r].get("Panels") or []))
        rows.append({"Panels": [dict(positions[r, c]) if (r, c) in region else _blank() for c in columns]})
    original = _row_indices(grid)
    indices = [original[r] if r < len(original) else r for r in range(rlo, rhi + 1)] if crop_rows else None
    return _piece(grid, rows, sequences, indices, carving)


def get_step_positions(num_cols):
    """GetStepPositions, GridSplitter.cs; returned positions are sorted, not priority order."""
    positions = [num_cols // 2]
    if num_cols >= 4:
        positions += [num_cols // 4, 3 * num_cols // 4]
    if num_cols >= 6:
        positions += [num_cols // 3, 2 * num_cols // 3]
    if num_cols >= 10:
        positions += [num_cols // 5, 4 * num_cols // 5]
    return sorted(set(positions))


def generate_horizontal_cuts(region, max_jogs):
    """GenerateHorizontalCuts and its multi/partial-cut helpers, GridSplitter.cs."""
    if not region:
        return
    min_r, max_r = min(r for r, c in region), max(r for r, c in region)
    min_c, max_c = min(c for r, c in region), max(c for r, c in region)
    if max_r <= min_r:
        return
    width = max_c - min_c + 1
    priority = sorted(range(min_r + 1, max_r + 1), key=lambda r: abs(r - (min_r + max_r) / 2.0))
    seen = set()

    def emit(cut):
        cut = tuple(cut)
        if cut not in seen:
            seen.add(cut)
            return (min_c, cut)
        return None

    def segmented(rows, boundaries):
        return [rows[sum(c >= b for b in boundaries)] for c in range(width)]

    def candidates():
        for base in priority:
            yield [base] * width
        steps = get_step_positions(width)
        if max_jogs >= 1:
            for base in priority:
                for step in steps:
                    if base + 1 <= max_r:
                        yield segmented([base, base + 1], [step])
                    if base - 1 > min_r:
                        yield segmented([base, base - 1], [step])
        if max_jogs >= 2:
            bumps = [(width // 3, 2 * width // 3), (0, width // 2), (width // 2, width), (width // 4, 3 * width // 4)]
            if width >= 6:
                bumps.append((2 * width // 5, 3 * width // 5))
            bumps += [(0, width // 3), (2 * width // 3, width)]
            for base in priority:
                for start, end in bumps:
                    if base + 1 <= max_r:
                        yield segmented([base, base + 1, base], [start, end])
                    if base - 1 > min_r:
                        yield segmented([base, base - 1, base], [start, end])
                for a, b in combinations(steps, 2):
                    if base + 2 <= max_r:
                        yield segmented([base, base + 1, base + 2], [a, b])
                    if base - 2 > min_r:
                        yield segmented([base, base - 1, base - 2], [a, b])
                    if base + 1 <= max_r and base > min_r:
                        yield segmented([base, base + 1, base], [a, b])
                    if base - 1 > min_r and base <= max_r:
                        yield segmented([base, base - 1, base], [a, b])
        counts = Counter(c - min_c for r, c in region)
        for jogs in range(3, min(max_jogs, 5) + 1):
            boundaries = [i * width // (jogs + 1) for i in range(1, jogs + 1)]
            interesting = [i for i in range(1, width - 1) if counts[i] < len(region) / width * 0.7 or counts[i] != counts[i - 1]]
            interesting += [width // 4, width // 2, 3 * width // 4]
            interesting = sorted({c for c in interesting if 0 < c < width})
            strategic = list(islice(combinations(interesting, jogs), 10))
            for rows in islice(product(priority[:5], repeat=jogs + 1), 50):
                yield segmented(rows, boundaries)
                for bounds in strategic:
                    yield segmented(rows, bounds)
        if width < 3:
            return
        occupied = {c for r, c in region}
        gap_start = None
        for col in range(min_c, max_c + 1):
            if col not in occupied and gap_start is None:
                gap_start = col
            elif col in occupied and gap_start is not None:
                for left in (True, False):
                    side = [p for p in region if (p[1] < gap_start if left else p[1] >= col)]
                    if len(side) < 4:
                        continue
                    lo, hi = min(r for r, c in side), max(r for r, c in side)
                    rows = sorted(range(lo + 1, hi + 1), key=lambda r: abs(r - (lo + hi) / 2.0))[:3]
                    for row in rows:
                        # GridSplitter.cs:2418/2455 says B, but maxRow+1 puts these in A.
                        yield [row if (c + min_c < gap_start if left else c + min_c >= col) else max_r + 1 for c in range(width)]
                gap_start = None

    for candidate in candidates():
        cut = emit(candidate)
        if cut is not None:
            yield cut


def split_json(grid, max_jogs=-1):
    """SplitJson/FindBestSplit, GridSplitter.cs; the first accepted cut wins."""
    region = panel_positions(grid)
    sizes = expand_flat_sequences(grid.get("Sequences"))
    if not region or not sizes:
        return None
    start = max_jogs if max_jogs >= 0 else (2 if len(region) >= 250 else 1)
    ceiling, cap = 5, None
    if len(region) > 2000:
        start, ceiling, cap = min(start, 1), 1, 4000
    partitions = {}
    peninsula_counts = {}
    for jogs in range(start, ceiling + 1):
        blocked = False
        cuts = generate_horizontal_cuts(region, jogs)
        if cap is not None:
            cuts = islice(cuts, cap)
        for min_col, cut in cuts:
            a = {p: None for p in region if p[0] < cut[p[1] - min_col]}
            b = {p: None for p in region if p not in a}
            if not a or not b or not is_connected(a) or not is_connected(b):
                continue
            issues = []
            for part in (a, b):
                key = frozenset(part)
                if key not in peninsula_counts:
                    peninsula_counts[key] = count_peninsulas(part)
                issues.append(peninsula_counts[key] > 1)
            if any(issues):
                blocked = True
                continue
            if len(a) not in partitions:
                partitions[len(a)] = find_size_partitions(sizes, len(a), len(b))
            if partitions[len(a)]:
                sa, sb = partitions[len(a)][0]
                return (_region_piece(grid, a, sizes_to_flat_sequences(sa)),
                        _region_piece(grid, b, sizes_to_flat_sequences(sb)))
        if not blocked:
            break
    return None


def split_json_vertical(grid):
    """SplitJsonVertical, GridSplitter.cs; crop at the gap and retain all rows."""
    region = panel_positions(grid)
    sizes = expand_flat_sequences(grid.get("Sequences"))
    if not region or not sizes:
        return None
    cols = {c for r, c in region}
    best = None
    best_score = float("inf")
    for gap in range(min(cols) + 1, max(cols)):
        if gap in cols:
            continue
        a = [p for p in region if p[1] < gap]
        b = [p for p in region if p[1] >= gap]
        partitions = find_size_partitions(sizes, len(a), len(b))
        if not a or not b or not partitions:
            continue
        height = max(max(r for r, c in part) - min(r for r, c in part) + 1 for part in (a, b))
        score = height * 100 + abs(len(a) - len(b))
        if score < best_score:
            best_score = score
            best = tuple(_piece(grid, [{"Panels": deepcopy(row["Panels"][:gap] if left else row["Panels"][gap:])} for row in grid["Rows"]], sizes_to_flat_sequences(s)) for left, s in zip((True, False), partitions[0]))
    return best


def force_vertical_split(grid, max_cols_per_piece=30):
    """ForceVerticalSplit, GridSplitter.cs; crop each region to its column bounds."""
    region = panel_positions(grid)
    sizes = expand_flat_sequences(grid.get("Sequences"))
    if not region or not sizes:
        return None
    lo, hi = min(c for r, c in region), max(c for r, c in region)
    for col in sorted(range(lo + 1, hi + 1), key=lambda c: abs(c - (lo + hi) // 2)):
        a = {p: None for p in region if p[1] < col}
        b = {p: None for p in region if p[1] >= col}
        if not a or not b or not is_connected(a) or not is_connected(b):
            continue
        if all(len({c for r, c in part}) > max_cols_per_piece for part in (a, b)):
            continue
        partitions = find_size_partitions(sizes, len(a), len(b))
        if partitions:
            return tuple(_region_piece(grid, part, sizes_to_flat_sequences(s), crop_columns=True) for part, s in zip((a, b), partitions[0]))
    return None


def bisect_vertically(grid):
    """BisectVertically, GridSplitter.cs; midpoint rounds up and layout stays full-width."""
    region = panel_positions(grid)
    if not region:
        return None
    lo, hi = min(c for r, c in region), max(c for r, c in region)
    if lo >= hi:
        return None
    mid = (lo + hi + 1) // 2
    sizes = expand_flat_sequences(grid.get("Sequences"))
    preferred = sizes[0] if sizes else 15
    output = []
    for left in (True, False):
        rows = []
        count = 0
        for row in grid["Rows"]:
            cells = []
            for c, cell in enumerate(row.get("Panels") or []):
                if (c < mid) == left:
                    new = dict(cell)
                    if cell["Code"] == 1:
                        count += 1
                    else:
                        new["Seq"] = 0
                    cells.append(new)
                else:
                    cells.append(_blank())
            rows.append({"Panels": cells})
        output.append(_piece(grid, rows, compute_sequences_for_panel_count(count, preferred)))
    return tuple(output)


def force_horizontal_split(grid, max_rows_per_piece=30):
    """ForceHorizontalSplit, GridSplitter.cs; strict pass then RT-001 fallback."""
    region = panel_positions(grid)
    if not region:
        return None
    lo, hi = min(r for r, c in region), max(r for r, c in region)
    rows = sorted(range(lo + 1, hi + 1), key=lambda r: abs(r - (lo + hi) // 2))
    sizes = expand_flat_sequences(grid.get("Sequences"))
    for mode in ("strict", "connected", "unconditional"):
        if mode == "strict" and not sizes:
            continue
        if mode != "strict" and hi - lo + 1 <= max_rows_per_piece:
            return None
        for row in rows:
            a = {p: None for p in region if p[0] < row}
            b = {p: None for p in region if p[0] >= row}
            if not a or not b:
                continue
            if mode != "unconditional" and (not is_connected(a) or not is_connected(b)):
                continue
            if mode == "strict":
                partitions = find_size_partitions(sizes, len(a), len(b))
                if not partitions:
                    continue
                if any(max(r for r, c in p) - min(r for r, c in p) + 1 > max_rows_per_piece for p in (a, b)):
                    continue
                sequences = [sizes_to_flat_sequences(s) for s in partitions[0]]
            else:
                sequences = [compute_sequences_for_panel_count(len(p), sizes[0] if sizes else 15) for p in (a, b)]
            return tuple(_region_piece(grid, p, s, crop_columns=True, crop_rows=True) for p, s in zip((a, b), sequences))
    return None


def count_dead_ends(region):
    """CountDeadEnds, RegionCarvingSplitter.cs; string neighbors are four-way."""
    return sum(len(_neighbors(p, region, STRING_DIRS)) == 1 for p in region)


def calculate_shape_simplicity(region):
    """CalculateShapeSimplicity, RegionCarvingSplitter.cs."""
    if len(region) <= 1:
        return float(len(region))
    height = max(r for r, c in region) - min(r for r, c in region) + 1
    width = max(c for r, c in region) - min(c for r, c in region) + 1
    area = height * width
    boundary = [p for p in region if len(_neighbors(p, region)) < 8]
    changes = sum(len(_neighbors(p, region)) < 4 for p in boundary) if len(boundary) > 2 else 0
    smoothness = max(0, 1.0 - (changes - 4) / 20.0)
    dead = count_dead_ends(region)
    peninsula = 0 if dead > 2 else 1.0 - dead / 4.0
    gaps = max(0, 1.0 - (area - len(region)) / 10.0)
    aspect = max(width, height) / max(1, min(width, height))
    bonus = 1.0 if aspect <= 3 else max(0, 1.0 - (aspect - 3) / 10.0)
    return max(0, min(1, len(region) / area * 0.35 + smoothness * 0.25
                      + peninsula * 0.20 + gaps * 0.15 + bonus * 0.05))


def quick_stringability_check(region):
    """QuickStringabilityCheck/HasBranchingStructure, RegionCarvingSplitter.cs."""
    if len(region) <= 2:
        return True
    neighbors = {p: _neighbors(p, region, STRING_DIRS) for p in region}
    if any(not ns for ns in neighbors.values()) or sum(len(ns) == 1 for ns in neighbors.values()) > 2:
        return False
    for panel, ns in neighbors.items():
        if len(ns) <= 2:
            continue
        without = {p: None for p in region if p != panel}
        components = connected_components(without, STRING_DIRS)
        if sum(any(n in component for n in ns) for component in components) > 2:
            return False
    return True


def is_stringable(region, max_search_depth=10000):
    """IsStringable/FindHamiltonianPath/CreatesIsolation, RegionCarvingSplitter.cs."""
    if len(region) <= 1:
        return True
    if not quick_stringability_check(region):
        return False
    starts = [p for p in region if len(_neighbors(p, region, STRING_DIRS)) == 1]
    if not starts:
        lo_r, hi_r = min(r for r, c in region), max(r for r, c in region)
        lo_c, hi_c = min(c for r, c in region), max(c for r, c in region)
        starts = [p for p in ((lo_r, lo_c), (lo_r, hi_c), (hi_r, lo_c), (hi_r, hi_c)) if p in region]
        if not starts:
            starts = [min(region)]
            if max(region) != starts[0]:
                starts.append(max(region))
    search_count = 0

    def visit(current, visited):
        nonlocal search_count
        previous = search_count
        search_count += 1
        if previous >= max_search_depth:
            return False
        if len(visited) == len(region):
            return True
        neighbors = [p for p in _neighbors(current, region, STRING_DIRS) if p not in visited]
        neighbors.sort(key=lambda p: sum(n not in visited for n in _neighbors(p, region, STRING_DIRS)))
        for p in neighbors:
            visited.add(p)
            remaining = {n: None for n in region if n not in visited}
            isolated = len(remaining) > 1 and any(not _neighbors(n, remaining, STRING_DIRS) for n in remaining)
            if not isolated and visit(p, visited):
                return True
            visited.remove(p)
        return False

    for start in starts:
        if visit(start, {start}):
            return True
        if search_count >= max_search_depth:
            break
    return False


def calculate_row_density(region):
    """CalculateRowDensity, ColumnCarvingStrategy.cs; excludes empty rows."""
    return len(region) / len({r for r, c in region}) if region else 0.0


def has_poor_row_density(region):
    """HasPoorRowDensity, ColumnCarvingStrategy.cs."""
    if len(region) < 50:
        return False
    width = max(c for r, c in region) - min(c for r, c in region) + 1
    counts = list(Counter(r for r, c in region).values())
    average = sum(counts) / len(counts)
    deviation = sqrt(sum((n - average) ** 2 for n in counts) / len(counts))
    return (len(counts) > width * 1.5 or average < width * 0.5
            or (deviation > average * 0.4 and average < width * 0.75))


def should_use_column_carving(region):
    """ShouldUseColumnCarving, ColumnCarvingStrategy.cs; returns (use, ordered gaps)."""
    if not region:
        return False, []
    lo, hi = min(c for r, c in region), max(c for r, c in region)
    width = hi - lo + 1
    counts = Counter(c for r, c in region)
    median = sorted(counts[c] for c in range(lo, hi + 1))[width // 2]
    gaps = [c for c in range(lo, hi + 1) if counts[c] < median * 0.15]
    zeros = [c for c in range(lo, hi + 1) if counts[c] == 0]

    def add(c):
        if c not in gaps:
            gaps.append(c)

    for c in zeros:
        add(c)
    for c in range(lo, hi):
        sparse = 0
        for i in range(min(3, hi - c + 1)):
            if counts[c + i] >= median * 0.3:
                break
            sparse += 1
        if sparse >= 2:
            add(c + sparse // 2)
    for c in range(lo + 1, hi):
        average = (counts[c - 1] + counts[c + 1]) / 2.0
        if counts[c] < median * 0.6 and average > median * 0.85 and counts[c] < average * 0.7:
            add(c)
    cliffs = []
    for c in range(lo, hi):
        if abs(counts[c] - counts[c + 1]) > median * 0.30:
            cliff = c if counts[c] < counts[c + 1] else c + 1
            cliffs.append(cliff)
            if lo + width // 5 < cliff < hi - width // 5:
                add(cliff)
    return (any(lo + width // 4 < c < hi - width // 4 for c in gaps)
            or bool(zeros) or len(cliffs) >= 2), gaps


def split_at_column(region, split_column):
    """SplitAtColumn, ColumnCarvingStrategy.cs; the column itself is excluded literally."""
    # ColumnCarvingStrategy.cs:236-237 uses < and >, not >=. Allocation rejects loss.
    return ({p: None for p in region if p[1] < split_column},
            {p: None for p in region if p[1] > split_column})


def find_best_vertical_split_column(region, gap_columns):
    """FindBestVerticalSplitColumn, ColumnCarvingStrategy.cs."""
    if not gap_columns:
        return -1
    lo, hi = min(c for r, c in region), max(c for r, c in region)
    middle = lo + (hi - lo + 1) // 2
    counts = Counter(c for r, c in region)
    candidates = []
    for gap in gap_columns:
        if gap == lo or counts[gap - 1] > 0 or gap == hi or counts[gap + 1] > 0:
            candidates.insert(0, gap)
        else:
            candidates.append(gap)
    for gap in sorted(candidates, key=lambda c: abs(c - middle)):
        a, b = split_at_column(region, gap)
        minimum = 20 if counts[gap] < 5 else 50
        if len(a) >= minimum and len(b) >= minimum and is_connected(a, False) and is_connected(b, False):
            return gap
    return -1


def detect_peninsula(region):
    """DetectPeninsula, ColumnCarvingStrategy.cs; returns the accepted column or -1."""
    if not region:
        return -1
    lo, hi = min(c for r, c in region), max(c for r, c in region)
    height = max(r for r, c in region) - min(r for r, c in region) + 1
    width = hi - lo + 1
    if width < 5 or height < 10:
        return -1
    counts = Counter(c for r, c in region)
    median = sorted(counts[c] for c in range(lo, hi + 1))[width // 2]
    left = right = False
    split = -1
    for is_left in (True, False):
        peninsula_width = 0
        columns = range(lo, min(lo + 2, hi) + 1) if is_left else range(hi, max(hi - 2, lo) - 1, -1)
        for c in columns:
            if counts[c] < height * 0.6:
                break
            peninsula_width += 1
        if 0 < peninsula_width <= 3:
            gap = lo + peninsula_width if is_left else hi - peninsula_width
            if lo <= gap <= hi and counts[gap] < median * 0.5:
                if is_left:
                    left, split = True, gap
                else:
                    right, split = True, gap + 1
    if left or right:
        peninsula = {p: None for p in region if (p[1] < split if left else p[1] >= split)}
        main = {p: None for p in region if p not in peninsula}
        if 10 <= len(peninsula) < 100 and len(main) >= 50 and is_connected(peninsula) and is_connected(main):
            return split
    return -1


def allocate_single_string(sequences, carved_size, remainder_size):
    """AllocateSingleString, RegionCarvingSplitter.cs; preserves the greedy allocation."""
    a, b, qa, qb = sequences
    if carved_size + remainder_size != a * qa + b * qb:
        return None
    allocated = [0, 0]
    remaining = carved_size
    for i in ((0, 1) if a >= b else (1, 0)):
        length, quantity = (a, qa) if i == 0 else (b, qb)
        if quantity > 0 and length > 0 and remaining > 0:
            allocated[i] = min(quantity, remaining // length)
            remaining -= allocated[i] * length
    if remaining > 0:
        return None
    first = [a, b, *allocated] if any(allocated) else [0, 0, 0, 0]
    second = [a, b, qa - allocated[0], qb - allocated[1]]
    if second[2] == second[3] == 0:
        second = [0, 0, 0, 0]
    elif second[2] == 0:
        second = [b, b - 1, second[3], 0]
    elif second[3] == 0:
        second = [a, a - 1, second[2], 0]
    return first, second


def build_segments(grid):
    """BuildSegments, RegionCarvingSplitter.cs; (row, first column, last column)."""
    segments = []
    for r, row in enumerate(grid["Rows"]):
        start = None
        panels = row.get("Panels") or []
        for c, panel in enumerate(panels):
            if panel["Code"] == 1 and start is None:
                start = c
            elif panel["Code"] != 1 and start is not None:
                segments.append((r, start, c - 1))
                start = None
        if start is not None:
            segments.append((r, start, len(panels) - 1))
    return segments


def get_corner_segments(segments):
    """GetCornerSegments, RegionCarvingSplitter.cs; preserves first-seen corner order."""
    if not segments:
        return []
    lo, hi = min(s[0] for s in segments), max(s[0] for s in segments)
    result = []

    def add(s):
        if s not in result:
            result.append(s)

    top = sorted((s for s in segments if s[0] == lo), key=lambda s: s[1])
    add(top[0])
    if len(top) > 1:
        add(top[-1])
    else:
        next_row = sorted((s for s in segments if s[0] == lo + 1), key=lambda s: -s[2])
        if next_row:
            add(next_row[0])
    bottom = sorted((s for s in segments if s[0] == hi), key=lambda s: s[1])
    add(bottom[0])
    add(bottom[-1])
    for rows in (range(lo, min(lo + 2, hi) + 1), range(hi, max(hi - 2, lo) - 1, -1)):
        for r in rows:
            for s in segments:
                if s[0] == r:
                    add(s)
    return result


def is_safe_to_add(panel, current_carve, all_panels):
    """IsSafeToAdd, RegionCarvingSplitter.cs; keeps the modulo-10 simplicity checkpoint."""
    carve = dict.fromkeys(current_carve)
    carve[panel] = None
    remainder = {p: None for p in all_panels if p not in carve}
    if len(remainder) < 50:
        return True
    if len(_neighbors(panel, all_panels)) <= 2 and not is_connected(remainder):
        return False
    if count_dead_ends(carve) > 3:
        return False
    if len(carve) % 10 == 0 and len(remainder) <= 200:
        if count_dead_ends(remainder) > 3 or calculate_shape_simplicity(remainder) < 0.35:
            return False
    return True


def try_carve_from_segment(all_panels, segments, start, target_size):
    """TryCarveFromSegment, RegionCarvingSplitter.cs; horizontal frontier and band growth."""
    result = {}
    lo, hi = min(s[0] for s in segments), max(s[0] for s in segments)
    down = start[0] <= (lo + hi) // 2
    min_c, max_c = min(s[1] for s in segments), max(s[2] for s in segments)
    right = start[1] <= (min_c + max_c) // 2
    col_from, col_to = (start[1], start[2]) if right else (start[2], start[1])
    direction = 1 if right else -1
    for c in range(col_from, col_to + direction, direction):
        p = (start[0], c)
        if p in all_panels:
            result[p] = None
            if len(result) >= target_size:
                return result
    band_start = band_end = start[0]
    # RegionCarvingSplitter.cs:1133 uses colFrom when expanding left, literally.
    frontier = {start[0]: col_to if right else col_from}

    def acceptable(p):
        return (not result or bool(_neighbors(p, result))) and is_safe_to_add(p, result, all_panels)

    for _ in range(target_size * 5):
        if len(result) >= target_size:
            break
        added = False
        for r in range(band_start, band_end + 1):
            if len(result) >= target_size:
                break
            if r not in frontier:
                frontier[r] = frontier[min(frontier, key=lambda row: abs(row - r))]
            candidate = (r, frontier[r] + direction)
            if candidate in all_panels and candidate not in result and acceptable(candidate):
                result[candidate] = None
                frontier[r] = candidate[1]
                added = True
        if added:
            continue
        expanded = True
        if down and band_end < hi:
            band_end += 1
        elif not down and band_start > lo:
            band_start -= 1
        elif down and band_start > lo:
            band_start -= 1
        elif not down and band_end < hi:
            band_end += 1
        else:
            expanded = False
        if expanded:
            for r in range(band_start, band_end + 1):
                if len(result) >= target_size:
                    break
                panels = sorted((p for p in all_panels if p[0] == r and p not in result), key=lambda p: p[1] if right else -p[1])
                for p in panels:
                    if len(result) >= target_size:
                        break
                    if acceptable(p):
                        result[p] = None
                        if r not in frontier or (p[1] > frontier[r] if right else p[1] < frontier[r]):
                            frontier[r] = p[1]
                        added = True
        if not added and len(result) < target_size:
            # Sorting before the pure predicate avoids doing expensive checks for
            # later candidates; stable order and the selected candidate are identical.
            candidates = sorted((p for p in all_panels if p not in result), key=lambda p: (abs(p[0] - start[0]), p[1] if right else -p[1]))
            candidate = next((p for p in candidates if acceptable(p)), None)
            if candidate is None:
                break
            result[candidate] = None
            band_start = min(band_start, candidate[0])
            band_end = max(band_end, candidate[0])
    return result if len(result) == target_size else None


def validate_carving(carved, remainder):
    """ValidateCarving, RegionCarvingSplitter.cs; both pieces need stringability."""
    if not is_connected(remainder) or calculate_shape_simplicity(remainder) < 0.5:
        return False
    for region in (carved, remainder):
        if count_dead_ends(region) > 2 or not quick_stringability_check(region):
            return False
        if len(region) <= 60 and not is_stringable(region, 8000):
            return False
    return True


def carve_segments(grid, target_sizes):
    """CarveSegments, RegionCarvingSplitter.cs; density, peninsula, column, then bands."""
    region = panel_positions(grid)
    if has_poor_row_density(region):
        return None
    split_col = detect_peninsula(region)
    if split_col >= 0:
        peninsula, main = split_at_column(region, split_col)
        if len(peninsula) > len(main):
            peninsula, main = main, peninsula
        if not has_poor_row_density(peninsula) and not has_poor_row_density(main):
            return main, peninsula
    use_columns, gaps = should_use_column_carving(region)
    if use_columns:
        split_col = find_best_vertical_split_column(region, gaps)
        if split_col > 0:
            left, right = split_at_column(region, split_col)
            for size in target_sizes:
                if len(left) == size:
                    return left, right
                if len(right) == size:
                    return right, left
            if min(len(left), len(right)) >= 50:
                return (left, right) if len(left) > len(right) else (right, left)
    segments = build_segments(grid)
    best, best_simplicity = None, -1
    for start in get_corner_segments(segments):
        for size in target_sizes:
            carved = try_carve_from_segment(region, segments, start, size)
            if carved is None or len(carved) != size:
                continue
            remainder = {p: None for p in region if p not in carved}
            if validate_carving(carved, remainder):
                return carved, remainder
            if is_connected(remainder):
                simplicity = calculate_shape_simplicity(remainder)
                if simplicity > best_simplicity:
                    best, best_simplicity = (carved, remainder), simplicity
    return best if best is not None and best_simplicity >= 0.3 else None


def region_carving_split_json(grid):
    """SplitJson, RegionCarvingSplitter.cs; CarverGrid does not emit RowIndices."""
    sequences = grid.get("Sequences")
    if sequences is None or len(sequences) < 4:
        return None
    targets = sorted({sequences[i] for i in (0, 1) if sequences[i] > 0 and sequences[i + 2] > 0}, reverse=True)
    if not targets:
        return None
    regions = carve_segments(grid, targets)
    if regions is None:
        return None
    a, b = regions
    if min(len(a), len(b)) < max(sequences[0], sequences[1]):
        return None
    allocated = allocate_single_string(sequences, len(a), len(b))
    if allocated is None:
        return None
    return tuple(_region_piece(grid, p, s, carving=True) for p, s in zip(regions, allocated))


def split_recursively_with_depth_and_jogs(grid, max_depth, current_depth=0, max_jogs=-1):
    """SplitRecursivelyWithDepthAndJogs, GridSplitHelper.cs (outer retry path)."""
    if current_depth == 0:
        max_pieces = get_max_pieces_for_panel_count(count_panels(grid))
        if max_jogs > 0:
            max_pieces = min(max_pieces, MAX_PIECES_WITH_JOGS)
        if max_pieces <= 1:
            if not exceeds_dimension_limit(grid):
                return [grid]
        else:
            max_depth = min(max_depth, ceil(log2(max_pieces)))
    if not needs_splitting(grid) or current_depth >= max_depth:
        if exceeds_dimension_limit(grid):
            pair = split_json(grid, 5) if count_non_empty_rows(grid) > 30 else None
            pair = pair or split_json_vertical(grid) or force_vertical_split(grid) or split_json(grid, 5)
            if pair:
                return [piece for part in pair for piece in split_recursively_with_depth_and_jogs(part, max_depth, current_depth + 1, max_jogs)]
        return [grid]
    use_carving = should_use_carving(grid) if current_depth == 0 else False
    pair = None
    if count_non_empty_columns(grid) > 30:
        pair = force_vertical_split(grid) or split_json_vertical(grid)
    if pair is None:
        pair = (region_carving_split_json(grid) if use_carving else None) or split_json(grid, max_jogs)
    if pair is None and exceeds_dimension_limit(grid):
        pair = force_horizontal_split(grid) if count_non_empty_rows(grid) > 30 else None
        pair = pair or split_json_vertical(grid) or force_vertical_split(grid) or split_json(grid, 5)
    if pair is None or (not exceeds_dimension_limit(grid) and min(map(count_panels, pair)) < 10):
        return [grid]
    return [piece for part in pair for piece in split_recursively_internal(part, max_depth, current_depth + 1, max_jogs, use_carving)]


def split_recursively_internal(grid, max_depth, current_depth, max_jogs, use_carving):
    """SplitRecursivelyWithDepthAndJogsInternal, GridSplitHelper.cs; retains strategy."""
    terminal = not needs_splitting(grid) or current_depth >= max_depth
    pair = None
    if not terminal:
        pair = (region_carving_split_json(grid) if use_carving else None) or split_json(grid, max_jogs)
    if pair is None and exceeds_dimension_limit(grid):
        pair = split_json_vertical(grid) or force_vertical_split(grid) or split_json(grid, 5)
    if pair is None:
        return [grid]
    if not terminal and not exceeds_dimension_limit(grid) and min(map(count_panels, pair)) < 10:
        return [grid]
    return [piece for part in pair for piece in split_recursively_internal(part, max_depth, current_depth + 1, max_jogs, use_carving)]


def bisect_until_within_limits(grid, depth=0):
    """BisectUntilWithinLimits, GridSplitHelper.cs; accepts at depth greater than ten."""
    if depth > 10 or not exceeds_dimension_limit(grid):
        return [grid]
    pair = bisect_vertically(grid) if count_non_empty_columns(grid) > 30 else None
    if pair is None and count_non_empty_rows(grid) > 30:
        pair = force_horizontal_split(grid)
    if pair is None:
        return [grid]
    return [piece for part in pair for piece in bisect_until_within_limits(part, depth + 1)]


def ensure_pieces_within_column_limit(pieces):
    """EnsurePiecesWithinColumnLimit, GridSplitHelper.cs; also enforces the row limit."""
    return [result for piece in pieces for result in bisect_until_within_limits(piece)]


def split_group(grid, *, depth=10, jogs=1):
    """PrepareIndicesForRetry for one group, GridSplitHelper.cs; BranchCmd.cs starts 10/1."""
    return ensure_pieces_within_column_limit(split_recursively_with_depth_and_jogs(grid, depth, 0, jogs))
