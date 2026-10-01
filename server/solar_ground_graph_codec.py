"""Compact Ground tracker slots for the solar design graph (codec leaf.solar-ground-slots.v1).

A Ground frame converted from one single-axis tracker is one matrix row of module_slots panels
(LEAFTRACKERSTOPANELGROUPS direct path, solar_ground_dsteps.tracker_panel_layout). Expanded into
v1 entities each slot costs at least 59 graph nodes plus its panel's provenance, validity and
extra, so the full b18 site (237 trackers, 69,678 slots) is about 5.9 M nodes and 71 MB, twelve
times the validator's 500,000 node limit. This codec keeps a frame's slots as ONE block:

    frame["ground_slots"] = {
        "codec": "leaf.solar-ground-slots.v1",
        "count": N,                    # 1 <= N <= MAX_FRAME_SLOTS, slot order
        "panel_ids": "<base64>",       # N x 16 bytes: each panel id's UUIDv4 bytes
        "centres": "<base64>",         # N x 16 bytes: little-endian float64 x then y, finite
        "angle": <number>,             # every slot panel's angle and matrix cell angle
        "panel": {"rev", "provenance", "validity", "extra"},   # shared by every slot panel
    }

and derives every redundant slot view (panel assignment, matrix cells, panel assignments,
sequences) from the graph's strings and inverters, exactly as
solar_solve_results.sync_assignments writes them. A compact frame keeps `panel_refs`, `matrix`,
`panel_assignments` and `sequences` as empty lists, and none of its slot panels is in
graph["panels"]; expanded, its slot panels follow every other panel, frames in graph order.

Contract (fails closed; every refusal is a payload-free GraphValidationError):
- INVALID_GROUND_SLOTS: a malformed block, row or template, a frame that disagrees with its
  block, or a frame compact_graph cannot encode without changing a value.
- GRAPH_LIMIT_EXCEEDED: a count over MAX_FRAME_SLOTS or MAX_GRAPH_SLOTS, or an expansion whose
  node or size floor exceeds the validator's MAX_NODES or MAX_BYTES. Both are decided from the
  counts and each row string's exact expected length BEFORE any row is decoded or any slot is
  built, so an expansion bomb costs one pass over the compact graph.
- DUPLICATE_APPLICATION_ID: a panel id repeated within or across blocks.
- expand_graph(compact_graph(g, ids)) is canonically byte-identical to g; compact_graph refuses
  rather than drop or change a value. A graph without blocks expands to an equal copy.
Pure functions: no I/O, no solver, no validator or schema edits. Validity of an expanded graph
stays solar_design_graph.validate_graph's job.
"""
from __future__ import annotations

import base64
import binascii
import copy
import json
import math
import re
import struct
from typing import NamedTuple

try:
    from . import solar_design_graph as _graph
    from .solar_design_graph import GraphValidationError, _bounded_json
except ImportError:
    import solar_design_graph as _graph
    from solar_design_graph import GraphValidationError, _bounded_json

CODEC = "leaf.solar-ground-slots.v1"
SLOTS_KEY = "ground_slots"
# Expanded, a frame is one matrix row: the schema's matrix row bound ($defs.frame.matrix items).
MAX_FRAME_SLOTS = 10000
# Expanded, every slot is a graph panel: the schema's panels bound (properties.panels.maxItems).
MAX_GRAPH_SLOTS = 100000
ROW_BYTES = 16
BLOCK_KEYS = ("codec", "count", "panel_ids", "centres", "angle", "panel")
TEMPLATE_KEYS = ("rev", "provenance", "validity", "extra")
MAX_REV = 1000000
# Nodes one unassigned expanded slot costs beyond its template's provenance, validity and extra,
# in _bounded_json accounting (one node per value and per object key): the panel entity 30
# (dict 1, 11 keys, id, kind, rev, frame_ref, matrix_cell 5, centre 3, angle, assignment 5),
# its panel_refs entry 1, its matrix cell 17 (dict 1, 8 keys, 8 values) and its panel
# assignment 11 (dict 1, 5 keys, 5 values). Assignment only adds sequence entries.
SLOT_FLOOR_NODES = 59
_PANEL_ID = re.compile(
    r"leaf:panel:([0-9a-f]{8})-([0-9a-f]{4})-(4[0-9a-f]{3})-([89ab][0-9a-f]{3})-([0-9a-f]{12})")
_DERIVED_LISTS = ("panel_refs", "matrix", "panel_assignments", "sequences")


class SlotTable(NamedTuple):
    """One decoded block: ids in slot order, centres flat (x0, y0, x1, y1, ...)."""
    ids: tuple
    centres: tuple
    angle: object
    panel: dict


def _invalid():
    return GraphValidationError("INVALID_GROUND_SLOTS")


def row_string_length(count: int) -> int:
    """Exact length of the padded standard base64 text of `count` 16-byte rows."""
    return (count * ROW_BYTES + 2) // 3 * 4


def json_cost(value) -> tuple[int, int]:
    """(nodes, size) of a JSON value in _bounded_json's accounting: one node per value and per
    object key; size is the UTF-8 length of every string and key, 24 per number and 5 per null
    or bool. Iterative. Call only on a value _bounded_json admitted; it checks nothing itself."""
    nodes = size = 0
    stack = [value]
    while stack:
        item = stack.pop()
        nodes += 1
        kind = type(item)
        if kind is dict:
            for key, child in item.items():
                nodes += 1
                size += len(key.encode("utf-8"))
                stack.append(child)
        elif kind is list:
            stack.extend(item)
        elif kind is str:
            size += len(item.encode("utf-8"))
        elif kind is int or kind is float:
            size += 24
        else:
            size += 5
    return nodes, size


def _is_number(value) -> bool:
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is int:
        try:
            return math.isfinite(float(value))
        except OverflowError:
            return False
    return False


def _check_template(panel) -> None:
    if type(panel) is not dict or set(panel) != set(TEMPLATE_KEYS):
        raise _invalid()
    rev = panel["rev"]
    if type(rev) is not int or not 0 <= rev <= MAX_REV:
        raise _invalid()
    if any(type(panel[key]) is not dict for key in ("provenance", "validity", "extra")):
        raise _invalid()
    # The validator's iterative bound admits the template before any copy or traversal.
    _bounded_json(panel)


def _check_count(count) -> int:
    if type(count) is not int or count < 1:
        raise _invalid()
    if count > MAX_FRAME_SLOTS:
        raise GraphValidationError("GRAPH_LIMIT_EXCEEDED")
    return count


def _check_block_shape(block) -> int:
    """Everything decidable without decoding a row: keys, codec, count and row string lengths."""
    if type(block) is not dict or set(block) != set(BLOCK_KEYS) or block["codec"] != CODEC:
        raise _invalid()
    count = _check_count(block["count"])
    expected = row_string_length(count)
    for key in ("panel_ids", "centres"):
        text = block[key]
        if type(text) is not str or len(text) != expected:
            raise _invalid()
    if not _is_number(block["angle"]):
        raise _invalid()
    _check_template(block["panel"])
    return count


def _rows(text: str, count: int) -> bytes:
    """Decode one row string whose length _check_block_shape already proved; canonical only."""
    try:
        raw = base64.b64decode(text.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeError):
        raise _invalid() from None
    if len(raw) != count * ROW_BYTES or base64.b64encode(raw).decode("ascii") != text:
        raise _invalid()
    return raw


def decode_slots(block) -> SlotTable:
    """Decode one block, bounded: shape and lengths first, then rows. Returns a SlotTable whose
    `panel` is the block's own template (not copied)."""
    count = _check_block_shape(block)
    ids_hex = _rows(block["panel_ids"], count).hex()
    ids = []
    for start in range(0, count * 32, 32):
        h = ids_hex[start:start + 32]
        if h[12] != "4" or h[16] not in "89ab":
            raise _invalid()
        ids.append(f"leaf:panel:{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}")
    if len(set(ids)) != count:
        raise GraphValidationError("DUPLICATE_APPLICATION_ID")
    centres = struct.unpack(f"<{2 * count}d", _rows(block["centres"], count))
    if not all(map(_is_number, centres)):
        raise _invalid()
    return SlotTable(tuple(ids), centres, block["angle"], block["panel"])


def encode_slots(panel_ids, centres, angle, panel) -> dict:
    """The block for one frame's slots, in slot order. Writers and compact_graph use it. Refuses
    anything decode_slots would not reproduce exactly: a panel id outside the schema's lowercase
    UUIDv4 panel id form, a centre that is not two finite floats, a non-finite angle, a template
    outside TEMPLATE_KEYS. Deterministic; the template is deep-copied."""
    if type(panel_ids) not in (list, tuple) or type(centres) not in (list, tuple):
        raise _invalid()
    count = _check_count(len(panel_ids))
    if len(centres) != count or not _is_number(angle):
        raise _invalid()
    _check_template(panel)
    id_rows = bytearray()
    for panel_id in panel_ids:
        match = _PANEL_ID.fullmatch(panel_id) if type(panel_id) is str else None
        if match is None:
            raise _invalid()
        id_rows += bytes.fromhex("".join(match.groups()))
    if len(set(panel_ids)) != count:
        raise GraphValidationError("DUPLICATE_APPLICATION_ID")
    flat = []
    for centre in centres:
        if (type(centre) not in (list, tuple) or len(centre) != 2
                or any(type(v) is not float or not math.isfinite(v) for v in centre)):
            raise _invalid()
        flat.extend(centre)
    return {"codec": CODEC, "count": count,
            "panel_ids": base64.b64encode(bytes(id_rows)).decode("ascii"),
            "centres": base64.b64encode(struct.pack(f"<{2 * count}d", *flat)).decode("ascii"),
            "angle": angle, "panel": copy.deepcopy(panel)}


def compact_frame_ids(graph) -> list:
    """Ids of the frames carrying a block, in frame order."""
    frames = graph.get("frames") if type(graph) is dict else None
    if type(frames) is not list:
        return []
    return [frame.get("id") for frame in frames if type(frame) is dict and SLOTS_KEY in frame]


def _compact_frames(graph) -> list:
    """(frame, count) for every block, with every frame agreement and both slot bounds checked
    and no row decoded."""
    frames = graph.get("frames")
    if type(frames) is not list:
        return []
    found, seen, total = [], set(), 0
    for frame in frames:
        if type(frame) is not dict or SLOTS_KEY not in frame:
            continue
        count = _check_block_shape(frame[SLOTS_KEY])
        tracker = frame.get("tracker")
        sizes = (frame.get("module_rows"), frame.get("module_columns"), frame.get("module_slots"),
                 tracker.get("module_slots") if type(tracker) is dict else None)
        if (frame.get("installation_design") != "Ground" or type(tracker) is not dict
                or any(type(value) is not int for value in sizes) or sizes != (1, count, count, count)
                or any(type(frame.get(key)) is not list or frame[key] for key in _DERIVED_LISTS)
                or type(frame.get("id")) is not str):
            raise _invalid()
        if frame["id"] in seen:
            raise GraphValidationError("DUPLICATE_APPLICATION_ID")
        seen.add(frame["id"])
        total += count
        if total > MAX_GRAPH_SLOTS:
            raise GraphValidationError("GRAPH_LIMIT_EXCEEDED")
        found.append((frame, count))
    return found


def decode_graph_slots(graph) -> dict:
    """{frame id: SlotTable} for every compact frame, in frame order. Admits the graph with
    _bounded_json, checks every frame agreement and the graph slot bound before decoding any row,
    then refuses a panel id repeated across blocks."""
    _bounded_json(graph)
    if type(graph) is not dict:
        raise GraphValidationError("INVALID_GRAPH")
    tables, ids = {}, set()
    for frame, count in _compact_frames(graph):
        table = decode_slots(frame[SLOTS_KEY])
        ids.update(table.ids)
        tables[frame["id"]] = table
    if len(ids) != sum(len(table.ids) for table in tables.values()):
        raise GraphValidationError("DUPLICATE_APPLICATION_ID")
    return tables


def expansion_floor(graph) -> tuple[int, int]:
    """(nodes, size) no expansion of this admitted compact graph can go below. Reads only counts
    and templates; decodes no row. Nodes equal the expanded count for an unassigned graph;
    assignments and sequences only add nodes. Size stays a lower bound."""
    nodes, size = json_cost(graph)
    for frame, count in _compact_frames(graph):
        block_nodes, block_size = json_cost(frame[SLOTS_KEY])
        nodes -= 1 + block_nodes
        nodes += 1  # The expanded matrix gains one row list.
        size -= len(SLOTS_KEY.encode("utf-8")) + block_size
        template = frame[SLOTS_KEY]["panel"]
        t_nodes = t_size = 0
        for key in ("provenance", "validity", "extra"):
            n, s = json_cost(template[key])
            t_nodes += n
            t_size += s
        nodes += count * (SLOT_FLOOR_NODES + t_nodes)
        size += count * t_size
    return nodes, size


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def expand_graph(graph) -> dict:
    """The exact v1 view of a compact graph: every block replaced by its slot panels, panel_refs,
    matrix row, panel assignments and sequences. A graph without blocks returns an equal deep
    copy. Refuses GRAPH_LIMIT_EXCEEDED from expansion_floor before any row is decoded, and again
    if the built graph fails _bounded_json. The input is never mutated or aliased."""
    _bounded_json(graph)
    if type(graph) is not dict:
        raise GraphValidationError("INVALID_GRAPH")
    if not _compact_frames(graph):
        return copy.deepcopy(graph)
    floor_nodes, floor_size = expansion_floor(graph)
    if floor_nodes > _graph.MAX_NODES or floor_size > _graph.MAX_BYTES:
        raise GraphValidationError("GRAPH_LIMIT_EXCEEDED")
    tables = decode_graph_slots(graph)
    result = copy.deepcopy(graph)
    try:
        members = {}
        for string in result["strings"]:
            for seq, ref in enumerate(string["ordered_panel_refs"]):
                members[ref] = (string["id"], seq)
        inputs = {assignment["string_ref"]: (inverter["id"], assignment["input_number"])
                  for inverter in result["inverters"] for assignment in inverter["input_assignments"]}
        slot_frame = {}
        for frame_id, table in tables.items():
            for panel_id in table.ids:
                slot_frame[panel_id] = frame_id
        sequences = {frame_id: [] for frame_id in tables}
        for string in result["strings"]:
            runs = {}
            for ref in string["ordered_panel_refs"]:
                frame_id = slot_frame.get(ref)
                if frame_id is not None:
                    runs.setdefault(frame_id, []).append(ref)
            for frame_id, refs in runs.items():
                sequences[frame_id].append({"string_ref": string["id"], "ordered_panel_refs": refs})
        panels = result["panels"]
        if type(panels) is not list:
            raise TypeError()
        for frame in result["frames"]:
            if type(frame) is not dict or SLOTS_KEY not in frame:
                continue
            table = tables[frame["id"]]
            template, angle, xy = table.panel, table.angle, table.centres
            cells, records = [], []
            for col, panel_id in enumerate(table.ids):
                string_ref, seq = members.get(panel_id, (None, None))
                inverter_id, input_number = inputs.get(string_ref, (None, None))
                x, y = xy[2 * col], xy[2 * col + 1]
                panels.append({
                    "id": panel_id, "kind": "panel", "rev": template["rev"],
                    "provenance": copy.deepcopy(template["provenance"]),
                    "extra": copy.deepcopy(template["extra"]),
                    "validity": copy.deepcopy(template["validity"]),
                    "frame_ref": frame["id"], "matrix_cell": {"row": 0, "col": col},
                    "centre": [x, y], "angle": angle,
                    "assignment": {"string_ref": string_ref, "seq": seq},
                })
                cells.append({"code": "panel", "panel_ref": panel_id, "seq": seq,
                              "inverter_id": inverter_id, "string_input_number": input_number,
                              "x": x, "y": y, "angle": angle})
                records.append({"panel_ref": panel_id, "string_ref": string_ref, "seq": seq,
                                "inverter_id": inverter_id, "string_input_number": input_number})
            frame["panel_refs"] = list(table.ids)
            frame["matrix"] = [cells]
            frame["panel_assignments"] = records
            frame["sequences"] = sequences[frame["id"]]
            del frame[SLOTS_KEY]
    except (KeyError, TypeError, AttributeError):
        raise _invalid() from None
    _bounded_json(result)
    return result


def compact_graph(graph, frame_ids) -> dict:
    """The compact graph whose expansion is canonically identical to `graph`, with exactly the
    named frames compacted. Each named frame must be a Ground tracker frame of one matrix row whose
    slot panels form, frames in graph order, the tail of graph["panels"], share one rev,
    provenance, validity, extra and angle, have float centres, and carry exactly the views
    expand_graph derives; otherwise INVALID_GROUND_SLOTS and nothing is returned. An empty list
    returns an equal deep copy. The input is never mutated or aliased."""
    try:
        _bounded_json(graph)
    except GraphValidationError as error:
        if error.code == "GRAPH_LIMIT_EXCEEDED":
            raise _invalid() from None
        raise
    if type(graph) is not dict:
        raise GraphValidationError("INVALID_GRAPH")
    if (type(frame_ids) not in (list, tuple) or any(type(item) is not str for item in frame_ids)
            or len(set(frame_ids)) != len(frame_ids)):
        raise _invalid()
    if not frame_ids:
        return copy.deepcopy(graph)
    try:
        frames, panels = graph["frames"], graph["panels"]
        named = set(frame_ids)
        positions = [index for index, frame in enumerate(frames) if frame["id"] in named]
        if (len(positions) != len(named) or len({frames[i]["id"] for i in positions}) != len(named)
                or any(SLOTS_KEY in frames[i] for i in positions)):
            raise _invalid()
        runs = [frames[i]["panel_refs"] for i in positions]
        total = sum(len(run) for run in runs)
        if total == 0 or total > len(panels):
            raise _invalid()
        tail = panels[len(panels) - total:]
        if [panel["id"] for panel in tail] != [ref for run in runs for ref in run]:
            raise _invalid()
        candidate = copy.deepcopy(graph)
        candidate["panels"] = candidate["panels"][:len(panels) - total]
        offset = 0
        for index, run in zip(positions, runs):
            slot_panels = tail[offset:offset + len(run)]
            offset += len(run)
            if not slot_panels:
                raise _invalid()
            first = slot_panels[0]
            template = {key: first[key] for key in TEMPLATE_KEYS}
            block = encode_slots(run, [panel["centre"] for panel in slot_panels], first["angle"],
                                 template)
            frame = candidate["frames"][index]
            for key in _DERIVED_LISTS:
                frame[key] = []
            frame[SLOTS_KEY] = block
    except GraphValidationError:
        raise
    except (KeyError, TypeError, AttributeError, IndexError):
        raise _invalid() from None
    try:
        expanded = expand_graph(candidate)
    except GraphValidationError:
        raise _invalid() from None
    if _canonical(expanded) != _canonical(graph):
        raise _invalid()
    return candidate
