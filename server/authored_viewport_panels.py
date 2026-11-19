"""Layer-visibility evidence from paper-space VIEWPORT entities and LAYER records."""
import re

from authored_dynamic_visibility import INSTANCE_SOURCE, _records


def matches(description):
    return (re.search(r"\bviewports?\b", description, re.I)
            and re.search(r"\b(?:panels?|modules?)\b", description, re.I)
            and re.search(r"\b(?:frozen|thawed|layers?)\b", description, re.I)
            and re.search(r"\b(?:count|list|enumerate|tally|which|how many)\b", description, re.I))


def viewport_handle(description):
    match = re.search(r"\bviewport\s+(?:handle\s+)?(?:0x)?([a-f0-9]+)\b", description, re.I)
    return match[1].upper() if match else ""


def attach_viewports(pairs, out):
    if (0, "VIEWPORT") not in pairs:
        return
    section = table = ""
    layers, viewports = {}, {}
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
        out["viewport_layers"] = {"layers": layers, "globally_hidden": sorted(hidden), "views": viewports}
    except (ValueError, TypeError) as exc:
        out["viewport_layers"] = {"error": str(exc)}


def build_body(description):
    return INSTANCE_SOURCE + _BODY.replace("__HANDLE__", repr(viewport_handle(description)))


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
    result = visible_instances(intake, evidence, hidden, set(layers.values()))
    return {**result, "viewport_handle": selected, "scope": "panel placement layer visibility"}, None
"""
