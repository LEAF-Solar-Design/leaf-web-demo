"""Author a read-only entity subtotal for a requested family of layer names."""
import re


_SUBTOTAL = re.compile(
    r"(?:show(?: me)?|list|give me) (?:just |only |the )*(?P<family>[\w-]+) layers"
    r",? (?:with )?(?:one |a )?subtotal per layer[.!]?",
    re.IGNORECASE,
)
_COUNT = re.compile(
    r"(?:count|tally) (?:entities|objects) on (?:each |every |the )?"
    r"(?P<family>[\w-]+) (?P<scope>layers?)[.!]?",
    re.IGNORECASE,
)


_COMPARISON = re.compile(
    r"(?:show(?: me)?|list|give me|compare) (?:separate )?"
    r"(?P<family>[\w-]+(?:, [\w-]+){0,2},? and [\w-]+) (?:layer )?subtotals"
    r"(?:,? and (?:flag|show|list) (?:any )?(?:overlapping layers|"
    r"layers? (?:whose names? )?(?:that )?match(?:es)? (?P<both>both)))?[.!]?",
    re.IGNORECASE,
)


def requested_family(description):
    request = " ".join(description.split())
    match = (_SUBTOTAL.fullmatch(request) or _COUNT.fullmatch(request)
             or _COMPARISON.fullmatch(request))
    if match:
        family = match["family"].casefold()
        # The generic author resolves a singular active or current layer at run time.
        if match.re is _COUNT and match["scope"].casefold() == "layer" and family in {"active", "current"}:
            return None
        if match.groupdict().get("both") and "," in family:
            return None
        return family


def build_body(family):
    families = tuple(dict.fromkeys(re.split(r",? and |, ", family)))
    return _BODY.replace("__FAMILIES__", repr(families))


_BODY = r'''
import re


def run(intake, params):
    families = __FAMILIES__

    def name_forms(family):
        forms = {family, family + "s"}
        if family.endswith("s"):
            forms.add(family[:-1])
        return forms

    forms_by_family = {family: name_forms(family) for family in families}

    def matching_families(layer):
        words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", layer)
        tokens = set(re.findall(r"[^\W_]+", words.casefold()))
        tokens.add(layer.casefold())
        return [family for family, forms in forms_by_family.items() if tokens & forms]

    observed = {}
    handle_counts = {}
    for row in intake.get("layers") or []:
        layer = row.get("name") if isinstance(row, dict) else row
        if isinstance(layer, str):
            observed[layer] = []
    for key in ("polylines", "inserts", "faces3d"):
        for entity in intake.get(key) or []:
            layer = entity.get("layer", "0")
            handle = entity.get("handle")
            # Intake uses L-prefixed synthetic IDs when a DXF handle is absent.
            if isinstance(handle, str) and re.fullmatch(r"[0-9A-Fa-f]+", handle):
                handle = handle.upper()
                handle_counts[handle] = handle_counts.get(handle, 0) + 1
            else:
                handle = None
            if isinstance(layer, str):
                observed.setdefault(layer, []).append(handle)

    counts = {family: {} for family in families}
    member_handles_by_layer = {}
    overlaps = []
    total = 0
    rows = []
    for layer in sorted(observed):
        matched = matching_families(layer)
        if not matched:
            continue
        handles = observed[layer]
        quantity = len(handles)
        # null means membership cannot be located exactly. [] is a known empty layer.
        member_handles_by_layer[layer] = (
            sorted(handles) if all(handle_counts.get(handle) == 1 for handle in handles) else None
        )
        for family in matched:
            counts[family][layer] = quantity
        total += quantity
        if len(families) > 1:
            overlap = len(matched) > 1
            if overlap:
                overlaps.append(layer)
            rows.append([layer, *[quantity if family in matched else "" for family in families],
                         "Yes" if overlap else ""])

    if len(families) == 1:
        result = {"counts": counts[families[0]], "total": total}
    else:
        result = {"table": {"columns": ["Layer", *families, "Overlap"], "rows": rows},
                  "overlapping_layers": overlaps, "total": total}
    result["member_handles_by_layer"] = member_handles_by_layer
    return result, None
'''
