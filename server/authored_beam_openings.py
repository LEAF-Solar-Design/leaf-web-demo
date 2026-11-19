"""Author a level takeoff from beam centerlines and opening station marks."""

import re

import authored_beam_lengths


def matches(description):
    prompt = description.casefold()
    return all(re.search(pattern, prompt) for pattern in (
        r"\b(?:steel|beams?)\b",
        r"\b(?:lengths?|deductions?)\b",
        r"\b(?:levels?|floors?|stor(?:ey|y)s?|stories)\b",
        r"\b(?:openings?|cut[- ]?outs?)\b",
        r"\b(?:installed|net|deduct\w*|subtract\w*|minus|remov\w*|exclud\w*|less)\b",
    ))


def build_body(description):
    gross_measure = authored_beam_lengths.build_body(
        "List beam lengths by layer.", entry_point="measure_gross_beams")
    return gross_measure + _BODY


_BODY = r"""
def run(intake, params):
    # Gross lengths and qualified paths come from the same traversal as a beam list.
    gross, _ = measure_gross_beams(intake, {})
    beams = {}
    intervals = {}
    by_leaf = {}
    for path, member in gross["lengths_by_handle"].items():
        match = re.fullmatch(r"(?:S[-_ ]+)?BEAMS?[-_ ]+(.+)", member["layer"], re.I)
        if not match:
            continue
        owner = path.upper()
        if owner in beams:
            raise ValueError("Opening stations require unique instance paths.")
        length = member["drawing_units"]
        if not math.isfinite(length) or length <= 0:
            raise ValueError("Beam centerline length must be finite and positive.")
        beams[owner] = {"level": match.group(1), "gross_length": length}
        intervals[owner] = []
        by_leaf.setdefault(owner.rsplit("/", 1)[-1], []).append(owner)
    if not beams:
        raise ValueError("Use BEAM-<level> layers for model-space beam centerlines.")

    # Mark stations are world distances from the placed centerline's start.
    # Reused definition handles require the full path including array indices.
    for note in intake.get("texts", []):
        if note.get("space") == "paper" or note.get("layer", "").casefold() not in ("opening", "openings"):
            continue
        fields = note["text"].split()
        if len(fields) != 4 or fields[0].casefold() != "opening":
            raise ValueError("Opening marks must read OPENING <beam handle> <start> <end>.")
        handle = fields[1].upper()
        if "/" not in handle:
            owners = by_leaf.get(handle, [])
            if len(owners) > 1:
                raise ValueError("Ambiguous opening owner " + handle + ": use the full beam instance path.")
            if owners:
                handle = owners[0]
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
    deductions = {}
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
        deductions[level] = deductions.get(level, 0.0) + covered
        if not math.isfinite(totals[level]) or not math.isfinite(deductions[level]):
            raise ValueError("Installed length exceeds the numeric range on level " + level)
    ranking = sorted(deductions, key=lambda level: (-deductions[level], level))
    greatest = [level for level in ranking if deductions[level] == deductions[ranking[0]]]
    return {"units": "drawing_units", "installed_length_by_level": totals, "beams": beams,
            "deduction_by_level": deductions, "floor_ranking": ranking,
            "greatest_deduction_levels": greatest}, None
"""
