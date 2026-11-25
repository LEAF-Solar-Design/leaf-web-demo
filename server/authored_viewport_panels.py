"""Layer-visibility evidence from paper-space VIEWPORT entities and LAYER records."""
import math
import re

from authored_clipped_panels import CLIP_SOURCE
from authored_dynamic_visibility import INSTANCE_SOURCE, _records
from authored_panel_regions import _outline


def matches(description):
    return (re.search(r"\bviewports?\b", description, re.I)
            and re.search(r"\b(?:panels?|modules?)\b", description, re.I)
            and re.search(r"\b(?:frozen|thawed|layers?)\b", description, re.I)
            and re.search(r"\b(?:count|list|enumerate|tally|which|how many)\b", description, re.I))


def viewport_handle(description):
    match = re.search(r"\bviewport\s+(?:handle\s+)?(?:0x)?([a-f0-9]+)\b", description, re.I)
    return match[1].upper() if match else ""


def _crop(fields, boundaries):
    """Read the paper crop and model-to-paper mapping for an orthographic plan view."""
    flags = int(fields.get(90, 0))
    direction = tuple(float(fields.get(code, default))
                      for code, default in ((16, 0), (26, 0), (36, 1)))
    if (flags & 7 or direction[:2] != (0, 0) or not math.isfinite(direction[2])
            or direction[2] <= 0 or float(fields.get(51, 0)) != 0):
        raise ValueError("Viewport crop requires an untwisted orthographic top view.")
    cx, cy, width, height, view_height = (float(fields[code]) for code in (10, 20, 40, 41, 45))
    pan_x, pan_y, target_x, target_y = (float(fields.get(code, 0)) for code in (12, 22, 17, 27))
    if (not all(map(math.isfinite, (cx, cy, width, height, view_height, pan_x, pan_y, target_x, target_y)))
            or min(width, height, view_height) <= 0):
        raise ValueError("Invalid viewport dimensions or model view center.")
    scale = height / view_height
    offset = (cx - (pan_x + target_x) * scale, cy - (pan_y + target_y) * scale)
    if not math.isfinite(scale) or scale <= 0 or not all(map(math.isfinite, offset)):
        raise ValueError("Viewport projection exceeds the numeric range.")
    clip_handle = fields.get(340, "").upper()
    if clip_handle or flags & 65536:
        matches = boundaries.get(clip_handle, ())
        if len(matches) != 1 or not clip_handle:
            raise ValueError("Missing or ambiguous viewport crop boundary.")
        boundary = matches[0]
        if dict(boundary).get(410, "").casefold() != fields.get(410, "").casefold():
            raise ValueError("Viewport crop boundary belongs to a different layout.")
        if not 3 <= int(dict(boundary).get(90, 0)) <= 512:
            raise ValueError("Invalid or oversized viewport crop boundary.")
        polygon = _outline(boundary)
    else:
        polygon = [(cx + x * width / 2, cy + y * height / 2)
                   for x, y in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
    return {"points": polygon, "inverted": False, "scale": scale, "offset": offset}


def attach_viewports(pairs, out):
    if (0, "VIEWPORT") not in pairs:
        return
    section = table = ""
    layers, viewports = {}, {}
    crop_inputs, boundaries = {}, {}
    hidden = set()
    names = set()
    try:
        for kind, record in _records(pairs):
            fields = dict(record)
            if kind == "SECTION":
                section = fields.get(2, "")
            elif kind == "ENDSEC":
                section = table = ""
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
            elif kind == "VIEWPORT" and section == "ENTITIES":
                if fields.get(67) != "1" and fields.get(410, "Model").casefold() == "model":
                    continue
                handle = fields.get(5, "").upper()
                if not re.fullmatch("[A-F0-9]+", handle) or handle in viewports:
                    raise ValueError("Missing or duplicate viewport identity.")
                viewports[handle] = {"layers": [v.upper() for c, v in record if c == 331],
                                     "enabled": int(fields.get(68, 1)) != 0 and
                                     not int(fields.get(90, 0)) & 131072 and int(fields.get(69, 0)) != 1}
                if viewports[handle]["enabled"]:
                    crop_inputs[handle] = fields
            elif (kind == "LWPOLYLINE" and section == "ENTITIES"
                  and (fields.get(67) == "1" or fields.get(410, "Model").casefold() != "model")):
                boundaries.setdefault(fields.get(5, "").upper(), []).append(record)
        for handle, fields in crop_inputs.items():
            try:
                viewports[handle]["crop"] = _crop(fields, boundaries)
            except (KeyError, TypeError, ValueError) as exc:
                viewports[handle]["crop_error"] = "Unresolved viewport crop: " + str(exc)
        out["viewport_layers"] = {"layers": layers, "globally_hidden": sorted(hidden), "views": viewports}
    except (ValueError, TypeError) as exc:
        out["viewport_layers"] = {"error": str(exc)}


def build_body(description):
    crop = bool(re.search(r"\b(?:crops?|cropped|clip(?:ped|ping)?|boundar(?:y|ies))\b", description, re.I))
    return CLIP_SOURCE + INSTANCE_SOURCE + _BODY.replace(
        "__HANDLE__", repr(viewport_handle(description))).replace("__CROP__", repr(crop))


_BODY = """
def run(intake, params):
    bound = __HANDLE__
    selected = params.get("viewport_handle", bound)
    if not isinstance(selected, str) or not selected or (bound and selected.upper() != bound):
        raise ValueError("Missing or conflicting viewport identity.")
    selected = selected.upper()
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
        def placement_filter(chain):
            frame = identity_frame()
            for ref, block, row, col in chain[:-1]:
                frame = insert_frame(frame, ref, block.get("base", (0, 0, 0)), row, col)
            ref, _, row, col = chain[-1]
            x, y, _ = insert_frame(frame, ref, (0, 0, 0), row, col)[0]
            px = x * crop["scale"] + crop["offset"][0]
            py = y * crop["scale"] + crop["offset"][1]
            if not math.isfinite(px) or not math.isfinite(py):
                raise ValueError("Viewport placement exceeds the numeric range.")
            return clip_accepts(crop, (px, py))
        scope = "thawed panel placement anchors inside the viewport crop"
    result = visible_instances(intake, evidence, hidden, set(layers.values()), placement_filter)
    return {**result, "viewport_handle": selected, "scope": scope}, None
"""
