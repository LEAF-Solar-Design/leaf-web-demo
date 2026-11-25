"""Author a read-only entity subtotal for a requested family of layer names."""
import re


_SUBTOTAL = re.compile(
    r"(?:show(?: me)?|list|give me) (?:just |only |the )*(?P<family>[\w-]+) layers"
    r",? (?:with )?(?:one |a )?subtotal per layer[.!]?",
    re.IGNORECASE,
)
_COUNT = re.compile(
    r"(?:count|tally) (?:entities|objects) on (?:each |every |the )?"
    r"(?P<family>[\w-]+) layers?[.!]?",
    re.IGNORECASE,
)


_COMPARISON = re.compile(
    r"(?:show(?: me)?|list|give me|compare) (?:separate )?"
    r"(?P<family>[\w-]+(?:, [\w-]+){0,2},? and [\w-]+) (?:layer )?subtotals"
    r"(?:,? and (?:flag|show|list) (?:any )?(?:overlapping layers|"
    r"layers? (?:whose names? )?(?:that )?match(?:es)? (?:both|all)))?[.!]?",
    re.IGNORECASE,
)


def requested_family(description):
    request = " ".join(description.split())
    match = (_SUBTOTAL.fullmatch(request) or _COUNT.fullmatch(request)
             or _COMPARISON.fullmatch(request))
    if match:
        return match["family"].casefold()


def build_body(family):
    families = tuple(dict.fromkeys(re.split(r", |,? and ", family)))
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
    for row in intake.get("layers") or []:
        layer = row.get("name") if isinstance(row, dict) else row
        if isinstance(layer, str):
            observed[layer] = 0
    for key in ("polylines", "inserts", "faces3d"):
        for entity in intake.get(key) or []:
            layer = entity.get("layer", "0")
            if isinstance(layer, str):
                observed[layer] = observed.get(layer, 0) + 1

    counts = {family: {} for family in families}
    overlaps = []
    total = 0
    rows = []
    for layer in sorted(observed):
        matched = matching_families(layer)
        if not matched:
            continue
        quantity = observed[layer]
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
        return ({"counts": counts[families[0]], "total": total}, None)
    return ({"table": {"columns": ["Layer", *families, "Overlap"], "rows": rows},
             "overlapping_layers": overlaps, "total": total}, None)
'''
