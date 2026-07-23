"""Billing → stored-tier seam for the platform lane (contract/BILLING.md §3).

The platform jobs lane branches on the org row's STORED tier
(platform/entitlements.py). Login-time claims track billing state through the
Auth0 Post-Login Action, but the stored tier had NO billing feed — a
subscription change never reached the orgs table. This module is that feed's
server half: it loads the canonical plan→tier mapping
(server/billing_tiers.py) through the same explicit-file-path seam
entitlements.py uses, so the stored leg and the login leg resolve tiers from
ONE table.

Activation is an OPERATOR decision, dark by default (census item 5):

  * ``LEAF_BILLING_SYNC_LIVE=1``   — the flag; unset/0 => endpoint answers 503.
  * ``LEAF_BILLING_SYNC_SECRET``   — shared secret for the caller hop
    (leaf_website's Stripe webhook); unset => 503 (fail closed, never open).

Both are read at call time so tests and subprocess overrides apply.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SERVER_DIR = _PROJECT_ROOT / "server"
_BILLING_TIERS_FILE = _SERVER_DIR / "billing_tiers.py"

TIER_SYNC_FLAG_ENV = "LEAF_BILLING_SYNC_LIVE"
TIER_SYNC_SECRET_ENV = "LEAF_BILLING_SYNC_SECRET"


def sync_enabled() -> bool:
    return os.environ.get(TIER_SYNC_FLAG_ENV, "0") == "1"


def sync_secret() -> str:
    """The configured hop secret ('' when unset — caller must fail closed)."""
    return os.environ.get(TIER_SYNC_SECRET_ENV, "")


def server_billing_tiers():
    """server/billing_tiers.py, loaded by explicit file path and cached.

    Mirrors entitlements._server_entitlements(): keeps this package importable
    without the server tree on sys.path and cannot re-shadow the stdlib
    ``platform`` module. billing_tiers is stdlib-only, so no sys.path append
    is needed.
    """
    mod = sys.modules.get("leaf_server_billing_tiers")
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location(
        "leaf_server_billing_tiers", _BILLING_TIERS_FILE
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"billing tiers module unavailable at {_BILLING_TIERS_FILE}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules["leaf_server_billing_tiers"] = mod
    return mod
