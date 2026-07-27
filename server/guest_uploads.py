"""
Guest drawing uploads (CONTRACT-ADDENDUM §19) — the feature's core module.

Owns everything the upload lane promises OUT LOUD in the UI, so the promise and
the mechanism cannot drift apart:

  * retention_hours() is THE one retention constant. The SB3/NT2 copy the
    frontend renders comes from /api/site/guest-upload-policy, which reads this
    function; purge_expired() honors the expiry STAMPED from this same function
    at upload time. There is no second copy of the number anywhere.
  * Guest tenants (``guest-<hex>``, write_loop.GUEST_TENANT_PREFIX) live in an
    isolated filesystem store (write_loop.guest_store_dir) so deletion is a
    provable filesystem operation — the StorageBackend interface has no delete.
  * The upload marker (upload.state.json, write_loop.upload_marker_key) is the
    single source of upload truth: ``extracting`` -> ``ready`` | ``failed``.
    ensure_demo_drawing treats a marker-without-manifest as a hard stop, which
    is what closes the fabrication trap (an uploaded id must NEVER fall through
    to the cached rooftop_demo intake bootstrap — real extraction or an honest
    error, nothing else).

HONESTY RULE for extraction: geometry only ever comes from the user's actual
bytes. A .dxf is parsed by server/dxf_intake.py (a real parse of their file) by
DEFAULT, in both modes — the DWG extract Activity binds HostDwg to a fixed
`input.dwg` localName, so a DXF sent there is rejected as an invalid drawing.
With LEAF_GUEST_DXF_EXTRACT=aps AND APS_LIVE=1, a .dxf instead goes through the
credential-holding broker to the DXF-correct Activity (HostDwg localName
`input.dxf`), a real full-fidelity APS extract that costs a paid run like DWG.
A .dwg goes through the broker (POST /broker/extract {upload: true}) at
APS_LIVE=1, and fails honestly (APS_UNAVAILABLE) at APS_LIVE=0 because no local
DWG reader exists.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import importlib.util
import json
import os
import secrets
import shutil
import sys
import threading
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests

import broker_client
import write_loop
from write_loop import GUEST_TENANT_PREFIX

SERVER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SERVER_DIR.parent

ACCEPTED_EXTENSIONS = (".dwg", ".dxf")


# --------------------------------------------------------------------------- #
# config — env with defaults, read at CALL time (test/subprocess overridable)
# --------------------------------------------------------------------------- #
def enabled() -> bool:
    """Feature master switch (policy endpoint reports it; routes 503 when off)."""
    return os.environ.get("LEAF_GUEST_UPLOADS_ENABLED", "1") == "1"


def retention_hours() -> float:
    """THE one retention constant (D-1, default 24 h). Every user-facing copy of
    the retention window and the purge job's stamped expiry both derive from
    this function — change the env, and the copy and the deletion move together."""
    try:
        return float(os.environ.get("LEAF_GUEST_RETENTION_HOURS", "24"))
    except ValueError:
        return 24.0


def max_upload_bytes() -> int:
    try:
        return int(os.environ.get("LEAF_UPLOAD_MAX_BYTES", str(25 * 1024 * 1024)))
    except ValueError:
        return 25 * 1024 * 1024


def uploads_dir() -> Path:
    """Staging area for raw uploaded files, broker-readable. MUST agree with the
    broker's _resolve_upload_dwg root (same env, same default)."""
    return Path(os.environ.get("LEAF_UPLOADS_DIR", str(PROJECT_ROOT / "data" / "uploads")))


def extract_timeout_s() -> float:
    try:
        return float(os.environ.get("LEAF_UPLOAD_EXTRACT_TIMEOUT_S", "900"))
    except ValueError:
        return 900.0


def _dxf_extract_mode() -> str:
    """How live DXF uploads extract: `local` (default) or `aps`.

    `local` — the built-in dxf_intake parser: free, instant, polylines + layer
    names. `aps` — the full-fidelity DXF Activity (INSERT/3DFACE/geo/xdata),
    which costs a paid APS run per upload just like DWG. `aps` only takes effect
    at APS_LIVE=1 AND once da.client.EXTRACT_DXF_ACTIVITY is provisioned; any
    unrecognized value falls back to `local` so a typo never silently bills
    APS. Flip this only after the Activity exists (da/provision_live.py
    --dxf-activity-only)."""
    mode = os.environ.get("LEAF_GUEST_DXF_EXTRACT", "local").strip().lower()
    return "aps" if mode == "aps" else "local"


def purge_interval_s() -> float:
    try:
        return float(os.environ.get("LEAF_GUEST_PURGE_INTERVAL_S", "300"))
    except ValueError:
        return 300.0


def guest_secret() -> Optional[str]:
    """HMAC secret for guest-session tokens (live-auth mode only). Unset =>
    the guest lane is OFF in live mode (upload returns an honest 503) — never
    an unsigned fallback."""
    return os.environ.get("LEAF_GUEST_SECRET") or None


def per_ip_daily_cap() -> int:
    try:
        return int(os.environ.get("LEAF_GUEST_UPLOADS_PER_IP_PER_DAY", "10"))
    except ValueError:
        return 10


def global_daily_cap() -> int:
    try:
        return int(os.environ.get("LEAF_GUEST_UPLOADS_PER_DAY", "100"))
    except ValueError:
        return 100


def policy_view() -> Dict[str, Any]:
    """The public /api/site/guest-upload-policy payload (pre-envelope). The
    frontend renders ALL retention/size copy from THIS — zero copy drift by
    construction."""
    import deps  # lazy: avoid import cycle at module load
    return {
        "enabled": enabled(),
        "retention_hours": retention_hours(),
        "max_bytes": max_upload_bytes(),
        "accepted": list(ACCEPTED_EXTENSIONS),
        "extract_live": bool(deps.APS_LIVE),
        "dxf_local_ok": True,
    }


# --------------------------------------------------------------------------- #
# identity: guest tenant ids + guest-session tokens
# --------------------------------------------------------------------------- #
def is_guest_tenant(tenant_id: str) -> bool:
    return str(tenant_id).startswith(GUEST_TENANT_PREFIX)


def mint_guest_tenant_id() -> str:
    """``guest-<12 hex>`` — slug-safe under the shared id rule (starts alnum,
    lowercase, within 63 chars)."""
    return GUEST_TENANT_PREFIX + secrets.token_hex(6)


def new_upload_drawing_id() -> str:
    """Mint a random guest fallback id with the public ``u-<10 hex>`` shape."""
    return "u-" + secrets.token_hex(5)


def new_account_upload_drawing_id() -> str:
    """Mint the canonical UUID used for a new account upload receipt."""
    return str(uuid.uuid4())


def derived_upload_drawing_id(tenant_id: str, data: bytes) -> str:
    """Content-derived id for GUEST uploads (same ``u-<10 hex>`` shape as the
    random mint): sha256(tenant : content). This is the upload idempotency
    key — a guest re-posting the SAME bytes lands on the SAME drawing, so an
    aborted upload whose receipt the client never saw is recovered by
    re-uploading instead of duplicated (FE review round 3, MAJOR)."""
    digest = hashlib.sha256(
        tenant_id.encode("utf-8") + b":" + hashlib.sha256(data).digest()).hexdigest()
    return "u-" + digest[:10]


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def mint_guest_session(tenant_id: str, expires_epoch: int) -> Optional[str]:
    """``<tenant_id>.<exp_epoch>.<hmac_hex>`` or None when no secret is set
    (auth-off demo: the X-Tenant-Id header stub is the identity instead)."""
    secret = guest_secret()
    if secret is None:
        return None
    payload = f"{tenant_id}.{int(expires_epoch)}"
    return f"{payload}.{_sign(payload, secret)}"


def verify_guest_session(token: str) -> Optional[str]:
    """Constant-time verify -> tenant_id, or None on ANY defect (malformed,
    expired, tampered, non-guest prefix, secret unset). Callers fall through to
    the ordinary auth path on None — never a permissive default."""
    secret = guest_secret()
    if secret is None or not isinstance(token, str):
        return None
    parts = token.rsplit(".", 2)
    if len(parts) != 3:
        return None
    tenant_id, exp_raw, sig = parts
    if not tenant_id.startswith(GUEST_TENANT_PREFIX):
        return None
    try:
        exp = int(exp_raw)
    except ValueError:
        return None
    expected = _sign(f"{tenant_id}.{exp}", secret)
    if not hmac.compare_digest(sig, expected):
        return None
    if time.time() >= exp:
        return None
    return tenant_id


# --------------------------------------------------------------------------- #
# guest rate limiting (cost exposure: each live DWG extraction is a paid APS
# run; a DXF is parsed locally by default — service CPU, not an APS charge, and
# dxf_intake is linear in input size (see its layer-dedup note) — but a paid APS
# run too under LEAF_GUEST_DXF_EXTRACT=aps, same caps as DWG)
# --------------------------------------------------------------------------- #
_RATE_LOCK = threading.Lock()
_RATE_STATE: Dict[str, Any] = {"day": None, "per_ip": {}, "total": 0}
_PG_COUNTER = None
_PG_CHARGE_RECEIPT: ContextVar[Optional[Tuple[str, str]]] = ContextVar(
    "guest_cap_charge_receipt", default=None)


class _GuestCapExceeded(Exception):
    def __init__(self, scope: str):
        super().__init__(scope)
        self.scope = scope


def _guest_cap_store() -> str:
    mode = os.environ.get("LEAF_GUEST_CAP_STORE", "memory").strip().lower()
    if mode not in {"memory", "postgres"}:
        raise RuntimeError(
            "LEAF_GUEST_CAP_STORE must be either 'memory' or 'postgres'")
    return mode


def _load_platform_counters():
    if "leaf_platform" not in sys.modules:
        package_dir = PROJECT_ROOT / "platform"
        spec = importlib.util.spec_from_file_location(
            "leaf_platform", package_dir / "__init__.py",
            submodule_search_locations=[str(package_dir)],
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("platform package could not be loaded")
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
        _PG_COUNTER = counter_type("guest_upload_counters")
    return _PG_COUNTER


def _rate_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _guest_cap_hmac_secret() -> str:
    secret = (os.environ.get("LEAF_GUEST_CAP_HMAC_SECRET")
              or guest_secret())
    if not secret:
        raise RuntimeError(
            "PostgreSQL guest caps require LEAF_GUEST_CAP_HMAC_SECRET "
            "or LEAF_GUEST_SECRET")
    return secret


def _guest_cap_retention_days() -> int:
    raw = os.environ.get("LEAF_GUEST_CAP_RETENTION_DAYS", "8")
    try:
        days = int(raw)
    except ValueError as exc:
        raise RuntimeError(
            "LEAF_GUEST_CAP_RETENTION_DAYS must be an integer") from exc
    if not 2 <= days <= 366:
        raise RuntimeError(
            "LEAF_GUEST_CAP_RETENTION_DAYS must be between 2 and 366")
    return days


def _postgres_counter_keys(client_ip: str) -> Tuple[str, str]:
    day = _rate_day()
    ip_digest = hmac.new(
        _guest_cap_hmac_secret().encode("utf-8"),
        str(client_ip or "unknown").encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return day, f"{day}:h1:{ip_digest}"


def _cleanup_old_postgres_counters(conn) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=_guest_cap_retention_days())
    conn.execute(
        """
        DELETE FROM guest_upload_counters
        WHERE ctid IN (
          SELECT ctid
          FROM guest_upload_counters
          WHERE updated_at < %(cutoff)s
          ORDER BY updated_at
          LIMIT 100
        )
        """,
        {"cutoff": cutoff},
    )


def _postgres_check_and_count(client_ip: str) -> Optional[str]:
    _PG_CHARGE_RECEIPT.set(None)
    db, _counter_type = _load_platform_counters()
    counter = _postgres_counter()
    day, ip_key = _postgres_counter_keys(client_ip)

    def consume(conn):
        ip_result = counter.consume_in_transaction(
            conn, namespace="guest_upload_ip", key=ip_key,
            limit=per_ip_daily_cap(),
        )
        if not ip_result.accepted:
            raise _GuestCapExceeded("ip")
        global_result = counter.consume_in_transaction(
            conn, namespace="guest_upload_global", key=day,
            limit=global_daily_cap(),
        )
        if not global_result.accepted:
            raise _GuestCapExceeded("global")
        _cleanup_old_postgres_counters(conn)

    try:
        db.run_transaction(consume, isolation="serializable", max_attempts=3)
    except _GuestCapExceeded as exc:
        return exc.scope
    _PG_CHARGE_RECEIPT.set((day, ip_key))
    return None


def _postgres_refund(client_ip: str) -> None:
    db, _counter_type = _load_platform_counters()
    receipt = _PG_CHARGE_RECEIPT.get()
    if receipt is None:
        raise RuntimeError(
            "cannot refund PostgreSQL guest cap without its charge receipt")
    day, ip_key = receipt

    def refund(conn):
        conn.execute(
            """
            UPDATE guest_upload_counters
            SET value = GREATEST(value - 1, 0), updated_at = NOW()
            WHERE namespace = %(namespace)s AND counter_key = %(key)s
            """,
            {"namespace": "guest_upload_ip", "key": ip_key},
        )
        conn.execute(
            """
            UPDATE guest_upload_counters
            SET value = GREATEST(value - 1, 0), updated_at = NOW()
            WHERE namespace = %(namespace)s AND counter_key = %(key)s
            """,
            {"namespace": "guest_upload_global", "key": day},
        )

    db.run_transaction(refund, isolation="serializable", max_attempts=3)
    _PG_CHARGE_RECEIPT.set(None)


def check_and_count_guest_upload(client_ip: str) -> Optional[str]:
    """Count one guest upload attempt. Returns None (allowed), "ip", or
    "global" naming the exceeded cap. Memory remains the legacy default.
    PostgreSQL is an explicit fleet-wide authority and never falls back."""
    if _guest_cap_store() == "postgres":
        return _postgres_check_and_count(client_ip)
    day = _rate_day()
    ip = str(client_ip or "unknown")
    with _RATE_LOCK:
        if _RATE_STATE["day"] != day:
            _RATE_STATE["day"] = day
            _RATE_STATE["per_ip"] = {}
            _RATE_STATE["total"] = 0
        if _RATE_STATE["total"] >= global_daily_cap():
            return "global"
        if _RATE_STATE["per_ip"].get(ip, 0) >= per_ip_daily_cap():
            return "ip"
        _RATE_STATE["per_ip"][ip] = _RATE_STATE["per_ip"].get(ip, 0) + 1
        _RATE_STATE["total"] += 1
        return None


def refund_guest_upload(client_ip: str) -> None:
    """Undo one check_and_count_guest_upload charge — used ONLY when the
    charged request then fails before any extraction starts (round-8 review:
    the visible-manifest replacement charges BEFORE its wipe so a rejected
    request never destroys readable data, and refunds if that wipe fails so
    the failure never burns a slot either). Floors at 0. A UTC-midnight
    rollover in the charge->refund microseconds can misdirect at most one
    count into the fresh day in legacy memory mode. PostgreSQL mode carries
    the exact charged day and HMAC key in a task-local receipt. A missing
    receipt or refund database error fails closed and leaves the slot consumed
    so a failed refund can never mint capacity."""
    if _guest_cap_store() == "postgres":
        _postgres_refund(client_ip)
        return
    ip = str(client_ip or "unknown")
    with _RATE_LOCK:
        if _RATE_STATE["per_ip"].get(ip, 0) > 0:
            _RATE_STATE["per_ip"][ip] -= 1
        if _RATE_STATE["total"] > 0:
            _RATE_STATE["total"] -= 1


def _reset_rate_state() -> None:  # test helper
    with _RATE_LOCK:
        _RATE_STATE["day"] = None
        _RATE_STATE["per_ip"] = {}
        _RATE_STATE["total"] = 0


# --------------------------------------------------------------------------- #
# upload marker — the single source of upload truth
# --------------------------------------------------------------------------- #
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def upload_store_mode() -> str:
    mode = os.environ.get("LEAF_UPLOAD_STORE", "legacy").strip().lower()
    if mode not in {"legacy", "postgres"}:
        raise RuntimeError("LEAF_UPLOAD_STORE must be 'legacy' or 'postgres'")
    if (
        mode == "postgres"
        and os.environ.get("LEAF_DRAWING_STORE", "legacy").strip().lower()
        != "postgres"
    ):
        raise RuntimeError(
            "LEAF_UPLOAD_STORE=postgres requires LEAF_DRAWING_STORE=postgres")
    return mode


def _drawing_db():
    import store  # write_loop installs da/ on sys.path
    return store._db()  # one pool and transaction policy for all drawing state


def _pg_marker_row(tenant_id: str, drawing_id: str):
    db = _drawing_db()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT marker, status FROM drawing_upload_attempts
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
            """,
            {"tenant": tenant_id, "drawing": drawing_id},
        )
        return cur.fetchone()


def read_marker(backend, tenant_id: str, drawing_id: str) -> Optional[Dict[str, Any]]:
    if upload_store_mode() == "postgres":
        row = _pg_marker_row(tenant_id, drawing_id)
        if row is None or row["status"] == "purged":
            return None
        return dict(row["marker"])
    key = write_loop.upload_marker_key(tenant_id, drawing_id)
    if not backend.exists(key):
        return None
    try:
        return json.loads(backend.get(key).decode("utf-8"))
    except (KeyError, ValueError):
        return None


def write_marker(backend, tenant_id: str, drawing_id: str, marker: Dict[str, Any]) -> None:
    if upload_store_mode() == "postgres":
        from psycopg.types.json import Jsonb
        db = _drawing_db()

        def operation(conn):
            row = conn.execute(
                """
                INSERT INTO drawing_upload_attempts
                  (tenant_id, drawing_id, attempt, marker, status,
                   retention_expires_at)
                VALUES
                  (%(tenant)s, %(drawing)s, %(attempt)s, %(marker)s, %(status)s,
                   %(retention)s)
                ON CONFLICT (tenant_id, drawing_id) DO UPDATE
                SET attempt = EXCLUDED.attempt, marker = EXCLUDED.marker,
                    status = EXCLUDED.status,
                    retention_expires_at = EXCLUDED.retention_expires_at,
                    extraction_owner = NULL,
                    extraction_expires_at = NULL, updated_at = NOW()
                WHERE drawing_upload_attempts.attempt = EXCLUDED.attempt
                   OR drawing_upload_attempts.status IN ('failed', 'purged')
                RETURNING attempt
                """,
                {
                    "tenant": tenant_id, "drawing": drawing_id,
                    "attempt": str(marker["attempt"]),
                    "marker": Jsonb(marker), "status": str(marker["status"]),
                    "retention": marker.get("retention_expires_at"),
                },
            ).fetchone()
            if row is None:
                raise RuntimeError("upload attempt was replaced or is being purged")

        db.run_transaction(operation, isolation="serializable")
        # Compatibility marker for the fail-closed bootstrap guard only.
        backend.put(
            write_loop.upload_marker_key(tenant_id, drawing_id),
            b'{"authority":"postgres"}',
        )
        return
    backend.put(write_loop.upload_marker_key(tenant_id, drawing_id),
                json.dumps(marker, separators=(",", ":")).encode("utf-8"))


def _claim_extraction(
    tenant_id: str, drawing_id: str,
) -> Optional[Tuple[Dict[str, Any], str, int]]:
    if upload_store_mode() != "postgres":
        marker = read_marker(None, tenant_id, drawing_id)
        return (marker, "", 0) if marker is not None else None
    owner = f"{os.getpid()}-{threading.get_ident()}-{secrets.token_hex(4)}"
    db = _drawing_db()

    def operation(conn):
        row = conn.execute(
            """
            UPDATE drawing_upload_attempts
            SET extraction_owner = %(owner)s,
                extraction_fence = extraction_fence + 1,
                extraction_expires_at =
                  NOW() + (%(ttl)s * INTERVAL '1 second'),
                updated_at = NOW()
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
              AND status = 'extracting'
              AND (extraction_expires_at IS NULL OR extraction_expires_at <= NOW())
            RETURNING marker, extraction_fence
            """,
            {
                "owner": owner, "ttl": extract_timeout_s(),
                "tenant": tenant_id, "drawing": drawing_id,
            },
        ).fetchone()
        if row is None:
            return None
        return dict(row["marker"]), owner, int(row["extraction_fence"])

    return db.run_transaction(operation, isolation="serializable")


def _release_pg_extraction_lease(
    tenant_id: str, drawing_id: str, attempt: str, owner: str, fence: int,
) -> bool:
    """Make a transient publication failure resumable by the same attempt."""
    db = _drawing_db()
    with db.connection() as conn:
        row = conn.execute(
            """
            UPDATE drawing_upload_attempts
            SET marker = jsonb_set(
                    jsonb_set(
                      marker,
                      '{finalize_retry_started_at}',
                      to_jsonb(clock_timestamp()::text),
                      true
                    ),
                    '{finalize_retry_count}',
                    to_jsonb(COALESCE((marker->>'finalize_retry_count')::int, 0) + 1),
                    true
                ),
                extraction_owner = NULL, extraction_expires_at = NULL,
                updated_at = NOW()
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
              AND attempt = %(attempt)s AND status = 'extracting'
              AND extraction_owner = %(owner)s
              AND extraction_fence = %(fence)s
            RETURNING attempt
            """,
            {
                "tenant": tenant_id,
                "drawing": drawing_id,
                "attempt": attempt,
                "owner": owner,
                "fence": fence,
            },
        ).fetchone()
    return row is not None


def _finish_pg_attempt(
    tenant_id: str, drawing_id: str, marker: Dict[str, Any],
    owner: str, fence: int,
) -> bool:
    from psycopg.types.json import Jsonb
    db = _drawing_db()
    with db.connection() as conn:
        row = conn.execute(
            """
            UPDATE drawing_upload_attempts
            SET marker = %(marker)s, status = %(status)s,
                extraction_owner = NULL, extraction_expires_at = NULL,
                updated_at = NOW()
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
              AND attempt = %(attempt)s AND status = 'extracting'
              AND extraction_owner = %(owner)s
              AND extraction_fence = %(fence)s
              AND extraction_expires_at > clock_timestamp()
            RETURNING attempt
            """,
            {
                "marker": Jsonb(marker), "status": str(marker["status"]),
                "tenant": tenant_id, "drawing": drawing_id,
                "attempt": str(marker["attempt"]), "owner": owner,
                "fence": fence,
            },
        ).fetchone()
    return row is not None


def _put_intake_cache_fenced(
    backend, tenant_id: str, drawing_id: str, key: str, data: bytes,
    attempt: str, owner: str, fence: int, version: int = 1,
) -> str:
    """Publish intake and persist its server-derived proof under the same fence."""
    db = _drawing_db()
    digest = hashlib.sha256(data).hexdigest()
    params = {
        "tenant": tenant_id, "drawing": drawing_id,
        "attempt": attempt, "owner": owner, "fence": fence,
        "version": int(version), "intake_ref": key, "intake_sha256": digest,
    }

    def operation(conn):
        row = conn.execute(
            """
            SELECT 1 FROM drawing_upload_attempts
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
              AND attempt = %(attempt)s AND status = 'extracting'
              AND extraction_owner = %(owner)s
              AND extraction_fence = %(fence)s
              AND extraction_expires_at > clock_timestamp()
            FOR UPDATE
            """,
            params,
        ).fetchone()
        if row is None:
            raise RuntimeError("upload extraction lease is stale")
        if backend.exists(key):
            existing = backend.get(key)
            if hashlib.sha256(existing).digest() != hashlib.sha256(data).digest():
                raise ValueError("intake cache artifact does not match reserved version")
        else:
            backend.put(key, data)
        version_row = conn.execute(
            """
            UPDATE drawing_store_versions
            SET intake_ref = %(intake_ref)s,
                intake_sha256 = %(intake_sha256)s
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
              AND version = %(version)s AND state = 'ready'
              AND (
                intake_ref IS NULL
                OR (intake_ref = %(intake_ref)s
                    AND intake_sha256 = %(intake_sha256)s)
              )
            RETURNING version
            """,
            params,
        ).fetchone()
        if version_row is None:
            raise ValueError("ready drawing version rejected intake proof")
        row = conn.execute(
            """
            SELECT 1 FROM drawing_upload_attempts
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
              AND attempt = %(attempt)s AND status = 'extracting'
              AND extraction_owner = %(owner)s
              AND extraction_fence = %(fence)s
              AND extraction_expires_at > clock_timestamp()
            """,
            params,
        ).fetchone()
        if row is None:
            raise RuntimeError(
                "upload extraction lease expired during intake publication")

    db.run_transaction(operation, isolation="serializable")
    return digest


def _finalize_pg_ready_attempt(
    backend, tenant_id: str, drawing_id: str, marker: Dict[str, Any],
    owner: str, fence: int, key: str, data: bytes, version: int = 1,
) -> str:
    """Publish cache, version visibility, and ready marker as one DB commit."""
    from psycopg.types.json import Jsonb

    db = _drawing_db()
    digest = hashlib.sha256(data).hexdigest()
    if (
        marker.get("status") != "ready"
        or marker.get("intake_ref") != key
        or marker.get("intake_sha256") != digest
    ):
        raise ValueError("ready upload marker does not match intake proof")
    params = {
        "tenant": tenant_id,
        "drawing": drawing_id,
        "attempt": str(marker["attempt"]),
        "owner": owner,
        "fence": fence,
        "version": int(version),
        "intake_ref": key,
        "intake_sha256": digest,
        "marker": Jsonb(marker),
        "status": str(marker["status"]),
    }

    backend.put_if_absent_or_verify(key, data)
    if hashlib.sha256(backend.get(key)).hexdigest() != digest:
        raise ValueError("intake cache artifact does not match reserved version")

    def operation(conn):
        row = conn.execute(
            """
            SELECT 1 FROM drawing_upload_attempts
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
              AND attempt = %(attempt)s AND status = 'extracting'
              AND extraction_owner = %(owner)s
              AND extraction_fence = %(fence)s
              AND extraction_expires_at > clock_timestamp()
            FOR UPDATE
            """,
            params,
        ).fetchone()
        if row is None:
            raise RuntimeError("upload extraction lease is stale")

        version_row = conn.execute(
            """
            UPDATE drawing_store_versions
            SET state = 'ready', ready_at = NOW(),
                reservation_token = NULL, reservation_expires_at = NULL,
                intake_ref = %(intake_ref)s,
                intake_sha256 = %(intake_sha256)s
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
              AND version = %(version)s AND state = 'reserved'
              AND reservation_token = %(attempt)s
            RETURNING version
            """,
            params,
        ).fetchone()
        if version_row is None:
            raise RuntimeError("reserved drawing version is not owned by upload attempt")

        manifest_row = conn.execute(
            """
            UPDATE drawing_store_manifests
            SET head = %(version)s, latest = %(version)s, updated_at = NOW()
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
              AND head IS NULL
            RETURNING head
            """,
            params,
        ).fetchone()
        if manifest_row is None:
            raise RuntimeError("drawing manifest is no longer reserved")

        attempt_row = conn.execute(
            """
            UPDATE drawing_upload_attempts
            SET marker = %(marker)s, status = %(status)s,
                extraction_owner = NULL, extraction_expires_at = NULL,
                updated_at = NOW()
            WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
              AND attempt = %(attempt)s AND status = 'extracting'
              AND extraction_owner = %(owner)s
              AND extraction_fence = %(fence)s
              AND extraction_expires_at > clock_timestamp()
            RETURNING attempt
            """,
            params,
        ).fetchone()
        if attempt_row is None:
            raise RuntimeError("upload extraction lease expired before ready commit")

    db.run_transaction(operation, isolation="serializable")
    try:
        backend.put(
            write_loop.upload_marker_key(tenant_id, drawing_id),
            b'{"authority":"postgres"}',
        )
    except Exception as exc:  # noqa: BLE001 - compatibility sentinel is not authority
        # Compatibility sentinel only. PostgreSQL is canonical after commit.
        write_loop.LOGGER.warning(
            "upload compatibility sentinel publication failed",
            extra={"error_type": type(exc).__name__},
        )
    return digest


def new_marker(*, filename: str, data: bytes, tenant_kind: str,
               source_ext: str = "") -> Dict[str, Any]:
    now = _now()
    expires = (_iso(now + timedelta(hours=retention_hours()))
               if tenant_kind == "guest" else None)
    return {
        "schema": 1,
        "status": "extracting",
        # Attempt generation (round-5 review, MAJOR): a failed-retry REPLACES
        # the marker with a fresh token; the old attempt's still-running
        # worker thread must then fence itself out instead of overwriting the
        # new attempt's state.
        "attempt": secrets.token_hex(8),
        "source_ext": source_ext,  # write_loop's live-write guard reads this
        "filename": str(filename),
        "bytes": len(data),
        "content_sha256": hashlib.sha256(data).hexdigest(),
        "uploaded_at": _iso(now),
        "retention_expires_at": expires,
        "tenant_kind": tenant_kind,
        "error": None,
        "extracted_version": None,
    }


# Per-drawing re-entrant critical sections shared by the upload route's
# marker+staging writes, the extraction ingest tail, _mark_failed, and the
# purge sweep's per-drawing delete (review round 4, both MAJORs): scoping the
# critical section to ONE drawing means an account tenant's slow live ingest
# can never serialize other uploads or delay the purge deadline of an
# unrelated guest drawing, while the SAME drawing's stage/ingest/fail/delete
# operations still can never interleave.
#
# REFCOUNTED (review round 5, MAJOR + MINOR): an entry exists only while some
# thread holds or awaits it, so (a) the registry stays bounded — account
# uploads no longer grow it forever — and (b) there is no eviction race: every
# acquire goes through the registry mutex, so a new caller either joins the
# entry a holder/waiter keeps alive or creates a fresh one only when nobody
# references the key. (The round-4 post-release eviction could hand two
# concurrent callers DIFFERENT lock objects for one key; content-derived
# guest ids are also re-mintable after purge, so eviction could not lean on
# "deleted ids never return" either.) Re-entrant: _mark_failed runs inside
# the extraction tail's held section.
class _KeyedLocks:
    def __init__(self) -> None:
        self._mu = threading.Lock()
        self._entries: Dict[Tuple[str, str], list] = {}  # key -> [RLock, refs]

    @contextlib.contextmanager
    def hold(self, key: Tuple[str, str]):
        with self._mu:
            entry = self._entries.get(key)
            if entry is None:
                entry = [threading.RLock(), 0]
                self._entries[key] = entry
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._mu:
                entry[1] -= 1
                if entry[1] <= 0:
                    self._entries.pop(key, None)


_DRAWING_LOCKS = _KeyedLocks()


def drawing_lock(tenant_id: str, drawing_id: str):
    """Context manager: the per-(tenant, drawing) critical section."""
    return _DRAWING_LOCKS.hold((tenant_id, drawing_id))


def guest_drawing_dir(tenant_id: str, drawing_id: str) -> Path:
    """The guest store's on-disk directory for one drawing (guest tenants are
    ALWAYS filesystem-backed — write_loop.backend_for_tenant)."""
    return (Path(write_loop.guest_store_dir()) / "tenants" / tenant_id
            / "drawings" / drawing_id)


def wipe_failed_attempt_residue(
    tenant_id: str, drawing_id: str, attempt: Optional[str] = None,
) -> bool:
    with write_loop.upload_mutation_commit_guard() as commit_enabled:
        if not commit_enabled:
            return False
        return _wipe_failed_attempt_residue(tenant_id, drawing_id, attempt)


def _wipe_failed_attempt_residue(
    tenant_id: str, drawing_id: str, attempt: Optional[str] = None,
) -> bool:
    """Delete a failed attempt's ingest residue (manifest, version blobs,
    intake cache — everything under the drawing dir EXCEPT the upload
    marker), then VERIFY the deletion (round-6 review, MAJOR: an unchecked
    rmtree that partially fails would either wedge the derived id behind
    version residue or, if the manifest survived, silently break same-id
    retry semantics). The marker survives any partial failure ON PURPOSE:
    it is what routes the next retry back into the replace path — the
    caller overwrites it after a True return, and never touches residue
    behind a False."""
    if upload_store_mode() == "postgres":
        if not attempt:
            return False
        db = _drawing_db()
        owner = f"reset-{os.getpid()}-{threading.get_ident()}-{secrets.token_hex(4)}"

        def claim(conn):
            row = conn.execute(
                """
                UPDATE drawing_upload_attempts
                SET status = 'purging', purge_owner = %(owner)s,
                    purge_fence = purge_fence + 1,
                    purge_expires_at = clock_timestamp() + INTERVAL '5 minutes',
                    updated_at = NOW()
                WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
                  AND attempt = %(attempt)s
                  AND (
                    status = 'failed'
                    OR (status = 'purging'
                        AND purge_expires_at <= clock_timestamp())
                  )
                RETURNING purge_fence
                """,
                {
                    "owner": owner, "tenant": tenant_id,
                    "drawing": drawing_id, "attempt": attempt,
                },
            ).fetchone()
            return int(row["purge_fence"]) if row is not None else None

        fence = db.run_transaction(claim, isolation="serializable")
        if fence is None:
            return False
        ok = _wipe_failed_attempt_files(tenant_id, drawing_id)

        def finish(conn):
            row = conn.execute(
                """
                SELECT 1 FROM drawing_upload_attempts
                WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
                  AND attempt = %(attempt)s AND status = 'purging'
                  AND purge_owner = %(owner)s AND purge_fence = %(fence)s
                FOR UPDATE
                """,
                {
                    "tenant": tenant_id, "drawing": drawing_id,
                    "attempt": attempt, "owner": owner, "fence": fence,
                },
            ).fetchone()
            if row is None:
                return False
            if ok:
                conn.execute(
                    """
                    DELETE FROM drawing_store_manifests
                    WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
                    """,
                    {"tenant": tenant_id, "drawing": drawing_id},
                )
            conn.execute(
                """
                UPDATE drawing_upload_attempts
                SET status = 'failed', purge_owner = NULL,
                    purge_expires_at = NULL, updated_at = NOW()
                WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
                  AND attempt = %(attempt)s AND purge_fence = %(fence)s
                """,
                {
                    "tenant": tenant_id, "drawing": drawing_id,
                    "attempt": attempt, "fence": fence,
                },
            )
            return ok

        return bool(db.run_transaction(finish, isolation="serializable"))
    return _wipe_failed_attempt_files(tenant_id, drawing_id)


def _wipe_failed_attempt_files(tenant_id: str, drawing_id: str) -> bool:
    # NEVER raises: every filesystem error (enumeration included) is
    # contained to the False return, so False is the ONE unsuccessful
    # outcome and the caller's refund/500 handling cannot be skipped by an
    # exception path (round-9 review, MAJOR).
    try:
        root = guest_drawing_dir(tenant_id, drawing_id)
        if not root.is_dir():
            return True
        ok = True
        for child in root.iterdir():
            if child.name == "upload.state.json":
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                _unlink_quiet(child)
            ok = ok and not child.exists()
        return ok
    except OSError:
        return False


def _mark_failed(backend, tenant_id: str, drawing_id: str, marker: Dict[str, Any],
                 error_code: str, message: str, retryable: bool,
                 *, extraction_owner: str = "", extraction_fence: int = 0) -> bool:
    with write_loop.upload_mutation_commit_guard() as commit_enabled:
        if not commit_enabled:
            return False
        return _mark_failed_committed(
            backend, tenant_id, drawing_id, marker, error_code, message,
            retryable, extraction_owner=extraction_owner,
            extraction_fence=extraction_fence,
        )


def _mark_failed_committed(
    backend, tenant_id: str, drawing_id: str, marker: Dict[str, Any],
    error_code: str, message: str, retryable: bool,
    *, extraction_owner: str = "", extraction_fence: int = 0,
) -> bool:
    # Under the drawing's lock, and only onto a marker that still IS this
    # attempt AND is still non-terminal: a purged drawing must not get a
    # marker resurrected behind its deletion receipt; a failed-retry's
    # REPLACEMENT marker (new attempt token) must not be overwritten by the
    # old attempt's late failure (round-5 review, MAJOR); and a committed
    # terminal state must never be demoted to failed by a twin worker's late
    # error or a caller holding a stale status snapshot (round-6 review,
    # MAJOR). Returns whether the failure record was actually written, so
    # callers reporting state can tell a persisted failure from a lost race.
    if upload_store_mode() == "postgres":
        failed = dict(marker)
        failed["status"] = "failed"
        failed["error"] = {
            "error_code": error_code, "message": message,
            "retryable": bool(retryable),
        }
        if extraction_owner:
            return _finish_pg_attempt(
                tenant_id, drawing_id, failed,
                extraction_owner, extraction_fence)
        from psycopg.types.json import Jsonb
        db = _drawing_db()
        with db.connection() as conn:
            row = conn.execute(
                """
                UPDATE drawing_upload_attempts
                SET marker = %(marker)s, status = 'failed',
                    extraction_owner = NULL, extraction_expires_at = NULL,
                    updated_at = NOW()
                WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
                  AND attempt = %(attempt)s AND status = 'extracting'
                  AND (extraction_expires_at IS NULL
                       OR extraction_expires_at <= NOW())
                RETURNING attempt
                """,
                {
                    "marker": Jsonb(failed), "tenant": tenant_id,
                    "drawing": drawing_id, "attempt": str(marker["attempt"]),
                },
            ).fetchone()
        return row is not None
    import store  # write_loop installs da/ on sys.path
    # `must_exist=False`, because a failure recorded BEFORE the ingest ever ran
    # has no manifest and refusing it would lose every pre-ingest error. The
    # evidence this brings instead is the marker, re-read INSIDE the section:
    # the purge deletes it with the rest of the drawing, so a vanished marker
    # means the receipt has already been written and this failure has nowhere to
    # go. Read outside the section (as it was) the answer goes stale while the
    # lock is waited for, and `write_marker` then recreates the drawing
    # directory behind that receipt.
    with drawing_lock(tenant_id, drawing_id), \
            store.legacy_drawing_guard(backend, tenant_id, drawing_id,
                                       must_exist=False):
        current = read_marker(backend, tenant_id, drawing_id)
        if current is None:
            return False
        if current.get("attempt") != marker.get("attempt"):
            return False
        if current.get("status") != "extracting":
            return False
        marker = dict(marker)
        marker["status"] = "failed"
        marker["error"] = {"error_code": error_code, "message": message,
                           "retryable": bool(retryable)}
        write_marker(backend, tenant_id, drawing_id, marker)
        return True


# --------------------------------------------------------------------------- #
# extraction — real geometry or an honest failure, never the demo intake
# --------------------------------------------------------------------------- #
def staged_path(tenant_id: str, drawing_id: str, ext: str) -> Path:
    """Tenant-BOUND staging name (`<tenant>--<drawing><ext>`): the broker's
    upload resolver rebuilds this exact name from the request's tenant_id, so
    a broker-authorized caller can only ever extract its OWN tenant's staged
    bytes — knowing another tenant's drawing id is not enough (review round 1,
    MAJOR: flat namespace had no tenant binding)."""
    return uploads_dir() / f"{tenant_id}--{drawing_id}{ext}"


def _verify_staged_source(
    tenant_id: str, drawing_id: str, ext: str, marker: Dict[str, Any],
) -> Path:
    """Bind extraction to the exact bytes reserved by the upload marker."""
    path = staged_path(tenant_id, drawing_id, ext)
    data = path.read_bytes()
    expected_bytes = marker.get("bytes")
    expected_digest = marker.get("content_sha256")
    if (
        isinstance(expected_bytes, bool)
        or not isinstance(expected_bytes, int)
        or expected_bytes != len(data)
        or not isinstance(expected_digest, str)
        or not hmac.compare_digest(
            expected_digest, hashlib.sha256(data).hexdigest())
    ):
        raise ValueError(
            "staged upload does not match its reserved source bytes")
    return path


def run_extraction(tenant_id: str, drawing_id: str, ext: str) -> None:
    with write_loop.upload_mutation_commit_guard() as commit_enabled:
        if not commit_enabled:
            return
        _run_extraction(tenant_id, drawing_id, ext)


def _run_extraction(tenant_id: str, drawing_id: str, ext: str) -> None:
    """Synchronous extraction body (the thread target calls this; tests call it
    directly). Reads the staged file, produces intake, ingests it as v1 under
    the tenant's backend, and transitions the marker. NEVER raises out — every
    failure path lands in the marker as an honest §10 error object."""
    import deps  # lazy: call-time APS_LIVE so tests can monkeypatch

    backend = write_loop.upload_backend_for_tenant(tenant_id)
    extraction_owner, extraction_fence = "", 0
    if not write_loop.fence_open():
        return
    if upload_store_mode() == "postgres":
        claim = _claim_extraction(tenant_id, drawing_id)
        if claim is None:
            return
        marker, extraction_owner, extraction_fence = claim
    else:
        marker = read_marker(backend, tenant_id, drawing_id)
        if marker is None:
            return  # purged mid-flight; nothing to report against

    try:
        source_path = _verify_staged_source(
            tenant_id, drawing_id, ext, marker)
        if ext == ".dxf" and _dxf_extract_mode() == "aps" and deps.APS_LIVE:
            # FULL-FIDELITY DXF via APS. Reachable only when the DXF-correct
            # Activity (da.client.EXTRACT_DXF_ACTIVITY, localName `input.dxf`
            # + save-safe QUIT) is provisioned; the broker selects it by the
            # staged file's .dxf extension. This is the SAME LISP extract the
            # DWG path runs, so it recovers INSERT / 3DFACE / geo / xdata that
            # the local parser cannot. It also costs a paid APS run per upload,
            # exactly like the DWG path, and is bounded by the same guest rate
            # caps — hence a deliberate opt-in flag rather than the default.
            intake = _extract_via_broker(
                tenant_id, drawing_id, str(marker.get("attempt") or ""))
        elif ext == ".dxf":
            # DEFAULT DXF path: parse locally (free, instant, always available).
            # The live APS extract Activity for DWG declares HostDwg with a
            # fixed `input.dwg` localName, so DXF bytes sent there arrive
            # wearing a .dwg extension and accoreconsole rejects them
            # ("Drawing file is not valid", ErrorStatus=434). The DXF-correct
            # Activity above lifts that, but until it is provisioned AND the
            # operator opts in (LEAF_GUEST_DXF_EXTRACT=aps), the local parser
            # is the honest path — and it is what policy_view() advertises as
            # `dxf_local_ok: True`. Fidelity: LWPOLYLINE/POLYLINE + layer names
            # only, less than the APS extract's INSERT/3DFACE/geo/xdata.
            import dxf_intake
            intake = dxf_intake.parse_dxf_file(source_path,
                                               source_name=marker.get("filename") or drawing_id)
        elif deps.APS_LIVE:
            intake = _extract_via_broker(
                tenant_id, drawing_id, str(marker.get("attempt") or ""))
        else:
            _mark_failed(backend, tenant_id, drawing_id, marker, "APS_UNAVAILABLE",
                         "DWG extraction requires the live APS path; "
                         "upload a DXF to try the local demo", retryable=False,
                         extraction_owner=extraction_owner,
                         extraction_fence=extraction_fence)
            return
    except _ExtractError as exc:
        _mark_failed(backend, tenant_id, drawing_id, marker,
                     exc.error_code, exc.message, exc.retryable,
                     extraction_owner=extraction_owner,
                     extraction_fence=extraction_fence)
        return
    except Exception as exc:  # noqa: BLE001 — thread boundary, must not leak
        _mark_failed(backend, tenant_id, drawing_id, marker, "INTERNAL",
                     f"{type(exc).__name__}: {exc}", retryable=False,
                     extraction_owner=extraction_owner,
                     extraction_fence=extraction_fence)
        return

    # Ingest v1 in the FORMAT-CORRECT representation (review rounds 1+2):
    #   * .dwg uploads: the version blob holds the user's RAW DWG bytes —
    #     exactly what a later live write signs and sends to APS as HostDwg.
    #   * .dxf uploads: the version blob holds the intake JSON — the SAME
    #     mock representation the demo drawing itself uses (a raw DXF under
    #     the store's immutable `.dwg` version key would hand APS a
    #     mislabeled file on the first live write; intake-JSON blobs are the
    #     established local shape and live writes on them fail the same
    #     honest way they do for the demo drawing).
    # Either way the parsed intake ALSO goes to the sibling intake-cache key,
    # which write_loop.read_intake prefers — one uniform read path.
    # ingest_drawing refuses an existing drawing, which is correct: an upload
    # id is minted fresh and collision-checked.
    #
    # The whole ingest runs INSIDE this drawing's lock, with a marker
    # re-check first: extraction can be slow, and the purge sweep may have
    # deleted this drawing (and logged its receipt) mid-flight — ingesting
    # after that would resurrect purged data behind a "deleted" receipt
    # (review round 3, MAJOR). A vanished marker means purged: abort, no
    # trace rewritten.
    import tempfile
    authority_lock = (
        contextlib.nullcontext() if upload_store_mode() == "postgres"
        else drawing_lock(tenant_id, drawing_id)
    )
    with authority_lock, write_loop.upload_mutation_commit_guard() as commit_enabled:
        if not commit_enabled:
            # The cutover fence protects every canonical commit, including
            # marker transitions.  Leave the attempt unchanged so the
            # operator can recover or retry it after the drain.
            return
        current = read_marker(backend, tenant_id, drawing_id)
        if current is None:
            return  # purged while extracting; the receipt stands, nothing returns
        if current.get("attempt") != marker.get("attempt"):
            return  # a failed-retry REPLACED this attempt; its worker owns the
            # drawing now — ingesting here would overwrite the new attempt
            # (round-5 review, MAJOR)
        if current.get("status") != "extracting":
            return  # already terminal: a twin worker of the SAME attempt (a
            # late-starting thread that read the replacement marker at entry)
            # finished first, or the timeout was PERSISTED as failed — a
            # second ingest would trip the already-exists refusal and flip a
            # good drawing to failed, and a post-timeout success would
            # destabilize the contract's persisted terminal state
        try:
            import store  # importable via write_loop's sys.path setup
            authority_guard = None
            if upload_store_mode() == "postgres":
                authority_guard = {
                    "attempt": marker["attempt"],
                    "owner": extraction_owner,
                    "fence": extraction_fence,
                    "defer_ready": True,
                }
            # The same three checks made above, re-made INSIDE the store's
            # per-drawing lock. Above they are a read with an unbounded wait
            # after them: the purge sweep runs in a SECOND process too, and
            # between that read and this ingest it can delete the drawing and
            # write its "deleted" receipt, after which creating version 1 here
            # would resurrect the upload behind a receipt saying it was gone
            # (the in-process half of this was round 3, MAJOR; the store guard
            # is what extends it across processes). A vanished or replaced
            # marker means the work is no longer ours to commit.
            def _still_our_attempt() -> bool:
                live = read_marker(backend, tenant_id, drawing_id)
                return (live is not None
                        and live.get("attempt") == marker.get("attempt")
                        and live.get("status") == "extracting")

            # Legacy authority only; the postgres path settles the same question
            # with row-level authority, and `ingest_drawing` refuses both at
            # once. Keyed on the STORE's authority mode, which is what
            # `ingest_drawing` actually branches on — `upload_store_mode` is a
            # different setting and the two can disagree.
            precondition = (None if store.authority_mode() == "postgres"
                            else _still_our_attempt)
            try:
                if ext == ".dwg":
                    store.ingest_drawing(backend, tenant_id,
                                         str(staged_path(tenant_id, drawing_id, ext)),
                                         drawing_id=drawing_id,
                                         authority_guard=authority_guard,
                                         precondition=precondition)
                else:
                    fd, tmp = tempfile.mkstemp(suffix=".intake.json")
                    try:
                        with os.fdopen(fd, "w", encoding="utf-8") as fh:
                            json.dump(intake, fh)
                        store.ingest_drawing(
                            backend, tenant_id, tmp, drawing_id=drawing_id,
                            authority_guard=authority_guard,
                            precondition=precondition)
                    finally:
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
            except store.DrawingVanished:
                # Purged mid-extraction. Return without writing ANYTHING: the
                # receipt stands, and marking the attempt failed would itself
                # recreate the drawing directory this just confirmed is gone.
                return
            cache_key = write_loop.intake_cache_key(tenant_id, drawing_id, 1)
            cache_data = json.dumps(
                intake, separators=(",", ":")).encode("utf-8")
            if upload_store_mode() == "postgres":
                ready_marker = dict(marker)
                ready_marker["status"] = "ready"
                ready_marker["extracted_version"] = 1
                ready_marker["intake_ref"] = cache_key
                ready_marker["intake_sha256"] = hashlib.sha256(
                    cache_data).hexdigest()
                _finalize_pg_ready_attempt(
                    backend, tenant_id, drawing_id, ready_marker,
                    extraction_owner, extraction_fence, cache_key, cache_data)
            else:
                # Guarded for the same reason the ingest is, and it is a
                # SEPARATE window: `ingest_drawing` released the shared lock
                # when it returned, so a second-process purge can delete the
                # drawing and write its receipt before this line runs. The
                # intake cache is an ordinary `put`, which recreates missing
                # parents, so writing it unguarded rebuilds the drawing
                # directory behind a "deleted" receipt just as a manifest save
                # would. A purged drawing has no manifest, so the guard raises
                # and this attempt stops without leaving a trace.
                try:
                    with store.legacy_drawing_guard(backend, tenant_id, drawing_id):
                        backend.put(cache_key, cache_data)
                except KeyError:
                    return  # purged between the ingest and the cache write
                intake_sha256 = hashlib.sha256(cache_data).hexdigest()
        except ValueError as exc:
            _mark_failed(backend, tenant_id, drawing_id, marker, "INTERNAL",
                         f"ingest failed: {exc}", retryable=False,
                         extraction_owner=extraction_owner,
                         extraction_fence=extraction_fence)
            return
        except Exception as exc:  # noqa: BLE001 - classify DB/storage failures
            transient = isinstance(exc, (OSError, RuntimeError))
            if upload_store_mode() == "postgres":
                try:
                    import psycopg
                    transient = transient or isinstance(exc, psycopg.Error)
                except ImportError:
                    pass
            if upload_store_mode() == "postgres" and transient:
                try:
                    current = read_marker(backend, tenant_id, drawing_id)
                except Exception:  # DB response may itself be unavailable
                    current = None
                if current is not None and current.get("status") == "ready":
                    return
                _release_pg_extraction_lease(
                    tenant_id, drawing_id, str(marker["attempt"]),
                    extraction_owner, extraction_fence)
                return
            write_loop.LOGGER.error(
                "upload extraction failed with a non-transient defect",
                extra={"error_type": type(exc).__name__},
            )
            _mark_failed(
                backend, tenant_id, drawing_id, marker, "INTERNAL",
                "ingest failed during canonical finalization", retryable=False,
                extraction_owner=extraction_owner,
                extraction_fence=extraction_fence,
            )
            return

        if upload_store_mode() == "postgres":
            return
        marker = dict(marker)
        marker["status"] = "ready"
        marker["extracted_version"] = 1
        marker["intake_ref"] = cache_key
        marker["intake_sha256"] = intake_sha256
        # The LAST window, and the worst one to leave open: this marker is what
        # makes the upload readable, so recreating a purged drawing's directory
        # here republishes it as ready with no manifest and no version blob
        # behind it. Guarded like the cache write above.
        try:
            with store.legacy_drawing_guard(backend, tenant_id, drawing_id):
                write_marker(backend, tenant_id, drawing_id, marker)
        except KeyError:
            return  # purged before this attempt could publish; receipt stands


class _ExtractError(Exception):
    def __init__(self, error_code: str, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.retryable = retryable


def _extract_via_broker(
    tenant_id: str, drawing_id: str, attempt: str
) -> Dict[str, Any]:
    """POST /broker/extract {upload: true} — the SAME credential boundary as
    every other APS operation (the app process never reads the credential)."""
    try:
        resp = requests.post(
            f"{broker_client.broker_url()}/broker/extract",
            json={
                "tenant_id": tenant_id,
                "dwg": drawing_id,
                "upload": True,
                "ledger_event_key": broker_client.extract_event_key(
                    tenant_id, drawing_id, upload=True, attempt=attempt),
            },
            headers=broker_client.broker_headers(),
            timeout=extract_timeout_s(),
        )
        body = resp.json()
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise _ExtractError("BROKER_UNREACHABLE",
                            f"APS broker extraction failed: {exc}", True) from exc
    except ValueError as exc:
        raise _ExtractError("BROKER_UNREACHABLE",
                            f"broker returned non-JSON: {exc}", True) from exc
    if resp.status_code >= 400 or not isinstance(body, dict) or "intake" not in body:
        err = (body.get("error") if isinstance(body, dict) else None) or {}
        raise _ExtractError(
            str(err.get("error_code") or "WORKITEM_FAILED"),
            str(err.get("message") or f"extraction failed (HTTP {resp.status_code})"),
            bool(err.get("retryable", True)))
    return body["intake"]


def start_extraction_thread(tenant_id: str, drawing_id: str, ext: str) -> threading.Thread:
    t = threading.Thread(target=run_extraction, args=(tenant_id, drawing_id, ext),
                         name=f"guest-extract-{drawing_id}", daemon=True)
    t.start()
    return t


# --------------------------------------------------------------------------- #
# status view (marker -> response body; stale ``extracting`` becomes honest)
# --------------------------------------------------------------------------- #
def status_view(backend, tenant_id: str, drawing_id: str) -> Optional[Dict[str, Any]]:
    marker = read_marker(backend, tenant_id, drawing_id)
    if marker is None:
        return None
    status = marker.get("status")
    error = marker.get("error")
    if status == "extracting":
        retry_count = int(marker.get("finalize_retry_count") or 0)
        try:
            started = datetime.fromisoformat(str(
                marker.get("finalize_retry_started_at")
                or marker.get("uploaded_at")
            ))
            stale = (
                retry_count >= 3
                or (_now() - started).total_seconds() > extract_timeout_s()
            )
        except ValueError:
            stale = True
        if not stale and upload_store_mode() == "postgres":
            source_ext = str(marker.get("source_ext") or "").lower()
            if source_ext not in {".dwg", ".dxf"}:
                source_ext = Path(str(marker.get("filename") or "")).suffix.lower()
            if source_ext in {".dwg", ".dxf"}:
                # Claim fencing makes duplicate poll-triggered workers cheap.
                # A released transient failure resumes the same attempt and
                # therefore replays the same broker event key.
                start_extraction_thread(tenant_id, drawing_id, source_ext)
        elif stale:
            # Persist the honest terminal state so the surface is stable —
            # an "extracting" that outlived its budget is a failure, not a
            # forever-spinner. If the persist LOSES the race (extraction
            # committed ready between our read and the locked write), report
            # the marker's true state instead of a failure this view merely
            # imagined (round-6 review, MAJOR).
            if _mark_failed(backend, tenant_id, drawing_id, marker, "TIMEOUT",
                            "extraction did not finish within the time budget",
                            retryable=True):
                status = "failed"
                error = {"error_code": "TIMEOUT",
                         "message": "extraction did not finish within the time budget",
                         "retryable": True}
            else:
                current = read_marker(backend, tenant_id, drawing_id)
                if current is None:
                    return None  # purged under us — honest 404 upstream
                # Adopt the WHOLE re-read marker, not just status/error: the
                # response below also serves filename/uploaded_at/retention,
                # and splicing the new status onto the stale snapshot's
                # metadata would hand out an incoherent hybrid (round-7
                # review, MINOR).
                marker = current
                status = current.get("status")
                error = current.get("error")
    return {
        "drawing_id": drawing_id,
        "status": status,
        "error": error,
        "filename": marker.get("filename"),
        "uploaded_at": marker.get("uploaded_at"),
        "retention_expires_at": marker.get("retention_expires_at"),
        "tenant_kind": marker.get("tenant_kind"),
        "extracted_version": marker.get("extracted_version"),
    }


# --------------------------------------------------------------------------- #
# purge — the promise-keeper. Deletes at the STAMPED expiry, logs every kill.
# --------------------------------------------------------------------------- #
def _purge_log_path() -> Path:
    return Path(write_loop.guest_store_dir()) / "purge.log.jsonl"


def _purge_expired_postgres(now: datetime) -> Dict[str, Any]:
    """Lease each expired upload, delete its blobs, then persist one receipt."""
    from psycopg.types.json import Jsonb
    db = _drawing_db()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT tenant_id, drawing_id
            FROM drawing_upload_attempts
            WHERE status <> 'purged'
              AND (status <> 'purging'
                   OR purge_expires_at <= clock_timestamp())
              AND retention_expires_at IS NOT NULL
              AND retention_expires_at <= %(now)s
            ORDER BY tenant_id, drawing_id
            """,
            {"now": now},
        )
        candidates = [(row["tenant_id"], row["drawing_id"])
                      for row in cur.fetchall()]
    purged, freed = [], 0
    owner = f"{os.getpid()}-{threading.get_ident()}-{secrets.token_hex(4)}"
    for tenant_id, drawing_id in candidates:
        def claim(conn):
            locked = conn.execute(
                """
                SELECT attempt, marker
                FROM drawing_upload_attempts
                WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
                FOR UPDATE
                """,
                {
                    "tenant": tenant_id, "drawing": drawing_id, "now": now,
                },
            ).fetchone()
            if locked is None:
                return None
            state = conn.execute(
                """
                SELECT status,
                       retention_expires_at <= %(now)s AS retention_due,
                       purge_expires_at IS NULL
                         OR purge_expires_at <= clock_timestamp()
                         AS purge_available,
                       extraction_expires_at IS NULL
                         OR extraction_expires_at <= clock_timestamp()
                         AS extraction_available
                FROM drawing_upload_attempts
                WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
                """,
                {
                    "tenant": tenant_id, "drawing": drawing_id, "now": now,
                },
            ).fetchone()
            if (
                state["status"] == "purged"
                or state["retention_due"] is not True
                or (
                    state["status"] == "purging"
                    and state["purge_available"] is not True
                )
                or (
                    state["status"] == "extracting"
                    and state["extraction_available"] is not True
                )
            ):
                return None
            row = conn.execute(
                """
                UPDATE drawing_upload_attempts
                SET status = 'purging', purge_owner = %(owner)s,
                    purge_fence = purge_fence + 1,
                    purge_expires_at =
                      clock_timestamp() + INTERVAL '5 minutes',
                    updated_at = NOW()
                WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
                RETURNING attempt, marker, purge_fence
                """,
                {
                    "owner": owner, "tenant": tenant_id,
                    "drawing": drawing_id,
                },
            ).fetchone()
            return (
                str(row["attempt"]), dict(row["marker"]),
                int(row["purge_fence"]),
            )

        lease = db.run_transaction(claim, isolation="serializable")
        if lease is None:
            continue
        attempt, marker, fence = lease
        drawing_dir = guest_drawing_dir(tenant_id, drawing_id)
        staged_files = [
            staged_path(tenant_id, drawing_id, ext)
            for ext in ACCEPTED_EXTENSIONS
        ]
        try:
            staged_bytes = sum(
                path.stat().st_size for path in staged_files if path.is_file())
            drawing_bytes = sum(
                path.stat().st_size for path in drawing_dir.rglob("*")
                if path.is_file()) if drawing_dir.is_dir() else 0
            for path in staged_files:
                _unlink_quiet(path)
            if not any(path.exists() for path in staged_files):
                shutil.rmtree(drawing_dir, ignore_errors=True)
            deleted = (
                not drawing_dir.exists()
                and not any(path.exists() for path in staged_files)
            )
        except OSError:
            deleted, staged_bytes, drawing_bytes = False, 0, 0
        freed_bytes = staged_bytes + drawing_bytes if deleted else 0

        def finish(conn):
            locked = conn.execute(
                """
                SELECT marker FROM drawing_upload_attempts
                WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
                  AND status = 'purging' AND purge_owner = %(owner)s
                  AND purge_fence = %(fence)s
                FOR UPDATE
                """,
                {
                    "tenant": tenant_id, "drawing": drawing_id,
                    "owner": owner, "fence": fence,
                },
            ).fetchone()
            if locked is None:
                return False
            status = "deleted" if deleted else "failed"
            conn.execute(
                """
                INSERT INTO drawing_purge_receipts
                  (tenant_id, drawing_id, attempt, purge_fence, status,
                   freed_bytes, detail)
                VALUES
                  (%(tenant)s, %(drawing)s, %(attempt)s, %(fence)s,
                   %(status)s, %(freed)s, %(detail)s)
                ON CONFLICT DO NOTHING
                """,
                {
                    "tenant": tenant_id, "drawing": drawing_id,
                    "attempt": attempt, "fence": fence, "status": status,
                    "freed": freed_bytes, "detail": Jsonb({}),
                },
            )
            if deleted:
                conn.execute(
                    """
                    DELETE FROM drawing_store_manifests
                    WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
                    """,
                    {"tenant": tenant_id, "drawing": drawing_id},
                )
                conn.execute(
                    """
                    UPDATE drawing_upload_attempts
                    SET status = 'purged', purge_owner = NULL,
                        purge_expires_at = NULL, updated_at = NOW()
                    WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
                    """,
                    {"tenant": tenant_id, "drawing": drawing_id},
                )
            else:
                old_status = str(dict(locked["marker"]).get("status", "failed"))
                conn.execute(
                    """
                    UPDATE drawing_upload_attempts
                    SET status = %(status)s, purge_owner = NULL,
                        purge_expires_at = NULL, updated_at = NOW()
                    WHERE tenant_id = %(tenant)s AND drawing_id = %(drawing)s
                    """,
                    {
                        "status": old_status, "tenant": tenant_id,
                        "drawing": drawing_id,
                    },
                )
            return True

        if not db.run_transaction(finish, isolation="serializable"):
            continue
        receipt = {
            "ts": _iso(now), "status": "deleted" if deleted else "failed",
            "tenant_id": tenant_id, "drawing_id": drawing_id,
        }
        if deleted:
            receipt["freed_bytes"] = freed_bytes
            purged.append({"tenant_id": tenant_id, "drawing_id": drawing_id})
            freed += freed_bytes
        _append_purge_log(receipt)
    return {"count": len(purged), "freed_bytes": freed, "purged": purged}


@contextlib.contextmanager
def _store_checkout_guard_for_purge(tenant_id: str, drawing_id: str):
    """Hold the STORE's per-drawing checkout guard across one deletion.

    The purge's own `drawing_lock` settles this process's extraction threads and
    nothing else: it is a dict on one interpreter's heap, so a second app replica
    (or the cron entry point at the bottom of this module) never sees it. Every
    legacy manifest writer is a load, edit, save, and `FilesystemBackend.put`
    recreates missing parents, so without a lock the two processes SHARE, a save
    already in flight put the drawing back after this sweep had written its
    "deleted" line -- a purge receipt that is false, which is the one thing this
    module exists to prevent. `store.legacy_purge_guard` is that shared lock: the
    same one `acquire_checkout_fence`, `release_checkout`, `put_drawing`, `undo`,
    `redo` and `ingest_drawing` take.

    Taken INSIDE `drawing_lock`, matching `run_extraction` (`drawing_lock` ->
    `ingest_drawing` -> the store guard). The store never reaches back into this
    module, so the order cannot invert.

    Guest drawings are always filesystem-backed (`write_loop.guest_store_dir`),
    which is what makes an OS-level lock available here at all.

    A directory whose name the store's id rule REJECTS gets no guard, and needs
    none: the store could not have written that key, so no legacy writer can be
    racing it. Yielding None rather than skipping the drawing keeps such residue
    collectable instead of immortal.
    """
    import store  # write_loop installs da/ on sys.path
    try:
        store.sanitize_id(tenant_id)
        store.sanitize_id(drawing_id)
    except Exception:  # noqa: BLE001 - any rejection means "not a store key"
        yield None
        return
    backend = store.FilesystemBackend(write_loop.guest_store_dir())
    with store.legacy_purge_guard(backend, tenant_id, drawing_id) as held:
        yield held


def purge_expired(now: Optional[datetime] = None) -> Dict[str, Any]:
    with write_loop.upload_mutation_commit_guard() as commit_enabled:
        if not commit_enabled:
            return {"count": 0, "freed_bytes": 0, "purged": []}
        return _purge_expired(now)


def _purge_expired(now: Optional[datetime] = None) -> Dict[str, Any]:
    """Delete every guest drawing whose STAMPED retention_expires_at has passed
    (plus its staged upload file), drop empty tenant dirs, and append one
    purge.log.jsonl line per deletion. Idempotent; safe to run concurrently
    with uploads (a drawing uploaded after the walk simply survives to the
    next sweep). Returns {count, freed_bytes, purged:[{tenant_id, drawing_id}]}."""
    now = now or _now()
    if upload_store_mode() == "postgres":
        return _purge_expired_postgres(now)
    import store  # write_loop installs da/ on sys.path
    root = Path(write_loop.guest_store_dir()) / "tenants"
    purged = []
    freed = 0
    if root.is_dir():
        for tenant_dir in sorted(root.iterdir()):
            drawings_root = tenant_dir / "drawings"
            if drawings_root.is_dir():
                for drawing_dir in sorted(drawings_root.iterdir()):
                    marker_path = drawing_dir / "upload.state.json"
                    expires = _read_expiry(marker_path)
                    if expires is None or expires > now:
                        continue
                    # Per-drawing critical section shared with the extraction
                    # threads (see run_extraction): deletion and ingest can
                    # never interleave, so nothing returns after its receipt.
                    with drawing_lock(tenant_dir.name, drawing_dir.name):
                        # Re-check the expiry INSIDE the lock (round-5
                        # review, MAJOR): between the read above and lock
                        # acquisition, a failed-retry can REPLACE this
                        # marker with a fresh quota-charged attempt — its
                        # new expiry must veto the stale deletion verdict.
                        expires = _read_expiry(marker_path)
                        if expires is None or expires > now:
                            continue
                        # And the STORE's lock for this drawing, which is the
                        # one a second process shares. Everything that makes
                        # the receipt true happens inside it: no legacy writer
                        # can be between its manifest read and its save while
                        # this runs, so none can recreate the directory behind
                        # the line written below.
                        try:
                            with _store_checkout_guard_for_purge(
                                    tenant_dir.name,
                                    drawing_dir.name) as checkout_lock:
                                # Order is load-bearing (round-2 review, MAJOR):
                                # staged RAW files first, VERIFIED gone, then the
                                # drawing dir. If the staged file survives we stop
                                # BEFORE touching the dir — the marker stays, so the
                                # next sweep retries the WHOLE drawing. Deleting the
                                # dir first would orphan a surviving raw file behind
                                # a "deleted" receipt.
                                staged_files = [
                                    staged_path(tenant_dir.name, drawing_dir.name, ext)
                                    for ext in ACCEPTED_EXTENSIONS]
                                staged_bytes = sum(f.stat().st_size for f in staged_files
                                                   if f.is_file())
                                for f in staged_files:
                                    _unlink_quiet(f)
                                size = sum(p.stat().st_size for p in drawing_dir.rglob("*")
                                           if p.is_file())
                                if not any(f.exists() for f in staged_files):
                                    shutil.rmtree(drawing_dir, ignore_errors=True)
                                if drawing_dir.exists() or any(f.exists() for f in staged_files):
                                    # Deletion FAILED (permissions, open handle).
                                    # Never log it as a kill — a false purge line is
                                    # the exact promise-breaking lie this module
                                    # exists to prevent. The drawing is still alive,
                                    # so its lock file stays: its writers need it.
                                    _append_purge_log({"ts": _iso(now), "status": "failed",
                                                       "tenant_id": tenant_dir.name,
                                                       "drawing_id": drawing_dir.name})
                                    print(f"[guest-uploads] purge FAILED to delete "
                                          f"{tenant_dir.name}/{drawing_dir.name}; will retry "
                                          f"next sweep", file=sys.stderr)
                                    continue
                                freed += size + staged_bytes
                                purged.append({"tenant_id": tenant_dir.name,
                                               "drawing_id": drawing_dir.name})
                                _append_purge_log({"ts": _iso(now), "status": "deleted",
                                                   "tenant_id": tenant_dir.name,
                                                   "drawing_id": drawing_dir.name,
                                                   "freed_bytes": size + staged_bytes})
                                # LAST act inside the lock, and only now that the
                                # drawing is provably gone and its receipt written.
                                # Holding the lock exclusively is the proof that no
                                # live holder is being robbed of its file; a caller
                                # parked on the retired one is turned away by the
                                # store's identity check. Nothing may follow it here.
                                if checkout_lock is not None:
                                    checkout_lock.reclaim()
                        except store.CheckoutLockTimeout:
                            # A writer has held this drawing past the store's
                            # budget. Nothing was deleted, so say so and let the
                            # next sweep retry rather than reporting a kill or
                            # abandoning the drawings after this one.
                            _append_purge_log({"ts": _iso(now), "status": "failed",
                                               "tenant_id": tenant_dir.name,
                                               "drawing_id": drawing_dir.name})
                            print(f"[guest-uploads] purge could not take the store "
                                  f"checkout lock for {tenant_dir.name}/"
                                  f"{drawing_dir.name}; will retry next sweep",
                                  file=sys.stderr)
                            continue
            # a tenant dir whose drawings are all gone is itself expired
            try:
                if drawings_root.is_dir() and not any(drawings_root.iterdir()):
                    drawings_root.rmdir()
                if not any(tenant_dir.iterdir()):
                    tenant_dir.rmdir()
            except OSError:
                pass
    # Orphaned staged files (marker/store already gone — e.g. a crash between
    # staging and marker write): mtime-based, same window, guest-or-account
    # agnostic because only upload staging ever writes here.
    updir = uploads_dir()
    if updir.is_dir():
        cutoff = now - timedelta(hours=retention_hours())
        for f in updir.iterdir():
            if not f.is_file() or f.suffix.lower() not in ACCEPTED_EXTENSIONS:
                continue
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if mtime <= cutoff:
                # Staged files are extraction INPUT only (the stored drawing is
                # the extracted intake); account uploads lose their raw staged
                # file on the same window without losing the drawing.
                size = f.stat().st_size
                _unlink_quiet(f)
                freed += size
    return {"count": len(purged), "freed_bytes": freed, "purged": purged}


def _read_expiry(marker_path: Path) -> Optional[datetime]:
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        raw = marker.get("retention_expires_at")
        return datetime.fromisoformat(raw) if raw else None
    except (OSError, ValueError):
        # Unreadable marker in the GUEST store: treat as expired-now? No —
        # deleting on a parse error could destroy a live upload on a transient
        # read race. It stays until its marker reads cleanly; the guest store
        # is bounded by the rate caps.
        return None


def _unlink_quiet(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def _append_purge_log(entry: Dict[str, Any]) -> None:
    try:
        path = _purge_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError as exc:  # pragma: no cover - best-effort observability
        print(f"[guest-uploads] purge log append failed: {exc}", file=sys.stderr)


_PURGE_THREAD: Optional[threading.Thread] = None


def start_purge_daemon() -> Optional[threading.Thread]:
    """Idempotent daemon starter (app.py startup). Best-effort-wrapped loop —
    a purge failure logs and retries next interval; it never kills the app."""
    global _PURGE_THREAD
    if _PURGE_THREAD is not None and _PURGE_THREAD.is_alive():
        return _PURGE_THREAD

    def _loop() -> None:
        while True:
            time.sleep(purge_interval_s())
            try:
                result = purge_expired()
                if result["count"]:
                    print(f"[guest-uploads] purged {result['count']} expired "
                          f"guest drawing(s), {result['freed_bytes']} bytes",
                          file=sys.stderr)
            except Exception as exc:  # noqa: BLE001 - daemon must survive
                print(f"[guest-uploads] purge sweep failed: {exc}", file=sys.stderr)

    _PURGE_THREAD = threading.Thread(target=_loop, name="guest-purge", daemon=True)
    _PURGE_THREAD.start()
    return _PURGE_THREAD


# --------------------------------------------------------------------------- #
# ASGI body limit — the REAL in-process wall against oversized upload bodies
# --------------------------------------------------------------------------- #
class _BodyTooLarge(Exception):
    pass


class UploadBodyLimitMiddleware:
    """Byte-counting ASGI guard on POST /api/drawings/upload (round-2 review,
    MAJOR): FastAPI spools multipart to temp disk BEFORE the route handler
    runs, so a handler-level check cannot bound disk use, and a chunked
    (length-less) body bypasses any Content-Length check. This wraps
    `receive` and aborts the request the moment the cumulative body exceeds
    the cap (+1 MiB multipart framing slack). Outcomes, both bounded:
      * oversized DECLARED Content-Length -> clean 413 before any read;
      * oversized chunked body -> the raised abort lands inside starlette's
        multipart parser, whose own broad except answers 400 ("error parsing
        the body") — the spool stops at the cap either way, which is the
        security property; the 413 fallback below fires only if the abort
        ever escapes the parser. Every other route passes through untouched."""

    _SLACK = 1_048_576

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope.get("type") != "http" or scope.get("method") != "POST"
                or scope.get("path") != "/api/drawings/upload"):
            await self.app(scope, receive, send)
            return

        cap = max_upload_bytes() + self._SLACK
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        declared = headers.get("content-length", "")
        if declared.isdigit() and int(declared) > cap:
            await self._send_413(send)
            return

        seen = 0
        response_started = False

        async def counting_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body") or b"")
                if seen > cap:
                    raise _BodyTooLarge()
            return message

        async def tracking_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _BodyTooLarge:
            if response_started:  # pragma: no cover - parse happens pre-response
                raise
            await self._send_413(send)

    @staticmethod
    async def _send_413(send):
        import json as _json
        body = _json.dumps({
            "error": {"error_code": "BAD_PARAMS",
                      "message": f"request exceeds the {max_upload_bytes()} byte upload cap",
                      "retryable": False},
            "degraded_mode": False,
        }).encode("utf-8")
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


# --------------------------------------------------------------------------- #
# upload validation
# --------------------------------------------------------------------------- #
def validate_upload(filename: str, data: bytes) -> Tuple[str, Optional[str]]:
    """Returns (ext, None) when acceptable, else ("", reason). Checks extension,
    emptiness, and cheap content sniffs (DWG magic 'AC1…'; DXF ASCII group-code
    structure or the binary sentinel). The size cap is enforced by the caller
    (it needs the pre-read length)."""
    name = str(filename or "").lower()
    ext = next((e for e in ACCEPTED_EXTENSIONS if name.endswith(e)), "")
    if not ext:
        return "", "only .dwg and .dxf files are accepted"
    if not data:
        return "", "the uploaded file is empty"
    if ext == ".dwg":
        if not data[:3] == b"AC1":
            return "", "not a DWG file (missing AC1 version magic)"
        return ext, None
    head = data[:4096]
    if head.startswith(b"AutoCAD Binary DXF"):
        return ext, None
    if b"SECTION" in head or b"HEADER" in head or b"ENTITIES" in head:
        return ext, None
    return "", "not a DXF file (no group-code structure in the first 4 KB)"
