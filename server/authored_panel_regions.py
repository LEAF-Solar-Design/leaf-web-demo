"""Panel area and state-aware placement queries over nested planar block clips."""
import math
import re

from authored_clipped_panels import CLIP_SOURCE, expansion_budget
from authored_dynamic_visibility import VISIBILITY_SOURCE
from dxf_spatial_clips import _polygon


def operation(description):
    if re.search(r"\b(?:area|areas|square|footprints?)\b", description, re.I):
        return "clipped_panel_area"
    return "state_clipped_panels"


def matches(description):
    return bool(re.search(r"\b(?:pv\s+)?(?:panels?|modules?)\b", description, re.I)
                and re.search(r"\b(?:xclip|clip(?:ped|ping)?s?)\b", description, re.I)
                and re.search(r"\b(?:areas?|square|footprints?|states?|overrides?|dynamic|visibility)\b",
                              description, re.I))


PANEL_NAME = r"(?:(?:pv|solar|photovoltaic) )?(?:panels?|modules?)"


def _outline(tags):
    groups = dict(tags)
    if (not int(groups.get(70, 0)) & 1 or
            any(float(v) != 0 for c, v in tags if c in (38, 39, 40, 41, 42, 43, 210, 220)) or
            float(groups.get(230, 1)) != 1):
        raise ValueError("A closed, straight, planar panel footprint is required.")
    points = []
    for c, v in tags:
        if c == 10:
            points.append([float(v)])
        elif c == 20:
            if not points or len(points[-1]) != 1:
                raise ValueError("Incomplete footprint coordinate.")
            points[-1].append(float(v))
    if (len(points) != int(groups.get(90, -1)) or len(points) < 3 or
            any(len(p) != 2 or not all(map(math.isfinite, p)) for p in points)):
        raise ValueError("Invalid panel footprint vertices.")
    return _polygon(points, (1, 0, 0, 0, 0, 1, 0, 0))


def attach_footprints(pairs, out):
    # Keep the unrounded outline, because legacy block preview coordinates
    # omit bulges and widths and have only millimetric drawing-unit precision.
    blocks = out.get("blocks", {})
    panels = {name.casefold() for name in blocks if re.fullmatch(
        PANEL_NAME, " ".join(re.findall(r"[a-z0-9]+", name.casefold())))}
    if not panels or (0, "LWPOLYLINE") not in pairs:
        return
    footprints = {}
    name = None
    i = 0
    while i < len(pairs):
        j = i + 1
        while j < len(pairs) and pairs[j][0] != 0:
            j += 1
        if pairs[i] == (0, "BLOCK"):
            name = next((v.casefold() for c,v in pairs[i+1:j] if c == 2), None)
        elif pairs[i] == (0, "ENDBLK"):
            name = None
        elif name in panels and pairs[i] == (0, "LWPOLYLINE"):
            try:
                if name in footprints:
                    raise ValueError("Ambiguous panel footprint outlines.")
                footprints[name] = {"points": _outline(pairs[i+1:j])}
            except (ValueError, TypeError, OverflowError) as exc:
                footprints[name] = {"error": str(exc)}
        i = j
    if footprints:
        out["panel_footprints"] = footprints


def build_body(description):
    area = operation(description) == "clipped_panel_area"
    return CLIP_SOURCE + VISIBILITY_SOURCE + (_AREA_SOURCE if area else "") + _TRAVERSAL.replace(
        "AREA_MODE", str(area)).replace("PANEL_PATTERN", repr(PANEL_NAME)).replace(
            "PLACEMENT_LIMIT", str(expansion_budget(description, 10000)))


_AREA_SOURCE = """
def visible_area(rings):
    # Each vertical slab has a fixed order of polygon edges between successive
    # vertex and intersection x coordinates. Its occupied height is linear, so
    # a midpoint slice integrates the slab exactly apart from floating point.
    xs = set()
    edges = []
    for index, (points, inverted) in enumerate(rings):
        xs.update(p[0] for p in points)
        for p, q in zip(points, points[1:]+points[:1]):
            if p[0] != q[0]:
                left, right = sorted((p,q))
                edges.append((left, right, index))
    if len(edges) > 256:
        raise ValueError("Panel and nested clip boundaries exceed supported edge limit.")
    def ordinate(edge, x):
        a,b,_ = edge
        return a[1]+(x-a[0])/(b[0]-a[0])*(b[1]-a[1])
    for i, edge in enumerate(edges):
        for other in edges[:i]:
            low, high = max(edge[0][0], other[0][0]), min(edge[1][0], other[1][0])
            if low < high:
                dy0 = ordinate(edge,low)-ordinate(other,low)
                dy1 = ordinate(edge,high)-ordinate(other,high)
                if (dy0 < 0 < dy1) or (dy1 < 0 < dy0):
                    xs.add(low+(high-low)*(dy0/(dy0-dy1)))
    cuts = sorted(xs)
    areas = []
    for left, right in zip(cuts, cuts[1:]):
        x = left + (right-left)*0.5
        hits = sorted((ordinate(edge,x), edge[2]) for edge in edges if edge[0][0] < x < edge[1][0])
        flags = [inverted for _,inverted in rings]
        previous = None
        for y, index in hits:
            if previous is not None and all(flags):
                areas.append((y-previous)*(right-left))
            flags[index] = not flags[index]
            previous = y
    return math.fsum(areas)
"""


_TRAVERSAL = """
def run(intake, params):
    clips = intake.get("insert_clips")
    visibility = intake.get("block_visibility", {})
    if clips is None and intake.get("inserts"):
        raise ValueError("Missing spatial clip evidence.")
    if visibility.get("error") or (not AREA_MODE and not visibility.get("definitions")):
        raise ValueError("Missing or unresolved block visibility settings.")
    if any("unresolved block reference" in msg for msg in intake.get("parseErrors", [])):
        raise ValueError("Unresolved block reference.")
    blocks = {n.casefold(): b for n,b in intake.get("blocks", {}).items()}
    paths, areas = set(), {}
    def children_for(ref, name, block):
        if re.fullmatch(PANEL_PATTERN, " ".join(re.findall(r"[a-z0-9]+", name))):
            return ()
        _, children = active_children(visibility or {"definitions": {}}, name,
                                      ref.get("handle", "").upper(), block.get("nested_inserts", []))
        return children

    expanded = preflight_expansion(intake.get("inserts", ()), blocks, PLACEMENT_LIMIT, children_for)
    def visit(ref, inherited, prefix, ancestors, parent, boundaries):
        if ref.get("space") == "paper":
            return
        handle = ref.get("handle", "").upper()
        name = ref["name"].casefold()
        block = blocks.get(name)
        if (not re.fullmatch(r"[A-F0-9]+", handle) or not block or block.get("nested_inserts_incomplete")
                or name in ancestors or len(ancestors) >= 32):
            raise ValueError("Invalid or incomplete nested panel reference.")
        layer = ref.get("layer", "0")
        layer = inherited if layer == "0" else layer
        children = block.get("nested_inserts", [])
        definition, children = active_children(visibility or {"definitions":{}}, name, handle, children)
        panel = re.fullmatch(PANEL_PATTERN, " ".join(re.findall(r"[a-z0-9]+",name)))
        if definition and panel:
            raise ValueError("Active geometry inside a panel definition is unresolved.")
        rows, cols = ref.get("rows",1),ref.get("columns",1)
        clip = clips.get(handle)
        if clip and "error" in clip:
            raise ValueError(clip["error"])
        base = block.get("base", (0,0,0))
        for row in range(rows):
            for col in range(cols):
                path = prefix + f"{handle}[{row},{col}]"
                frame = insert_frame(parent, ref, base, row, col)
                if (frame[1][2] or frame[2][2] or
                        frame[1][0]*frame[2][1] == frame[2][0]*frame[1][1]):
                    raise ValueError("Panel placement requires a nondegenerate XY transform.")
                chain = boundaries
                if clip:
                    pts = [transform_point(frame, (*p,0))[:2] for p in clip["points"]]
                    chain = (*chain, {"points":pts, "inverted":clip["inverted"]})
                if panel and AREA_MODE:
                    outline = intake.get("panel_footprints", {}).get(name, {})
                    if not outline.get("points") or not block.get("complete") or children:
                        raise ValueError(outline.get("error","Missing complete panel footprint."))
                    polygon = [transform_point(frame, (*p,0))[:2] for p in outline["points"]]
                    rings = [(polygon,False)] + [(c["points"],c["inverted"]) for c in chain]
                    area = visible_area(rings)
                    width = max(p[0] for p in polygon)-min(p[0] for p in polygon)
                    height = max(p[1] for p in polygon)-min(p[1] for p in polygon)
                    roundoff = 16 * max(math.ulp(v) for p in polygon for v in p) * (width+height)
                    if area > roundoff:
                        areas[layer] = areas.get(layer,0) + area
                    else:
                        continue
                elif panel:
                    origin = transform_point(frame, base)
                    if not all(clip_accepts(c,origin) for c in chain):
                        continue
                else:
                    for child in children:
                        visit(child,layer,path+"/",ancestors+(name,),frame,chain)
                    continue
                if path in paths:
                    raise ValueError("Duplicate panel placement path.")
                paths.add(path)
    for ref in intake.get("inserts", []):
        visit(ref,"0","",(),identity_frame(),())
    if AREA_MODE:
        return {"area_by_layer":areas, "expanded_placements":expanded, "area_units":"square drawing units", "visible_panels":len(paths),
                "boundary_policy":"edge contact contributes zero footprint area"}, None
    return {"visible_paths":sorted(paths), "expanded_placements":expanded, "total":len(paths),
            "boundary_policy":"include panel insertion points on clip edges"}, None
"""
