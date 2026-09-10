"""
Minimal, honest ASCII-DXF -> intake parser (guest uploads, the DEFAULT DXF path).

Extracts ONLY what it can literally read from the user's own bytes —
LWPOLYLINE / POLYLINE entities and layer names — into the exact intake shape
`data/rooftop_demo.intake.json` established:

    {"dwg": <source name>, "layers": [...],
     "polylines": [{"layer", "closed", "pts": [[x, y, z], ...],
                    "xdata": null, "handle"}]}

HONESTY: nothing is invented. No entities -> an intake with zero polylines
(honest and renderable as such). A binary DXF raises — nothing reads it here.

This is the DEFAULT DXF extractor and the ONLY one the local (APS_LIVE=0) demo
has: it shows a REAL end-to-end guest flow on the user's own DXF without
fabricating geometry, cheaply and instantly. It is intentionally minimal
(LWPOLYLINE/POLYLINE + layers, with bounded blocks and INSERTs). For full
fidelity (3DFACE/geo/xdata and unsupported block children)
a live deployment can instead route DXF to the DXF-correct APS Activity by
setting LEAF_GUEST_DXF_EXTRACT=aps (see guest_uploads.run_extraction and
da.client.EXTRACT_DXF_ACTIVITY); that path costs a paid APS run. DWG always
extracts through APS.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BINARY_SENTINEL = b"AutoCAD Binary DXF"
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
# Mirrors server/intake_dxf.py's _LINEWEIGHTS (the DXF group-370 enumeration
# the writer accepts): duplicated, not imported, because intake_dxf already
# imports from this module and a reverse import would be circular.
_LINEWEIGHTS = frozenset({-3, -2, -1, 0, 5, 9, 13, 15, 18, 20, 25, 30, 35,
                          40, 50, 53, 60, 70, 80, 90, 100, 106, 120, 140, 158, 200, 211})


class DxfParseError(ValueError):
    """The file is not something this minimal parser can honestly read."""


def _mleader_number(value, integer=False):
    try:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite value")
        return int(value) if integer else number
    except (ValueError, TypeError, OverflowError) as exc:
        raise DxfParseError("malformed MLEADER numeric value") from exc


def parse_dxf_file(path: Path, *, source_name: str = "") -> Dict[str, Any]:
    raw = Path(path).read_bytes()
    return parse_dxf_bytes(raw, source_name=source_name or Path(path).name)


def parse_dxf_bytes(raw: bytes, *, source_name: str = "upload.dxf") -> Dict[str, Any]:
    if raw.startswith(BINARY_SENTINEL):
        raise DxfParseError(
            "binary DXF is not supported; re-save this drawing as ASCII DXF "
            "or upload the DWG instead")
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception as exc:  # pragma: no cover - decode with replace cannot raise
        raise DxfParseError(f"undecodable DXF: {exc}") from exc

    pairs = _group_pairs(text)
    layers: List[str] = []
    # Membership set beside the ordered list. `layers` stays a list because
    # first-seen ORDER is part of the intake shape; the set only answers
    # "seen already?" in O(1). A plain `x not in layers` scan was quadratic in
    # the number of unique layers, and a guest can pick that number: ~36 bytes
    # per LAYER entry means ~728k layers fit inside LEAF_UPLOAD_MAX_BYTES,
    # which measured out to over an hour of pegged CPU on ONE unauthenticated
    # upload. Cheap to write, and it was the live-traffic blocker for routing
    # DXF here (see guest_uploads.run_extraction).
    seen_layers: set[str] = set()
    polylines: List[Dict[str, Any]] = []
    texts: List[Dict[str, Any]] = []
    circles: List[Dict[str, Any]] = []
    arcs: List[Dict[str, Any]] = []
    inserts: List[Dict[str, Any]] = []
    blocks: Dict[str, Any] = {}
    parse_errors: List[str] = []
    block_count = 0
    has_blocks = False
    handle_seq = 0
    dropped_count = [0]
    # W4g-7b-04s: LINEAR/ALIGNED dimensions (the DM intake shape; the layer
    # comes from group 8, like every other entity here) and the loaded
    # dimstyle catalogue.
    dimensions: List[Dict[str, Any]] = []
    dimensions_unsupported = 0
    dimstyles: List[str] = []
    seen_dimstyles: set[str] = set()
    # W4g-7b-3s: colour/linetype/lineweight for every LINE / LWPOLYLINE /
    # CIRCLE / ARC / INSERT, the same EP shape da/intake_parse.py builds from
    # accoreconsole's entget so the plan route's DXF preflight
    # (server/routers/drawings.py) can compare a set_color/set_linetype/
    # set_lineweight target's actual DXF groups the same way either source.
    properties: Dict[str, Any] = {}
    insert_properties: Dict[str, Any] = {}
    objects = {}
    model_records = {}
    textstyles = {}
    mleader_records = []

    i = 0
    n = len(pairs)
    section: Optional[str] = None
    while i < n:
        code, value = pairs[i]
        if code == 0 and value == "SECTION" and i + 1 < n and pairs[i + 1][0] == 2:
            section = pairs[i + 1][1].upper()
            if section == "BLOCKS":
                has_blocks = True
            i += 2
            continue
        if code == 0 and value == "ENDSEC":
            section = None
            i += 1
            continue
        if section == "OBJECTS" and code == 0:
            j = i + 1
            while j < n and pairs[j][0] != 0:
                j += 1
            record = pairs[i + 1:j]
            handle = next((v.upper() for c, v in record if c == 5), None)
            if handle:
                objects[handle] = (value.upper(), record)
            i = j
            continue
        if section == "TABLES" and code == 0 and value == "STYLE":
            groups, i = _entity_groups(pairs, i + 1)
            if groups.get(5):
                textstyles[groups[5].upper()] = groups.get(2, "")
            continue
        if section == "TABLES" and code == 0 and value == "LAYER":
            # the next code-2 before the next code-0 names the layer
            j = i + 1
            while j < n and pairs[j][0] != 0:
                if pairs[j][0] == 2 and pairs[j][1] not in seen_layers:
                    seen_layers.add(pairs[j][1])
                    layers.append(pairs[j][1])
                j += 1
            i = j
            continue
        if section == "TABLES" and code == 0 and value == "DIMSTYLE":
            j = i + 1
            while j < n and pairs[j][0] != 0:
                if pairs[j][0] == 2 and pairs[j][1] not in seen_dimstyles:
                    seen_dimstyles.add(pairs[j][1])
                    dimstyles.append(pairs[j][1])
                j += 1
            i = j
            continue
        if section == "BLOCKS" and code == 0 and value == "BLOCK":
            name, block, i = _parse_block(pairs, i + 1, parse_errors)
            if name and not name.startswith("*"):
                block_count += 1
                if block_count <= 200:
                    blocks[name] = block
            continue
        if section == "ENTITIES" and code == 0:
            space_info = {}
            j = i + 1
            while j < n and pairs[j][0] != 0:
                if pairs[j] == (67, "1"):
                    space_info["space"] = "paper"
                elif pairs[j][0] == 410:
                    space_info["layout"] = pairs[j][1]
                j += 1
            record = pairs[i + 1:j]
            handle = next((v for c, v in record if c == 5), None)
            if handle:
                if value == "POLYLINE":
                    vertex_end = j
                    while vertex_end < n and pairs[vertex_end] == (0, "VERTEX"):
                        vertex_end += 1
                        while vertex_end < n and pairs[vertex_end][0] != 0:
                            if pairs[vertex_end][0] in (40, 41, 42):
                                record.append(pairs[vertex_end])
                            vertex_end += 1
                model_records[handle] = (value, record)
            if space_info.get("space") != "paper":
                space_info.clear()
        if section == "ENTITIES" and code == 0 and value == "MULTILEADER":
            if not space_info and dict(record).get(410, "Model") == "Model":
                mleader_records.append(record)
            i = j
            continue
        if section == "ENTITIES" and code == 0 and value == "INSERT":
            entity, i = _parse_insert(pairs, i + 1, dropped_count)
            if entity is not None:
                entity.update(space_info)
            props = entity.pop("_properties", None)
            if entity["layer"] not in seen_layers:
                seen_layers.add(entity["layer"])
                layers.append(entity["layer"])
            inserts.append(entity)
            if props is not None and entity.get("handle"):
                insert_properties[entity["handle"]] = props
            continue
        if section == "ENTITIES" and code == 0 and value == "LWPOLYLINE":
            entity, i = _parse_lwpolyline(pairs, i + 1, dropped_count)
            if entity is not None:
                entity.update(space_info)
            handle_seq += 1
            _finish_entity(entity, handle_seq, layers, seen_layers, polylines, properties)
            continue
        if section == "ENTITIES" and code == 0 and value == "POLYLINE":
            entity, i = _parse_polyline(pairs, i + 1, dropped_count)
            if entity is not None:
                entity.update(space_info)
            handle_seq += 1
            _finish_entity(entity, handle_seq, layers, seen_layers, polylines, properties)
            continue
        if section == "ENTITIES" and code == 0 and value == "LINE":
            # A LINE is a 2-point open polyline to the viewer and to every tool: no new
            # intake field, the frozen §1 shape renders it as-is.
            entity, i = _parse_line(pairs, i + 1, dropped_count)
            if entity is not None:
                entity.update(space_info)
            handle_seq += 1
            _finish_entity(entity, handle_seq, layers, seen_layers, polylines, properties)
            continue
        if section == "ENTITIES" and code == 0 and value in ("CIRCLE", "ARC"):
            # W4g-3: the kinds the browser engine draws besides lines and
            # polylines, as ADDITIVE §1 fields `circles` / `arcs` (a viewer
            # or tool that does not know them ignores them). Centre in WCS,
            # radius, the normal, and for an arc its start/end in degrees.
            entity, i = _parse_circle_or_arc(pairs, i + 1, value, dropped_count)
            if entity is not None:
                entity.update(space_info)
            handle_seq += 1
            if entity is not None:
                props = entity.pop("_properties", None)
                if not entity["handle"]:
                    entity["handle"] = f"L{handle_seq:X}"
                if entity["layer"] not in seen_layers:
                    seen_layers.add(entity["layer"])
                    layers.append(entity["layer"])
                (circles if value == "CIRCLE" else arcs).append(entity)
                if props is not None:
                    properties[entity["handle"]] = props
            continue
        if section == "ENTITIES" and code == 0 and value == "DIMENSION":
            entity, i = _parse_dimension(pairs, i + 1)
            if entity is not None:
                entity.update(space_info)
            handle_seq += 1
            if entity.get("unsupported"):
                dimensions_unsupported += 1
            else:
                entity.pop("unsupported", None)
                if not entity["handle"]:
                    entity["handle"] = f"L{handle_seq:X}"
                if entity["layer"] not in seen_layers:
                    seen_layers.add(entity["layer"])
                    layers.append(entity["layer"])
                dimensions.append(entity)
            continue
        if section == "ENTITIES" and code == 0 and value in ("TEXT", "MTEXT"):
            entity, i = _parse_text(pairs, i + 1, value)
            if entity is not None:
                entity.update(space_info)
            handle_seq += 1
            if entity["text"]:
                if not entity["handle"]:
                    entity["handle"] = f"L{handle_seq:X}"
                if entity["layer"] not in seen_layers:
                    seen_layers.add(entity["layer"])
                    layers.append(entity["layer"])
                texts.append(entity)
            continue
        i += 1

    resolved_inserts = []
    for entity in inserts:
        if entity["name"] not in blocks:
            parse_errors.append(
                f"INSERT {entity['handle']}: unresolved block reference {entity['name']}")
        else:
            resolved_inserts.append(entity)
            if entity.get("handle") in insert_properties:
                properties[entity["handle"]] = insert_properties[entity["handle"]]
    out: Dict[str, Any] = {"dwg": source_name, "layers": layers, "polylines": polylines}
    mlstyles = {}
    for handle, (kind, record) in objects.items():
        if kind == "MLEADERSTYLE":
            groups = dict(record)
            style = {"name": groups.get(3, ""),
                     "textstyle": textstyles.get(groups.get(342, "").upper(), ""),
                     **{field: round(_mleader_number(groups.get(code, "0")), 5)
                        for field, code in (("height", 45), ("arrow", 44),
                                            ("dogleg", 43), ("gap", 42))},
                     "segments": _mleader_number(groups.get(173, "0"), integer=True)}
            mlstyles[handle] = style
            out.setdefault("mlstyles", []).append(style)
    for record in mleader_records:
        entity = _parse_mleader(record, mlstyles, textstyles)
        if entity is None:
            out["mleaders_unsupported"] = out.get("mleaders_unsupported", 0) + 1
            handle = next((value for code, value in record if code == 5), "")
            if handle:
                out.setdefault("mleaders_unsupported_handles", []).append(handle.upper())
        else:
            out.setdefault("mleaders", []).append(entity)
            if entity["layer"] not in seen_layers:
                seen_layers.add(entity["layer"])
                layers.append(entity["layer"])
    def dictionary_entries(record):
        key = None
        for code, value in record:
            if code == 3:
                key = value
            elif code in (350, 360) and key is not None:
                yield key, value.upper()
                key = None

    for kind, record in objects.values():
        if kind != "DICTIONARY" or next((v for c, v in record if c == 330), "0") != "0":
            continue
        for key, dictionary in dictionary_entries(record):
            if key.upper() != "ACAD_GROUP":
                continue
            group_kind, group_dictionary = objects.get(dictionary, (None, []))
            if group_kind != "DICTIONARY":
                raise DxfParseError("ACAD_GROUP must reference a DICTIONARY")
            out["groups"] = []
            for name, handle in dictionary_entries(group_dictionary):
                group_kind, group = objects.get(handle, (None, []))
                if group_kind != "GROUP":
                    raise DxfParseError("ACAD_GROUP entry must reference a GROUP")
                out["groups"].append({
                    "handle": handle, "name": name, "owner": dictionary,
                    "flags": int(next((v for c, v in group if c == 70), "0")),
                    "selectable": int(next((v for c, v in group if c == 71), "1")),
                    "members": [v.upper() for c, v in group if c == 340]})
    if properties:
        out["properties"] = properties
    if dropped_count[0]:
        out["propertiesDropped"] = dropped_count[0]
    if texts:
        # ADDITIVE §1 field (frontend ignores unknown keys): drawing labels for tools
        # that classify views by the text inside a frame.
        out["texts"] = texts
    if circles:
        out["circles"] = circles
    if arcs:
        out["arcs"] = arcs
    if resolved_inserts:
        out["inserts"] = resolved_inserts
    if dimensions:
        out["dimensions"] = dimensions
    if dimstyles:
        out["dimstyles"] = dimstyles
    if dimensions_unsupported:
        out["dimensions_unsupported"] = dimensions_unsupported
    if has_blocks:
        out["blocks"] = blocks
    if block_count > 200:
        out["blocksCapped"] = block_count
    out["memberEvidenceCovered"] = True
    if model_records:
        entities = {e["handle"]: e for field in ("polylines", "circles", "arcs")
                    for e in out.get(field, [])}
        graph = {**objects, **{h.upper(): row for h, row in model_records.items()}}
        def dimension_dependency(record):
            pending = [v.upper() for c, v in record if c in (330, 340, 350, 360)]
            visited = set()
            while pending:
                target = pending.pop()
                if target in visited:
                    continue
                visited.add(target)
                if len(visited) > 256:
                    return True
                kind, data = graph.get(target, (None, []))
                if kind == "DIMENSION":
                    return True
                if kind in ("DIMASSOC", "DICTIONARY", "XRECORD"):
                    pending.extend(v.upper() for c, v in data if c in (330, 331, 340, 350, 360))
            return False
        associated = set()
        for kind, record in objects.values():
            if kind == "DIMASSOC" and dimension_dependency(record):
                associated.update(v.upper() for c, v in record if c in (331, 332, 340))
        for handle, (kind, record) in model_records.items():
            if kind not in ("LINE", "LWPOLYLINE", "POLYLINE", "CIRCLE", "ARC"):
                continue
            entity = entities.get(handle)
            if entity is None:
                continue
            groups = dict(record)
            normal = list(_group_point(groups, 210, (0, 0, 1)))
            if any(abs(a - b) > 1e-6 for a, b in zip(normal, (0, 0, 1))):
                entity["normal"] = normal
            bulges = []
            if kind == "LWPOLYLINE":
                for code, value in record:
                    if code == 10:
                        bulges.append(0.0)
                    elif code == 42 and bulges:
                        bulges[-1] = float(value)
                if normal[2] < 0:
                    # The arbitrary-axis algorithm reflects XY for a negative
                    # normal z, reversing bulge sweep (the same rule
                    # da/intake_parse.py's BM sign fix applies).
                    bulges = [-b for b in bulges]
            elif kind == "POLYLINE":
                # VERTEX coordinates are not copied into record; keep classic bulges unchanged.
                bulges = [float(v) for c, v in record if c == 42]
            if any(bulges):
                entity["bulges"] = bulges
            if kind in ("LWPOLYLINE", "POLYLINE") and any(float(v) != 0 for c, v in record if c in (40, 41, 43)):
                entity["width"] = True
            if groups.get(67) == "1" or groups.get(410, "Model") != "Model":
                entity["space"] = "paper"
            if handle.upper() in associated or dimension_dependency(record):
                entity["dimensionRef"] = True
    if parse_errors:
        out["parseErrors"] = parse_errors
    return out


def _parse_mleader(record, styles, textstyles):
    """Walk the nested contexts before resolving the top-level style handles."""
    state = 0
    closed = False
    branches = 0
    vertices = []
    context, leader, top = {}, {}, {}
    try:
        for i, (code, value) in enumerate(record):
            if (code, value) == (300, "CONTEXT_DATA{"):
                state = 1
            elif state == 1 and (code, value) == (302, "LEADER{"):
                state = 2
            elif state == 2 and (code, value) == (304, "LEADER_LINE{"):
                state = 3
                branches += 1
            elif state == 3 and (code, value) == (305, "}"):
                state = 2
            elif state == 2 and (code, value) == (303, "}"):
                state = 1
            elif state == 1 and (code, value) == (301, "}"):
                state, closed = 0, True
            elif state:
                point_code = ((state == 1 and code == 12) or
                              (state == 2 and code in (10, 11)) or
                              (state == 3 and code == 10))
                if point_code:
                    point = record[i:i + 3]
                    if [c for c, _ in point] != [code, code + 10, code + 20]:
                        return None
                    value = [round(_mleader_number(v), 3) for _, v in point]
                if state == 1:
                    context[code] = value
                elif state == 2:
                    leader[code] = value
                elif code == 10:
                    vertices.append(value)
            elif closed:
                top.setdefault(code, value)
        if (branches != 1 or _mleader_number(top.get(172, "0"), integer=True) != 2
                or 304 not in context or not vertices):
            return None
        style = styles.get(top.get(340, "").upper())
        if style is None:
            return None
        groups = dict(record)
        handle = groups.get(5, "")
        if not re.fullmatch(r"[0-9A-Fa-f]+", handle):
            return None
        return {"handle": handle, "layer": groups.get(8, "0") or "0",
                "style": style["name"],
                "textstyle": textstyles.get(top.get(343, "").upper(), ""),
                "height": round(_mleader_number(context[41]), 5),
                "arrow": round(_mleader_number(context[140]), 5),
                "dogleg": round(_mleader_number(leader[40]), 5),
                "attachment": _mleader_number(context[171], integer=True), "pts": vertices + [leader[10]],
                "landing": leader[10], "dogleg_dir": leader[11],
                "textpt": context[12], "text": context[304]}
    except DxfParseError:
        raise
    except (KeyError, ValueError, TypeError, OverflowError):
        return None


def _entity_properties(aci, linetype, lineweight, truecolor,
                       dropped: Optional[List[int]] = None) -> Optional[Dict[str, Any]]:
    """The EP shape (da/intake_parse.py's `properties[handle]`) built from raw
    DXF group values 62/6/370/420: absent means ByLayer/BYLAYER, exactly as
    entget reports it and exactly what the mutation plan's setters compare
    against (server/routers/drawings.py's uploaded-DXF preflight). None (not
    a defaulted dict) when the entity carries none of the four groups, so an
    untouched entity round-trips with no `properties` entry at all: a dense
    default here would make every entity, not just a styled one, appear in
    `properties`, and break the byte-for-byte intake round trip that already
    pins an entity with no style groups to carry no `properties` key.

    w4g-7b-03s-d D1: the writer (server/intake_dxf.py) refuses an aci outside
    0..256, a lineweight outside its enumeration, or an oversized/control-
    character linetype name, so a value this reader stored unnormalized would
    make the version's OWN later `intake_to_dxf` (write_loop.read_dxf's synth
    leg) fail every time. A negative 62 is AutoCAD's "layer off" flag on that
    colour, so its magnitude is the ACI (a -7 reads as aci 7); a 62 or 370
    the writer would still refuse is DROPPED (the field reads as absent/
    default) and counted in `dropped`, never stored as something the writer
    cannot round-trip."""
    if aci is None and linetype is None and lineweight is None and truecolor is None:
        return None
    rgb = None
    if truecolor is not None:
        packed = _int(truecolor) & 0xFFFFFF
        rgb = [(packed >> 16) & 0xFF, (packed >> 8) & 0xFF, packed & 0xFF]
    aci_value = 256
    if aci is not None:
        magnitude = abs(_int(aci))
        if 0 <= magnitude <= 256:
            aci_value = magnitude
        elif dropped is not None:
            dropped[0] += 1
    linetype_value = "ByLayer"
    if linetype is not None:
        if (isinstance(linetype, str) and linetype and len(linetype) <= 255
                and not _CONTROL_RE.search(linetype)):
            linetype_value = linetype
        elif dropped is not None:
            dropped[0] += 1
    lineweight_value = -1
    if lineweight is not None:
        raw_weight = _int(lineweight)
        if raw_weight in _LINEWEIGHTS:
            lineweight_value = raw_weight
        elif dropped is not None:
            dropped[0] += 1
    return {
        "aci": aci_value,
        "rgb": rgb,
        "linetype": linetype_value,
        "lineweight": lineweight_value,
    }


def _entity_groups(pairs, i):
    start = i
    while i < len(pairs) and pairs[i][0] != 0:
        i += 1
    return dict(pairs[start:i]), i


def _group_point(groups, code=10, default=(0.0, 0.0, 0.0)):
    return [_float(groups.get(code + j * 10, str(v))) for j, v in enumerate(default)]


def _parse_insert(pairs, i, dropped=None):
    groups, i = _entity_groups(pairs, i)
    normal = _group_point(groups, 210, (0.0, 0.0, 1.0))
    point = _ocs_to_wcs(_group_point(groups), normal)
    # Match the IN parser's existing x/y/z and nrm keys, including WCS position.
    return {"name": groups.get(2, ""), "layer": groups.get(8, "0") or "0",
            "x": round(point[0], 3), "y": round(point[1], 3), "z": round(point[2], 3),
            "rot": round(math.radians(_float(groups.get(50, "0"))), 6),
            "nrm": [round(v, 6) for v in normal],
            "scale": [_float(groups.get(code, "1")) for code in (41, 42, 43)],
            "handle": groups.get(5, ""),
            "_properties": _entity_properties(
                groups.get(62), groups.get(6), groups.get(370), groups.get(420), dropped)}, i


def _parse_block_child(pairs, i, kind):
    groups, end = _entity_groups(pairs, i)
    child = {"kind": kind, "layer": groups.get(8, "0") or "0"}
    properties = _entity_properties(groups.get(62), groups.get(6), groups.get(370), groups.get(420))
    if properties is not None:
        child["properties"] = properties
    if kind in ("LINE", "LWPOLYLINE", "CIRCLE", "ARC"):
        # These children must not inherit the legacy top-level parsers'
        # substitution of zero for unreadable coordinates.
        for code, value in pairs[i:end]:
            if code in (10, 20, 30, 11, 21, 31, 38, 40, 50, 51, 210, 220, 230):
                if not math.isfinite(float(value)):
                    raise ValueError("malformed block geometry")
    if kind == "LINE":
        if not all(code in groups for code in (10, 20, 11, 21)):
            raise ValueError("LINE needs two points")
        entity, end = _parse_line(pairs, i)
        child["pts"] = [[round(v, 3) for v in p] for p in entity["pts"]]
    elif kind == "LWPOLYLINE":
        entity, end = _parse_lwpolyline(pairs, i, wcs=False)
        if (len(entity["pts"]) < 2 or
                sum(code == 10 for code, _ in pairs[i:end]) !=
                sum(code == 20 for code, _ in pairs[i:end])):
            raise ValueError("LWPOLYLINE needs at least two complete points")
        child.update(closed=entity["closed"],
                     nrm=[round(v, 6) for v in _group_point(groups, 210, (0, 0, 1))],
                     elev=round(_float(groups.get(38, "0")), 3),
                     pts=[[round(v, 3) for v in p[:2]] for p in entity["pts"]])
    elif kind in ("CIRCLE", "ARC"):
        entity, end = _parse_circle_or_arc(pairs, i, kind)
        if entity is None or round(entity["r"], 3) <= 0:
            raise ValueError(f"{kind} radius must be positive")
        # BKE centres are block-local OCS points; the normal is the child's
        # own 210/220/230 (default +z), matching the top-level CI/AR shape.
        child.update(c=[round(v, 3) for v in _group_point(groups)], r=round(entity["r"], 3),
                     nrm=[round(v, 6) for v in _group_point(groups, 210, (0, 0, 1))])
        if kind == "ARC":
            child.update(start_deg=round(entity["start_deg"], 6),
                         end_deg=round(entity["end_deg"], 6))
    elif kind == "TEXT":
        entity, end = _parse_text(pairs, i, kind)
        value = entity["text"].replace("|", " ")
        child.update(pt=[round(v, 3) for v in _group_point(groups)],
                     height=round(_float(groups.get(40, "0")), 3),
                     rot=round(_float(groups.get(50, "0")), 6),
                     text=" ".join(value.split())[:_TEXT_MAX_CHARS])
    else:
        if kind == "POLYLINE":
            _, end = _parse_polyline(pairs, i)
        child = {"kind": "OTHER", "type": kind, "layer": ""}
    if "nrm" in child and not any(child["nrm"]):
        raise ValueError("block child normal must not be the zero vector")
    return child, end


def _parse_block(pairs, i, parse_errors):
    groups, i = _entity_groups(pairs, i)
    name = groups.get(2, "")
    flags = _int(groups.get(70, "0"))
    block = {"base": [round(v, 3) for v in _group_point(groups)],
             "count": 0, "complete": True, "children": []}
    while i < len(pairs):
        code, kind = pairs[i]
        if code == 0 and kind == "ENDBLK":
            _, i = _entity_groups(pairs, i + 1)
            return ("" if flags & 1 else name), block, i
        if code == 0 and kind in ("ENDSEC", "BLOCK", "EOF"):
            break
        if code != 0:
            i += 1
            continue
        block["count"] += 1
        try:
            child, end = _parse_block_child(pairs, i + 1, kind)
        except (ValueError, OverflowError, TypeError) as exc:
            block["complete"] = False
            parse_errors.append(f"BKE {name} ({kind}): {exc}")
            _, i = _entity_groups(pairs, i + 1)
            continue
        i = end
        if child["kind"] == "OTHER" or block["count"] > 60:
            block["complete"] = False
        if block["count"] <= 60:
            block["children"].append(child)
    block["complete"] = False  # unterminated definition
    return ("" if flags & 1 else name), block, i


def _ocs_to_wcs(point: List[float], normal: List[float]) -> List[float]:
    """AutoCAD's arbitrary-axis algorithm (the same one da/intake_parse.o2w
    applies to the extractor's OCS output), so a tilted circle's centre lands
    where AutoCAD puts it. A +z normal is the identity."""
    nx, ny, nz = normal
    unit = (nx * nx + ny * ny + nz * nz) ** 0.5 or 1.0
    nx, ny, nz = nx / unit, ny / unit, nz / unit
    if abs(nx) < 1 / 64.0 and abs(ny) < 1 / 64.0:
        ax = (nz, 0.0, -nx)  # (0,1,0) x n
    else:
        ax = (-ny, nx, 0.0)  # (0,0,1) x n
    length = (ax[0] ** 2 + ax[1] ** 2 + ax[2] ** 2) ** 0.5 or 1.0
    ax = (ax[0] / length, ax[1] / length, ax[2] / length)
    ay = (ny * ax[2] - nz * ax[1], nz * ax[0] - nx * ax[2], nx * ax[1] - ny * ax[0])
    x, y, z = point
    return [x * ax[0] + y * ay[0] + z * nx,
            x * ax[1] + y * ay[1] + z * ny,
            x * ax[2] + y * ay[2] + z * nz]


def _parse_circle_or_arc(pairs: List[Tuple[int, str]], i: int, kind: str, dropped=None):
    """CIRCLE / ARC: layer=8, handle=5, centre (10, 20, 30) in OCS, radius=40,
    normal (210, 220, 230, default +z), ARC start=50 / end=51 in DEGREES (the
    DXF file convention; entget's radians never reach a file). A radius that
    is not positive carries no geometry and is dropped, like a 1-point
    polyline."""
    layer = "0"
    handle = ""
    c = [0.0, 0.0, 0.0]
    normal = [0.0, 0.0, 1.0]
    radius = 0.0
    start = 0.0
    end = 360.0 if kind == "ARC" else 0.0
    aci = linetype = lineweight = truecolor = None
    n = len(pairs)
    while i < n and pairs[i][0] != 0:
        code, value = pairs[i]
        if code == 8:
            layer = value or "0"
        elif code == 5:
            handle = value
        elif code == 10:
            c[0] = _float(value)
        elif code == 20:
            c[1] = _float(value)
        elif code == 30:
            c[2] = _float(value)
        elif code == 40:
            radius = _float(value)
        elif code == 50:
            start = _float(value)
        elif code == 51:
            end = _float(value)
        elif code == 210:
            normal[0] = _float(value)
        elif code == 220:
            normal[1] = _float(value)
        elif code == 230:
            normal[2] = _float(value)
        elif code == 62:
            aci = value
        elif code == 6:
            linetype = value
        elif code == 370:
            lineweight = value
        elif code == 420:
            truecolor = value
        i += 1
    if not radius > 0.0:
        return None, i
    centre = _ocs_to_wcs(c, normal) if normal != [0.0, 0.0, 1.0] else c
    entity: Dict[str, Any] = {"layer": layer, "c": centre, "r": radius,
                              "nrm": normal, "handle": handle,
                              "_properties": _entity_properties(
                                  aci, linetype, lineweight, truecolor, dropped)}
    if kind == "ARC":
        entity["start_deg"] = start
        entity["end_deg"] = end
    return entity, i


def _parse_dimension(pairs: List[Tuple[int, str]], i: int):
    """DIMENSION: handle=5, layer=8, flags=70 (bits 0-3: 0 rotated/linear, 1
    aligned; any other subtype is unsupported), def1=(13,23,33),
    def2=(14,24,34), dimline=(10,20,30), rotation=50 (DXF degrees, LINEAR
    only), style=3 (default Standard), normal=(210,220,230, default +z),
    measurement=42 (group 42 when present, else the same projection rule
    da/lisp.py's DM inspect block and server/mutation_plan.py compute).

    F3 (opus round-one read of PR #1119): per the DXF spec, groups 13/14/10
    on a DIMENSION are WCS points; only 11/12/16 are OCS (this parser has no
    field for those). Unlike CIRCLE/ARC/INSERT, 13/14/10 are read here EXACTLY
    as given, with no arbitrary-axis (OCS->WCS) transform; 210/220/230 is kept
    only as the informational normal record."""
    handle = ""
    layer = "0"
    flags = 0
    p13 = [0.0, 0.0, 0.0]
    p14 = [0.0, 0.0, 0.0]
    p10 = [0.0, 0.0, 0.0]
    rotation = 0.0
    style = "Standard"
    normal = [0.0, 0.0, 1.0]
    measurement = None
    n = len(pairs)
    while i < n and pairs[i][0] != 0:
        code, value = pairs[i]
        if code == 5:
            handle = value
        elif code == 8:
            layer = value or "0"
        elif code == 70:
            flags = _int(value)
        elif code == 13:
            p13[0] = _float(value)
        elif code == 23:
            p13[1] = _float(value)
        elif code == 33:
            p13[2] = _float(value)
        elif code == 14:
            p14[0] = _float(value)
        elif code == 24:
            p14[1] = _float(value)
        elif code == 34:
            p14[2] = _float(value)
        elif code == 10:
            p10[0] = _float(value)
        elif code == 20:
            p10[1] = _float(value)
        elif code == 30:
            p10[2] = _float(value)
        elif code == 50:
            rotation = _float(value)
        elif code == 3:
            style = value or "Standard"
        elif code == 210:
            normal[0] = _float(value)
        elif code == 220:
            normal[1] = _float(value)
        elif code == 230:
            normal[2] = _float(value)
        elif code == 42:
            measurement = _float(value)
        i += 1
    subtype = flags & 15
    if subtype not in (0, 1):
        return {"unsupported": True, "handle": handle}, i
    kind = "LINEAR" if subtype == 0 else "ALIGNED"
    wp1, wp2, wdl = p13, p14, p10  # F3: WCS as given, never OCS-transformed
    if kind != "LINEAR":
        rotation = 0.0
    if measurement is None:
        dx, dy = wp2[0] - wp1[0], wp2[1] - wp1[1]
        if kind == "LINEAR":
            radians = math.radians(rotation)
            measurement = abs(dx * math.cos(radians) + dy * math.sin(radians))
        else:
            measurement = math.sqrt(dx * dx + dy * dy + (wp2[2] - wp1[2]) ** 2)
    entity: Dict[str, Any] = {
        "unsupported": False, "type": kind, "layer": layer,
        "p1": [round(v, 3) for v in wp1], "p2": [round(v, 3) for v in wp2],
        "dimline": [round(v, 3) for v in wdl], "rotation_deg": round(rotation, 6),
        "style": style, "nrm": [round(v, 6) for v in normal],
        "measurement": round(measurement, 3), "handle": handle,
    }
    return entity, i


def _finish_entity(entity: Dict[str, Any], seq: int, layers: List[str],
                   seen_layers: set, polylines: List[Dict[str, Any]],
                   properties: Dict[str, Any]) -> None:
    props = entity.pop("_properties", None)
    if len(entity["pts"]) < 2:
        return  # a 0/1-point polyline carries no geometry worth claiming
    if not entity.get("handle"):
        entity["handle"] = f"L{seq:X}"  # synthetic-but-labeled: DXF handle absent
    if entity["layer"] not in seen_layers:
        seen_layers.add(entity["layer"])
        layers.append(entity["layer"])
    polylines.append(entity)
    if props is not None:
        properties[entity["handle"]] = props


def _group_pairs(text: str) -> List[Tuple[int, str]]:
    """DXF is (code line, value line) pairs. Tolerates \r\n and blank tail."""
    lines = text.splitlines()
    pairs: List[Tuple[int, str]] = []
    for k in range(0, len(lines) - 1, 2):
        code_raw = lines[k].strip()
        try:
            code = int(code_raw)
        except ValueError:
            # Not a group-code line where one belongs: the pairing is broken;
            # resync by scanning forward one line. (Cheap tolerance for files
            # with a stray blank line — real writers do not produce these, but
            # a hand-edited demo file might.)
            continue
        pairs.append((code, lines[k + 1].strip()))
    if not pairs:
        raise DxfParseError("no DXF group-code pairs found")
    return pairs


def _parse_lwpolyline(pairs: List[Tuple[int, str]], i: int, dropped=None, wcs=True):
    """LWPOLYLINE: layer=8, handle=5, flags=70 (bit 1 = closed), elevation=38,
    vertices as repeated (10=x, 20=y). For a TOP-LEVEL entity (wcs=True, the
    default), 10/20/38 are OCS relative to the entity's extrusion normal
    (210/220/230, default +z), so each vertex is lifted into WCS through
    `_ocs_to_wcs`, the same arbitrary-axis transform CIRCLE/ARC already apply
    to their centre; the raw normal survives on the row as `normal` (omitted
    for a +z normal, so a +Z polyline's row stays byte-identical, and
    matching the key the member-evidence pass below already sets on this
    same row) for server/intake_dxf.py's exact inverse. `_parse_block_child`
    calls this with wcs=False: a block child's own 10/20/38 are BLOCK-LOCAL
    OCS by design (W4g-7c-2s) and must stay raw, so no transform and no
    normal validation happen there; the caller derives its own `nrm`
    evidence straight from the raw groups. A classic 2D
    POLYLINE's own OCS convention is untouched here (separate, later record);
    a classic 3D POLYLINE (`_parse_polyline`) never reads 210 at all."""
    layer = "0"
    handle = ""
    closed = False
    elevation = 0.0
    xs: List[float] = []
    ys: List[float] = []
    normal = [0.0, 0.0, 1.0]
    aci = linetype = lineweight = truecolor = None
    n = len(pairs)
    while i < n and pairs[i][0] != 0:
        code, value = pairs[i]
        if code == 8:
            layer = value or "0"
        elif code == 5:
            handle = value
        elif code == 70:
            closed = bool(_int(value) & 1)
        elif code == 38:
            elevation = _float(value)
        elif code == 10:
            xs.append(_float(value))
        elif code == 20:
            ys.append(_float(value))
        elif code == 210:
            normal[0] = _float(value)
        elif code == 220:
            normal[1] = _float(value)
        elif code == 230:
            normal[2] = _float(value)
        elif code == 62:
            aci = value
        elif code == 6:
            linetype = value
        elif code == 370:
            lineweight = value
        elif code == 420:
            truecolor = value
        i += 1
    if wcs:
        if not all(math.isfinite(v) for v in normal):
            raise DxfParseError("LWPOLYLINE normal must be finite")
        if not any(normal):
            raise DxfParseError("LWPOLYLINE normal must not be the zero vector")
        identity = normal == [0.0, 0.0, 1.0]
        pts = [([x, y, elevation] if identity else _ocs_to_wcs([x, y, elevation], normal))
               for x, y in zip(xs, ys)]
    else:
        identity = True
        pts = [[x, y, elevation] for x, y in zip(xs, ys)]
    entity = {"layer": layer, "closed": closed, "pts": pts,
              "xdata": None, "handle": handle,
              "_properties": _entity_properties(aci, linetype, lineweight, truecolor, dropped)}
    if wcs and not identity:
        entity["normal"] = [round(v, 6) for v in normal]
    return entity, i


def _parse_polyline(pairs: List[Tuple[int, str]], i: int, dropped=None):
    """Classic POLYLINE ... VERTEX* ... SEQEND: flags=70 on POLYLINE, vertices
    carry (10, 20, 30)."""
    layer = "0"
    handle = ""
    closed = False
    pts: List[List[float]] = []
    aci = linetype = lineweight = truecolor = None
    n = len(pairs)
    while i < n and pairs[i][0] != 0:
        code, value = pairs[i]
        if code == 8:
            layer = value or "0"
        elif code == 5:
            handle = value
        elif code == 70:
            closed = bool(_int(value) & 1)
        elif code == 62:
            aci = value
        elif code == 6:
            linetype = value
        elif code == 370:
            lineweight = value
        elif code == 420:
            truecolor = value
        i += 1
    while i < n:
        code, value = pairs[i]
        if code == 0 and value == "VERTEX":
            x = y = z = 0.0
            i += 1
            while i < n and pairs[i][0] != 0:
                c, v = pairs[i]
                if c == 10:
                    x = _float(v)
                elif c == 20:
                    y = _float(v)
                elif c == 30:
                    z = _float(v)
                i += 1
            pts.append([x, y, z])
            continue
        if code == 0 and value == "SEQEND":
            i += 1
            while i < n and pairs[i][0] != 0:
                i += 1
            break
        break  # any other entity start ends this POLYLINE (missing SEQEND)
    return {"layer": layer, "closed": closed, "pts": pts,
            "xdata": None, "handle": handle,
            "_properties": _entity_properties(aci, linetype, lineweight, truecolor, dropped)}, i


def _parse_line(pairs: List[Tuple[int, str]], i: int, dropped=None):
    """LINE: layer=8, handle=5, start (10, 20, 30), end (11, 21, 31). Emitted in the
    polyline shape (closed=False, two pts) so nothing downstream learns a new type."""
    layer = "0"
    handle = ""
    a = [0.0, 0.0, 0.0]
    b = [0.0, 0.0, 0.0]
    aci = linetype = lineweight = truecolor = None
    n = len(pairs)
    while i < n and pairs[i][0] != 0:
        code, value = pairs[i]
        if code == 8:
            layer = value or "0"
        elif code == 5:
            handle = value
        elif code == 10:
            a[0] = _float(value)
        elif code == 20:
            a[1] = _float(value)
        elif code == 30:
            a[2] = _float(value)
        elif code == 11:
            b[0] = _float(value)
        elif code == 21:
            b[1] = _float(value)
        elif code == 31:
            b[2] = _float(value)
        elif code == 62:
            aci = value
        elif code == 6:
            linetype = value
        elif code == 370:
            lineweight = value
        elif code == 420:
            truecolor = value
        i += 1
    return {"layer": layer, "closed": False, "pts": [a, b], "xdata": None, "handle": handle,
            "_properties": _entity_properties(aci, linetype, lineweight, truecolor, dropped)}, i


_MTEXT_FORMAT_CODES = ("\\p", "\\f", "\\F", "\\H", "\\W", "\\C", "\\c", "\\Q", "\\T", "\\A", "\\S")
_MTEXT_TOGGLE_CODES = ("\\L", "\\l", "\\O", "\\o", "\\K", "\\k")
_TEXT_MAX_CHARS = 512


def _parse_text(pairs: List[Tuple[int, str]], i: int, kind: str):
    """TEXT / MTEXT: layer=8, handle=5, insertion (10, 20), value=1 (MTEXT may continue
    in 3-codes). MTEXT inline formatting codes are stripped to plain words; the value is
    capped so a hostile file cannot inflate the intake."""
    layer = "0"
    handle = ""
    x = y = 0.0
    parts: List[str] = []
    n = len(pairs)
    while i < n and pairs[i][0] != 0:
        code, value = pairs[i]
        if code == 8:
            layer = value or "0"
        elif code == 5:
            handle = value
        elif code == 10:
            x = _float(value)
        elif code == 20:
            y = _float(value)
        elif code == 3:
            parts.append(value)
        elif code == 1:
            parts.append(value)
        i += 1
    text = "".join(parts)
    if kind == "MTEXT":
        text = _strip_mtext(text)
    text = " ".join(text.split())[:_TEXT_MAX_CHARS]
    return {"kind": kind, "layer": layer, "pt": [x, y], "text": text, "handle": handle}, i


def _strip_mtext(s: str) -> str:
    """Drop MTEXT formatting: {\\fArial|b0;...} groups keep their text, \\P is a line break."""
    out: List[str] = []
    j = 0
    L = len(s)
    while j < L:
        c = s[j]
        if c == "\\" and j + 1 < L:
            code = s[j:j + 2]
            if code in ("\\P", "\\~"):
                out.append(" ")
                j += 2
                continue
            if code in _MTEXT_TOGGLE_CODES:
                j += 2
                continue
            if code in ("\\\\", "\\{", "\\}"):
                out.append(s[j + 1])
                j += 2
                continue
            if code in _MTEXT_FORMAT_CODES:
                k = s.find(";", j)
                j = (k + 1) if k >= 0 else L
                continue
            j += 2
            continue
        if c in "{}":
            j += 1
            continue
        out.append(c)
        j += 1
    return "".join(out)


def _int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


def _float(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        return 0.0
