"""Generate joined-spline measurements from transformed model-space endpoints."""
import re

import authored_beam_lengths


def matches(description):
    text = description.casefold()
    return (re.search(r"\b(?:splines?|nurbs)\b", text)
            and re.search(r"\bbeams?\b", text)
            and re.search(r"\b(?:join\w*|connect\w*|chains?|gaps?|breaks?|continu\w*)\b", text))


def build_body(description):
    return authored_beam_lengths.build_body(description, entry_point="collect_spline_segments") + _BODY


_BODY = """
JOIN_TOLERANCE = 1e-7  # drawing units


def chain_report(members, factor, unit):
    # Each segment starts as a chain with two free ends. Candidate joins are
    # between ends on different segments; never join a segment to itself.
    if len(members) > 256:
        raise ValueError("Joined beam report supports at most 256 segments per layer.")
    ends = [(handle, point) for handle, member in sorted(members.items()) for point in member["ends"]]
    links = []
    nearby = [0] * len(ends)
    for i, (left, a) in enumerate(ends):
        for j in range(i+1, len(ends)):
            right, b = ends[j]
            if left == right:
                continue
            distance = math.dist(a, b)
            if distance <= JOIN_TOLERANCE:
                nearby[i] += 1
                nearby[j] += 1
            links.append((distance, i, j))
    if any(count > 1 for count in nearby):
        raise ValueError("Ambiguous spline junction: multiple endpoints within tolerance.")

    owner = {handle: handle for handle in members}
    used = set()
    gaps = []
    count = len(members)
    ordered = sorted(links)
    for index, (distance, i, j) in enumerate(ordered):
        if i in used or j in used:
            continue
        left, right = ends[i][0], ends[j][0]
        u, v = owner[left], owner[right]
        if u == v:
            if distance <= JOIN_TOLERANCE and count > 1:
                raise ValueError("Closed spline chain cannot join the remaining components.")
            continue
        if distance > JOIN_TOLERANCE:
            # Only suggest the shortest available connection. A tie competing
            # for either endpoint or connecting the same components is ambiguous.
            for d, x, y in ordered[index+1:]:
                if d - distance > JOIN_TOLERANCE:
                    break
                ux, vy = owner[ends[x][0]], owner[ends[y][0]]
                if x in used or y in used or ux == vy:
                    continue
                if i in (x, y) or j in (x, y) or {ux, vy} == {u, v}:
                    raise ValueError("Ambiguous equally close spline gap endpoints.")
            gaps.append({"between": [left, right], "distance": distance*factor})
        used.update((i, j))
        for key in owner:
            if owner[key] == v:
                owner[key] = u
        count -= 1
        if count == 1:
            break
    if count != 1:
        raise ValueError("Could not resolve one spline chain on this beam layer.")
    return {"segments": sorted(members), "segment_length": sum(m[unit] for m in members.values()),
            "continuous": not gaps, "gaps": gaps}


def run(intake, params):
    report, _ = collect_spline_segments(intake, params)
    groups = {}
    for handle, member in report["lengths_by_handle"].items():
        groups.setdefault(member["layer"], {})[handle] = member
    factor = report["output_per_drawing_unit"]
    return {"units": report["units"], "length_relative_tolerance": LENGTH_RELATIVE_TOLERANCE,
            "endpoint_tolerance": JOIN_TOLERANCE*factor,
            "gap_policy": "nearest free endpoints on one beam layer; gaps excluded from length",
            "chains_by_layer": {layer: chain_report(group, factor, report["units"])
                                for layer, group in groups.items()}}, None
"""
