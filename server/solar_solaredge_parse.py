"""Port of the plugin's SolarEdge PDF converter: page primitives to grids, strings and matrices.

Source of truth: Branch2025 ``SolarEdgePdfConverter.cs`` (``SequenceComputer`` and the
converter), run on the primitives :mod:`solar_solaredge_pdf` extracts. The port keeps the
plugin's ordering and tie-breaking exactly, quirks included, because the golden dump of the
plugin is the acceptance oracle:

- spatial lookups return the FIRST hit in the plugin's cell scan order, not the nearest;
- ``HashSet``/``Dictionary`` enumeration is insertion order, so dicts stand in for both;
- ``List.Sort`` is .NET's unstable introsort, ported verbatim in :func:`_dotnet_sort`;
- ``Math.Pow(x, 2)`` goes through the C runtime's ``pow`` (``math.pow``), which is not always
  ``x * x``; ``Math.Round`` is banker's rounding (Python ``round``); ``(int)`` truncates.

Contract: deterministic and bounded. Every all-pairs loop in the plugin is replaced by a spatial
index that provably returns the same answer, and every bucket scan spends from a fixed work budget,
so an adversarial page raises ``SolarEdgeParseError`` instead of running unbounded.
Measured 2026-09-24 on the fixture (3526 panels, 46.5k curves): 0.5 s and 0.33M work units
against a 200M budget; ported literally, the plugin's all-pairs loops (curves x optimizers alone)
would run on the order of 100M Python iterations.
"""
from __future__ import annotations

import math
import re

try:
    from .solar_solaredge_pdf import PagePrimitives
except ImportError:
    from solar_solaredge_pdf import PagePrimitives


MAX_PANELS = 100_000
MAX_OPTIMIZERS = 100_000
MAX_WORK = 200_000_000
INT32_MAX = 2**31 - 1

LEGEND_MARKERS = ("string design report", "stringing report", "legend",
                  "inverter", "string color", "module type")
_STRING_LABEL = re.compile(r"^\d+\.\d+$")
_STRING_LABEL_PARTS = re.compile(r"^(\d+)\.(\d+)$")
_OPT_ID_LABEL = re.compile(r"^\d+$")
_DOTNET_WHITE = "".join(map(chr, (9, 10, 11, 12, 13, 32)))
_POW = math.pow  # Math.Pow(x, 2): the C runtime pow, not x * x
_BLACK = (0.0, 0.0, 0.0)


class SolarEdgeParseError(ValueError):
    """The page cannot be parsed within bounds, or the plugin itself would have thrown."""

    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


# ---------------------------------------------------------------- .NET semantics

def _dotnet_sort(items, compare):
    """``List<T>.Sort(Comparison<T>)`` on .NET 8: ArraySortHelper's introspective sort.

    Unstable above 16 items; reproduced operation for operation so equal keys land where the
    plugin puts them."""
    n = len(items)
    if n > 1:
        _intro_sort(items, 0, n, 2 * (n.bit_length() - 1 + 1), compare)


def _swap_if_greater(keys, compare, i, j):
    if compare(keys[i], keys[j]) > 0:
        keys[i], keys[j] = keys[j], keys[i]


def _intro_sort(keys, lo, hi_excl, depth_limit, compare):
    size = hi_excl - lo
    while size > 1:
        if size <= 16:
            if size == 2:
                _swap_if_greater(keys, compare, lo, lo + 1)
                return
            if size == 3:
                _swap_if_greater(keys, compare, lo, lo + 1)
                _swap_if_greater(keys, compare, lo, lo + 2)
                _swap_if_greater(keys, compare, lo + 1, lo + 2)
                return
            for i in range(lo, lo + size - 1):
                t = keys[i + 1]
                j = i
                while j >= lo and compare(t, keys[j]) < 0:
                    keys[j + 1] = keys[j]
                    j -= 1
                keys[j + 1] = t
            return
        if depth_limit == 0:
            _heap_sort(keys, lo, size, compare)
            return
        depth_limit -= 1
        p = _pick_pivot_and_partition(keys, lo, size, compare)
        _intro_sort(keys, p + 1, lo + size, depth_limit, compare)
        size = p - lo


def _pick_pivot_and_partition(keys, lo, size, compare):
    hi = lo + size - 1
    middle = lo + ((size - 1) >> 1)
    _swap_if_greater(keys, compare, lo, middle)
    _swap_if_greater(keys, compare, lo, hi)
    _swap_if_greater(keys, compare, middle, hi)
    pivot = keys[middle]
    keys[middle], keys[hi - 1] = keys[hi - 1], keys[middle]
    left, right = lo, hi - 1
    while left < right:
        left += 1
        while compare(keys[left], pivot) < 0:
            left += 1
        right -= 1
        while compare(pivot, keys[right]) < 0:
            right -= 1
        if left >= right:
            break
        keys[left], keys[right] = keys[right], keys[left]
    if left != hi - 1:
        keys[left], keys[hi - 1] = keys[hi - 1], keys[left]
    return left


def _heap_sort(keys, lo, n, compare):
    for i in range(n >> 1, 0, -1):
        _down_heap(keys, lo, i, n, compare)
    for i in range(n, 1, -1):
        keys[lo], keys[lo + i - 1] = keys[lo + i - 1], keys[lo]
        _down_heap(keys, lo, 1, i - 1, compare)


def _down_heap(keys, lo, i, n, compare):
    d = keys[lo + i - 1]
    while i <= n >> 1:
        child = 2 * i
        if child < n and compare(keys[lo + child - 1], keys[lo + child]) < 0:
            child += 1
        if not compare(d, keys[lo + child - 1]) < 0:
            break
        keys[lo + i - 1] = keys[lo + child - 1]
        i = child
    keys[lo + i - 1] = d


def _cmp(a, b):
    return (a > b) - (a < b)


def _dotnet_int(text):
    """``int.Parse`` of a regex-matched digit run: surrounding .NET white space allowed, ASCII
    digits only, Int32 range; anything else throws in the plugin, so it raises here."""
    text = text.strip(_DOTNET_WHITE)
    if not text.isascii() or not text.isdigit():
        raise SolarEdgeParseError("LABEL_NOT_INT32", text[:32])
    value = int(text)
    if value > INT32_MAX:
        raise SolarEdgeParseError("LABEL_NOT_INT32", text[:32])
    return value


def _mean(values):
    """LINQ ``Average``: a left-to-right double sum divided by the count (no compensation)."""
    total = 0.0
    count = 0
    for v in values:
        total += v
        count += 1
    return total / count


def _saturated(c):
    hi = max(c[0], c[1], c[2])
    return (hi - min(c[0], c[1], c[2])) > 0.3 or hi > 0.9


def _grey(c):
    r, g, b = c
    max_diff = max(abs(r - g), max(abs(g - b), abs(r - b)))
    if max_diff > 0.1:
        return False
    avg = (r + g + b) / 3.0
    return 0.4 < avg < 0.85


def _panel_fill(c):
    r, g, b = c
    if (r + g + b) / 3.0 < 0.85:
        return False
    if max(abs(r - g), max(abs(g - b), abs(r - b))) < 0.03:
        return False
    return not _grey(c)


def _ignore_case_fold(text):
    """OrdinalIgnoreCase compares simple upper-case mappings char by char."""
    out = []
    for ch in text:
        up = ch.upper()
        out.append(up if len(up) == 1 else ch)
    return "".join(out)


# ---------------------------------------------------------------- model

class Panel:
    __slots__ = ("id", "cx", "cy", "x1", "y1", "x2", "y2", "rot_cx", "rot_cy",
                 "row", "col", "seq", "grid_id", "hex_id")

    def __init__(self, pid, cx, cy, x1, y1, x2, y2):
        self.id = pid
        self.cx = cx
        self.cy = cy
        self.x1 = x1
        self.y1 = y1
        self.x2 = x2
        self.y2 = y2
        self.rot_cx = 0.0
        self.rot_cy = 0.0
        self.row = -1
        self.col = -1
        self.seq = 0
        self.grid_id = -1
        self.hex_id = ""


class Optimizer:
    __slots__ = ("id", "cx", "cy", "color", "panel_ids", "label")

    def __init__(self, oid, cx, cy, color):
        self.id = oid
        self.cx = cx
        self.cy = cy
        self.color = color
        self.panel_ids = []
        self.label = ""


class StringInfo:
    __slots__ = ("inverter_id", "string_input_number", "continuous_number", "panel_seqs", "label")

    def __init__(self, inverter_id, string_input_number, panel_seqs, label):
        self.inverter_id = inverter_id
        self.string_input_number = string_input_number
        self.continuous_number = 0
        self.panel_seqs = panel_seqs
        self.label = label

    def to_json(self):
        return {"InverterId": self.inverter_id, "StringInputNumber": self.string_input_number,
                "ContinuousNumber": self.continuous_number, "PanelSeqs": list(self.panel_seqs),
                "Label": self.label}


def _cell(value, size):
    return int(value / size)  # C# (int) cast: truncation toward zero


class _ScanIndex:
    """The plugin's optimizer grid lookup, answering with EVERY hit in its scan order.

    The plugin buckets by truncated cell, scans dx -1..1 outer, dy inner, bucket order, and
    returns the first hit. Keeping all hits in that order lets one global index answer a lookup
    restricted to any subset (the subset's own index scans the same cells in the same relative
    order), which is how the per-grid ``SequenceComputer`` and ``DetectGridAngle`` indexes are
    derived without rebuilding or rescanning."""

    def __init__(self, parse, items, size, tolerance, use_pow):
        self.parse = parse
        self.size = size
        self.tol_sq = tolerance * tolerance
        self.use_pow = use_pow
        self.buckets = {}
        for item in items:
            self.buckets.setdefault((_cell(item.cx, size), _cell(item.cy, size)), []).append(item)

    def hits(self, x, y):
        gx = _cell(x, self.size)
        gy = _cell(y, self.size)
        out = []
        buckets = self.buckets
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                bucket = buckets.get((gx + dx, gy + dy))
                if bucket is None:
                    continue
                self.parse.spend(len(bucket))
                for item in bucket:
                    if self.use_pow:
                        dist_sq = _POW(item.cx - x, 2) + _POW(item.cy - y, 2)
                    else:
                        dist_sq = (item.cx - x) * (item.cx - x) + (item.cy - y) * (item.cy - y)
                    if dist_sq < self.tol_sq:
                        out.append(item)
        return out


def _first_in(hits, ids):
    for item in hits:
        if item.id in ids:
            return item
    return None


# ---------------------------------------------------------------- converter

class SolarEdgeParse:
    """The converter's constructor pipeline plus ``GenerateMatrixJsons``, on page primitives."""

    def __init__(self, prims: PagePrimitives):
        self.work = 0
        self.page_width = prims.page_width
        self.page_height = prims.page_height
        self.letters = prims.letters
        self.paths = prims.paths
        self.lines = prims.lines
        self.curves = prims.curves

        self.legend_threshold = self._find_legend_threshold()
        self.panels = self._extract_panels()
        self._build_panel_spatial_index()
        self.optimizers = self._extract_optimizers()
        self._build_electrical_hits()
        self.panel_grids = self._group_panels_into_grids()
        self.final_grids = self._merge_electrically_connected_grids()
        self.has_position_keywords = self._detect_position_keywords()
        self._sequence_cache = {}
        self.inverter_strings = {}
        self.all_string_infos = []
        self.inverter_pdf_colors = {}
        self._parse_inverter_string_info()

    def spend(self, amount):
        self.work += amount
        if self.work > MAX_WORK:
            raise SolarEdgeParseError("WORK_BUDGET_EXCEEDED", str(MAX_WORK))

    def _flip(self, y):
        return self.page_height - y

    # ---- legend threshold (:956)
    def _find_legend_threshold(self):
        words = []
        current = []
        length = 0  # StringBuilder.Length: an empty letter value does not start a word
        word_start_x = 0.0
        last_x = -1000.0
        last_y = -1000.0
        for value, x, y, width in sorted(self.letters, key=lambda t: (t[2], t[1])):
            new_word = abs(y - last_y) > 5 or (x - last_x) > 10
            if new_word and length > 0:
                words.append(("".join(current).lower(), word_start_x))
                current = []
                length = 0
            if length == 0:
                word_start_x = x
            current.append(value)
            length += len(value)
            last_x = x + width
            last_y = y
        if length > 0:
            words.append(("".join(current).lower(), word_start_x))
        legend_x = []
        for text, x in words:
            if x < self.page_width * 0.5:
                continue
            for marker in LEGEND_MARKERS:
                if marker in text:
                    legend_x.append(x)
                    break
        if legend_x:
            return min(legend_x) - 20
        return self.page_width * 0.92

    # ---- panels (:1023)
    def _extract_panels(self):
        panels = []
        for filled, _stroked, bbox, fill, _stroke in self.paths:
            if not filled or bbox is None:
                continue
            left, bottom, right, top = bbox
            width = right - left
            height = top - bottom
            min_dim = min(width, height)
            max_dim = max(width, height)
            if min_dim < 8 or max_dim < 12 or max_dim > 200:
                continue
            if not _panel_fill(fill if fill is not None else _BLACK):
                continue
            cx = (left + right) / 2.0
            cy = self._flip((top + bottom) / 2.0)
            if cx >= self.legend_threshold:
                continue
            if len(panels) >= MAX_PANELS:
                raise SolarEdgeParseError("TOO_MANY_PANELS", str(MAX_PANELS))
            panels.append(Panel(len(panels), cx, cy, left, self._flip(top), right,
                                self._flip(bottom)))
        return panels

    def _build_panel_spatial_index(self):
        index = {}
        for panel in self.panels:
            index.setdefault((_cell(panel.cx, 30), _cell(panel.cy, 30)), []).append(panel)
        self.panel_index = index

    def _find_panels_near(self, x, y, radius):
        result = []
        gx = _cell(x, 30)
        gy = _cell(y, 30)
        reach = int(radius / 30) + 1
        index = self.panel_index
        for dx in range(-reach, reach + 1):
            for dy in range(-reach, reach + 1):
                bucket = index.get((gx + dx, gy + dy))
                if bucket is None:
                    continue
                self.spend(len(bucket))
                for p in bucket:
                    if math.sqrt(_POW(p.cx - x, 2) + _POW(p.cy - y, 2)) <= radius:
                        result.append(p)
        return result

    def _find_panel_at(self, x, y, tolerance=15):
        nearby = self._find_panels_near(x, y, tolerance)
        return nearby[0] if nearby else None

    # ---- optimizers (:1139)
    def _extract_optimizers(self):
        path_opts = []
        for _filled, stroked, bbox, _fill, stroke in self.paths:
            if not stroked or bbox is None:
                continue
            left, bottom, right, top = bbox
            width = right - left
            height = top - bottom
            if width < 5 or width > 20 or height < 5 or height > 20:
                continue
            if abs(width - height) > 3:
                continue
            color = stroke if stroke is not None else _BLACK
            if not _saturated(color):
                continue
            cx = (left + right) / 2.0
            cy = self._flip((top + bottom) / 2.0)
            if cx >= self.legend_threshold:
                continue
            path_opts.append((cx, cy, color))

        # Dedup (first wins when |dx| < 3 and |dy| < 3): floored 3-unit cells, +-1 neighbours
        # cover every pair the plugin's all-pairs loop compares.
        dedup = []
        cells = {}
        for opt in path_opts:
            cx, cy = opt[0], opt[1]
            fx, fy = math.floor(cx / 3), math.floor(cy / 3)
            duplicate = False
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    bucket = cells.get((fx + dx, fy + dy))
                    if bucket is None:
                        continue
                    self.spend(len(bucket))
                    for ex in bucket:
                        if abs(ex[0] - cx) < 3 and abs(ex[1] - cy) < 3:
                            duplicate = True
                            break
                    if duplicate:
                        break
                if duplicate:
                    break
            if not duplicate:
                dedup.append(opt)
                cells.setdefault((fx, fy), []).append(opt)
        if len(dedup) > MAX_OPTIMIZERS:
            raise SolarEdgeParseError("TOO_MANY_OPTIMIZERS", str(MAX_OPTIMIZERS))

        # Connected when a saturated curve (len^2 >= 64) starts or ends within dist^2 < 400.
        near = {}
        for i, opt in enumerate(dedup):
            near.setdefault((math.floor(opt[0] / 20), math.floor(opt[1] / 20)), []).append(i)
        connected = set()
        for curve in self.curves:
            sx, sy, ex, ey = curve[0], curve[1], curve[6], curve[7]
            if _POW(sx - ex, 2) + _POW(sy - ey, 2) < 64:
                continue
            if not _saturated(curve[8]):
                continue
            for px, py in ((sx, sy), (ex, ey)):
                fx, fy = math.floor(px / 20), math.floor(py / 20)
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        bucket = near.get((fx + dx, fy + dy))
                        if bucket is None:
                            continue
                        self.spend(len(bucket))
                        for i in bucket:
                            if i in connected:
                                continue
                            ocx, ocy = dedup[i][0], dedup[i][1]
                            d1 = _POW(sx - ocx, 2) + _POW(sy - ocy, 2)
                            d2 = _POW(ex - ocx, 2) + _POW(ey - ocy, 2)
                            if d1 < 400 or d2 < 400:
                                connected.add(i)

        optimizers = []
        for i, opt in enumerate(dedup):
            if i in connected:
                optimizers.append(Optimizer(len(optimizers), opt[0], opt[1], opt[2]))
        self._associate_optimizers_with_panels(optimizers)
        self._extract_optimizer_labels(optimizers)
        return optimizers

    # ---- association (:1250)
    def _associate_optimizers_with_panels(self, optimizers):
        if not optimizers or not self.panels:
            return
        ratio = len(self.panels) / len(optimizers)
        two_to_one = ratio > 1.5
        opt_index = {}
        for opt in optimizers:
            opt_index.setdefault((_cell(opt.cx, 20), _cell(opt.cy, 20)), []).append(opt)

        def find_opt_at(x, y, tolerance=8):
            gx = _cell(x, 20)
            gy = _cell(y, 20)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    bucket = opt_index.get((gx + dx, gy + dy))
                    if bucket is None:
                        continue
                    self.spend(len(bucket))
                    for opt in bucket:
                        if math.sqrt(_POW(opt.cx - x, 2) + _POW(opt.cy - y, 2)) < tolerance:
                            return opt
            return None

        line_panels = {}
        has_line = set()
        if two_to_one:
            for line in self.lines:
                if not _saturated(line[4]):
                    continue
                length = math.sqrt(_POW(line[0] - line[2], 2) + _POW(line[1] - line[3], 2))
                if length <= 5 or length >= 130:
                    continue
                opt1 = find_opt_at(line[0], line[1])
                opt2 = find_opt_at(line[2], line[3])
                if opt1 is not None and opt2 is None:
                    panel = self._find_panel_at(line[2], line[3])
                    if panel is not None:
                        line_panels.setdefault(opt1.id, {})[panel.id] = None
                        has_line.add(opt1.id)
                elif opt2 is not None and opt1 is None:
                    panel = self._find_panel_at(line[0], line[1])
                    if panel is not None:
                        line_panels.setdefault(opt2.id, {})[panel.id] = None
                        has_line.add(opt2.id)

        assigned = set()
        for opt in optimizers:
            primary = self._find_panel_at(opt.cx, opt.cy, 12)
            if primary is not None and primary.id not in assigned:
                opt.panel_ids.append(primary.id)
                assigned.add(primary.id)
            if two_to_one and opt.id in line_panels:
                for pid in line_panels[opt.id]:
                    if pid not in assigned:
                        opt.panel_ids.append(pid)
                        assigned.add(pid)

        def needed_for(opt):
            return 2 if two_to_one and opt.id in has_line else 1

        for opt in optimizers:
            needed = needed_for(opt)
            current = len(opt.panel_ids)
            if current < needed:
                nearby = [p for p in self._find_panels_near(opt.cx, opt.cy, 50)
                          if p.id not in assigned]
                nearby.sort(key=lambda p: _POW(p.cx - opt.cx, 2) + _POW(p.cy - opt.cy, 2))
                for panel in nearby[:needed - current]:
                    opt.panel_ids.append(panel.id)
                    assigned.add(panel.id)

        # Orphans: nearest optimizer under capacity within 60, first minimum in optimizer order.
        # Floored 60-unit cells, +-1 neighbours, candidates visited in optimizer order.
        unassigned = [p for p in self.panels if p.id not in assigned]
        if not unassigned:
            return
        wide = {}
        for opt in optimizers:
            wide.setdefault((math.floor(opt.cx / 60), math.floor(opt.cy / 60)), []).append(opt)
        for panel in unassigned:
            fx, fy = math.floor(panel.cx / 60), math.floor(panel.cy / 60)
            candidates = []
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    bucket = wide.get((fx + dx, fy + dy))
                    if bucket is not None:
                        self.spend(len(bucket))
                        candidates.extend(bucket)
            candidates.sort(key=lambda o: o.id)
            best = None
            best_dist = math.inf
            for opt in candidates:
                if len(opt.panel_ids) >= needed_for(opt):
                    continue
                dist = math.sqrt(_POW(panel.cx - opt.cx, 2) + _POW(panel.cy - opt.cy, 2))
                if dist < best_dist and dist < 60:
                    best_dist = dist
                    best = opt
            if best is not None:
                best.panel_ids.append(panel.id)
                assigned.add(panel.id)

    # ---- labels (:1408)
    def _extract_optimizer_labels(self, optimizers):
        letters = sorted(self.letters, key=lambda t: (t[2], t[1]))
        words = []
        current = ""
        word_x = word_y = 0.0
        for i, (value, x, y_raw, _width) in enumerate(letters):
            y = self._flip(y_raw)
            if not current:
                current = value
                word_x, word_y = x, y
            elif i > 0:
                prev = letters[i - 1]
                x_gap = x - (prev[1] + prev[3])
                y_diff = abs(y_raw - prev[2])
                if -10 < x_gap < 20 and y_diff < 5:
                    current += value
                else:
                    if current and not current.isspace():
                        words.append((current, word_x, word_y))
                    current = value
                    word_x, word_y = x, y
        if current and not current.isspace():
            words.append((current, word_x, word_y))

        # Nearest optimizer with dist^2 < 900 (first minimum in optimizer order): floored
        # 30-unit cells, +-1 neighbours.
        cells = {}
        for opt in optimizers:
            cells.setdefault((math.floor(opt.cx / 30), math.floor(opt.cy / 30)), []).append(opt)
        for text, x, y in words:
            if not _STRING_LABEL.match(text) and not _OPT_ID_LABEL.match(text):
                continue
            if x >= self.legend_threshold:
                continue
            fx, fy = math.floor(x / 30), math.floor(y / 30)
            candidates = []
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    bucket = cells.get((fx + dx, fy + dy))
                    if bucket is not None:
                        self.spend(len(bucket))
                        candidates.extend(bucket)
            candidates.sort(key=lambda o: o.id)
            nearest = None
            min_dist = math.inf
            for opt in candidates:
                dist = _POW(x - opt.cx, 2) + _POW(y - opt.cy, 2)
                if dist < min_dist and dist < 900:
                    min_dist = dist
                    nearest = opt
            if nearest is not None and not nearest.label:
                nearest.label = text

    # ---- grids (:1491)
    def _group_panels_into_grids(self):
        panels = self.panels
        if not panels:
            return []
        index = {}
        for panel in panels:
            index.setdefault((_cell(panel.cx, 80), _cell(panel.cy, 80)), []).append(panel)

        typical = None
        for p1 in panels[:min(500, len(panels))]:
            gx, gy = _cell(p1.cx, 80), _cell(p1.cy, 80)
            for dxc in range(-2, 3):
                for dyc in range(-2, 3):
                    bucket = index.get((gx + dxc, gy + dyc))
                    if bucket is None:
                        continue
                    self.spend(len(bucket))
                    for p2 in bucket:
                        if p1.id >= p2.id:
                            continue
                        x_diff = abs(p2.cx - p1.cx)
                        y_diff = abs(p2.cy - p1.cy)
                        dist = math.sqrt(x_diff * x_diff + y_diff * y_diff)
                        if 5 < dist < 150:
                            if y_diff < x_diff * 0.3:
                                spacing = x_diff
                            elif x_diff < y_diff * 0.3:
                                spacing = y_diff
                            else:
                                continue
                            if typical is None or spacing < typical:
                                typical = spacing
        typical_spacing = typical if typical is not None else 40
        adjacency = typical_spacing * 1.8
        cardinal = typical_spacing * 0.4

        def neighbors(panel):
            out = []
            gx, gy = _cell(panel.cx, 80), _cell(panel.cy, 80)
            for dxc in range(-2, 3):
                for dyc in range(-2, 3):
                    bucket = index.get((gx + dxc, gy + dyc))
                    if bucket is None:
                        continue
                    self.spend(len(bucket))
                    for other in bucket:
                        if other.id == panel.id:
                            continue
                        x_diff = abs(panel.cx - other.cx)
                        y_diff = abs(panel.cy - other.cy)
                        if math.sqrt(x_diff * x_diff + y_diff * y_diff) > adjacency:
                            continue
                        if y_diff < cardinal or x_diff < cardinal:
                            out.append(other)
            return out

        visited = set()
        grids = []
        for panel in panels:
            if panel.id in visited:
                continue
            grid = []
            queue = [panel]
            head = 0
            while head < len(queue):
                current = queue[head]
                head += 1
                if current.id in visited:
                    continue
                visited.add(current.id)
                grid.append(current)
                current.grid_id = len(grids)
                for nb in neighbors(current):
                    if nb.id not in visited:
                        queue.append(nb)
                self.spend(1)
            if grid:
                grids.append(grid)
        _dotnet_sort(grids, lambda a, b: _cmp(len(b), len(a)))
        for idx, grid in enumerate(grids):
            for panel in grid:
                panel.grid_id = idx
        return grids

    # ---- merge (:1630)
    def _merge_electrically_connected_grids(self):
        if len(self.panel_grids) <= 1:
            return self.panel_grids
        panel_to_grid = {}
        for idx, grid in enumerate(self.panel_grids):
            for panel in grid:
                panel_to_grid[panel.id] = idx
        parent = list(range(len(self.panel_grids)))

        def find(x):
            root = x
            while parent[root] != root:
                root = parent[root]
            while parent[x] != root:
                parent[x], x = root, parent[x]
            return root

        def union(x, y):
            px, py = find(x), find(y)
            if px != py:
                parent[py] = px

        def grid_of(opt):
            if opt.panel_ids and opt.panel_ids[0] in panel_to_grid:
                return panel_to_grid[opt.panel_ids[0]]
            return -1

        for hits1, hits2 in self._electrical_curves + self._electrical_lines:
            opt1, opt2 = hits1[0], hits2[0]
            if opt1.id != opt2.id:
                g1, g2 = grid_of(opt1), grid_of(opt2)
                if g1 >= 0 and g2 >= 0 and g1 != g2:
                    union(g1, g2)

        for opt in self.optimizers:
            if len(opt.panel_ids) < 2:
                continue
            spanned = {}
            for pid in opt.panel_ids:
                if pid in panel_to_grid:
                    spanned[panel_to_grid[pid]] = None
            if len(spanned) > 1:
                grid_list = list(spanned)
                for other in grid_list[1:]:
                    union(grid_list[0], other)

        merged = {}
        for panel in self.panels:
            original = panel_to_grid.get(panel.id, 0)
            root = find(original)
            merged.setdefault(root, []).append(panel)
            panel.grid_id = root
        result = list(merged.values())
        _dotnet_sort(result, lambda a, b: _cmp(len(b), len(a)))
        self._angle_curves = None
        for idx, grid in enumerate(result):
            for panel in grid:
                panel.grid_id = idx
            self._assign_row_column_coordinates(grid)
        return result

    def _build_electrical_hits(self):
        """Saturated curves (len^2 >= 64) then lines (len^2 >= 16) that touch an optimizer at
        both ends within 12 (20-unit cells), with every hit in scan order. Shared by the merge
        (first hit over all optimizers) and each grid's SequenceComputer (first hit in grid)."""
        self.opt_scan = _ScanIndex(self, self.optimizers, 20, 12, use_pow=True)
        self._electrical_curves = self._endpoint_hits(
            (c[0], c[1], c[6], c[7]) for c in self.curves
            if _saturated(c[8]) and _POW(c[0] - c[6], 2) + _POW(c[1] - c[7], 2) >= 64)
        self._electrical_lines = self._endpoint_hits(
            (ln[0], ln[1], ln[2], ln[3]) for ln in self.lines
            if _saturated(ln[4]) and _POW(ln[0] - ln[2], 2) + _POW(ln[1] - ln[3], 2) >= 16)

    def _endpoint_hits(self, segments):
        """(start hits, end hits) per segment whose both ends touch some optimizer, in order.

        A segment with an empty hit list at either end can never join two optimizers in any
        subset, so dropping it changes no answer."""
        out = []
        scan = self.opt_scan
        for sx, sy, ex, ey in segments:
            h1 = scan.hits(sx, sy)
            if not h1:
                continue
            h2 = scan.hits(ex, ey)
            if h2:
                out.append((h1, h2))
        return out

    # ---- rows and columns (:1773)
    def _assign_row_column_coordinates(self, grid_panels):
        if not grid_panels:
            return
        grid_ids = {p.id for p in grid_panels}
        grid_opts = [o for o in self.optimizers if any(pid in grid_ids for pid in o.panel_ids)]
        angle = self._detect_grid_angle(grid_opts)
        cos_a = math.cos(-angle)
        sin_a = math.sin(-angle)
        rotate = abs(angle) > 0.001
        for p in grid_panels:
            if rotate:
                p.rot_cx = p.cx * cos_a - p.cy * sin_a
                p.rot_cy = p.cx * sin_a + p.cy * cos_a
            else:
                p.rot_cx = p.cx
                p.rot_cy = p.cy

        ordered = sorted(grid_panels, key=lambda p: p.rot_cy)
        rows = []
        current = [ordered[0]]
        row_start = ordered[0].rot_cy
        for panel in ordered[1:]:
            if abs(panel.rot_cy - row_start) < 12:
                current.append(panel)
            else:
                rows.append(current)
                current = [panel]
                row_start = panel.rot_cy
        rows.append(current)
        for row in rows:
            _dotnet_sort(row, lambda a, b: _cmp(a.rot_cx, b.rot_cx))

        x_spacings = []
        for row in rows:
            for i in range(1, len(row)):
                spacing = row[i].rot_cx - row[i - 1].rot_cx
                if 10 < spacing < 100:
                    x_spacings.append(spacing)
        typical_x = sorted(x_spacings)[len(x_spacings) // 2] if x_spacings else 30

        typical_y = 37
        for i in range(1, len(rows)):
            spacing = _mean(p.rot_cy for p in rows[i]) - _mean(p.rot_cy for p in rows[i - 1])
            if 10 < spacing < 100:
                typical_y = spacing
                break

        min_x = min(p.rot_cx for p in grid_panels)
        min_y = min(p.rot_cy for p in grid_panels)
        for row in rows:
            row_idx = int(round((_mean(p.rot_cy for p in row) - min_y) / typical_y))
            for panel in row:
                panel.row = row_idx
                panel.col = int(round((panel.rot_cx - min_x) / typical_x))
                panel.hex_id = format(panel.id, "04x")

    def _detect_grid_angle(self, grid_opts):
        if not grid_opts:
            return 0.0
        if self._angle_curves is None:
            scan = _ScanIndex(self, self.optimizers, 10, 5, use_pow=False)
            picked = []
            for c in self.curves:
                if not _saturated(c[8]):
                    continue
                x1, y1, x2, y2 = c[0], c[1], c[6], c[7]
                dx = abs(x2 - x1)
                dy = abs(y2 - y1)
                length_sq = dx * dx + dy * dy
                if length_sq < 225 or length_sq > 40000:
                    continue
                if dx > dy and dx > 20:
                    h1 = scan.hits(x1, y1)
                    if not h1:
                        continue
                    h2 = scan.hits(x2, y2)
                    if h2:
                        picked.append((x1, y1, x2, y2, h1, h2))
            self._angle_curves = picked
        ids = {o.id for o in grid_opts}
        angles = []
        for x1, y1, x2, y2, h1, h2 in self._angle_curves:
            opt1 = _first_in(h1, ids)
            opt2 = _first_in(h2, ids)
            if opt1 is not None and opt2 is not None and opt1.id != opt2.id:
                if x1 < x2:
                    angles.append(math.atan2(y2 - y1, x2 - x1))
                else:
                    angles.append(math.atan2(y1 - y2, x1 - x2))
        if not angles:
            return 0.0
        angles.sort()
        return angles[len(angles) // 2]

    # ---- keywords (:750)
    def _detect_position_keywords(self):
        text = _ignore_case_fold("".join(t[0] for t in self.letters))
        return all(k in text for k in ("CENTER", "LEFT", "RIGHT"))

    # ---- SequenceComputer (:96)
    def _sequences(self, grid_index):
        cached = self._sequence_cache.get(grid_index)
        if cached is None:
            cached = _SequenceComputer(self, self.final_grids[grid_index]).compute()
            self._sequence_cache[grid_index] = cached
        return cached

    def _apply_sequences(self, grid, sequences):
        for panel in grid:
            seq = sequences.get(panel.id)
            if seq is not None:
                panel.seq = seq

    # ---- inverter info (:773)
    def _parse_inverter_string_info(self):
        for gi, grid in enumerate(self.final_grids):
            sequences, strings = self._sequences(gi)
            self._apply_sequences(grid, sequences)
            panel_by_id = {p.id: p for p in grid}
            for string_opts in strings:
                first = next((o for o in string_opts if _STRING_LABEL_PARTS.match(o.label or "")),
                             None)
                if first is None:
                    continue
                m = _STRING_LABEL_PARTS.match(first.label)
                inverter_id = _dotnet_int(m.group(1))
                string_input = _dotnet_int(m.group(2))
                seqs = []
                for opt in string_opts:
                    for pid in opt.panel_ids:
                        panel = panel_by_id.get(pid)
                        if panel is not None and panel.seq > 0:
                            seqs.append(panel.seq)
                info = StringInfo(inverter_id, string_input, seqs, first.label)
                self.inverter_strings.setdefault(inverter_id, []).append(info)
                if inverter_id not in self.inverter_pdf_colors and _saturated(first.color):
                    self.inverter_pdf_colors[inverter_id] = first.color
                self.all_string_infos.append(info)
        for infos in self.inverter_strings.values():
            _dotnet_sort(infos, lambda a, b: _cmp(a.string_input_number, b.string_input_number))

    # ---- ratio (:676)
    def detect_optimizer_ratio(self):
        if not self.optimizers:
            return ("unknown", -1, 0, 0)
        two = one = panels_two = panels_one = 0
        for opt in self.optimizers:
            if len(opt.panel_ids) == 2:
                two += 1
                panels_two += 2
            elif len(opt.panel_ids) == 1:
                one += 1
                panels_one += 1
        if two > 0 and one == 0:
            return ("2:1", 2, panels_two, 0)
        if two == 0 and one > 0:
            return ("1:1", 1, 0, panels_one)
        if two > 0 and one > 0:
            if two / (two + one) >= 0.5:
                return ("2:1 with exceptions", 2, panels_two, panels_one)
            return ("1:1 with exceptions", 1, panels_two, panels_one)
        return ("unknown", -1, 0, 0)

    # ---- matrices (:1959)
    def generate_matrix_jsons(self, base_name="solaredge"):
        results = []
        for gi, grid in enumerate(self.final_grids):
            sequences, strings = self._sequences(gi)
            self._apply_sequences(grid, sequences)
            seq_lengths = [sum(len(o.panel_ids) for o in s) for s in strings]
            num_rows = max(p.row for p in grid) + 1
            num_cols = max(p.col for p in grid) + 1
            lookup = {}
            for p in grid:
                lookup[(p.row, p.col)] = p
            break_rows = [r for r in range(num_rows)
                          if not any((r, c) in lookup for c in range(num_cols))]
            break_cols = [c for c in range(num_cols)
                          if not any((r, c) in lookup for r in range(num_rows))]
            sub_grids = _detect_sub_grids(lookup, num_rows, num_cols)
            bridges = _detect_bridge_connections(grid, sub_grids)

            seq_to_inverter = {}
            panel_by_id = {p.id: p for p in grid}
            for string_opts in strings:
                first = next((o for o in string_opts if _STRING_LABEL_PARTS.match(o.label or "")),
                             None)
                if first is None:
                    continue
                m = _STRING_LABEL_PARTS.match(first.label)
                info = (_dotnet_int(m.group(1)), _dotnet_int(m.group(2)))
                for opt in string_opts:
                    for pid in opt.panel_ids:
                        panel = panel_by_id.get(pid)
                        if panel is not None and panel.seq > 0 and panel.seq not in seq_to_inverter:
                            seq_to_inverter[panel.seq] = info

            rows = []
            for row_idx in range(num_rows - 1, -1, -1):
                cells = []
                for col_idx in range(num_cols):
                    p = lookup.get((row_idx, num_cols - 1 - col_idx))
                    if p is not None:
                        inv, sin = seq_to_inverter.get(p.seq, (-1, 0))
                        cells.append(_panel_json(1, p.hex_id, p.seq, inv, sin))
                    else:
                        cells.append(_panel_json(0, "", 0, -1, 0))
                rows.append({"Panels": cells})
            results.append({
                "Dwgname": f"{base_name}_grid{gi + 1}.dwg",
                "Sequences": seq_lengths,
                "Rows": rows,
                "Modify": [seq_lengths[0] if seq_lengths else 0, 0],
                "GridBreakRows": break_rows,
                "GridBreakCols": break_cols,
                "SubGrids": [{"Id": sg[0], "RowStart": min(c[0] for c in sg[1]),
                              "RowEnd": max(c[0] for c in sg[1]),
                              "ColStart": min(c[1] for c in sg[1]),
                              "ColEnd": max(c[1] for c in sg[1]),
                              "PanelCount": len(sg[1]),
                              "Cells": [[r, c] for r, c in sg[1]]} for sg in sub_grids],
                "BridgeConnections": bridges,
                "SolvedSequenceLengths": None,
                "SolvedPanelsPerString": 0,
            })
        return results


def _panel_json(code, hex_id, seq, inverter_id, string_input):
    return {"Code": code, "Id": hex_id, "Seq": seq, "InverterId": inverter_id,
            "StringInputNumber": string_input, "X": 0, "Y": 0, "Angle": 0}


def _detect_sub_grids(lookup, num_rows, num_cols):
    sub_grids = []
    visited = set()
    for row in range(num_rows):
        for col in range(num_cols):
            if (row, col) not in lookup or (row, col) in visited:
                continue
            region = []
            queue = [(row, col)]
            head = 0
            while head < len(queue):
                r, c = queue[head]
                head += 1
                if (r, c) in visited or (r, c) not in lookup:
                    continue
                visited.add((r, c))
                region.append((r, c))
                if r > 0:
                    queue.append((r - 1, c))
                if r < num_rows - 1:
                    queue.append((r + 1, c))
                if c > 0:
                    queue.append((r, c - 1))
                if c < num_cols - 1:
                    queue.append((r, c + 1))
            if region:
                sub_grids.append((len(sub_grids), region))
    return sub_grids


def _detect_bridge_connections(grid, sub_grids):
    bridges = []
    if len(sub_grids) <= 1:
        return bridges
    cell_to_sub = {}
    for sid, cells in sub_grids:
        for cell in cells:
            cell_to_sub[cell] = sid
    by_seq = sorted((p for p in grid if p.seq > 0), key=lambda p: p.seq)
    for curr, nxt in zip(by_seq, by_seq[1:]):
        a = cell_to_sub.get((curr.row, curr.col))
        b = cell_to_sub.get((nxt.row, nxt.col))
        if a is None or b is None:
            continue
        if a != b:
            bridges.append({"FromSubgrid": a, "FromRow": curr.row, "FromCol": curr.col,
                            "ToSubgrid": b, "ToRow": nxt.row, "ToCol": nxt.col})
    return bridges


class _SequenceComputer:
    """``SequenceComputer`` for one grid: electrical graph, string walk, panel order."""

    def __init__(self, parse, grid_panels):
        self.parse = parse
        self.panel_by_id = {p.id: p for p in grid_panels}
        grid_ids = self.panel_by_id
        self.optimizers = [o for o in parse.optimizers
                           if any(pid in grid_ids for pid in o.panel_ids)]
        self.opt_by_id = {o.id: o for o in self.optimizers}
        self.graph = self._build_graph()

    def _build_graph(self):
        graph = {o.id: {} for o in self.optimizers}
        ids = self.opt_by_id
        for h1, h2 in self.parse._electrical_curves + self.parse._electrical_lines:
            opt1 = _first_in(h1, ids)
            if opt1 is None:
                continue
            opt2 = _first_in(h2, ids)
            if opt2 is not None and opt1.id != opt2.id:
                graph[opt1.id][opt2.id] = None
                graph[opt2.id][opt1.id] = None
        return graph

    def find_strings(self):
        strings = []
        visited = set()
        starts = [o for o in self.optimizers if _STRING_LABEL.match(o.label)]
        keyed = []
        for o in starts:
            major, minor = o.label.split(".")
            keyed.append(((_dotnet_int(major), _dotnet_int(minor)), o))
        keyed.sort(key=lambda kv: kv[0])
        for _, start in keyed:
            if start.id in visited:
                continue
            path = self._follow(start, visited)
            if path:
                strings.append(path)
        return strings

    def _follow(self, start, visited):
        path = []
        current = start
        while current is not None:
            if current.id in visited:
                break
            visited.add(current.id)
            path.append(current)
            nxt = None
            for conn in self.graph.get(current.id, ()):
                if conn not in visited and conn in self.opt_by_id:
                    nxt = self.opt_by_id[conn]
                    break
            current = nxt
            self.parse.spend(1)
        return path

    def compute(self):
        sequences = {}
        strings = self.find_strings()
        counter = 1
        for string_opts in strings:
            last = None
            h_dir = [0]
            for i, opt in enumerate(string_opts):
                panels = [self.panel_by_id[pid] for pid in opt.panel_ids if pid in self.panel_by_id]
                if not panels:
                    continue
                next_opt = string_opts[i + 1] if i + 1 < len(string_opts) else None
                if len(panels) == 1:
                    ordered = panels
                    if last is not None:
                        dx = panels[0].cx - last.cx
                        if abs(dx) > 10:
                            h_dir[0] = 1 if dx > 0 else -1
                else:
                    ordered = self._determine_order(panels[0], panels[1], last, next_opt, h_dir,
                                                    opt)
                for panel in ordered:
                    if panel.id not in sequences:
                        sequences[panel.id] = counter
                        counter += 1
                last = ordered[-1]
        return sequences, strings

    def _determine_order(self, p1, p2, last, next_opt, h_dir, current_opt):
        next_panels = None
        if next_opt is not None:
            next_panels = [self.panel_by_id[pid] for pid in next_opt.panel_ids
                           if pid in self.panel_by_id]

        def dist(a, bx, by):
            return math.sqrt(_POW(a.cx - bx, 2) + _POW(a.cy - by, 2))

        if last is not None:
            p1_same = p1.row == last.row
            p2_same = p2.row == last.row
            if not p1_same and not p2_same and h_dir[0] != 0:
                avg_x = (p1.cx + p2.cx) / 2.0
                h_dir[0] = 1 if last.cx < avg_x else -1
            if p1_same != p2_same:
                ordered = [p1, p2] if p1_same else [p2, p1]
                if h_dir[0] != 0:
                    h_dir[0] = -h_dir[0]
            elif not p1_same and not p2_same and next_panels is not None and len(next_panels) >= 1:
                d1 = min(abs(p1.cx - np.cx) + abs(p1.cy - np.cy) for np in next_panels)
                d2 = min(abs(p2.cx - np.cx) + abs(p2.cy - np.cy) for np in next_panels)
                ordered = [p2, p1] if d1 < d2 else [p1, p2]
            else:
                if next_opt is not None and abs(next_opt.cy - current_opt.cy) > 20:
                    if h_dir[0] != 0:
                        if h_dir[0] < 0:
                            ordered = [p2, p1] if p1.cx < p2.cx else [p1, p2]
                        else:
                            ordered = [p2, p1] if p1.cx > p2.cx else [p1, p2]
                    else:
                        ordered = _distance_ordering(p1, p2, last)
                elif next_opt is not None:
                    d1_last = dist(p1, last.cx, last.cy)
                    d2_last = dist(p2, last.cx, last.cy)
                    d1_next = dist(p1, next_opt.cx, next_opt.cy)
                    d2_next = dist(p2, next_opt.cx, next_opt.cy)
                    cost1 = d1_last + d2_next
                    cost2 = d2_last + d1_next
                    ordered = [p1, p2] if cost1 <= cost2 else [p2, p1]
                else:
                    ordered = _distance_ordering(p1, p2, last)
            entry, exit_ = ordered[0], ordered[1]
            dx = exit_.cx - entry.cx
            if abs(dx) > 10:
                h_dir[0] = 1 if dx > 0 else -1
        elif next_opt is not None:
            going_right = next_opt.cx > current_opt.cx
            if p1.row != p2.row:
                entry_panel = None
                if next_panels is not None and len(next_panels) >= 2:
                    np1, np2 = next_panels[0], next_panels[1]
                    if going_right:
                        entry_panel = np1 if np1.cx < np2.cx else np2
                    else:
                        entry_panel = np1 if np1.cx > np2.cx else np2
                elif next_panels is not None and len(next_panels) == 1:
                    entry_panel = next_panels[0]
                if entry_panel is not None:
                    p1_entry = p1.row == entry_panel.row
                    p2_entry = p2.row == entry_panel.row
                    if p1_entry and not p2_entry:
                        ordered = [p2, p1]
                    elif p2_entry and not p1_entry:
                        ordered = [p1, p2]
                    elif going_right:
                        ordered = [p1, p2] if p2.cx > p1.cx else [p2, p1]
                    else:
                        ordered = [p1, p2] if p2.cx < p1.cx else [p2, p1]
                elif going_right:
                    ordered = [p1, p2] if p2.cx > p1.cx else [p2, p1]
                else:
                    ordered = [p1, p2] if p2.cx < p1.cx else [p2, p1]
            else:
                if abs(next_opt.cy - current_opt.cy) > 20:
                    if next_panels is not None and len(next_panels) >= 2:
                        np1, np2 = next_panels[0], next_panels[1]
                        p1_best = min(abs(p1.cx - np1.cx), abs(p1.cx - np2.cx))
                        p2_best = min(abs(p2.cx - np1.cx), abs(p2.cx - np2.cx))
                        if abs(p1_best - p2_best) < 5:
                            d1 = min(dist(p1, np.cx, np.cy) for np in next_panels)
                            d2 = min(dist(p2, np.cx, np.cy) for np in next_panels)
                            ordered = [p2, p1] if d1 < d2 else [p1, p2]
                        elif p1_best < p2_best:
                            ordered = [p2, p1]
                        else:
                            ordered = [p1, p2]
                    else:
                        d1 = abs(p1.cx - next_opt.cx)
                        d2 = abs(p2.cx - next_opt.cx)
                        ordered = [p2, p1] if d1 <= d2 else [p1, p2]
                else:
                    d1 = dist(p1, next_opt.cx, next_opt.cy)
                    d2 = dist(p2, next_opt.cx, next_opt.cy)
                    ordered = [p2, p1] if d1 <= d2 else [p1, p2]
            dx_dir = next_opt.cx - current_opt.cx
            if abs(dx_dir) > 10:
                h_dir[0] = 1 if dx_dir > 0 else -1
        else:
            ordered = sorted([p1, p2], key=lambda p: (p.cy, p.cx))
        return ordered


def _distance_ordering(p1, p2, last):
    d1 = math.sqrt(_POW(p1.cx - last.cx, 2) + _POW(p1.cy - last.cy, 2))
    d2 = math.sqrt(_POW(p2.cx - last.cx, 2) + _POW(p2.cy - last.cy, 2))
    return [p1, p2] if d1 <= d2 else [p2, p1]


# ---------------------------------------------------------------- entry points

def parse_primitives(prims, base_name="solaredge"):
    """Run the converter and ``GenerateMatrixJsons(base_name)``; returns the parse and matrices."""
    parse = SolarEdgeParse(prims)
    matrices = parse.generate_matrix_jsons(base_name)
    return parse, matrices


def parse_to_golden(parse, matrices):
    """The S2 sections in the golden dump's JSON shape (dict sections keep insertion order)."""
    ratio = parse.detect_optimizer_ratio()
    return {
        "legend_threshold": parse.legend_threshold,
        "panels": [{"id": p.id, "cx": p.cx, "cy": p.cy, "x1": p.x1, "y1": p.y1, "x2": p.x2,
                    "y2": p.y2} for p in parse.panels],
        "optimizers": [{"id": o.id, "cx": o.cx, "cy": o.cy, "color": list(o.color),
                        "panel_ids": list(o.panel_ids), "label": o.label}
                       for o in parse.optimizers],
        "grids": [[p.id for p in grid] for grid in parse.final_grids],
        "has_position_keywords": parse.has_position_keywords,
        "optimizer_ratio": {"type": ratio[0], "frequency": ratio[1], "two_panel": ratio[2],
                            "one_panel": ratio[3]},
        "matrices": matrices,
        "panel_layout": [{"id": p.id, "rot_cx": p.rot_cx, "rot_cy": p.rot_cy, "row": p.row,
                          "col": p.col, "seq": p.seq, "grid": p.grid_id, "hex": p.hex_id}
                         for p in parse.panels],
        "inverter_strings": {str(k): [i.to_json() for i in v]
                             for k, v in parse.inverter_strings.items()},
        "all_string_infos": [i.to_json() for i in parse.all_string_infos],
        "inverter_pdf_colors": {str(k): list(v) for k, v in parse.inverter_pdf_colors.items()},
    }
