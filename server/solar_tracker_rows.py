"""Create explicit manual tracker rows once on a drawing's Ground physical head.

Physical publication uses the existing parent CAS. Graph resolution and physical
publication are separate operations; this domain provides no HTTP or checkout policy.
"""
from copy import deepcopy
import hashlib
import json
import math
import re

import write_loop  # first: makes the drawing store available to the physical head module
import solar_frames_piles as fp
import solar_ground_terrain_adapter as ta
import solar_physical_head as ph
import solar_physical_state as ps
from solar_design_graph import GraphValidationError
from solar_graph_context import resolve_graph_context

OPERATION = "manual-create"
RESULT_SCHEMA = "leaf.solar-tracker-rows.v1"
MAX_ROWS = 256
MAX_REQUEST_BYTES = 262144
MAX_ROW_SLOTS = 10000
MAX_TOTAL_SLOTS = 100000
INDEPENDENT_STATE_KEYS = frozenset({
    "grid", "frames", "piles", "settings",
    "restriction_outlines", "road_lines", "setback_rings",
    "vegetation_outlines", "grading_settings", "export_settings",
    "pile_store_text", "next_handle", "mesh_faces",
    "terrain_face_colors", "collision_layer",
})
CODES = frozenset({
    "TRACKER_ROWS_OPERATION_UNSUPPORTED", "TRACKER_ROWS_REQUEST_INVALID",
    "TRACKER_ROWS_ROWS_INVALID", "TRACKER_ROWS_ROW_INVALID", "TRACKER_ROWS_POWER_INVALID",
    "TRACKER_ROWS_HEAD_INVALID", "TRACKER_ROWS_UNITS_UNSUPPORTED", "TRACKER_ROWS_UNITS_MISMATCH",
    "TRACKER_ROWS_FRAME_UNSUPPORTED", "TRACKER_ROWS_SLOTS_INVALID", "TRACKER_ROWS_AXIS_INVALID",
    "TRACKER_ROWS_WIDTH_INVALID", "TRACKER_ROWS_LIMIT_EXCEEDED", "TRACKER_ROWS_PROJECT_ID_INVALID",
    "TRACKER_ROWS_PROJECT_MISMATCH", "TRACKER_ROWS_DRAWING_NOT_FOUND", "TRACKER_ROWS_GRAPH_REQUIRED",
    "TRACKER_ROWS_GROUND_REQUIRED", "TRACKER_ROWS_GRAPH_CONVERTED", "TRACKER_ROWS_DEPENDENT_STATE",
    "TRACKER_ROWS_ALREADY_EXISTS", "TRACKER_ROWS_STALE_HEAD", "TRACKER_ROWS_STATE_INVALID",
    "TRACKER_ROWS_WRITES_DRAINED", "TRACKER_ROWS_STORE_UNAVAILABLE", "TRACKER_ROWS_STORE_UNSAFE",
    "TRACKER_ROWS_LOG_FULL", "TRACKER_ROWS_CHECKOUT_REQUIRED", "TRACKER_ROWS_CHECKOUT_UNAVAILABLE",
    "TRACKER_ROWS_CONTENT_TYPE_UNSUPPORTED",
})
_HEX64 = re.compile(r"[0-9a-f]{64}")
_ROW_KEYS = frozenset({"axis_start", "axis_end", "cross_axis_width_du", "slots"})


class TrackerRowsError(ps.PhysicalStateError):
    """A named, payload-free refusal in the tracker row vocabulary."""


def _number(value, bound):
    try:
        return type(value) in (int, float) and math.isfinite(value) and abs(value) <= bound
    except OverflowError:
        return False


def validate_manual_create(*, rows, module_power_watts, expected_head, drawing_units,
                           request_bytes=None) -> dict:
    """Pure normalization and canonical request binding, without calculated row fields."""
    if expected_head is not None and (type(expected_head) is not str
                                     or not _HEX64.fullmatch(expected_head)):
        raise TrackerRowsError("TRACKER_ROWS_HEAD_INVALID")
    if type(drawing_units) is not str or drawing_units not in ("m", "ft"):
        raise TrackerRowsError("TRACKER_ROWS_UNITS_UNSUPPORTED")
    if request_bytes is not None:
        if type(request_bytes) is not int or request_bytes < 0:
            raise TrackerRowsError("TRACKER_ROWS_REQUEST_INVALID")
        if request_bytes > MAX_REQUEST_BYTES:
            raise TrackerRowsError("TRACKER_ROWS_LIMIT_EXCEEDED")
    if type(rows) is not list or not rows:
        raise TrackerRowsError("TRACKER_ROWS_ROWS_INVALID")
    if len(rows) > MAX_ROWS:
        raise TrackerRowsError("TRACKER_ROWS_LIMIT_EXCEEDED")
    if not _number(module_power_watts, 1e15) or module_power_watts <= 0:
        raise TrackerRowsError("TRACKER_ROWS_POWER_INVALID")
    normalized = []
    total = 0
    mpu = ps.UNITS[drawing_units]
    for row in rows:
        if type(row) is not dict or set(row) != _ROW_KEYS:
            raise TrackerRowsError("TRACKER_ROWS_ROW_INVALID")
        points = []
        for key in ("axis_start", "axis_end"):
            point = row[key]
            if (type(point) is not list or len(point) != 2
                    or not all(_number(v, 1e9) for v in point)):
                raise TrackerRowsError("TRACKER_ROWS_ROW_INVALID")
            # + 0.0 maps -0.0 to 0.0, so the digest binds the value and not its sign spelling.
            points.append([float(v) + 0.0 for v in point])
        slots = row["slots"]
        if type(slots) is not int or not 1 <= slots <= MAX_ROW_SLOTS:
            raise TrackerRowsError("TRACKER_ROWS_SLOTS_INVALID")
        total += slots
        if total > MAX_TOTAL_SLOTS:
            raise TrackerRowsError("TRACKER_ROWS_LIMIT_EXCEEDED")
        # The conversion's own arithmetic (solar_ground_buildout.read_tracker_row and
        # solar_ground_dsteps._tracker_panel_layout): math.dist rounds differently at both
        # axis limits, so an accepted row could then be refused by conversion.
        dx = points[1][0] - points[0][0]
        dy = points[1][1] - points[0][1]
        length = math.sqrt(dx * dx + dy * dy)
        if length <= 1e-9 or length * mpu > 100000:
            raise TrackerRowsError("TRACKER_ROWS_AXIS_INVALID")
        width = row["cross_axis_width_du"]
        if not _number(width, 1e15) or width <= 1e-9 or width * mpu > 1000:
            raise TrackerRowsError("TRACKER_ROWS_WIDTH_INVALID")
        normalized.append({"axis_start": points[0], "axis_end": points[1],
                           "cross_axis_width_du": float(width), "slots": slots})
    power = float(module_power_watts)
    envelope = {"operation": OPERATION, "expected_head": expected_head,
                "drawing_units": drawing_units, "rows": normalized, "module_power_watts": power}
    raw = json.dumps(envelope, sort_keys=True, separators=(",", ":"),
                     allow_nan=False).encode("utf-8")
    if len(raw) > MAX_REQUEST_BYTES:
        raise TrackerRowsError("TRACKER_ROWS_LIMIT_EXCEEDED")
    return {**envelope, "rows": [{**row, "row_index": i, "source_command": "manual"}
                                 for i, row in enumerate(normalized)],
            "request_sha256": hashlib.sha256(raw).hexdigest(),
            "summary": {"rows": len(normalized), "slots": total, "module_power_watts": power}}


def _translated(code):
    if code in CODES:
        return code
    if code == "TERRAIN_FRAME_UNSUPPORTED" or code == "PHYSICAL_STATE_FRAME_INVALID":
        return "TRACKER_ROWS_FRAME_UNSUPPORTED"
    if code in ("PHYSICAL_HEAD_CONFLICT", "PHYSICAL_STATE_CONFLICT", "PHYSICAL_STATE_PARENT_NOT_FOUND"):
        return "TRACKER_ROWS_STALE_HEAD"
    if code == "PHYSICAL_HEAD_LOG_FULL":
        return "TRACKER_ROWS_LOG_FULL"
    if code == "PHYSICAL_HEAD_STORE_UNSAFE":
        return "TRACKER_ROWS_STORE_UNSAFE"
    for suffix in ("PROJECT_ID_INVALID", "PROJECT_MISMATCH", "DRAWING_NOT_FOUND", "GRAPH_REQUIRED",
                   "WRITES_DRAINED", "STORE_UNAVAILABLE"):
        if code.endswith("_" + suffix) or code == suffix:
            return "TRACKER_ROWS_" + suffix
    if code == "GRAPH_CONTEXT_UNAVAILABLE" or code == "PHYSICAL_HEAD_ID_INVALID":
        return "TRACKER_ROWS_DRAWING_NOT_FOUND"
    if code in ("GRAPH_NOT_EMBEDDED", "GRAPH_DIGEST_MISMATCH"):
        return "TRACKER_ROWS_GRAPH_REQUIRED"
    if code == "PHYSICAL_STATE_UNITS_UNSUPPORTED":
        return "TRACKER_ROWS_UNITS_UNSUPPORTED"
    return "TRACKER_ROWS_STATE_INVALID"


def _result(drawing_id, project_id, validated, document, head, created):
    return {"schema": RESULT_SCHEMA, "operation": OPERATION,
            "outcome": "published" if created else "retry", "created": created,
            "drawing_id": drawing_id, "project_id": project_id,
            "expected_head": validated["expected_head"], "head": head,
            "frame": ta.document_frame(document), "summary": validated["summary"],
            "terrain_standing": fp.terrain_standing(document["state"],
                                                   document["units"]["meters_per_unit"])}


def publish_manual_create(backend, tenant_id, drawing_id, *, rows, module_power_watts,
                          expected_head, project_id=None, request_bytes=None) -> dict:
    """Preflight a first creation or return the current head's exact normalized retry."""
    try:
        if project_id is not None and (type(project_id) is not str or not 1 <= len(project_id) <= 100):
            raise TrackerRowsError("TRACKER_ROWS_PROJECT_ID_INVALID")
        if write_loop.drawing_mutations_refusal() is not None:
            raise TrackerRowsError("TRACKER_ROWS_WRITES_DRAINED")
        context = resolve_graph_context(backend, tenant_id, drawing_id, "head", project_id=project_id)
        graph = context["graph"]
        project = context["project_id"]
        graph_units = graph["project"]["units"]
        validated = validate_manual_create(rows=rows, module_power_watts=module_power_watts,
                                           expected_head=expected_head,
                                           drawing_units=graph_units["drawing_units"],
                                           request_bytes=request_bytes)
        head, previous = ph.load_physical_head(backend, tenant_id, drawing_id, project_id=project)
        if (head is not None and head["parent"] == expected_head
                and previous["capability"] == OPERATION
                and previous["source"]["sha256"] == validated["request_sha256"]
                and previous["state"].get("tracker_rows") == validated["rows"]
                and previous["state"].get("settings", {}).get("TrackerModulePmaxW")
                == validated["module_power_watts"]):
            return _result(drawing_id, project, validated, previous, head, False)
        if expected_head != (None if head is None else head["state"]["artifact_id"]):
            raise TrackerRowsError("TRACKER_ROWS_STALE_HEAD")
        if graph["project"]["installation_design"] != "Ground":
            raise TrackerRowsError("TRACKER_ROWS_GROUND_REQUIRED")
        if "physical_state" in graph.get("extra", {}):
            raise TrackerRowsError("TRACKER_ROWS_GRAPH_CONVERTED")
        units = validated["drawing_units"]
        if graph_units["meters_per_unit"] != ps.UNITS[units]:
            raise TrackerRowsError("TRACKER_ROWS_UNITS_MISMATCH")
        if graph_units["wcs_to_ucs"] != list(ta.IDENTITY_TRANSFORM):
            raise TrackerRowsError("TRACKER_ROWS_FRAME_UNSUPPORTED")
        if previous is None:
            state = {}
            frame = deepcopy(ps.DEFAULT_FRAME)
        else:
            if previous["units"]["drawing_units"] != units:
                raise TrackerRowsError("TRACKER_ROWS_UNITS_MISMATCH")
            ta.document_frame(previous)
            state = ps.decode_state(ps.encode_state(previous["state"]))
            frame = deepcopy(previous["frame"])
            if "tracker_rows" in state:
                raise TrackerRowsError("TRACKER_ROWS_ALREADY_EXISTS")
            for key, value in state.items():
                if key not in INDEPENDENT_STATE_KEYS and value not in (None, [], {}, "", b""):
                    raise TrackerRowsError("TRACKER_ROWS_DEPENDENT_STATE")
            fp.terrain_standing(state, ps.UNITS[units])
        state["tracker_rows"] = validated["rows"]
        state["settings"] = dict(state.get("settings", {}),
                                 TrackerModulePmaxW=validated["module_power_watts"])
        document = ps.physical_document(state, drawing_units=units, frame=frame,
                                        source_sha256=validated["request_sha256"],
                                        capability=OPERATION, parent=expected_head)
        # Result helpers validate the preserved state before any publication.
        result = _result(drawing_id, project, validated, document, None, True)
        published = ph.publish_physical_state(backend, tenant_id, drawing_id, document, project_id=project)
        result.update(head=published["head"], created=published["created"],
                      outcome="published" if published["created"] else "retry")
        return result
    except GraphValidationError as exc:
        code = _translated(exc.code)
        raise TrackerRowsError("TRACKER_ROWS_GRAPH_REQUIRED" if code == "TRACKER_ROWS_STATE_INVALID"
                               else code) from None
    except ps.PhysicalStateError as exc:
        raise TrackerRowsError(_translated(exc.code)) from None
    except (OSError, RuntimeError):
        raise TrackerRowsError("TRACKER_ROWS_STORE_UNAVAILABLE") from None
    except (TypeError, ValueError, LookupError, AttributeError, ArithmeticError, RecursionError):
        raise TrackerRowsError("TRACKER_ROWS_STATE_INVALID") from None
