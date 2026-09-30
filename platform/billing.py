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
    (leaf_website's Stripe webhook); a non-empty value takes precedence.
  * ``LEAF_BILLING_SYNC_SECRET_ID`` — fallback Secrets Manager id when the
    direct secret is empty. One in-process entry caches SecretString for at
    most 300 seconds (monotonic), with serialized fetches and bounded AWS
    timeouts. Invalid ids or any fetch failure return '' (503, fail closed);
    failures are cached for at most 30 seconds. Logs contain only exception
    class and id, never the secret value or exception message.

All environment variables are read at call time so overrides apply.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
import threading
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SERVER_DIR = _PROJECT_ROOT / "server"
_BILLING_TIERS_FILE = _SERVER_DIR / "billing_tiers.py"

TIER_SYNC_FLAG_ENV = "LEAF_BILLING_SYNC_LIVE"
TIER_SYNC_SECRET_ENV = "LEAF_BILLING_SYNC_SECRET"
TIER_SYNC_SECRET_ID_ENV = "LEAF_BILLING_SYNC_SECRET_ID"

_logger = logging.getLogger(__name__)
_secret_cache: tuple[str, str, float] | None = None
_secret_lock = threading.Lock()


def _secrets_client_factory():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "secretsmanager",
        config=Config(connect_timeout=2, read_timeout=3,
                      retries={"max_attempts": 2, "mode": "standard"}),
    )


def sync_enabled() -> bool:
    return os.environ.get(TIER_SYNC_FLAG_ENV, "0") == "1"


def sync_secret() -> str:
    """Return the non-empty direct env secret, otherwise fetch by secret id.

    The id is read at call time and must be at most 512 characters without
    whitespace/control characters. A lock serializes fetches; one id-keyed
    entry caches SecretString for at most 300 monotonic seconds. Lazy AWS
    imports and bounded client timeouts keep the adapter optional. Any failure
    returns '' (caller must fail closed), cached for at most 30 seconds; logs
    contain only exception class and id, never value or exception message.
    With neither environment variable configured, return ''.
    """
    global _secret_cache
    direct = os.environ.get(TIER_SYNC_SECRET_ENV, "")
    if direct:
        return direct
    secret_id = os.environ.get(TIER_SYNC_SECRET_ID_ENV, "")
    if not secret_id or len(secret_id) > 512 or any(
        char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159
        for char in secret_id
    ):
        return ""
    with _secret_lock:
        now = time.monotonic()
        if _secret_cache is not None:
            cached_id, value, expires_at = _secret_cache
            if cached_id == secret_id and now < expires_at:
                return value
        try:
            response = _secrets_client_factory().get_secret_value(SecretId=secret_id)
            value = response.get("SecretString")
            if not isinstance(value, str) or not value:
                raise ValueError
        except Exception as exc:
            _logger.warning("%s %s", type(exc).__name__, secret_id)
            _secret_cache = (secret_id, "", time.monotonic() + 30)
            return ""
        _secret_cache = (secret_id, value, time.monotonic() + 300)
        return value


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
