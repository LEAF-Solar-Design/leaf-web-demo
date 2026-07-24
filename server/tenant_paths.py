"""
Tenant -> mushy-repo directory resolution, SHARED by deps.py (the catalog fold)
and tool_loader.py (entry resolution) so both agree, byte-for-byte, on where a
given tenant's repo lives.

Wave 4 (multi-tenant Build lane): a tenant's authored tools live in their OWN
mushy repo at ``$LEAF_TENANTS_DIR/<tenant_id>``. ``$LEAF_TENANT_REPO`` is kept as
a BACK-COMPAT override for the demo tenant only (the proven wave-3 single-tenant
loop set exactly that env). Both envs are read at CALL time so a subprocess/test
override applies.

OFF-BY-DEFAULT invariant: with NEITHER env set this returns ``None`` (no tenant
fold, no tenant entry resolution), so a demo with neither knob is BYTE-IDENTICAL
to the pre-wave-4 world. The ``C:/tmp/leaf-tenants`` default lives ONLY in the TS
harness provider (which provisions repos), never here — so the Python fold never
silently activates just because that directory happens to exist on disk.

PATH SAFETY: a tenant_id is treated as a single path component. Anything that
looks like traversal or contains a separator resolves to ``None`` (no repo) so a
crafted tenant_id can never escape ``$LEAF_TENANTS_DIR``.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from tenant_id_validator import is_valid_tenant_id  # F13: the ONE shared reject-don't-collapse rule

DEFAULT_TENANT = "demo-tenant"


def _safe_component(tenant_id: str) -> Optional[str]:
    """Return tenant_id iff it is a single, traversal-free component that also matches
    the ONE shared tenant-id rule (server/tenant_id_validator, security-audit F13), else None.

    Previously permitted ``.`` / ``_`` / unicode, DISAGREEING with da/store.py's
    (then-collapsing) ``sanitize_id`` — a distinct tenant differing only in those
    chars could resolve to its own repo dir here yet COLLIDE with another tenant on a
    single store key. Now both accept exactly the same charset (``[a-z0-9_-]``).
    """
    tid = "" if tenant_id is None else str(tenant_id)
    if not tid:
        return None
    # a well-formed tenant id is its own basename (no embedded separator).
    basename = tid.replace("\\", "/").split("/")[-1]
    if basename != tid or not is_valid_tenant_id(basename):
        return None
    return basename


def resolve_tenant_repo_dir(tenant_id: Optional[str]) -> Optional[Path]:
    """Resolve a tenant to its mushy-repo checkout root, or None (fold/resolution off).

    Two modes, chosen by which env is set:

      * MULTI-TENANT (``$LEAF_TENANTS_DIR`` set) -> per-tenant dir ``<base>/<tenant_id>``
        (path-safe). ``$LEAF_TENANT_REPO`` additionally OVERRIDES the demo tenant to a
        specific path. This is the wave-4 isolation mode: tenant A and tenant B resolve
        to DIFFERENT dirs, so their authored tools never mingle.
      * LEGACY SINGLE-REPO (only ``$LEAF_TENANT_REPO`` set) -> that ONE repo serves EVERY
        tenant (the proven wave-3 single-tenant demo — one repo, one grant). No isolation
        by design; this is the demo/back-compat mode.
      * NEITHER set -> None (fold + entry resolution OFF; byte-identical to pre-wave-3).

    A ``None``/empty tenant_id is treated as the demo tenant (legacy no-tenant calls).
    """
    tid = str(tenant_id).strip() if tenant_id is not None else ""
    if not tid:
        tid = DEFAULT_TENANT
    comp = _safe_component(tid)
    if comp is None:
        return None
    tid = comp

    # An effective pin, when present, is the only runtime authority. The
    # import is lazy to avoid a dependency cycle during startup. No pin keeps
    # the legacy mutable-checkout behavior exactly as before.
    from customization_service import effective_catalog_dir
    pinned = effective_catalog_dir(tid)
    if pinned is not None:
        return pinned

    single = os.environ.get("LEAF_TENANT_REPO", "").strip()
    base = os.environ.get("LEAF_TENANTS_DIR", "").strip()

    if base:  # multi-tenant mode
        if tid == DEFAULT_TENANT and single:
            return Path(single)  # demo-tenant back-compat override
        return Path(base) / tid

    if single:  # legacy single-repo mode: one repo for all tenants (wave-3 back-compat)
        return Path(single)

    return None
