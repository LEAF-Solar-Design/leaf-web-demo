"""Generate the offline tool for clip-aware counts of nested panel INSERT origins."""
import re

from authored_block_transform import TRANSFORM_SOURCE


def matches(description):
    return bool(re.search(r"\b(?:xclip|clip(?:ped|ping)?s?)\b", description, re.I)
                and re.search(r"\b(?:panels?|modules?)\b", description, re.I))


def expansion_budget(description, maximum):
    match = re.search(r"\b(?:budget(?: of)?|at most|limit(?: expansion)? to)\s+(\d+)\s+placements?\b",
                      description, re.I)
    return min(int(match[1]), maximum) if match else maximum


def build_body(description):
    return _BODY.replace("PLACEMENT_LIMIT", str(expansion_budget(description, 100000)))


CLIP_SOURCE = """
import re
import math
""" + TRANSFORM_SOURCE + """

def preflight_expansion(roots, blocks, limit, children_for):
    # Count INSERT occurrences before expanding any array.
    memo = {}

    def checked(count):
        if count > limit:
            raise ValueError(f"Clipped block expansion exceeds the expansion budget of {limit} placements.")
        return count

    def cost(ref, ancestors):
        if ref.get("space") == "paper":
            return 0
        name = ref["name"].casefold()
        block = blocks.get(name)
        if block is None or block.get("nested_inserts_incomplete"):
            raise ValueError("Incomplete nested panel reference.")
        if name in ancestors or len(ancestors) >= 32:
            raise ValueError("Cyclic or deeply nested clipped block reference.")
        rows, cols = ref.get("rows", 1), ref.get("columns", 1)
        if type(rows) is not int or type(cols) is not int or min(rows, cols) < 1:
            raise ValueError("Invalid clipped reference array size.")
        copies = checked(rows * cols)
        # State overrides belong to the instance's source handle.
        key = (name, ref.get("handle"), len(ancestors))
        if key not in memo:
            subtotal = 1
            for child in children_for(ref, name, block):
                subtotal = checked(subtotal + cost(child, (*ancestors, name)))
            memo[key] = subtotal
        return checked(copies * memo[key])

    total = 0
    for root in roots:
        total = checked(total + cost(root, ()))
    return total


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
    def is_panel(name):
        words = " ".join(re.findall(r"[a-z0-9]+", name))
        return re.fullmatch(r"(?:(?:pv|solar|photovoltaic) )?(?:panels?|modules?)", words)

    def children(ref, name, block):
        return () if is_panel(name) else block.get("nested_inserts", ())

    expanded = preflight_expansion(intake.get("inserts", ()), blocks, PLACEMENT_LIMIT, children)

    def visit(insert, inherited, ancestors, placements):
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
        panel = is_panel(name)
        rows, cols = insert.get("rows", 1), insert.get("columns", 1)
        for row in range(rows):
            for col in range(cols):
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
    return ({"total": sum(counts.values()), "counts_by_layer": counts, "expanded_placements": expanded,
             "boundary_policy": "include panel insertion points on clip edges"}, None)
"""
