"""Compare panel placements and group membership across bound DXF revisions."""
import re

from authored_block_transform import TRANSFORM_SOURCE


PARAMS = {
    "type": "object",
    "properties": {
        "floors": {
            "type": "array", "minItems": 1,
            "description": "Floor names and inclusive base elevations in the drawing's world Z units.",
            "items": {
                "type": "object", "required": ["name", "elevation"],
                "properties": {"name": {"type": "string"}, "elevation": {"type": "number"}},
                "additionalProperties": False,
            },
        },
    },
    "required": ["floors"],
    "additionalProperties": False,
}


def matches(description):
    return (floor_comparison_requested(description) or layer_comparison_requested(description)
            or group_comparison_requested(description))


def floor_comparison_requested(description):
    prompt = description.casefold()
    return all(re.search(pattern, prompt) for pattern in (
        r"\b(?:panels?|modules?)\b", r"\b(?:revisions?|versions?|parent drawing)\b",
        r"\b(?:floors?|levels?|storeys?|story|stories)\b", r"\b(?:mov\w*|chang\w*|relocat\w*)\b",
    ))


def layer_comparison_requested(description):
    return all(re.search(pattern, description.casefold()) for pattern in (
        r"\b(?:panels?|modules?)\b", r"\b(?:revisions?|versions?|parent drawing)\b",
        r"\blayers?\b", r"\b(?:mov\w*|relocat\w*)\b",
    ))


def group_comparison_requested(description):
    prompt = description.casefold()
    return (
        re.search(r"\b(?:revisions?|versions?|parent drawing)\b", prompt) is not None
        and re.search(r"\b(?:dxf|drawings?|panels?|modules?|physical totals)\b", prompt) is not None
        and re.search(r"\b(?:dxf groups?|group membership|membership churn)\b", prompt) is not None
        and re.search(r"\b(?:compar\w*|chang\w*|churn|add\w*|remov\w*|delta)\b", prompt) is not None
    )


def parameter_schema(description):
    schema = PARAMS if floor_comparison_requested(description) else {
        "type": "object", "properties": {}, "required": [], "additionalProperties": False,
    }
    if not group_comparison_requested(description):
        return schema
    return {**schema, "properties": {**schema["properties"], "group_name": {
        "type": "string", "minLength": 1,
        "description": "Compare a single saved DXF group by name. Omit to report all changed groups.",
    }}}


def bind_linked_dxfs(store, tenant_id, project_id, artifact_id, prior_version,
                     current_version, prior_bytes, current_bytes):
    """Bind bytes already obtained by an authorized host to its scoped ledger.

    This adapter performs no storage discovery. Callers supply the two files and
    their scope. The generated tool only compares snapshots available to it.
    """
    if any(type(v) is not int or v < 1 for v in (prior_version, current_version)):
        raise ValueError("Expected positive integer drawing versions.")
    scope = (tenant_id, project_id, artifact_id)
    store.verify_read(*scope, prior_version, prior_bytes)
    current = store.verify_read(*scope, current_version, current_bytes)
    if current.parent_version != prior_version:
        raise ValueError("Selected revisions must share a direct parent link.")
    return _bind_snapshots(artifact_id, prior_version, current_version, prior_bytes, current_bytes)


def bind_project_parent(tenant, context):
    """Resolve the selected publication's recorded parent inside its exact drawing."""
    from uuid import UUID

    import project_drawing_editor as editor
    import solar_project_context as project

    binding = context.binding
    proof = binding.version.provenance or {}
    if proof.get("schema") != "leaf.project-drawing-publication.v1":
        raise ValueError("The selected drawing version has no recorded parent publication.")
    try:
        parent_id = UUID(proof["parent_version_id"])
    except (KeyError, TypeError, AttributeError, ValueError):
        raise ValueError("The selected drawing version has no valid parent reference.") from None
    parent = project.graph_store().resolve_version_binding(
        binding.organization_id, binding.project_id, parent_id, drawing_id=binding.drawing_id)
    if not 0 < parent.version.seq < binding.version.seq:
        raise ValueError("The recorded parent must precede the selected drawing version.")
    scope = tenant, binding.project_id, binding.drawing_id
    prior_bytes, _ = editor.read_dxf(*scope, parent_id)
    current_bytes, _ = editor.read_dxf(*scope, binding.input_version_id)
    return _bind_snapshots(str(binding.drawing_id), parent.version.seq, binding.version.seq,
                           prior_bytes, current_bytes)


def _bind_snapshots(artifact_id, prior_version, current_version, prior_bytes, current_bytes):
    from dxf_intake import parse_dxf_bytes

    intake = parse_dxf_bytes(current_bytes)
    previous = parse_dxf_bytes(prior_bytes)
    if previous.get("insunits") != intake.get("insunits"):
        raise ValueError("Linked drawing versions must share insertion units for the floor schedule.")
    intake["linked_panel_revision"] = {
        "drawing_id": artifact_id, "prior_version": prior_version, "current_version": current_version,
        "parent_version": prior_version, "prior_intake": previous,
    }
    return intake


def build_body(description):
    groups = group_comparison_requested(description)
    floors = floor_comparison_requested(description)
    layer_moves = layer_comparison_requested(description)
    layers = layer_moves or (floors and bool(re.search(r"\blayers?\b", description.casefold())))
    physical_totals = floors or layer_moves or bool(re.search(
        r"\b(?:physical totals|(?:panel|module) (?:counts?|totals?)|"
        r"(?:count|how many)(?: \w+){0,3} (?:panels|modules))\b", description.casefold(),
    ))
    return (
        "import math\nimport re\n"
        f"COMPARE_GROUPS = {groups}\nCOMPARE_FLOORS = {floors}\nINCLUDE_LAYERS = {layers}\n"
        f"COMPARE_LAYER_MOVES = {layer_moves}\nCOUNT_PANELS = {physical_totals}\n"
        + TRANSFORM_SOURCE + _BODY
    )


_BODY = r"""
def floor_schedule(params):
    floors = params.get("floors")
    if not isinstance(floors, list) or not floors:
        raise ValueError("Supply the floor names and their world Z base elevations.")
    schedule = []
    names = set()
    elevations = set()
    for floor in floors:
        name, z = floor.get("name"), floor.get("elevation")
        if (not isinstance(name, str) or not name.strip() or name in names
                or type(z) not in (int, float) or not math.isfinite(z) or z in elevations):
            raise ValueError("Floor names and finite floor elevations must each be unique.")
        names.add(name)
        elevations.add(z)
        schedule.append((z, name))
    return sorted(schedule, reverse=True)


def panel_name(name):
    words = " ".join(re.findall(r"[a-z0-9]+", name.casefold()))
    return re.fullmatch(r"(?:(?:pv|solar|photovoltaic) )?(?:panels?|modules?)", words) is not None


def collect(drawing, drawing_id, version, floors):
    if any("unresolved block reference" in msg for msg in drawing.get("parseErrors", [])):
        raise ValueError("Incomplete block references for panel comparison.")
    if any(p.get("space") != "paper" and p.get("closed") and panel_name(p.get("layer", ""))
           for p in drawing.get("polylines", [])):
        raise ValueError("Panel outlines require block placement identities for this comparison.")
    blocks = {name.casefold(): block for name, block in drawing.get("blocks", {}).items()}
    records = {}
    seen = set()
    remaining = 100000
    stack = [(i, identity_frame(), "", ()) for i in drawing.get("inserts", []) if i.get("space") != "paper"]
    while stack:
        insert, parent, prefix, ancestors = stack.pop()
        name = insert.get("name", "").casefold()
        handle = insert.get("handle", "").upper()
        if not re.fullmatch(r"[A-F0-9]+", handle) or (prefix, handle) in seen:
            raise ValueError("Missing or ambiguous INSERT handle in the revision placement path.")
        seen.add((prefix, handle))
        block = blocks.get(name)
        if block is None or name in ancestors or block.get("nested_inserts_incomplete"):
            raise ValueError("Incomplete or cyclic panel block reference.")
        children = block.get("nested_inserts", [])
        if "nested_inserts" not in block and (block.get("count", 0) > len(block.get("children", []))
                or any(child.get("type") == "INSERT" for child in block.get("children", []))):
            raise ValueError("Incomplete nested panel reference identities.")
        base = block["base"]
        rows, cols = insert.get("rows", 1), insert.get("columns", 1)
        if type(rows) is not int or type(cols) is not int or min(rows, cols) < 1:
            raise ValueError("Invalid panel array size.")
        remaining -= rows * cols
        if remaining < 0:
            raise ValueError("Panel comparison exceeds the placement limit.")
        is_panel = panel_name(name)
        for row in range(rows):
            for col in range(cols):
                path = f"{prefix}{handle}[{row},{col}]"
                frame = insert_frame(parent, insert, base, row, col)
                if is_panel:
                    point = transform_point(frame, base)
                    if not all(math.isfinite(v) for v in point):
                        raise ValueError("Panel position must be finite.")
                    floor = next((name for z, name in floors or [] if point[2] >= z), None)
                    if floors is not None and floor is None:
                        raise ValueError("Panel falls below the lowest supplied floor.")
                    records[path] = {"drawing_id": drawing_id, "version": version, "path": path,
                                     "floor": floor, "position": list(point)}
                    if INCLUDE_LAYERS:
                        records[path]["layer"] = insert.get("layer") or "0"
                else:
                    if len(stack) + len(children) > remaining:
                        raise ValueError("Panel comparison exceeds the placement limit.")
                    stack.extend((child, frame, path + "/", ancestors + (name,)) for child in children)
    return records


def group_catalog(drawing):
    rows = {}
    for group in drawing.get("groups", []):
        name = group["name"]
        key = name.casefold()
        if not name or key in rows:
            raise ValueError("Missing or ambiguous DXF group name.")
        members = set(group["members"])
        if any(not re.fullmatch(r"[A-F0-9]+", handle) for handle in members):
            raise ValueError("Invalid saved DXF group member handle.")
        rows[key] = (name, members)
    return rows


def compare_groups(prior, current, name):
    old, new = group_catalog(prior), group_catalog(current)
    keys = old.keys() | new.keys()
    if name is not None:
        if not isinstance(name, str) or name.casefold() not in keys:
            raise ValueError("The named DXF group is absent from both revisions.")
        keys = {name.casefold()}
    changes = []
    for key in sorted(keys):
        group_name = (new.get(key) or old[key])[0]
        before = old[key][1] if key in old else set()
        after = new[key][1] if key in new else set()
        if name is not None or before != after or (key in old) != (key in new):
            changes.append({
                "name": group_name,
                "prior_present": key in old, "current_present": key in new,
                "counts": {"prior": len(before), "current": len(after)},
                "added": sorted(after - before), "removed": sorted(before - after),
                "retained": sorted(before & after),
            })
    return changes


def run(intake, params):
    binding = intake.get("linked_panel_revision") or {}
    prior = binding.get("prior_intake")
    previous, current = binding.get("prior_version"), binding.get("current_version")
    drawing_id = binding.get("drawing_id")
    if (not drawing_id or not isinstance(prior, dict) or type(previous) is not int
            or type(current) is not int or not 0 < previous < current
            or binding.get("parent_version") != previous):
        raise ValueError("Two linked drawing revisions must be supplied by the version host.")
    result = {}
    if COMPARE_GROUPS:
        result["group_membership"] = {
            "drawing_id": drawing_id, "prior_version": previous, "current_version": current,
            "groups": compare_groups(prior, intake, params.get("group_name")),
        }
    if not COUNT_PANELS:
        return result, None
    floors = floor_schedule(params) if COMPARE_FLOORS else None
    old = collect(prior, drawing_id, previous, floors)
    new = collect(intake, drawing_id, current, floors)
    moved = [{"prior": old[path], "current": new[path]} for path in sorted(old.keys() & new.keys())
             if old[path]["floor"] != new[path]["floor"] or (
                 COMPARE_LAYER_MOVES and old[path]["layer"] != new[path]["layer"]
                 and old[path]["position"] != new[path]["position"])]
    result.update({"counts": {"prior": len(old), "current": len(new)}, "moved": moved,
                   "added": [new[p] for p in sorted(new.keys() - old.keys())],
                   "removed": [old[p] for p in sorted(old.keys() - new.keys())]})
    return result, None
"""
