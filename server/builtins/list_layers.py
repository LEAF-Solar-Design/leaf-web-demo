"""Built-in: list-layers — enumerate the distinct layer names in the drawing.

A self-contained built-in OUTSIDE the original 3-op family (count / measure /
highlight): its engine_op is `list_layers`, it has no OPS/OP_ALIASES/MIRRORS
entry, and its result shape ({"layers": [...], "count": N}) differs from
count-by-layer's ({"counts": {...}, "total": N}). Reference implementation that
the authoring templater mirrors into a self-contained authored file.
"""
from __future__ import annotations


def run(intake, params):
    params = params or {}
    prefix = params.get("prefix")
    names = set()
    for key in ("polylines", "inserts", "faces3d"):
        for ent in intake.get(key) or []:
            names.add(ent.get("layer", "0"))
    ordered = sorted(names)
    if prefix:
        ordered = [n for n in ordered if n.startswith(prefix)]
    return ({"layers": ordered, "count": len(ordered)}, None)
