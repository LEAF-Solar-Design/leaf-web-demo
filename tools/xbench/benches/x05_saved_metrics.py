"""Independent, bounded measurements of caller-supplied canonical saved graphs.

No product parsers, stored digests, module-name inference, or input mutation.
Uncertainties deliberately contain no customer evidence.
"""

import base64
from decimal import Context, Decimal, localcontext
import hashlib
import json
import struct
import uuid


MAX_PANELS = 100_000
MAX_COLLECTION = 20_000
MAX_TEXT = 16 * 1024 * 1024
MAX_NODES = 500_000
MAX_DEPTH = 32
SAFE_INTEGER = 2**53 - 1
METRICS = ("panels", "optimizers", "inverters", "strings", "kw_dc")


def _finite(value):
    return type(value) in (int, float) and -1.7976931348623157e308 <= value <= 1.7976931348623157e308


def _bounded_json(value):
    """Preflight before encoding/decoding; depth also bounds cyclic input."""
    stack = [(value, 0)]
    nodes = size = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            return False
        if type(item) is dict:
            if len(item) > MAX_PANELS:
                return False
            for key, child in item.items():
                if type(key) is not str:
                    return False
                stack.extend(((key, depth + 1), (child, depth + 1)))
        elif type(item) is list:
            if len(item) > MAX_PANELS:
                return False
            stack.extend((child, depth + 1) for child in item)
        elif type(item) is str:
            if len(item) > MAX_TEXT:
                return False
            try:
                size += len(item.encode("utf-8"))
            except UnicodeError:
                return False
        elif type(item) is int:
            if item.bit_length() > 1024:
                return False
            size += 310
        elif type(item) is float:
            if not _finite(item):
                return False
            size += 24
        elif item is None or type(item) is bool:
            size += 5
        else:
            return False
        if size > MAX_TEXT:
            return False
    return True


def _collection(value, limit):
    return type(value) is list and len(value) <= limit


def _id(value, kind):
    prefix = "leaf:" + kind + ":"
    if type(value) is not str or len(value) != len(prefix) + 36 or not value.startswith(prefix):
        return False
    try:
        parsed = uuid.UUID(value[len(prefix):])
    except ValueError:
        return False
    return parsed.version == 4 and parsed.variant == uuid.RFC_4122 and str(parsed) == value[len(prefix):]


def _identities(items, kind, limit):
    if not _collection(items, limit):
        return None
    seen = set()
    for item in items:
        if type(item) is not dict or not _id(item.get("id"), kind) or item["id"] in seen:
            return None
        seen.add(item["id"])
    return seen


def _column(text, count):
    if type(text) is not str or len(text) != ((count * 16 + 2) // 3) * 4:
        return None
    try:
        raw = base64.b64decode(text, validate=True)
    except (ValueError, UnicodeError):
        return None
    return raw if len(raw) == count * 16 and base64.b64encode(raw).decode("ascii") == text else None


def _slots(block):
    keys = {"codec", "count", "panel_ids", "centres", "angle", "panel"}
    if type(block) is not dict or block.keys() != keys:
        return None
    count = block["count"]
    if (block["codec"] != "leaf.solar-ground-slots.v1" or type(count) is not int
            or not 1 <= count <= 10_000 or not _finite(block["angle"])):
        return None
    panel = block["panel"]
    if (type(panel) is not dict or panel.keys() != {"rev", "provenance", "validity", "extra"}
            or type(panel["rev"]) is not int or not 0 <= panel["rev"] <= 1_000_000
            or any(type(panel[key]) is not dict for key in ("provenance", "validity", "extra"))
            or not _bounded_json(panel)):
        return None
    raw = _column(block["panel_ids"], count)
    centres = _column(block["centres"], count)
    if raw is None or centres is None:
        return None
    if any(not _finite(x) or not _finite(y) for x, y in struct.iter_unpack("<dd", centres)):
        return None
    ids = set()
    for offset in range(0, len(raw), 16):
        row = uuid.UUID(bytes=raw[offset:offset + 16])
        if row.version != 4 or row.variant != uuid.RFC_4122:
            return None
        ids.add("leaf:panel:" + str(row))
    return ids if len(ids) == count else None


def _panels(graph):
    panels = _identities(graph.get("panels"), "panel", MAX_PANELS)
    frames = graph.get("frames")
    if panels is None or _identities(frames, "frame", MAX_COLLECTION) is None:
        return None, None
    compact = {}
    for index, frame in enumerate(frames):
        if "ground_slots" not in frame:
            continue
        ids = _slots(frame["ground_slots"])
        if ids is None or panels.intersection(ids) or len(panels) + len(ids) > MAX_PANELS:
            return None, None
        panels.update(ids)
        compact[index] = ids
    return panels, compact


def _strings(items, panels):
    ids = _identities(items, "string", MAX_COLLECTION)
    if panels is None or ids is None:
        return None
    assigned = set()
    for item in items:
        refs, count = item.get("ordered_panel_refs"), item.get("module_count")
        if (not _collection(refs, MAX_PANELS) or type(count) is not int
                or not 0 < count <= SAFE_INTEGER or count != len(refs)):
            return None
        for ref in refs:
            if not _id(ref, "panel") or ref not in panels or ref in assigned:
                return None
            assigned.add(ref)
    return str(len(ids))


def _inverters(items):
    if _identities(items, "inverter", MAX_COLLECTION) is None:
        return None
    count = 0
    for item in items:
        equipment = item.get("equipment_type")
        if type(equipment) is not str or equipment not in ("string_inverter", "central_inverter", "combiner_box"):
            return None
        count += equipment != "combiner_box"
    return str(count)


def _power(value):
    if not _finite(value) or not 0 < value < 10**18:
        return None
    power = Decimal(str(value))
    # Trim insignificant zeros without normalize(), which uses ambient context.
    digits = list(power.as_tuple().digits)
    exponent = power.as_tuple().exponent
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    return power if exponent >= -15 else None


def _dc(frames, panels, compact):
    if not panels:
        return None
    covered = set()
    with localcontext(Context(prec=200)):
        total = Decimal(0)
        for index, frame in enumerate(frames):
            refs = compact.get(index) if index in compact else frame.get("panel_refs")
            power = _power(frame.get("module_power_watts"))
            if (index not in compact and not _collection(refs, MAX_PANELS)) or power is None:
                return None
            for ref in refs:
                if not _id(ref, "panel") or ref not in panels or ref in covered:
                    return None
                covered.add(ref)
            total += power * len(refs) / 1000
            if total >= Decimal(10**18):
                return None
        if covered != panels:
            return None
        text = format(total, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text


def measure_saved_graph(graph):
    """Return only numeric strings, fixed uncertainty codes, and a string digest.

    Available metrics have an empty reason. String hashing validates the full
    JSON payload independently of its semantic suitability for counting.
    """
    values = dict.fromkeys(METRICS)
    reasons = {
        "panels": "PANEL_IDENTITY_OR_COMPACT_INVALID",
        "optimizers": "OPTIMIZER_MEASUREMENT_ABSENT",
        "inverters": "INVERTER_IDENTITY_OR_TYPE_INVALID",
        "strings": "STRING_IDENTITY_OR_ASSIGNMENT_INVALID",
        "kw_dc": "DC_COVERAGE_OR_POWER_INVALID",
    }
    digest = None
    if type(graph) is dict:
        strings = graph.get("strings")
        if _collection(strings, MAX_COLLECTION) and _bounded_json(strings):
            payload = json.dumps(strings, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if len(payload) <= MAX_TEXT:
                digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        panels, compact = _panels(graph)
        if panels is not None:
            values["panels"] = str(len(panels))
            values["kw_dc"] = _dc(graph["frames"], panels, compact)
        values["strings"] = _strings(strings, panels)
        values["inverters"] = _inverters(graph.get("inverters"))
    for metric, value in values.items():
        if value is not None:
            reasons[metric] = ""
    return {"values": values, "reasons": reasons, "strings_sha256": digest}
