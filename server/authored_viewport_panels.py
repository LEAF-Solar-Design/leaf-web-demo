"""Layer-visibility evidence from paper-space VIEWPORT entities and LAYER records."""
import math
import re

from authored_clipped_panels import CLIP_SOURCE
from authored_dynamic_visibility import INSTANCE_SOURCE, _records
from authored_panel_regions import _outline


def matches(description):
    return (re.search(r"\bviewports?\b", description, re.I)
            and re.search(r"\b(?:panels?|modules?)\b", description, re.I)
            and re.search(r"\b(?:frozen|unfrozen|thawed|layers?)\b", description, re.I)
            and (comparison_requested(description)
                 or re.search(r"\b(?:count|list|enumerate|tally|which|how many)\b", description, re.I)))


def camera_diagnostics_requested(description):
    return (re.search(r"\bviewports?\b", description, re.I)
            and re.search(r"\bcameras?\b", description, re.I)
            and re.search(r"\b(?:degenerate|invalid|malformed|broken|uncheckable|unreadable|"
                          r"diagnos(?:e|tic|tics))\b", description, re.I))


def viewport_handle(description):
    match = re.search(r"\bviewport\s+(?:handle\s+)?(?:0x)?([a-f0-9]+)\b", description, re.I)
    return match[1].upper() if match else ""


_VIEWPORT_IDS = re.compile(
    r"\bviewports?\s+(?:handles?\s+)?((?:0x)?[a-f0-9]+\b"
    r"(?:\s*(?:,\s*(?:and\s+)?|and\s+|versus\s+|vs\.?\s+)"
    r"(?:viewport\s+(?:handle\s+)?)?(?:0x)?[a-f0-9]+\b)*)", re.I)


def comparison_requested(description):
    names = re.findall(r"\bviewports?\b", description, re.I)
    return ((len(names) > 1 or any(name.casefold() == "viewports" for name in names)
             or re.search(r"\b(?:two|2) viewport inventor(?:y|ies)\b", description, re.I)
             or len(_explicit_viewport_ids(description)) > 1)
            and bool(re.search(r"\b(?:compare|comparison|differences?|only one|exclusive|versus|vs)\b",
                               description, re.I)))


def revision_comparison_requested(description):
    panel_scope = re.search(r"\b(?:panels?|modules?|footprints?)\b", description, re.I)
    layer_inventory = (re.search(r"\binventor(?:y|ies)\b", description, re.I)
                       and re.search(r"\b(?:layer freezing|dynamic (?:visibility|state))\b", description, re.I))
    return (comparison_requested(description)
            and re.search(r"\b(?:revisions?|drawing versions?|parent drawing)\b", description, re.I)
            and (panel_scope or layer_inventory))


def _explicit_viewport_ids(description):
    return [value.upper() for match in _VIEWPORT_IDS.finditer(description)
            for value in re.findall(r"\b(?:0x)?([a-f0-9]+)\b", match[1], re.I)]


def comparison_handles(description):
    handles = _explicit_viewport_ids(description)
    if handles and (len(handles) != 2 or handles[0] == handles[1]):
        raise ValueError("Viewport comparison requires two distinct viewport identities.")
    return handles


def parameter_schema(description):
    if comparison_requested(description):
        field = "viewport_handles"
        prop = {"type": "array", "items": {"type": "string", "pattern": "^(?:0[xX])?[a-fA-F0-9]+$"},
                "minItems": 2, "maxItems": 2, "uniqueItems": True,
                "description": "Two paper-space viewport handles in comparison order."}
        bound = comparison_handles(description)
    else:
        field, prop = "viewport_handle", {"type": "string"}
        bound = viewport_handle(description)
    return {"type": "object", "properties": {field: prop}, "required": [] if bound else [field]}


def _camera(fields):
    """Read the camera and model-to-paper mapping."""
    flags = int(fields.get(90, 0))
    direction = tuple(float(fields.get(code, default))
                      for code, default in ((16, 0), (26, 0), (36, 1)))
    twist = math.radians(float(fields.get(51, 0)))
    direction_length = math.hypot(*direction)
    if not math.isfinite(direction_length) or not direction_length or not math.isfinite(twist):
        raise ValueError("Viewport crop requires a finite view direction and twist.")
    cx, cy, width, height, view_height = (float(fields[code]) for code in (10, 20, 40, 41, 45))
    view_center = tuple(float(fields.get(code, 0)) for code in (12, 22))
    target = tuple(float(fields.get(code, 0)) for code in (17, 27, 37))
    if (not all(map(math.isfinite, (cx, cy, width, height, view_height, *view_center, *target)))
            or min(width, height, view_height) <= 0):
        raise ValueError("Invalid viewport dimensions or model view center.")
    scale = height / view_height
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("Viewport projection exceeds the numeric range.")
    eye_distance = None
    if flags & 1:
        lens = float(fields.get(42, 0))
        # Match the drawing raster camera model. Require the DXF camera
        # distance and 42 mm film-diagonal framing to agree independently.
        calibrated_distance = (lens / 42) * view_height * (math.hypot(width, height) / height)
        if (36 not in fields or 37 not in fields or not math.isfinite(lens) or lens <= 0
                or not math.isclose(direction_length, calibrated_distance, rel_tol=1e-8)):
            raise ValueError("Unsupported perspective viewport calibration.")
        eye_distance = direction_length
    return {"scale": scale, "paper_center": (cx, cy), "view_center": view_center,
            "target": target, "direction": direction, "twist": twist,
            "eye_distance": eye_distance}


def _crop(fields, boundaries, camera, layout_owner):
    """Read the depth planes and paper crop for a resolved camera."""
    flags = int(fields.get(90, 0))
    eye_distance = camera["eye_distance"]
    if flags & 2 and not flags & 16 and eye_distance is None:
        raise ValueError("Orthographic front clipping requires an explicit plane.")
    front = float(fields[43]) if flags & 2 and flags & 16 else math.inf
    back = float(fields[44]) if flags & 4 else -math.inf
    if ((flags & 2 and flags & 16 and not math.isfinite(front))
            or (flags & 4 and not math.isfinite(back)) or front < back):
        raise ValueError("Invalid viewport depth planes.")
    clip_handle = fields.get(340, "").upper()
    if clip_handle or flags & 65536:
        matches = boundaries.get(clip_handle, ())
        if len(matches) != 1 or not clip_handle:
            raise ValueError("Missing or ambiguous viewport crop boundary.")
        boundary, boundary_layout, boundary_owner = matches[0]
        if (boundary_layout.casefold() != fields[410].casefold()
                or layout_owner and boundary_owner and layout_owner != boundary_owner):
            raise ValueError("Viewport crop boundary belongs to a different layout.")
        if not 3 <= int(dict(boundary).get(90, 0)) <= 512:
            raise ValueError("Invalid or oversized viewport crop boundary.")
        polygon = _outline(boundary)
    else:
        cx, cy = camera["paper_center"]
        width, height = (float(fields[code]) for code in (40, 41))
        polygon = [(cx + x * width / 2, cy + y * height / 2)
                   for x, y in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
    if not all(math.isfinite(value) for point in polygon for value in point):
        raise ValueError("Viewport crop exceeds the numeric range.")
    return {**camera, "points": polygon, "inverted": False,
            "front": front if math.isfinite(front) else None,
            "back": back if math.isfinite(back) else None}


def _activation_value(fields, code, default):
    try:
        return int(fields.get(code, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Unreadable viewport activation field (DXF group {code}).") from exc


def _paper_layouts_by_owner(pairs):
    """Resolve standard paper space via reciprocal BLOCK_RECORD and LAYOUT links."""
    section = table = ""
    block_records, layouts = {}, {}
    for kind, record in _records(pairs):
        if kind not in ("SECTION", "ENDSEC", "TABLE", "ENDTAB", "BLOCK_RECORD", "LAYOUT"):
            continue
        fields = dict(record)
        if kind == "SECTION":
            section = fields.get(2, "")
        elif kind == "ENDSEC":
            section = table = ""
        elif kind == "TABLE":
            table = fields.get(2, "")
        elif kind == "ENDTAB":
            table = ""
        elif kind == "BLOCK_RECORD" and section == "TABLES" and table == "BLOCK_RECORD":
            handle = fields.get(5, "").upper()
            name = fields.get(2, "").casefold()
            layout = fields.get(340, "").upper()
            if handle and re.fullmatch(r"\*paper_space\d*", name) and layout:
                block_records.setdefault(handle, []).append((name, layout))
        elif kind == "LAYOUT" and section == "OBJECTS" and (100, "AcDbLayout") in record:
            handle = fields.get(5, "").upper()
            layout_fields = record[record.index((100, "AcDbLayout")) + 1:]
            name = next((value for code, value in layout_fields if code == 1), "")
            owner = next((value.upper() for code, value in layout_fields if code == 330), "")
            if handle and owner and name and name.casefold() != "model":
                layouts.setdefault(handle, []).append((owner, name))

    resolved = {}
    for owner, records in block_records.items():
        if len(records) != 1:
            continue
        block_name, layout_handle = records[0]
        linked_layouts = layouts.get(layout_handle, ())
        if len(linked_layouts) == 1 and linked_layouts[0][0] == owner:
            resolved[owner] = (block_name, linked_layouts[0][1])
    return resolved


def _record_owner(record):
    """Read the entity owner outside extension groups and subclass fields."""
    depth = 0
    for code, value in record:
        if code == 100:
            break
        if code == 102 and value.startswith("{"):
            depth += 1
        elif code == 102 and value == "}":
            depth = max(depth - 1, 0)
        elif code == 330 and not depth:
            return value.upper()
    return ""


def attach_viewports(pairs, out):
    if (0, "VIEWPORT") not in pairs:
        return
    section = table = ""
    paper_blocks = _paper_layouts_by_owner(pairs)
    paper_owner = paper_layout = ""
    layers, viewports = {}, {}
    crop_inputs, boundaries = {}, {}
    hidden = set()
    names = set()
    activation_error = None
    try:
        for kind, record in _records(pairs):
            fields = dict(record)
            if kind == "SECTION":
                section = fields.get(2, "")
            elif kind == "ENDSEC":
                section = table = paper_owner = paper_layout = ""
            elif kind == "TABLE":
                table = fields.get(2, "")
            elif kind == "ENDTAB":
                table = ""
            elif kind == "LAYER" and section == "TABLES" and table == "LAYER":
                handle = fields.get(5, "").upper()
                name = fields.get(2, "").casefold()
                if not re.fullmatch("[A-F0-9]+", handle) or not name or handle in layers or name in names:
                    raise ValueError("Missing or duplicate layer identity.")
                layers[handle] = name
                names.add(name)
                if int(fields.get(70, 0)) & 1 or int(fields.get(62, 7)) < 0:
                    hidden.add(name)
            elif kind == "BLOCK" and section == "BLOCKS":
                owner = _record_owner(record)
                block = paper_blocks.get(owner)
                if block and block[0] == fields.get(2, "").casefold():
                    paper_owner, paper_layout = owner, block[1]
                else:
                    paper_owner = paper_layout = ""
            elif kind == "ENDBLK":
                paper_owner = paper_layout = ""
            elif kind in ("VIEWPORT", "LWPOLYLINE"):
                owner = _record_owner(record)
                layout_tag = fields.get(410, "")
                if section == "BLOCKS" and paper_owner and owner == paper_owner:
                    if layout_tag and layout_tag.casefold() != paper_layout.casefold():
                        continue
                    layout = paper_layout
                elif section == "ENTITIES" and (
                    fields.get(67) == "1" or layout_tag and layout_tag.casefold() != "model"
                ):
                    layout = layout_tag or paper_blocks.get(owner, ("", ""))[1]
                else:
                    continue
                fields[410] = layout
                handle = fields.get(5, "").upper()
                if kind == "LWPOLYLINE":
                    boundaries.setdefault(handle, []).append(
                        (record, layout, owner if owner in paper_blocks else ""))
                    continue
                if not re.fullmatch("[A-F0-9]+", handle) or handle in viewports:
                    raise ValueError("Missing or duplicate viewport identity.")
                view = {"layout": layout,
                        "layers": [v.upper() for c, v in record if c == 331]}
                viewports[handle] = view
                try:
                    view["enabled"] = (_activation_value(fields, 68, 1) != 0
                                       and not _activation_value(fields, 90, 0) & 131072
                                       and _activation_value(fields, 69, 0) != 1)
                except ValueError as exc:
                    view["activation_error"] = str(exc)
                    activation_error = activation_error or str(exc.__cause__)
                    continue
                if view["enabled"]:
                    crop_inputs[handle] = (fields, owner if owner in paper_blocks else "")
        for handle, (fields, layout_owner) in crop_inputs.items():
            view = viewports[handle]
            try:
                camera = _camera(fields)
            except (KeyError, TypeError, ValueError) as exc:
                view["camera_error"] = str(exc)
                view["crop_error"] = "Unresolved viewport crop: " + str(exc)
                continue
            try:
                view["crop"] = _crop(fields, boundaries, camera, layout_owner)
            except (KeyError, TypeError, ValueError) as exc:
                view["crop_error"] = "Unresolved viewport crop: " + str(exc)
        out["viewport_layers"] = {"layers": layers, "globally_hidden": sorted(hidden), "views": viewports}
        if activation_error:
            out["viewport_layers"]["error"] = activation_error
    except (ValueError, TypeError) as exc:
        out["viewport_layers"] = {"error": activation_error or str(exc)}


def build_body(description):
    crop = bool(re.search(r"\b(?:crops?|cropped|cropping|clip(?:ped|ping)?|boundar(?:y|ies))\b", description, re.I))
    explicit_footprint = bool(re.search(r"\bfootprints?\b", description, re.I))
    linked = revision_comparison_requested(description)
    crop = crop or explicit_footprint or bool(linked and re.search(r"\bperspective\b", description, re.I))
    footprint = explicit_footprint or (crop and bool(re.search(
        r"\b(?:whole|fully|entirely|wholly|completely)\b", description, re.I)))
    body = _BODY.replace("__CROP__", repr(crop)).replace("__FOOTPRINT__", repr(footprint))
    if comparison_requested(description):
        entry = _SELECT_PAIR.replace("__HANDLES__", repr(comparison_handles(description)))
        entry += _REVISION_COMPARE if linked else _COMPARE
    else:
        entry = _SINGLE.replace("__HANDLE__", repr(viewport_handle(description)))
    return CLIP_SOURCE + INSTANCE_SOURCE + _GEOMETRY + body + entry


CAMERA_DIAGNOSTICS = """
def run(intake, params):
    inventory = intake.get("viewport_layers", {})
    if "views" not in inventory and inventory.get("error"):
        raise ValueError(inventory["error"])
    affected, uncheckable = [], []
    checked = 0
    for handle, view in sorted(inventory.get("views", {}).items()):
        if "activation_error" in view:
            uncheckable.append({"viewport_handle": handle, "layout": view["layout"],
                                "reason": view["activation_error"]})
        elif view["enabled"]:
            checked += 1
            if "camera_error" in view:
                affected.append({"viewport_handle": handle, "layout": view["layout"],
                                 "reason": view["camera_error"]})
    if not checked and not uncheckable:
        raise ValueError("No active paper-space viewport cameras found.")
    return {"affected_cameras": affected,
            "checked_count": checked, "total": len(affected),
            "uncheckable_cameras": uncheckable, "uncheckable_count": len(uncheckable),
            "scope": "active paper-space viewport cameras"}, None
"""


_GEOMETRY = """
def cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def dot(left, right):
    return sum(a * b for a, b in zip(left, right))


def project_to_paper(crop, axes, point):
    # WCS to DCS is the inverse of the viewport's negative view twist about
    # its WCS target. DCS-to-paper then pans and scales around the view center.
    relative = tuple(value - target for value, target in zip(point, crop["target"]))
    x, y, depth = (dot(axis, relative) for axis in axes)
    if not all(map(math.isfinite, (x, y, depth))):
        raise ValueError("Viewport placement exceeds the numeric range.")
    front, back = crop["front"], crop["back"]
    if (front is not None and depth > front) or (back is not None and depth < back):
        return None
    eye_distance = crop["eye_distance"]
    if eye_distance is not None:
        distance = eye_distance - depth
        if distance <= max(1e-12 * eye_distance, math.ulp(eye_distance)):
            return None
        factor = eye_distance / distance
        x, y = x * factor, y * factor
    angle = crop["twist"]
    cosine, sine = math.cos(angle), math.sin(angle)
    dcs = (cosine * x - sine * y, sine * x + cosine * y)
    paper = tuple(center + (value - pan) * crop["scale"]
                  for center, value, pan in zip(crop["paper_center"], dcs, crop["view_center"]))
    if not all(map(math.isfinite, paper)):
        raise ValueError("Viewport placement exceeds the numeric range.")
    return paper


def has_projected_area(crop, frame, view_normal):
    a, b = frame[1:3]
    normal = (a[1] * b[2] - a[2] * b[1],
              a[2] * b[0] - a[0] * b[2],
              a[0] * b[1] - a[1] * b[0])
    magnitude = math.hypot(*normal)
    if not magnitude or not math.isfinite(magnitude):
        raise ValueError("Panel footprint has a degenerate placement.")
    if crop["eye_distance"] is not None:
        ray = tuple(target + direction - origin for target, direction, origin in
                    zip(crop["target"], crop["direction"], frame[0]))
        ray_length = math.hypot(*ray)
        if not math.isfinite(ray_length):
            raise ValueError("Viewport camera exceeds the numeric range.")
        if not ray_length:
            return False
        view_normal = tuple(component / ray_length for component in ray)
    return abs(dot(normal, view_normal)) > 16 * math.ulp(magnitude)


def crop_contains_footprint(crop, footprint, budget):
    boundary = crop["points"]
    budget[0] -= 2 * len(boundary) * len(footprint)
    if budget[0] < 0:
        raise ValueError("Viewport footprint containment exceeds the work limit.")
    if not all(clip_accepts(crop, point) for point in footprint):
        return False

    # In a concave crop, endpoints can both be inside while an edge crosses
    # a notch. Split each footprint edge at crop crossings and sample each span.
    boundary_edges = list(zip(boundary, boundary[1:] + boundary[:1]))
    for start, end in zip(footprint, footprint[1:] + footprint[:1]):
        edge = (end[0] - start[0], end[1] - start[1])
        cuts = {0.0, 1.0}
        for first, last in boundary_edges:
            other = (last[0] - first[0], last[1] - first[1])
            denominator = cross(edge, other)
            if denominator:
                delta = (first[0] - start[0], first[1] - start[1])
                t, u = cross(delta, other) / denominator, cross(delta, edge) / denominator
                if 0 < t < 1 and 0 <= u <= 1:
                    cuts.add(t)
        cuts = sorted(cuts)
        budget[0] -= (len(cuts) - 1) * len(boundary)
        if budget[0] < 0:
            raise ValueError("Viewport footprint containment exceeds the work limit.")
        for low, high in zip(cuts, cuts[1:]):
            midpoint = (low + high) / 2
            point = tuple(origin + midpoint * delta for origin, delta in zip(start, edge))
            if not clip_accepts(crop, point):
                return False
    return True
"""


_BODY = """
def measure_viewport(intake, selected):
    inventory = intake.get("viewport_layers", {})
    if inventory.get("error"):
        raise ValueError(inventory["error"])
    view = inventory.get("views", {}).get(selected)
    if not view or not view["enabled"]:
        raise ValueError("Missing or inactive paper-space viewport identity.")
    layers = inventory["layers"]
    if any(handle not in layers for handle in view["layers"]):
        raise ValueError("Unresolved viewport frozen-layer handle.")
    hidden = set(inventory["globally_hidden"])
    hidden.update(layers[handle] for handle in view["layers"])
    evidence = intake.get("block_visibility", {"definitions": {}})
    if evidence.get("error"):
        raise ValueError(evidence["error"])
    placement_filter = None
    scope = "panel placement layer visibility"
    if __CROP__:
        crop = view.get("crop")
        if not crop:
            raise ValueError(view.get("crop_error", "Missing viewport crop evidence."))
        axes = extrusion_axes(crop["direction"])
        budget = [2000000]
        def placement_filter(chain):
            frame = identity_frame()
            for ref, block, row, col in chain:
                frame = insert_frame(frame, ref, block.get("base", (0, 0, 0)), row, col)
            if __FOOTPRINT__:
                outline = intake.get("panel_footprints", {}).get(ref["name"].casefold(), {})
                if not outline.get("points") or not block.get("complete") or block.get("nested_inserts"):
                    raise ValueError(outline.get("error", "Missing complete panel footprint."))
                if not has_projected_area(crop, frame, axes[2]):
                    return False
                footprint = [project_to_paper(crop, axes, transform_point(frame, (*point, 0)))
                             for point in outline["points"]]
                return None not in footprint and crop_contains_footprint(crop, footprint, budget)
            anchor = transform_point(frame, block.get("base", (0, 0, 0)))
            paper = project_to_paper(crop, axes, anchor)
            return paper is not None and clip_accepts(crop, paper)
        scope = ("complete thawed panel footprints inside the viewport crop" if __FOOTPRINT__
                 else "thawed panel placement anchors inside the viewport crop")
    result = visible_instances(intake, evidence, hidden, set(layers.values()), placement_filter)
    return {**result, "viewport_handle": selected, "scope": scope}
"""


_SINGLE = """
def run(intake, params):
    bound = __HANDLE__
    selected = params.get("viewport_handle", bound)
    if not isinstance(selected, str) or not selected or (bound and selected.upper() != bound):
        raise ValueError("Missing or conflicting viewport identity.")
    return measure_viewport(intake, selected.upper()), None
"""


_SELECT_PAIR = """
def selected_viewports(params):
    bound = __HANDLES__
    selected = params.get("viewport_handles", bound)
    if ("viewport_handle" in params or not isinstance(selected, list) or len(selected) != 2
            or any(not isinstance(handle, str)
                   or not re.fullmatch(r"(?:0[xX])?[a-fA-F0-9]+", handle) for handle in selected)):
        raise ValueError("Viewport comparison requires two distinct viewport identities.")
    selected = [handle.upper().removeprefix("0X") for handle in selected]
    if len(set(selected)) != 2 or (bound and selected != bound):
        raise ValueError("Duplicate or conflicting viewport identities.")
    return selected
"""


_COMPARE = """
def run(intake, params):
    selected = selected_viewports(params)
    left_id, right_id = selected
    left, right = (measure_viewport(intake, handle) for handle in selected)
    left_paths, right_paths = set(left["visible_paths"]), set(right["visible_paths"])
    only_in = {left_id: sorted(left_paths - right_paths), right_id: sorted(right_paths - left_paths)}
    return {"viewport_handles": selected, "counts_by_viewport": {left_id: left["total"], right_id: right["total"]},
            "only_in": only_in, "shared_paths": sorted(left_paths & right_paths),
            "total": len(left_paths ^ right_paths), "scope": left["scope"]}, None
"""


_REVISION_COMPARE = """
def run(intake, params):
    binding = intake.get("linked_panel_revision") or {}
    prior = binding.get("prior_intake")
    previous, current = binding.get("prior_version"), binding.get("current_version")
    if (not binding.get("drawing_id") or not isinstance(prior, dict)
            or type(previous) is not int or type(current) is not int
            or not 0 < previous < current or binding.get("parent_version") != previous):
        raise ValueError("Two linked drawing revisions must be supplied by the version host.")
    selected = selected_viewports(params)
    changes = {}
    for handle in selected:
        before = measure_viewport(prior, handle)
        after = measure_viewport(intake, handle)
        old_paths, new_paths = set(before["visible_paths"]), set(after["visible_paths"])
        changes[handle] = {"prior_count": before["total"], "current_count": after["total"],
                           "added": sorted(new_paths - old_paths), "removed": sorted(old_paths - new_paths)}
    return {"viewport_handles": selected, "prior_version": previous, "current_version": current,
            "changes_by_viewport": changes,
            "total": sum(len(change["added"]) + len(change["removed"]) for change in changes.values()),
            "scope": after["scope"]}, None
"""
