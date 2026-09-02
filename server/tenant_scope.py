"""Per-tenant tool scope: the server-owned allowlist that turns one tenant's
catalog into a locked, single-purpose app.

Why (2026-09-02): a client handed a bespoke tool (timber-cutlist) as a
"standalone application" must see and run ONLY that tool. Their seat (org tier
`restricted`, the read-only floor, plus the `client_tool` role in
server/roles.json that adds upload) already withholds build / converse / write;
this module withholds the rest of the CATALOG, so the tenant cannot list,
resolve, or run anything the operator did not scope to them. The tier
vocabulary stays frozen (contract/AUTH.md §11); roles are operator-extensible.

Policy file: server/tenant_tool_scopes.json (override with
LEAF_TENANT_TOOL_SCOPES_FILE). Shape::

    {"scopes": {"<tenant_id>": {"label": "Cut lists", "tools": ["timber-cutlist"]}}}

Contract (fail closed, bounded, no allocation for unscoped tenants):
  * A tenant absent from the file is UNSCOPED: `filter_rows` returns its input
    object unchanged, so every existing caller is byte-identical.
  * A scoped tenant sees exactly the catalog rows whose `name` is listed. A
    listed name with no catalog row is simply absent (never an error, never a
    fallback to "everything").
  * A scoped entry with an unreadable/empty `tools` list scopes the tenant to
    NOTHING (an operator typo must never widen access).
  * An absent file is the "nobody is scoped" policy. A PRESENT but unreadable
    or malformed file raises ScopePolicyError: a corrupted scope file must not
    silently unlock a scoped tenant, and the request layer turns the error into
    the same structured 503 the entitlement policy uses.
  * Bounds: at most MAX_SCOPES entries, MAX_TOOLS names per scope, MAX_LABEL
    chars per label. Tenant ids must satisfy the ONE shared tenant-id rule.
The parsed policy is cached on (path, mtime_ns, size) so the per-request cost
for /api/tools is one stat(), not a JSON parse.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from tenant_id_validator import is_valid_tenant_id

_DEFAULT_FILE = Path(__file__).resolve().parent / "tenant_tool_scopes.json"
MAX_SCOPES = 256
MAX_TOOLS = 64
MAX_LABEL = 120
_TOOL_NAME_MAX = 128


class ScopePolicyError(ValueError):
    """The scope file is present but cannot be trusted (unreadable / malformed)."""


class TenantScope:
    """One tenant's allowlist. Immutable; `tools` is a frozenset of tool names."""

    __slots__ = ("tenant_id", "label", "tools")

    def __init__(self, tenant_id: str, label: str, tools: FrozenSet[str]) -> None:
        self.tenant_id = tenant_id
        self.label = label
        self.tools = tools

    def public_view(self) -> Dict[str, Any]:
        """The shape the web reads from /api/entitlements: names only, sorted."""
        return {"label": self.label, "tools": sorted(self.tools)}


_lock = threading.Lock()
_cache_key: Optional[Tuple[str, int, int]] = None
_cache_value: Dict[str, TenantScope] = {}


def _policy_file() -> Path:
    return Path(os.environ.get("LEAF_TENANT_TOOL_SCOPES_FILE") or _DEFAULT_FILE)


def _clean_tools(raw: Any) -> FrozenSet[str]:
    """Only real, bounded strings survive; anything else scopes to nothing."""
    if not isinstance(raw, list) or len(raw) > MAX_TOOLS:
        return frozenset()
    names = set()
    for item in raw:
        if not isinstance(item, str):
            return frozenset()
        name = item.strip()
        if not name or len(name) > _TOOL_NAME_MAX:
            return frozenset()
        names.add(name)
    return frozenset(names)


def _parse(text: str, path: Path) -> Dict[str, TenantScope]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ScopePolicyError(f"tenant scope file invalid JSON at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ScopePolicyError(f"tenant scope file top level must be a mapping ({path})")
    scopes = raw.get("scopes", {})
    if not isinstance(scopes, dict):
        raise ScopePolicyError(f"tenant scope file 'scopes' must be a mapping ({path})")
    if len(scopes) > MAX_SCOPES:
        raise ScopePolicyError(f"tenant scope file lists more than {MAX_SCOPES} tenants ({path})")
    out: Dict[str, TenantScope] = {}
    for tenant_id, entry in scopes.items():
        if not is_valid_tenant_id(tenant_id):
            raise ScopePolicyError(f"tenant scope file has an invalid tenant id {tenant_id!r} ({path})")
        if not isinstance(entry, dict):
            # A malformed entry is a scoped tenant with NOTHING, never unscoped.
            out[tenant_id] = TenantScope(tenant_id, tenant_id, frozenset())
            continue
        label = entry.get("label")
        if not isinstance(label, str) or not label.strip():
            label = tenant_id
        out[tenant_id] = TenantScope(tenant_id, label.strip()[:MAX_LABEL], _clean_tools(entry.get("tools")))
    return out


def load_scopes() -> Dict[str, TenantScope]:
    """The tenant_id -> TenantScope map; cached on the file's (path, mtime, size)."""
    global _cache_key, _cache_value
    path = _policy_file()
    try:
        st = path.stat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ScopePolicyError(f"tenant scope file unreadable at {path}: {exc}") from exc
    key = (str(path), st.st_mtime_ns, st.st_size)
    with _lock:
        if _cache_key == key:
            return _cache_value
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ScopePolicyError(f"tenant scope file unreadable at {path}: {exc}") from exc
    parsed = _parse(text, path)
    with _lock:
        _cache_key, _cache_value = key, parsed
    return parsed


def reset_cache() -> None:
    """Tests only: forget the parsed policy."""
    global _cache_key, _cache_value
    with _lock:
        _cache_key, _cache_value = None, {}


def scope_for(tenant_id: Any) -> Optional[TenantScope]:
    """The tenant's scope, or None when the tenant is unscoped."""
    tid = str(tenant_id).strip() if tenant_id is not None else ""
    if not tid:
        return None
    return load_scopes().get(tid)


def filter_rows(tenant_id: Any, rows: List[Tuple[Dict[str, Any], str]]) -> List[Tuple[Dict[str, Any], str]]:
    """Narrow folded catalog rows to the tenant's allowlist.

    Unscoped tenant -> the SAME list object back (no copy, no allocation).
    Scoped tenant -> only rows whose name is listed; order preserved.
    """
    scope = scope_for(tenant_id)
    if scope is None:
        return rows
    allowed = scope.tools
    return [pair for pair in rows if pair[0].get("name") in allowed]


def public_view(tenant_id: Any) -> Optional[Dict[str, Any]]:
    """`{label, tools}` for the web shell, or None when unscoped."""
    scope = scope_for(tenant_id)
    return scope.public_view() if scope is not None else None
