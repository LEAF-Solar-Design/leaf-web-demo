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
    "added", "removed", "transforms",
    "set_layer", "set_points", "set_circle", "set_arc",
})
_V2_FIELDS = frozenset({"set_layer", "set_points", "set_circle", "set_arc"})
_ADDED_FIELDS = frozenset({
    "handle", "kind", "layer", "closed", "pts", "xdata", "c", "r",
    "start_deg", "end_deg",
})
_ADD_KINDS = ("LWPOLYLINE", "LINE", "CIRCLE", "ARC")
_INSERT_FIELDS = frozenset({"handle", "kind", "layer", "name", "pt", "rot", "scale"})
V3_ADD_KINDS = ("INSERT", "DIMENSION")
V3_SET_OPS = ("set_color", "set_linetype", "set_lineweight")
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
    for field, kind in (("polylines", "LWPOLYLINE"), ("circles", "CIRCLE"), ("arcs", "ARC")):
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


def validate_mutations(
    intake: Dict[str, Any], mutations: Any, *, allow_transforms: bool = True,
    allow_xdata: bool = False, reject_noop: bool = True,
) -> Dict[str, Any]:
    """Strictly validate and canonicalize the frozen mutation data contract."""
    if not isinstance(mutations, dict):
        raise ValueError("result.mutations must be an object")
    if any(field in mutations for field in V3_SET_OPS):
        raise ValueError("contract v3 is not enabled on this deployment")
    # `removed_kinds` is a canonical-only annotation this function writes (the
    # kind of each non-polyline removal, for the plan header); on input it is
    # never trusted, only recomputed, so a canonical set re-validates cleanly.
    unknown = set(mutations) - _MUTATION_FIELDS - {"removed_kinds"}
    if unknown:
        raise ValueError(f"unknown mutation fields: {', '.join(sorted(map(str, unknown)))}")
    _reject_raw_fields(mutations)
    index = _index_intake(intake)
    removed_raw = _op_list(mutations, "removed")
    added_raw = _op_list(mutations, "added")
    transforms_raw = _op_list(mutations, "transforms")
    set_layer_raw = _op_list(mutations, "set_layer")
    set_points_raw = _op_list(mutations, "set_points")
    set_circle_raw = _op_list(mutations, "set_circle")
    set_arc_raw = _op_list(mutations, "set_arc")
    op_count = (
        len(removed_raw) + len(added_raw) + len(transforms_raw)
        + len(set_layer_raw) + len(set_points_raw) + len(set_circle_raw)
        + len(set_arc_raw)
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
        if raw.get("kind") == "DIMENSION":
            raise ValueError("contract v3 is not enabled on this deployment")
        # Keep the routing skeleton's name-only placeholder refusal. INSERT
        # execution requires the complete transform, not just a capability tag.
        if raw.get("kind") == "INSERT" and set(raw) <= {"kind", "handle", "name"}:
            raise ValueError("contract v3 is not enabled on this deployment")
        extra = set(raw) - (_INSERT_FIELDS if raw.get("kind") == "INSERT" else _ADDED_FIELDS)
        if extra:
            raise ValueError(f"added entity at index {position} has unknown fields")
        handle = _handle(raw.get("handle"), f"added[{position}].handle")
        if handle in index or handle in added_handles:
            raise ValueError(f"duplicate or conflicting added handle {handle!r}")
        kind = raw.get("kind", "LWPOLYLINE")
        if kind not in _ADD_KINDS and kind != "INSERT":
            raise ValueError(f"added entity {handle!r} has an unsupported kind")
        layer = _layer(raw.get("layer"))
        if kind == "INSERT":
            name = raw.get("name")
            if (not isinstance(name, str) or not name or len(name) > 255
                    or any(char in name for char in ("|", "\r", "\n"))):
                raise ValueError("added INSERT name is not a safe block name")
            if name.startswith("*"):
                raise ValueError("added INSERT cannot use a system or anonymous block name")
            blocks = intake.get("blocks")
            if not isinstance(blocks, dict) or name not in blocks:
                raise ValueError(f"block {name} is not defined in this drawing")
            block = blocks[name]
            if (not isinstance(block, dict) or block.get("complete") is not True
                    or block.get("baseUnknown")):
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

    canonical: Dict[str, Any] = {}
    if added:
        canonical["added"] = sorted(
            added, key=lambda item: canonical_json_bytes(item))
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
    for field in V3_SET_OPS:
        entries = canonical.get(field)
        if isinstance(entries, list) and any(isinstance(entry, dict) for entry in entries):
            return True
    added = canonical.get("added")
    return isinstance(added, list) and any(
        isinstance(entity, dict) and entity.get("kind") in V3_ADD_KINDS
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
    lines = [f"LEAF_MUTATION_PLAN|{version}", f"BASE_SHA256|{base_sha256}"]
    for handle in canonical.get("removed", []):
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
        else:
            lines.append(
                f"ADDARC|{layer}|{_fmt3(entity['c'])}|{_fmt(entity['r'])}|"
                f"{_fmt(entity['start_deg'])}|{_fmt(entity['end_deg'])}")
    plan = ("\n".join(lines) + "\n").encode("utf-8" if version == 3 else "ascii")
    if len(plan) > MAX_PLAN_BYTES:
        raise ValueError("mutation plan exceeds the byte bound")
    return plan


def plan_sha256(plan: bytes) -> str:
    return hashlib.sha256(plan).hexdigest()
