"""Per-tenant DAILY authoring-attempt quota — the counter store.

POLICY (is the quota on, what is the limit, what the over-quota envelope looks
like) lives in ``da/usage.py`` alongside the daily RUN quota it stands beside.
This module owns only the COUNT: charge one attempt for a tenant on the current
UTC day, and say whether that charge was accepted.

Store selection mirrors the guest upload caps (``guest_uploads._guest_cap_store``)
because that is the same problem — a fleet-wide daily count that must survive a
restart:

  * ``memory`` (default) — an in-process counter. Correct for local runs, the
    demo, and tests. NOT durable and NOT shared between replicas; a restart
    clears it.
  * ``postgres`` — the durable authority. ``leaf_platform.counters.SharedCounterStore``
    bound to ``author_quota_counters`` (migration 0023), charged inside one
    serializable transaction so two concurrent attempts cannot both take the
    last slot. It NEVER falls back to memory: a database failure with the quota
    configured raises, and the router turns that into a refusal, because a gate
    that silently degrades to unmetered is not a gate.

The counter is charged BEFORE the harness runs and is never refunded. An
authoring attempt that fails downstream still spent Anthropic and sandbox money,
which is exactly what this quota bounds. (Guest uploads refund because a
rejected upload consumed nothing.)
"""
from __future__ import annotations

import importlib.util
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Tuple

SERVER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SERVER_DIR.parent

# The authority-owned table bound to the shared counter primitive (migration 0023).
COUNTER_TABLE = "author_quota_counters"
# One namespace for this authority; the day and tenant live in the counter key.
COUNTER_NAMESPACE = "author_daily"

_PG_COUNTER: Any = None

_MEMORY_LOCK = threading.Lock()
# {counter_key: value} — reset implicitly, since a new UTC day is a new key.
_MEMORY_STATE: dict[str, int] = {}


class AuthorQuotaStoreError(RuntimeError):
    """The configured counter store could not be reached or is misconfigured."""


def store_mode() -> str:
    mode = os.environ.get("LEAF_AUTHOR_QUOTA_STORE", "memory").strip().lower()
    if mode not in {"memory", "postgres"}:
        raise AuthorQuotaStoreError(
            "LEAF_AUTHOR_QUOTA_STORE must be either 'memory' or 'postgres'")
    return mode


def retention_days() -> int:
    raw = os.environ.get("LEAF_AUTHOR_QUOTA_RETENTION_DAYS", "8")
    try:
        days = int(raw)
    except ValueError as exc:
        raise AuthorQuotaStoreError(
            "LEAF_AUTHOR_QUOTA_RETENTION_DAYS must be an integer") from exc
    if not 2 <= days <= 366:
        raise AuthorQuotaStoreError(
            "LEAF_AUTHOR_QUOTA_RETENTION_DAYS must be between 2 and 366")
    return days


def counter_key(tenant_id: str, day: str) -> str:
    """``<utc-day>:<tenant id>``.

    The tenant id is stored in the clear on purpose: it is already the durable
    partition key across this platform's tables, so hashing it here (as the
    guest caps hash an IP) would buy no privacy and would cost the operator the
    ability to read the counter. Ids are bounded and validated upstream by
    ``tenant_id_validator.is_valid_tenant_id``.
    """
    return f"{day}:{tenant_id}"


def _load_platform_counters():
    """Load the platform package under its non-colliding alias.

    The repository's ``platform/`` directory shadows the stdlib module of that
    name, so it is loaded by path under ``leaf_platform``, exactly as
    ``server/app.py``, ``platform_link.py`` and ``guest_uploads.py`` do.
    """
    if "leaf_platform" not in sys.modules:
        package_dir = PROJECT_ROOT / "platform"
        spec = importlib.util.spec_from_file_location(
            "leaf_platform", package_dir / "__init__.py",
            submodule_search_locations=[str(package_dir)],
        )
        if spec is None or spec.loader is None:
            raise AuthorQuotaStoreError("platform package could not be loaded")
        module = importlib.util.module_from_spec(spec)
        sys.modules["leaf_platform"] = module
        spec.loader.exec_module(module)
    import leaf_platform.db as db  # noqa: PLC0415
    from leaf_platform.counters import SharedCounterStore  # noqa: PLC0415
    return db, SharedCounterStore


def _postgres_counter():
    global _PG_COUNTER
    if _PG_COUNTER is None:
        _db, counter_type = _load_platform_counters()
        _PG_COUNTER = counter_type(COUNTER_TABLE)
    return _PG_COUNTER


def _cleanup_old_counters(conn) -> None:
    """Delete at most 100 expired rows, as the guest cap authority does."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days())
    conn.execute(
        f"""
        DELETE FROM {COUNTER_TABLE}
        WHERE ctid IN (
          SELECT ctid
          FROM {COUNTER_TABLE}
          WHERE updated_at < %(cutoff)s
          ORDER BY updated_at
          LIMIT 100
        )
        """,
        {"cutoff": cutoff},
    )


def _postgres_charge(key: str, limit: int) -> Tuple[bool, int]:
    # Package loading and counter construction are INSIDE the wrapper: an import
    # or initialization failure is a store outage like any other, and must become
    # the opaque 503 refusal rather than escaping as a raw 500.
    try:
        db, _counter_type = _load_platform_counters()
        counter = _postgres_counter()

        def consume(conn):
            result = counter.consume_in_transaction(
                conn, namespace=COUNTER_NAMESPACE, key=key, limit=limit,
            )
            if result.accepted:
                _cleanup_old_counters(conn)
            return result

        result = db.run_transaction(consume, isolation="serializable", max_attempts=3)
    except AuthorQuotaStoreError:
        raise
    except Exception as exc:  # noqa: BLE001
        # Type only: a database error message can carry a connection string.
        raise AuthorQuotaStoreError(
            f"author quota counter unavailable: {type(exc).__name__}") from exc
    return bool(result.accepted), int(result.value)


def _memory_charge(key: str, limit: int) -> Tuple[bool, int]:
    with _MEMORY_LOCK:
        used = _MEMORY_STATE.get(key, 0)
        if used + 1 > limit:
            return False, used
        _MEMORY_STATE[key] = used + 1
        return True, used + 1


def charge(tenant_id: str, day: str, limit: int, *,
           require_durable: bool = False) -> Tuple[bool, int]:
    """Charge one authoring attempt against ``limit`` for ``tenant_id`` on ``day``.

    Returns ``(accepted, value)``. On acceptance ``value`` is the count INCLUDING
    this attempt; on refusal it is the already-recorded count, which the caller
    reports as ``used``. Raises ``AuthorQuotaStoreError`` if the configured store
    is unreachable or misconfigured — never silently unmetered.

    ``require_durable`` refuses the memory counter outright. The caller sets it
    for any deployment where a per-process counter would not be a real cap: two
    replicas keep two independent counts, so at limit N a tenant gets N per
    replica, and a restart returns every tenant to zero. A quota configured
    against memory in such a deployment reads as enforced while bounding almost
    nothing, so it fails loudly at the first attempt instead.
    """
    if require_durable and store_mode() != "postgres":
        raise AuthorQuotaStoreError(
            "LEAF_DAILY_AUTHOR_QUOTA is configured but LEAF_AUTHOR_QUOTA_STORE is "
            "not 'postgres'; a per-process counter is not a cap across replicas "
            "or restarts")
    key = counter_key(tenant_id, day)
    if store_mode() == "postgres":
        return _postgres_charge(key, limit)
    return _memory_charge(key, limit)


def reset_memory_state() -> None:
    """Clear the in-process counter (tests only)."""
    with _MEMORY_LOCK:
        _MEMORY_STATE.clear()


def reset_postgres_counter() -> None:
    """Drop the memoized counter binding (tests only)."""
    global _PG_COUNTER
    _PG_COUNTER = None
