"""Generate the offline drawing tool for per-layer beam centerline takeoffs."""
import re

from authored_block_transform import TRANSFORM_SOURCE
from authored_spline_length import SPLINE_LENGTH_SOURCE


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
                and (_GROUPED.search(prompt)
                     or re.search(r"\bcurved\s+beams?\b", prompt)))


def build_body(description, *, entry_point="run"):
    if entry_point not in ("run", "measure_gross_beams", "collect_spline_segments"):
        raise ValueError("Unsupported beam measurement entry point.")
    for pattern, label, factor in _UNITS:
        if re.search(pattern, description, re.IGNORECASE):
            break
    else:
        label, factor = "drawing_units", 1.0
    collect_segments = entry_point == "collect_spline_segments"
    return (_BODY.replace("__ENTRY_POINT__", entry_point)
            .replace("__OUTPUT_UNIT__", repr(label))
            .replace("__OUTPUT_FACTOR__", repr(factor))
            .replace("__COLLECT_SPLINES__", repr(collect_segments))
            .replace("__LIST_MEMBERS__", repr(collect_segments or bool(re.search(r"\blist(?:s|ing)?\b|\beach beam\b", description, re.I)))))


_BODY = """
import math
import re
""" + TRANSFORM_SOURCE + SPLINE_LENGTH_SOURCE + """


LENGTH_RELATIVE_TOLERANCE = 1e-5


def curved_length(frame, delta, bulge, normal):
    # The unscaled chord and n x chord span the circle. Its tangent at
    # mid-sweep points along the chord, including clockwise and major arcs.
    nx, ny, nz = unit_vector(normal)
    dx, dy, dz = delta
    across = (ny*dz - nz*dy, nz*dx - nx*dz, nx*dy - ny*dx)
    along = transform_vector(frame, delta)
    across = transform_vector(frame, across)
    size = abs(bulge)
    half = 2 * math.atan(size)
    # Factor out radius times sweep to avoid constructing a remote arc center.
    arc_factor = (math.atan(size) / size) + size * math.atan(size)
    if frame == identity_frame():
        return math.hypot(*along) * arc_factor

    def speed(angle):
        c = math.cos(angle)
        s = math.copysign(1.0, bulge) * math.sin(angle)
        return math.hypot(*(c*x + s*y for x, y in zip(along, across)))

    # For each subarc its chord bounds length below, and its two endpoint
    # tangents up to their intersection bound length above. Both bounds
    # survive any affine placement. Double the mesh and reuse endpoints.
    count = 4
    step = 2 * half / count
    endpoints = (speed(-half) + speed(half)) / 2
    endpoints += sum(speed(-half + i*step) for i in range(1, count))
    while count <= 16384:
        midpoints = sum(speed(-half + (i + .5)*step) for i in range(count))
        h = step / 2
        if h == 0:
            return arc_factor * speed(0)
        lower = (math.sin(h) / h) * (midpoints / count)
        upper = (math.tan(h) / h) * (endpoints / count)
        if upper - lower <= 2 * LENGTH_RELATIVE_TOLERANCE * lower:
            return arc_factor * ((lower + upper) / 2)
        endpoints += midpoints
        count *= 2
        step /= 2
    raise ValueError("Curved beam length could not meet the reported tolerance.")


def __ENTRY_POINT__(intake, params):
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
    identity = identity_frame()

    def measure(path, frame, inherited, prefix, copies):
        layer = path.get("layer", "0")
        if layer == "0":
            layer = inherited
        if __COLLECT_SPLINES__ and path.get("kind") != "SPLINE":
            return
        if path.get("space") == "paper":
            return
        if not re.search(r"(?:^|[^a-z0-9])beams?(?:$|[^a-z0-9])", layer.casefold()):
            return
        if path.get("closed"):
            if __COLLECT_SPLINES__:
                raise ValueError("A closed spline has no free ends for a joined beam report.")
            return
        if path.get("kind") == "SPLINE":
            length = spline_length(path, frame)
        else:
            points = path["pts"]
            bulges = path.get("bulges", [])
            if bulges and len(bulges) != len(points):
                raise ValueError("Beam curve lacks per-vertex bulges.")
            length = 0.0
            for i in range(len(points) - 1):
                delta = [b-a for a, b in zip(points[i], points[i+1])]
                if len(delta) == 2:
                    delta.append(0)
                bulge = bulges[i] if bulges else 0.0
                if bulge:
                    length += curved_length(frame, delta, bulge, path.get("nrm", (0, 0, 1)))
                else:
                    length += math.hypot(*transform_vector(frame, delta))
        total = totals.get(layer, 0.0) + copies * length * factor
        if not math.isfinite(total):
            raise ValueError("Beam length exceeds the supported numeric range.")
        totals[layer] = total
        if __LIST_MEMBERS__:
            handle = path.get("handle")
            key = "/".join((*prefix, handle)) if handle and all(prefix) else ""
            if not key or key in members:
                raise ValueError("Individual beam lengths require unique entity handles.")
            members[key] = {"layer": layer, __OUTPUT_UNIT__: length * factor}
            if __COLLECT_SPLINES__:
                members[key]["ends"] = spline_endpoints(path, frame)

    for path in intake.get("polylines", []):
        measure(path, identity, "0", (), 1)

    for path in intake.get("splines", []):
        measure(path, identity, "0", (), 1)

    blocks = {name.casefold(): block for name, block in intake.get("blocks", {}).items()}
    visited = 0

    def visit(insert, parent, inherited, prefix, ancestors, copies):
        nonlocal visited
        if insert.get("space") == "paper":
            return
        name = insert["name"].casefold()
        block = blocks.get(name)
        if name in ancestors or len(ancestors) >= 32:
            raise ValueError("Beam traversal encountered a cyclic or too deeply nested block.")
        if block is None or block.get("nested_inserts_incomplete") or block.get("beam_paths_incomplete"):
            raise ValueError("Incomplete block evidence for beam measurement: " + name)
        paths = block.get("beam_paths")
        if paths is None:
            children = block.get("children", [])
            if block.get("count", len(children)) > len(children):
                raise ValueError("Beam paths require complete block geometry: " + name)
            paths = []
            for child in children:
                if child.get("type") in ("LINE", "LWPOLYLINE", "POLYLINE", "SPLINE"):
                    raise ValueError("Missing beam geometry in block: " + name)
                if child.get("type") == "INSERT" and "nested_inserts" not in block:
                    raise ValueError("Missing nested reference in block: " + name)
                if child.get("kind") == "LINE":
                    paths.append(child)
                elif child.get("kind") == "LWPOLYLINE":
                    axes = ((0, 0, 0), *extrusion_axes(child.get("nrm", (0, 0, 1))))
                    points = [transform_point(axes, (p[0], p[1], child.get("elev", 0)))
                              for p in child["pts"]]
                    paths.append(dict(child, pts=points))
        layer = insert.get("layer", "0")
        if layer == "0":
            layer = inherited
        rows, cols = insert.get("rows", 1), insert.get("columns", 1)
        if rows < 1 or cols < 1:
            raise ValueError("Invalid beam block array.")
        placements = rows * cols if __LIST_MEMBERS__ else 1
        visited += placements
        if visited > 10000:
            raise ValueError("Beam block traversal exceeds 10000 placements.")
        if not __LIST_MEMBERS__:
            copies *= rows * cols
        for index in range(placements):
            row, column = divmod(index, cols)
            frame = insert_frame(parent, insert, block.get("base", (0, 0, 0)), row, column)
            branch = prefix
            if __LIST_MEMBERS__:
                tag = insert.get("handle")
                if tag and rows * cols > 1:
                    tag += "[" + str(index // cols) + "," + str(index % cols) + "]"
                branch = (*prefix, tag)
            for path in paths:
                measure(path, frame, layer, branch, copies)
            for child in block.get("nested_inserts", []):
                visit(child, frame, layer, branch, (*ancestors, name), copies)

    if any("unresolved block reference" in message for message in intake.get("parseErrors", [])):
        raise ValueError("Unresolved block reference in beam measurement.")
    for insert in intake.get("inserts", []):
        visit(insert, identity, "0", (), (), 1)
    result = {"lengths_by_layer": totals, "units": __OUTPUT_UNIT__,
              "length_relative_tolerance": LENGTH_RELATIVE_TOLERANCE}
    if __COLLECT_SPLINES__:
        result["output_per_drawing_unit"] = factor
    if __LIST_MEMBERS__:
        result["lengths_by_handle"] = members
    return (result, None)
"""
