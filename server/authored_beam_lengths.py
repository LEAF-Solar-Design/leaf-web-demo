"""Generate the offline drawing tool for per-layer beam centerline takeoffs."""
import re

from authored_block_transform import TRANSFORM_SOURCE


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
""" + TRANSFORM_SOURCE + """


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
    identity = identity_frame()

    def measure(path, frame, inherited, prefix, copies):
        layer = path.get("layer", "0")
        if layer == "0":
            layer = inherited
        if path.get("space") == "paper" or path.get("closed"):
            return
        if not re.search(r"(?:^|[^a-z0-9])beams?(?:$|[^a-z0-9])", layer.casefold()):
            return
        points = path["pts"]
        bulges = path.get("bulges", [])
        if bulges and len(bulges) != len(points):
            raise ValueError("Beam curve lacks per-vertex bulges.")
        if any(bulges) and frame != identity:
            # A circular bulge stays circular under a similarity in its plane.
            # An anisotropically stretched arc requires elliptical integration.
            x, y, _ = extrusion_axes(path.get("nrm", (0, 0, 1)))
            x, y = transform_vector(frame, x), transform_vector(frame, y)
            nx, ny = math.hypot(*x), math.hypot(*y)
            if (not math.isclose(nx, ny, rel_tol=1e-8)
                    or abs(sum(a*b for a, b in zip(x, y))) > 1e-8*nx*ny):
                raise ValueError("A nonuniformly scaled curved beam requires elliptical measurement.")
        length = 0.0
        for i in range(len(points) - 1):
            delta = [b-a for a, b in zip(points[i], points[i+1])]
            if len(delta) == 2:
                delta.append(0)
            chord = math.hypot(*transform_vector(frame, delta))
            bulge = abs(bulges[i]) if bulges else 0.0
            if bulge:
                chord *= (1 + bulge**2) * (math.atan(bulge) / bulge)
            length += chord
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

    for path in intake.get("polylines", []):
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
                if child.get("type") in ("LINE", "LWPOLYLINE", "POLYLINE"):
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
        # Array offsets and translations cannot affect a centerline length.
        frame = insert_frame(parent, insert, block.get("base", (0, 0, 0)))
        for index in range(placements):
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
    result = {"lengths_by_layer": totals, "units": __OUTPUT_UNIT__}
    if __LIST_MEMBERS__:
        result["lengths_by_handle"] = members
    return (result, None)
"""
