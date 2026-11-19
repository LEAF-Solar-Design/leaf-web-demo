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


def cutout_breakdown(description):
    return bool(re.search(r"\b(?:each|per|individual)\s+(?:overlapping\s+)?(?:cutouts?|exclusion loops?)\b",
                          description, re.I)
                and re.search(r"\bareas?\b", description, re.I))


def matches(description):
    if cutout_breakdown(description):
        return True
    return bool(re.search(r"\b(?:pv\s+)?(?:panels?|modules?)\b", description, re.I)
                and re.search(r"\b(?:xclip|clip(?:ped|ping)?s?|cutouts?|exclusion (?:loops?|boundaries))\b",
                              description, re.I)
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
    per_placement = bool(re.search(
        r"\b(?:each|per|every|individual)\s+(?:(?:pv|solar|photovoltaic)\s+)?(?:panels?|modules?)\b",
        description, re.I))
    return CLIP_SOURCE + VISIBILITY_SOURCE + (_AREA_SOURCE if area else "") + _TRAVERSAL.replace(
        "AREA_MODE", str(area)).replace("CUTOUT_BREAKDOWN", str(cutout_breakdown(description))).replace(
        "PER_PLACEMENT", str(per_placement)).replace("PANEL_PATTERN", repr(PANEL_NAME)).replace(
            "PLACEMENT_LIMIT", str(expansion_budget(description, 10000)))


_AREA_SOURCE = """
def visible_area(rings, breakdown):
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
    removed = {}
    for left, right in zip(cuts, cuts[1:]):
        # Adjacent floats have no representable interior x. Select edges by
        # the slab endpoints and integrate their linear height at both ends.
        hits = sorted(((ordinate(edge,left)+ordinate(edge,right))*0.5, edge[2])
                      for edge in edges if edge[0][0] <= left and right <= edge[1][0])
        flags = [inverted for _,inverted in rings]
        previous = None
        for y, index in hits:
            if previous is not None and all(flags):
                areas.append((y-previous)*(right-left))
            elif breakdown and previous is not None and y > previous:
                # Within the footprint and every retaining clip, the failed
                # inverted rings identify exactly which cutouts own this strip.
                if all(flags[i] or inverted for i, (_, inverted) in enumerate(rings)):
                    owners = tuple(i for i, flag in enumerate(flags) if not flag)
                    removed.setdefault(owners, []).append((y-previous)*(right-left))
            flags[index] = not flags[index]
            previous = y
    return math.fsum(areas), {owners: math.fsum(values) for owners,values in removed.items()}
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
    placement_areas = {}
    removed_groups = {}
    def children_for(ref, name, block):
        if re.fullmatch(PANEL_PATTERN, " ".join(re.findall(r"[a-z0-9]+", name))):
            return ()
        _, children = active_children(visibility or {"definitions": {}}, name,
                                      ref.get("handle", "").upper(), block.get("nested_inserts", []))
        return children

    expanded = preflight_expansion(intake.get("inserts", ()), blocks, PLACEMENT_LIMIT, children_for)
    def visit(ref, inherited, prefix, ancestors, parent, boundaries, area_scale):
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
                ax,ay,az = frame[1]
                bx,by,bz = frame[2]
                jacobian = math.hypot(ay*bz-az*by, az*bx-ax*bz, ax*by-ay*bx)
                if not jacobian or not math.isfinite(jacobian):
                    raise ValueError("Panel placement requires a nondegenerate surface transform.")
                if (not AREA_MODE and (az or bz)) or (
                        AREA_MODE and boundaries and (clip or panel) and (az or bz or frame[0][2])):
                    raise ValueError("Panel and clip planes are not coplanar.")
                scale = area_scale
                if AREA_MODE and not boundaries and (clip or panel):
                    # Measure in the first shared clip frame to retain local
                    # detail even when the site has large world coordinates.
                    scale = jacobian
                    frame = identity_frame()
                chain = boundaries
                if clip:
                    pts = [transform_point(frame, (*p,0))[:2] for p in clip["points"]]
                    chain = (*chain, {"points":pts, "inverted":clip["inverted"], "path":path})
                if panel and AREA_MODE:
                    outline = intake.get("panel_footprints", {}).get(name, {})
                    if not outline.get("points") or not block.get("complete") or children:
                        raise ValueError(outline.get("error","Missing complete panel footprint."))
                    polygon = [transform_point(frame, (*p,0))[:2] for p in outline["points"]]
                    rings = [(polygon,False)] + [(c["points"],c["inverted"]) for c in chain]
                    area, removed = visible_area(rings, CUTOUT_BREAKDOWN)
                    area *= scale
                    for indices, value in removed.items():
                        owners = tuple(sorted(chain[i-1]["path"] for i in indices))
                        total = removed_groups.get(owners, 0) + value*scale
                        if not math.isfinite(total):
                            raise ValueError("Removed panel area exceeds numeric range.")
                        removed_groups[owners] = total
                    if not math.isfinite(area):
                        raise ValueError("Panel footprint area exceeds numeric range.")
                    if PER_PLACEMENT:
                        if path in placement_areas:
                            raise ValueError("Duplicate panel placement path.")
                        placement_areas[path] = area
                    if area > 0:
                        areas[layer] = areas.get(layer,0) + area
                    else:
                        continue
                elif panel:
                    origin = transform_point(frame, base)
                    if not all(clip_accepts(c,origin) for c in chain):
                        continue
                else:
                    for child in children:
                        visit(child,layer,path+"/",ancestors+(name,),frame,chain,scale)
                    continue
                if path in paths:
                    raise ValueError("Duplicate panel placement path.")
                paths.add(path)
    for ref in intake.get("inserts", []):
        visit(ref,"0","",(),identity_frame(),(),1)
    if AREA_MODE:
        details = {"area_by_placement": placement_areas} if PER_PLACEMENT else {}
        if CUTOUT_BREAKDOWN:
            if not intake.get("panel_footprints"):
                raise ValueError("A cutout breakdown requires panel footprint evidence.")
            details["removed_area_groups"] = [{"cutout_paths": list(owners), "area": value}
                                              for owners,value in sorted(removed_groups.items())]
            details["removed_area_basis"] = "panel footprints; shared area is reported once for the containing cutouts"
        return {**details, "area_by_layer":areas, "expanded_placements":expanded, "area_units":"square drawing units", "visible_panels":len(paths),
                "boundary_policy":"edge or corner contact contributes zero footprint area"}, None
    return {"visible_paths":sorted(paths), "expanded_placements":expanded, "total":len(paths),
            "boundary_policy":"include panel insertion points on clip edges"}, None
"""
