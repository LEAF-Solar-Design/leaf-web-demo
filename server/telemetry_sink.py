"""Product-event sink: buffered, best-effort event delivery from the APP.

Design of record: ops-dashboard docs + C:/tmp/leaf-platform-telemetry/
events_design.md (P2). This module is the ONE shared sink for server-side
emitters and the /api/telemetry ingest route.

GCP DEPRECATION (TEL-6): BigQuery insertAll was the original backend; the
platform is cutting over to Kinesis Firehose (PutRecordBatch into
leaf-telemetry-events-<env>, landing as newline-delimited JSON objects under
events/ in the staging/prod bucket). LEAF_TELEMETRY_BACKEND selects the path
(bigquery|firehose|dual; default bigquery until cutover). Both backends write
the IDENTICAL row built once in emit() -- same promoted columns, same
bounded-queue/drop-oldest queueing, same kill switch -- so a Firehose object
and a BigQuery row for the same event are byte-identical JSON. dual writes
both, independently and best-effort, for the cutover verification window.

WHY direct insertAll (and not a proxy/log-sink): the platform already has an
authenticated public door (this FastAPI app), and leaf_website has streamed
events into BigQuery from a non-GCP host this way for months, including the
day-table auto-create lock this module copies.

CONTRACT (mirrors emf_metrics.py, verbatim where it matters):
  - SAFETY: every public function is best-effort and NEVER raises into the
    caller. Emitters sit on terminal/finally paths; nothing here may escape.
  - Kill switch: LEAF_TELEMETRY_DISABLED=1 turns every call into a no-op,
    for every backend, checked before any backend-specific gate.
  - Dependency boundary: google-cloud-bigquery ships in the APP image only.
    The import is guarded; where the SDK is absent (broker image, most test
    environments) the bigquery path reports disabled and drops silently.
    boto3 (Firehose) is already a hard server/ dependency (mcp_authority.py
    KMS signing), so the firehose path is gated on credential/permission
    reality at flush time, not on SDK presence.
  - dual FAILS CLOSED unless BOTH backends are individually ready: a
    partially-configured dual deploy must never silently write only one
    side, because the acceptance contract is BigQuery rows and S3 objects
    AGREEING on count.
  - Telemetry is loss-tolerant BY CONTRACT: bounded queue (drop-oldest),
    batch flush, one structured stderr line per failed/dropped batch
    (build-the-log-line discipline), no disk spool, no retries beyond the
    single table-create retry (bigquery) / the single PutRecordBatch call
    (firehose, which already retries transient errors internally).

Schema (docs/PLATFORM_TELEMETRY.md is the contract of record):
  date-sharded tables `<prefix>YYYYMMDD` in dataset LEAF_TELEMETRY_DATASET,
  promoted columns timestamp/event_type/event_name/tenant_id/tenant_kind/
  user_email/session_id/environment/app_version + JSON `labels` whose values
  are ALL strings. The Firehose path ships the SAME dict as one JSON line
  per record (labels travels as its already-serialized JSON string, exactly
  as it does in the BigQuery row, so a downstream Athena/Glue table over the
  bucket sees the identical shape as the BigQuery promoted columns).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
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

_BACKENDS = ("bigquery", "firehose", "dual")
_FIREHOSE_STREAM_PREFIX = "leaf-telemetry-events-"

_lock = threading.Lock()
_queue: deque = deque(maxlen=_QUEUE_MAX)
_wake = threading.Condition(_lock)
_flusher_lock = threading.Lock()
_flusher_started = False
_client = None
_created_tables: set = set()
_noted: set = set()
_sdk_checked: Optional[bool] = None
_firehose_client = None
_firehose_sdk_checked: Optional[bool] = None

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


def backend() -> str:
    """LEAF_TELEMETRY_BACKEND selects the write path. An unrecognized value
    fails CLOSED to the safe default ("bigquery", today's live path) rather
    than silently picking a backend nobody asked for."""
    val = os.environ.get("LEAF_TELEMETRY_BACKEND", "bigquery").strip().lower()
    return val if val in _BACKENDS else "bigquery"


def _aws_region() -> str:
    return os.environ.get("AWS_REGION", "us-east-1").strip() or "us-east-1"


def firehose_stream() -> str:
    """leaf-telemetry-events-<env> by default (staging stream EXISTS; prod is
    the audit lane's terraform, not this card); overridable for local/dev."""
    override = os.environ.get("LEAF_TELEMETRY_FIREHOSE_STREAM")
    return override.strip() if override and override.strip() else (
        f"{_FIREHOSE_STREAM_PREFIX}{_environment()}"
    )


def _bigquery_disabled_reason() -> Optional[str]:
    """Credential PRESENCE, not validity: a malformed key or wrong-project SA
    surfaces on the first flush as the sink's one stderr drop line, never in
    a product path. The SDK import result is cached so terminal-path callers
    pay it at most once per process."""
    global _sdk_checked
    # Credentials FIRST: a credential-less deployment (today's default) must
    # never pay the SDK import on a request path (review #426 round-1 warn
    # 7). The import only ever runs where creds exist, once.
    if not (
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
        or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    ):
        return "no GOOGLE_APPLICATION_CREDENTIALS[_JSON] configured"
    if _sdk_checked is None:
        try:
            import google.cloud.bigquery  # noqa: F401  (app image only)

            _sdk_checked = True
        except Exception:  # noqa: BLE001
            _sdk_checked = False
    if not _sdk_checked:
        return "google-cloud-bigquery not installed (broker image or test env)"
    return None


def _firehose_disabled_reason() -> Optional[str]:
    """boto3 is already a hard server/ dependency (mcp_authority.py KMS), so
    unlike bigquery there is no separate SDK-presence gate to protect a
    credential-less deployment from -- the only honest check left at this
    layer is import success. Stream existence and IAM permission surface at
    the first PutRecordBatch, exactly like the bigquery credential-validity
    deferral above (this is the tier-1 live probe the operator/train step
    covers, not something buildable without AWS access)."""
    global _firehose_sdk_checked
    if _firehose_sdk_checked is None:
        try:
            import boto3  # noqa: F401

            _firehose_sdk_checked = True
        except Exception:  # noqa: BLE001
            _firehose_sdk_checked = False
    if not _firehose_sdk_checked:
        return "boto3 not installed"
    return None


def disabled_reason() -> Optional[str]:
    """None when the sink can run; else why it cannot. Never raises."""
    try:
        if os.environ.get("LEAF_TELEMETRY_DISABLED", "") == "1":
            return "kill switch LEAF_TELEMETRY_DISABLED=1"
        b = backend()
        if b == "bigquery":
            return _bigquery_disabled_reason()
        if b == "firehose":
            return _firehose_disabled_reason()
        # dual: fail closed unless BOTH sides are ready -- see module
        # docstring on why a half-configured dual deploy may not write only
        # one backend.
        bq_reason = _bigquery_disabled_reason()
        fh_reason = _firehose_disabled_reason()
        if bq_reason or fh_reason:
            return (
                f"dual backend not fully configured "
                f"(bigquery: {bq_reason or 'ok'}; firehose: {fh_reason or 'ok'})"
            )
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


def _make_firehose_client():  # pragma: no cover - exercised only with the real SDK
    import boto3

    return boto3.client("firehose", region_name=_aws_region())


def _get_firehose_client():
    global _firehose_client
    if _firehose_client is None:
        _firehose_client = _make_firehose_client()
    return _firehose_client


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


def _flush_bigquery(rows: List[Dict[str, Any]]) -> None:
    """Insert one batch via legacy insertAll; on failure drop it with ONE
    structured stderr line. Self-contained and never raises: this is the
    ORIGINAL bigquery flush body, unchanged in shape, so a bigquery-only
    (today's default) deploy behaves byte-for-byte as it did pre-TEL-6."""
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
                f"[leaf-telemetry] bigquery flush dropped {len(rows)} event(s): "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr, flush=True,
            )
        except Exception:  # noqa: BLE001
            pass


def _flush_firehose(rows: List[Dict[str, Any]]) -> None:
    """PutRecordBatch one batch to the configured stream: one record per row,
    each the row dict as a single newline-terminated JSON line (S3 lands
    newline-delimited JSON under events/, matching the BigQuery promoted-
    column row shape exactly -- `labels` travels as the SAME already-
    stringified JSON value). Firehose's own partial-failure semantics
    (FailedPutCount) drop only the failed records with one stderr line; a
    whole-batch exception (auth, missing stream, network) drops the batch.
    Self-contained and never raises, mirroring _flush_bigquery."""
    stream = firehose_stream()
    try:
        client = _get_firehose_client()
        records = [
            {"Data": (json.dumps(row, default=str) + "\n").encode("utf-8")}
            for row in rows
        ]
        resp = client.put_record_batch(DeliveryStreamName=stream, Records=records)
        bad = int(resp.get("FailedPutCount") or 0)
        with _lock:
            _stats["flushed"] += len(rows) - bad
            _stats["dropped_flush"] += bad
        if bad:
            first_err = next(
                (r.get("ErrorMessage") for r in resp.get("RequestResponses", [])
                 if r.get("ErrorCode")),
                None,
            )
            _note_once(
                f"firehose_invalid:{stream}",
                f"PutRecordBatch dropped {bad} record(s) on {stream}; first: {first_err}",
            )
    except Exception as exc:  # noqa: BLE001 - drop, count, one log line, never raise
        with _lock:
            _stats["dropped_flush"] += len(rows)
        try:
            print(
                f"[leaf-telemetry] firehose flush dropped {len(rows)} event(s) "
                f"on {stream}: {type(exc).__name__}: {exc}",
                file=sys.stderr, flush=True,
            )
        except Exception:  # noqa: BLE001
            pass


def _flush_batch(rows: List[Dict[str, Any]]) -> None:
    """Deliver one batch to every backend LEAF_TELEMETRY_BACKEND configures.
    Each backend flush is independent and best-effort (matching the sink's
    own loss-tolerant contract); dual calls both against the IDENTICAL rows
    list, which is what makes their counts agree under healthy delivery."""
    b = backend()
    if b in ("bigquery", "dual"):
        try:
            _flush_bigquery(rows)
        except Exception as exc:  # noqa: BLE001 - preserve the other dual leg
            with _lock:
                _stats["dropped_flush"] += len(rows)
            _note_once(
                "bigquery_unexpected",
                f"bigquery flush raised unexpectedly for {len(rows)} event(s): "
                f"{type(exc).__name__}: {exc}",
            )
    if b in ("firehose", "dual"):
        try:
            _flush_firehose(rows)
        except Exception as exc:  # noqa: BLE001 - preserve the other dual leg
            with _lock:
                _stats["dropped_flush"] += len(rows)
            _note_once(
                "firehose_unexpected",
                f"firehose flush raised unexpectedly for {len(rows)} event(s): "
                f"{type(exc).__name__}: {exc}",
            )


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


def _stringify_labels(labels: Optional[Dict[str, Any]]) -> Optional[str]:
    """All label values become STRINGS (exemplar SAFE_CAST discipline), and
    the whole object is returned as ONE json.dumps string. LIVE-PROVEN, not
    theory: the legacy insertAll API (insert_rows_json) takes a JSON column
    as a JSON-encoded string and parses it into a JSON VALUE server-side;
    passing a dict fails the row with "This field: labels is not a record"
    (first live insert, staging 2026-08-04, the day table was created and
    the row skipped). JSON_VALUE(labels.x) works against the parsed value."""
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
    return json.dumps(out) if out else None


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
        # through the ingest door) may assert a different schema_version or
        # parity identity. The one generated event_id rides inside labels so
        # the existing BigQuery JSON column and Firehose row stay compatible.
        merged["schema_version"] = "1"
        merged["event_id"] = str(uuid.uuid4())
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
