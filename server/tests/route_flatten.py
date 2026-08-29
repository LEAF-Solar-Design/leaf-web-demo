"""Flatten an app's route tree into leaf routes with full paths.

fastapi 0.110+ stopped flattening ``include_router``: each call appends ONE
``_IncludedRouter`` node (no ``.path``, no ``.methods``, no ``.dependant``)
whose children hang off ``original_router.routes`` and whose include-time
prefix lives at ``include_context.prefix``. Any test that walks ``app.routes``
reading ``.path`` directly therefore either crashes or -- far worse --
silently enumerates nothing, which turns every "surface is empty" assertion
vacuous. This helper is the one sanctioned walk; do not hand-roll ``r.path``
loops in tests.

Fails closed on an unknown container: a node with neither children nor a
``path`` is yielded as a leaf with its bare prefix, so a pinned-set assertion
sees an unexpected entry instead of silence.
"""


def iter_leaf_routes(routes, prefix=""):
    """Yield (full_path, route) for every LEAF route, descending fastapi>=0.110
    _IncludedRouter nodes (include-time prefix applied) and starlette Mounts
    (mount path applied), recursively."""
    for r in routes:
        original_router = getattr(r, "original_router", None)
        if original_router is not None:  # fastapi>=0.110 _IncludedRouter
            ctx = getattr(r, "include_context", None)
            ctx_prefix = getattr(ctx, "prefix", "") or ""
            yield from iter_leaf_routes(original_router.routes,
                                        prefix + ctx_prefix)
            continue
        try:
            subs = getattr(r, "routes", None)
        except Exception:
            subs = None
        if subs:  # a Mount / sub-application
            yield from iter_leaf_routes(subs, prefix + getattr(r, "path", ""))
            continue
        yield prefix + getattr(r, "path", ""), r


def leaf_paths(app_or_routes):
    """Every leaf route path of an app (or a raw routes list), flattened."""
    routes = getattr(app_or_routes, "routes", app_or_routes)
    return [p for p, _ in iter_leaf_routes(routes)]
