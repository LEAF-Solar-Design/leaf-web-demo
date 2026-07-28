"""Trusted read-only instant tool used for the first warm-pool proof."""


def run(intake, params):
    context = intake.get("drawing_context", intake)
    params = params or {}
    prefix = params.get("prefix")
    names = set(context.get("layers") or [])
    for kind in ("polylines", "inserts", "faces3d"):
        for entity in context.get(kind) or []:
            if isinstance(entity, dict):
                names.add(entity.get("layer", "0"))
    ordered = sorted(str(name) for name in names)
    if isinstance(prefix, str) and prefix:
        ordered = [name for name in ordered if name.startswith(prefix)]
    return {"layers": ordered, "count": len(ordered)}, None
