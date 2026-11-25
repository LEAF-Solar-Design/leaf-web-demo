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


def requested_family(description):
    request = " ".join(description.split())
    match = _SUBTOTAL.fullmatch(request) or _COUNT.fullmatch(request)
    if match:
        return match["family"].casefold()


def build_body(family):
    return _BODY.replace("__FAMILY__", repr(family))


_BODY = r'''
import re


def run(intake, params):
    family = __FAMILY__
    names = {family, family + "s"}
    if family.endswith("s"):
        names.add(family[:-1])

    def matches(layer):
        words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", layer)
        tokens = re.findall(r"[^\W_]+", words.casefold())
        return layer.casefold() in names or any(token in names for token in tokens)

    counts = {}
    for row in intake.get("layers") or []:
        layer = row.get("name") if isinstance(row, dict) else row
        if isinstance(layer, str) and matches(layer):
            counts[layer] = 0
    for key in ("polylines", "inserts", "faces3d"):
        for entity in intake.get(key) or []:
            layer = entity.get("layer", "0")
            if layer in counts:
                counts[layer] += 1
            elif matches(layer):
                counts[layer] = 1
    ordered = {layer: counts[layer] for layer in sorted(counts)}
    return ({"counts": ordered, "total": sum(ordered.values())}, None)
'''
