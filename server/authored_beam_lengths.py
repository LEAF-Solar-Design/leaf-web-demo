"""Generate the offline drawing tool for per-layer beam centerline takeoffs."""
import re


_GROUPED = re.compile(r"\b(?:by|per|each|every)\s+(?:drawing\s+)?layer\b|\blayer[- ]by[- ]layer\b")
_UNITS = (
    (r"\b(?:millimet(?:er|re)s?|mm)\b", "mm", 0.001),
    (r"\b(?:centimet(?:er|re)s?|cm)\b", "cm", 0.01),
    (r"\b(?:feet|foot|ft)\b", "ft", 0.3048),
    (r"\b(?:inches|inch)\b", "in", 0.0254),
    (r"\b(?:met(?:er|re)s?|m)\b", "m", 1.0),
)


def matches(description):
    prompt = description.casefold()
    return bool(re.search(r"\bbeams?\b", prompt)
                and re.search(r"\blengths?\b", prompt)
                and _GROUPED.search(prompt))


def build_body(description):
    for pattern, label, factor in _UNITS:
        if re.search(pattern, description, re.IGNORECASE):
            break
    else:
        label, factor = "drawing_units", 1.0
    return (_BODY.replace("__OUTPUT_UNIT__", repr(label))
            .replace("__OUTPUT_FACTOR__", repr(factor))
            .replace("__LIST_MEMBERS__", repr(bool(re.search(r"\blist(?:s|ing)?\b|\beach beam\b", description, re.I)))))


_BODY = """
import math
import re


def run(intake, params):
    metres_per_unit = {1: 0.0254, 2: 0.3048, 3: 1609.344, 4: 0.001, 5: 0.01,
                       6: 1.0, 7: 1000.0, 10: 0.9144, 14: 0.1}
    source_unit = intake.get("insunits")
    factor = 1.0
    if __OUTPUT_UNIT__ != "drawing_units":
        if source_unit not in metres_per_unit:
            raise ValueError("Set a supported drawing insertion unit before measuring beam lengths.")
        factor = metres_per_unit[source_unit] / __OUTPUT_FACTOR__
    totals = {}
    members = {}
    for path in intake.get("polylines", []):
        layer = path.get("layer", "0")
        if path.get("space") == "paper" or path.get("closed"):
            continue
        if not re.search(r"(?:^|[^a-z0-9])beams?(?:$|[^a-z0-9])", layer.casefold()):
            continue
        points = path["pts"]
        bulges = path.get("bulges", [])
        if bulges and len(bulges) != len(points):
            raise ValueError("Beam curve lacks per-vertex bulges.")
        length = 0.0
        for i in range(len(points) - 1):
            chord = math.dist(points[i], points[i+1])
            bulge = abs(bulges[i]) if bulges else 0.0
            if bulge:
                chord *= (1 + bulge**2) * (math.atan(bulge) / bulge)
            length += chord
        total = totals.get(layer, 0.0) + length * factor
        if not math.isfinite(total):
            raise ValueError("Beam length exceeds the supported numeric range.")
        totals[layer] = total
        if __LIST_MEMBERS__:
            handle = path.get("handle")
            if not handle or handle in members:
                raise ValueError("Individual beam lengths require unique entity handles.")
            members[handle] = {"layer": layer, __OUTPUT_UNIT__: length * factor}
    result = {"lengths_by_layer": totals, "units": __OUTPUT_UNIT__}
    if __LIST_MEMBERS__:
        result["lengths_by_handle"] = members
    return (result, None)
"""
