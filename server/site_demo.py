"""Public-site demo solve (marketing site backend) — owns the inline §2
"string-panels" tool package and the cached demo solve it produces.

Execution path: the SAME broker chokepoint as every other tool run —
``broker_client.run_via_broker`` POSTs /broker/run over HTTP (broker_client.py
is HTTP-only; there is no in-process path), so the broker process must be up
(``cd server && uvicorn broker:app --port 8140``). The broker appends one
attribution ledger line per durable run identity. Receiving any section 3
terminal envelope back therefore proves
the durable ledger line exists, which is what ``receipt.ledger_line`` records.
A broker that is down surfaces as ``broker_client.BrokerUnreachable``, which
routers/site.py turns into a §10 BROKER_UNREACHABLE envelope (HTTP 502).

Caching: the solve is recomputed when (sha256(intake bytes), solver_version)
changes or the proof reaches its 20-hour renewal limit. It uses an in-process
cache plus a write-through JSON file at
``$LEAF_SITE_CACHE_FILE`` (default: ``site_demo_cache.json`` alongside the
broker ledger — the same ``$BROKER_LEDGER``-or-``server/`` directory the ledger
resolves to). ``receipt.path`` records how THIS response was served
("broker" | "memory-cache" | "file-cache"); the other receipt fields describe
the original computation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import broker_client
import deps

SERVER_DIR = Path(__file__).resolve().parent

SITE_TENANT = "site-demo"
# Bump only after a reviewed operator recovery invalidates a terminal demo
# admission. The broker keeps the old denial and ledger evidence under its
# original key, while this generation receives a fresh idempotency identity.
# r3 follows the reviewed staging E2B substrate recovery on 2026-07-29.
SITE_RUN_GENERATION = "r3"
SITE_DWG = "rooftop_demo"

# Inline §2 tool package. Entry resolves via tool_loader.resolve_local_file
# (server-dir-relative), so the broker executes server/builtins/string_panels.py.
# Electrical defaults are MARKETING-DEMO values (public Q.Peak 425-class
# datasheet ballpark) pending operator blessing — see builtins/string_panels.py.
SITE_TOOL: Dict[str, Any] = {
    "name": "string-panels",
    "version": "1.2.0",
    "description": ("Bank-clustered, rotation-aware serpentine DC string routing "
                    "over the Panels layer with an NEC 690.7 cold-temperature "
                    "string-size check."),
    "kind": "script",
    "engine_op": "string_panels",
    "entry": "builtins/string_panels.py",
    "offline_only": True,
    "params": {
        "type": "object",
        "properties": {
            "module_model": {
                "type": "string",
                "default": "Q.Peak DUO XL-G11.7 / 425",
                "description": "Module model label (marketing-demo default).",
            },
            "voc": {
                "type": "number",
                "default": 48.5,
                "description": "Module open-circuit voltage at STC, volts (demo default).",
            },
            "temp_coeff_pct_per_c": {
                "type": "number",
                "default": -0.27,
                "description": "Voc temperature coefficient, %/degC (demo default).",
            },
            "design_min_temp_c": {
                "type": "number",
                "default": -25.0,
                "description": "Design minimum ambient temperature, degC (demo default).",
            },
            "max_system_voltage": {
                "type": "number",
                "default": 1000.0,
                "description": ("NEC 690.7 maximum system voltage, volts "
                                "(commercial-rooftop demo default)."),
            },
            "panel_layer": {
                "type": "string",
                "default": "Panels",
                "description": "Intake layer carrying the closed panel polylines.",
            },
            "cluster_radius_factor": {
                "type": "number",
                "default": 3.0,
                "description": ("Bank clustering radius as a multiple of the median "
                                "nearest-neighbor panel distance; strings never "
                                "cross bank gaps."),
            },
        },
        "required": [],
    },
    "returns": {
        "type": "object",
        "properties": {
            "module": {"type": "object"},
            "electrical": {"type": "object"},
            "strings": {"type": "array"},
            "stats": {"type": "object"},
        },
    },
    "capabilities": ["drawing.read"],
    "provenance": {"author": "site-demo-lane", "created": "2026-07-20T00:00:00Z"},
}

# Marketing framing constants for the public site (not tool output).
DRAFTING_HOURS = 25
SOLVE_MINUTES = 3
SITE_SOLVE_REFRESH_SECONDS = 20 * 60 * 60
SITE_SOLVE_WAIT_SECONDS = 610


class SiteSolveError(Exception):
    """The broker returned a §10/§3 failure envelope for the demo solve."""

    def __init__(self, envelope: Dict[str, Any]) -> None:
        self.envelope = envelope or {}
        err = (self.envelope.get("error") or {})
        super().__init__(err.get("message") or "demo solve failed")


_condition = threading.Condition()
_MEM: Dict[str, Any] = {"key": None, "solve": None}
_IN_FLIGHT: set[str] = set()


def cache_file() -> Path:
    """Write-through cache location: $LEAF_SITE_CACHE_FILE, defaulting to
    ``site_demo_cache.json`` in the broker ledger's directory (the SAME
    $BROKER_LEDGER-or-server/ resolution broker.py uses). Read at call time so
    test/subprocess env overrides apply."""
    override = os.environ.get("LEAF_SITE_CACHE_FILE")
    if override:
        return Path(override)
    ledger = Path(os.environ.get("BROKER_LEDGER", str(SERVER_DIR / "broker_ledger.jsonl")))
    return ledger.parent / "site_demo_cache.json"


def clear_cache() -> None:
    """Test hook: drop the in-process cache (the file cache is env-addressed)."""
    with _condition:
        _MEM["key"] = None
        _MEM["solve"] = None


def intake_sha256() -> str:
    """sha256 of the RAW cached-intake bytes (the APS_LIVE=0 sample the broker
    solves against — deps.DATA_FILE, the same file broker.py reads)."""
    return hashlib.sha256(deps.DATA_FILE.read_bytes()).hexdigest()


def _cache_key(sha: str) -> str:
    return f"{sha}:{SITE_TOOL['version']}"


def _now_epoch() -> float:
    return time.time()


def _window_start(now: float) -> int:
    return int(now // SITE_SOLVE_REFRESH_SECONDS) * SITE_SOLVE_REFRESH_SECONDS


def _format_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _computed_at_epoch(solve: Dict[str, Any]) -> Optional[float]:
    value = (solve.get("receipt") or {}).get("computed_at")
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _is_fresh(solve: Dict[str, Any], now: float) -> bool:
    computed_at = _computed_at_epoch(solve)
    if computed_at is None:
        return False
    age = now - computed_at
    return 0 <= age < SITE_SOLVE_REFRESH_SECONDS


def _load_file_cache(key: str) -> Optional[Dict[str, Any]]:
    path = cache_file()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a corrupt cache file just means recompute
        return None
    if data.get("key") == key and isinstance(data.get("solve"), dict):
        return data["solve"]
    return None


def _write_file_cache(key: str, solve: Dict[str, Any]) -> None:
    path = cache_file()
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps({"key": key, "solve": solve}, indent=2),
                             encoding="utf-8")
        os.replace(temporary, path)
    except OSError:  # cache write failure never breaks the response
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _compute_solve(sha: str, run_id: str, computed_at: str) -> Dict[str, Any]:
    """One REAL broker run -> assembled solve body with a real receipt.
    Raises broker_client.BrokerUnreachable (broker down) or SiteSolveError
    (broker answered with a failure envelope)."""
    env = broker_client.run_via_broker(
        SITE_TENANT, SITE_TOOL, {}, SITE_DWG, aps_live=False,
        ledger_event_key=(
            f"site-demo:{sha}:{SITE_TOOL['version']}:"
            f"{SITE_RUN_GENERATION}:{run_id}"
        ),
    )
    if not env.get("ok"):
        raise SiteSolveError(env)
    result = env.get("result") or {}
    stats = dict(result.get("stats") or {})
    stats["drafting_hours"] = DRAFTING_HOURS
    stats["solve_minutes"] = SOLVE_MINUTES
    return {
        "solve_id": f"site-{sha[:8]}",
        "intake_sha256": sha,
        "solver": {"tool": SITE_TOOL["name"],
                   "version": env.get("version") or SITE_TOOL["version"]},
        "module": result.get("module") or {},
        "electrical": result.get("electrical") or {},
        "strings": result.get("strings") or [],
        "stats": stats,
        "receipt": {
            "computed_at": computed_at,
            "timing_ms": env.get("timing_ms"),
            "path": "broker",
            # One durable ledger line per run identity. Replayed terminal
            # envelopes point at that same line.
            "ledger_line": True,
            "degraded_mode": bool(env.get("degraded_mode")),
        },
    }


def get_demo_solve(refresh_id: Optional[str] = None) -> Dict[str, Any]:
    """Return a current public solve proof.

    Automatic calls use one stable broker identity per 20-hour window. One
    process-local condition collapses concurrent refreshes without holding a
    lock across file IO or the broker request. Expired evidence is never served
    as current if renewal fails.
    """
    sha = intake_sha256()
    key = _cache_key(sha)
    now = _now_epoch()
    if refresh_id is not None:
        run_id = refresh_id
        computed_at = _format_utc(now)
    else:
        start = _window_start(now)
        run_id = f"window-{start}"
        computed_at = _format_utc(start)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", run_id):
        raise ValueError("refresh_id must be a safe non-empty identifier")

    flight_key = f"{key}:{run_id}"
    while True:
        with _condition:
            if (refresh_id is None and _MEM["key"] == key
                    and isinstance(_MEM["solve"], dict)
                    and _is_fresh(_MEM["solve"], _now_epoch())):
                return _served(_MEM["solve"], "memory-cache")
            if flight_key not in _IN_FLIGHT:
                _IN_FLIGHT.add(flight_key)
                break
            notified = _condition.wait(timeout=SITE_SOLVE_WAIT_SECONDS)
            if not notified and flight_key in _IN_FLIGHT:
                raise broker_client.BrokerUnreachable(
                    "timed out waiting for the demo solve renewal")

    try:
        if refresh_id is None:
            cached = _load_file_cache(key)
            if cached is not None and _is_fresh(cached, _now_epoch()):
                with _condition:
                    _MEM["key"], _MEM["solve"] = key, cached
                return _served(cached, "file-cache")
        solve = _compute_solve(sha, run_id, computed_at)
        _write_file_cache(key, solve)
        with _condition:
            _MEM["key"], _MEM["solve"] = key, solve
        return _served(solve, "broker")
    finally:
        with _condition:
            _IN_FLIGHT.discard(flight_key)
            _condition.notify_all()


def _served(solve: Dict[str, Any], path: str) -> Dict[str, Any]:
    out = json.loads(json.dumps(solve))  # deep copy — callers never mutate the cache
    receipt = dict(out.get("receipt") or {})
    receipt["path"] = path
    out["receipt"] = receipt
    return out


def solve_etag(solve: Dict[str, Any]) -> str:
    """Strong ETag over the intake, solver, and renewable proof timestamp."""
    material = ":".join([
        solve["intake_sha256"],
        solve["solver"]["version"],
        str((solve.get("receipt") or {}).get("computed_at", "")),
    ])
    return f'"{hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]}"'
