"""Serialize and reopen the Ground physical design state through the Solar artifact store.

The state is the neutral dict the Ground kernels thread from step to step (frames, piles,
terrain grid, settings, tracker rows, grading, build-out, arrays, shade and dialog records).
It does not fit the design graph: the committed terrain site's state after t7 (6.1 MB, 359,448
JSON nodes) placed in the graph's `extra` is refused GRAPH_LIMIT_EXCEEDED by validate_graph, so
it lives in revision-bound JSON artifacts.

A document binds the state to its units, frame (coordinate system, transform, elevation datum,
CRS), source intake digest, the capability that wrote it and its parent state. Encoding is
lossless: tuples and bytes are tagged, every other value is plain JSON, and a reopened state
equals the stored one exactly, Python types included. Every value is bounded; malformed
input, stored bytes or bindings fail closed with a named code and no payload in the message.
No clock, network or graph write lives here.
"""
import base64
import hashlib
import json
import math
import re

import solar_artifacts
import write_loop
from solar_design_graph import GraphValidationError
from solar_graph_context import resolve_graph_context

DOCUMENT_SCHEMA = "leaf.solar-physical-state.v1"
RESULT_SCHEMA = "leaf.solar-physical-state-result.v1"
TOOL = "solar-physical-state"
MEDIA_TYPE = "application/json"
FILENAME = "physical-state.json"
SOURCE_KIND = "ground-intake"
MAX_DOCUMENT_BYTES = solar_artifacts.MAX_ARTIFACT_BYTES
MAX_NODES = 1_000_000
MAX_DEPTH = 32
MAX_STRING_CHARS = 8_388_608
MAX_BYTES_VALUE = 4_194_304
MAX_ABS_FLOAT = 1e15
MAX_ABS_INT = 9_007_199_254_740_991
MAX_COUNTER = 1_000_000_000
TUPLE_TAG = "$tuple"
BYTES_TAG = "$bytes"
UNITS = {"m": 1.0, "ft": 0.3048}
DEFAULT_FRAME = {"coordinate_system": "world",
                 "transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
                 "elevation_datum": "unrecorded", "crs": "none"}
DOCUMENT_ORDER = ("schema", "units", "frame", "source", "capability", "parent", "state")
DOCUMENT_KEYS = frozenset(DOCUMENT_ORDER)
UNITS_ORDER = ("drawing_units", "meters_per_unit")
FRAME_ORDER = ("coordinate_system", "transform", "elevation_datum", "crs")
SOURCE_ORDER = ("kind", "sha256")

_LIST = (list,)
_DICT = (dict,)
_DICT_OR_NONE = (dict, type(None))
# The closed state key table: every key the Ground kernels' state carries, and the Python
# types its value may take. A key absent from the state stays absent after a round trip.
STATE_KEYS = {
    "array_outlines": _LIST, "arrays": _LIST, "collision_layer": _LIST, "export_preview": _LIST,
    "fence_mesh_faces": _LIST, "frames": _LIST, "grade_pads": _LIST, "piles": _LIST,
    "restriction_outlines": _LIST, "road_labels": _LIST, "road_lines": _LIST, "setback_rings": _LIST,
    "shade_heatmap": _LIST, "shading": _LIST, "slope_markers": _LIST, "tracker_rows": _LIST,
    "trenches": _LIST, "tubes": _LIST, "vegetation_outlines": _LIST,
    "grading_settings": _DICT, "settings": _DICT, "shade_files": _DICT, "status_records": _DICT,
    "grid": _DICT_OR_NONE, "dsm": _DICT_OR_NONE, "last_scene": _DICT_OR_NONE,
    "export_settings": _DICT_OR_NONE, "shading_last": _DICT_OR_NONE,
    "terrain_face_colors": (list, type(None)),
    "area_record": (str, type(None)), "pile_store_text": (str, type(None)),
    "mesh_faces": (int,), "next_handle": (int,),
    "last_bom": (bytes, type(None)),
}
COUNTER_KEYS = frozenset({"mesh_faces", "next_handle"})
CODES = frozenset({
    "PHYSICAL_STATE_INVALID", "PHYSICAL_STATE_KEY_UNKNOWN", "PHYSICAL_STATE_TYPE_MISMATCH",
    "PHYSICAL_STATE_UNSUPPORTED_VALUE", "PHYSICAL_STATE_RESERVED_KEY", "PHYSICAL_STATE_NONFINITE",
    "PHYSICAL_STATE_LIMIT_EXCEEDED", "PHYSICAL_STATE_TAG_INVALID", "PHYSICAL_STATE_NOT_CANONICAL",
    "PHYSICAL_STATE_UNITS_UNSUPPORTED", "PHYSICAL_STATE_FRAME_INVALID", "PHYSICAL_STATE_SOURCE_INVALID",
    "PHYSICAL_STATE_CAPABILITY_INVALID", "PHYSICAL_STATE_PARENT_INVALID", "PHYSICAL_STATE_PARENT_NOT_FOUND",
    "PHYSICAL_STATE_PROJECT_ID_INVALID", "PHYSICAL_STATE_PROJECT_MISMATCH", "PHYSICAL_STATE_DRAWING_NOT_FOUND",
    "PHYSICAL_STATE_GRAPH_REQUIRED", "PHYSICAL_STATE_WRITES_DRAINED", "PHYSICAL_STATE_STORE_UNAVAILABLE",
    "PHYSICAL_STATE_CONFLICT", "PHYSICAL_STATE_NOT_FOUND", "PHYSICAL_STATE_ID_INVALID",
    "PHYSICAL_STATE_CORRUPT", "PHYSICAL_STATE_KIND_MISMATCH",
})
_HEX64 = re.compile(r"[0-9a-f]{64}")
_CAPABILITY = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_EPSG = re.compile(r"EPSG:[1-9][0-9]{0,5}")
_ARTIFACT_CODES = {
    "ARTIFACT_WRITES_DRAINED": "PHYSICAL_STATE_WRITES_DRAINED",
    "ARTIFACT_STORE_UNAVAILABLE": "PHYSICAL_STATE_STORE_UNAVAILABLE",
    "ARTIFACT_CONFLICT": "PHYSICAL_STATE_CONFLICT",
    "ARTIFACT_NOT_FOUND": "PHYSICAL_STATE_NOT_FOUND",
    "ARTIFACT_ID_INVALID": "PHYSICAL_STATE_ID_INVALID",
    "ARTIFACT_CORRUPT": "PHYSICAL_STATE_CORRUPT",
    "ARTIFACT_VERIFY_MISMATCH": "PHYSICAL_STATE_CORRUPT",
    "ARTIFACT_TOO_LARGE": "PHYSICAL_STATE_LIMIT_EXCEEDED",
}


class PhysicalStateError(ValueError):
    """A named, payload-free refusal."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical_sha256(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def _compact(value):
    """The one byte form: compact, ASCII-escaped, insertion order kept (never key-sorted, so a
    reopened state iterates exactly as the kernels built it)."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def _refuse_constant(value):
    raise ValueError("nonfinite JSON constant")


class _Budget:
    __slots__ = ("nodes",)

    def __init__(self):
        self.nodes = 0

    def take(self, depth):
        self.nodes += 1
        if self.nodes > MAX_NODES or depth > MAX_DEPTH:
            raise PhysicalStateError("PHYSICAL_STATE_LIMIT_EXCEEDED")


def _scalar(value):
    """A JSON scalar kept exactly as given; fails closed on anything unbounded."""
    kind = type(value)
    if value is None or kind is bool:
        return value
    if kind is int:
        if abs(value) > MAX_ABS_INT:
            raise PhysicalStateError("PHYSICAL_STATE_LIMIT_EXCEEDED")
        return value
    if kind is float:
        if not math.isfinite(value):
            raise PhysicalStateError("PHYSICAL_STATE_NONFINITE")
        if abs(value) > MAX_ABS_FLOAT:
            raise PhysicalStateError("PHYSICAL_STATE_LIMIT_EXCEEDED")
        return value
    if kind is str:
        if len(value) > MAX_STRING_CHARS:
            raise PhysicalStateError("PHYSICAL_STATE_LIMIT_EXCEEDED")
        return value
    raise PhysicalStateError("PHYSICAL_STATE_UNSUPPORTED_VALUE")


def _encode(value, depth, budget):
    budget.take(depth)
    kind = type(value)
    if kind is dict:
        out = {}
        for key, item in value.items():
            if type(key) is not str:
                raise PhysicalStateError("PHYSICAL_STATE_UNSUPPORTED_VALUE")
            if key.startswith("$"):
                raise PhysicalStateError("PHYSICAL_STATE_RESERVED_KEY")
            if len(key) > MAX_STRING_CHARS:
                raise PhysicalStateError("PHYSICAL_STATE_LIMIT_EXCEEDED")
            out[key] = _encode(item, depth + 1, budget)
        return out
    if kind is list:
        return [_encode(item, depth + 1, budget) for item in value]
    if kind is tuple:
        return {TUPLE_TAG: [_encode(item, depth + 2, budget) for item in value]}
    if kind is bytes:
        if len(value) > MAX_BYTES_VALUE:
            raise PhysicalStateError("PHYSICAL_STATE_LIMIT_EXCEEDED")
        return {BYTES_TAG: base64.b64encode(value).decode("ascii")}
    return _scalar(value)


def _decode(value, depth, budget):
    budget.take(depth)
    kind = type(value)
    if kind is dict:
        if any(type(key) is not str for key in value):
            raise PhysicalStateError("PHYSICAL_STATE_UNSUPPORTED_VALUE")
        if any(key.startswith("$") for key in value):
            if len(value) != 1:
                raise PhysicalStateError("PHYSICAL_STATE_TAG_INVALID")
            (tag, item), = value.items()
            if tag == TUPLE_TAG and type(item) is list:
                return tuple(_decode(child, depth + 2, budget) for child in item)
            if tag == BYTES_TAG and type(item) is str:
                if len(item) > (MAX_BYTES_VALUE + 2) // 3 * 4:
                    raise PhysicalStateError("PHYSICAL_STATE_LIMIT_EXCEEDED")
                try:
                    raw = base64.b64decode(item.encode("ascii"), validate=True)
                except (ValueError, UnicodeError):
                    raise PhysicalStateError("PHYSICAL_STATE_TAG_INVALID") from None
                if len(raw) > MAX_BYTES_VALUE:
                    raise PhysicalStateError("PHYSICAL_STATE_LIMIT_EXCEEDED")
                if base64.b64encode(raw).decode("ascii") != item:
                    raise PhysicalStateError("PHYSICAL_STATE_TAG_INVALID")
                return raw
            raise PhysicalStateError("PHYSICAL_STATE_TAG_INVALID")
        out = {}
        for key, item in value.items():
            if len(key) > MAX_STRING_CHARS:
                raise PhysicalStateError("PHYSICAL_STATE_LIMIT_EXCEEDED")
            out[key] = _decode(item, depth + 1, budget)
        return out
    if kind is list:
        return [_decode(item, depth + 1, budget) for item in value]
    return _scalar(value)


def _check_state_table(state):
    if type(state) is not dict:
        raise PhysicalStateError("PHYSICAL_STATE_INVALID")
    for key, value in state.items():
        if key not in STATE_KEYS:
            raise PhysicalStateError("PHYSICAL_STATE_KEY_UNKNOWN")
        if type(value) not in STATE_KEYS[key]:
            raise PhysicalStateError("PHYSICAL_STATE_TYPE_MISMATCH")
        if key in COUNTER_KEYS and not 0 <= value <= MAX_COUNTER:
            raise PhysicalStateError("PHYSICAL_STATE_LIMIT_EXCEEDED")


def encode_state(state):
    """The state as tagged plain JSON (tuples and bytes tagged); the input is never mutated."""
    _check_state_table(state)
    return _encode(state, 0, _Budget())


def decode_state(encoded):
    """The inverse of encode_state: equal to the encoded state, Python types included."""
    if type(encoded) is not dict:
        raise PhysicalStateError("PHYSICAL_STATE_INVALID")
    state = _decode(encoded, 0, _Budget())
    _check_state_table(state)
    return state


def _number(value):
    if type(value) is int:
        return abs(value) <= MAX_ABS_FLOAT
    return (type(value) is float and math.isfinite(value)
            and abs(value) <= MAX_ABS_FLOAT)


def _check_envelope(document):
    if type(document) is not dict or set(document) != DOCUMENT_KEYS:
        raise PhysicalStateError("PHYSICAL_STATE_INVALID")
    if document["schema"] != DOCUMENT_SCHEMA:
        raise PhysicalStateError("PHYSICAL_STATE_INVALID")
    units = document["units"]
    if (type(units) is not dict or set(units) != set(UNITS_ORDER)
            or type(units["drawing_units"]) is not str
            or units["drawing_units"] not in UNITS
            or type(units["meters_per_unit"]) is not float
            or units["meters_per_unit"] != UNITS[units["drawing_units"]]):
        raise PhysicalStateError("PHYSICAL_STATE_UNITS_UNSUPPORTED")
    frame = document["frame"]
    if (type(frame) is not dict or set(frame) != set(FRAME_ORDER)
            or frame["coordinate_system"] != "world"
            or type(frame["transform"]) is not list or len(frame["transform"]) != 16
            or not all(_number(v) for v in frame["transform"])
            or not (frame["elevation_datum"] == "unrecorded"
                    or (type(frame["elevation_datum"]) is str and _EPSG.fullmatch(frame["elevation_datum"])))
            or not (frame["crs"] == "none"
                    or (type(frame["crs"]) is str and _EPSG.fullmatch(frame["crs"])))):
        raise PhysicalStateError("PHYSICAL_STATE_FRAME_INVALID")
    source = document["source"]
    if (type(source) is not dict or set(source) != set(SOURCE_ORDER) or source["kind"] != SOURCE_KIND
            or type(source["sha256"]) is not str or not _HEX64.fullmatch(source["sha256"])):
        raise PhysicalStateError("PHYSICAL_STATE_SOURCE_INVALID")
    capability = document["capability"]
    if type(capability) is not str or not _CAPABILITY.fullmatch(capability):
        raise PhysicalStateError("PHYSICAL_STATE_CAPABILITY_INVALID")
    parent = document["parent"]
    if parent is not None and (type(parent) is not str or not _HEX64.fullmatch(parent)):
        raise PhysicalStateError("PHYSICAL_STATE_PARENT_INVALID")


def physical_document(state, *, drawing_units, source_sha256, capability, parent=None, frame=None):
    """A validated document holding a deep copy of `state` (the copy is the reopened form)."""
    document = {"schema": DOCUMENT_SCHEMA,
                "units": {"drawing_units": drawing_units,
                          "meters_per_unit": UNITS.get(drawing_units) if type(drawing_units) is str else None},
                "frame": json.loads(json.dumps(DEFAULT_FRAME)) if frame is None else frame,
                "source": {"kind": SOURCE_KIND, "sha256": source_sha256},
                "capability": capability, "parent": parent, "state": state}
    _check_envelope(document)
    document["state"] = decode_state(encode_state(state))
    return document


def encode_document(document):
    """The document's compact bytes (envelope keys in DOCUMENT_ORDER, state dicts in their own
    order); at most MAX_DOCUMENT_BYTES."""
    _check_envelope(document)
    envelope = {key: document[key] for key in DOCUMENT_ORDER}
    envelope["units"] = {key: document["units"][key] for key in UNITS_ORDER}
    envelope["frame"] = {key: document["frame"][key] for key in FRAME_ORDER}
    envelope["source"] = {key: document["source"][key] for key in SOURCE_ORDER}
    envelope["state"] = encode_state(document["state"])
    try:
        data = _compact(envelope)
    except (ValueError, TypeError, RecursionError):
        raise PhysicalStateError("PHYSICAL_STATE_INVALID") from None
    if len(data) > MAX_DOCUMENT_BYTES:
        raise PhysicalStateError("PHYSICAL_STATE_LIMIT_EXCEEDED")
    return data


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("nonfinite JSON number")
    return value


def decode_document(data):
    """The document the bytes hold; refuses any byte string encode_document would not write."""
    if type(data) is not bytes or not data:
        raise PhysicalStateError("PHYSICAL_STATE_INVALID")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise PhysicalStateError("PHYSICAL_STATE_LIMIT_EXCEEDED")
    try:
        parsed = json.loads(data.decode("utf-8", errors="strict"), parse_constant=_refuse_constant,
                            parse_float=_finite_float)
    except (ValueError, RecursionError):
        raise PhysicalStateError("PHYSICAL_STATE_INVALID") from None
    if type(parsed) is not dict or set(parsed) != DOCUMENT_KEYS:
        raise PhysicalStateError("PHYSICAL_STATE_INVALID")
    document = dict(parsed, state=decode_state(parsed["state"]))
    if encode_document(document) != data:
        raise PhysicalStateError("PHYSICAL_STATE_NOT_CANONICAL")
    return document


def _artifact_refusal(code):
    return _ARTIFACT_CODES.get(code, "PHYSICAL_STATE_INVALID")


def load_physical_state(backend, tenant_id, drawing_id, artifact_id, *, project_id):
    """(meta, document) for a stored physical state of this tenant, drawing and project."""
    if type(project_id) is not str or not 1 <= len(project_id) <= 100:
        raise PhysicalStateError("PHYSICAL_STATE_PROJECT_ID_INVALID")
    try:
        meta, content = solar_artifacts.read_artifact(backend, tenant_id, drawing_id, artifact_id)
    except GraphValidationError as exc:
        raise PhysicalStateError(_artifact_refusal(exc.code)) from None
    if meta["tool"] != TOOL or meta["media_type"] != MEDIA_TYPE or meta["filename"] != FILENAME:
        raise PhysicalStateError("PHYSICAL_STATE_KIND_MISMATCH")
    if meta["project_id"] != project_id:
        raise PhysicalStateError("PHYSICAL_STATE_PROJECT_MISMATCH")
    if meta["request_sha256"] != meta["content_sha256"]:
        raise PhysicalStateError("PHYSICAL_STATE_CORRUPT")
    try:
        document = decode_document(content)
    except PhysicalStateError:
        raise PhysicalStateError("PHYSICAL_STATE_CORRUPT") from None
    return meta, document


def store_physical_state(backend, tenant_id, drawing_id, document, *, project_id=None):
    """Store the document bound to the drawing's head revision; the same bytes at the same
    head return the same reference. A parent must be a stored state of the same project."""
    if write_loop.drawing_mutations_refusal() is not None:
        raise PhysicalStateError("PHYSICAL_STATE_WRITES_DRAINED")
    if project_id is not None and (type(project_id) is not str or not 1 <= len(project_id) <= 100):
        raise PhysicalStateError("PHYSICAL_STATE_PROJECT_ID_INVALID")
    data = encode_document(document)
    try:
        context = resolve_graph_context(backend, tenant_id, drawing_id, "head", project_id=project_id)
    except GraphValidationError as exc:
        code = {"PROJECT_MISMATCH": "PHYSICAL_STATE_PROJECT_MISMATCH",
                "GRAPH_CONTEXT_UNAVAILABLE": "PHYSICAL_STATE_DRAWING_NOT_FOUND"}.get(
                    exc.code, "PHYSICAL_STATE_GRAPH_REQUIRED")
        raise PhysicalStateError(code) from None
    except (OSError, RuntimeError):
        raise PhysicalStateError("PHYSICAL_STATE_STORE_UNAVAILABLE") from None
    parent = document["parent"]
    if parent is not None:
        try:
            load_physical_state(backend, tenant_id, drawing_id, parent, project_id=context["project_id"])
        except PhysicalStateError as exc:
            if exc.code in ("PHYSICAL_STATE_STORE_UNAVAILABLE", "PHYSICAL_STATE_NOT_FOUND"):
                raise PhysicalStateError("PHYSICAL_STATE_STORE_UNAVAILABLE"
                                         if exc.code == "PHYSICAL_STATE_STORE_UNAVAILABLE"
                                         else "PHYSICAL_STATE_PARENT_NOT_FOUND") from None
            raise PhysicalStateError("PHYSICAL_STATE_PARENT_INVALID") from None
    content_sha256 = hashlib.sha256(data).hexdigest()
    sink = solar_artifacts.ArtifactSink(backend, tenant_id, drawing_id, context, TOOL, content_sha256, False)
    try:
        prepared = sink.prepare(solar_artifacts.ArtifactOutput(
            {"capability": document["capability"]}, MEDIA_TYPE, FILENAME, data))
        ref = sink.finish(prepared)
    except GraphValidationError as exc:
        raise PhysicalStateError(_artifact_refusal(exc.code)) from None
    return {"schema": RESULT_SCHEMA, "drawing_id": drawing_id, "project_id": context["project_id"],
            "source_version": context["resolved_version"], "graph_sha256": context["graph_sha256"],
            "capability": document["capability"], "parent": parent, "state": ref}
