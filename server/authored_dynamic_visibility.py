"""Offline visibility-state reporting for explicitly labelled dynamic INSERTs."""
import re


def matches(description):
    prompt = description.casefold()
    return bool(re.search(r"\b(?:panels?|modules?)\b", prompt)
                and re.search(r"\b(?:visib\w*|shown|hidden)\b", prompt)
                and re.search(r"\b(?:state|states|dynamic|overrides?|settings?)\b", prompt))


def _records(pairs):
    start = 0
    while start < len(pairs):
        end = start + 1
        while end < len(pairs) and pairs[end][0] != 0:
            end += 1
        if pairs[start][0] == 0:
            yield pairs[start][1], pairs[start + 1:end]
        start = end


def _parameter(record):
    # LibreDWG, BLOCKVISIBILITYPARAMETER: 301 labels the property, 331
    # identifies the controlled entities, 303/94/332 describe each state.
    def count(fields, code, expected):
        values = [v for k, v in fields if k == code]
        if len(values) != 1 or int(values[0]) != expected:
            raise ValueError("Invalid visibility table count.")

    labels = [i for i, (code, _) in enumerate(record) if code == 301]
    if len(labels) != 1:
        raise ValueError("Ambiguous visibility selector.")
    record = record[labels[0]:]
    tag = record[0][1].casefold()
    offsets = [i for i, (code, _) in enumerate(record) if code == 303]
    if not offsets or not tag:
        raise ValueError("Missing visibility states.")
    header = record[:offsets[0]]
    controlled = [v.upper() for k, v in header if k == 331]
    count(header, 93, len(controlled))
    count(header, 92, len(offsets))
    if not controlled or len(set(controlled)) != len(controlled):
        raise ValueError("Missing or duplicate visibility members.")
    states = {}
    for start, end in zip(offsets, offsets[1:] + [len(record)]):
        entry = record[start:end]
        label = entry[0][1].casefold()
        visible = [v.upper() for k, v in entry if k == 332]
        count(entry, 94, len(visible))
        if (not label or label in states or len(visible) != len(set(visible))
                or not set(visible).issubset(controlled)):
            raise ValueError("Ambiguous visibility state members.")
        states[label] = visible
    return tag, controlled, states


def attach_visibility(pairs, out):
    """Preserve explicit state evidence without impacting unrelated DXF reads."""
    if (0, "BLOCKVISIBILITYPARAMETER") not in pairs:
        return
    try:
        out["block_visibility"] = _read_visibility(pairs)
    except (ValueError, KeyError) as exc:
        out["block_visibility"] = {"error": "Unresolved block visibility evidence: " + str(exc)}


def _read_visibility(pairs):
    section = block = owner = None
    owners, defaults, overrides, parameters = {}, {}, {}, []
    for kind, record in _records(pairs):
        field = dict(record)
        if kind == "SECTION":
            section = field.get(2)
        elif kind == "ENDSEC":
            section = block = None
        elif section == "BLOCKS" and kind == "BLOCK":
            block = field.get(2, "").casefold()
        elif kind == "ENDBLK":
            block = None
        elif section == "OBJECTS" and kind == "BLOCKVISIBILITYPARAMETER":
            parameters.append(_parameter(record))
        handle = field.get(5, "").upper()
        if block and handle and kind not in ("BLOCK", "ENDBLK"):
            if handle in owners:
                raise ValueError("Duplicate block entity handle.")
            owners[handle] = block
        if section not in ("BLOCKS", "ENTITIES"):
            continue
        tag = field.get(2, "").casefold()
        if kind == "ATTDEF" and block:
            defaults.setdefault((block, tag), []).append(field.get(1, "").casefold())
        if kind == "INSERT":
            owner = handle if field.get(66) == "1" else None
        elif kind == "ATTRIB" and owner:
            if field.get(330, owner).upper() != owner:
                raise ValueError("Visibility override owner mismatch.")
            overrides.setdefault(owner, {}).setdefault(tag, []).append(field.get(1, "").casefold())
        else:
            owner = None
    definitions = {}
    for tag, controlled, states in parameters:
        names = {owners.get(h) for h in controlled}
        if None in names or len(names) != 1:
            raise ValueError("Visibility members have unresolved block ownership.")
        block = names.pop()
        labels = defaults.get((block, tag), [])
        if block in definitions or len(labels) != 1 or labels[0] not in states:
            raise ValueError("Missing or ambiguous explicit visibility default.")
        definitions[block] = {"tag": tag, "controlled": controlled, "states": states, "default": labels[0]}
    return {"definitions": definitions, "overrides": overrides}


def build_body(description):
    return _BODY


VISIBILITY_SOURCE = r"""
import re


def active_children(evidence, name, handle, children):
    definitions = evidence["definitions"]
    definition = definitions.get(name)
    if name.startswith("*u") and not definition:
        raise ValueError("Unresolved anonymous dynamic block visibility state.")
    if definition:
        controlled = set(definition["controlled"])
        if any(child.get("handle", "").upper() not in controlled for child in children):
            raise ValueError("Partial visibility coverage for nested INSERTs.")
        overrides = evidence["overrides"].get(handle, {}).get(definition["tag"], [])
        if len(overrides) > 1:
            raise ValueError("Ambiguous visibility override.")
        state = overrides[0] if overrides else definition["default"]
        if state not in definition["states"]:
            raise ValueError("Unresolved visibility state " + state)
        visible = set(definition["states"][state])
        children = [child for child in children if child["handle"].upper() in visible]
    return definition, children
"""


INSTANCE_SOURCE = VISIBILITY_SOURCE + r"""


def visible_instances(intake, evidence, hidden_layers=None, known_layers=None):
    if any("unresolved block reference" in msg for msg in intake.get("parseErrors", [])):
        raise ValueError("Unresolved INSERT in visibility traversal.")
    blocks = {name.casefold(): definition for name, definition in intake.get("blocks", {}).items()}
    visible_paths = []
    visited = 0

    def visit(ref, prefix, ancestors, parent_layer):
        layer = ref.get("layer", "0").casefold()
        layer = parent_layer if layer == "0" else layer
        if known_layers is not None and layer not in known_layers:
            raise ValueError("Unresolved placement layer in viewport.")
        if hidden_layers is not None and layer in hidden_layers:
            return
        nonlocal visited
        name = ref.get("name", "").casefold()
        block = blocks.get(name)
        handle = ref.get("handle", "").upper()
        if handle in intake.get("insert_clips", {}):
            raise ValueError("Combined visibility and active clipping is unsupported for INSERT " + handle)
        if (not re.fullmatch(r"[A-F0-9]+", handle) or not block or name in ancestors
                or len(ancestors) >= 32 or block.get("nested_inserts_incomplete")):
            raise ValueError("Incomplete or cyclic visibility instance path.")
        children = block.get("nested_inserts", [])
        if "nested_inserts" not in block and (block.get("count", 0) > len(block.get("children", []))
                or any(child.get("type") == "INSERT" for child in block.get("children", []))):
            raise ValueError("Missing nested visibility references.")
        definition, children = active_children(evidence, name, handle, children)
        rows, cols = ref.get("rows", 1), ref.get("columns", 1)
        if type(rows) is not int or type(cols) is not int or min(rows, cols) < 1:
            raise ValueError("Invalid visibility array multiplicity.")
        visited += rows * cols
        if visited > 10000:
            raise ValueError("Visibility traversal exceeds 10000 placements.")
        words = " ".join(re.findall(r"[a-z0-9]+", name))
        module = re.fullmatch(r"(?:(?:pv|solar|photovoltaic) )?(?:panels?|modules?)", words)
        if definition and module:
            raise ValueError("Visibility inside a module requires active geometry evidence.")
        for row in range(rows):
            for col in range(cols):
                path = prefix + f"{handle}[{row},{col}]"
                if module:
                    visible_paths.append(path)
                else:
                    for child in children:
                        visit(child, path + "/", ancestors + (name,), layer)

    for ref in intake.get("inserts", []):
        if ref.get("space") != "paper":
            visit(ref, "", (), "0")
    if len(set(visible_paths)) != len(visible_paths):
        raise ValueError("Duplicate visibility placement paths.")
    return {"total": len(visible_paths), "visible_paths": sorted(visible_paths)}
"""


_BODY = INSTANCE_SOURCE + """
def run(intake, params):
    evidence = intake.get("block_visibility", {})
    if not evidence.get("definitions"):
        raise ValueError(evidence.get("error", "Missing block visibility evidence."))
    return visible_instances(intake, evidence), None
"""
