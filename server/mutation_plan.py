"""Validation and deterministic lowering for authored drawing mutations.

This module is deliberately pure. It accepts only the product mutation data
contract and emits a closed, line-oriented data plan. It never accepts or
emits AutoLISP, command text, file paths, or other executable input.

Contract v1 (catalog tools): ``added`` closed LWPOLYLINEs, ``removed`` and
``transforms`` (dx/dy/rotation_deg) over LWPOLYLINE handles. Contract v2
(W4g-3, the browser engine's saves): ``added`` may carry ``kind`` LINE, CIRCLE
or ARC and an open LWPOLYLINE; ``removed`` covers every kind the intake names
(polylines, circles, arcs); ``set_layer``, ``set_points``, ``set_circle`` and
``set_arc`` replace one existing entity's layer or geometry. A v1 input yields
byte-identical canonical data and plan text to before; the plan header reads
``LEAF_MUTATION_PLAN|2`` only when a v2 capability is used, so a v1-only plan
still runs on the previous Activity alias during a rollout.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple


MAX_PLAN_BYTES = 1_048_576
MAX_OPERATIONS = 5_000
MAX_ENTITIES = 200_000
MAX_POINTS = 100_000
MAX_POINTS_PER_ENTITY = 10_000
MAX_COORDINATE = 1_000_000_000.0
MAX_ANGLE_DEG = 3_600.0
PLANAR_TOLERANCE = 1e-6
_HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_EXISTING_HANDLE_RE = re.compile(r"^[0-9A-Fa-f]{1,32}$")
_LAYER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.$-]{0,254}$")
_MUTATION_FIELDS = frozenset({
    "added", "removed", "transforms", "added_groups", "removed_groups", "block_defs",
    "set_layer", "set_points", "set_circle", "set_arc",
})
_V2_FIELDS = frozenset({"set_layer", "set_points", "set_circle", "set_arc"})
_ADDED_FIELDS = frozenset({
    "handle", "kind", "layer", "closed", "pts", "xdata", "c", "r",
    "start_deg", "end_deg",
})
_ADD_KINDS = ("LWPOLYLINE", "LINE", "CIRCLE", "ARC")
_INSERT_FIELDS = frozenset({"handle", "kind", "layer", "name", "pt", "rot", "scale"})
_MLEADER_FIELDS = frozenset({"handle", "kind", "layer", "style", "pts", "text"})
_DIMENSION_FIELDS = frozenset({
    "handle", "kind", "layer", "dimtype", "def1", "def2", "dimline", "rotation", "style",
    # `measurement` is never used to COMPUTE the canonical value (that is
    # always derived from def1/def2/rotation); when supplied it is instead
    # checked against that computed value (F5: a disagreeing supplied
    # measurement is refused) so a caller cannot fail-open a wrong value
    # through, while an agreeing one, or the field this same function wrote
    # into an already-canonical record, re-validates cleanly.
    "measurement",
})
DIMENSION_MEASUREMENT_TOLERANCE = 1e-3
_DIMTYPES = ("LINEAR", "ALIGNED")
V3_ADD_KINDS = ("INSERT", "DIMENSION", "MLEADER")
V3_SET_OPS = ("set_color", "set_linetype", "set_lineweight")
LINEWEIGHTS = frozenset({-3, -2, -1, 0, 5, 9, 13, 15, 18, 20, 25, 30, 35,
                        40, 50, 53, 60, 70, 80, 90, 100, 106, 120, 140, 158, 200, 211})
STYLE_FIELDS = {"color": "aci", "linetype": "name", "lineweight": "weight"}
_STANDARD_LINETYPES = ("ByLayer", "ByBlock", "Continuous")


def _known_linetype_names(intake: Dict[str, Any]) -> List[str]:
    """Every linetype spelling this drawing is known to carry (w4g-7b-03s-c
    R6, reordered by w4g-7b-03s-d D3): the head's OWN spellings first, in
    encounter order and de-duplicated case-insensitively, then the three
    standard names appended only when no head spelling already matches one
    case-insensitively. A head whose entity carries "CONTINUOUS" must
    canonicalize a "continuous" request to "CONTINUOUS", not to the standard
    "Continuous" that a standard-names-first order would have matched
    first. RESIDUAL: the intake carries no LTYPE table, so a linetype that
    is loaded in the drawing but currently unused by every entity is
    refused here until a later change adds an LT catalogue (the 04s DS
    pattern is the model); the interpreter itself still accepts any loaded
    name via `tblsearch`, so this is strictly narrower, never wider, than
    what the drawing actually supports."""
    names: List[str] = []
    seen_lower: set = set()
    properties = intake.get("properties")
    if isinstance(properties, dict):
        for entry in properties.values():
            if isinstance(entry, dict):
                name = entry.get("linetype")
                if isinstance(name, str) and name and name.lower() not in seen_lower:
                    seen_lower.add(name.lower())
                    names.append(name)
    for standard in _STANDARD_LINETYPES:
        if standard.lower() not in seen_lower:
            seen_lower.add(standard.lower())
            names.append(standard)
    return names


def _canonical_linetype_name(value: str, known: List[str]) -> str:
    """w4g-7b-03s-c R2: canonicalize to the drawing's own spelling on a
    case-insensitive match (the same rule as the added-INSERT layer
    canonicalization below), so the interpreter's case-insensitive
    `tblsearch` and the verifier's case-insensitive comparison never
    disagree with what the plan actually names."""
    for candidate in known:
        if candidate.lower() == value.lower():
            return candidate
    raise ValueError(f"linetype {value} is not loaded in this drawing")


def _style_value(field: str, value: Any, known_linetypes: List[str]) -> Any:
    if field == "color":
        if type(value) is not int or not 0 <= value <= 256:
            raise ValueError("color aci must be an integer in 0..256")
    elif field == "lineweight":
        if type(value) is not int or value not in LINEWEIGHTS:
            raise ValueError(f"lineweight {value} is not a valid enumeration value")
    else:
        if not isinstance(value, str) or not _LAYER_RE.fullmatch(value):
            raise ValueError("linetype is not a safe linetype name")
        value = _canonical_linetype_name(value, known_linetypes)
    return value


_TRANSFORM_FIELDS = frozenset({"handle", "dx", "dy", "rotation_deg"})
_SET_LAYER_FIELDS = frozenset({"handle", "layer"})
_SET_POINTS_FIELDS = frozenset({"handle", "pts", "closed"})
_SET_CIRCLE_FIELDS = frozenset({"handle", "c", "r"})
_SET_ARC_FIELDS = frozenset({"handle", "c", "r", "start_deg", "end_deg"})
_RAW_FIELDS = frozenset({
    "code", "command", "commands", "script", "lisp", "autolisp", "shell",
    "powershell", "python", "path", "url",
})
_UP_NORMAL = (0.0, 0.0, 1.0)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _number(value: Any, field: str, *, limit: float = MAX_COORDINATE) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    if isinstance(value, int) and abs(value) > limit:
        # A huge int (e.g. a 310-digit literal) can raise OverflowError inside
        # float() before the ordinary range check below ever runs; catch the
        # int case here so the refusal is always this ValueError, never a 500.
        raise ValueError(f"{field} is outside the supported range")
    try:
        result = float(value)
    except OverflowError:
        raise ValueError(f"{field} is outside the supported range") from None
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    if abs(result) > limit:
        raise ValueError(f"{field} is outside the supported range")
    return 0.0 if result == 0 else result


def _handle(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _HANDLE_RE.fullmatch(value):
        raise ValueError(f"{field} is not a safe handle")
    return value


def _existing_handle(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _EXISTING_HANDLE_RE.fullmatch(value):
        raise ValueError(f"{field} is not an AutoCAD handle")
    return value


def _layer(value: Any) -> str:
    if not isinstance(value, str) or not _LAYER_RE.fullmatch(value):
        raise ValueError("added layer is not a safe layer name")
    if value in {".", ".."} or "|" in value or "\r" in value or "\n" in value:
        raise ValueError("added layer is not a safe layer name")
    return value


def _canonicalize_layer(layer: str, intake: Dict[str, Any]) -> str:
    # AutoCAD layer names are case-insensitive: an admitted spelling that
    # differs only in case from an existing layer creates on that layer and
    # reads back with the existing spelling, so the canonical form must
    # already carry it. A layer absent from the intake keeps its given
    # spelling (the interpreter creates it fresh).
    existing_layers = intake.get("layers")
    if isinstance(existing_layers, list):
        for existing_layer in existing_layers:
            if isinstance(existing_layer, str) and existing_layer.lower() == layer.lower():
                return existing_layer
    return layer


def _reject_raw_fields(value: Any, path: str = "mutations") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string field")
            if key.strip().lower() in _RAW_FIELDS:
                raise ValueError(f"{path}.{key} is executable input and is forbidden")
            _reject_raw_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_raw_fields(child, f"{path}[{index}]")


def _point3(value: Any, field: str) -> List[float]:
    """One [x, y] or [x, y, z] point as three finite floats."""
    if not isinstance(value, list) or len(value) not in (2, 3):
        raise ValueError(f"{field} is a malformed point")
    xyz = [_number(item, field) for item in value]
    if len(xyz) == 2:
        xyz.append(0.0)
    return xyz


def _points(value: Any, field: str, *, minimum: int) -> List[List[float]]:
    if not isinstance(value, list) or not minimum <= len(value) <= MAX_POINTS_PER_ENTITY:
        raise ValueError(f"{field} has an invalid point count")
    return [_point3(point, f"{field} point {index}") for index, point in enumerate(value)]


def _normal_is_up(entity: Dict[str, Any]) -> bool:
    normal = entity.get("nrm")
    if normal is None:
        return True
    try:
        return all(abs(float(a) - b) <= 1e-6 for a, b in zip(normal, _UP_NORMAL))
    except (TypeError, ValueError):
        return False


def _same_round(entity: Dict[str, Any], centre: List[float], radius: float,
                start: Optional[float] = None, end: Optional[float] = None) -> bool:
    """True when a circle/arc replacement names the entity's current geometry
    exactly (the same no-op rule set_layer and set_points apply)."""
    try:
        current = [float(v) for v in (entity.get("c") or [])]
        current += [0.0] * (3 - len(current))
        if current[:3] != centre or float(entity.get("r")) != radius:
            return False
        if start is None:
            return True
        return float(entity.get("start_deg")) == start and float(entity.get("end_deg")) == end
    except (TypeError, ValueError):
        return False


def _index_intake(intake: Dict[str, Any]) -> Dict[str, Tuple[str, Dict[str, Any]]]:
    """Every handle the intake names, with its kind. A handle that appears
    twice anywhere is ambiguous and is dropped, so no op can name it."""
    if not isinstance(intake, dict):
        raise ValueError("intake must be an object")
    result: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    ambiguous = set()
    total = 0
    for field, kind in (("polylines", "LWPOLYLINE"), ("circles", "CIRCLE"), ("arcs", "ARC"),
                        ("dimensions", "DIMENSION"), ("mleaders", "MULTILEADER")):
        entities = intake.get(field) or []
        if not isinstance(entities, list):
            raise ValueError(f"intake {field} must be a list")
        total += len(entities)
        if total > MAX_ENTITIES:
            raise ValueError("intake entities exceed the supported entity bound")
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            handle = entity.get("handle")
            if not isinstance(handle, str) or not handle:
                continue
            if handle in result:
                ambiguous.add(handle)
            else:
                result[handle] = (kind, entity)
    for handle in ambiguous:
        result.pop(handle, None)
    return result


def _existing_by_handle(intake: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """The polyline entities by handle (the v1 view, used by the transform
    lowering, which only ever names polylines)."""
    return {
        handle: entity for handle, (kind, entity) in _index_intake(intake).items()
        if kind == "LWPOLYLINE"
    }


def _op_list(mutations: Dict[str, Any], field: str) -> List[Any]:
    raw = mutations.get(field, [])
    if not isinstance(raw, list):
        raise ValueError(f"mutations.{field} must be a list")
    return raw


def _validate_block_defs(intake, mutations, index):
    definitions = _op_list(mutations, "block_defs")
    if not definitions:
        return []
    if intake.get("memberEvidenceCovered") is not True or any(
            str(error).startswith(("BM:", "MEC:")) for error in intake.get("parseErrors", [])):
        raise ValueError("member evidence unavailable in this inspection")
    blocks = intake.get("blocks", {})
    total = intake.get("blocksCapped", len(blocks))
    if total > len(blocks):
        raise ValueError(
            f"the block catalogue is incomplete: {total} definitions, {len(blocks)} listed; "
            "create the block on a drawing whose catalogue fits the display cap")
    occupied = {n.casefold() for n in blocks}
    removed = _op_list(mutations, "removed")
    added = _op_list(mutations, "added")
    used = set()
    result = []
    for raw in definitions:
        if not isinstance(raw, dict) or set(raw) != {"name", "base", "members", "insert"}:
            raise ValueError("block definition requires name, base, members and insert only")
        name = raw["name"]
        if (not isinstance(name, str) or not 1 <= len(name) <= 255 or name.startswith("*")
                or any(c in "|\r\n" or not 32 <= ord(c) <= 126 for c in name)):
            raise ValueError("block definition name must use the INSERT-name charset")
        if name.casefold() in occupied:
            raise ValueError("block definition name collides case-insensitively with the catalogue")
        occupied.add(name.casefold())
        if not isinstance(raw["base"], list) or len(raw["base"]) != 3:
            raise ValueError("block definition base must have three components")
        base = [round(v, 3) for v in _point3(raw["base"], "block base")]
        members = raw["members"]
        if not isinstance(members, list) or not 1 <= len(members) <= 60:
            raise ValueError("block definition requires 1..60 committed members")
        seen = set()
        for h in members:
            _existing_handle(h, "block member")
            if h.upper() in seen or h.upper() in used:
                raise ValueError("block members must be distinct across definitions")
            seen.add(h.upper())
            if h not in index:
                raise ValueError("block member must be a committed LINE, straight LWPOLYLINE, CIRCLE or ARC; nested INSERT is excluded")
            kind, entity = index[h]
            if entity.get("kind", kind) not in ("LINE", "LWPOLYLINE", "CIRCLE", "ARC"):
                raise ValueError("block member kind must be LINE, LWPOLYLINE, CIRCLE or ARC")
            if (entity.get("paper_space") or entity.get("paperspace") or entity.get("block")
                    or entity.get("space", "model") not in ("model", "Model", "ModelSpace", 0)):
                raise ValueError("block members must be model-space entities, never nested")
            if "normal" in entity:
                raise ValueError("block members must be planar with normal +Z")
            normal = entity.get("nrm", [0, 0, 1])
            if not isinstance(normal, (list, tuple)) or len(normal) != 3 or not _normal_is_up(entity):
                raise ValueError("block members must be planar with normal +Z")
            if "bulges" in entity or entity.get("bulge", 0):
                raise ValueError("block LWPOLYLINE must have straight segments, every bulge 0")
            if entity.get("width"):
                raise ValueError("block LWPOLYLINE must have zero constant and vertex width")
            if (any(h.upper() in {str(m).upper() for m in g.get("members", [])}
                    for g in intake.get("groups", [])) or any(
                    str(m.get("member", "")).upper() == h.upper()
                    for m in intake.get("group_memberships", []))):
                raise ValueError("block member belongs to a GROUP; ungroup it first")
            props = (intake.get("properties") or {}).get(h, entity.get("properties", {}))
            if (props.get("aci", 256) == 0 or str(props.get("linetype", "ByLayer")).casefold() == "byblock"
                    or props.get("lineweight", -1) == -2):
                raise ValueError("block members cannot carry ByBlock colour, linetype or lineweight")
            dependencies = entity.get("dimension_refs", [])
            dimension_handles = {str(d.get("handle", "")).upper() for d in intake.get("dimensions", [])}
            for reactor in entity.get("reactors", []):
                if ((isinstance(reactor, str) and reactor.upper() in dimension_handles)
                        or (isinstance(reactor, dict) and reactor.get("kind", reactor.get("type")) == "DIMENSION")):
                    dependencies = [reactor]
            def references(value):
                if isinstance(value, str):
                    return value.upper() == h.upper()
                if isinstance(value, list):
                    return any(references(v) for v in value)
                if isinstance(value, dict):
                    return any(references(v) for v in value.values())
                return False
            for dim in intake.get("dimensions", []):
                if any(references(dim.get(field)) for field in (
                        "references", "associated_handles", "definition_association", "association")):
                    dependencies = [dim.get("handle")]
            if dependencies or "dimensionRef" in entity:
                raise ValueError("block member is referenced by a DIMENSION association or reactor")
            for op in ("transforms", "set_layer", "set_points", "set_circle", "set_arc", *V3_SET_OPS):
                if any(isinstance(e, dict) and e.get("handle") == h for e in _op_list(mutations, op)):
                    raise ValueError("block members must be UNCHANGED committed entities")
            if h not in removed:
                raise ValueError("each block member must also be in removed for atomic REPLACE")
        used.update(seen)
        ordinal = raw["insert"]
        if type(ordinal) is not int or not 0 <= ordinal < len(added):
            raise ValueError("block insert ordinal must point at its matching INSERT")
        insert = added[ordinal]
        matches = [e for e in added if isinstance(e, dict) and e.get("kind") == "INSERT"
                   and str(e.get("name", "")).casefold() == name.casefold()]
        if (len(matches) != 1 or not isinstance(insert, dict) or insert.get("kind") != "INSERT"
                or insert.get("name") != name or insert.get("layer") != "0"
                or insert.get("pt") != raw["base"] or insert.get("rot") != 0
                or insert.get("scale") != [1, 1, 1]):
            raise ValueError("block insert must match name and base on layer 0, rotation 0, scale 1,1,1")
        result.append({"name": name, "base": base, "members": list(members), "insert": ordinal})
    return sorted(result, key=lambda b: b["name"])


def validate_mutations(
    intake: Dict[str, Any], mutations: Any, *, allow_transforms: bool = True,
    allow_xdata: bool = False, reject_noop: bool = True,
) -> Dict[str, Any]:
    """Strictly validate and canonicalize the frozen mutation data contract."""
    if not isinstance(mutations, dict):
        raise ValueError("result.mutations must be an object")
    # `removed_kinds` is a canonical-only annotation this function writes (the
    # kind of each non-polyline removal, for the plan header); on input it is
    # never trusted, only recomputed, so a canonical set re-validates cleanly.
    unknown = set(mutations) - _MUTATION_FIELDS - set(V3_SET_OPS) - {"removed_kinds"}
    if unknown:
        raise ValueError(f"unknown mutation fields: {', '.join(sorted(map(str, unknown)))}")
    _reject_raw_fields(mutations)
    index = _index_intake(intake)
    block_defs = _validate_block_defs(intake, mutations, index)
    known_linetypes = _known_linetype_names(intake)
    removed_raw = _op_list(mutations, "removed")
    added_raw = _op_list(mutations, "added")
    transforms_raw = _op_list(mutations, "transforms")
    set_layer_raw = _op_list(mutations, "set_layer")
    set_points_raw = _op_list(mutations, "set_points")
    set_circle_raw = _op_list(mutations, "set_circle")
    set_arc_raw = _op_list(mutations, "set_arc")
    style_raw = {op: _op_list(mutations, op) for op in V3_SET_OPS}
    added_groups_raw = _op_list(mutations, "added_groups")
    removed_groups_raw = _op_list(mutations, "removed_groups")
    op_count = (
        len(removed_raw) + len(added_raw) + len(transforms_raw)
        + len(set_layer_raw) + len(set_points_raw) + len(set_circle_raw)
        + len(set_arc_raw) + sum(len(style_raw[op]) for op in V3_SET_OPS)
        + len(added_groups_raw) + len(removed_groups_raw) + len(block_defs)
    )
    if op_count == 0 and reject_noop:
        raise ValueError("mutations must contain at least one operation")
    if op_count > MAX_OPERATIONS:
        raise ValueError("mutation operation bound exceeded")

    removed: List[str] = []
    removed_seen = set()
    removed_kinds: Dict[str, str] = {}
    for position, raw in enumerate(removed_raw):
        handle = _existing_handle(raw, f"removed[{position}]")
        if handle in removed_seen:
            raise ValueError(f"duplicate removed handle {handle!r}")
        if handle not in index:
            raise ValueError(f"unknown removed handle {handle!r}")
        removed_seen.add(handle)
        removed.append(handle)
        kind = index[handle][0]
        if kind != "LWPOLYLINE":
            removed_kinds[handle] = kind

    transforms: List[Dict[str, Any]] = []
    transformed_seen = set()
    if transforms_raw and not allow_transforms:
        raise ValueError("live mutation plans do not yet support transforms")
    for position, raw in enumerate(transforms_raw):
        if not isinstance(raw, dict) or set(raw) - _TRANSFORM_FIELDS:
            raise ValueError(f"transform at index {position} has unknown fields")
        handle = _existing_handle(
            raw.get("handle"), f"transforms[{position}].handle")
        if handle not in index:
            raise ValueError(f"unknown transform handle {handle!r}")
        if index[handle][0] != "LWPOLYLINE":
            raise ValueError(f"transform handle {handle!r} is not a polyline")
        if handle in transformed_seen:
            raise ValueError(f"duplicate transform handle {handle!r}")
        if handle in removed_seen:
            raise ValueError(f"handle {handle!r} cannot be removed and transformed")
        if "dx" not in raw or "dy" not in raw:
            raise ValueError(f"transform {handle!r} requires dx and dy")
        dx = _number(raw["dx"], f"transform {handle!r} dx", limit=10_000)
        dy = _number(raw["dy"], f"transform {handle!r} dy", limit=10_000)
        rotation = _number(
            raw.get("rotation_deg", 0), f"transform {handle!r} rotation_deg",
            limit=360,
        )
        if dx == 0 and dy == 0 and rotation == 0 and reject_noop:
            raise ValueError(f"transform {handle!r} is a no-op")
        transformed_seen.add(handle)
        transforms.append({
            "handle": handle, "dx": dx, "dy": dy,
            "rotation_deg": rotation,
        })

    # v2 replacements: one geometry op per handle, one layer op per handle,
    # nothing on a removed handle. Each op names an entity of the kind it
    # replaces, and a tilted circle or arc (a normal other than +z) is
    # refused, since the plan writes its centre in world coordinates.
    geometry_seen = set()
    relayered_seen = set()

    def _target(handle_raw: Any, field: str, kinds: Tuple[str, ...]) -> Tuple[str, str, Dict[str, Any]]:
        handle = _existing_handle(handle_raw, field)
        if handle not in index:
            raise ValueError(f"unknown {field.split('[')[0]} handle {handle!r}")
        if handle in removed_seen:
            raise ValueError(f"handle {handle!r} cannot be removed and replaced")
        kind, entity = index[handle]
        if kind == "MULTILEADER":
            raise ValueError("MLEADER is not a property target in this contract")
        if kind not in kinds:
            raise ValueError(f"{field.split('[')[0]} handle {handle!r} is a {kind}")
        return handle, kind, entity

    set_layer: List[Dict[str, Any]] = []
    for position, raw in enumerate(set_layer_raw):
        if not isinstance(raw, dict) or set(raw) != _SET_LAYER_FIELDS:
            raise ValueError(f"set_layer at index {position} has unknown or missing fields")
        handle, _kind, entity = _target(raw.get("handle"), f"set_layer[{position}]", ("LWPOLYLINE", "CIRCLE", "ARC"))
        if handle in relayered_seen:
            raise ValueError(f"duplicate set_layer handle {handle!r}")
        layer = _layer(raw.get("layer"))
        if layer == entity.get("layer") and reject_noop:
            raise ValueError(f"set_layer {handle!r} is a no-op")
        relayered_seen.add(handle)
        set_layer.append({"handle": handle, "layer": layer})

    total_points = 0
    set_points: List[Dict[str, Any]] = []
    for position, raw in enumerate(set_points_raw):
        if not isinstance(raw, dict) or not {"handle", "pts"} <= set(raw) or set(raw) - _SET_POINTS_FIELDS:
            raise ValueError(f"set_points at index {position} has unknown or missing fields")
        handle, _kind, entity = _target(raw.get("handle"), f"set_points[{position}]", ("LWPOLYLINE",))
        if handle in geometry_seen or handle in transformed_seen:
            raise ValueError(f"handle {handle!r} has more than one geometry operation")
        closed = raw.get("closed", bool(entity.get("closed")))
        if not isinstance(closed, bool):
            raise ValueError(f"set_points {handle!r} closed must be a boolean")
        points = _points(raw.get("pts"), f"set_points {handle!r}", minimum=3 if closed else 2)
        total_points += len(points)
        if total_points > MAX_POINTS:
            raise ValueError("mutation point bound exceeded")
        if (points == [list(map(float, p)) + [0.0] * (3 - len(p)) for p in (entity.get("pts") or [])]
                and closed == bool(entity.get("closed")) and reject_noop):
            raise ValueError(f"set_points {handle!r} is a no-op")
        geometry_seen.add(handle)
        set_points.append({"handle": handle, "closed": closed, "pts": points})

    set_circle: List[Dict[str, Any]] = []
    for position, raw in enumerate(set_circle_raw):
        if not isinstance(raw, dict) or set(raw) != _SET_CIRCLE_FIELDS:
            raise ValueError(f"set_circle at index {position} has unknown or missing fields")
        handle, _kind, entity = _target(raw.get("handle"), f"set_circle[{position}]", ("CIRCLE",))
        if handle in geometry_seen:
            raise ValueError(f"handle {handle!r} has more than one geometry operation")
        if not _normal_is_up(entity):
            raise ValueError(f"set_circle {handle!r}: a tilted circle is not editable here")
        centre = _point3(raw.get("c"), f"set_circle {handle!r} c")
        radius = _number(raw.get("r"), f"set_circle {handle!r} r")
        if radius <= 0:
            raise ValueError(f"set_circle {handle!r} r must be positive")
        if reject_noop and _same_round(entity, centre, radius):
            raise ValueError(f"set_circle {handle!r} is a no-op")
        geometry_seen.add(handle)
        set_circle.append({"handle": handle, "c": centre, "r": radius})

    set_arc: List[Dict[str, Any]] = []
    for position, raw in enumerate(set_arc_raw):
        if not isinstance(raw, dict) or set(raw) != _SET_ARC_FIELDS:
            raise ValueError(f"set_arc at index {position} has unknown or missing fields")
        handle, _kind, entity = _target(raw.get("handle"), f"set_arc[{position}]", ("ARC",))
        if handle in geometry_seen:
            raise ValueError(f"handle {handle!r} has more than one geometry operation")
        if not _normal_is_up(entity):
            raise ValueError(f"set_arc {handle!r}: a tilted arc is not editable here")
        centre = _point3(raw.get("c"), f"set_arc {handle!r} c")
        radius = _number(raw.get("r"), f"set_arc {handle!r} r")
        if radius <= 0:
            raise ValueError(f"set_arc {handle!r} r must be positive")
        start = _number(raw.get("start_deg"), f"set_arc {handle!r} start_deg", limit=MAX_ANGLE_DEG)
        end = _number(raw.get("end_deg"), f"set_arc {handle!r} end_deg", limit=MAX_ANGLE_DEG)
        if abs(math.fmod(end - start, 360.0)) < 1e-9:
            raise ValueError(f"set_arc {handle!r} has no sweep")
        if reject_noop and _same_round(entity, centre, radius, start, end):
            raise ValueError(f"set_arc {handle!r} is a no-op")
        geometry_seen.add(handle)
        set_arc.append({
            "handle": handle, "c": centre, "r": radius,
            "start_deg": start, "end_deg": end,
        })

    added: List[Dict[str, Any]] = []
    added_handles = set()
    for position, raw in enumerate(added_raw):
        if not isinstance(raw, dict):
            raise ValueError(f"added entity at index {position} must be an object")
        # Keep the routing skeleton's name-only placeholder refusal. INSERT
        # execution requires the complete transform, not just a capability tag.
        if raw.get("kind") == "INSERT" and set(raw) <= {"kind", "handle", "name"}:
            raise ValueError("contract v3 is not enabled on this deployment")
        if raw.get("kind") == "INSERT":
            allowed_fields = _INSERT_FIELDS
        elif raw.get("kind") == "DIMENSION":
            allowed_fields = _DIMENSION_FIELDS
        elif raw.get("kind") == "MLEADER":
            allowed_fields = _MLEADER_FIELDS
            if set(raw) & set(STYLE_FIELDS):
                raise ValueError("MLEADER carries no colour, linetype or lineweight in this contract")
        else:
            allowed_fields = _ADDED_FIELDS
        extra = set(raw) - allowed_fields - set(STYLE_FIELDS)
        if extra:
            raise ValueError(f"added entity at index {position} has unknown fields")
        handle = _handle(raw.get("handle"), f"added[{position}].handle")
        if handle in index or handle in added_handles:
            raise ValueError(f"duplicate or conflicting added handle {handle!r}")
        kind = raw.get("kind", "LWPOLYLINE")
        if kind not in _ADD_KINDS and kind not in V3_ADD_KINDS:
            raise ValueError(f"added entity {handle!r} has an unsupported kind")
        layer = _layer(raw.get("layer"))
        if kind == "MLEADER":
            layer = _canonicalize_layer(layer, intake)
            style = raw.get("style")
            if not isinstance(style, str) or not _LAYER_RE.fullmatch(style):
                raise ValueError("added MLEADER style is not a safe dimstyle name")
            if "mlstyles" in intake:
                catalogue = intake["mlstyles"]
                if not isinstance(catalogue, list):
                    raise ValueError("intake mlstyles must be a list")
                entry = next((item for item in catalogue
                              if isinstance(item, dict) and isinstance(item.get("name"), str)
                              and item["name"].lower() == style.lower()), None)
                if entry is None:
                    raise ValueError(f"mleader style {style} is not loaded in this drawing")
                if entry.get("segments") != 1:
                    raise ValueError("mleader style must take exactly two points in this contract")
                style = entry["name"]
            points_raw = raw.get("pts")
            if not isinstance(points_raw, list) or len(points_raw) != 2:
                raise ValueError("mleader requires exactly two points")
            points = [[0.0 if round(value, 3) == 0 else round(value, 3)
                       for value in _point3(point, "added MLEADER point")]
                      for point in points_raw]
            if any(point[2] != 0.0 for point in points):
                raise ValueError("mleader points must lie in the XY plane (z = 0)")
            if points[0] == points[1]:
                raise ValueError("mleader points coincide")
            text = raw.get("text")
            if not isinstance(text, str):
                raise ValueError("mleader text must be a string")
            if text != text.strip():
                raise ValueError("mleader text carries edge whitespace")
            if not 1 <= len(text) <= 256:
                raise ValueError("mleader text must contain 1..256 characters")
            if any(not 0x20 <= ord(char) <= 0x7E or char in "|\\%" for char in text):
                raise ValueError("mleader text must use the closed printable ASCII charset")
            total_points += 2
            if total_points > MAX_POINTS:
                raise ValueError("mutation point bound exceeded")
            added_handles.add(handle)
            added.append({"handle": handle, "kind": kind, "layer": layer,
                          "style": style, "pts": points, "text": text})
            continue
        if kind == "INSERT":
            layer = _canonicalize_layer(layer, intake)
            name = raw.get("name")
            if (not isinstance(name, str) or not name or len(name) > 255
                    or any(char in name for char in ("|", "\r", "\n"))):
                raise ValueError("added INSERT name is not a safe block name")
            if name.startswith("*"):
                raise ValueError("added INSERT cannot use a system or anonymous block name")
            # The plan reader uses the host MBCS code page until a decoder round.
            if any(not 0x20 <= ord(char) <= 0x7E for char in name):
                raise ValueError("block names outside printable ASCII are not carried in this round")
            blocks = intake.get("blocks")
            if name in {b["name"] for b in block_defs}:
                blocks = dict(blocks or {})
                blocks[name] = {"complete": True, "children": [], "count": 0}
            if not isinstance(blocks, dict) or name not in blocks:
                raise ValueError(f"block {name} is not defined in this drawing")
            block = blocks[name]
            if (not isinstance(block, dict) or block.get("complete") is not True
                    or block.get("baseUnknown")
                    or not isinstance(block.get("children"), list)
                    or block.get("count") != len(block["children"])):
                raise ValueError(f"block {name} is incomplete in this drawing")
            if not isinstance(raw.get("pt"), list) or len(raw["pt"]) != 3:
                raise ValueError(f"added INSERT {handle!r} pt must have three components")
            point = [round(value, 3) for value in _point3(raw["pt"], "added INSERT pt")]
            rotation = _number(raw.get("rot"), "added INSERT rot", limit=float("inf"))
            rotation = round(rotation % 360.0, 6) % 360.0
            if not isinstance(raw.get("scale"), list) or len(raw["scale"]) != 3:
                raise ValueError(f"added INSERT {handle!r} scale must have three components")
            scale = [round(_number(value, "added INSERT scale"), 4) for value in raw["scale"]]
            if any(value == 0 for value in scale):
                raise ValueError("added INSERT scale components must be non-zero")
            if any(isinstance(entity, dict) and entity.get("handle") == handle
                   for entity in intake.get("inserts") or []):
                raise ValueError(f"duplicate or conflicting added handle {handle!r}")
            total_points += 1
            if total_points > MAX_POINTS:
                raise ValueError("mutation point bound exceeded")
            added_handles.add(handle)
            added.append({
                "handle": handle, "kind": "INSERT", "name": name, "layer": layer,
                "pt": [0.0 if value == 0 else value for value in point],
                "rot": rotation, "scale": scale,
            })
            continue
        if kind == "DIMENSION":
            layer = _canonicalize_layer(layer, intake)
            dimtype = raw.get("dimtype")
            if dimtype not in _DIMTYPES:
                raise ValueError(f"added DIMENSION {handle!r} has an unsupported dimtype")
            def1 = [0.0 if round(value, 3) == 0 else round(value, 3)
                    for value in _point3(raw.get("def1"), f"added DIMENSION {handle!r} def1")]
            def2 = [0.0 if round(value, 3) == 0 else round(value, 3)
                    for value in _point3(raw.get("def2"), f"added DIMENSION {handle!r} def2")]
            if def1 == def2:
                raise ValueError("dimension definition points coincide")
            dimline_given = _point3(raw.get("dimline"), f"added DIMENSION {handle!r} dimline")
            # F9: the planar contract. def1, def2 and the supplied dimline
            # must all lie in z = 0 (after the same 3-dp quantization every
            # coordinate here carries); a def2 with real depth would silently
            # yield a 3-D measurement (e.g. 7.071 for def2 [3,4,5]) against a
            # tool that only ever draws in the XY plane.
            if (def1[2] != 0.0 or def2[2] != 0.0
                    or (0.0 if round(dimline_given[2], 3) == 0 else round(dimline_given[2], 3)) != 0.0):
                raise ValueError("dimension points must lie in the XY plane (z = 0) in this round")
            if dimtype == "LINEAR":
                if "rotation" not in raw:
                    raise ValueError(f"added DIMENSION {handle!r} LINEAR requires rotation")
                rotation = _number(raw.get("rotation"), "added DIMENSION rotation", limit=float("inf"))
                rotation = round(rotation % 360.0, 6) % 360.0
                axis = (math.cos(math.radians(rotation)), math.sin(math.radians(rotation)))
            else:
                if "rotation" in raw:
                    raise ValueError("rotation is only valid for LINEAR")
                rotation = 0.0
                axis_dx, axis_dy = def2[0] - def1[0], def2[1] - def1[1]
                axis_len = math.sqrt(axis_dx * axis_dx + axis_dy * axis_dy)
                if axis_len <= PLANAR_TOLERANCE:
                    raise ValueError("dimension definition points coincide")
                axis = (axis_dx / axis_len, axis_dy / axis_len)
            # AutoCAD stores group 10 not as the point the caller gave but as
            # def2 projected onto the dimension line (the line through the
            # given point, parallel to the dimension axis); canonicalizing
            # here keeps the plan, the mock writer and the verifier's readback
            # agreeing on the one point AutoCAD will actually persist.
            normal = (-axis[1], axis[0])
            offset_given = ((dimline_given[0] - def1[0]) * normal[0]
                            + (dimline_given[1] - def1[1]) * normal[1])
            offset_def2 = (def2[0] - def1[0]) * normal[0] + (def2[1] - def1[1]) * normal[1]
            shift = offset_given - offset_def2
            projected = (def2[0] + shift * normal[0], def2[1] + shift * normal[1], def2[2])
            # F7: off an axis-aligned rotation the 3-dp round is not a fixed
            # point of this projection (re-projecting the rounded point can
            # move it back out by up to ~6e-4, more than the 5e-4 half
            # quantum), so a re-validate of an already-canonical dimline
            # could drift by 0.001 forever. When the supplied point already
            # lies within the measurement tolerance of its OWN projection
            # (it is canonical up to the quantum), it is kept exactly as
            # given instead of re-derived, making a second validate a no-op.
            if (abs(dimline_given[0] - projected[0]) <= DIMENSION_MEASUREMENT_TOLERANCE
                    and abs(dimline_given[1] - projected[1]) <= DIMENSION_MEASUREMENT_TOLERANCE
                    and abs(dimline_given[2] - projected[2]) <= DIMENSION_MEASUREMENT_TOLERANCE):
                dimline = [0.0 if round(value, 3) == 0 else round(value, 3)
                           for value in dimline_given]
            else:
                dimline = [0.0 if round(value, 3) == 0 else round(value, 3)
                           for value in projected]
            style = raw.get("style")
            if not isinstance(style, str) or not _LAYER_RE.fullmatch(style):
                raise ValueError("added DIMENSION style is not a safe dimstyle name")
            dimstyles = intake.get("dimstyles")
            if isinstance(dimstyles, list):
                if style not in dimstyles:
                    raise ValueError(f"dimstyle {style} is not loaded in this drawing")
            elif style != "Standard":
                raise ValueError(f"dimstyle {style} is not loaded in this drawing")
            dx = def2[0] - def1[0]
            dy = def2[1] - def1[1]
            dz = def2[2] - def1[2]
            if dimtype == "LINEAR":
                radians = math.radians(rotation)
                measurement = round(abs(dx * math.cos(radians) + dy * math.sin(radians)), 3)
            else:
                measurement = round(math.sqrt(dx * dx + dy * dy + dz * dz), 3)
            # F5 (fail-open): a supplied measurement is never silently
            # replaced by the computed one. Refuse a disagreeing supplied
            # value outright rather than discarding it; an agreeing one (or
            # none at all) keeps the computed value, so idempotency on an
            # already-canonical record still holds.
            if "measurement" in raw:
                given_measurement = _number(
                    raw.get("measurement"), f"added DIMENSION {handle!r} measurement",
                    limit=float("inf"))
                if abs(given_measurement - measurement) > DIMENSION_MEASUREMENT_TOLERANCE:
                    raise ValueError(
                        f"dimension measurement {given_measurement} disagrees with the "
                        f"definition points ({measurement})")
            added_handles.add(handle)
            entity = {
                "handle": handle, "kind": "DIMENSION", "dimtype": dimtype, "layer": layer,
                "def1": def1, "def2": def2, "dimline": dimline, "style": style,
                "measurement": measurement,
            }
            if dimtype == "LINEAR":
                entity["rotation"] = rotation
            added.append(entity)
            continue
        xdata = raw.get("xdata")
        if xdata is not None and not allow_xdata:
            # The MVP Activity creates geometry only. Silently discarding xdata
            # would make the declared and persisted mutation differ.
            raise ValueError(f"added entity {handle!r} xdata is not supported")
        if kind == "LWPOLYLINE":
            for field in ("c", "r", "start_deg", "end_deg"):
                if field in raw:
                    raise ValueError(f"added entity {handle!r} has unknown fields")
            closed = raw.get("closed")
            if not isinstance(closed, bool):
                raise ValueError(f"added entity {handle!r} must be a closed polyline")
            points = _points(raw.get("pts"), f"added entity {handle!r}", minimum=3 if closed else 2)
            total_points += len(points)
            if total_points > MAX_POINTS:
                raise ValueError("mutation point bound exceeded")
            added_handles.add(handle)
            added.append({
                "handle": handle, "layer": layer, "closed": closed, "pts": points,
                "xdata": xdata,
            })
            continue
        if xdata is not None or "closed" in raw:
            raise ValueError(f"added entity {handle!r} has unknown fields")
        if kind == "LINE":
            for field in ("c", "r", "start_deg", "end_deg"):
                if field in raw:
                    raise ValueError(f"added entity {handle!r} has unknown fields")
            points = _points(raw.get("pts"), f"added entity {handle!r}", minimum=2)
            if len(points) != 2:
                raise ValueError(f"added entity {handle!r} has an invalid point count")
            if points[0] == points[1]:
                raise ValueError(f"added entity {handle!r} has zero length")
            total_points += 2
            if total_points > MAX_POINTS:
                raise ValueError("mutation point bound exceeded")
            added_handles.add(handle)
            added.append({"handle": handle, "kind": "LINE", "layer": layer, "pts": points})
            continue
        if "pts" in raw:
            raise ValueError(f"added entity {handle!r} has unknown fields")
        centre = _point3(raw.get("c"), f"added entity {handle!r} c")
        radius = _number(raw.get("r"), f"added entity {handle!r} r")
        if radius <= 0:
            raise ValueError(f"added entity {handle!r} r must be positive")
        if kind == "CIRCLE":
            for field in ("start_deg", "end_deg"):
                if field in raw:
                    raise ValueError(f"added entity {handle!r} has unknown fields")
            added_handles.add(handle)
            added.append({"handle": handle, "kind": "CIRCLE", "layer": layer, "c": centre, "r": radius})
            continue
        start = _number(raw.get("start_deg"), f"added entity {handle!r} start_deg", limit=MAX_ANGLE_DEG)
        end = _number(raw.get("end_deg"), f"added entity {handle!r} end_deg", limit=MAX_ANGLE_DEG)
        if abs(math.fmod(end - start, 360.0)) < 1e-9:
            raise ValueError(f"added entity {handle!r} has no sweep")
        added_handles.add(handle)
        added.append({
            "handle": handle, "kind": "ARC", "layer": layer, "c": centre, "r": radius,
            "start_deg": start, "end_deg": end,
        })

    # Style does not change canonical geometry or the add ordinal.
    styles = {raw["handle"]: {field: _style_value(field, raw[field], known_linetypes)
                              for field in STYLE_FIELDS if field in raw}
              for raw in added_raw}
    added.sort(key=canonical_json_bytes)
    canonical_ordinals = {entity["handle"]: i for i, entity in enumerate(added)}
    submitted_ordinals = {i: canonical_ordinals[entity["handle"]]
                          for i, entity in enumerate(added_raw)}
    for definition in block_defs:
        definition["insert"] = submitted_ordinals[definition["insert"]]
    for entity in added:
        entity.update(styles[entity["handle"]])
    property_index: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    if any(style_raw.values()):
        # Only built (and only able to raise on a duplicate insert handle)
        # when a style op actually needs it: a v1/v2-only plan never pays
        # for, or is refused by, a property-targeting concern it never uses.
        property_index = dict(index)
        for entity in intake.get("inserts") or []:
            handle = entity.get("handle")
            if handle in property_index:
                raise ValueError(f"ambiguous property handle {handle!r}")
            property_index[handle] = ("INSERT", entity)
    canonical: Dict[str, Any] = {}
    if block_defs:
        canonical["block_defs"] = block_defs
    if added_groups_raw or removed_groups_raw:
        def group_name(value):
            if isinstance(value, str):
                value = value.upper()
                if value != value.strip():
                    raise ValueError("group name must not have leading or trailing whitespace or be whitespace only")
            if (not isinstance(value, str) or not 1 <= len(value) <= 255
                    or any(c in '<>/\\\\":;?*|,=`' for c in value)
                    or any(not 0x20 <= ord(c) <= 0x7E for c in value)):
                raise ValueError("group name must be safe printable ASCII, 1..255 characters")
            return value.upper()

        head_groups = {g["name"].casefold(): g for g in intake.get("groups", [])}
        removed_names = {}
        for raw in removed_groups_raw:
            name = group_name(raw)
            key = name.casefold()
            if key in removed_names:
                raise ValueError("removed group names must be unique case-insensitively")
            if key not in head_groups:
                raise ValueError(f"removed group {name!r} must exist")
            removed_names[key] = name
        member_handles = set()
        ambiguous_members = set()
        supported_kinds = {"LINE", "LWPOLYLINE", "CIRCLE", "ARC", "TEXT", "INSERT",
                           "DIMENSION", "POINT", "ELLIPSE"}
        for field in ("polylines", "circles", "arcs", "texts", "inserts",
                      "dimensions", "points", "ellipses"):
            for entity in intake.get(field, []):
                handle = entity.get("handle")
                if (isinstance(handle, str) and _EXISTING_HANDLE_RE.fullmatch(handle)
                        and not entity.get("paper_space") and not entity.get("paperspace")
                        and entity.get("space", "model") in ("model", "Model", "ModelSpace", 0)
                        and not entity.get("block")
                        and entity.get("kind", "LINE") in supported_kinds):
                    if handle.upper() in member_handles:
                        ambiguous_members.add(handle.upper())
                    member_handles.add(handle.upper())
        member_handles.difference_update(ambiguous_members)
        for block in (intake.get("blocks") or {}).values():
            for child in block.get("children", []):
                member_handles.discard(str(child.get("handle", "")).upper())
        groups = []
        names = set()
        for raw in added_groups_raw:
            if not isinstance(raw, dict) or set(raw) != {"name", "members"}:
                raise ValueError("added group requires name and members only")
            name = group_name(raw["name"])
            key = name.casefold()
            if key in names or (key in head_groups and key not in removed_names):
                raise ValueError(f"group name {name!r} collides case-insensitively")
            names.add(key)
            if not isinstance(raw["members"], list) or len(raw["members"]) < 2:
                raise ValueError("added group requires at least two distinct members")
            members, seen = [], set()
            for member in raw["members"]:
                if isinstance(member, str):
                    handle = _existing_handle(member, "group member").upper()
                    if handle not in member_handles:
                        raise ValueError("group member must be an existing supported model-space entity")
                    if handle in {h.upper() for h in removed_seen}:
                        raise ValueError("group member is also removed")
                    token, value = ("H", handle), handle
                elif (isinstance(member, dict) and set(member) == {"add"}
                      and type(member["add"]) is int and 0 <= member["add"] < len(added)):
                    ordinal = submitted_ordinals[member["add"]]
                    token, value = ("A", ordinal), {"add": ordinal}
                else:
                    raise ValueError("group member ordinal must resolve inside canonical added")
                if token in seen:
                    raise ValueError("added group members must be distinct")
                seen.add(token)
                members.append(value)
            groups.append({"name": name, "members": members})
        if groups:
            canonical["added_groups"] = sorted(groups, key=lambda g: g["name"])
        if removed_names:
            canonical["removed_groups"] = sorted(removed_names.values())
    for op, field in zip(V3_SET_OPS, STYLE_FIELDS):
        key = STYLE_FIELDS[field]
        entries = []
        seen = set()
        for raw in style_raw[op]:
            if not isinstance(raw, dict) or set(raw) != {"handle", key}:
                raise ValueError(f"{op} has unknown or missing fields")
            handle = _existing_handle(raw["handle"], op)
            if handle in seen:
                raise ValueError(f"duplicate {op} handle {handle!r}")
            if handle in removed_seen:
                raise ValueError(f"property target {handle!r} is also removed")
            if handle not in property_index:
                raise ValueError(f"unknown {op} handle {handle!r}")
            if property_index[handle][0] == "MULTILEADER":
                raise ValueError("MLEADER is not a property target in this contract")
            value = _style_value(field, raw[key], known_linetypes)
            prop_key = {"color": "aci", "linetype": "linetype", "lineweight": "lineweight"}[field]
            properties = (intake.get("properties") or {}).get(handle, {})
            # w4g-7b-03s-d D3: a linetype no-op compares case-insensitively
            # (the head's own spelling and the canonicalized request may
            # differ only in case); the other two properties compare exact.
            current = properties.get(prop_key)
            if field == "linetype":
                is_same = (prop_key in properties and isinstance(current, str)
                           and current.lower() == value.lower())
            else:
                is_same = prop_key in properties and current == value
            if (reject_noop and is_same
                    and (field != "color" or properties.get("rgb") is None)):
                raise ValueError(f"{op} {handle!r} is a no-op")
            seen.add(handle)
            entries.append({"handle": handle, key: value})
        if entries:
            canonical[op] = sorted(entries, key=lambda item: int(item["handle"], 16))
    if added:
        canonical["added"] = added
    if removed:
        canonical["removed"] = sorted(removed)
    if removed_kinds:
        canonical["removed_kinds"] = dict(sorted(removed_kinds.items()))
    if transforms:
        canonical["transforms"] = sorted(
            transforms, key=lambda item: item["handle"])
    if set_layer:
        canonical["set_layer"] = sorted(set_layer, key=lambda item: item["handle"])
    if set_points:
        canonical["set_points"] = sorted(set_points, key=lambda item: item["handle"])
    if set_circle:
        canonical["set_circle"] = sorted(set_circle, key=lambda item: item["handle"])
    if set_arc:
        canonical["set_arc"] = sorted(set_arc, key=lambda item: item["handle"])
    if len(canonical_json_bytes(canonical)) > MAX_PLAN_BYTES:
        raise ValueError("canonical mutation data exceeds the byte bound")
    return canonical


def uses_v3(canonical: Any) -> bool:
    """True when canonical data carries a declared v3 capability."""
    if not isinstance(canonical, dict):
        return False
    if isinstance(canonical.get("block_defs"), list) and any(
            isinstance(b, dict) for b in canonical["block_defs"]):
        return True
    if (isinstance(canonical.get("added_groups"), list)
            and any(isinstance(g, dict) for g in canonical["added_groups"])) or (
            isinstance(canonical.get("removed_groups"), list)
            and any(isinstance(n, str) and n for n in canonical["removed_groups"])):
        return True
    for field in V3_SET_OPS:
        entries = canonical.get(field)
        if isinstance(entries, list) and any(isinstance(entry, dict) for entry in entries):
            return True
    removed_kinds = canonical.get("removed_kinds")
    if isinstance(removed_kinds, dict) and any(
            kind in ("DIMENSION", "MULTILEADER") for kind in removed_kinds.values()):
        return True
    added = canonical.get("added")
    return isinstance(added, list) and any(
        isinstance(entity, dict) and (entity.get("kind") in V3_ADD_KINDS
                                     or any(field in entity for field in STYLE_FIELDS))
        for entity in added
    )


def uses_v2(canonical: Dict[str, Any]) -> bool:
    """True when the canonical data needs the v2 interpreter: any replacement
    op, a non-polyline removal, or an added entity that is not a closed
    LWPOLYLINE."""
    if any(canonical.get(field) for field in _V2_FIELDS):
        return True
    if canonical.get("removed_kinds"):
        return True
    for entity in canonical.get("added", []):
        if entity.get("kind", "LWPOLYLINE") != "LWPOLYLINE" or entity.get("closed") is not True:
            return True
    return False


def _dot(left: Iterable[float], right: Iterable[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def _cross(left: Iterable[float], right: Iterable[float]) -> Tuple[float, float, float]:
    ax, ay, az = left
    bx, by, bz = right
    return ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx


def _unit(vector: Iterable[float]) -> Tuple[float, float, float]:
    values = tuple(vector)
    length = math.sqrt(_dot(values, values))
    if length <= PLANAR_TOLERANCE:
        raise ValueError("added polyline has no stable plane normal")
    return tuple(value / length for value in values)  # type: ignore[return-value]


def world_to_ocs(points: List[List[float]]) -> Dict[str, Any]:
    """Lower one planar WCS polyline to AutoCAD's arbitrary-axis OCS."""
    origin = points[0]
    normal = None
    for index in range(1, len(points) - 1):
        first = [points[index][axis] - origin[axis] for axis in range(3)]
        second = [points[index + 1][axis] - origin[axis] for axis in range(3)]
        candidate = _cross(first, second)
        if math.sqrt(_dot(candidate, candidate)) > PLANAR_TOLERANCE:
            normal = _unit(candidate)
            break
    if normal is None:
        raise ValueError("added polyline points are collinear")
    if normal[2] < 0 or (normal[2] == 0 and (normal[1] < 0 or (normal[1] == 0 and normal[0] < 0))):
        normal = tuple(-value for value in normal)
    if abs(normal[0]) < 1.0 / 64.0 and abs(normal[1]) < 1.0 / 64.0:
        axis_x = _unit(_cross((0.0, 1.0, 0.0), normal))
    else:
        axis_x = _unit(_cross((0.0, 0.0, 1.0), normal))
    axis_y = _cross(normal, axis_x)
    elevation = _dot(origin, normal)
    ocs_points = []
    for point in points:
        if abs(_dot(point, normal) - elevation) > PLANAR_TOLERANCE:
            raise ValueError("added polyline is not planar")
        ocs_points.append([_dot(point, axis_x), _dot(point, axis_y)])
    return {
        "normal": list(normal), "elevation": elevation, "points": ocs_points,
        "axis_x": list(axis_x), "axis_y": list(axis_y),
    }


def world_to_ocs_any(points: List[List[float]]) -> Dict[str, Any]:
    """`world_to_ocs` for a point set that may be collinear (an open two-point
    polyline, a LINE-shaped LWPOLYLINE): a collinear set lies in every plane
    through it, so the +z plane at the set's elevation is chosen when every z
    agrees; a collinear set with differing z has no planar LWPOLYLINE form."""
    try:
        return world_to_ocs(points)
    except ValueError as exc:
        if "collinear" not in str(exc):
            raise
    zs = {round(float(point[2]), 9) for point in points}
    if len(zs) != 1:
        raise ValueError("collinear points with differing z have no planar polyline form")
    elevation = float(points[0][2])
    return {
        "normal": list(_UP_NORMAL), "elevation": elevation,
        "points": [[float(point[0]), float(point[1])] for point in points],
        "axis_x": [1.0, 0.0, 0.0], "axis_y": [0.0, 1.0, 0.0],
    }


def _fmt(value: float) -> str:
    value = 0.0 if abs(value) < 5e-13 else value
    return format(value, ".12g")


def _fmt3(point: List[float]) -> str:
    return ",".join(_fmt(float(value)) for value in point[:3])


def transformed_points(
    points: List[List[float]], transform: Dict[str, Any],
) -> List[List[float]]:
    """Apply one frozen panel-transform/v1 operation in world XY."""
    center_x = sum(float(point[0]) for point in points) / len(points)
    center_y = sum(float(point[1]) for point in points) / len(points)
    radians = math.radians(float(transform.get("rotation_deg", 0)))
    cosine, sine = math.cos(radians), math.sin(radians)
    dx, dy = float(transform["dx"]), float(transform["dy"])
    result = []
    for point in points:
        relative_x = float(point[0]) - center_x
        relative_y = float(point[1]) - center_y
        transformed = list(point)
        transformed[0] = round(
            center_x + relative_x * cosine - relative_y * sine + dx, 9)
        transformed[1] = round(
            center_y + relative_x * sine + relative_y * cosine + dy, 9)
        transformed[0] = 0.0 if transformed[0] == 0 else transformed[0]
        transformed[1] = 0.0 if transformed[1] == 0 else transformed[1]
        result.append(transformed)
    return result


def _ocs_line(tag: str, head: str, lowered: Dict[str, Any]) -> str:
    normal = ",".join(_fmt(value) for value in lowered["normal"])
    vertices = ";".join(
        ",".join(_fmt(value) for value in point)
        for point in lowered["points"]
    )
    return f"{tag}|{head}|{normal}|{_fmt(lowered['elevation'])}|{vertices}"


def emit_plan(
    canonical: Dict[str, Any], *, base_sha256: str,
    base_intake: Optional[Dict[str, Any]] = None,
    contract: int | None = None,
) -> bytes:
    """Emit the exact data-only Activity input for one canonical mutation set."""
    if not re.fullmatch(r"[0-9a-f]{64}", base_sha256):
        raise ValueError("base_sha256 must be lowercase hex")
    if contract is not None and (type(contract) is not int or contract not in (2, 3)):
        raise ValueError("contract must be 2 or 3")
    version = contract if contract is not None else (
        3 if uses_v3(canonical) else 2 if uses_v2(canonical) else 1)
    if uses_v3(canonical) and version != 3:
        raise ValueError("contract v3 is required for property operations")
    lines = [f"LEAF_MUTATION_PLAN|{version}", f"BASE_SHA256|{base_sha256}"]
    for definition in canonical.get("block_defs", []):
        members = ";".join(f"H:{h}" for h in definition["members"])
        base = ",".join(f"{v:.3f}" for v in definition["base"])
        lines.append(f"ADDBLOCKDEF|{definition['name']}|{base}|{members}")
    for name in canonical.get("removed_groups", []):
        lines.append(f"REMOVEGROUP|{name.upper()}")
    for handle in ([] if canonical.get("block_defs") else canonical.get("removed", [])):
        lines.append(f"REMOVE|{handle}")
    if canonical.get("transforms"):
        if base_intake is None:
            raise ValueError("base_intake is required for live transforms")
        existing = _existing_by_handle(base_intake)
        for transform in canonical["transforms"]:
            handle = transform["handle"]
            entity = existing.get(handle)
            if entity is None:
                raise ValueError(f"transform handle {handle!r} is unavailable")
            points = entity.get("pts")
            if not isinstance(points, list) or len(points) < 3:
                raise ValueError(
                    f"transform handle {handle!r} has invalid source geometry")
            target = transformed_points(points, transform)
            lines.append(_ocs_line("TRANSFORM", handle, world_to_ocs(target)))
    for item in canonical.get("set_layer", []):
        lines.append(f"RELAYER|{item['handle']}|{item['layer']}")
    for item in canonical.get("set_points", []):
        lowered = world_to_ocs_any(item["pts"])
        lines.append(_ocs_line("SETPOINTS", f"{item['handle']}|{1 if item['closed'] else 0}", lowered))
    for item in canonical.get("set_circle", []):
        lines.append(f"SETCIRCLE|{item['handle']}|{_fmt3(item['c'])}|{_fmt(item['r'])}")
    for item in canonical.get("set_arc", []):
        lines.append(
            f"SETARC|{item['handle']}|{_fmt3(item['c'])}|{_fmt(item['r'])}|"
            f"{_fmt(item['start_deg'])}|{_fmt(item['end_deg'])}")
    for entity in canonical.get("added", []):
        layer = entity["layer"]
        kind = entity.get("kind", "LWPOLYLINE")
        if kind == "LWPOLYLINE":
            if entity.get("closed") is True:
                lines.append(_ocs_line("ADD", layer, world_to_ocs(entity["pts"])))
            else:
                lines.append(_ocs_line("ADDOPEN", layer, world_to_ocs_any(entity["pts"])))
        elif kind == "LINE":
            lines.append(f"ADDLINE|{layer}|{_fmt3(entity['pts'][0])}|{_fmt3(entity['pts'][1])}")
        elif kind == "CIRCLE":
            lines.append(f"ADDCIRCLE|{layer}|{_fmt3(entity['c'])}|{_fmt(entity['r'])}")
        elif kind == "INSERT":
            point = ",".join(format(value, ".3f") for value in entity["pt"])
            scale = ",".join(format(value, ".4f") for value in entity["scale"])
            lines.append(
                f"ADDINSERT|{layer}|{entity['name']}|{point}|{entity['rot']:.6f}|{scale}")
        elif kind == "DIMENSION":
            def1 = ",".join(format(value, ".3f") for value in entity["def1"])
            def2 = ",".join(format(value, ".3f") for value in entity["def2"])
            dimline = ",".join(format(value, ".3f") for value in entity["dimline"])
            if entity["dimtype"] == "LINEAR":
                lines.append(
                    f"ADDDIMLINEAR|{layer}|{entity['style']}|{def1}|{def2}|{dimline}|"
                    f"{entity['rotation']:.6f}")
            else:
                lines.append(f"ADDDIMALIGNED|{layer}|{entity['style']}|{def1}|{def2}|{dimline}")
        elif kind == "MLEADER":
            first, second = [",".join(format(value, ".3f") for value in point)
                             for point in entity["pts"]]
            lines.append(f"ADDMLEADER|{layer}|{entity['style']}|{first}|{second}|{entity['text']}")
        else:
            lines.append(
                f"ADDARC|{layer}|{_fmt3(entity['c'])}|{_fmt(entity['r'])}|"
                f"{_fmt(entity['start_deg'])}|{_fmt(entity['end_deg'])}")
    for op, field, tag in zip(V3_SET_OPS, STYLE_FIELDS,
                              ("SETCOLOR", "SETLINETYPE", "SETLINEWEIGHT")):
        for item in sorted(canonical.get(op, []), key=lambda item: int(item["handle"], 16)):
            lines.append(f"{tag}|H:{item['handle']}|{item[STYLE_FIELDS[field]]}")
        for ordinal, entity in enumerate(canonical.get("added", [])):
            if field in entity:
                lines.append(f"{tag}|A:{ordinal}|{entity[field]}")
    for group in canonical.get("added_groups", []):
        members = ";".join(f"H:{m}" if isinstance(m, str) else f"A:{m['add']}"
                           for m in group["members"])
        lines.append(f"ADDGROUP|{group['name'].upper()}|{members}")
    if canonical.get("block_defs"):
        lines.extend(f"REMOVE|{h}" for h in canonical.get("removed", []))
    plan = ("\n".join(lines) + "\n").encode("utf-8" if version == 3 else "ascii")
    if len(plan) > MAX_PLAN_BYTES:
        raise ValueError("mutation plan exceeds the byte bound")
    return plan


def plan_sha256(plan: bytes) -> str:
    return hashlib.sha256(plan).hexdigest()
