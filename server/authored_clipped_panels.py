"""Generate the offline tool for clip-aware counts of nested panel INSERT origins."""
import re

from authored_block_transform import TRANSFORM_SOURCE


def matches(description):
    return bool(re.search(r"\b(?:xclip|clip(?:ped|ping)?s?)\b", description, re.I)
                and re.search(r"\b(?:panels?|modules?)\b", description, re.I))


def build_body(description):
    return _BODY


CLIP_SOURCE = """
import re
import math
""" + TRANSFORM_SOURCE + """

def clip_accepts(clip, point):
    inside = False
    x, y = point[:2]
    polygon = clip["points"]
    for i, (a, b) in enumerate(polygon):
        c, d = polygon[i-1]
        dx, dy = c-a, d-b
        # Relative to edge length and coordinate roundoff, including both
        # standard and inverted boundary points.
        epsilon = 8 * max(math.ulp(v) for v in (a,b,c,d,x,y))
        if (abs((x-a)*dy - (y-b)*dx) <= epsilon * math.hypot(dx, dy)
                and min(a,c)-epsilon <= x <= max(a,c)+epsilon
                and min(b,d)-epsilon <= y <= max(b,d)+epsilon):
            return True
        if (b > y) != (d > y) and x < a + (y-b)*dx/dy:
            inside = not inside
    return inside != clip["inverted"]

"""


_BODY = CLIP_SOURCE + """
def run(intake, params):
    counts = {}
    clips = intake.get("insert_clips")
    if intake.get("inserts") and clips is None:
        raise ValueError("Missing DXF clip evidence for panel references.")
    visibility = intake.get("block_visibility", {})
    if visibility.get("error"):
        raise ValueError("Unresolved visibility state for clipped panel references.")
    blocks = {name.casefold(): block for name, block in intake.get("blocks", {}).items()}
    visited = 0

    def visit(insert, inherited, ancestors, placements):
        nonlocal visited
        if insert.get("space") == "paper":
            return
        name = insert["name"].casefold()
        layer = insert.get("layer", "0")
        if layer == "0":
            layer = inherited
        if name in visibility.get("definitions", {}):
            raise ValueError("Combined dynamic visibility and spatial clip counts require resolved joint evidence.")
        block = blocks.get(name)
        if block is None or block.get("nested_inserts_incomplete"):
            raise ValueError("Incomplete block catalogue for clipped panel count.")
        if name in ancestors or len(ancestors) >= 32:
            raise ValueError("Cyclic or deeply nested clipped block reference.")
        clip = clips.get((insert.get("handle") or "").upper())
        if clip and clip.get("error"):
            raise ValueError(clip["error"])
        base = block.get("base", (0,0,0))
        words = " ".join(re.findall(r"[a-z0-9]+", name))
        panel = re.fullmatch(r"(?:(?:pv|solar|photovoltaic) )?(?:panels?|modules?)", words)
        rows, cols = insert.get("rows", 1), insert.get("columns", 1)
        if min(rows, cols) < 1 or visited + rows*cols > 100000:
            raise ValueError("Clipped block array exceeds the panel count limit.")
        for row in range(rows):
            for col in range(cols):
                visited += 1
                if visited > 100000:
                    raise ValueError("Clipped block traversal exceeds the panel count limit.")
                frame = insert_frame(identity_frame(), insert, base, row, col)
                chain = (*placements, (clip, frame))
                if panel:
                    point = base
                    for boundary, local_to_parent in reversed(chain):
                        if boundary and not clip_accepts(boundary, point):
                            break
                        point = transform_point(local_to_parent, point)
                    else:
                        counts[layer] = counts.get(layer, 0) + 1
                else:
                    for child in block.get("nested_inserts", []):
                        visit(child, layer, (*ancestors, name), chain)
    for insert in intake.get("inserts", []):
        visit(insert, "0", (), ())
    return ({"total": sum(counts.values()), "counts_by_layer": counts,
             "boundary_policy": "include panel insertion points on clip edges"}, None)
"""
