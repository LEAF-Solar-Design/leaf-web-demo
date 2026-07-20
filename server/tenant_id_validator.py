"""server/tenant_id_validator.py — the ONE canonical tenant / opaque-id rule.

Security-audit 2026-07-18 F13: THREE different id validators disagreed —

  * ``da/store.py`` ``sanitize_id``    COLLAPSED an id to ``[a-z0-9-]`` (so
                                       ``acme_corp`` and ``acme-corp`` both folded
                                       onto the SAME store key);
  * ``server/tenant_paths.py``         permitted ``.`` ``_`` and unicode as a path
                                       component;
  * harness ``safeBase`` (TS)          rejected via ``^[A-Za-z0-9._-]+$`` (uppercase
                                       and dots allowed).

Because ``sanitize_id`` *collapsed* rather than *rejected*, two DISTINCT tenants
that differed only in non-``[a-z0-9-]`` characters (``acme_corp`` vs ``acme-corp``,
``Acme Corp`` vs ``acme-corp``) COLLIDED on one namespace in the store — a
cross-tenant data-mingling hazard.

FIX: one shared **REJECT-don't-collapse** rule, mirrored byte-for-byte in the TS
``safeBase``. We NEVER transform an id into a canonical form (transforming is
exactly what lets two inputs collide); an id is ACCEPTED unchanged iff it already
matches, else REJECTED.

CHARSET — ``^[a-z0-9][a-z0-9_-]{0,62}$``
    lowercase alphanumeric + hyphen + underscore, must start alphanumeric, 1..63 chars.

    * keeps the demo tenant ``demo-tenant`` valid;
    * keeps the platform's real Auth0 tenant ids valid — ``org_leaf_demo`` and
      ``org_acme_solar`` (see ``data/tenants.sample.json`` + ``server/auth0-actions``;
      the underscores are load-bearing, so the charset MUST admit ``_``);
    * rejects uppercase, spaces, dots, ``/`` / ``\\`` (path traversal) and unicode.

Reject-don't-collapse means ``acme-corp`` and ``acme_corp`` are now DISTINCT tenants
with DISTINCT store keys (they collided before); a dirty id like ``Acme Corp`` is
rejected outright instead of being silently folded onto another tenant's namespace.
"""
from __future__ import annotations

import re

# The canonical pattern. MIRRORED verbatim in
# harness/src/ports/impl/oauthGrantProvider.ts (safeBase). Keep the three call sites
# — da/store.py sanitize_id, server/tenant_paths.py _safe_component, and the TS
# safeBase — in agreement; da/test_hardening_1f.py FAILS if they drift.
TENANT_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,62}$"
_TENANT_ID_RE = re.compile(TENANT_ID_PATTERN)


def is_valid_tenant_id(value: object) -> bool:
    """True iff ``value`` is a ``str`` that already matches the canonical rule.

    Performs NO transformation — a soft predicate for callers (tenant_paths) that
    prefer a ``None`` sentinel over an exception.
    """
    return isinstance(value, str) and _TENANT_ID_RE.match(value) is not None


def validate_tenant_id(value: object, *, kind: str = "tenant id") -> str:
    """Return ``value`` UNCHANGED iff it matches the canonical rule, else raise ``ValueError``.

    REJECT-don't-collapse: we never normalise (normalising — lower-casing, stripping,
    char-folding — is what let distinct ids collide, security-audit F13).
    """
    if not is_valid_tenant_id(value):
        raise ValueError(
            f"invalid {kind} {value!r}: must match {TENANT_ID_PATTERN} "
            "(lowercase a-z / 0-9 / '_' / '-', start alphanumeric, 1..63 chars)"
        )
    return value  # type: ignore[return-value]
