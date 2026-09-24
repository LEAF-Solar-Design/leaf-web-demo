"""Port of the plugin's ImportSolarEdgePDF command after the parse: matching and the drawn strings.

Source of truth: Branch2025 ``BranchCmd.cs`` ``ImportSolarEdgePdf`` (18883-19633) and its helpers,
and ``StringPlacement.cs`` ``ReadSolvedMatrix`` / ``DrawAllSequencesCore``. Run on the matrices
:mod:`solar_solaredge_parse` produces and on the drawing's panel groups (PanelGroupCreate's
matrices, ported by :func:`solar_panel_group_kernel.group_matrix`).

S3, the PDF matrices to matchable grids (``BranchCmd.cs:19112-19160``):

- a grid with more than one sub-grid is split: ``IdentifyBridgeStringSeqs`` (every Seq of each
  whole string holding a bridge pair), ``SplitMergedGridIntoSubGrids`` / ``ExtractSubGridByCells``
  (the sub-grid's cells mapped through the 180-degree row turn into its bounding box, other cells
  empty) and ``RemoveBridgePanelsFromSubGrids`` (bridge Seqs set to 0, the sub-grid's remaining
  strings renumbered from 1 by the original string boundaries);
- each drawing group, in selection order, takes the FIRST unused matchable grid whose row count,
  per-row width and Code pattern are identical (``ValidateStructuralMatch``), and the merge takes
  Id from the drawing and Seq, InverterId, StringInputNumber from the PDF.

S4, the strings the plugin draws (``DrawAllSequencesCore``, ``StringPlacement.cs:935``): per match,
Seq 1..max is walked and cut by the match's ``Sequences`` counts, NOT by PDF string membership;
a Seq whose cell holds no drawing entity is skipped (the plugin prints "No panel found"); a
trailing partial string is still drawn. Each string keeps its panels in Seq order, upper-cased,
which is the order ``CableXData`` records (``Cable.cs:210``), and the first panel's InverterId and
StringInputNumber. Then the bridge strings (``BranchCmd.cs:19337-19559``): one single-row string
per bridge string, handles mapped back through the matched sub-grids' cells.

Deliberate divergence, fail closed: where the plugin prints a warning and carries on (a group
with no structural match, a bridge grid with no matched sub-grid, a bridge panel whose handle
cannot be found) this port refuses with a named :class:`SolarEdgeImportError`, and an ambiguous
match (a group whose shape fits more than one unused grid) refuses unless the caller states the
group order is the plugin's own selection order (``selection_order="recorded"``), because the
plugin's first-fit answer then depends on an order the caller may not know.

The group row angle. For block panels the plugin's angle is the block rotation plus the block
definition's rectangle rotation; :func:`lattice_row_angle` recovers it from the centroids when
every panel sits on one lattice, and refuses otherwise.

Contract: pure, deterministic, bounded. Inputs are never mutated (the plugin mutates its own
matrices; every step here works on copies). Matching is one signature lookup per group, so the
whole import is O(total cells) past the kernel's matrix build, with at most ``MAX_GROUPS``
groups, ``MAX_GRIDS`` grids and ``MAX_CELLS`` cells per matrix. Measured on the fixture
(25 groups, 3526 panels): about 0.05 s after the PDF parse.
"""
from __future__ import annotations

import copy
import math
from typing import Any, Mapping, Sequence

try:
    from .solar_panel_group_kernel import group_matrix
except ImportError:
    from solar_panel_group_kernel import group_matrix


MAX_GROUPS = 10_000
MAX_GRIDS = 10_000
MAX_CELLS = 1_000_000
MAX_PANELS = 200_000
# Cells plus points visited by the lattice-angle search; measured 21,680 on the fixture (3526 panels).
MAX_LATTICE_WORK = 20_000_000
SELECTION_ORDERS = ("recorded", "unknown")


class SolarEdgeImportError(ValueError):
    """The import cannot be reproduced without guessing; nothing is returned."""

    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


# ---------------------------------------------------------------- matrix helpers

def _rows(matrix):
    rows = matrix.get("Rows") if isinstance(matrix, Mapping) else None
    if not isinstance(rows, list) or not rows:
        raise SolarEdgeImportError("malformed-matrix", "Rows must be a nonempty list")
    cells = 0
    for row in rows:
        panels = row.get("Panels") if isinstance(row, Mapping) else None
        if not isinstance(panels, list):
            raise SolarEdgeImportError("malformed-matrix", "every row needs a Panels list")
        cells += len(panels)
        for cell in panels:
            if (not isinstance(cell, Mapping) or type(cell.get("Code")) is not int
                    or type(cell.get("Seq", 0)) is not int or cell.get("Seq", 0) < 0):
                raise SolarEdgeImportError("malformed-matrix", "every cell needs an int Code and Seq")
    if cells > MAX_CELLS:
        raise SolarEdgeImportError("bound-exceeded", f"{cells} cells in one matrix")
    return rows


def _sequences(matrix):
    seqs = matrix.get("Sequences") or []
    if not isinstance(seqs, list) or any(type(s) is not int or s < 0 for s in seqs):
        raise SolarEdgeImportError("malformed-matrix", "Sequences must be nonnegative ints")
    return seqs


def _empty_cell():
    # ExtractSubGridByCells' empty cell (BranchCmd.cs:20006).
    return {"Code": 0, "Id": "", "Seq": 0, "InverterId": -1, "StringInputNumber": 0}


def _copy_cell(cell):
    return {"Code": cell["Code"], "Id": cell.get("Id") or "", "Seq": cell.get("Seq", 0),
            "InverterId": cell.get("InverterId", -1),
            "StringInputNumber": cell.get("StringInputNumber", 0)}


def _json_cells(sub_grid, total_rows, total_cols):
    """A sub-grid's pre-rotation cells in the JSON's rotated (row, col) coordinates."""
    cells = sub_grid.get("Cells")
    if not isinstance(cells, list) or not cells:
        # The plugin falls back to ExtractSubGridByBoundingBox; the converter always writes Cells.
        raise SolarEdgeImportError("unsupported-sub-grid", "sub-grid without Cells")
    return {(total_rows - 1 - r, total_cols - 1 - c) for r, c in cells}


# ---------------------------------------------------------------- S3: matchable grids

def identify_bridge_string_seqs(matrix):
    """IdentifyBridgeStringSeqs (BranchCmd.cs:20084): every Seq of each string holding a bridge."""
    bridge = set()
    connections = matrix.get("BridgeConnections") or []
    if not connections:
        return bridge
    rows = _rows(matrix)
    seq_by_pos = {}
    for r, row in enumerate(rows):
        for c, cell in enumerate(row["Panels"]):
            if cell["Code"] == 1 and cell.get("Seq", 0) > 0:
                seq_by_pos[(r, c)] = cell["Seq"]
    total_rows = len(rows)
    total_cols = len(rows[0]["Panels"])
    sequences = _sequences(matrix)
    for link in connections:
        from_seq = seq_by_pos.get((total_rows - 1 - link["FromRow"], total_cols - 1 - link["FromCol"]), 0)
        to_seq = seq_by_pos.get((total_rows - 1 - link["ToRow"], total_cols - 1 - link["ToCol"]), 0)
        if from_seq == 0 or to_seq == 0:
            continue
        current = 1
        for length in sequences:
            start, end = current, current + length - 1
            if start <= from_seq <= end or start <= to_seq <= end:
                bridge.update(range(start, end + 1))
            current += length
    return bridge


def extract_sub_grid_by_cells(matrix, sub_grid):
    """ExtractSubGridByCells (BranchCmd.cs:19966): the cells' bounding box, others emptied."""
    rows = _rows(matrix)
    total_rows, total_cols = len(rows), len(rows[0]["Panels"])
    cells = _json_cells(sub_grid, total_rows, total_cols)
    min_row = min(r for r, _ in cells)
    max_row = max(r for r, _ in cells)
    min_col = min(c for _, c in cells)
    max_col = max(c for _, c in cells)
    out_rows = []
    for r in range(min_row, max_row + 1):
        if r < 0 or r >= len(rows):
            continue
        source = rows[r]["Panels"]
        out = []
        for c in range(min_col, max_col + 1):
            if c < 0 or c >= len(source):
                continue
            out.append(_copy_cell(source[c]) if (r, c) in cells else _empty_cell())
        out_rows.append({"Panels": out})
    return {"Dwgname": f"{matrix.get('Dwgname', '')}_subgrid{sub_grid['Id']}",
            "Sequences": list(_sequences(matrix)), "Rows": out_rows}


def remove_bridge_panels(sub_json, bridge_seqs, original_sequences):
    """RemoveBridgePanelsFromSubGrids (BranchCmd.cs:20266) on one sub-grid, in place."""
    present = set()
    for row in sub_json["Rows"]:
        for cell in row["Panels"]:
            if cell["Code"] == 1 and cell["Seq"] > 0 and cell["Seq"] not in bridge_seqs:
                present.add(cell["Seq"])
    for row in sub_json["Rows"]:
        for cell in row["Panels"]:
            if cell["Code"] == 1 and cell["Seq"] in bridge_seqs:
                cell["Seq"] = 0
    boundaries = []
    current = 1
    for length in original_sequences:
        boundaries.append((current, current + length - 1, length))
        current += length
    new_sequences = []
    counter = 1
    for start, end, length in boundaries:
        seqs = range(start, end + 1)
        if not any(s in present for s in seqs) or any(s in bridge_seqs for s in seqs):
            continue
        new_sequences.append(length)
        shift = counter - start
        for row in sub_json["Rows"]:
            for cell in row["Panels"]:
                if cell["Code"] == 1 and start <= cell["Seq"] <= end:
                    cell["Seq"] += shift
        counter += length
    sub_json["Sequences"] = new_sequences


def matchable_grids(matrices: Sequence[Mapping[str, Any]]):
    """BranchCmd.cs:19112-19160: (matchable grids, bridge data), neither aliasing the input.

    matchable: ``[{"json", "original_index", "sub_grid_id"}]`` in the plugin's order.
    bridge data: ``[{"original_index", "original", "bridge_seqs", "bridge_panels"}]`` where
    ``bridge_panels`` is ``[(orig_seq, row, col)]`` in row-major scan order.
    """
    if not isinstance(matrices, (list, tuple)) or len(matrices) > MAX_GRIDS:
        raise SolarEdgeImportError("malformed-matrices", "matrices must be a bounded list")
    matchable, bridges = [], []
    for index, source in enumerate(matrices):
        matrix = copy.deepcopy(source)
        rows = _rows(matrix)
        sub_grids = matrix.get("SubGrids") or []
        if len(sub_grids) > 1:
            bridge_seqs = identify_bridge_string_seqs(matrix)
            if bridge_seqs:
                panels = [(cell["Seq"], r, c) for r, row in enumerate(rows)
                          for c, cell in enumerate(row["Panels"])
                          if cell["Code"] == 1 and cell["Seq"] in bridge_seqs]
                bridges.append({"original_index": index, "original": matrix,
                                "bridge_seqs": bridge_seqs, "bridge_panels": panels})
            for sub_grid in sub_grids:
                sub_json = extract_sub_grid_by_cells(matrix, sub_grid)
                remove_bridge_panels(sub_json, bridge_seqs, _sequences(matrix))
                matchable.append({"json": sub_json, "original_index": index,
                                  "sub_grid_id": sub_grid["Id"]})
        else:
            matchable.append({"json": matrix, "original_index": index, "sub_grid_id": -1})
    return matchable, bridges


# ---------------------------------------------------------------- S3: drawing groups and match

def drawing_group_matrix(group_panels, row_angle, alignment_tolerance):
    """PrecomputeGroup's MatrixJson (BranchCmdCore.cs:244-266): Code 1 and the handle, or 0."""
    matrix = group_matrix(group_panels, row_angle, alignment_tolerance)
    return {"Rows": [{"Panels": [{"Code": 1 if h is not None else 0, "Id": h or ""} for h in row]}
                     for row in matrix]}


def structural_match(drawing, pdf):
    """ValidateStructuralMatch (BranchCmd.cs:20384): row count, per-row width, Code pattern."""
    return shape_signature(drawing) == shape_signature(pdf)


def shape_signature(matrix):
    """Everything ValidateStructuralMatch compares, as one hashable value.

    Two matrices match exactly when their signatures are equal, so the first-fit scan over the
    matchable grids becomes one dict lookup per group (O(total cells), no groups x grids pass).
    """
    return tuple(tuple(cell["Code"] for cell in row["Panels"]) for row in matrix["Rows"])


def lattice_row_angle(centres, *, spread_tolerance=1e-6):
    """The shared panel angle, reconstructed from the centroid lattice; fails closed.

    The plugin's group row angle is the panel's angle (PanelGroup ctor, PanelGroup.cs:25), and
    for a block panel that is ``br.Rotation`` plus the block definition's rectangle rotation
    (``PanelBlockDefinition`` mBaseAngle, AcadCommandBaseCore.cs:505). An intake that records
    only centroids and block rotation cannot give the second term. When every panel's
    nearest-neighbour vector folds (mod 90 degrees) to the same direction within
    ``spread_tolerance`` radians, that direction IS the lattice's rotation, and the long side
    runs along it or across it; this returns the angle in [-pi/4, pi/4) and the caller picks
    the axis family from the definition's extents. Any spread above the tolerance refuses:
    the panels do not share one orientation and no single angle may be guessed.
    """
    points = [(float(x), float(y)) for x, y in centres]
    if len(points) < 2 or len(points) > MAX_PANELS:
        raise SolarEdgeImportError("lattice-angle-unavailable", f"{len(points)} panels")
    if not all(math.isfinite(c) for p in points for c in p):
        raise SolarEdgeImportError("lattice-angle-unavailable", "non-finite centroid")
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
    # About one point per cell on a uniform layout.
    size = span / max(int(len(points) ** 0.5), 1)
    cells_across = int(span / size) + 2
    buckets = {}
    for i, (x, y) in enumerate(points):
        buckets.setdefault((int(x // size), int(y // size)), []).append(i)
    folded = []
    work = 0
    for i, (x, y) in enumerate(points):
        gx, gy = int(x // size), int(y // size)
        best = None
        ring = 0
        # Grow the searched square ring by ring. A point outside ring r is at least r * size
        # away, so the search stops once the best distance is within that bound.
        while ring <= cells_across and (best is None or best[0] > (ring * size) ** 2):
            for cx in range(gx - ring, gx + ring + 1):
                for cy in range(gy - ring, gy + ring + 1):
                    if max(abs(cx - gx), abs(cy - gy)) != ring:
                        continue
                    bucket = buckets.get((cx, cy), ())
                    work += 1 + len(bucket)
                    if work > MAX_LATTICE_WORK:
                        raise SolarEdgeImportError("bound-exceeded", "lattice angle search")
                    for j in bucket:
                        if j != i:
                            d = (points[j][0] - x) ** 2 + (points[j][1] - y) ** 2
                            if best is None or d < best[0]:
                                best = (d, j)
            ring += 1
        if best is None or best[0] == 0.0:
            raise SolarEdgeImportError("lattice-angle-unavailable", "coincident or isolated panels")
        j = best[1]
        angle = math.atan2(points[j][1] - y, points[j][0] - x)
        folded.append((angle + math.pi / 4) % (math.pi / 2) - math.pi / 4)
    folded.sort()
    if folded[-1] - folded[0] > spread_tolerance:
        raise SolarEdgeImportError(
            "lattice-angle-ambiguous",
            f"nearest-neighbour directions spread {folded[-1] - folded[0]:.3g} rad")
    return folded[len(folded) // 2]


def merge(drawing, pdf):
    """MergePdfSolutionWithPanelGroup (BranchCmd.cs:20409): Id from the drawing, the rest from the PDF."""
    rows = []
    for ra, rb in zip(drawing["Rows"], pdf["Rows"]):
        rows.append({"Panels": [{"Code": ca["Code"], "Id": ca["Id"], "Seq": cb["Seq"],
                                 "InverterId": cb["InverterId"],
                                 "StringInputNumber": cb["StringInputNumber"]}
                                for ca, cb in zip(ra["Panels"], rb["Panels"])]})
    return {"Sequences": list(pdf.get("Sequences") or []), "Rows": rows}


def draw_sequences(merged, entity_handles):
    """ReadSolvedMatrix + DrawAllSequencesCore (StringPlacement.cs:640-755, 935-1117).

    Returns ``[{"panels", "inverter_id", "string_input_number", "partial"}]`` in draw order.
    ``entity_handles`` is the set of upper-case drawing panel handles (the plugin's
    ``p.mHasEntity``). The first cell holding a Seq wins; Seq 0 is ignored.
    """
    by_seq = {}
    for row in merged["Rows"]:
        for cell in row["Panels"]:
            seq = cell["Seq"]
            if seq != 0 and seq not in by_seq:
                by_seq[seq] = cell
    sequences = list(merged["Sequences"])
    per_string = sequences[0] if sequences else 0
    if per_string == 0:
        per_string = 6
    last = max(by_seq) if by_seq else 0
    strings, current, first = [], [], None
    for seq in range(1, last + 1):
        cell = by_seq.get(seq)
        if cell is None:
            continue
        handle = (cell["Id"] or "").strip().upper()
        if handle not in entity_handles:
            continue  # "No panel found for Seq" (StringPlacement.cs:1103)
        current.append(handle)
        if len(current) == 1:
            first = cell
        if len(current) == per_string:
            strings.append({"panels": current, "inverter_id": first["InverterId"],
                            "string_input_number": first["StringInputNumber"], "partial": False})
            current, first = [], None
            if sequences:
                sequences.pop(0)
                following = sequences[0] if sequences else 0
                if following != 0:
                    per_string = following
    if current:
        strings.append({"panels": current, "inverter_id": first["InverterId"],
                        "string_input_number": first["StringInputNumber"], "partial": True})
    return strings


def _group_bridge_panels(sequences, bridge_seqs, bridge_panels):
    """GroupBridgePanelsByString (BranchCmd.cs:19899)."""
    by_seq = {}
    for panel in bridge_panels:
        if panel[0] in by_seq:
            # ToDictionary throws on a duplicate key; the plugin's command dies here.
            raise SolarEdgeImportError("duplicate-bridge-seq", str(panel[0]))
        by_seq[panel[0]] = panel
    result = []
    current = 1
    for length in sequences:
        seqs = range(current, current + length)
        if any(s in bridge_seqs for s in seqs):
            found = [by_seq[s] for s in seqs if s in by_seq]
            if found:
                result.append(found)
        current += length
    return result


def run_import(matrices, groups, panels, *, row_angle=0.0, alignment_tolerance,
               selection_order="unknown"):
    """S3 and S4 end to end.

    ``groups``: the drawing's panel groups in the plugin's selection order, each
    ``{"block": handle, "panels": [panel handle, ...]}`` (member order as recorded).
    ``panels``: ``[{"handle", "x", "y"}]``, the drawing's panel centroids.
    Returns ``{"matches", "strings", "bridge_strings", "unassigned"}``; every string is
    ``{"panels", "inverter_id", "string_input_number", "partial", "source"}``.
    """
    if selection_order not in SELECTION_ORDERS:
        raise SolarEdgeImportError("invalid-selection-order", repr(selection_order))
    if not isinstance(groups, (list, tuple)) or not groups or len(groups) > MAX_GROUPS:
        raise SolarEdgeImportError("malformed-groups", "groups must be a nonempty bounded list")
    if not isinstance(panels, (list, tuple)) or len(panels) > MAX_PANELS:
        raise SolarEdgeImportError("malformed-panels", "panels must be a bounded list")
    centre = {}
    for panel in panels:
        handle = panel.get("handle") if isinstance(panel, Mapping) else None
        if not isinstance(handle, str) or not handle:
            raise SolarEdgeImportError("malformed-panels", "every panel needs a handle")
        key = handle.upper()
        if key in centre:
            raise SolarEdgeImportError("duplicate-panel", key)
        x, y = panel.get("x"), panel.get("y")
        if type(x) not in (int, float) or type(y) not in (int, float):
            raise SolarEdgeImportError("malformed-panels", f"panel {key} needs numeric x and y")
        centre[key] = (float(x), float(y))
    entity_handles = set(centre)

    grids, bridge_data = matchable_grids(matrices)
    # ValidateStructuralMatch as a lookup: signature -> matchable indices in plugin order.
    by_shape = {}
    for i, grid in enumerate(grids):
        by_shape.setdefault(shape_signature(grid["json"]), []).append(i)
    used = set()
    matches = []
    by_original = {}
    strings = []
    grouped = []
    for gi, group in enumerate(groups):
        members = group.get("panels") if isinstance(group, Mapping) else None
        if not isinstance(members, list) or not members:
            raise SolarEdgeImportError("malformed-groups", f"group {gi} has no panels")
        missing = [h for h in members if not isinstance(h, str) or h.upper() not in centre]
        if missing:
            raise SolarEdgeImportError("unknown-group-panel", f"group {gi}: {missing[:3]}")
        grouped.extend(h.upper() for h in members)
        drawing = drawing_group_matrix(
            [{"handle": h.upper(), "centre": centre[h.upper()]} for h in members],
            row_angle, alignment_tolerance)
        candidates = [i for i in by_shape.get(shape_signature(drawing), ()) if i not in used]
        if not candidates:
            raise SolarEdgeImportError(
                "no-structural-match",
                f"group {gi} ({group.get('block')}, {len(members)} panels) fits no unused PDF grid")
        if len(candidates) > 1 and selection_order != "recorded":
            raise SolarEdgeImportError(
                "ambiguous-structural-match",
                f"group {gi} ({group.get('block')}) fits PDF grids {candidates}; the plugin takes "
                "the first in its selection order, so this is reproducible only with "
                "selection_order='recorded' and the plugin's own order")
        index = candidates[0]
        merged = merge(drawing, grids[index]["json"])
        drawn = draw_sequences(merged, entity_handles)
        used.add(index)
        grid = grids[index]
        match = {"group_index": gi, "block": group.get("block"), "grid_index": index,
                 "original_index": grid["original_index"], "sub_grid_id": grid["sub_grid_id"],
                 "candidates": candidates, "merged": merged}
        matches.append(match)
        by_original.setdefault(grid["original_index"], []).append(match)
        for s in drawn:
            s["source"] = {"kind": "group", "block": group.get("block"), "group_index": gi}
            strings.append(s)

    bridge_strings = []
    for data in bridge_data:
        original = data["original"]
        found = by_original.get(data["original_index"])
        if not found:
            raise SolarEdgeImportError(
                "bridge-grid-unmatched",
                f"PDF grid {data['original_index'] + 1} has bridge strings but no matched sub-grid")
        rows = original["Rows"]
        total_rows, total_cols = len(rows), len(rows[0]["Panels"])
        lookup = {}
        for match in found:
            sub_grid = next((sg for sg in original.get("SubGrids") or []
                             if sg["Id"] == match["sub_grid_id"]), None)
            if sub_grid is None or not sub_grid.get("Cells"):
                continue
            cells = _json_cells(sub_grid, total_rows, total_cols)
            min_row = min(r for r, _ in cells)
            min_col = min(c for _, c in cells)
            for mr, row in enumerate(match["merged"]["Rows"]):
                for mc, cell in enumerate(row["Panels"]):
                    if cell["Code"] == 1 and cell["Id"]:
                        pos = (min_row + mr, min_col + mc)
                        if pos in cells:
                            lookup[pos] = cell["Id"]
        ordered = sorted(data["bridge_panels"], key=lambda p: p[0])  # OrderBy: stable
        for members in _group_bridge_panels(_sequences(original), data["bridge_seqs"], ordered):
            _, r0, c0 = members[0]
            inverter_id, string_input = -1, 0
            if 0 <= r0 < total_rows and 0 <= c0 < len(rows[r0]["Panels"]):
                first = rows[r0]["Panels"][c0]
                inverter_id, string_input = first["InverterId"], first["StringInputNumber"]
            handles = []
            for seq, r, c in members:
                handle = lookup.get((r, c), "")
                if not handle:
                    raise SolarEdgeImportError(
                        "bridge-handle-missing",
                        f"PDF grid {data['original_index'] + 1} bridge panel Seq {seq} at ({r}, {c})")
                handles.append(handle)
            bridge = {"Sequences": [len(handles)],
                      "Rows": [{"Panels": [{"Code": 1, "Id": h, "Seq": i + 1,
                                            "InverterId": inverter_id,
                                            "StringInputNumber": string_input}
                                           for i, h in enumerate(handles)]}]}
            for s in draw_sequences(bridge, entity_handles):
                s["source"] = {"kind": "bridge", "original_index": data["original_index"]}
                bridge_strings.append(s)

    assigned = {h for s in strings + bridge_strings for h in s["panels"]}
    unassigned = sorted(set(grouped) - assigned, key=lambda h: int(h, 16))
    return {"matches": matches, "strings": strings, "bridge_strings": bridge_strings,
            "unassigned": unassigned, "matchable_count": len(grids),
            "bridge_grid_count": len(bridge_data)}
