"""The call-time surface-config fold: read one consumer repo's surface-config.json NOW.

Sibling of registry.py, same discipline: a call-time read of
<root>/surface-config.json, tolerant of every failure mode a consumer repo can
produce. Missing root / file / oversize / bad JSON / non-object / schema
violation -> {} (never raises): a broken overlay must degrade to "no
override", not take a tenant's UI down.

Schema: contract/surface-config.v1.schema.json (repo root). Its slot names
mirror web/src/site/productSurfaces.js (leaf-web-demo, a separate lane this
worktree does not read) -- the leaf-web-demo half pins that equality with a
test; this module only enforces the closed vocabulary the schema names.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional

SURFACE_CONFIG_FILE = "surface-config.json"

# 64 KiB: a tenant overlay is a handful of UI slot values, not a payload class
# that needs headroom. Bounding the read before json.loads keeps a hostile or
# corrupt repo file from forcing an unbounded parse allocation on every
# call-time fold (build doctrine: bound every allocation).
MAX_SURFACE_CONFIG_BYTES = 64 * 1024

SURFACE_IDS = frozenset({"browser", "cad", "solar", "ios", "sheets"})

# The slot names contract/surface-config.v1.schema.json declares at the
# surface-overlay level. Three carry their own closed field set (checked
# below); the rest are typed as objects only, because their interior field
# shape lives in productSurfaces.js, which this module cannot read.
_SLOT_NAMES = frozenset({
    "chrome", "toolbar", "rails", "commandLine", "authoring", "versions",
    "conversations", "builds", "contextMenu", "groundMaterial",
})

_CHROME_FIELDS = frozenset({"tab"})
_CONVERSATIONS_FIELDS = frozenset({"scope"})
_BUILDS_FIELDS = frozenset({"routes"})

_GENERIC_OBJECT_SLOTS = frozenset({
    "toolbar", "rails", "commandLine", "versions", "contextMenu", "groundMaterial",
})


def _default_on_error(cfg: Path, exc: Exception) -> None:
    print(f"[mushy-fold] bad surface-config.json at {cfg}: {exc}", file=sys.stderr)


def _valid_chrome(value: Any) -> bool:
    if not isinstance(value, dict) or not set(value).issubset(_CHROME_FIELDS):
        return False
    tab = value.get("tab")
    return tab is None or isinstance(tab, str)


def _valid_conversations(value: Any) -> bool:
    if not isinstance(value, dict) or not set(value).issubset(_CONVERSATIONS_FIELDS):
        return False
    scope = value.get("scope")
    return scope is None or isinstance(scope, str)


def _valid_builds(value: Any) -> bool:
    if not isinstance(value, dict) or not set(value).issubset(_BUILDS_FIELDS):
        return False
    routes = value.get("routes")
    return routes is None or (
        isinstance(routes, list) and all(isinstance(r, str) for r in routes)
    )


def _valid_slot(name: str, value: Any) -> bool:
    """One fixed-depth dispatch over the closed `_SLOT_NAMES` set -- no
    recursion, so validation depth is bounded by construction, not by input."""
    if name == "chrome":
        return _valid_chrome(value)
    if name == "conversations":
        return _valid_conversations(value)
    if name == "builds":
        return _valid_builds(value)
    if name == "authoring":
        return isinstance(value, bool)
    if name in _GENERIC_OBJECT_SLOTS:
        return isinstance(value, dict)
    return False  # unreachable: every _SLOT_NAMES member is handled above


def _valid_overlay(data: Any) -> bool:
    """surface id -> slot name -> one typed slot value; a bounded two-level walk."""
    if not isinstance(data, dict):
        return False
    for surface_id, slots in data.items():
        if surface_id not in SURFACE_IDS or not isinstance(slots, dict):
            return False
        for slot_name, slot_value in slots.items():
            if slot_name not in _SLOT_NAMES or not _valid_slot(slot_name, slot_value):
                return False
    return True


def load_repo_surface_config(
    root: Optional[Path],
    *,
    on_error: Callable[[Path, Exception], None] = _default_on_error,
) -> Dict[str, Any]:
    """Fold ONE consumer repo's surface-config.json overlay when the repo root resolves.

    Returns ``{}`` for: no root, missing file, an oversize file, bad JSON, a
    non-object payload, or any schema violation (unknown surface id, unknown
    slot, wrong slot type). The WHOLE file is rejected on any of these, never
    a partial merge, so a tenant can never end up with half an overlay
    silently in effect.

    ``on_error`` receives the surface-config path and the exception, mirroring
    ``load_repo_registry_tools``'s injected diagnostic.
    """
    if root is None:
        return {}
    cfg = Path(root) / SURFACE_CONFIG_FILE
    if not cfg.exists():
        return {}
    try:
        if cfg.stat().st_size > MAX_SURFACE_CONFIG_BYTES:
            raise ValueError(
                f"surface-config.json exceeds {MAX_SURFACE_CONFIG_BYTES} bytes"
            )
        data = json.loads(cfg.read_text(encoding="utf-8"))
        if not _valid_overlay(data):
            raise ValueError("surface-config.json failed schema validation")
        return data
    except Exception as exc:
        on_error(cfg, exc)
        return {}
