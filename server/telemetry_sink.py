"""Product-event sink: buffered, best-effort BigQuery insertAll from the APP.

Design of record: ops-dashboard docs + C:/tmp/leaf-platform-telemetry/
events_design.md (P2). This module is the ONE shared sink for server-side
emitters and the /api/telemetry ingest route.

WHY direct insertAll (and not a proxy/log-sink): the platform already has an
authenticated public door (this FastAPI app), and leaf_website has streamed
events into BigQuery from a non-GCP host this way for months, including the
day-table auto-create lock this module copies.

CONTRACT (mirrors emf_metrics.py, verbatim where it matters):
  - SAFETY: every public function is best-effort and NEVER raises into the
    caller. Emitters sit on terminal/finally paths; nothing here may escape.
  - Kill switch: LEAF_TELEMETRY_DISABLED=1 turns every call into a no-op.
  - Dependency boundary: google-cloud-bigquery ships in the APP image only.
    The import is guarded; where the SDK is absent (broker image, most test
    environments) the sink reports disabled and drops silently.
  - Telemetry is loss-tolerant BY CONTRACT: bounded queue (drop-oldest),
    batch flush, one structured stderr line per failed/dropped batch
    (build-the-log-line discipline), no disk spool, no retries beyond the
    single table-create retry.

Schema (docs/PLATFORM_TELEMETRY.md is the contract of record):
  date-sharded tables `<prefix>YYYYMMDD` in dataset LEAF_TELEMETRY_DATASET,
  promoted columns timestamp/event_type/event_name/tenant_id/tenant_kind/
  user_email/session_id/environment/app_version + JSON `labels` whose values
  are ALL strings.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional

EVENT_TYPES = ("custom_event", "error", "exception", "page_view")

_QUEUE_MAX = 2000
_BATCH_MAX = 250
_FLUSH_INTERVAL_S = 2.0

_TABLE_SCHEMA = [
    ("timestamp", "TIMESTAMP", "REQUIRED"),
    ("event_type", "STRING", "REQUIRED"),
    ("event_name", "STRING", "REQUIRED"),
    ("tenant_id", "STRING", "REQUIRED"),
    ("tenant_kind", "STRING", "REQUIRED"),
    ("user_email", "STRING", "NULLABLE"),
    ("session_id", "STRING", "REQUIRED"),
    ("environment", "STRING", "REQUIRED"),
    ("app_version", "STRING", "NULLABLE"),
    ("labels", "JSON", "NULLABLE"),
]

_lock = threading.Lock()
_queue: deque = deque(maxlen=_QUEUE_MAX)
_wake = threading.Condition(_lock)
_flusher_lock = threading.Lock()
_flusher_started = False
_client = None
_created_tables: set = set()
_noted: set = set()
_sdk_checked: Optional[bool] = None

# Counters are observability for tests and the ops surface, never control flow.
_stats = {"enqueued": 0, "dropped_overflow": 0, "dropped_flush": 0, "flushed": 0}


def _note_once(key: str, message: str) -> None:
    if key in _noted:
        return
    _noted.add(key)
    try:
        print(f"[leaf-telemetry] {message}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 - stderr itself may be closed; nothing may escape
        pass


def _dataset() -> str:
    return os.environ.get("LEAF_TELEMETRY_DATASET", "leaf_platform_analytics")


def _table_prefix() -> str:
    return os.environ.get("LEAF_TELEMETRY_TABLE_PREFIX", "leaf_platform_events_")


def _environment() -> str:
    return os.environ.get("LEAF_METRICS_ENVIRONMENT", "unknown")


def _app_version() -> Optional[str]:
    return os.environ.get("LEAF_APP_VERSION") or os.environ.get("GIT_SHA") or None


def disabled_reason() -> Optional[str]:
    """None when the sink can run; else why it cannot. Never raises.

    Credential PRESENCE, not validity: a malformed key or wrong-project SA
    surfaces on the first flush as the sink's one stderr drop line, never in
    a product path. The SDK import result is cached so terminal-path callers
    pay it at most once per process."""
    global _sdk_checked
    try:
        if os.environ.get("LEAF_TELEMETRY_DISABLED", "") == "1":
            return "kill switch LEAF_TELEMETRY_DISABLED=1"
        if _sdk_checked is None:
            try:
                import google.cloud.bigquery  # noqa: F401  (app image only)

                _sdk_checked = True
            except Exception:  # noqa: BLE001
                _sdk_checked = False
        if not _sdk_checked:
            return "google-cloud-bigquery not installed (broker image or test env)"
        if not (
            os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
            or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        ):
            return "no GOOGLE_APPLICATION_CREDENTIALS[_JSON] configured"
        return None
    except Exception:  # noqa: BLE001
        return "sink self-check failed"


def _make_client():  # pragma: no cover - exercised only with the real SDK
    from google.cloud import bigquery
    from google.oauth2 import service_account

    inline = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if inline:
        info = json.loads(inline)
        creds = service_account.Credentials.from_service_account_info(info)
        project = os.environ.get("BIGQUERY_PROJECT_ID") or info.get("project_id")
        return bigquery.Client(project=project, credentials=creds)
    return bigquery.Client(project=os.environ.get("BIGQUERY_PROJECT_ID") or None)


def _get_client():
    global _client
    if _client is None:
        _client = _make_client()
    return _client


def _table_id(day: str) -> str:
    client = _get_client()
    return f"{client.project}.{_dataset()}.{_table_prefix()}{day}"


def _ensure_table(day: str) -> None:
    """Create the day's table once per process (leaf_website creation-lock
    pattern). exists_ok=True absorbs the benign already-exists race; any
    OTHER failure (network, permission) RAISES into _flush_batch's drop
    handler and is NOT cached as created, so the next batch retries instead
    of dropping for the rest of the day. The memo only ever holds the
    current and previous day."""
    if day in _created_tables:
        return
    from google.cloud import bigquery

    client = _get_client()
    table = bigquery.Table(
        _table_id(day),
        schema=[bigquery.SchemaField(n, t, mode=m) for n, t, m in _TABLE_SCHEMA],
    )
    client.create_table(table, exists_ok=True)
    if len(_created_tables) >= 2:
        _created_tables.clear()
    _created_tables.add(day)


def _flush_batch(rows: List[Dict[str, Any]]) -> None:
    """Insert one batch; on failure drop it with ONE structured stderr line."""
    day = time.strftime("%Y%m%d", time.gmtime())
    try:
        _ensure_table(day)
        errors = _get_client().insert_rows_json(
            _table_id(day), rows, skip_invalid_rows=True, ignore_unknown_values=True,
        )
        bad = len(errors) if errors else 0
        with _lock:
            _stats["flushed"] += len(rows) - bad
            _stats["dropped_flush"] += bad
        if bad:
            _note_once(
                f"invalid:{day}",
                f"insertAll skipped {bad} invalid row(s) for {day}; first: {errors[0]}",
            )
    except Exception as exc:  # noqa: BLE001 - drop, count, one log line, never raise
        with _lock:
            _stats["dropped_flush"] += len(rows)
        try:
            print(
                f"[leaf-telemetry] flush dropped {len(rows)} event(s): "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr, flush=True,
            )
        except Exception:  # noqa: BLE001
            pass


def _drain_once(block_s: float) -> None:
    with _wake:
        if not _queue:
            _wake.wait(timeout=block_s)
        batch = []
        while _queue and len(batch) < _BATCH_MAX:
            batch.append(_queue.popleft())
    if batch:
        _flush_batch(batch)


def _flusher_loop() -> None:  # pragma: no cover - thread loop; body covered via _drain_once
    while True:
        try:
            _drain_once(_FLUSH_INTERVAL_S)
        except Exception:  # noqa: BLE001 - the flusher must never die
            time.sleep(_FLUSH_INTERVAL_S)


def _ensure_flusher() -> None:
    """Start the flusher exactly once; the flag flips only AFTER a successful
    Thread.start(), so a failed start retries on the next emit."""
    global _flusher_started
    if _flusher_started:
        return
    with _flusher_lock:
        if _flusher_started:
            return
        threading.Thread(
            target=_flusher_loop, name="leaf-telemetry-flush", daemon=True
        ).start()
        _flusher_started = True


def _stringify_labels(labels: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """All label values become STRINGS (exemplar SAFE_CAST discipline). The
    return value is a DICT, not serialized text: BigQuery's streaming API
    stores a JSON column from an object; a pre-serialized string would land
    as a JSON string and break every JSON_VALUE(labels.x) query."""
    if not labels:
        return None
    out: Dict[str, str] = {}
    for k, v in labels.items():
        if v is None:
            continue
        try:
            out[str(k)] = v if isinstance(v, str) else json.dumps(v) if isinstance(v, (dict, list)) else str(v)
        except Exception:  # noqa: BLE001 - skip the one bad value, keep the event
            continue
    return out or None


def emit(
    event_name: str,
    *,
    event_type: str = "custom_event",
    tenant_id: str,
    tenant_kind: str,
    session_id: str,
    user_email: Optional[str] = None,
    labels: Optional[Dict[str, Any]] = None,
) -> bool:
    """Enqueue one event. Returns whether it was accepted; NEVER raises.

    `schema_version` is stamped into labels here so no emitter can forget it.
    Timestamp is the SERVER clock (RFC3339 UTC); a client timestamp belongs in
    labels as `client_ts` (the ingest route clamps it).
    """
    try:
        reason = disabled_reason()
        if reason is not None:
            _note_once("disabled", f"sink disabled: {reason}")
            return False
        if event_type not in EVENT_TYPES:
            return False
        merged = dict(labels or {})
        # Server authority: no caller (least of all a client payload routed
        # through the ingest door) may assert a different schema_version.
        merged["schema_version"] = "1"
        row = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
            + f".{int((time.time() % 1) * 1e6):06d}Z",
            "event_type": event_type,
            "event_name": str(event_name),
            "tenant_id": str(tenant_id),
            "tenant_kind": str(tenant_kind),
            "user_email": user_email,
            "session_id": str(session_id),
            "environment": _environment(),
            "app_version": _app_version(),
            "labels": _stringify_labels(merged),
        }
        with _wake:
            if len(_queue) == _QUEUE_MAX:
                _stats["dropped_overflow"] += 1
                _queue.popleft()  # drop OLDEST; newest telemetry wins
            _queue.append(row)
            _stats["enqueued"] += 1
            _wake.notify()
        _ensure_flusher()
        return True
    except Exception:  # noqa: BLE001 - the emf_metrics contract, verbatim
        return False


def stats() -> Dict[str, int]:
    """Snapshot of sink counters (tests + ops surface). Never raises."""
    try:
        with _lock:
            return dict(_stats)
    except Exception:  # noqa: BLE001
        return {}
