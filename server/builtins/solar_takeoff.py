"""Version-bound preview quantities from the graph and stored Ground state."""
import copy
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
import hashlib
import json
import math
import re
import uuid

import solar_ground_buildout as bo
import solar_ground_layout as ground_layout
import solar_physical_state as ps
from solar_ground_conversion import tracker_entities
from solar_artifacts import ArtifactOutput
from solar_design_graph import GraphValidationError

READS_PHYSICAL_HEAD = True
READS_SOURCE_BINDING = True
TOOL = "solar-takeoff"
SCHEMA = "leaf.solar-takeoff.v1"
PROJECT_REF_SCHEMA = "leaf.solar-project-artifact-ref.v1"
MAX_LINES = 128
MAX_INVERTER_GROUPS = 64
MAX_GAUGE_GROUPS = 32
MAX_DESCRIPTION = 200
MAX_LABEL = 120
MAX_LINE_ID = 96
MAX_QUANTITY = Decimal("1e12")
MIN_PILE_SPACING_M = Decimal("0.5")
MAX_PILE_SPACING_M = Decimal("100")
PILE_SPACING_STEP = Decimal("0.1")
LINE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,31}){0,3}$")
KERNEL_ERRORS = (ValueError, TypeError, KeyError, IndexError, AttributeError,
                 OverflowError, ZeroDivisionError, RecursionError)
_HEX = re.compile(r"[0-9a-f]{64}")
_STANDALONE_KEYS = {"artifact_id", "media_type", "filename", "byte_length",
                    "content_sha256", "source_version", "schema", "download"}
_PROJECT_KEYS = {"organization_id", "project_id", "drawing_id", "source_version_id",
                 "source_intake_sha256", "graph_sha256", "artifact_id", "media_type",
                 "filename", "byte_length", "content_sha256", "schema", "download"}
_GROUND = (
    ("modules", "Module", "ea"), ("tube_length", "Torque Tube", "m"),
    ("tube_sections", "Torque Tube", "ea"), ("piles", "Pile", "ea"),
    ("drives", "Drive Unit", "ea"), ("dc_capacity", "DC Capacity", "kWp"),
)
_PHASE_B = (
    ("dc_homeruns", "ft", "E3a"), ("trench", "ft", "E3a"),
    ("harness", "ea", "E3a"), ("roads", "m", "E3a"), ("fence", "m", "E3a"),
    ("string_wire", "ft", "E2"), ("feeder_conductor_spec", "ft", "E2"),
    ("ac_collection", "ft", "E3b"), ("transformers_mv", "ea", "E3b"),
    ("conduit", "ft", "E3b"), ("grounding", "ft", "E3b"),
    ("terminations", "ea", "E3b"), ("drainage", "m", "E3b"), ("labor", "ea", "E4"),
)


def _fail(code):
    raise GraphValidationError(code)


def _hex(value):
    return type(value) is str and _HEX.fullmatch(value) is not None


def _number(value, code="TAKEOFF_INPUT_INVALID"):
    if type(value) not in (int, float):
        _fail(code)
    try:
        value = float(value)
    except (ValueError, OverflowError):
        _fail(code)
    if not math.isfinite(value):
        _fail(code)
    return Decimal(repr(value))


def _spacing(params):
    if type(params) is not dict or set(params) - {"pile_spacing_m"}:
        _fail("INVALID_TAKEOFF_REQUEST")
    value = _number(params.get("pile_spacing_m", bo.DEFAULT_PILE_SPACING_M),
                    "INVALID_TAKEOFF_REQUEST")
    if not MIN_PILE_SPACING_M <= value <= MAX_PILE_SPACING_M or value % PILE_SPACING_STEP:
        _fail("INVALID_TAKEOFF_REQUEST")
    return value


def _binding(source):
    if (type(source) is not dict
            or set(source) != {"drawing_id", "drawing_version", "graph_sha256", "project_id"}
            or type(source["drawing_id"]) is not str
            or type(source["drawing_version"]) is not int or source["drawing_version"] < 1
            or not _hex(source["graph_sha256"])
            or (source["project_id"] is not None and type(source["project_id"]) is not str)):
        _fail("TAKEOFF_SOURCE_REQUIRED")


def _physical(wrapper):
    if wrapper is None:
        return
    try:
        if type(wrapper) is not dict or set(wrapper) != {"head", "document"}:
            raise ValueError()
        head, doc = wrapper["head"], wrapper["document"]
        if type(head) is not dict or type(doc) is not dict:
            raise ValueError()
        ref = head["state"]
        if (type(head["index"]) is not int or not 0 <= head["index"] <= 4095
                or type(ref) is not dict or not _hex(ref["artifact_id"])
                or not _hex(ref["content_sha256"])):
            raise ValueError()
        if "source_version_id" in ref:
            value = ref["source_version_id"]
            if (set(ref) != _PROJECT_KEYS or ref["schema"] != PROJECT_REF_SCHEMA
                    or type(value) is not str or str(uuid.UUID(value)) != value):
                raise ValueError()
        elif (set(ref) != _STANDALONE_KEYS or ref["schema"] != "leaf.solar-artifact-ref.v1"
              or type(ref["source_version"]) is not int or ref["source_version"] < 1):
            raise ValueError()
        if (type(doc["units"]) is not dict
                or type(doc["units"]["meters_per_unit"]) is not float
                or doc["units"]["meters_per_unit"] not in ps.UNITS.values()
                or type(doc["state"]) is not dict):
            raise ValueError()
    except (KeyError, ValueError, TypeError, AttributeError):
        _fail("TAKEOFF_PHYSICAL_INPUT_INVALID")


def _quantity(value, unit):
    value = value if isinstance(value, Decimal) else _number(value)
    if not value.is_finite():
        _fail("TAKEOFF_INPUT_INVALID")
    if abs(value) > MAX_QUANTITY:
        _fail("TAKEOFF_LIMIT_EXCEEDED")
    if unit == "ea":
        if value != value.to_integral_value():
            _fail("TAKEOFF_INPUT_INVALID")
        return str(int(value))
    value = value.quantize(Decimal("0.001"), rounding=ROUND_HALF_EVEN)
    return format(abs(value) if value == 0 else value, ".3f")


def _line(line_id, category, description, unit, quantity, kind, basis, source, physical,
          *, reason=None, status="unavailable", priceable=True):
    provenance = {"kind": kind, "drawing_version": source["drawing_version"], "basis": basis}
    if kind == "physical" and physical is not None:
        head = physical["head"]
        provenance["physical_state"] = {
            "head_index": head["index"], "artifact_id": head["state"]["artifact_id"],
            "content_sha256": head["state"]["content_sha256"],
            "state_source_version": head["state"].get("source_version"),
        }
    computed = reason is None
    return {"line_id": line_id, "category": category, "description": description[:MAX_DESCRIPTION],
            "unit": unit, "quantity": _quantity(quantity, unit) if computed else None,
            "status": "computed" if computed else status, "reason": reason,
            "priceable": computed and priceable, "source": provenance}


def _graph_lines(graph, source, physical):
    def line(key, category, description, unit, quantity, basis):
        return _line(key, category, description, unit, quantity, "graph", basis, source, physical)

    strings = graph.get("strings", [])
    lines = [line("strings.count", "Strings", "Strings", "ea", len(strings), "graph.strings"),
             line("strings.modules", "Modules", "Modules in strings", "ea",
                  sum((_number(s["module_count"]) for s in strings), Decimal(0)),
                  "graph.strings.module_count")]
    groups = {}
    for inv in graph.get("inverters", []):
        key = (inv["is_l2"], inv.get("equipment_type", "unclassified"), inv["model"])
        if (type(key[0]) is not bool or key[1] not in
                {"unclassified", "string_inverter", "combiner_box", "central_inverter"}
                or type(key[2]) is not str):
            _fail("TAKEOFF_INPUT_INVALID")
        groups[key] = groups.get(key, 0) + 1
        if len(groups) > MAX_INVERTER_GROUPS:
            _fail("TAKEOFF_LIMIT_EXCEEDED")
    for (l2, equipment, model), count in sorted(groups.items()):
        suffix = hashlib.sha256(model.encode("utf-8")).hexdigest()[:30]
        key = f"inverters.{'l2' if l2 else 'l1'}.{equipment}.m_{suffix}"
        lines.append(line(key, "Inverters", f"{equipment} {model[:MAX_LABEL]}", "ea", count,
                          "graph.inverters (is_l2, equipment_type, model)"))
    feeders = [r for r in graph.get("routes", []) if r["route_kind"] == "feeder"]
    lines.append(line("feeders.count", "Feeders", "Feeders", "ea", len(feeders), "graph.routes.feeder"))
    gauges = {}
    for route in feeders:
        gauge = route["wire_gauge"]
        if type(gauge) is not str:
            _fail("TAKEOFF_INPUT_INVALID")
        gauges[gauge] = gauges.get(gauge, Decimal(0)) + _number(route["length_ft"])
        if len(gauges) > MAX_GAUGE_GROUPS:
            _fail("TAKEOFF_LIMIT_EXCEEDED")
    for gauge, total in sorted(gauges.items()):
        suffix = "g_" + hashlib.sha256(gauge.encode("utf-8")).hexdigest()[:30] if gauge else "unspecified"
        lines.append(line("feeders.length." + suffix, "Feeders",
                          "Feeder length, gauge " + (gauge[:MAX_LABEL] or "unspecified"),
                          "ft", total, "graph.routes.feeder.length_ft (wire_gauge)"))
    return lines


def _ground_lines(source, physical, spacing, reason):
    mapped = {}
    if reason is None:
        state = physical["document"]["state"]
        frames, rows_in, settings = (state.get("frames", []), state.get("tracker_rows", []),
                                     state.get("settings", {}))
        if type(frames) is not list or type(rows_in) is not list or type(settings) is not dict:
            _fail("TAKEOFF_PHYSICAL_INPUT_INVALID")
        try:
            m = ground_layout.get_active_module(ground_layout.load_settings(settings))
            module = {"cross_axis_m": m.cross_axis_m, "along_axis_m": m.along_axis_m, "pmax_w": m.pmax_w}
            rows = bo.read_tracker_rows(tracker_entities({"frames": frames, "tracker_rows": rows_in}),
                                        physical["document"]["units"]["meters_per_unit"])
            if not rows:
                reason = "TRACKER_ROWS_ABSENT"
            else:
                bom = bo.compute_bom(rows, module, float(spacing))
                mapped = {(item["category"], item["unit"]): item for item in bom["lines"]}
        except bo.BuildoutBoundsError:
            _fail("TAKEOFF_LIMIT_EXCEEDED")
        except KERNEL_ERRORS:
            _fail("TAKEOFF_KERNEL_FAILED")
    lines = []
    for name, category, unit in _GROUND:
        item = mapped.get((category, unit))
        why = reason
        if why is None and item is None:
            if name != "dc_capacity":
                _fail("TAKEOFF_KERNEL_FAILED")
            why = "MODULE_POWER_UNSET"
        lines.append(_line("ground." + name, category, item["description"] if item else category,
                           unit, item["quantity"] if item else None, "physical",
                           "state.frames + state.tracker_rows (Ground BOM)", source, physical, reason=why))
    return lines


def _pad_totals(pads):
    boxes = []
    boundaries = set()
    duplicate = False
    missing = False
    cut, fill = Decimal(0), Decimal(0)
    for pad in pads:
        if type(pad) is not dict or type(pad.get("vertices")) is not list:
            _fail("TAKEOFF_PHYSICAL_INPUT_INVALID")
        vertices = pad["vertices"]
        if not 3 <= len(vertices) <= bo.MAX_POLYLINE_VERTICES:
            _fail("TAKEOFF_PHYSICAL_INPUT_INVALID")
        coords = []
        full = []
        for point in vertices:
            if type(point) is not list or len(point) < 2:
                _fail("TAKEOFF_PHYSICAL_INPUT_INVALID")
            for value in point:
                _number(value, "TAKEOFF_PHYSICAL_INPUT_INVALID")
            coords.append(point[:2])
            full.append(tuple(point))
        boundary = tuple(full)
        duplicate = duplicate or boundary in boundaries
        boundaries.add(boundary)
        boxes.append((min(p[0] for p in coords), max(p[0] for p in coords),
                      min(p[1] for p in coords), max(p[1] for p in coords)))
        if "cut_fill" not in pad:
            _fail("TAKEOFF_PHYSICAL_INPUT_INVALID")
        cf = pad["cut_fill"]
        if cf is None:
            missing = True
        elif type(cf) is not dict or not {"cut_m3", "fill_m3"} <= set(cf):
            _fail("TAKEOFF_PHYSICAL_INPUT_INVALID")
        else:
            cut += _number(cf["cut_m3"], "TAKEOFF_PHYSICAL_INPUT_INVALID")
            fill += _number(cf["fill_m3"], "TAKEOFF_PHYSICAL_INPUT_INVALID")
    if not pads:
        return "GRADE_PADS_ABSENT", cut, fill
    if missing:
        return "GRADE_PAD_WITHOUT_TERRAIN", cut, fill
    if duplicate:
        return "GRADE_PAD_OVERLAP", cut, fill
    active = []
    for box in sorted(boxes):
        active = [other for other in active if other[1] > box[0]]
        for other in active:
            if (box[0] < other[1] and other[0] < box[1]
                    and box[2] < other[3] and other[2] < box[3]):
                return "GRADE_PAD_OVERLAP", cut, fill
        active.append(box)
    return None, cut, fill


def _earthwork_lines(source, physical, reason):
    pads = [] if physical is None else physical["document"]["state"].get("grade_pads", [])
    cut, fill = Decimal(0), Decimal(0)
    if reason is None:
        if type(pads) is not list:
            _fail("TAKEOFF_PHYSICAL_INPUT_INVALID")
        if len(pads) > bo.MAX_PADS:
            _fail("TAKEOFF_LIMIT_EXCEEDED")
        reason, cut, fill = _pad_totals(pads)
    count = len(pads) if type(pads) is list else 0
    basis = f"state.grade_pads ({count} pads)"
    if reason is None:
        cut = Decimal(_quantity(cut, "m3"))
        fill = Decimal(_quantity(fill, "m3"))
    return [_line("earthwork." + name, "Earthwork",
                  f"{name.capitalize()}, {basis}; pads graded independently against base terrain; "
                  "overlapping pads refused, not reconciled.", "m3", value, "physical", basis,
                  source, physical, reason=reason, priceable=name != "net")
            for name, value in (("cut", cut), ("fill", fill), ("net", cut - fill))]


def run(graph, params, physical_head=None, source_binding=None):
    # Enough precision to sum finite binary64 representations without rounding totals.
    with localcontext() as ctx:
        ctx.prec = 700
        spacing = _spacing(params)
        _binding(source_binding)
        _physical(physical_head)
        reason = "PHYSICAL_STATE_ABSENT" if physical_head is None else None
        if physical_head is not None:
            ref = physical_head["head"]["state"]
            if "source_version" in ref and ref["source_version"] > source_binding["drawing_version"]:
                reason = "PHYSICAL_STATE_AHEAD_OF_VERSION"
        lines = _graph_lines(graph, source_binding, physical_head)
        lines.extend(_ground_lines(source_binding, physical_head, spacing, reason))
        lines.extend(_earthwork_lines(source_binding, physical_head, reason))
        lines.extend(_line("phase_b." + name, "Phase B", name.replace("_", " "), unit,
                           None, "none", "phase-b:" + phase, source_binding, physical_head,
                           reason="phase-b:" + phase, status="not-computed", priceable=False)
                     for name, unit, phase in _PHASE_B)
        if len(lines) > MAX_LINES:
            _fail("TAKEOFF_LIMIT_EXCEEDED")
        ids = [line["line_id"] for line in lines]
        if len(set(ids)) != len(ids):
            _fail("TAKEOFF_INPUT_INVALID")
        if any(len(key) > MAX_LINE_ID or LINE_ID_PATTERN.fullmatch(key) is None for key in ids):
            _fail("TAKEOFF_INPUT_INVALID")
        summary = {"schema": SCHEMA, "maturity": "preview", "complete": False,
                   "head": copy.deepcopy(physical_head["head"]) if physical_head else None,
                   "source": copy.deepcopy(source_binding),
                   "settings": {"pile_spacing_m": str(spacing.quantize(PILE_SPACING_STEP))},
                   "counts": {key: sum(line["status"] == status for line in lines)
                              for key, status in (("computed", "computed"), ("unavailable", "unavailable"),
                                                  ("not_computed", "not-computed"))},
                   "lines": lines}
        content = json.dumps(summary, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
        return ArtifactOutput(summary, "application/json", "takeoff.json", content)
