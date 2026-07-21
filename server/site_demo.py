"""Public-site demo solve (marketing site backend) — owns the inline §2
"string-panels" tool package and the cached demo solve it produces.

Execution path: the SAME broker chokepoint as every other tool run —
``broker_client.run_via_broker`` POSTs /broker/run over HTTP (broker_client.py
is HTTP-only; there is no in-process path), so the broker process must be up
(``cd server && uvicorn broker:app --port 8140``). The broker appends exactly
ONE attribution ledger line per /broker/run request (broker.py::broker_run,
``finally: _ledger_append``) — receiving ANY §3 envelope back therefore proves
a ledger line was appended, which is what ``receipt.ledger_line`` records.
A broker that is down surfaces as ``broker_client.BrokerUnreachable``, which
routers/site.py turns into a §10 BROKER_UNREACHABLE envelope (HTTP 502).

Caching: the solve is recomputed ONLY when (sha256(intake bytes),
solver_version) changes — an in-process cache plus a write-through JSON file at
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
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import broker_client
import deps

SERVER_DIR = Path(__file__).resolve().parent

SITE_TENANT = "site-demo"
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
    "entry": "builtins/string_panels.py",
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


class SiteSolveError(Exception):
    """The broker returned a §10/§3 failure envelope for the demo solve."""

    def __init__(self, envelope: Dict[str, Any]) -> None:
        self.envelope = envelope or {}
        err = (self.envelope.get("error") or {})
        super().__init__(err.get("message") or "demo solve failed")


_lock = threading.Lock()
_MEM: Dict[str, Any] = {"key": None, "solve": None}


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
    with _lock:
        _MEM["key"] = None
        _MEM["solve"] = None


def intake_sha256() -> str:
    """sha256 of the RAW cached-intake bytes (the APS_LIVE=0 sample the broker
    solves against — deps.DATA_FILE, the same file broker.py reads)."""
    return hashlib.sha256(deps.DATA_FILE.read_bytes()).hexdigest()


def _cache_key(sha: str) -> str:
    return f"{sha}:{SITE_TOOL['version']}"


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
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"key": key, "solve": solve}, indent=2),
                        encoding="utf-8")
    except OSError:  # cache write failure never breaks the response
        pass


def _compute_solve(sha: str) -> Dict[str, Any]:
    """One REAL broker run -> assembled solve body with a real receipt.
    Raises broker_client.BrokerUnreachable (broker down) or SiteSolveError
    (broker answered with a failure envelope)."""
    env = broker_client.run_via_broker(
        SITE_TENANT, SITE_TOOL, {}, SITE_DWG, aps_live=False)
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
            "computed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "timing_ms": env.get("timing_ms"),
            "path": "broker",
            # broker.py appends exactly ONE ledger line per /broker/run request
            # (finally-clause) — an envelope in hand proves the line was written.
            "ledger_line": True,
            "degraded_mode": bool(env.get("degraded_mode")),
        },
    }


def get_demo_solve() -> Dict[str, Any]:
    """The cached public demo solve. Recomputes only when the cache key
    (sha256(intake bytes), solver_version) misses in BOTH the in-process and
    the file cache. ``receipt.path`` reflects how this response was served."""
    sha = intake_sha256()
    key = _cache_key(sha)
    with _lock:
        if _MEM["key"] == key and _MEM["solve"] is not None:
            return _served(_MEM["solve"], "memory-cache")
        cached = _load_file_cache(key)
        if cached is not None:
            _MEM["key"], _MEM["solve"] = key, cached
            return _served(cached, "file-cache")
        solve = _compute_solve(sha)
        _MEM["key"], _MEM["solve"] = key, solve
        _write_file_cache(key, solve)
        return _served(solve, "broker")


def _served(solve: Dict[str, Any], path: str) -> Dict[str, Any]:
    out = json.loads(json.dumps(solve))  # deep copy — callers never mutate the cache
    receipt = dict(out.get("receipt") or {})
    receipt["path"] = path
    out["receipt"] = receipt
    return out


def solve_etag(solve: Dict[str, Any]) -> str:
    """Strong ETag: first 16 hex chars of the intake sha + solver version."""
    return f'"{solve["intake_sha256"][:16]}-{solve["solver"]["version"]}"'
