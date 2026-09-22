"""Branch2025 response-side parity for Studio's split solar groups.

Sources: GridSplitHelper.cs, BranchCmd.cs and StringPlacement.cs in the
Branch2025 reference checkout. Responses may be JSON objects or JSON strings;
results are detached JSON objects (or the plugin's empty-string sentinel).
This module does not call a solver or access drawing entities.
"""

from copy import deepcopy
import json
import re


def _object(response):
    value = json.loads(response) if isinstance(response, str) else response
    if not isinstance(value, dict):
        raise ValueError("Expected a response object")
    return value


def _try_int(value):
    # Match Int32.TryParse over JToken.ToString(): an integral JSON float prints
    # without a decimal point (0.0 -> "0"), so it parses; 0.5 or "0.0" does not.
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        value = int(value)
    text = str(value).strip()
    if not re.fullmatch(r"[+-]?[0-9]+", text):
        return None
    number = int(text)
    return number if -(2**31) <= number < 2**31 else None


def _int(value):
    return _try_int(value) or 0


def _array(value):
    return value if isinstance(value, list) else []


def is_response_failed(response):
    """GridSplitHelper.IsResponseFailed, lines 109-179 (status is ignored)."""
    try:
        obj = _object(response)
        if obj.get("error") is not None:
            return True
        data = obj.get("data")
        if data is None:
            return True
        if data.get("final_grid") is None or data.get("best_result") is None:
            return True
        if _try_int(data.get("total_valid_solutions")) == 0:
            return True
        return "No valid solutions" in str(data.get("message") or "")
    except (ValueError, TypeError, AttributeError):
        return True


def failed_group_indices(responses, piece_indices):
    """Original group indices with ANY failed or missing piece (lines 185-207)."""
    return [index for index, pieces in enumerate(piece_indices)
            if any(piece >= len(responses) or is_response_failed(responses[piece])
                   for piece in pieces)]


def _panels(grid):
    for row in _array(grid.get("Rows")):
        yield from _array(row.get("Panels"))


def combine_flat_sequences(seq_a, seq_b):
    """Literal CombineFlatSequences (2428-2487), including discarded quantities."""
    counts = {}
    for sequence in (seq_a, seq_b):
        if not isinstance(sequence, list) or len(sequence) < 4:
            continue
        first, second, qty_first, qty_second = map(_int, sequence[:4])
        for length, quantity in ((first, qty_first), (second, qty_second)):
            if length > 0 and quantity > 0:
                counts[length] = counts.get(length, 0) + quantity
    lengths = (sorted(counts, reverse=True) + [0, 0])[:2]
    return [lengths[:], lengths[:]]


def _merge_rows(grid_a, indices_a, grid_b, indices_b, panel_seqs):
    # GridSplitHelper.MergeGridRowsWithTracking, lines 2170-2341.
    rows_a, rows_b = _array(grid_a.get("Rows")), _array(grid_b.get("Rows"))
    if indices_a is None:
        indices_a = list(range(len(rows_a)))
    if indices_b is None:
        indices_b = list(range(len(rows_b)))
    row_count = max([0] + [index + 1 for index in indices_a + indices_b])
    lookup_a = {index: row["Panels"] for index, row in zip(indices_a, rows_a)
                if isinstance(row.get("Panels"), list)}
    lookup_b = {index: row["Panels"] for index, row in zip(indices_b, rows_b)
                if isinstance(row.get("Panels"), list)}
    vertical = bool(set(indices_a).intersection(indices_b))
    width_a = max((len(_array(row.get("Panels"))) for row in rows_a), default=0)
    width_b = max((len(_array(row.get("Panels"))) for row in rows_b), default=0)
    width = width_a + width_b if vertical else max(width_a, width_b)
    merged = []
    for index in range(row_count):
        panels_a, panels_b = lookup_a.get(index), lookup_b.get(index)
        shared = vertical and panels_a is not None and panels_b is not None
        panels = []
        for col in range(width):
            a = b = None
            if shared:
                if col < width_a:
                    a = panels_a[col] if col < len(panels_a) else None
                else:
                    b_col = col - width_a
                    b = panels_b[b_col] if b_col < len(panels_b) else None
            else:
                a = panels_a[col] if panels_a is not None and col < len(panels_a) else None
                b = panels_b[col] if panels_b is not None and col < len(panels_b) else None
            source = next((p for p in (a, b)
                           if p is not None and _int(p.get("Code")) == 1), None)
            identity = source.get("Id") if source is not None else None
            if identity is not None and str(identity) != "":
                identity = str(identity)
                panels.append({"Code": 1, "Id": identity,
                               "Seq": panel_seqs.get(identity, source.get("Seq") or 0)})
            else:
                panels.append({"Code": 0, "Id": "", "Seq": 0})
        merged.append({"Panels": panels})
    return merged


def _distance(value):
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def _combine(response_a, indices_a, response_b, indices_b, tracked=True):
    # Both combine overloads return the first successful piece if merging fails.
    if is_response_failed(response_a):
        return "" if is_response_failed(response_b) else deepcopy(_object(response_b))
    if is_response_failed(response_b):
        return deepcopy(_object(response_a))
    try:
        a, b = deepcopy(_object(response_a)), deepcopy(_object(response_b))
        data_a, data_b = a["data"], b["data"]
        grid_a, grid_b = data_a["final_grid"], data_b["final_grid"]
        # FindMaxSeq examines ALL codes; OffsetSeqNumbers offsets positive Seq only.
        maximum = max([0] + [_int(p.get("Seq")) for p in _panels(grid_a)])
        if maximum:
            for panel in _panels(grid_b):
                sequence = _int(panel.get("Seq"))
                if sequence > 0:
                    panel["Seq"] = sequence + maximum
        panel_seqs = {}
        for grid in (grid_a, grid_b):
            for panel in _panels(grid):
                identity = panel.get("Id")
                if identity is not None and str(identity) and _int(panel.get("Code")) == 1:
                    panel_seqs.setdefault(str(identity), _int(panel.get("Seq")))
        if not tracked:
            indices_a = [_int(i) for i in _array(grid_a.get("RowIndices"))] or None
            indices_b = [_int(i) for i in _array(grid_b.get("RowIndices"))] or None
        rows = _merge_rows(grid_a, indices_a, grid_b, indices_b, panel_seqs)
        # Missing sequence_length contributes NO lengths. Do not synthesize sizes.
        seq_a = _array(data_a["best_result"]["info"].get("sequence_length"))
        seq_b = _array(data_b["best_result"]["info"].get("sequence_length"))
        return {"data": {
            "final_grid": {
                "Dwgname": deepcopy(grid_a.get("Dwgname")), "Rows": rows,
                "Modify": deepcopy(grid_a.get("Modify", [])),
                "Sequences": combine_flat_sequences(grid_a.get("Sequences"),
                                                    grid_b.get("Sequences")),
            },
            "distance_total": _distance(data_a.get("distance_total"))
                              + _distance(data_b.get("distance_total")),
            "best_result": {"info": {"sequence_length": deepcopy(seq_a + seq_b)},
                            "model": "combined"},
        }}
    except (ValueError, TypeError, AttributeError, KeyError, IndexError):
        return deepcopy(_object(response_a))


def restitch(piece_responses, piece_row_indices=None):
    """Restitch one group in emission order, retaining the plugin's merge oddities.

    Pass tracked row indices per piece, or None for the legacy overload that
    reads RowIndices from each response. The normal retry caller first rejects
    entire failed groups with failed_group_indices; this function, like the
    plugin's combine helper, filters failed pieces when called directly.
    """
    if len(piece_responses) == 1:
        response = piece_responses[0]
        try:
            return deepcopy(_object(response))
        except (ValueError, TypeError):
            # The singleton RestitchResponses branch does not validate or parse.
            return deepcopy(response)
    valid = [(response, piece_row_indices[index]
              if piece_row_indices is not None and index < len(piece_row_indices) else None)
             for index, response in enumerate(piece_responses)
             if not is_response_failed(response)]
    if not valid:
        return ""
    response, rows = valid[0]
    response = deepcopy(_object(response))
    for next_response, next_rows in valid[1:]:
        response = _combine(response, rows, next_response, next_rows,
                            tracked=piece_row_indices is not None)
        # GridSplitHelper.cs:1959-1960: the accumulated grid now has all rows.
        rows = None
    return response


def ordered_strings(merged_response):
    """Cut final-grid Seq order as StringPlacement.DrawAllSequencesCore does.

    BranchCmd.cs:2118 replaces grid.Sequences with info.sequence_length.
    StringPlacement.cs:1092-1099 keeps the last length when that list runs out;
    1110-1115 commits the remainder. Thus a trailing partial-beam piece gets
    continued cuts, not a newly computed or balanced sequence allocation.
    Panel Ids stand in for the existing drawing entities on this pure path.
    """
    if not merged_response:
        return []
    data = _object(merged_response)["data"]
    sequences = _array(data["best_result"]["info"].get("sequence_length"))
    by_sequence = {}
    for panel in _panels(data["final_grid"]):
        sequence = _int(panel.get("Seq"))
        if sequence > 0 and panel.get("Id"):
            # ReadSolvedMatrix (683-684, 749-755): first panel for each Seq wins.
            by_sequence.setdefault(sequence, str(panel["Id"]).upper())
    length = (_int(sequences[0]) if sequences else 0) or 6
    result, current, index = [], [], 0
    for sequence in sorted(by_sequence):
        current.append(by_sequence[sequence])
        if len(current) == length:
            result.append(current)
            current = []
            if index < len(sequences):
                index += 1
                next_length = _int(sequences[index]) if index < len(sequences) else 0
                if next_length != 0:
                    length = next_length
    if current:
        result.append(current)
    return result


def escalate(state):
    """Return a copied {jogs, depth, exhausted} retry state (BranchCmd:8135-8156)."""
    result = deepcopy(state)
    if result["jogs"] < 2:
        result["jogs"] = 2
    elif result["depth"] == 10:
        result["depth"] = 1
    elif result["depth"] == 1:
        result["depth"] = 0
    else:
        result["exhausted"] = True
    return result
