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
    # Aggregate linear takeoffs do not require an explicit grouping instruction.
    if re.search(r"\b(?:total|combined|aggregate|sum)\s+beam\s+lengths?\b"
                 r"|\b(?:total|combined|aggregate)\s+lengths?\s+of\s+"
                 r"(?:(?:the|each|all|my|these|those)\s+)*beams?\b", prompt):
        return True
    if (re.search(r"\bbeam\s+totals?\b", prompt)
            and re.search(r"\b(?:framing|centerlines?|centrelines?)\b", prompt)
            and not re.search(r"\b(?:count|number|area|volume|weight|cost|mass)\b", prompt)):
        return True
    spline = re.search(r"\b(?:splines?|nurbs|knots?)\b", prompt)
    if spline and re.search(r"\bbeams?\b", prompt) and re.search(r"\b(?:lengths?|measure|how long)\b", prompt):
        return True
    return bool(re.search(r"\bbeams?\b", prompt)
                and re.search(r"\blengths?\b", prompt)
                and (_GROUPED.search(prompt)
                     or re.search(r"\b(?:curved|spatial)\s+beams?\b|\b(?:3d|polylines?|vertices)\b|\bellip(?:ses?|tical)\b", prompt)))


def build_body(description, *, entry_point="run"):
    if entry_point not in ("run", "measure_gross_beams", "collect_spline_segments"):
        raise ValueError("Unsupported beam measurement entry point.")
    for pattern, label, factor in _UNITS:
        if re.search(pattern, description, re.IGNORECASE):
            break
    else:
        label, factor = "drawing_units", 1.0
    collect_segments = entry_point == "collect_spline_segments"
    requested_budget = re.search(r"\b(?:at most|budget of|limit of)\s+(\d+)\s+(?:spline )?evaluations?\b",
                                 description, re.I)
    evaluation_limit = min(int(requested_budget[1]), 1_000_000) if requested_budget else 1_000_000
    span_detail = entry_point == "run" and bool(re.search(r"\b(?:knots?|spans?|jumps?|discontin\w*)\b", description, re.I))
    spline_request = span_detail or re.search(r"\b(?:splines?|nurbs)\b", description, re.I)
    mixed_scope = re.search(
        r"\b(?:lines?|(?:lw)?polylines?|arcs?|ellip(?:ses?|tical)|bulges?|straight beams?|"
        r"(?:all|every) beams?|including (?:rational )?(?:splines?|nurbs))\b", description, re.I)
    explicit_spline_scope = re.search(
        r"\b(?:only (?:the )?(?:splines?|nurbs)|(?:splines?|nurbs)(?: beams?)? only)\b", description, re.I)
    only_splines = collect_segments or bool(spline_request and (explicit_spline_scope or not mixed_scope))
    periodic = bool(re.search(r"\bperiodic\b", description, re.I))
    survey = bool(re.search(r"\b(?:georeferenced|survey|large[- ]coordinates?|large[- ]origins?)\b", description, re.I))
    endpoints = bool(re.search(r"\b(?:endpoints?|start and end (?:coordinates|points))\b", description, re.I)
                     or spline_request and re.search(r"\b(?:orientation|direction)\b", description, re.I))
    list_members = re.search(r"\blist(?:s|ing)?\b|\beach\s+(?:(?:spline|nurbs)\s+)?beam\b", description, re.I)
    return (_BODY.replace("__ENTRY_POINT__", entry_point)
            .replace("__PERIODIC__", repr(periodic))
            .replace("__SURVEY__", repr(survey))
            .replace("__OUTPUT_UNIT__", repr(label))
            .replace("__OUTPUT_FACTOR__", repr(factor))
            .replace("__ONLY_SPLINES__", repr(only_splines))
            .replace("__COLLECT_SPLINES__", repr(collect_segments))
            .replace("__EVALUATION_LIMIT__", str(evaluation_limit))
            .replace("__ENDPOINTS__", repr(endpoints))
            .replace("__SPAN_DETAIL__", repr(span_detail))
            .replace("__LIST_MEMBERS__", repr(collect_segments or span_detail or endpoints or bool(list_members))))


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

    return bounded_conic_length(speed, half, arc_factor)


def bounded_conic_length(speed, half, arc_factor):
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


def ellipse_axes(path):
    major = path["major"]
    axis = unit_vector(major)
    nx, ny, nz = normal = unit_vector(path["nrm"])
    ratio = path["ratio"]
    if not 0 < ratio <= 1 or abs(sum(a*n for a, n in zip(axis, normal))) > 1e-8:
        raise ValueError("Invalid ELLIPSE axes.")
    x, y, z = major
    minor = tuple(ratio*v for v in (ny*z - nz*y, nz*x - nx*z, nx*y - ny*x))
    return major, minor


def native_ellipse_length(path, frame):
    major, minor = ellipse_axes(path)
    u, v = transform_vector(frame, major), transform_vector(frame, minor)
    start, end = path["start_param"], path["end_param"]
    if end == start or abs(end-start) > math.tau + 1e-12:
        raise ValueError("Ambiguous or invalid ELLIPSE sweep.")
    # A rounded positive full turn can exceed tau by a few ulps. Modulo
    # would turn it into a near-zero arc. Preserve genuinely small sweeps.
    sweep = math.tau if end-start >= math.tau else ((end-start) % math.tau or math.tau)
    mid = start + sweep / 2

    def speed(offset):
        angle = mid + offset
        return math.hypot(*(b*math.cos(angle) - a*math.sin(angle) for a, b in zip(u, v)))

    return bounded_conic_length(speed, sweep/2, sweep)


def native_arc_length(path, frame):
    radius = path["r"]
    start = math.radians(path["start_deg"])
    degrees = path["end_deg"] - path["start_deg"]
    if not all(math.isfinite(v) for v in (radius, start, degrees)) or radius <= 0:
        raise ValueError("Invalid ARC measurement.")
    if degrees == 0 or abs(degrees) > 360:
        raise ValueError("Ambiguous or invalid ARC sweep.")
    sweep = math.radians(degrees % 360 or 360)
    x_axis, y_axis, normal = extrusion_axes(path.get("nrm", (0, 0, 1)))
    # Construct each subarc's chord from its midpoint tangent. No distant
    # center subtraction, and a full turn never degenerates to a zero chord.
    count = math.ceil(sweep / (math.pi / 2))
    angle = sweep / count
    chord = 2*radius*math.sin(angle/2)
    bulge = math.tan(angle/4)
    length = 0.0
    for index in range(count):
        mid = start + (index + .5)*angle
        delta = tuple(chord*(v*math.cos(mid) - u*math.sin(mid)) for u, v in zip(x_axis, y_axis))
        length += curved_length(frame, delta, bulge, normal)
    return length


def __ENTRY_POINT__(intake, params):
    metres_per_unit = {1: 0.0254, 2: 0.3048, 3: 1609.344, 4: 0.001, 5: 0.01,
                       6: 1.0, 7: 1000.0, 10: 0.9144, 14: 0.1}
    source_unit = intake.get("insunits")
    if intake.get("insunits_error"):
        raise ValueError(intake["insunits_error"])
    factor = 1.0
    if __OUTPUT_UNIT__ != "drawing_units":
        if source_unit not in metres_per_unit:
            raise ValueError("Set a supported drawing insertion unit before measuring beam lengths.")
        factor = metres_per_unit[source_unit] / __OUTPUT_FACTOR__
    totals = {}
    evaluation_budget = [__EVALUATION_LIMIT__]
    members = {}
    span_reports = {}
    identity = identity_frame()

    def distinct_entities(records):
        # Handles identify siblings within a definition. Each INSERT placement
        # still visits that definition with its own transform and multiplicity.
        seen = {}
        unique = []
        for record in records:
            handle = record.get("handle")
            if handle:
                key = handle.upper()
                if key in seen:
                    if seen[key] != record:
                        raise ValueError("Conflicting beam evidence for handle " + key)
                    continue
                seen[key] = record
            unique.append(record)
        return unique

    def measure(path, frame, inherited, prefix, copies):
        layer = path.get("layer", "0")
        if layer == "0":
            layer = inherited
        if __ONLY_SPLINES__ and path.get("kind") != "SPLINE":
            return
        if path.get("space") == "paper":
            return
        if not re.search(r"(?:^|[^a-z0-9])beams?(?:$|[^a-z0-9])", layer.casefold()):
            return
        if __PERIODIC__ and not path.get("periodic"):
            return
        if path.get("measurement_error"):
            raise ValueError(path["measurement_error"])
        if (path.get("closed") and not path.get("polyline3d") and not path.get("bulges")
                and not (__PERIODIC__ and path.get("periodic"))):
            if __COLLECT_SPLINES__:
                raise ValueError("A closed spline has no free ends for a joined beam report.")
            return
        if path.get("kind") == "ELLIPSE":
            if __SURVEY__:
                raise ValueError("Survey precision bounds require straight beam coordinates.")
            length = native_ellipse_length(path, frame)
        elif path.get("kind") == "ARC":
            if __SURVEY__:
                raise ValueError("Survey precision bounds require straight beam coordinates.")
            length = native_arc_length(path, frame)
        elif path.get("kind") == "SPLINE":
            if __SURVEY__:
                raise ValueError("Survey precision bounds require straight beam coordinates.")
            length, span_report = spline_length(path, frame, factor, __SPAN_DETAIL__, evaluation_budget)
        else:
            points = path["pts"]
            vectors = path.get("measurement_vectors")
            bulges = path.get("bulges", [])
            if __SURVEY__ and any(bulges):
                raise ValueError("Survey precision bounds require straight beam coordinates.")
            if bulges and len(bulges) != len(points):
                raise ValueError("Beam curve lacks per-vertex bulges.")
            length = 0.0
            for i in range(len(points) if path.get("closed") else len(points) - 1):
                nxt = (i + 1) % len(points)
                delta = (vectors[i] if vectors is not None and i < len(vectors)
                         else [b-a for a,b in zip(points[i], points[nxt])])
                if __SURVEY__:
                    if not any(delta):
                        raise ValueError("Collapsed beam segment has no measurable length.")
                    if vectors is None and max(map(abs, (*points[i], *points[nxt]))) > 1e7 * math.hypot(*delta):
                        raise ValueError("Insufficient source precision for short beam segment.")
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
            if __SPAN_DETAIL__ and path.get("kind") == "SPLINE":
                span_reports[key] = span_report
            if (__COLLECT_SPLINES__ or __ENDPOINTS__) and path.get("kind") == "SPLINE":
                members[key]["ends"] = spline_endpoints(path, frame, evaluation_budget, require_continuity=__COLLECT_SPLINES__)
            elif __ENDPOINTS__:
                if path.get("kind") in ("ARC", "ELLIPSE"):
                    if "c" not in path:
                        raise ValueError("Missing conic center for endpoint coordinates.")
                    if path["kind"] == "ARC":
                        u, v, _ = extrusion_axes(path.get("nrm", (0, 0, 1)))
                        u, v = tuple(path["r"]*x for x in u), tuple(path["r"]*y for y in v)
                        start, end = math.radians(path["start_deg"]), math.radians(path["end_deg"])
                    else:
                        u, v = ellipse_axes(path)
                        start, end = path["start_param"], path["end_param"]
                    points = [tuple(c + a*math.cos(t) + b*math.sin(t) for c, a, b in zip(path["c"], u, v))
                              for t in (start, end)]
                else:
                    points = (path["pts"][0], path["pts"][0 if path.get("closed") else -1])
                ends = [transform_point(frame, tuple(point) if len(point) == 3 else (*point, 0)) for point in points]
                if not all(math.isfinite(value) for end in ends for value in end):
                    raise ValueError("Unresolved transformed beam endpoint.")
                members[key]["ends"] = ends

    spatial_loops = set(intake.get("beam_polyline3d_handles", []))
    for path in distinct_entities(intake.get("polylines", [])):
        if path["closed"] and path.get("handle") in spatial_loops:
            path = dict(path, polyline3d=True)
        measure(path, identity, "0", (), 1)

    for path in distinct_entities(intake.get("beam_ellipses", [])):
        measure(path, identity, "0", (), 1)

    for arc in distinct_entities(intake.get("arcs", [])):
        measure(dict(arc, kind="ARC"), identity, "0", (), 1)

    for path in distinct_entities(intake.get("splines", [])):
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
                if child.get("type") in ("LINE", "LWPOLYLINE", "POLYLINE", "SPLINE", "ELLIPSE"):
                    raise ValueError("Missing beam geometry in block: " + name)
                if child.get("type") == "INSERT" and "nested_inserts" not in block:
                    raise ValueError("Missing nested reference in block: " + name)
                if child.get("kind") in ("LINE", "ARC"):
                    paths.append(child)
                elif child.get("kind") == "LWPOLYLINE":
                    axes = ((0, 0, 0), *extrusion_axes(child.get("nrm", (0, 0, 1))))
                    points = [transform_point(axes, (p[0], p[1], child.get("elev", 0)))
                              for p in child["pts"]]
                    paths.append(dict(child, pts=points))
        elif not any(path.get("kind") == "ARC" for path in paths):
            paths = paths + [dict(child, measurement_error="Reimport this drawing to measure native block arcs at source precision.")
                             for child in block.get("children", []) if child.get("kind") == "ARC"]
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
        paths = distinct_entities(paths)
        nested = distinct_entities(block.get("nested_inserts", []))
        for index in range(placements):
            row, column = divmod(index, cols)
            frame = insert_frame(parent, insert, block.get("measurement_base", block.get("base", (0, 0, 0))), row, column, source_precision=True)
            branch = prefix
            if __LIST_MEMBERS__:
                tag = insert.get("handle")
                if tag and rows * cols > 1:
                    tag += "[" + str(index // cols) + "," + str(index % cols) + "]"
                branch = (*prefix, tag)
            for path in paths:
                measure(path, frame, layer, branch, copies)
            for child in nested:
                visit(child, frame, layer, branch, (*ancestors, name), copies)

    if any("unresolved block reference" in message for message in intake.get("parseErrors", [])):
        raise ValueError("Unresolved block reference in beam measurement.")
    placements = intake.get("beam_insert_placements", {})
    for insert in distinct_entities(intake.get("inserts", [])):
        readings = placements.get(insert.get("handle"), [])
        if readings:
            if any(reading != readings[0] for reading in readings[1:]):
                raise ValueError("Conflicting full-precision beam placement.")
            insert = dict(insert, measurement_placement=readings[0])
        visit(insert, identity, "0", (), (), 1)
    result = {"lengths_by_layer": totals, "units": __OUTPUT_UNIT__,
              "length_relative_tolerance": LENGTH_RELATIVE_TOLERANCE}
    if evaluation_budget[0] < __EVALUATION_LIMIT__:
        result["spline_evaluations"] = __EVALUATION_LIMIT__ - evaluation_budget[0]
        result["spline_evaluation_limit"] = __EVALUATION_LIMIT__
    if __SURVEY__:
        result["absolute_error_by_layer"] = {layer: value*LENGTH_RELATIVE_TOLERANCE for layer, value in totals.items()}
    if __ENDPOINTS__:
        result["endpoint_units"] = "drawing_units"
    if __SPAN_DETAIL__:
        result["spline_spans_by_handle"] = span_reports
    if __COLLECT_SPLINES__:
        result["output_per_drawing_unit"] = factor
    if __LIST_MEMBERS__:
        result["lengths_by_handle"] = members
    return (result, None)
"""
