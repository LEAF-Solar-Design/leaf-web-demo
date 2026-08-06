"""Consumer -> mushy-repo directory resolution, read at CALL time.

Extracted from leaf-web-demo server/tenant_paths.py (consumer #1 keeps a thin
wrapper that layers its platform-specific catalog-pin override on top of this).

Semantics carried over exactly:

* Both env vars are read at CALL time so a subprocess/test override applies.
* OFF-BY-DEFAULT invariant: with NEITHER env set this returns ``None`` (no
  fold, no entry resolution) — a consumer with neither knob is byte-identical
  to a world without the fold. No default directory is baked in here; only the
  author-side provider (which provisions repos) may know one.
* PATH SAFETY: a consumer_id is treated as a single path component. Anything
  that looks like traversal or contains a separator resolves to ``None`` (no
  repo) so a crafted id can never escape the consumers directory.

Two modes, chosen by which env is set:

* MULTI-CONSUMER (``consumers_dir_env`` set) -> per-consumer dir
  ``<base>/<consumer_id>`` (path-safe). ``single_repo_env`` additionally
  OVERRIDES the default consumer to a specific path.
* LEGACY SINGLE-REPO (only ``single_repo_env`` set) -> that ONE repo serves
  EVERY consumer. No isolation by design; back-compat mode.
* NEITHER set -> ``None`` (fold + entry resolution OFF).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

from .ids import is_valid_consumer_id

DEFAULT_CONSUMER = "default"

# Env names are PARAMETERS with these generic defaults; consumer #1
# (leaf-web-demo) passes its historical LEAF_TENANTS_DIR / LEAF_TENANT_REPO.
CONSUMERS_DIR_ENV = "MUSHY_CONSUMERS_DIR"
SINGLE_REPO_ENV = "MUSHY_CONSUMER_REPO"


def safe_component(
    consumer_id: Optional[str],
    *,
    validator: Callable[[str], bool] = is_valid_consumer_id,
) -> Optional[str]:
    """Return consumer_id iff it is a single, traversal-free component that also
    matches the injected id rule (REJECT-don't-collapse), else None.

    The basename check keeps an embedded separator from ever reaching the
    validator: a well-formed consumer id is its own basename.
    """
    cid = "" if consumer_id is None else str(consumer_id)
    if not cid:
        return None
    basename = cid.replace("\\", "/").split("/")[-1]
    if basename != cid or not validator(basename):
        return None
    return basename


def resolve_consumer_repo_dir(
    consumer_id: Optional[str],
    *,
    consumers_dir_env: str = CONSUMERS_DIR_ENV,
    single_repo_env: str = SINGLE_REPO_ENV,
    default_consumer: str = DEFAULT_CONSUMER,
    validator: Callable[[str], bool] = is_valid_consumer_id,
) -> Optional[Path]:
    """Resolve a consumer to its mushy-repo checkout root, or None (fold off).

    A ``None``/empty consumer_id is treated as the default consumer (legacy
    no-consumer calls).
    """
    cid = str(consumer_id).strip() if consumer_id is not None else ""
    if not cid:
        cid = default_consumer
    comp = safe_component(cid, validator=validator)
    if comp is None:
        return None
    cid = comp

    single = os.environ.get(single_repo_env, "").strip()
    base = os.environ.get(consumers_dir_env, "").strip()

    if base:  # multi-consumer mode
        if cid == default_consumer and single:
            return Path(single)  # default-consumer back-compat override
        return Path(base) / cid

    if single:  # legacy single-repo mode: one repo for all consumers
        return Path(single)

    return None
