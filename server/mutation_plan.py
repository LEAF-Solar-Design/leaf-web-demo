"""Validation and deterministic lowering for authored drawing mutations.

This module is deliberately pure. It accepts only the product mutation data
contract and emits a closed, line-oriented data plan. It never accepts or
emits AutoLISP, command text, file paths, or other executable input.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Dict, Iterable, List, Tuple


MAX_PLAN_BYTES = 1_048_576
MAX_OPERATIONS = 5_000
MAX_ENTITIES = 200_000
MAX_POINTS = 100_000
MAX_POINTS_PER_ENTITY = 10_000
MAX_COORDINATE = 1_000_000_000.0
PLANAR_TOLERANCE = 1e-6
_HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_EXISTING_HANDLE_RE = re.compile(r"^[0-9A-Fa-f]{1,32}$")
_LAYER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.$-]{0,254}$")
_MUTATION_FIELDS = frozenset({"added", "removed", "transforms"})
_ADDED_FIELDS = frozenset({"handle", "layer", "closed", "pts", "xdata"})
_TRANSFORM_FIELDS = frozenset({"handle", "dx", "dy", "rotation_deg"})
_RAW_FIELDS = frozenset({
    "code", "command", "commands", "script", "lisp", "autolisp", "shell",
    "powershell", "python", "path", "url",
})


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _number(value: Any, field: str, *, limit: float = MAX_COORDINATE) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    result = float(value)
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


def _existing_by_handle(intake: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    if not isinstance(intake, dict):
        raise ValueError("intake must be an object")
    polylines = intake.get("polylines") or []
    if not isinstance(polylines, list) or len(polylines) > MAX_ENTITIES:
        raise ValueError("intake polylines exceed the supported entity bound")
    result: Dict[str, Dict[str, Any]] = {}
    ambiguous = set()
    for entity in polylines:
        if not isinstance(entity, dict):
            continue
        handle = entity.get("handle")
        if not isinstance(handle, str) or not handle:
            continue
        if handle in result:
            ambiguous.add(handle)
        else:
            result[handle] = entity
    for handle in ambiguous:
        result.pop(handle, None)
    return result


def validate_mutations(
    intake: Dict[str, Any], mutations: Any, *, allow_transforms: bool = True,
    allow_xdata: bool = False, reject_noop: bool = True,
) -> Dict[str, Any]:
    """Strictly validate and canonicalize the frozen mutation data contract."""
    if not isinstance(mutations, dict):
        raise ValueError("result.mutations must be an object")
    unknown = set(mutations) - _MUTATION_FIELDS
    if unknown:
        raise ValueError(f"unknown mutation fields: {', '.join(sorted(map(str, unknown)))}")
    _reject_raw_fields(mutations)
    existing = _existing_by_handle(intake)
    removed_raw = mutations.get("removed", [])
    added_raw = mutations.get("added", [])
    transforms_raw = mutations.get("transforms", [])
    if not isinstance(removed_raw, list) or not isinstance(added_raw, list):
        raise ValueError("mutations.added and mutations.removed must be lists")
    if not isinstance(transforms_raw, list):
        raise ValueError("mutations.transforms must be a list")
    op_count = len(removed_raw) + len(added_raw) + len(transforms_raw)
    if op_count == 0 and reject_noop:
        raise ValueError("mutations must contain at least one operation")
    if op_count > MAX_OPERATIONS:
        raise ValueError("mutation operation bound exceeded")

    removed: List[str] = []
    removed_seen = set()
    for index, raw in enumerate(removed_raw):
        handle = _existing_handle(raw, f"removed[{index}]")
        if handle in removed_seen:
            raise ValueError(f"duplicate removed handle {handle!r}")
        if handle not in existing:
            raise ValueError(f"unknown removed handle {handle!r}")
        removed_seen.add(handle)
        removed.append(handle)

    transforms: List[Dict[str, Any]] = []
    transformed_seen = set()
    if transforms_raw and not allow_transforms:
        raise ValueError("live mutation plans do not yet support transforms")
    for index, raw in enumerate(transforms_raw):
        if not isinstance(raw, dict) or set(raw) - _TRANSFORM_FIELDS:
            raise ValueError(f"transform at index {index} has unknown fields")
        handle = _existing_handle(
            raw.get("handle"), f"transforms[{index}].handle")
        if handle not in existing:
            raise ValueError(f"unknown transform handle {handle!r}")
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

    added: List[Dict[str, Any]] = []
    added_handles = set()
    total_points = 0
    for index, raw in enumerate(added_raw):
        if not isinstance(raw, dict):
            raise ValueError(f"added entity at index {index} must be an object")
        extra = set(raw) - _ADDED_FIELDS
        if extra:
            raise ValueError(f"added entity at index {index} has unknown fields")
        handle = _handle(raw.get("handle"), f"added[{index}].handle")
        if handle in existing or handle in added_handles:
            raise ValueError(f"duplicate or conflicting added handle {handle!r}")
        if raw.get("closed") is not True:
            raise ValueError(f"added entity {handle!r} must be a closed polyline")
        layer = _layer(raw.get("layer"))
        points_raw = raw.get("pts")
        if not isinstance(points_raw, list) or not 3 <= len(points_raw) <= MAX_POINTS_PER_ENTITY:
            raise ValueError(f"added entity {handle!r} has an invalid point count")
        total_points += len(points_raw)
        if total_points > MAX_POINTS:
            raise ValueError("mutation point bound exceeded")
        points: List[List[float]] = []
        for point_index, point in enumerate(points_raw):
            if not isinstance(point, list) or len(point) not in (2, 3):
                raise ValueError(f"added entity {handle!r} has malformed point {point_index}")
            xyz = [
                _number(value, f"added {handle!r} point {point_index}")
                for value in point
            ]
            if len(xyz) == 2:
                xyz.append(0.0)
            points.append(xyz)
        xdata = raw.get("xdata")
        if xdata is not None and not allow_xdata:
            # The MVP Activity creates geometry only. Silently discarding xdata
            # would make the declared and persisted mutation differ.
            raise ValueError(f"added entity {handle!r} xdata is not supported")
        added_handles.add(handle)
        added.append({
            "handle": handle, "layer": layer, "closed": True, "pts": points,
            "xdata": xdata,
        })

    canonical: Dict[str, Any] = {}
    if added:
        canonical["added"] = sorted(
            added, key=lambda item: canonical_json_bytes(item))
    if removed:
        canonical["removed"] = sorted(removed)
    if transforms:
        canonical["transforms"] = sorted(
            transforms, key=lambda item: item["handle"])
    if len(canonical_json_bytes(canonical)) > MAX_PLAN_BYTES:
        raise ValueError("canonical mutation data exceeds the byte bound")
    return canonical


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


def _fmt(value: float) -> str:
    value = 0.0 if abs(value) < 5e-13 else value
    return format(value, ".12g")


def emit_plan(canonical: Dict[str, Any], *, base_sha256: str) -> bytes:
    """Emit the exact data-only Activity input for one canonical mutation set."""
    if not re.fullmatch(r"[0-9a-f]{64}", base_sha256):
        raise ValueError("base_sha256 must be lowercase hex")
    if canonical.get("transforms"):
        raise ValueError("live mutation plans do not yet support transforms")
    lines = ["LEAF_MUTATION_PLAN|1", f"BASE_SHA256|{base_sha256}"]
    for handle in canonical.get("removed", []):
        lines.append(f"REMOVE|{handle}")
    for entity in canonical.get("added", []):
        lowered = world_to_ocs(entity["pts"])
        layer = entity["layer"]
        normal = ",".join(_fmt(value) for value in lowered["normal"])
        vertices = ";".join(
            ",".join(_fmt(value) for value in point)
            for point in lowered["points"]
        )
        lines.append(
            f"ADD|{layer}|{normal}|{_fmt(lowered['elevation'])}|{vertices}")
    plan = ("\n".join(lines) + "\n").encode("ascii")
    if len(plan) > MAX_PLAN_BYTES:
        raise ValueError("mutation plan exceeds the byte bound")
    return plan


def plan_sha256(plan: bytes) -> str:
    return hashlib.sha256(plan).hexdigest()
