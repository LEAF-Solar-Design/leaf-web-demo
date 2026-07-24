"""Bounded, credential-free dependency readiness for the app process.

The public contract returns only stable dependency names and coarse states.
Probe targets, paths, credentials, exception text, and database identity never
enter the response.
"""
from __future__ import annotations

import json
import math
import os
import re
import secrets
import threading
import time
import urllib.request
from copy import deepcopy
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

_SERVER_DIR = Path(__file__).resolve().parent
_REVISION_RE = re.compile(r"^(?:[0-9a-fA-F]{7,64}|sha256:[0-9a-fA-F]{64})$")
_TRUE = {"1", "true", "yes", "on"}
_REVISION_KEYS = (
    "LEAF_BUILD_REVISION",
    "LEAF_SOURCE_SHA",
    "SOURCE_REVISION",
    "GIT_COMMIT",
    "VERCEL_GIT_COMMIT_SHA",
    "RENDER_GIT_COMMIT",
)
_PROBE_WORKERS = 6
_PROBE_EXECUTOR = ThreadPoolExecutor(
    max_workers=_PROBE_WORKERS, thread_name_prefix="leaf-ready-probe")
_PROBE_SLOTS = threading.BoundedSemaphore(_PROBE_WORKERS)
_PROBE_SLOT_LOCK = threading.Lock()
_PROBE_SLOTS_IN_USE = 0
_PROBE_SLOT_HIGH_WATER = 0
_CACHE_TTL_S = 0.25
_CACHE_CONDITION = threading.Condition()
_CACHE_RESULT: Optional[Dict[str, Any]] = None
_CACHE_AT = 0.0
_CACHE_RUNNING = False


def _try_acquire_probe_slot() -> bool:
    global _PROBE_SLOTS_IN_USE, _PROBE_SLOT_HIGH_WATER
    if not _PROBE_SLOTS.acquire(blocking=False):
        return False
    with _PROBE_SLOT_LOCK:
        _PROBE_SLOTS_IN_USE += 1
        _PROBE_SLOT_HIGH_WATER = max(_PROBE_SLOT_HIGH_WATER, _PROBE_SLOTS_IN_USE)
    return True


def _release_probe_slot(_future: Optional[Future[Any]] = None) -> None:
    global _PROBE_SLOTS_IN_USE
    with _PROBE_SLOT_LOCK:
        _PROBE_SLOTS_IN_USE -= 1
    _PROBE_SLOTS.release()


def _probe_slot_snapshot() -> tuple[int, int]:
    with _PROBE_SLOT_LOCK:
        return _PROBE_SLOTS_IN_USE, _PROBE_SLOT_HIGH_WATER


class DependencyUnavailable(RuntimeError):
    """The dependency could not serve a readiness probe."""


class DependencyDegraded(RuntimeError):
    """The dependency is optional or usable with reduced assurance."""


@dataclass(frozen=True)
class DependencySpec:
    name: str
    required: bool
    probe: Callable[[], None]


def _truthy(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() in _TRUE


def _timeout_seconds(env: Mapping[str, str]) -> float:
    try:
        value = float(env.get("LEAF_READINESS_TIMEOUT_S", "0.75"))
    except (TypeError, ValueError):
        value = 0.75
    if not math.isfinite(value):
        value = 0.75
    return min(max(value, 0.05), 2.0)


def _bounded_float(
        env: Mapping[str, str], name: str, default: float,
        minimum: float, maximum: float) -> float:
    try:
        value = float(env.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return min(max(value, minimum), maximum)


def _source_revision(env: Mapping[str, str]) -> Optional[str]:
    for name in _REVISION_KEYS:
        value = env.get(name, "").strip()
        if value and _REVISION_RE.fullmatch(value):
            return value.lower()
    return None


def _http_probe(url: str, timeout: float) -> None:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise DependencyUnavailable()
            body = response.read(4097)
    except DependencyUnavailable:
        raise
    except Exception as exc:
        raise DependencyUnavailable() from exc
    if len(body) > 4096:
        raise DependencyUnavailable()
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DependencyUnavailable() from exc
    if parsed.get("ok") is not True:
        raise DependencyUnavailable()


def _database_parts(timeout: float):
    import platform_link

    _store, db, _deps = platform_link._load_platform()
    url = db.get_database_url()
    db.validate_database_url(url)
    import psycopg

    statement_timeout_ms = max(50, int(timeout * 1000))
    return db, psycopg, url, statement_timeout_ms


def _configure_statement_timeout(cur: Any, timeout_ms: int) -> None:
    """Bound probe queries without sending proxy-sensitive startup options."""
    cur.execute(
        "SELECT set_config('statement_timeout', %s, true)",
        (f"{timeout_ms}ms",),
    )


def _database_probe(timeout: float) -> None:
    try:
        db, psycopg, url, statement_timeout_ms = _database_parts(timeout)
        with psycopg.connect(
                url, connect_timeout=max(1, math.ceil(timeout))) as conn:
            with conn.cursor() as cur:
                _configure_statement_timeout(cur, statement_timeout_ms)
                cur.execute(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = ANY(%s)",
                    (list(db._REQUIRED_COLUMNS),),
                )
                found: Dict[str, set[str]] = {}
                for table, column in cur.fetchall():
                    found.setdefault(table, set()).add(column)
        missing = any(
            columns - found.get(table, set())
            for table, columns in db._REQUIRED_COLUMNS.items()
        )
        if missing:
            raise DependencyDegraded()
    except DependencyDegraded:
        raise
    except Exception as exc:
        raise DependencyUnavailable() from exc


def _worker_probe(
        timeout: float, tool_name: str, max_age_seconds: float,
        expected_revision: Optional[str]) -> None:
    try:
        _db, psycopg, url, statement_timeout_ms = _database_parts(timeout)
        with psycopg.connect(
                url, connect_timeout=max(1, math.ceil(timeout))) as conn:
            with conn.cursor() as cur:
                _configure_statement_timeout(cur, statement_timeout_ms)
                cur.execute(
                    "SELECT source_revision, observed_at "
                    "FROM canonical_worker_heartbeats WHERE tool_name = %s "
                    "ORDER BY observed_at DESC LIMIT 1",
                    (tool_name,),
                )
                row = cur.fetchone()
        if not row:
            raise DependencyDegraded()
        observed_at = row[1]
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - observed_at).total_seconds()
        if age < 0 or age > max_age_seconds:
            raise DependencyDegraded()
        if expected_revision is not None and row[0] != expected_revision:
            raise DependencyDegraded()
    except DependencyDegraded:
        raise
    except Exception as exc:
        raise DependencyUnavailable() from exc


def _durability_cycle(directory: Path) -> None:
    if not directory.is_dir():
        raise DependencyUnavailable()
    sentinel = directory / f".leaf-ready-{secrets.token_hex(16)}"
    fd: Optional[int] = None
    created = False
    try:
        fd = os.open(sentinel, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        created = True
        os.write(fd, b"leaf-readiness\n")
        os.fsync(fd)
        os.close(fd)
        fd = None
        sentinel.unlink()
        created = False
    except Exception as exc:
        raise DependencyUnavailable() from exc
    finally:
        if fd is not None:
            os.close(fd)
        if created:
            try:
                sentinel.unlink()
            except OSError:
                pass


def _durable_stores_probe(env: Mapping[str, str]) -> None:
    guest_store = Path(env.get(
        "LEAF_GUEST_STORE_DIR", str(_SERVER_DIR / "guest_drawings")))
    try:
        guest_store.mkdir(parents=False, exist_ok=True)
    except OSError as exc:
        raise DependencyUnavailable() from exc
    locations = (
        Path(env.get("JOBS_DB", str(_SERVER_DIR / "jobs.db"))).parent,
        Path(env.get("LEAF_STORE_DIR", str(_SERVER_DIR / "drawings"))),
        guest_store,
        Path(env.get("LEAF_AGENT_STATE_DIR", str(_SERVER_DIR.parent / "data"))),
        Path(env.get("LEAF_TENANTS_DIR", str(_SERVER_DIR / "tenants"))),
    )
    seen = set()
    for location in locations:
        resolved = os.path.abspath(location)
        if resolved not in seen:
            seen.add(resolved)
            _durability_cycle(Path(resolved))


def _build_probe(env: Mapping[str, str], required: bool) -> None:
    revision = _source_revision(env)
    if revision is not None:
        return
    if required:
        raise DependencyUnavailable()
    raise DependencyDegraded()


def default_specs(
        env: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None) -> list[DependencySpec]:
    current = os.environ if env is None else env
    budget = _timeout_seconds(current) if timeout is None else timeout
    broker_base = current.get("BROKER_URL", "http://127.0.0.1:8140").rstrip("/")
    harness_base = (
        current.get("LEAF_CONVERSE_HARNESS_URL")
        or current.get("LEAF_AUTHOR_HARNESS_URL")
        or ""
    ).rstrip("/")
    database_configured = bool(current.get("DATABASE_URL", "").strip())
    database_required = _truthy(current, "LEAF_PLATFORM_POSTGRES_REQUIRED")
    worker_required = _truthy(current, "LEAF_CANONICAL_WORKER_REQUIRED")
    harness_required = (
        _truthy(current, "LEAF_AUTHORED_EXECUTION") or bool(harness_base)
    )
    build_required = _truthy(current, "LEAF_BUILD_REVISION_REQUIRED")
    expected_worker_revision = current.get(
        "LEAF_EXPECTED_WORKER_REVISION", "").strip().lower() or None
    if (expected_worker_revision is not None and
            not _REVISION_RE.fullmatch(expected_worker_revision)):
        expected_worker_revision = "__invalid__"

    def unavailable_or_degraded(required: bool) -> None:
        if required:
            raise DependencyUnavailable()
        raise DependencyDegraded()

    return [
        DependencySpec(
            "broker", True,
            lambda: _http_probe(f"{broker_base}/broker/health", budget)),
        DependencySpec(
            "harness", harness_required,
            (lambda: _http_probe(f"{harness_base}/health", budget))
            if harness_base else (lambda: unavailable_or_degraded(harness_required))),
        DependencySpec(
            "database", database_required,
            (lambda: _database_probe(budget))
            if database_configured else (lambda: unavailable_or_degraded(database_required))),
        DependencySpec(
            "worker", worker_required,
            (lambda: _worker_probe(
                budget,
                current.get("LEAF_CANONICAL_WORKER_TOOL", "string-autofill-opt"),
                _bounded_float(
                    current, "LEAF_WORKER_HEARTBEAT_MAX_AGE_S", 30.0, 1.0, 300.0),
                expected_worker_revision,
            ))
            if database_configured else (lambda: unavailable_or_degraded(worker_required))),
        DependencySpec(
            "durable_stores", True, lambda: _durable_stores_probe(current)),
        DependencySpec(
            "build", build_required, lambda: _build_probe(current, build_required)),
    ]


def _collect_readiness(
        current: Mapping[str, str], selected: list[DependencySpec],
        timeout: float) -> Dict[str, Any]:
    started = time.monotonic()

    def run_probe(spec: DependencySpec) -> tuple[str, int]:
        probe_started = time.monotonic()
        state = "ready"
        try:
            spec.probe()
        except DependencyDegraded:
            state = "degraded"
        except Exception:
            state = "unavailable"
        return state, max(0, int((time.monotonic() - probe_started) * 1000))

    futures: Dict[Future[tuple[str, int]], DependencySpec] = {}
    results: Dict[str, Dict[str, Any]] = {}
    for spec in selected:
        if not _try_acquire_probe_slot():
            results[spec.name] = {
                "state": "timeout",
                "required": spec.required,
                "latency_ms": 0,
            }
            continue
        try:
            future = _PROBE_EXECUTOR.submit(run_probe, spec)
        except Exception:
            _release_probe_slot()
            results[spec.name] = {
                "state": "unavailable",
                "required": spec.required,
                "latency_ms": 0,
            }
            continue
        future.add_done_callback(_release_probe_slot)
        futures[future] = spec
    done, pending = wait(futures, timeout=timeout)
    for future in done:
        spec = futures[future]
        state, latency_ms = future.result()
        results[spec.name] = {
            "state": state,
            "required": spec.required,
            "latency_ms": latency_ms,
        }
    for future in pending:
        spec = futures[future]
        future.cancel()
        results[spec.name] = {
            "state": "timeout",
            "required": spec.required,
            "latency_ms": max(0, int(timeout * 1000)),
        }
    ordered = {spec.name: results[spec.name] for spec in selected}
    ready = all(
        not item["required"] or item["state"] == "ready"
        for item in ordered.values()
    )
    degraded = any(item["state"] != "ready" for item in ordered.values())
    status = "ready" if not degraded else ("degraded" if ready else "unavailable")
    return {
        "ok": ready,
        "ready": ready,
        "status": status,
        "dependencies": ordered,
        "source_revision": _source_revision(current),
        "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
    }


def readiness_report(
        env: Optional[Mapping[str, str]] = None,
        specs: Optional[list[DependencySpec]] = None) -> Dict[str, Any]:
    current = os.environ if env is None else env
    timeout = _timeout_seconds(current)
    selected = default_specs(current, timeout) if specs is None else specs
    if env is not None or specs is not None:
        return _collect_readiness(current, selected, timeout)

    global _CACHE_RESULT, _CACHE_AT, _CACHE_RUNNING
    with _CACHE_CONDITION:
        now = time.monotonic()
        if _CACHE_RESULT is not None and now - _CACHE_AT <= _CACHE_TTL_S:
            return deepcopy(_CACHE_RESULT)
        if _CACHE_RUNNING:
            deadline = now + timeout + 0.05
            while _CACHE_RUNNING and time.monotonic() < deadline:
                _CACHE_CONDITION.wait(max(0.001, deadline - time.monotonic()))
            if _CACHE_RESULT is not None:
                return deepcopy(_CACHE_RESULT)
            if _CACHE_RUNNING:
                ordered = {
                    spec.name: {
                        "state": "timeout",
                        "required": spec.required,
                        "latency_ms": max(0, int(timeout * 1000)),
                    }
                    for spec in selected
                }
                return {
                    "ok": False,
                    "ready": False,
                    "status": "unavailable",
                    "dependencies": ordered,
                    "source_revision": _source_revision(current),
                    "duration_ms": max(0, int(timeout * 1000)),
                }
        _CACHE_RUNNING = True
    try:
        result = _collect_readiness(current, selected, timeout)
    finally:
        with _CACHE_CONDITION:
            if "result" in locals():
                _CACHE_RESULT = result
                _CACHE_AT = time.monotonic()
            _CACHE_RUNNING = False
            _CACHE_CONDITION.notify_all()
    return deepcopy(result)
