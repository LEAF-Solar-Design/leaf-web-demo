"""Author a level takeoff from beam centerlines and opening station marks."""

import re


def matches(description):
    prompt = description.casefold()
    return all(re.search(pattern, prompt) for pattern in (
        r"\b(?:steel|beams?)\b",
        r"\blengths?\b",
        r"\b(?:levels?|floors?|stor(?:ey|y)s?|stories)\b",
        r"\b(?:openings?|cut[- ]?outs?)\b",
        r"\b(?:installed|net|deduct\w*|subtract\w*|minus|remov\w*|exclud\w*|less)\b",
    ))


def build_body(description):
    return _BODY


_BODY = r"""
import math
import re


def centerline_length(path):
    points = path["pts"]
    bulges = path.get("bulges", [])
    if len(points) < 2 or (bulges and len(bulges) != len(points)):
        raise ValueError("Incomplete beam centerline geometry.")
    length = 0.0
    for index in range(len(points) - 1):
        chord = math.dist(points[index], points[index + 1])
        bulge = abs(bulges[index]) if bulges else 0.0
        if bulge:
            chord *= (1 + bulge**2) * (math.atan(bulge) / bulge)
        length += chord
    if not math.isfinite(length) or length <= 0:
        raise ValueError("Beam centerline length must be finite and positive.")
    return length


def run(intake, params):
    # Open centerlines use BEAM-<level> or S-BEAM-<level> layers.
    beams = {}
    intervals = {}
    for path in intake.get("polylines", []):
        if path.get("space") == "paper" or path.get("closed"):
            continue
        layer = path.get("layer", "")
        match = re.fullmatch(r"(?:S[-_ ]+)?BEAMS?[-_ ]+(.+)", layer, re.I)
        if not match:
            continue
        handle = path["handle"].upper()
        if not handle or handle in beams:
            raise ValueError("Opening stations require unique beam handles.")
        beams[handle] = {"level": match.group(1), "gross_length": centerline_length(path)}
        intervals[handle] = []
    if not beams:
        raise ValueError("Use BEAM-<level> layers for model-space beam centerlines.")

    # Stations run from the first vertex in drawing units. A model-space
    # TEXT or MTEXT on Openings carries: OPENING <beam handle> <start> <end>.
    for note in intake.get("texts", []):
        if note.get("space") == "paper" or note.get("layer", "").casefold() not in ("opening", "openings"):
            continue
        fields = note["text"].split()
        if len(fields) != 4 or fields[0].casefold() != "opening":
            raise ValueError("Opening marks must read OPENING <beam handle> <start> <end>.")
        handle = fields[1].upper()
        if handle not in beams:
            raise ValueError("Opening references unknown beam " + handle)
        try:
            start, end = float(fields[2]), float(fields[3])
        except ValueError:
            raise ValueError("Opening stations must be numeric for beam " + handle)
        if not 0 <= start < end <= beams[handle]["gross_length"]:
            raise ValueError("Opening stations must be ordered and within beam " + handle)
        intervals[handle].append((start, end))

    totals = {}
    for handle, beam in beams.items():
        covered = 0.0
        previous_end = 0.0
        for start, end in sorted(intervals[handle]):
            if end > previous_end:
                covered += end - max(start, previous_end)
                previous_end = end
        beam["opening_length"] = covered
        beam["installed_length"] = beam["gross_length"] - covered
        level = beam["level"]
        totals[level] = totals.get(level, 0.0) + beam["installed_length"]
        if not math.isfinite(totals[level]):
            raise ValueError("Installed length exceeds the numeric range on level " + level)
    return {"units": "drawing_units", "installed_length_by_level": totals, "beams": beams}, None
"""
