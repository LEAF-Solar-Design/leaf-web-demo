"""
APS credential broker v1 (CONTRACT-ADDENDUM section 8) — its own process.

THE security property: this is the ONLY process that imports da/client.py and
the only one that can read ~/.aps/credentials.json. Tenant-facing code (app.py,
jobs.py) calls POST /broker/run over HTTP and never holds the secret.

Also the attribution + quota + kill-switch chokepoint:
  - every /broker/run appends exactly ONE JSONL line to broker_ledger.jsonl
  - POST /broker/tenants/{tid}/disable|enable flips a persisted kill-switch
  - an in-process egress allowlist guards every outbound HTTP request

Run:  cd server && uvicorn broker:app --port 8140   (env BROKER_PORT for python broker.py)

Env:
  BROKER_PORT     (default 8140)
  BROKER_LEDGER   (default server/broker_ledger.jsonl)
  BROKER_TENANTS  (default server/broker_tenants.json)
  LEAF_BROKER_STORE (legacy default; postgres enables the shared authority)
  LEAF_BROKER_RECONCILE_SECRET (required for operator reconciliation routes)
  APS_CRED        (passed through to da/client.py; only read on live runs)
  LEAF_RUNTIME_ENV (set to production for the fail-closed deployment posture)
  LEAF_AUTHORED_EXECUTION (production default 0; 1 requires LEAF_SANDBOX)
"""
from __future__ import annotations

# Cache the STDLIB `queue` module now, before this process ever inserts `da/`
# onto sys.path (it does so lazily in _get_da, and da/queue.py — a transparent
# stdlib superset — otherwise shadows it). Belt-and-suspenders: da/queue.py is
# already a superset, but pinning the stdlib module here removes all ambiguity.
import queue as _stdlib_queue  # noqa: F401
import sys as _sys_early
_sys_early.dont_write_bytecode = True  # tenant tool files: no __pycache__ (races git add in the tenant repo during authoring)
import platform as _stdlib_platform  # noqa: F401  (cache stdlib before the
#   PROJECT_ROOT sys.path insert below makes the local platform/ package shadow it;
#   requests/urllib3 import `platform` lazily inside request handlers)

import functools
import hashlib
import hmac
import inspect
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

SERVER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SERVER_DIR.parent
DATA_DIR = PROJECT_ROOT / "data"
DATA_FILE = DATA_DIR / "rooftop_demo.intake.json"
for p in (str(PROJECT_ROOT), str(SERVER_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import tools_fallback as fb  # noqa: E402,F401  (compat: builtins delegate here)
import entitlements  # noqa: E402  (F10: broker-side tier re-check; IMPORT/CALL only)
import drawing_identity  # noqa: E402
from envelopes import (  # noqa: E402
    DEFAULT_HTTP_STATUS,
    ErrorCode,
    err_envelope,
    error_obj,
    install_error_handlers,
    ok_envelope,
    with_envelope_fields,
)
from tool_loader import (  # noqa: E402
    _sandbox_tier as _tool_sandbox_tier,
    is_trusted_builtin_tool,
    run_tool_dynamic,
)
from tool_validate import validate_params  # noqa: E402
import write_loop  # noqa: E402  (M2 write branch; never imports da.* at top)
import platform_link  # noqa: E402  (collision-safe leaf_platform store loader)
from jobs import PLAN_TOOL, PLAN_TOOL_NAME  # noqa: E402

try:  # noqa: E402 - APS domain metrics via CloudWatch EMF; best-effort, optional
    import emf_metrics
except Exception:  # pragma: no cover - emit is optional; its absence must not break the broker
    emf_metrics = None  # type: ignore[assignment]


def _emit_aps_metric(entry: Dict[str, Any], event_key: str) -> None:
    """Best-effort APS EMF emit. NEVER raises: called from broker `finally`
    blocks, so nothing here may escape into the response/ledger path (defence in
    depth on top of emf_metrics' own best-effort handlers)."""
    if emf_metrics is None:
        return
    try:
        entry["event_key"] = event_key
        emf_metrics.emit_broker_run(entry)
    except Exception:  # noqa: BLE001 - metrics must never break a broker run
        pass

LEDGER_PATH = Path(os.environ.get("BROKER_LEDGER", str(SERVER_DIR / "broker_ledger.jsonl")))
TENANTS_PATH = Path(os.environ.get("BROKER_TENANTS", str(SERVER_DIR / "broker_tenants.json")))
APS_ENDPOINT = "https://developer.api.autodesk.com"
QA_SLEEP_CAP_S = 30.0

_ledger_lock = threading.Lock()
_tenants_lock = threading.Lock()
_pg_store = None


def _broker_store_mode() -> str:
    mode = os.environ.get("LEAF_BROKER_STORE", "legacy").strip().lower()
    if mode not in {"legacy", "postgres"}:
        raise BrokerStateError(
            "LEAF_BROKER_STORE must be 'legacy' or 'postgres'")
    return mode


def _drawing_store_mode() -> str:
    mode = os.environ.get("LEAF_DRAWING_STORE", "legacy").strip().lower()
    if mode not in {"legacy", "postgres"}:
        raise BrokerStateError(
            "LEAF_DRAWING_STORE must be 'legacy' or 'postgres'")
    return mode


def _postgres_store():
    global _pg_store
    if _pg_store is None:
        from broker_pg_store import get_store

        _pg_store = get_store()
    return _pg_store


# --------------------------------------------------------------------------- #
# egress allowlist — central guard: ANY outbound HTTP from this process must
# target an allowed host. Installed at import (before any da call can run).
# v1 note: live APS runs also touch OSS direct-to-S3 signed URLs, so
# *.amazonaws.com is included alongside the APS host. Extend via
# BROKER_EGRESS_EXTRA (comma-separated host suffixes).
# --------------------------------------------------------------------------- #
ALLOWED_HOSTS = {"developer.api.autodesk.com", "127.0.0.1", "localhost"}
ALLOWED_SUFFIXES = [".amazonaws.com"]
for extra in filter(None, os.environ.get("BROKER_EGRESS_EXTRA", "").split(",")):
    ALLOWED_SUFFIXES.append(extra.strip())


class EgressBlocked(Exception):
    pass


def _egress_allowed(host: str) -> bool:
    if host in ALLOWED_HOSTS:
        return True
    return any(host.endswith(sfx) for sfx in ALLOWED_SUFFIXES)


def _install_egress_guard() -> None:
    import requests.adapters

    orig_send = requests.adapters.HTTPAdapter.send

    def guarded_send(self, request, *args, **kwargs):  # noqa: ANN001
        host = urllib.parse.urlsplit(request.url).hostname or ""
        if not _egress_allowed(host):
            raise EgressBlocked(f"egress to {host!r} blocked by broker allowlist")
        return orig_send(self, request, *args, **kwargs)

    requests.adapters.HTTPAdapter.send = guarded_send  # type: ignore[method-assign]


_install_egress_guard()


# --------------------------------------------------------------------------- #
# tenant kill-switch (persisted)
# --------------------------------------------------------------------------- #
_MISSING_TENANT = object()  # absent record, as distinct from a corrupt one


class BrokerStateError(RuntimeError):
    """Raised when PRESENT broker state cannot be trusted.

    The broker is the sole credential holder and the home of the tenant kill
    switch, so refusing to start beats starting with every kill flag disarmed.
    """


class ApsCapacityUnavailable(RuntimeError):
    """Fleet-wide APS slot ceiling is full before any WorkItem submission."""


def _aps_max_concurrency() -> int:
    value = int(os.environ.get("APS_MAX_CONCURRENCY", "1"))
    if value < 1 or value > 100:
        raise BrokerStateError("APS_MAX_CONCURRENCY must be between 1 and 100")
    return value


def _aps_slot_lease_seconds() -> int:
    value = int(os.environ.get("APS_SLOT_LEASE_SECONDS", "900"))
    if value < 60 or value > 86400:
        raise BrokerStateError(
            "APS_SLOT_LEASE_SECONDS must be between 60 and 86400")
    return value


def _load_tenants() -> Dict[str, Any]:
    """The persisted tenant records.

    ABSENT is the only safe-empty case (first boot, nothing provisioned yet).
    A PRESENT file that cannot be parsed used to collapse to {} — and since this
    runs ONCE at import, that silently disarmed every tenant kill flag for the
    whole process lifetime AND, with no record to carry a tier, promoted every
    tenant to the friction-free `demo` default in _tenant_tier(). Corrupt state
    now refuses to load.
    """
    try:
        text = TENANTS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise BrokerStateError(f"broker tenants file unreadable at {TENANTS_PATH}: {exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BrokerStateError(f"broker tenants file invalid JSON at {TENANTS_PATH}: {exc}") from exc
    if not isinstance(raw, dict):
        raise BrokerStateError(f"broker tenants top level must be a mapping ({TENANTS_PATH})")
    return raw


def _save_tenants(t: Dict[str, Any]) -> None:
    TENANTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{TENANTS_PATH.name}.", suffix=".tmp", dir=TENANTS_PATH.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(t, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, TENANTS_PATH)
        try:
            dir_fd = os.open(TENANTS_PATH.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


_tenants: Dict[str, Any] = _load_tenants()


def tenant_disabled(tid: str) -> bool:
    """Whether this tenant is killed.

    `bool(...)` coerced by truthiness, so a PRESENT-but-falsy flag — `null`, `0`,
    `""` — read as ENABLED: exactly the direction a kill switch must never fail.
    Only an explicit False (or an absent flag) enables; anything unparseable
    disables, because "I cannot tell whether this tenant is killed" has one safe
    answer.
    """
    if _broker_store_mode() == "postgres":
        rec = _postgres_store().tenant(tid)
        return bool(rec["disabled"]) if rec is not None else False
    with _tenants_lock:
        rec = _tenants.get(tid, _MISSING_TENANT)
    if rec is _MISSING_TENANT:
        return False  # never provisioned -> not killed
    if not isinstance(rec, dict):
        return True  # corrupt record -> fail CLOSED
    if "disabled" not in rec:
        return False  # provisioned, flag never set -> not killed
    flag = rec["disabled"]
    if isinstance(flag, bool):
        return flag
    return True  # present but not a real boolean -> fail CLOSED


def set_tenant_disabled(tid: str, disabled: bool) -> None:
    global _tenants
    if _broker_store_mode() == "postgres":
        _postgres_store().set_tenant_disabled(tid, disabled)
        return
    with _tenants_lock:
        existing = _tenants.get(tid, _MISSING_TENANT)
        if existing is _MISSING_TENANT:
            rec: Dict[str, Any] = {}
        elif isinstance(existing, dict):
            rec = dict(existing)
        else:
            # A present-but-corrupt record: setdefault would return it unchanged
            # and the item-assignment below would raise a bare TypeError. Refuse
            # cleanly instead — and do NOT overwrite it blind, which could log a
            # kill as applied while the on-disk state stays corrupt.
            raise BrokerStateError(
                f"tenant {tid!r} has a corrupt record ({type(existing).__name__}); "
                f"refusing to write over it — repair broker_tenants.json first")
        rec["disabled"] = disabled
        rec["updated_at"] = time.time()
        candidate = dict(_tenants)
        candidate[tid] = rec
        _save_tenants(candidate)
        _tenants = candidate


# --------------------------------------------------------------------------- #
# attribution ledger — exactly ONE line per /broker/run
# --------------------------------------------------------------------------- #
def _conform_ledger_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Enforce the FROZEN leaf.broker-ledger-line.v1 types at the single append
    chokepoint. The wire model accepts arbitrary JSON inside the tool package
    (a non-string `name` or `engine_op` reaches the entry untyped), and cost
    blocks come from tool envelopes; conforming HERE means every written line
    — ok, denial, or garbage-input — is schema-valid."""
    tool = entry.get("tool")
    entry["tool"] = tool if isinstance(tool, str) else None
    engine_op = entry.get("engine_op")
    entry["engine_op"] = engine_op if isinstance(engine_op, str) else ""
    # A tool envelope is unchecked input too: `error: {error_code: null}` would
    # otherwise flow through .get("error_code", "error") as None (the default
    # only covers a MISSING key) and append a non-string status.
    status = entry.get("status")
    entry["status"] = status if isinstance(status, str) else "error"
    for key in ("engine_seconds", "usd_est"):
        val = entry.get(key)
        # Finite numbers only: NaN/Infinity would serialize as bare NaN/Infinity
        # tokens — not valid JSON, so not a valid ledger line. float() (and
        # math.isfinite) raise OverflowError on an int too large for a float
        # (e.g. 10**400) — this runs in broker_run's `finally`, so it must
        # NEVER raise: an oversized int conforms to null like any other
        # unusable number.
        num = None
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            try:
                num = float(val)
            except OverflowError:
                num = None
            if num is not None and not math.isfinite(num):
                num = None
        entry[key] = num
    return entry


def _validate_terminal_ledger_numbers(entry: Dict[str, Any]) -> None:
    """Reject accounting values that could erase or corrupt recorded spend."""
    for field in ("engine_seconds", "usd_est"):
        value = entry.get(field)
        if value is None:
            continue
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or float(value) < 0
        ):
            raise BrokerStateError(
                f"broker ledger {field} must be finite and nonnegative")


def _ledger_append(entry: Dict[str, Any], event_key: Optional[str] = None) -> None:
    # allow_nan=False: if a future unconformed field ever carries a non-finite
    # number, fail LOUD here rather than write an unparseable ledger line.
    conformed = _conform_ledger_entry(entry)
    if _broker_store_mode() == "postgres":
        raise BrokerStateError(
            "PostgreSQL ledger completion must use the admission state machine")
    line = json.dumps(conformed, separators=(",", ":"), allow_nan=False)
    with _ledger_lock:
        with open(LEDGER_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


# --------------------------------------------------------------------------- #
# da/client.py loader (lazy — only a live run touches it; import failure must
# not break broker boot)
# --------------------------------------------------------------------------- #
_da_mod = None
_blank_dwg_mod = None


def _in_test_process() -> bool:
    """True only while a pytest test is executing.

    Deliberately NOT `"pytest" in sys.modules`: server/requirements.txt ships
    pytest and deploy/Dockerfile.broker installs it into the production image, so
    any prod process that imports pytest for any reason would look like a test
    and silently lose live APS. `PYTEST_CURRENT_TEST` is set by pytest only for
    the duration of each test and is never present in a real server process.
    (sol-critic PR #117 round 2, blocker 3.)"""
    return "PYTEST_CURRENT_TEST" in os.environ


def _get_da():
    """Resolve the APS Design Automation client — the ONLY object that reads the
    real credential and can spend money.

    FAIL CLOSED UNDER TEST. The live path is chosen by the REQUEST field
    `aps_live`, not by the APS_LIVE env var, so the documented `APS_LIVE=0
    pytest` command does NOT prevent a test that passes `aps_live=True` from
    reaching real APS with the credentials at ~/.aps/credentials.json. Every
    such test today monkeypatches this function; a future one that forgets would
    silently submit a paid WorkItem.

    RAISE, do not return None. None is an ordinary "no credential" signal that
    callers absorb into a degraded pure-python write (see run_write_mock's
    degraded=True branch), which would turn a forgotten monkeypatch into a
    quietly WRONG passing test instead of a caught mistake. A test that
    genuinely wants the live client must opt in with
    LEAF_ALLOW_LIVE_APS_IN_TESTS=1.
    (sol-critic PR #117, round 1 blocker 6 and round 2 blocker 3.)"""
    if _in_test_process() and os.environ.get(
        "LEAF_ALLOW_LIVE_APS_IN_TESTS", "").strip().lower() not in ("1", "true", "yes", "on"):
        raise RuntimeError(
            "refusing to load the live APS client inside a test process: this call "
            "would spend real money against ~/.aps/credentials.json. Monkeypatch "
            "broker._get_da in this test, or set LEAF_ALLOW_LIVE_APS_IN_TESTS=1 to "
            "opt in deliberately."
        )

    global _da_mod
    if _da_mod is None:
        import importlib.util

        path = PROJECT_ROOT / "da" / "client.py"
        if not path.exists():
            return None
        try:
            spec = importlib.util.spec_from_file_location("da_client", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            _da_mod = mod
        except Exception as exc:  # noqa: BLE001
            print(f"[broker] da/client.py import failed: {exc}", file=sys.stderr)
            return None
    return _da_mod


def _get_blank_dwg_producer():
    """Load the broker-owned no-input producer without importing credentials."""
    global _blank_dwg_mod
    if _blank_dwg_mod is None:
        import importlib.util

        path = SERVER_DIR / "da" / "blank_dwg.py"
        spec = importlib.util.spec_from_file_location("leaf_blank_dwg", path)
        if spec is None or spec.loader is None:
            raise RuntimeError("blank DWG producer is unavailable")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _blank_dwg_mod = mod
    return _blank_dwg_mod


def _blank_dwg_read_tool() -> Dict[str, Any]:
    """Return the exact operator-owned read witness from the engine registry."""
    registry = json.loads((PROJECT_ROOT / "engine" / "registry.json").read_text(
        encoding="utf-8"))
    matches = [
        tool for tool in registry.get("tools", [])
        if tool.get("name") == "count-by-layer"
    ]
    if len(matches) != 1 or matches[0].get("engine_op") != "count_by_layer":
        raise RuntimeError("operator count-by-layer tool is unavailable")
    return dict(matches[0])


# --------------------------------------------------------------------------- #
# da/usage.py + da/reaper.py loaders (pure modules — no credential; safe to load
# in the broker, which is the metering + cap + reap chokepoint). Loaded by path
# under distinct names so they never depend on sys.path ordering.
# --------------------------------------------------------------------------- #
def _load_da_module(filename: str, mod_name: str):
    import importlib.util

    path = PROJECT_ROOT / "da" / filename
    if not path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod
    except Exception as exc:  # noqa: BLE001
        print(f"[broker] {filename} import failed: {exc}", file=sys.stderr)
        return None


def _load_server_da_module(filename: str, mod_name: str):
    """Load a broker-local DA seam without importing the deployable ``da`` client."""
    import importlib.util

    path = SERVER_DIR / "da" / filename
    if not path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        return mod
    except Exception as exc:  # noqa: BLE001
        print(f"[broker] server/da/{filename} import failed: {exc}", file=sys.stderr)
        return None


_usage_mod = None
_reaper_mod = None
_callbacks_mod = None


def _get_usage():
    global _usage_mod
    if _usage_mod is None:
        _usage_mod = _load_da_module("usage.py", "leaf_usage")
    return _usage_mod


def _get_reaper():
    global _reaper_mod
    if _reaper_mod is None:
        _reaper_mod = _load_da_module("reaper.py", "leaf_reaper")
    return _reaper_mod


def _get_callbacks():
    """Load the broker-owned callback seam without importing the root ``da`` package."""
    global _callbacks_mod
    if _callbacks_mod is None:
        _callbacks_mod = _load_server_da_module("callbacks.py", "leaf_broker_callbacks")
    return _callbacks_mod


class CallbackPrimaryUnavailable(RuntimeError):
    """The reserved callback-primary flag was selected."""


class CallbackPrimaryConfigurationError(RuntimeError):
    """Callback-primary was selected without all required operator settings."""


def _callback_primary_requested() -> bool:
    """Read the broker-owned posture without depending on the callback module."""
    return os.environ.get("LEAF_CALLBACK_PRIMARY", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _require_supported_live_completion_mode() -> None:
    """Reject the reserved callback mode before any APS live side effect."""
    callbacks = _get_callbacks()
    if callbacks is None:
        if _callback_primary_requested():
            raise CallbackPrimaryUnavailable(
                "callback-primary requested but the callback module is unavailable; "
                "refusing the APS live run"
            )
        return
    config_error = callbacks.callback_primary_configuration_error()
    if config_error:
        raise CallbackPrimaryConfigurationError(config_error)
    if callbacks.callback_primary_enabled():
        raise CallbackPrimaryUnavailable(
            "callback-primary is reserved: the APS-to-Leaf callback translation "
            "adapter is a follow-up; native APS onComplete does not satisfy the "
            "signed /da/callback contract"
        )


def _accepts_on_submitted(fn: Any) -> bool:
    """Whether `fn` can take an `on_submitted` kwarg.

    The live client is resolved at runtime and is a test double in most suites,
    so the kwarg is offered only to implementations that declare it (explicitly
    or via **kwargs). An older/stubbed run_tool keeps its exact call signature.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if "on_submitted" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _run_live_tool(da: Any, local: str, tool: Dict[str, Any], params: Dict[str, Any],
                   on_submitted=None) -> Dict[str, Any]:
    """Select the configured completion mechanism for one live run.

    The polling call is deliberately byte-for-byte compatible by default.
    Callback-primary is reserved until a translation adapter can turn native
    APS completion metadata into the signed Leaf receipt envelope. Selecting
    the flag fails closed without submitting or silently polling. The existing
    reaper remains available for abandoned polling work.

    `on_submitted` (set only when the caller supplied a job_id) is forwarded so
    the WorkItem id is registered before the poll blocks.
    """
    _require_supported_live_completion_mode()
    if on_submitted is not None and _accepts_on_submitted(da.run_tool):
        return da.run_tool(local, tool, params, on_submitted=on_submitted)
    return da.run_tool(local, tool, params)


def _cap_preflight(tenant_id: str, tool: Dict[str, Any]):
    """Broker-side HARD pre-flight cost cap (the kill-switch chokepoint).

    Returns (quota_env, http_status) to REJECT, or None to proceed. OFF unless a
    positive cap is configured for the tenant (env LEAF_TENANT_CAP_USD, per-tenant
    LEAF_USAGE_CAPS[_FILE]) — so a demo/backbone run with no cap configured is
    never gated and this adds no ledger I/O. The broker ledger is the
    AUTHORITATIVE prior-spend source; usage.py is the local fallback.
    """
    usage = _get_usage()
    if usage is None:
        return None
    cap = usage.cap_for(tenant_id)
    if cap is None:
        return None  # uncapped -> proceed (no ledger read)
    if _broker_store_mode() == "postgres":
        spent = _postgres_store().spent_usd(tenant_id)
    else:
        spent = usage.spent_from_broker_ledger(tenant_id, LEDGER_PATH)
    decision = usage.check_cap(tenant_id, cap=cap, spent=spent, tool=tool.get("name"))
    if decision.get("ok"):
        return None
    # reject: coerce degraded_mode to a boolean for the schema-valid wire body
    env = dict(decision)
    env["degraded_mode"] = False
    return env, 402  # 402 Payment Required — tenant spend cap reached


def _run_quota_preflight(tenant_id: str, tier: str, tool: Dict[str, Any]):
    """Broker-side coarse per-tenant DAILY RUN quota (F12 + A4) — a COUNT-based cap on
    APS-money runs/tenant/UTC-day, keyed on ``tier``, standing ALONGSIDE the USD spend
    cap (_cap_preflight) and ORDER-INDEPENDENT of it.

    Returns ``(quota_env, 429)`` to REJECT, or ``None`` to proceed. The count is the
    tenant's APS_LIVE=1 runs today from the AUTHORITATIVE broker ledger (spec §a excludes
    mock/free runs); a new UTC day resets the window with NO cron (the count keys on
    YYYY-MM-DD). Unmetered tiers (hosted_pro) always proceed. Unlike the spend cap (OFF
    unless a cap is configured) this guard is ON BY DEFAULT for metered tiers (free N=20)
    — a demo tenant on the live path is capped, never uncapped — and a DIFFERENT tenant
    under its own cap is unaffected. The caller invokes this only on the APS_LIVE=1 path.
    If the usage module can't load, the guard is absent and the run is not blocked
    (fail-open only when the guard itself is missing)."""
    usage = _get_usage()
    if usage is None:
        return None
    if _broker_store_mode() == "postgres":
        limit = usage.daily_run_limit_for(tier)
        used = _postgres_store().daily_live_run_count(tenant_id)
        if limit is None:
            decision = {
                "ok": True, "metered": False, "tier": tier,
                "limit": None, "used": used,
            }
        elif used >= limit:
            decision = usage.daily_quota_envelope(
                tenant_id, tier, limit, used, tool=tool.get("name"))
            decision.update({
                "metered": True, "tier": tier, "limit": limit, "used": used,
            })
        else:
            decision = {
                "ok": True, "metered": True, "tier": tier,
                "limit": limit, "used": used,
            }
    else:
        decision = usage.daily_run_quota_check(tenant_id, tier, LEDGER_PATH)
    if decision.get("ok"):
        return None
    # reject: coerce degraded_mode to a boolean for the schema-valid wire body
    env = dict(decision)
    env["degraded_mode"] = False
    return env, 429  # 429 Too Many Requests — daily run cap reached (distinct from 402 spend cap)


# --------------------------------------------------------------------------- #
# Deployed-posture authored-execution containment.
#
# LEAF_RUNTIME_ENV is an explicit deployment posture. In production, authored
# execution defaults OFF and can turn on only when both the operator flag and
# an approved sandbox tier are present. In any deployed posture (staging or
# production) authored execution requires an engaged sandbox tier, whether the
# flag is explicit or defaulted on. Local/demo behavior stays unchanged.
# --------------------------------------------------------------------------- #
def _production_runtime() -> bool:
    return os.environ.get("LEAF_RUNTIME_ENV", "").strip().lower() == "production"


def _deployed_runtime() -> bool:
    """True when LEAF_RUNTIME_ENV names a real deployed posture.

    Staging and production are the two deployed postures (the same primary
    signal routers/demand.py::_deployed_runtime uses; the deploy contract
    requires LEAF_RUNTIME_ENV in every deployed task definition). Local, demo,
    dev, and test runs leave the variable unset or use another value and never
    enter the deployed branch.
    """
    return os.environ.get("LEAF_RUNTIME_ENV", "").strip().lower() in (
        "staging", "production")


def _authored_execution_enabled() -> bool:
    raw = os.environ.get("LEAF_AUTHORED_EXECUTION")
    if raw is None:
        return not _production_runtime()
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _authored_execution_explicitly_armed() -> bool:
    """True only when LEAF_AUTHORED_EXECUTION is EXPLICITLY set to a truthy value.

    Distinct from `_authored_execution_enabled()`, which defaults ON outside the
    production posture. The sandbox floor in `validate_runtime_safety` fires on
    the EXPLICIT flag in every posture, and additionally on the EFFECTIVE
    (defaulted-on) value in a deployed posture, so only local/demo deployments
    (no deployed LEAF_RUNTIME_ENV, authored execution defaulted on, sandbox off)
    keep their existing in-process behavior byte-for-byte.
    """
    raw = os.environ.get("LEAF_AUTHORED_EXECUTION")
    return raw is not None and raw.strip().lower() in ("1", "true", "yes", "on")


def _sandbox_configured() -> bool:
    return os.environ.get("LEAF_TOOL_SANDBOX_PROVIDER", "").strip().lower() == "e2b"


def validate_runtime_safety() -> None:
    """Reject unsafe or ambiguous production broker configuration."""
    # Posture-INDEPENDENT fail-closed floor. Tenant-authored tool code must
    # NEVER execute in-process in this credential-holding broker. The floor
    # fires when authored execution is EXPLICITLY armed (any posture) and ALSO
    # when it is merely EFFECTIVELY enabled -- the flag unset and defaulting on
    # -- in a DEPLOYED posture (staging/production), where an unset flag is a
    # misconfiguration, not a dev convenience. This catches both the silent
    # footgun where LEAF_SANDBOX="1" reads as a non-tier value and the staging
    # counterexample where the flag is simply absent. Only local/demo (no
    # deployed posture, flag unset) keeps the in-process default.
    if _authored_execution_explicitly_armed() or (
            _deployed_runtime() and _authored_execution_enabled()):
        tier = _tool_sandbox_tier()
        if tier not in ("subprocess", "microvm"):
            raise RuntimeError(
                "authored execution is enabled (LEAF_AUTHORED_EXECUTION="
                f"{os.environ.get('LEAF_AUTHORED_EXECUTION')!r}; unset defaults "
                "on outside production) without an engaged sandbox tier: set "
                "LEAF_TOOL_SANDBOX_PROVIDER=e2b (or LEAF_SANDBOX=e2b|e2b-microvm)"
                ". Refusing to start with tenant tool code able to execute "
                f"in-process (sandbox tier={tier!r})."
            )
    if not _production_runtime():
        return
    if not os.environ.get(BROKER_SECRET_ENV, "").strip():
        raise RuntimeError("production broker requires nonblank LEAF_BROKER_SECRET")
    if not _auth_live():
        raise RuntimeError("production broker requires LEAF_AUTH_LIVE=1")
    if os.environ.get("LEAF_QA_HOOKS", "").strip() != "0":
        raise RuntimeError("production broker requires explicit LEAF_QA_HOOKS=0")
    if _broker_store_mode() != "postgres":
        raise RuntimeError("production broker requires LEAF_BROKER_STORE=postgres")
    if _drawing_store_mode() != "postgres":
        raise RuntimeError("production broker requires LEAF_DRAWING_STORE=postgres")
    if os.environ.get("LEAF_UPLOAD_STORE", "").strip().lower() != "postgres":
        raise RuntimeError("production broker requires LEAF_UPLOAD_STORE=postgres")
    if write_loop.blob_store_mode() != "filesystem":
        raise RuntimeError("production broker requires LEAF_BLOB_STORE=filesystem")
    if not os.environ.get("DATABASE_URL", "").strip():
        raise RuntimeError("production broker requires DATABASE_URL")
    if not os.environ.get("LEAF_DRAWING_MUTATIONS_FENCE_FILE", "").strip():
        raise RuntimeError(
            "production broker requires LEAF_DRAWING_MUTATIONS_FENCE_FILE")
    if _authored_execution_enabled() and not _sandbox_configured():
        raise RuntimeError(
            "production authored execution requires LEAF_TOOL_SANDBOX_PROVIDER=e2b"
        )
    if _authored_execution_enabled() and not (
            os.environ.get("E2B_API_KEY", "").strip()
            or os.environ.get("E2B_API_KEY_FILE", "").strip()):
        raise RuntimeError(
            "production tool sandbox requires an E2B credential source"
        )


@asynccontextmanager
async def _broker_lifespan(_app: FastAPI):
    """Run deployment safety checks on supported FastAPI and Starlette releases."""
    validate_runtime_safety()
    if _drawing_store_mode() == "postgres":
        _postgres_store().validate_drawing_schema()
    if _broker_store_mode() == "postgres":
        _postgres_store().validate_schema()
    callbacks = _get_callbacks()
    if callbacks is not None:
        callbacks.validate_replay_store_startup()
    import jobs
    jobs.validate_store_startup()
    yield


# --------------------------------------------------------------------------- #
# app
# --------------------------------------------------------------------------- #
app = FastAPI(
    title="Leaf APS broker v1",
    version="1.0.0",
    lifespan=_broker_lifespan,
)
install_error_handlers(app)


# --------------------------------------------------------------------------- #
# F4(a): caller-auth on every /broker/* route (health stays OPEN for liveness
# probes). The app→broker client (broker_client.py) sends X-Broker-Secret read
# from the SAME env; Codex injects the value at deploy. Discipline:
#   * secret SET             -> ALWAYS enforced, constant-time (hmac.compare_digest)
#   * live mode + secret unset -> 503 (fail-closed: refuse to serve an exposed,
#                                       mis-configured broker)
#   * off-live + secret unset  -> friction-free demo (byte-identical to today)
# Raising HTTPException routes through install_error_handlers -> a §10 envelope
# body with the correct status (401 wrong/absent, 503 unconfigured).
# --------------------------------------------------------------------------- #
BROKER_SECRET_ENV = "LEAF_BROKER_SECRET"
BROKER_RECONCILE_SECRET_ENV = "LEAF_BROKER_RECONCILE_SECRET"


def _broker_secret() -> Optional[str]:
    val = os.environ.get(BROKER_SECRET_ENV, "").strip()
    return val if val else None


def _auth_live() -> bool:
    """Live/public mode — read at call time so one process can be toggled.

    Delegates to deps.auth_live(), THE canonical LEAF_AUTH_LIVE parser: a private
    exact-"1" copy here once meant `LEAF_AUTH_LIVE=true` turned server auth on
    while leaving this broker gate in demo posture (split authentication)."""
    import deps  # lazy: avoid import cycle at module load

    return deps.auth_live()


def _qa_hooks_enabled() -> bool:
    """F12: whether QA-only latency/test hooks (the ``_qa_sleep_s`` run param) are
    HONORED. Explicit env ``LEAF_QA_HOOKS`` wins (``1``/``true``/``yes``/``on`` -> ON,
    anything else -> OFF). When UNSET the default tracks the deployment posture, mirroring
    the friction-free-demo / fail-closed-live broker-auth rule:

        * local / demo / test (LEAF_AUTH_LIVE != 1)  -> ON   (the backbone + tests use it)
        * live / public prod   (LEAF_AUTH_LIVE == 1)  -> OFF  (a tenant can't starve the
                                                               shared worker pool with a
                                                               large sleep)

    So the EXACT default is: ON everywhere EXCEPT live/public mode, and a literal
    ``LEAF_QA_HOOKS=0`` forces it OFF even locally.
    """
    raw = os.environ.get("LEAF_QA_HOOKS")
    if raw is not None:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return not _auth_live()


def require_broker_auth(x_broker_secret: Optional[str] = Header(default=None)) -> None:
    """Reject any /broker/* caller lacking the shared secret. Never logs the secret."""
    secret = _broker_secret()
    if secret is None:
        if _auth_live():
            raise HTTPException(
                status_code=503,
                detail="broker caller-auth is not configured (LEAF_BROKER_SECRET unset)",
            )
        return  # off-live demo: no secret required (unchanged behaviour)
    provided = x_broker_secret or ""
    if not hmac.compare_digest(provided.encode("utf-8"), secret.encode("utf-8")):
        raise HTTPException(status_code=401, detail="invalid or missing X-Broker-Secret")


def require_broker_reconcile_auth(
    x_broker_secret: Optional[str] = Header(default=None),
    x_broker_reconcile_secret: Optional[str] = Header(default=None),
) -> None:
    """Stronger operator-only gate for irreversible admission resolution."""
    require_broker_auth(x_broker_secret)
    secret = os.environ.get(BROKER_RECONCILE_SECRET_ENV, "").strip()
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="broker reconciliation is not configured",
        )
    presented = (x_broker_reconcile_secret or "").strip()
    if not hmac.compare_digest(
            presented.encode("utf-8"), secret.encode("utf-8")):
        raise HTTPException(
            status_code=401,
            detail="invalid or missing X-Broker-Reconcile-Secret",
        )


# --------------------------------------------------------------------------- #
# F10: broker-side tier ENTITLEMENT re-check (defense-in-depth) — a DIRECT broker
# call must not bypass the app-side tier gate. The tier is resolved from a
# broker-TRUSTED source keyed by tenant_id (NEVER the request body — a direct
# caller could forge it): the persisted broker tenants record, then env
# LEAF_BROKER_TENANT_TIERS (JSON {tenant_id: tier}). Unknown tenant -> "demo"
# (friction-free; matches the platform's open-demo design and keeps the async
# spine unbroken). Operators provision real tiers here to make the re-check bite.
# --------------------------------------------------------------------------- #
class _TenantTier:
    """Minimal tenant carrier so entitlements.resolve_tier applies its own fail-closed
    rule (present-but-empty tier -> 'restricted')."""

    __slots__ = ("tier",)

    def __init__(self, tier: Any) -> None:
        self.tier = tier


def _provisioned_tier(tenant_id: str) -> Optional[str]:
    if _broker_store_mode() == "postgres":
        rec = _postgres_store().tenant(tenant_id)
        if rec is not None and rec.get("tier") is not None:
            return str(rec.get("tier") or "")
        return None
    with _tenants_lock:
        rec = _tenants.get(tenant_id, _MISSING_TENANT)
    if rec is not _MISSING_TENANT and not isinstance(rec, dict):
        # A corrupt record is not an unprovisioned one: falling through would end
        # at `provisioned is None` -> DEFAULT_TIER (demo, full access). Unknown
        # must resolve restricted instead.
        return entitlements.RESTRICTED_TIER
    if isinstance(rec, dict) and "tier" in rec:
        return str(rec.get("tier") or "")
    raw = os.environ.get("LEAF_BROKER_TENANT_TIERS")
    if raw:
        try:
            m = json.loads(raw)
        except Exception:  # noqa: BLE001
            m = None
        if isinstance(m, dict) and tenant_id in m:
            return str(m.get(tenant_id) or "")
    return None


def _tenant_tier(tenant_id: str) -> str:
    provisioned = _provisioned_tier(tenant_id)
    if provisioned is None:
        return entitlements.DEFAULT_TIER  # no verified tier at the broker -> friction-free demo
    return entitlements.resolve_tier(_TenantTier(provisioned))


# --------------------------------------------------------------------------- #
# job_id -> live WorkItem correlation (tab-close reaping)
# --------------------------------------------------------------------------- #
# The app/jobs side knows a job_id; only THIS process ever sees the APS WorkItem
# id, and only for the duration of the blocking poll inside da/client. Without a
# mapping between the two, POST /broker/reap arrives with `workitem_id: None` and
# there is nothing to cancel -- a closed tab leaves paid compute running to
# completion. This registry is that mapping: written the moment the WorkItem is
# submitted, dropped when broker_run leaves (success, failure, or timeout), so it
# only ever holds genuinely in-flight runs.
#
# Each entry is (workitem_id, owner_token, recorded_at). The OWNER matters: a
# redelivery of the same job POSTs the same job_id, is rejected as already
# leased/executing, and then runs the same `finally` as a real run. Without
# ownership that duplicate would evict the ORIGINAL run's correlation and make a
# live, billing WorkItem permanently unreapable. A run therefore evicts ONLY the
# entry it registered itself -- an EXACT token match. Entries replayed from disk
# after a restart carry no owner, so no run can evict them; they are cleared by a
# successful cancel, replaced when the job runs again, or aged out at replay.
_active_workitems: Dict[str, tuple] = {}
_active_workitems_lock = threading.Lock()

# Correlations are also appended to disk so they survive a broker restart. The
# WorkItem outlives this process -- APS keeps running and billing it -- so a
# restart must not be what makes an abandoned run uncancellable.
ACTIVE_WORKITEMS_PATH = Path(
    os.environ.get("BROKER_ACTIVE_WORKITEMS_PATH",
                   str(Path(LEDGER_PATH).parent / "active_workitems.jsonl")))
_ACTIVE_WORKITEMS_COMPACT_LINES = 2000
# A garbage-collection backstop, NOT a liveness claim. da/client's 900s poll
# ceiling does not bound the WorkItem: _poll_workitem simply returns once it
# expires and issues no DELETE, so the run can still be executing on APS long
# after this process stopped watching it. The TTL therefore has to sit beyond
# any plausible WorkItem lifetime -- discarding a correlation early would throw
# away the only means of cancelling something still billing -- and exists only
# so the recovered set cannot grow forever across restarts.
ACTIVE_WORKITEM_TTL_S = float(os.environ.get("BROKER_ACTIVE_WORKITEM_TTL_S", "86400"))
_sidecar_lines = 0


def _persist_workitem_event_locked(event: str, job_id: str,
                                   workitem_id: Optional[str]) -> None:
    """Append one open/close event. CALLER MUST HOLD `_active_workitems_lock`.

    The append is ordered with the in-memory mutation on purpose: appending
    outside the lock lets two threads write in the opposite order from the
    order they mutated memory, so a `close` can land before the `open` it
    closes and a restart resurrects a WorkItem that already finished.

    Best-effort: a broken sidecar must never fail a paid run, it only costs
    restart recovery.
    """
    global _sidecar_lines
    try:
        ACTIVE_WORKITEMS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with ACTIVE_WORKITEMS_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": event, "job_id": job_id,
                                 "workitem_id": workitem_id,
                                 "ts": time.time()}) + "\n")
        _sidecar_lines += 1
    except Exception as exc:  # noqa: BLE001
        print(f"[leaf-broker] could not persist workitem correlation "
              f"({event} {job_id}): {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        return
    if _sidecar_lines > _ACTIVE_WORKITEMS_COMPACT_LINES:
        # Checked at RUNTIME, not only at import: a long-lived broker would
        # otherwise cross the bound once and grow unchecked forever after.
        # The TTL is applied HERE too, not only at replay: disowned entries are
        # cleared by a cancel or a replacement, and without ageing them out at
        # runtime a process that never restarts would keep every one it ever
        # disowned, and then rewrite that growing map on every event.
        now = time.time()
        for stale in [job_id for job_id, entry in _active_workitems.items()
                      if (now - entry[2]) > ACTIVE_WORKITEM_TTL_S]:
            del _active_workitems[stale]
        _compact_persisted_workitems(dict(_active_workitems))
        _sidecar_lines = len(_active_workitems)


def _replay_persisted_workitems() -> Dict[str, tuple]:
    """Rebuild the open correlations from the sidecar.

    Entries come back UNOWNED (token None): the process that owned them is gone.
    Anything older than ACTIVE_WORKITEM_TTL_S is discarded -- that WorkItem has
    certainly finished, and keeping it would only grow the set forever.
    """
    global _sidecar_lines
    open_map: Dict[str, tuple] = {}
    lines = 0
    now = time.time()
    try:
        if not ACTIVE_WORKITEMS_PATH.exists():
            return open_map
        with ACTIVE_WORKITEMS_PATH.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                lines += 1
                rec = json.loads(line)
                job_id = rec.get("job_id")
                if not job_id:
                    continue
                if rec.get("event") == "open" and rec.get("workitem_id"):
                    open_map[job_id] = (str(rec["workitem_id"]), None,
                                        float(rec.get("ts") or now))
                else:
                    open_map.pop(job_id, None)
    except Exception as exc:  # noqa: BLE001
        print(f"[leaf-broker] could not replay workitem correlations: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return open_map
    open_map = {job_id: entry for job_id, entry in open_map.items()
                if (now - entry[2]) <= ACTIVE_WORKITEM_TTL_S}
    _sidecar_lines = lines
    if lines > _ACTIVE_WORKITEMS_COMPACT_LINES:
        _compact_persisted_workitems(open_map)
        _sidecar_lines = len(open_map)
    return open_map


def _compact_persisted_workitems(open_map: Dict[str, tuple]) -> None:
    """Rewrite the sidecar as just the still-open correlations."""
    try:
        tmp = ACTIVE_WORKITEMS_PATH.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for job_id, entry in open_map.items():
                fh.write(json.dumps({"event": "open", "job_id": job_id,
                                     "workitem_id": entry[0],
                                     "ts": entry[2]}) + "\n")
        tmp.replace(ACTIVE_WORKITEMS_PATH)
    except Exception as exc:  # noqa: BLE001
        print(f"[leaf-broker] could not compact workitem correlations: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


def _record_active_workitem(job_id: str, workitem_id: Optional[str],
                            run_token: Optional[str] = None) -> None:
    if not job_id or not workitem_id:
        return
    with _active_workitems_lock:
        _active_workitems[job_id] = (str(workitem_id), run_token, time.time())
        _persist_workitem_event_locked("open", job_id, str(workitem_id))


def _drop_active_workitem(job_id: Optional[str],
                          run_token: Optional[str] = None,
                          expected_workitem_id: Optional[str] = None) -> Optional[str]:
    """Evict and return job_id's WorkItem id, subject to two guards.

    `run_token` (a run leaving): evicts ONLY on an exact owner match, so neither
    a duplicate delivery nor a run that registered nothing can drop a correlation
    it does not own -- including one recovered from disk after a restart, which
    has no owner and must survive until it is cancelled or replaced.

    `expected_workitem_id` (the reap path): evicts only if the entry still names
    the WorkItem that was actually cancelled. Resolution snapshots an id before
    the DELETE; a new run for the same job can install a fresh correlation while
    that DELETE is in flight, and dropping by job_id alone would throw away the
    NEW, live WorkItem's only means of cancellation.
    """
    if not job_id:
        return None
    with _active_workitems_lock:
        current = _active_workitems.get(job_id)
        if current is None:
            return None
        workitem_id, owner, _recorded_at = current
        if run_token is not None and owner != run_token:
            return None  # not this run's correlation: leave it alone
        if expected_workitem_id is not None and workitem_id != str(expected_workitem_id):
            return None  # a newer correlation replaced the one we cancelled
        del _active_workitems[job_id]
        _persist_workitem_event_locked("close", job_id, workitem_id)
    return workitem_id


def _disown_active_workitem(job_id: Optional[str], run_token: Optional[str]) -> bool:
    """Give up ownership of job_id's correlation WITHOUT closing it.

    Used when a run ends but its WorkItem may still be executing: da/client's
    poll simply RETURNS when its ceiling expires (it issues no DELETE), and a
    mid-poll exception leaves the same state. Dropping there would persist a
    `close` for something still running and billing, and nothing could ever
    address it again. Disowning keeps the correlation reapable by a later beacon
    while making sure this finished run cannot later evict it. Growth is bounded
    by the replay TTL and sidecar compaction.
    """
    if not job_id:
        return False
    with _active_workitems_lock:
        current = _active_workitems.get(job_id)
        if current is None:
            return False
        workitem_id, owner, recorded_at = current
        if run_token is not None and owner != run_token:
            return False
        _active_workitems[job_id] = (workitem_id, None, recorded_at)
        return True


def _owned_workitem_for(job_id: Optional[str],
                        run_token: Optional[str]) -> Optional[str]:
    """job_id's WorkItem id, but only if `run_token` registered it."""
    if not job_id or not run_token:
        return None
    with _active_workitems_lock:
        current = _active_workitems.get(job_id)
        if current is None or current[1] != run_token:
            return None
        return current[0]


def _reap_or_disown_own_workitem(job_id: Optional[str],
                                 run_token: Optional[str]) -> None:
    """End-of-run cleanup for a WorkItem that may still be executing.

    Waiting for a later beacon does not work here: by the time the app sees this
    run fail it makes the job row terminal, and the orphan sweep only selects
    submitted/running rows, so no close ever arrives for it. The broker is also
    the only process holding the credential. So it cancels its own orphan.

    Best effort, and never fatal -- this runs in broker_run's `finally`, where
    raising would replace the real response. If the cancel does not happen the
    correlation is disowned instead: kept, still reapable, no longer evictable by
    this finished run.
    """
    if not job_id:
        return
    try:
        # OWNERSHIP FIRST. Every early return in broker_run -- a duplicate
        # delivery rejected as leased/executing, a kill-switched tenant, a quota
        # rejection -- leaves terminal_env unset and lands here, and none of them
        # registered anything. Cancelling by job_id alone would issue a DELETE
        # against the WorkItem of the run that IS in flight. A run may only reap
        # the correlation it registered itself.
        workitem_id = _owned_workitem_for(job_id, run_token)
        if not workitem_id:
            return
        reaper = _get_reaper()
        if reaper is not None and reaper.reap_live_enabled():
            outcome = reaper.cancel_client_for(
                live=True, da_client=_get_da()).cancel(workitem_id)
            if isinstance(outcome, dict) and outcome.get("cancelled"):
                _settle_cancelled_workitem(job_id, workitem_id)
                return
    except Exception as exc:  # noqa: BLE001
        print(f"[leaf-broker] could not cancel the orphaned WorkItem for job "
              f"{job_id}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    _disown_active_workitem(job_id, run_token)


def _settle_cancelled_workitem(job_id: Optional[str],
                               cancelled_workitem_id: Optional[str]) -> bool:
    """Evict `cancelled_workitem_id` and report whether the JOB is now settled.

    One lock acquisition on purpose. Deciding "did anything survive?" after the
    eviction released the lock leaves a window in which a replacement run
    registers a fresh, live correlation and the job is acknowledged anyway --
    telling the caller to stop retrying while the NEW WorkItem is still billing.

    Settled means nothing of this job is in flight: either the entry we just
    cancelled was the current one, or there was no correlation at all.
    """
    if not job_id:
        return False
    with _active_workitems_lock:
        current = _active_workitems.get(job_id)
        if current is None:
            return True  # nothing in flight to keep retrying for
        workitem_id, _owner, _recorded_at = current
        if cancelled_workitem_id is not None and workitem_id != str(cancelled_workitem_id):
            return False  # a newer WorkItem is live: do NOT acknowledge
        del _active_workitems[job_id]
        _persist_workitem_event_locked("close", job_id, workitem_id)
        return True


def active_workitem_for(job_id: Optional[str]) -> Optional[str]:
    """Read job_id's in-flight WorkItem id without evicting it."""
    if not job_id:
        return None
    with _active_workitems_lock:
        current = _active_workitems.get(job_id)
        return current[0] if current else None


_active_workitems.update(_replay_persisted_workitems())


def _submission_recorder(req: "Union[BrokerRunRequest, BrokerPlanRunRequest]",
                         run_token: Optional[str]):
    """The `on_submitted` callback for one run, or None when there is nothing to
    correlate. No job_id -> no callback -> the call is byte-for-byte unchanged."""
    if not req.job_id:
        return None
    return functools.partial(_record_active_workitem, req.job_id,
                             run_token=run_token)


class BrokerRunRequest(BaseModel):
    tenant_id: str
    tool: Dict[str, Any]
    params: Dict[str, Any] = {}
    dwg: str = "rooftop_demo"
    aps_live: bool = False
    # Exact validate_tool output for a design-time staged broker test. The
    # broker accepts it only when aps_live=false and executes it only inside the
    # configured sandbox. It is never written to the ledger.
    test_source: Optional[str] = None
    # None -> head (unchanged); otherwise pin to an immutable drawing version.
    dwg_version: Optional[int] = None
    # Required in PostgreSQL mode. Use one durable key across job redeliveries.
    ledger_event_key: Optional[str] = None
    # Single-writer identity of the caller that submitted this run, carried from
    # POST /api/run (where it is spelled `holder`/`fence`, matching the public
    # checkout vocabulary) so the store can refuse a version published under
    # ANOTHER session's checkout. Qualified names here because this model has no
    # other checkout context. Both optional, so an older app that sends neither
    # behaves exactly as before — which is also why they are absent from
    # _broker_request_fingerprint below: the fingerprint identifies the WORK, and
    # two sessions asking for the same tool+params are the same work even when
    # only one of them is authorized to publish the result.
    checkout_holder: Optional[str] = None
    checkout_fence: Optional[int] = None
    # Optional durable job identity. When present, this run's live WorkItem id is
    # registered against it so /broker/reap can cancel it on tab close. Omitting
    # it leaves behaviour and the response shape byte-for-byte unchanged. It is
    # deliberately NOT part of _broker_request_fingerprint: the fingerprint
    # identifies the WORK, and adding a per-job field would make an existing
    # ledger row's fingerprint unrecognisable on replay.
    job_id: Optional[str] = None


class PlanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    drawing_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")
    parent_version: int = Field(ge=1, strict=True)
    mutations: Dict[str, Any]
    plan_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    source_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


class BrokerPlanRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    plan: PlanBody
    dwg: str
    dwg_version: int = Field(strict=True)
    ledger_event_key: Optional[str] = None
    checkout_holder: Optional[str] = None
    checkout_fence: Optional[int] = None
    job_id: Optional[str] = None

    @model_validator(mode="after")
    def consistent_plan_identity(self) -> BrokerPlanRunRequest:
        if self.dwg != self.plan.drawing_id:
            raise ValueError("plan request names a different drawing")
        if self.dwg_version != self.plan.parent_version:
            raise ValueError("plan request names a different parent")
        return self


class _BlankDwgBrokerRunRequest(BrokerRunRequest):
    """Internal-only run type. The public /broker/run parser never creates it."""


class BlankDwgFeasibilityRequest(BaseModel):
    """Closed input for the protected one-shot feasibility route."""
    tenant_id: str
    project_id: str
    ledger_event_key: str
    job_id: str
    source_sha: str
    drawing_name: str = "APS blank drawing feasibility"


def _blank_dwg_tool() -> Dict[str, Any]:
    return {
        "name": "aps-blank-dwg-feasibility",
        "version": "1.0.0",
        "kind": "broker_operator",
        "engine_op": "aps_blank_dwg_feasibility",
        "params": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "source_sha": {"type": "string"},
                "drawing_name": {"type": "string"},
            },
            "required": ["project_id", "source_sha", "drawing_name"],
            "additionalProperties": False,
        },
        "capabilities": ["drawing.write"],
        "provenance": {"source": "server/da/blank_dwg.py"},
    }


def _is_blank_dwg_tool(tool: Dict[str, Any]) -> bool:
    return (
        tool.get("name") == "aps-blank-dwg-feasibility"
        and tool.get("engine_op") == "aps_blank_dwg_feasibility"
        and tool.get("provenance") == {"source": "server/da/blank_dwg.py"}
    )


def _is_blank_dwg_request(req: BrokerRunRequest, tool: Dict[str, Any]) -> bool:
    return isinstance(req, _BlankDwgBrokerRunRequest) and _is_blank_dwg_tool(tool)


def _publish_blank_dwg(
    payload: bytes, digest: str, *, tenant_id: str, project_id: str, drawing_name: str
) -> Dict[str, Any]:
    """Publish the validated bytes as one tenant and project-owned version 1."""
    import store as drawing_store  # da/store.py, made importable by write_loop

    org_id = uuid.UUID(tenant_id)
    project_uuid = uuid.UUID(project_id)
    canonical = platform_link.platform_store()
    project = canonical.get_project(org_id, project_uuid)
    if project is None or project.status != "active":
        raise ValueError("project is unavailable for this tenant")

    artifact = canonical.create_drawing_artifact(org_id, project_uuid, drawing_name)
    drawing_id = str(artifact.drawing_id)
    backend = write_loop.default_backend(aps_live=True, da=_get_da())
    with tempfile.TemporaryDirectory(prefix="leaf-blank-dwg-publish-") as tmp:
        local = Path(tmp) / "blank.dwg"
        local.write_bytes(payload)
        stored = drawing_store.ingest_drawing(
            backend, tenant_id, str(local), drawing_id=drawing_id
        )
    if stored != {"drawing_id": drawing_id, "version": 1}:
        raise RuntimeError("blank DWG store did not publish exact version 1")
    object_key = drawing_store.drawing_version_key(tenant_id, drawing_id, 1)
    version = canonical.create_drawing_version(
        org_id,
        project_uuid,
        drawing_id=artifact.drawing_id,
        oss_object=object_key,
        intake_ref=f"sha256:{digest}",
        created_by="aps_blank_dwg_feasibility",
    )
    if version.seq != 1:
        raise RuntimeError("blank DWG project version is not version 1")
    return {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "drawing_id": drawing_id,
        "version_id": str(version.version_id),
        "version": 1,
        "object_key": object_key,
        "sha256": digest,
    }


_CAMPAIGN_HOST_FIXTURE_PROFILE = "leaf.campaign-host-validation-fixture.v1"


def _is_campaign_host_fixture(req: BrokerRunRequest) -> bool:
    return (
        isinstance(req.test_source, str) and bool(req.test_source.strip())
        and req.aps_live is False
        and req.tool.get("name") == "campaign-host-enrollment"
        and req.tool.get("capabilities") == []
        and req.params == {}
        and req.dwg == "rooftop_demo" and req.dwg_version is None
    )


def _campaign_host_fixture(second: bool = False) -> Dict[str, Any]:
    """Synthetic design-time evidence, never a host or tenant authority."""
    digit = "2" if second else "1"
    context = {
        "schema": "leaf.campaign-capability.v1",
        "tenant_id": "synthetic-broker-host-validation",
        "org_id": "00000000-0000-4000-8000-000000000001",
        "project_id": "00000000-0000-4000-8000-000000000002",
        "campaign_id": "00000000-0000-4000-8000-000000000003",
        "enrollment_id": "00000000-0000-4000-8000-000000000004",
        "link_id": "00000000-0000-4000-8000-000000000005",
        "capability": "campaign.host-enrollment",
        "tool_name": "campaign-host-enrollment",
        "change_set_id": "synthetic-change-" + digit,
        "catalog_commit": digit * 40,
        "effective_catalog_digest": digit * 64,
        "tool_manifest_sha256": "sha256:" + "a" * 64,
        "tool_source_sha256": "b" * 64,
        "profile_selector": "campaign-default-v1",
    }
    job_id = "00000000-0000-4000-8000-00000000001" + digit
    operation = {"schema": "leaf.campaign-host-operation.v1",
                 "job_id": job_id, "context": context}
    digest = hashlib.sha256(json.dumps(
        operation, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode("utf-8")).hexdigest()
    return {
        "schema": "leaf.campaign-host-validation.v1",
        "job_id": job_id,
        "operation_id": "00000000-0000-4000-8000-00000000002" + digit,
        "input_sha256": digest,
        "capability_provenance": context,
        "host_readback": {
            "config_identity_before": "c" * 64 if second else None,
            "config_identity_after": "c" * 64 if second else "d" * 64,
            "readback_sha256": "e" * 64 if second else "f" * 64,
            "reason": "already_applied" if second else "verified",
        },
    }


def _run_campaign_host_fixture(req, tool, run_dynamic, t0):
    def failed():
        return (err_envelope(
            ErrorCode.BAD_PARAMS, "host validation fixture failed",
            retryable=False, tool=tool.get("name"),
            version=tool.get("version", "1.0.0")), 400)

    first = None
    # Fixed finite cases, each with independent objects. No retries.
    for case in ("verified", "already_applied", "extra_intake", "extra_context",
                 "extra_readback", "missing_schema", "wrong_schema",
                 "bad_uuid", "bad_digest", "held"):
        intake = _campaign_host_fixture(case == "already_applied")
        expected = {
            "verified": True, "operation_id": intake["operation_id"],
            "input_sha256": intake["input_sha256"],
            "readback_sha256": intake["host_readback"]["readback_sha256"],
        }
        if case == "extra_intake":
            intake["extra"] = True
        elif case == "extra_context":
            intake["capability_provenance"]["extra"] = True
        elif case == "extra_readback":
            intake["host_readback"]["extra"] = True
        elif case == "missing_schema":
            del intake["schema"]
        elif case == "wrong_schema":
            intake["schema"] = "invalid"
        elif case == "bad_uuid":
            intake["operation_id"] = intake["operation_id"].replace("-", "")
        elif case == "bad_digest":
            intake["input_sha256"] = "INVALID"
        elif case == "held":
            intake["host_readback"]["reason"] = "lifecycle_handoff_required"
        try:
            env = run_dynamic(tool, intake, {}, aps_live=False, da=None,
                              t0=t0, tenant_id=req.tenant_id)
        except Exception:  # Infrastructure exceptions never prove rejection.
            return failed()
        if not isinstance(env, dict):
            return failed()
        if case in ("verified", "already_applied"):
            result = env.get("result")
            if (env.get("ok") is not True or "overlay" not in env
                    or env["overlay"] is not None
                    or set(env) - {"ok", "tool", "version", "result", "overlay",
                                   "timing_ms", "cost", "error", "degraded_mode",
                                   "execution_provenance"}
                    or not isinstance(result, dict) or result != expected
                    or result.get("verified") is not True):
                return failed()
            if first is None:
                first = env
        else:
            error = env.get("error")
            if (env.get("ok") is not False or not isinstance(error, dict)
                    or error.get("error_code") != ErrorCode.INTERNAL
                    or not isinstance(error.get("message"), str)
                    or not error["message"].startswith(
                        "tool 'campaign-host-enrollment' raised ")):
                return failed()
    return first, 200


def _broker_request_fingerprint(req: Union[BrokerRunRequest, BrokerPlanRunRequest]) -> str:
    if isinstance(req, BrokerPlanRunRequest):
        fingerprint_input = {
            "tenant_id": req.tenant_id, "plan": req.plan.model_dump(),
            "dwg": req.dwg, "aps_live": True, "dwg_version": req.dwg_version,
        }
    else:
        fingerprint_input = {
            "tenant_id": req.tenant_id,
            "tool": req.tool,
            "params": req.params,
            "dwg": req.dwg,
            "aps_live": bool(req.aps_live),
            "dwg_version": req.dwg_version,
        }
        if req.test_source is not None:
            fingerprint_input["test_source_sha256"] = hashlib.sha256(
                req.test_source.encode("utf-8")
            ).hexdigest()
        if _is_campaign_host_fixture(req):
            fingerprint_input["fixture_profile"] = _CAMPAIGN_HOST_FIXTURE_PROFILE
    canonical = json.dumps(
        fingerprint_input, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class BrokerExtractRequest(BaseModel):
    tenant_id: str
    dwg: str = "rooftop_demo"
    # Guest/account upload lane (CONTRACT-ADDENDUM §19): when true, `dwg`
    # resolves inside the UPLOADS staging area (data/uploads/) instead of the
    # curated library — via _resolve_upload_dwg, which applies the IDENTICAL
    # strictness. The two namespaces never cross-resolve.
    upload: bool = False
    # Required for live extraction under the PostgreSQL broker authority.
    ledger_event_key: Optional[str] = None


def _broker_extract_fingerprint(req: BrokerExtractRequest) -> str:
    canonical = json.dumps({
        "tenant_id": req.tenant_id,
        "dwg": req.dwg,
        "upload": bool(req.upload),
        "operation": "extract",
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_DWG_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


def _resolve_live_dwg(dwg: str) -> Path:
    """Resolve a registry-style drawing name inside the broker-owned data root.

    The public contract is a bare drawing identifier, never a path and never a
    filename.  Keeping this check in the credential-holding broker means a
    compromised app process cannot use ``..``, platform-specific separators,
    absolute paths, symlinks, or a double/misleading suffix to make APS upload
    an arbitrary local file.
    """
    if not isinstance(dwg, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", dwg):
        raise ValueError("dwg must be a bare drawing name (letters, digits, '_' or '-')")
    # Inline LITERAL rebind of the _DWG_NAME_RE rule (pinned equal by
    # test_codeql_barrier_literals.py): a literal fullmatch is a taint barrier
    # static analysis proves; the same pattern behind the compiled constant
    # earns no credit.
    dwg = str(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", dwg).group(0))

    root = DATA_DIR.resolve(strict=True)
    registry_dwg = drawing_identity.source_id(dwg)
    candidate = DATA_DIR / f"{registry_dwg}.dwg"
    if candidate.is_symlink():
        raise ValueError("dwg symlinks are not allowed")
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"unknown drawing: {dwg}") from exc
    if resolved.parent != root or not resolved.is_file():
        raise ValueError("dwg must resolve to a regular file in the drawing store")
    return resolved


def _uploads_root() -> Path:
    """The upload staging root the app process writes into (guest_uploads.
    uploads_dir — SAME env, SAME default, kept in lockstep by
    tests/test_broker_upload_resolver.py)."""
    return Path(os.environ.get("LEAF_UPLOADS_DIR", str(PROJECT_ROOT / "data" / "uploads")))


def _resolve_upload_dwg(name: str, tenant_id: str) -> Path:
    """Resolve an uploaded drawing id inside the uploads staging area ONLY,
    BOUND to the requesting tenant.

    Same defense posture as _resolve_live_dwg (bare names, no symlinks, must
    resolve to a regular file whose parent IS the uploads root) so a
    compromised app process cannot use the upload lane to make APS read an
    arbitrary local file. The staged filename is `<tenant>--<drawing><ext>`
    (guest_uploads.staged_path) and BOTH parts are re-validated here, so a
    caller can only reach files staged under the tenant_id it presents —
    knowing another tenant's drawing id resolves nothing (review round 1,
    MAJOR: the flat namespace had no tenant binding). Tries `.dwg` then
    `.dxf` — the upload endpoint stages exactly one of the two."""
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", name):
        raise ValueError("dwg must be a bare drawing name (letters, digits, '_' or '-')")
    if not isinstance(tenant_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", tenant_id):
        raise ValueError("tenant_id must be a bare id (letters, digits, '_' or '-')")
    # Inline LITERAL rebinds of the _DWG_NAME_RE rule (pinned equal by
    # test_codeql_barrier_literals.py): provable taint barriers for both parts
    # of the staged name — the compiled constant earns no barrier credit.
    name = str(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", name).group(0))
    tenant_id = str(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", tenant_id).group(0))
    try:
        root = _uploads_root().resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"unknown uploaded drawing: {name}") from exc
    for suffix in (".dwg", ".dxf"):
        candidate = _uploads_root() / f"{tenant_id}--{name}{suffix}"
        if candidate.is_symlink():
            raise ValueError("dwg symlinks are not allowed")
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            continue
        if resolved.parent != root or not resolved.is_file():
            raise ValueError("dwg must resolve to a regular file in the uploads area")
        return resolved
    raise ValueError(f"unknown uploaded drawing: {name}")


class LiveReadResolutionError(ValueError):
    """A tenant-safe, machine-classified uploaded-DWG resolution failure."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _classified_bad_params(
    reason_code: str,
    message: str,
    *,
    tool: Optional[str] = None,
) -> tuple[Dict[str, Any], int]:
    """Build one non-retryable BAD_PARAMS envelope with a stable public reason."""
    env = err_envelope(
        ErrorCode.BAD_PARAMS,
        message,
        retryable=False,
        tool=tool,
    )
    env["error"]["reason_code"] = reason_code
    return env, DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS]


def _resolve_live_read_dwg(req: BrokerRunRequest) -> tuple[Path, bool]:
    """Resolve one live read to a curated or tenant-store DWG.

    An omitted version preserves the curated registry contract. A present
    version explicitly selects the tenant-owned immutable store, so failure
    there never falls back to a same-named curated drawing. All store and
    upload checks complete before the temporary DWG can reach APS.
    """
    if req.dwg_version is None:
        return _resolve_live_dwg(req.dwg), False

    import guest_uploads
    import store

    backend = write_loop.upload_backend_for_tenant(req.tenant_id)
    marker = guest_uploads.read_marker(backend, req.tenant_id, req.dwg)
    if not isinstance(marker, dict):
        raise LiveReadResolutionError(
            "uploaded_marker_unreadable",
            f"dwg_version selects uploaded drawing {req.dwg!r}, which has no "
            f"readable upload marker")
    if marker.get("status") != "ready":
        raise LiveReadResolutionError(
            "uploaded_marker_not_ready",
            f"uploaded drawing {req.dwg!r} is not ready for a live run")
    source_ext = str(marker.get("source_ext") or "").lower()
    if not source_ext:
        source_ext = Path(str(marker.get("filename") or "")).suffix.lower()
    if source_ext != ".dwg":
        raise LiveReadResolutionError(
            "uploaded_source_not_dwg",
            f"live runs need a DWG source; drawing {req.dwg!r} was "
            f"uploaded as {source_ext or 'an unknown format'}")

    unavailable_message = (
        f"dwg_version not in tenant store: "
        f"{req.tenant_id}/{req.dwg}@{req.dwg_version}"
    )
    try:
        store.load_manifest(backend, req.tenant_id, req.dwg)
    except (KeyError, ValueError) as exc:
        raise LiveReadResolutionError(
            "uploaded_manifest_unavailable",
            unavailable_message if isinstance(exc, KeyError) else str(exc),
        ) from exc
    try:
        version, version_key = store.resolve_version(
            backend, req.tenant_id, req.dwg, req.dwg_version)
    except (KeyError, ValueError) as exc:
        raise LiveReadResolutionError(
            "uploaded_version_unavailable",
            unavailable_message if isinstance(exc, KeyError) else str(exc),
        ) from exc
    try:
        write_loop.read_intake(backend, req.tenant_id, req.dwg, version)
    except (KeyError, ValueError) as exc:
        raise LiveReadResolutionError(
            "uploaded_intake_unavailable",
            unavailable_message if isinstance(exc, KeyError) else str(exc),
        ) from exc
    try:
        stored_source = backend.get(version_key)
    except KeyError as exc:
        raise LiveReadResolutionError(
            "uploaded_source_unavailable", unavailable_message) from exc
    try:
        source, bridged = write_loop._live_execution_source_bytes(stored_source)
    except ValueError as exc:
        raise LiveReadResolutionError(
            "uploaded_source_not_immutable", str(exc)) from exc
    if bridged:
        raise LiveReadResolutionError(
            "uploaded_source_not_immutable",
            "uploaded drawing source is not an immutable DWG",
        )
    if not source:
        raise LiveReadResolutionError(
            "uploaded_source_empty", "tenant drawing DWG source is empty")

    fd, tmp_name = tempfile.mkstemp(suffix=".dwg")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(source)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return Path(tmp_name), True


def _live_script_is_nonempty(tool: Dict[str, Any], da: Any) -> bool:
    """True iff da/client.py's REAL `tool_activity_spec(tool)` — the ACTUAL function
    that provisions this tool's live Activity — produces a non-empty (non-whitespace)
    accoreconsole script for it.

    Ongoing purpose: this guard calls the REAL function (never a re-implemented
    heuristic of what "looks like" a live script), so it tracks any future drift
    in that resolution logic and FAILS CLOSED on any tool whose emitted script is
    empty or unreadable (a dangling or mistyped `script` path, an unreadable
    file, a spec-build failure). Such a tool gets an explicit, non-retryable
    BAD_PARAMS instead of silently submitting an EMPTY-script WorkItem to APS.

    History (review 2026-07-22, round 2, HIGH): a PRIOR version of this guard
    only checked for the PRESENCE of an `engine_script`/`.lsp` `script`
    reference, which was not sufficient. At the time, `engine/registry.json`'s
    shipped tool entries declared their `.lsp` path as `tools/<name>.lsp` while
    the files lived at `engine/tools/<name>.lsp`, so `tool_activity_spec`'s
    project-root-relative read failed (caught, silently fell back to an EMPTY
    script) for EVERY then-shipped engine-registry tool. That path mismatch was
    fixed in PR #15: `engine/registry.json` + `da/registry_live.json` now declare
    root-relative `engine/tools/<name>.lsp` paths, so the shipped tools resolve
    to non-empty scripts and pass this guard.
    """
    if da is None or not hasattr(da, "tool_activity_spec"):
        return False  # can't verify the REAL resolution -> fail closed, never guess
    try:
        spec = da.tool_activity_spec(tool)
    except Exception:
        return False  # a spec-build failure IS "no usable live script"
    script = ((spec or {}).get("settings") or {}).get("script") or {}
    value = script.get("value") if isinstance(script, dict) else None
    return bool(value) and bool(str(value).strip())


@app.get("/broker/health")
def health() -> Dict[str, Any]:
    return with_envelope_fields({
        "ok": True,
        "role": "aps-broker",
        "aps_endpoint": APS_ENDPOINT,
        "ledger": str(LEDGER_PATH),
        # Through the authoritative reader, not a second truthiness/`.get` scan:
        # v.get("disabled") reported a null flag as ENABLED and crashed on a
        # non-dict record, disagreeing with the very kill switch it reports.
        "tenants_disabled": (
            _postgres_store().disabled_tenant_ids()
            if _broker_store_mode() == "postgres"
            else sorted(t for t in _tenants if tenant_disabled(t))
        ),
    })


@app.post("/broker/tenants/{tid}/disable", dependencies=[Depends(require_broker_auth)])
def disable_tenant(tid: str) -> Dict[str, Any]:
    set_tenant_disabled(tid, True)
    return with_envelope_fields({"ok": True, "tenant_id": tid, "disabled": True})


@app.post("/broker/tenants/{tid}/enable", dependencies=[Depends(require_broker_auth)])
def enable_tenant(tid: str) -> Dict[str, Any]:
    set_tenant_disabled(tid, False)
    return with_envelope_fields({"ok": True, "tenant_id": tid, "disabled": False})


class BrokerAdmissionResolution(BaseModel):
    tenant_id: str
    resolution: str
    operator_id: str
    reason: str
    evidence_ref: str
    confirmation: str
    result: Optional[Dict[str, Any]] = None
    http_status: Optional[int] = None
    ledger_entry: Optional[Dict[str, Any]] = None


_FROZEN_LEDGER_KEYS = {
    "ts", "tenant_id", "tool", "engine_op", "aps_endpoint", "aps_live",
    "engine_seconds", "usd_est", "status",
}


def _validated_reconciliation_ledger(
    raw: Dict[str, Any], *, tenant_id: str, aps_live: bool,
) -> Dict[str, Any]:
    if set(raw) != _FROZEN_LEDGER_KEYS:
        raise HTTPException(
            status_code=400,
            detail="verified terminal ledger_entry must contain the frozen nine keys",
        )
    if raw.get("tenant_id") != tenant_id or raw.get("aps_live") is not aps_live:
        raise HTTPException(
            status_code=400,
            detail="verified terminal ledger tenant/live identity does not match admission",
        )
    if (
        not isinstance(raw.get("ts"), (int, float))
        or isinstance(raw.get("ts"), bool)
        or not math.isfinite(float(raw["ts"]))
    ):
        raise HTTPException(status_code=400, detail="ledger ts must be numeric")
    if not isinstance(raw.get("aps_endpoint"), str) or not raw["aps_endpoint"]:
        raise HTTPException(status_code=400, detail="ledger aps_endpoint must be a string")
    for field in ("engine_seconds", "usd_est"):
        value = raw.get(field)
        if value is not None and (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or float(value) < 0
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"verified terminal {field} must be finite and nonnegative"
                ),
            )
    entry = _conform_ledger_entry(dict(raw))
    if entry["status"] == "error" and raw.get("status") != "error":
        raise HTTPException(status_code=400, detail="ledger status must be a string")
    return entry


_RECONCILIATION_COST_TOLERANCE = 1e-9


def _crosscheck_reconciliation_cost(
    result: Dict[str, Any], entry: Dict[str, Any],
) -> None:
    """Require result cost evidence to agree with the immutable ledger."""
    if "cost" not in result:
        raise HTTPException(
            status_code=400,
            detail="verified terminal result requires a cost field",
        )
    cost = result["cost"]
    if cost is None:
        if any(entry[field] is not None for field in ("engine_seconds", "usd_est")):
            raise HTTPException(
                status_code=400,
                detail="ledger cost must be null when verified result cost is null",
            )
        return
    if not isinstance(cost, dict):
        raise HTTPException(
            status_code=400,
            detail="verified terminal result cost must be an object or null",
        )
    for field in ("engine_seconds", "usd_est"):
        value = cost.get(field)
        if (
            field not in cost
            or not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or float(value) < 0
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"verified terminal result cost {field} must be finite "
                    "and nonnegative"
                ),
            )
        ledger_value = entry[field]
        if ledger_value is None or not math.isclose(
            float(value),
            float(ledger_value),
            rel_tol=_RECONCILIATION_COST_TOLERANCE,
            abs_tol=_RECONCILIATION_COST_TOLERANCE,
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"verified terminal result cost {field} does not match "
                    "the ledger"
                ),
            )


def _require_postgres_reconciliation_store():
    if _broker_store_mode() != "postgres":
        raise HTTPException(
            status_code=409,
            detail="broker admission reconciliation requires PostgreSQL authority",
        )
    return _postgres_store()


@app.get(
    "/broker/admin/admissions/executing",
    dependencies=[Depends(require_broker_reconcile_auth)],
)
def list_executing_admissions(limit: int = 100) -> Dict[str, Any]:
    rows = _require_postgres_reconciliation_store().list_executing(limit)
    return with_envelope_fields({"ok": True, "admissions": rows})


@app.get(
    "/broker/admin/admissions/{event_key}",
    dependencies=[Depends(require_broker_reconcile_auth)],
)
def broker_admission_status(event_key: str, tenant_id: str) -> Dict[str, Any]:
    status = _require_postgres_reconciliation_store().admission_status(
        event_key, tenant_id)
    if status is None:
        raise HTTPException(status_code=404, detail="broker admission not found")
    return with_envelope_fields({"ok": True, "admission": status})


@app.post(
    "/broker/admin/admissions/{event_key}/resolve",
    dependencies=[Depends(require_broker_reconcile_auth)],
)
def resolve_executing_admission(
    event_key: str, request: BrokerAdmissionResolution,
) -> Dict[str, Any]:
    allowed = {"confirmed_failed_no_charge", "verified_terminal"}
    if request.resolution not in allowed:
        raise HTTPException(status_code=400, detail="unsupported admission resolution")
    if len(request.operator_id.strip()) < 3:
        raise HTTPException(status_code=400, detail="operator_id is required")
    if len(request.reason.strip()) < 16:
        raise HTTPException(status_code=400, detail="resolution reason is too short")
    if len(request.evidence_ref.strip()) < 8:
        raise HTTPException(status_code=400, detail="APS evidence_ref is required")
    expected = (
        f"RESOLVE {request.tenant_id} {event_key} {request.resolution}"
    )
    if not hmac.compare_digest(
            request.confirmation.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(
            status_code=400,
            detail="operator confirmation phrase does not match this resolution",
        )
    store = _require_postgres_reconciliation_store()
    status = store.admission_status(event_key, request.tenant_id)
    if status is None or status.get("state") != "executing":
        raise HTTPException(
            status_code=409,
            detail="only an executing admission can be reconciled",
        )
    if request.resolution == "confirmed_failed_no_charge":
        result = err_envelope(
            ErrorCode.WORKITEM_FAILED,
            "operator verified that APS accepted no paid work for this admission",
            retryable=False,
        )
        http_status = DEFAULT_HTTP_STATUS[ErrorCode.WORKITEM_FAILED]
        entry = {
            "ts": time.time(),
            "tenant_id": request.tenant_id,
            "tool": None,
            "engine_op": "",
            "aps_endpoint": APS_ENDPOINT,
            "aps_live": bool(status["aps_live"]),
            "engine_seconds": None,
            "usd_est": None,
            "status": "RECONCILED_FAILED_NO_CHARGE",
        }
    else:
        if request.result is None or request.ledger_entry is None:
            raise HTTPException(
                status_code=400,
                detail="verified_terminal requires result and ledger_entry",
            )
        if not isinstance(request.result.get("ok"), bool):
            raise HTTPException(
                status_code=400, detail="verified terminal result requires boolean ok")
        if not isinstance(request.result.get("degraded_mode"), bool):
            raise HTTPException(
                status_code=400,
                detail="verified terminal result requires boolean degraded_mode",
            )
        if request.http_status is None or not 100 <= request.http_status <= 599:
            raise HTTPException(
                status_code=400, detail="verified terminal http_status is invalid")
        result = request.result
        http_status = request.http_status
        entry = _validated_reconciliation_ledger(
            request.ledger_entry,
            tenant_id=request.tenant_id,
            aps_live=bool(status["aps_live"]),
        )
        _crosscheck_reconciliation_cost(result, entry)
        if result["ok"]:
            if not 200 <= http_status < 300 or entry["status"] != "ok":
                raise HTTPException(
                    status_code=400,
                    detail="successful verified result requires 2xx HTTP and ledger status ok",
                )
        else:
            error = result.get("error")
            error_code = error.get("error_code") if isinstance(error, dict) else None
            if (
                not isinstance(error_code, str)
                or not 400 <= http_status <= 599
                or entry["status"] != error_code
                or error_code in {ErrorCode.QUOTA_EXCEEDED, ErrorCode.TENANT_DISABLED}
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "failed verified result requires matching non-quota error "
                        "status and 4xx/5xx HTTP"
                    ),
                )
    outcome = store.reconcile_executing(
        event_key,
        request.tenant_id,
        resolution=request.resolution,
        operator_id=request.operator_id.strip(),
        reason=request.reason.strip(),
        evidence_ref=request.evidence_ref.strip(),
        entry=_conform_ledger_entry(entry),
        result=result,
        http_status=http_status,
    )
    return with_envelope_fields({"ok": True, "resolution": outcome})


class BrokerReapRequest(BaseModel):
    # each record: {status, workitem_id, session_closed?|lease_expires?}
    records: List[Dict[str, Any]] = []
    live: Optional[bool] = None  # None -> reaper decides (APS_LIVE + BROKER_REAP_LIVE)


def _resolve_reap_workitems(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fill in each record's missing `workitem_id` from the live-run registry.

    The app/jobs side supplies the orphan SIGNAL (job_id + session_closed) but
    never learns the WorkItem id -- only this process does. A record that arrives
    with no workitem_id is therefore not un-reapable, it is un-CORRELATED: look
    the id up by job_id. Records that already carry a workitem_id, or that name
    no known job, pass through untouched (sweep leaves an id-less orphan's cancel
    as a no-op, exactly as before).

    This PEEKS. Deciding what is an orphan is sweep()'s job, and a caller may
    include healthy rows in the same batch -- consuming their entry here would
    leave a still-running job with no id to cancel when its tab really does
    close. Eviction happens afterwards, for the rows sweep actually reaped.
    """
    out: List[Dict[str, Any]] = []
    for rec in records:
        rec = dict(rec)
        if not rec.get("workitem_id"):
            resolved = active_workitem_for(rec.get("job_id"))
            if resolved:
                rec["workitem_id"] = resolved
        out.append(rec)
    return out


@app.post("/broker/reap", dependencies=[Depends(require_broker_auth)])
def broker_reap(req: BrokerReapRequest) -> JSONResponse:
    """Cancel orphaned WorkItems (closed tab / expired lease). Only the
    credential-holding broker can issue the DA cancel; the app/jobs side supplies
    the orphan records. Live DELETE is gated (APS_LIVE + BROKER_REAP_LIVE, or an
    explicit `live:true`); otherwise a stub client records intent without touching
    APS. Returns the reaped WorkItem ids."""
    reaper = _get_reaper()
    if reaper is None:
        return JSONResponse(status_code=500,
                            content=err_envelope(ErrorCode.INTERNAL,
                                                 "reaper module unavailable", retryable=False))
    use_live = reaper.reap_live_enabled() if req.live is None else bool(req.live)
    cc = reaper.cancel_client_for(live=use_live, da_client=_get_da() if use_live else None)
    records = _resolve_reap_workitems(req.records)
    reaped = reaper.sweep(records, cancel_client=cc)
    live_cancel = not isinstance(cc, reaper.StubCancelClient)
    cancelled_jobs: List[str] = []
    for rec in reaped:
        # Drop the correlation ONLY when a real cancel really succeeded. sweep()
        # marks a row reaped regardless of outcome, so evicting on that alone
        # would throw the id away after a FAILED DELETE, or in a deployment
        # where live reaping is off and the inert stub cancelled nothing at all
        # -- and the id is the only thing that can stop the billing. Keeping it
        # lets a later sweep retry; dropping it on success is what keeps a
        # repeated beacon from cancelling the same WorkItem twice.
        outcome = rec.get("reap_outcome")
        cancelled = isinstance(outcome, dict) and bool(outcome.get("cancelled"))
        if live_cancel and cancelled:
            job_id = rec.get("job_id")
            # Evict and decide "is this job settled?" under ONE lock: a
            # replacement run registering between the two would otherwise be
            # acknowledged away while its WorkItem is still billing.
            if job_id and _settle_cancelled_workitem(job_id, rec.get("workitem_id")):
                cancelled_jobs.append(str(job_id))
    return JSONResponse(status_code=200, content=with_envelope_fields({
        "ok": True,
        "reaped": [r.get("workitem_id") for r in reaped],
        "count": len(reaped),
        "live": live_cancel,
        # Which jobs a REAL cancel really succeeded for. "reaped" says a row was
        # swept, which is not the same thing: a refused DELETE, an id that could
        # not be resolved, or an inert stub all sweep a row while cancelling
        # nothing. The caller needs the difference to know whether to retry, and
        # it never learns the WorkItem id, so this is keyed by job.
        "cancelled_jobs": cancelled_jobs,
    }))


def _complete_callback_job(job_id: str, callback: Dict[str, Any]) -> str:
    """Use the durable job spine's single terminal transition for APS callbacks."""
    import jobs

    record = jobs.get_job(job_id)
    if record is None:
        return "missing"
    job_attempt = int(record.get("attempt") or 0)
    # THE CALLBACK'S SIGNED ATTEMPT DECIDES WHETHER THIS COMPLETION STILL APPLIES.
    #
    # This function used to read the attempt off the JOB RECORD and stamp it onto
    # the provenance, discarding the attempt the adapter had signed. That turned a
    # late delivery into a cross-attempt completion: attempt 1's envelope is
    # delayed, attempt 1's lease expires, the store reclaims and starts attempt 2,
    # then attempt 1's envelope lands and is stamped `attempt: 2` and used to
    # complete attempt 2. The receipt names an attempt whose output it does not
    # describe, and the signature over `attempt: 1` no longer matches the claim the
    # spine recorded. The adapter binds the attempt to authority precisely so this
    # side can rely on it, and then this side threw it away.
    #
    # A callback that names a different attempt than the one currently running is
    # STALE, not applicable. Refuse it rather than retargeting it.
    # THE ATTEMPT IS REQUIRED, not optional. Round 12 caught the fail-open version
    # of this guard: enforcing the binding only when the callback SUPPLIED an
    # attempt left the entire hole reachable by simply omitting the field. A
    # delayed attempt-1 success with no `attempt` was still stamped with the job's
    # current attempt and still completed attempt 2. "Validate it if present" is
    # not a binding, it is a suggestion, and this module already learned the same
    # lesson about an optional reservation guard.
    #
    # Nothing legitimate omits it: the adapter is the only emitter on this route
    # and it always signs an attempt. An emitter that does not is not one whose
    # completions we can place.
    claimed_attempt = callback.get("attempt")
    if type(claimed_attempt) is not int:
        raise ValueError("callback must carry an int attempt")
    if claimed_attempt != job_attempt:
        raise ValueError(
            f"callback names attempt {claimed_attempt} but the job is on attempt "
            f"{job_attempt}; a stale attempt's callback cannot complete a newer one")
    # The WorkItem id is signed too, so carry it into the receipt instead of
    # dropping it: provenance that cannot name the WorkItem it came from cannot be
    # reconciled against APS afterwards. There is deliberately NO cross-check
    # against the job record here, because there is nothing to check it against —
    # the jobs table has no `workitem_id` column. The binding that matters already
    # happened in the adapter, which refuses `wrong_workitem` by comparing the
    # completion against the dispatched id handed to it as authority.
    claimed_workitem = callback.get("workitem_id")
    if claimed_workitem is not None and type(claimed_workitem) is not str:
        raise ValueError("callback workitem_id must be a string")

    def _provenance(**extra: Any) -> Dict[str, Any]:
        # `attempt` is the one the callback SIGNED, already proven equal to the
        # job's current attempt above. It is never substituted from the record.
        base: Dict[str, Any] = {"attempt": claimed_attempt, "execution_path": "cloud"}
        if claimed_workitem:
            base["workitem_id"] = claimed_workitem
        base.update(extra)
        return base

    # THE RACE THIS ONCE LEFT HALF OPEN IS NOW CLOSED, BUT NOT WHERE THIS COMMENT
    # USED TO SAY. The check above reads the attempt here, and a lease reclaim can
    # advance the job before the terminal write lands. Two mechanisms bear on that
    # window and only the SECOND one closes it, so they are worth naming apart:
    #
    #   1. `jobs.complete_callback` re-reads the DURABLE attempt and runs
    #      `_validate_terminal_context` against it. This happens BEFORE the store
    #      opens its transaction, not inside it, so on its own it only narrows the
    #      window. It is what binds a FAILURE that names an attempt, which is the
    #      half this block used to call unfixed.
    #   2. The transaction itself is attempt-qualified: `job_pg_store.complete`
    #      updates `WHERE ... AND attempt = %(attempt)s`, so an attempt that moved
    #      after step 1 matches no row and the terminal write refuses. THIS is what
    #      actually closes the race, and the earlier text credited step 1 with it.
    #
    # Everything this route emits names an attempt, because `_provenance` below
    # always sets it. A failure carrying no provenance at all is still accepted on
    # purpose, since the orphan reaper raises those and has no attempt to name.
    #
    # This block also used to defer the failure-half fix to whoever owned
    # `server/jobs.py`, naming PRs #129, #130 and #141 as the reason not to touch
    # it. All three merged and the fix landed; a stale deferral sends the next
    # reader on a dead errand.

    raw_status = str(callback.get("status", "")).strip().lower()
    if raw_status in {"success", "complete"}:
        result = callback.get("result")
        result_env = dict(result) if isinstance(result, dict) else {"result": result}
        result_env["ok"] = True
        provenance = _provenance()
        result_env.setdefault("execution_provenance", provenance)
        return jobs.complete_callback(job_id, "complete", result_env=result_env,
                                      provenance=provenance)
    if raw_status in {"failed", "failure", "cancelled", "canceled"}:
        message = str(callback.get("message") or f"Design Automation callback status: {raw_status}")
        # The failure branch carried NO attempt at all, so a stale attempt's failure
        # callback could fail a newer attempt with no record of which attempt failed.
        # It runs through the same guard and the same provenance builder.
        return jobs.complete_callback(
            job_id,
            "failed",
            error=error_obj(ErrorCode.WORKITEM_FAILED, message, retryable=False),
            provenance=_provenance(callback_status=raw_status),
        )
    raise ValueError("callback status must be a terminal Design Automation status")


@app.post("/da/callback")
async def da_callback(request: Request,
                      x_leaf_signature: Optional[str] = Header(default=None),
                      x_leaf_timestamp: Optional[str] = Header(default=None),
                      x_leaf_nonce: Optional[str] = Header(default=None)) -> JSONResponse:
    """Accept exactly one signed Design Automation terminal callback.

    This route intentionally has no broker shared-secret dependency. It is an
    inbound APS seam, authenticated by the HMAC over its raw body instead.
    """
    callbacks = _get_callbacks()
    if callbacks is None:
        return JSONResponse(status_code=500, content=err_envelope(
            ErrorCode.INTERNAL, "callback module unavailable", retryable=False))
    outcome = callbacks.consume_callback(await request.body(), x_leaf_signature,
                                         x_leaf_timestamp, x_leaf_nonce)
    if not outcome.get("ok"):
        reason = str(outcome.get("reason", "bad_callback"))
        status_code = 503 if reason == "not_configured" else (409 if reason == "replay" else 401)
        return JSONResponse(status_code=status_code, content=err_envelope(
            ErrorCode.BAD_PARAMS, f"callback rejected: {reason}", retryable=False))
    try:
        completion = _complete_callback_job(outcome["job_id"], outcome["callback"])
    except ValueError as exc:
        return JSONResponse(status_code=409, content=err_envelope(
            ErrorCode.BAD_PARAMS, f"callback completion rejected: {exc}", retryable=False))
    if completion == "missing":
        return JSONResponse(status_code=404, content=err_envelope(
            ErrorCode.BAD_PARAMS, "callback completion rejected: unknown job_id", retryable=False))
    if completion not in {"applied", "duplicate"}:
        return JSONResponse(status_code=409, content=err_envelope(
            ErrorCode.BAD_PARAMS, f"callback completion rejected: {completion}", retryable=False))
    return JSONResponse(status_code=200, content=with_envelope_fields({
        "ok": True,
        "job_id": outcome["job_id"],
        "completion_mode": "callback",
        "completion": completion,
    }))


@app.post("/broker/extract", dependencies=[Depends(require_broker_auth)])
def broker_extract(req: BrokerExtractRequest) -> JSONResponse:
    # ONLY the upload lane commits drawing state here. A library/session
    # extraction (`upload=False`, what routers/session.py sends) is a READ: it
    # must stay available during a cutover drain, and must not hold the shared
    # fence across an APS call that can run for minutes -- doing so would also
    # delay the exclusive lock the cutover control needs to flip the fence.
    if not req.upload:
        return _broker_extract(req)
    with write_loop.upload_mutation_commit_guard() as commit_enabled:
        if not commit_enabled:
            return JSONResponse(
                status_code=503,
                content=err_envelope(
                    ErrorCode.APS_UNAVAILABLE,
                    "drawing mutations are temporarily disabled for a storage cutover",
                    retryable=True,
                ),
            )
        return _broker_extract(req)


def _broker_extract(req: BrokerExtractRequest) -> JSONResponse:
    """Extract intake through the credential-holding process only."""
    if tenant_disabled(req.tenant_id):
        return JSONResponse(
            status_code=DEFAULT_HTTP_STATUS[ErrorCode.TENANT_DISABLED],
            content=err_envelope(
                ErrorCode.TENANT_DISABLED,
                f"tenant {req.tenant_id!r} is disabled by the kill-switch",
                retryable=False,
            ),
        )
    # Upload-lane only: a read extraction is not a drawing commit.
    if req.upload and not write_loop.fence_open():
        return JSONResponse(
            status_code=503,
            content=err_envelope(
                ErrorCode.APS_UNAVAILABLE,
                "drawing mutations are temporarily disabled for a storage cutover",
                retryable=True,
            ),
        )
    try:
        local = (_resolve_upload_dwg(req.dwg, req.tenant_id) if req.upload
                 else _resolve_live_dwg(req.dwg))
    except ValueError as exc:
        return JSONResponse(
            status_code=DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS],
            content=err_envelope(ErrorCode.BAD_PARAMS, str(exc), retryable=False),
        )

    da = _get_da()
    if da is None or not hasattr(da, "extract"):
        return JSONResponse(
            status_code=DEFAULT_HTTP_STATUS[ErrorCode.APS_UNAVAILABLE],
            content=err_envelope(
                ErrorCode.APS_UNAVAILABLE,
                "APS extraction client is unavailable in the broker",
                retryable=False,
            ),
        )
    if _broker_store_mode() != "postgres":
        try:
            intake = da.extract(str(local))
            if req.upload and not write_loop.fence_open():
                return JSONResponse(
                    status_code=503,
                    content=err_envelope(
                        ErrorCode.APS_UNAVAILABLE,
                        "drawing mutations were drained before extraction commit",
                        retryable=True,
                    ),
                )
            return JSONResponse(
                status_code=200,
                content=with_envelope_fields({"intake": intake}),
            )
        except EgressBlocked as exc:
            return JSONResponse(
                status_code=500,
                content=err_envelope(ErrorCode.INTERNAL, str(exc), retryable=False),
            )
        except FileNotFoundError as exc:
            # The message of a FileNotFoundError names broker-local filesystem
            # paths (e.g. the APS credential file); log it, never return it.
            print(f"[leaf-broker] extraction unavailable: {exc}", file=sys.stderr)
            return JSONResponse(
                status_code=DEFAULT_HTTP_STATUS[ErrorCode.APS_UNAVAILABLE],
                content=err_envelope(
                    ErrorCode.APS_UNAVAILABLE,
                    "APS extraction prerequisites are missing in the broker",
                    retryable=False),
            )
        except Exception as exc:  # noqa: BLE001
            # Class name only: an arbitrary exception's message can carry
            # internal paths or state. Full detail goes to the broker log.
            print(f"[leaf-broker] extraction failed: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return JSONResponse(
                status_code=DEFAULT_HTTP_STATUS[ErrorCode.WORKITEM_FAILED],
                content=err_envelope(
                    ErrorCode.WORKITEM_FAILED,
                    f"extraction failed: {type(exc).__name__}",
                    retryable=True,
                ),
            )

    if not req.ledger_event_key:
        return JSONResponse(
            status_code=DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS],
            content=err_envelope(
                ErrorCode.BAD_PARAMS,
                "ledger_event_key is required for PostgreSQL broker extraction",
                retryable=False,
            ),
        )

    usage = _get_usage()
    tier = _tenant_tier(req.tenant_id)
    estimate = float(getattr(usage, "DEFAULT_EST_USD", 0.0)) if usage else 0.0
    spend_cap = usage.cap_for(req.tenant_id) if usage else None
    daily_limit = usage.daily_run_limit_for(tier) if usage is not None else None
    admission = _postgres_store().admit_run(
        req.ledger_event_key,
        req.tenant_id,
        aps_live=True,
        estimated_usd=estimate,
        spend_cap=spend_cap,
        daily_limit=daily_limit,
        request_fingerprint=_broker_extract_fingerprint(req),
    )
    decision = admission["status"]
    if decision == "replay":
        return JSONResponse(
            status_code=admission["http_status"], content=admission["result"])
    if decision in {"collision", "mismatch"}:
        return JSONResponse(
            status_code=DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS],
            content=err_envelope(
                ErrorCode.BAD_PARAMS,
                "broker extraction event key is not valid for this request",
                retryable=False,
            ),
        )
    if decision in {"leased", "executing"}:
        retryable = decision == "leased"
        detail = (
            "broker extraction event is already leased"
            if retryable
            else (
                "broker extraction started; automatic retry is unsafe and "
                "operator reconciliation may be required"
            )
        )
        return JSONResponse(
            status_code=DEFAULT_HTTP_STATUS[ErrorCode.TURN_IN_PROGRESS],
            content=err_envelope(
                ErrorCode.TURN_IN_PROGRESS, detail, retryable=retryable),
        )

    entry: Dict[str, Any] = {
        "ts": time.time(),
        "tenant_id": req.tenant_id,
        "tool": "__extract__",
        "engine_op": "extract",
        "aps_endpoint": APS_ENDPOINT,
        "aps_live": True,
        "engine_seconds": None,
        "usd_est": None,
        "status": "unknown",
    }
    terminal_env: Optional[Dict[str, Any]] = None
    terminal_status: Optional[int] = None
    capacity_wait = False
    try:
        if decision == "spend_quota":
            assert usage is not None
            terminal_env = usage.quota_envelope(
                req.tenant_id,
                admission["spent"] + admission["reserved"],
                admission["estimated_usd"],
                admission["cap"],
                tool="__extract__",
            )
            terminal_env["degraded_mode"] = False
            terminal_status = 402
            entry["status"] = ErrorCode.QUOTA_EXCEEDED
        elif decision == "daily_quota":
            assert usage is not None
            terminal_env = usage.daily_quota_envelope(
                req.tenant_id,
                tier,
                admission["limit"],
                admission["used"],
                tool="__extract__",
            )
            terminal_env["degraded_mode"] = False
            terminal_status = 429
            entry["status"] = ErrorCode.QUOTA_EXCEEDED
        elif decision == "acquired":
            started = _postgres_store().mark_execution_started(
                req.ledger_event_key,
                req.tenant_id,
                admission["lease_token"],
                aps_live=True,
                max_concurrency=_aps_max_concurrency(),
                slot_lease_seconds=_aps_slot_lease_seconds(),
            )
            if not started:
                capacity_wait = True
                terminal_env = err_envelope(
                    ErrorCode.APS_UNAVAILABLE,
                    "APS fleet concurrency limit is currently full",
                    retryable=True,
                )
                terminal_status = DEFAULT_HTTP_STATUS[ErrorCode.APS_UNAVAILABLE]
            else:
                intake = da.extract(str(local))
                entry["usd_est"] = estimate
                if req.upload and not write_loop.fence_open():
                    terminal_env = err_envelope(
                        ErrorCode.APS_UNAVAILABLE,
                        "drawing mutations were drained before extraction commit",
                        retryable=True,
                    )
                    terminal_status = 503
                    entry["status"] = ErrorCode.APS_UNAVAILABLE
                else:
                    terminal_env = with_envelope_fields({"intake": intake})
                    terminal_status = 200
                    entry["status"] = "ok"
        else:
            raise BrokerStateError(
                f"unknown broker extraction admission result {decision!r}")
    except EgressBlocked as exc:
        terminal_env = err_envelope(
            ErrorCode.INTERNAL, str(exc), retryable=False)
        terminal_status = 500
        entry["status"] = ErrorCode.INTERNAL
    except FileNotFoundError as exc:
        terminal_env = err_envelope(
            ErrorCode.APS_UNAVAILABLE, str(exc), retryable=False)
        terminal_status = DEFAULT_HTTP_STATUS[ErrorCode.APS_UNAVAILABLE]
        entry["status"] = ErrorCode.APS_UNAVAILABLE
    except Exception as exc:  # noqa: BLE001
        terminal_env = err_envelope(
            ErrorCode.WORKITEM_FAILED,
            f"{type(exc).__name__}: {exc}",
            retryable=True,
        )
        terminal_status = DEFAULT_HTTP_STATUS[ErrorCode.WORKITEM_FAILED]
        entry["status"] = ErrorCode.WORKITEM_FAILED
    finally:
        if not capacity_wait:
            if terminal_env is None or terminal_status is None:
                raise BrokerStateError(
                    "admitted broker extraction has no terminal result")
            _validate_terminal_ledger_numbers(entry)
            _postgres_store().complete_run(
                req.ledger_event_key,
                req.tenant_id,
                admission["lease_token"],
                _conform_ledger_entry(entry),
                terminal_env,
                terminal_status,
            )
            _emit_aps_metric(entry, req.ledger_event_key)
    assert terminal_env is not None and terminal_status is not None
    return JSONResponse(status_code=terminal_status, content=terminal_env)


@app.post(
    "/broker/blank-dwg/feasibility",
    dependencies=[Depends(require_broker_reconcile_auth)],
)
def blank_dwg_feasibility(req: BlankDwgFeasibilityRequest) -> JSONResponse:
    """Protected one-shot source for the dormant APS feasibility workflow."""
    try:
        tenant_id = str(uuid.UUID(req.tenant_id))
        project_id = str(uuid.UUID(req.project_id))
    except (TypeError, ValueError):
        env, status = _classified_bad_params(
            "blank_dwg_scope_invalid",
            "tenant_id and project_id must be canonical UUIDs",
            tool="aps-blank-dwg-feasibility",
        )
        return JSONResponse(status_code=status, content=env)
    if not re.fullmatch(r"[0-9a-f]{40}", req.source_sha or ""):
        env, status = _classified_bad_params(
            "blank_dwg_source_invalid",
            "source_sha must be 40 lowercase hexadecimal characters",
            tool="aps-blank-dwg-feasibility",
        )
        return JSONResponse(status_code=status, content=env)
    if not req.drawing_name.strip() or len(req.drawing_name) > 200:
        env, status = _classified_bad_params(
            "blank_dwg_name_invalid",
            "drawing_name must contain 1 to 200 characters",
            tool="aps-blank-dwg-feasibility",
        )
        return JSONResponse(status_code=status, content=env)
    run = _BlankDwgBrokerRunRequest(
        tenant_id=tenant_id,
        tool=_blank_dwg_tool(),
        params={
            "project_id": project_id,
            "source_sha": req.source_sha,
            "drawing_name": req.drawing_name.strip(),
        },
        dwg="blank",
        aps_live=True,
        ledger_event_key=req.ledger_event_key,
        job_id=req.job_id,
    )
    return broker_run(run)


@app.post("/broker/run", dependencies=[Depends(require_broker_auth)])
def broker_run(req: BrokerRunRequest) -> JSONResponse:
    if (req.tool or {}).get("name") == PLAN_TOOL_NAME:
        return _broker_run(req)
    if not write_loop.is_write_tool(req.tool or {}):
        return _broker_run(req)
    with write_loop.drawing_mutation_commit_guard() as commit_enabled:
        if not commit_enabled:
            return JSONResponse(
                status_code=503,
                content=err_envelope(
                    ErrorCode.APS_UNAVAILABLE,
                    "drawing mutations are temporarily disabled for a storage cutover",
                    retryable=True,
                    tool=(req.tool or {}).get("name"),
                ),
            )
        return _broker_run(req)


@app.post("/broker/run-plan", dependencies=[Depends(require_broker_auth)])
def broker_run_plan(req: BrokerPlanRunRequest) -> JSONResponse:
    """Apply a server-owned data plan under the live drawing mutation guard.

    A positive readiness result cached up to 60 s followed by an alias move
    leaves a window in which the WorkItem runs against the moved alias. The
    receipt records the Activity version observed by the readiness check.
    """
    with write_loop.drawing_mutation_commit_guard() as commit_enabled:
        if not commit_enabled:
            return JSONResponse(
                status_code=503,
                content=err_envelope(
                    ErrorCode.APS_UNAVAILABLE,
                    "drawing mutations are temporarily disabled for a storage cutover",
                    retryable=True,
                    tool=PLAN_TOOL_NAME,
                ),
            )
        return _broker_run_plan(req)


def _broker_run_plan(req: BrokerPlanRunRequest) -> JSONResponse:
    return _broker_run_request(req)


def _broker_run(req: BrokerRunRequest) -> JSONResponse:
    if (req.tool or {}).get("name") == PLAN_TOOL_NAME:
        return JSONResponse(
            status_code=DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS],
            content=err_envelope(ErrorCode.BAD_PARAMS, "reserved tool name",
                                 retryable=False, tool=PLAN_TOOL_NAME),
        )
    return _broker_run_request(req)


def _broker_run_request(req: Union[BrokerRunRequest, BrokerPlanRunRequest]) -> JSONResponse:
    """Share admission and one terminal attribution for tool and plan requests."""
    t0 = time.perf_counter()
    is_plan = isinstance(req, BrokerPlanRunRequest)
    tool = PLAN_TOOL if is_plan else (req.tool or {})
    aps_live = True if is_plan else bool(req.aps_live)
    engine_op = tool.get("engine_op", "")
    entry: Dict[str, Any] = {
        "ts": time.time(),
        "tenant_id": req.tenant_id,
        "tool": tool.get("name"),
        "engine_op": engine_op,
        "aps_endpoint": APS_ENDPOINT,
        "aps_live": aps_live,
        "engine_seconds": None,
        "usd_est": None,
        "status": "unknown",
    }
    postgres_mode = _broker_store_mode() == "postgres"
    ledger_event_key = req.ledger_event_key or str(uuid.uuid4())
    # Identifies THIS invocation as the owner of any WorkItem correlation it
    # registers, so the `finally` below evicts only what this call created. A
    # redelivery of the same job_id gets its own token, registers nothing, and
    # therefore cannot evict the in-flight run's correlation.
    run_token = uuid.uuid4().hex
    admission: Optional[Dict[str, Any]] = None
    terminal_env: Optional[Dict[str, Any]] = None
    terminal_status: Optional[int] = None
    try:
        if postgres_mode and not req.ledger_event_key:
            entry["status"] = ErrorCode.BAD_PARAMS
            env, status = _classified_bad_params(
                "broker_event_key_required",
                "ledger_event_key is required when LEAF_BROKER_STORE=postgres",
                tool=tool.get("name"),
            )
            return JSONResponse(status_code=status, content=env)
        if postgres_mode:
            usage = _get_usage()
            tier = _tenant_tier(req.tenant_id)
            estimate = float(getattr(usage, "DEFAULT_EST_USD", 0.0)) if usage else 0.0
            spend_cap = usage.cap_for(req.tenant_id) if usage else None
            daily_limit = (
                usage.daily_run_limit_for(tier)
                if usage is not None and aps_live else None
            )
            admission = _postgres_store().admit_run(
                ledger_event_key,
                req.tenant_id,
                aps_live=aps_live,
                estimated_usd=estimate,
                spend_cap=spend_cap,
                daily_limit=daily_limit,
                request_fingerprint=_broker_request_fingerprint(req),
            )
            decision = admission["status"]
            if decision == "replay":
                return JSONResponse(
                    status_code=admission["http_status"],
                    content=admission["result"],
                )
            if decision in {"collision", "mismatch"}:
                admission = None
                env, status = _classified_bad_params(
                    "broker_event_key_invalid",
                    "broker run event key is not valid for this request",
                    tool=tool.get("name"),
                )
                return JSONResponse(status_code=status, content=env)
            if decision in {"leased", "executing"}:
                admission = None
                retryable = decision == "leased"
                detail = (
                    "broker run event is already leased"
                    if retryable
                    else "broker run execution started; automatic retry is unsafe"
                )
                env = err_envelope(
                    ErrorCode.TURN_IN_PROGRESS,
                    detail,
                    retryable=retryable,
                    tool=tool.get("name"),
                )
                return JSONResponse(
                    status_code=DEFAULT_HTTP_STATUS[ErrorCode.TURN_IN_PROGRESS],
                    content=env,
                )
            if decision == "spend_quota":
                assert usage is not None
                terminal_env = usage.quota_envelope(
                    req.tenant_id,
                    admission["spent"] + admission["reserved"],
                    admission["estimated_usd"],
                    admission["cap"],
                    tool=tool.get("name"),
                )
                terminal_env["degraded_mode"] = False
                terminal_status = 402
                entry["status"] = ErrorCode.QUOTA_EXCEEDED
                return JSONResponse(status_code=terminal_status, content=terminal_env)
            if decision == "daily_quota":
                assert usage is not None
                terminal_env = usage.daily_quota_envelope(
                    req.tenant_id,
                    tier,
                    admission["limit"],
                    admission["used"],
                    tool=tool.get("name"),
                )
                terminal_env["degraded_mode"] = False
                terminal_status = 429
                entry["status"] = ErrorCode.QUOTA_EXCEEDED
                return JSONResponse(status_code=terminal_status, content=terminal_env)
            if decision != "acquired":
                raise BrokerStateError(f"unknown broker admission result {decision!r}")

        execute = _execute_plan if is_plan else _execute
        terminal_env, terminal_status = execute(
            req, tool, engine_op, t0, entry,
            quota_reserved=postgres_mode,
            admission=admission,
            run_token=run_token,
        )
        if is_plan and "activity_version" in entry:
            terminal_env = dict(terminal_env)
            terminal_env["result"] = dict(
                terminal_env.get("result") or {}, activity_version=entry["activity_version"])
        entry["status"] = (
            "ok" if terminal_env.get("ok")
            else (terminal_env.get("error") or {}).get("error_code", "error")
        )
        return JSONResponse(status_code=terminal_status, content=terminal_env)
    except (CallbackPrimaryConfigurationError, CallbackPrimaryUnavailable) as exc:
        entry["status"] = "INTERNAL"
        terminal_env = err_envelope(
            ErrorCode.INTERNAL, str(exc), retryable=False, tool=tool.get("name"))
        terminal_status = DEFAULT_HTTP_STATUS[ErrorCode.INTERNAL]
        return JSONResponse(status_code=terminal_status, content=terminal_env)
    except ApsCapacityUnavailable:
        if admission is not None:
            admission["capacity_wait"] = True
        terminal_env = err_envelope(
            ErrorCode.APS_UNAVAILABLE,
            "APS fleet concurrency limit is currently full",
            retryable=True,
            tool=tool.get("name"),
        )
        terminal_status = DEFAULT_HTTP_STATUS[ErrorCode.APS_UNAVAILABLE]
        return JSONResponse(status_code=terminal_status, content=terminal_env)
    except entitlements.EntitlementsError:
        entry["status"] = "INTERNAL"
        required = entitlements.tool_required_capability(tool)
        tier = _tenant_tier(req.tenant_id)
        response = entitlements.policy_unavailable_response(required, tier)
        terminal_env = json.loads(response.body)
        terminal_status = response.status_code
        return response
    except Exception as exc:  # noqa: BLE001
        entry["status"] = "INTERNAL"
        # Class name only in the envelope; the message can carry internal
        # paths or state, so it goes to the broker log instead.
        print(f"[leaf-broker] run failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        terminal_env = err_envelope(
            ErrorCode.INTERNAL, f"run failed: {type(exc).__name__}", retryable=False,
            tool=tool.get("name"))
        terminal_status = DEFAULT_HTTP_STATUS[ErrorCode.INTERNAL]
        return JSONResponse(status_code=terminal_status, content=terminal_env)
    finally:
        # This run is over, but "over" is not the same as "the WorkItem stopped".
        # A SUCCESSFUL run polled its WorkItem to a terminal state, so the
        # correlation is genuinely dead and is dropped. Any other ending -- a
        # poll that hit da/client's 900s ceiling and simply RETURNED without a
        # DELETE, a mid-poll exception, a failure -- can leave the WorkItem
        # executing and BILLING, and persisting a `close` for it would make it
        # permanently unaddressable. For those the broker cancels its own orphan
        # -- no beacon is coming, because the app makes the row terminal and the
        # sweep only selects submitted/running rows -- and disowns it if that
        # cancel does not happen. Either way the token is what stops a duplicate
        # delivery, which registered nothing, from touching the live run's
        # correlation.
        if terminal_env is not None and terminal_env.get("ok"):
            _drop_active_workitem(req.job_id, run_token)
        else:
            _reap_or_disown_own_workitem(req.job_id, run_token)
        if (
            postgres_mode and admission is not None
            and admission.get("lease_token") and not admission.get("capacity_wait")
        ):
            if terminal_env is None or terminal_status is None:
                raise BrokerStateError("admitted broker run has no terminal result")
            _validate_terminal_ledger_numbers(entry)
            _postgres_store().complete_run(
                ledger_event_key,
                req.tenant_id,
                admission["lease_token"],
                _conform_ledger_entry(entry),
                terminal_env,
                terminal_status,
            )
            _emit_aps_metric(entry, ledger_event_key)
        elif not postgres_mode:
            _ledger_append(entry, ledger_event_key)
            _emit_aps_metric(entry, ledger_event_key)


def _start_admitted_execution(
    req: Union[BrokerRunRequest, BrokerPlanRunRequest], admission: Optional[Dict[str, Any]], *,
    aps_submission: Optional[bool] = None,
) -> None:
    """Persist the irreversible boundary immediately before tool execution."""
    if admission is None or admission.get("execution_started"):
        return
    live_slot = bool(req.aps_live) if aps_submission is None else bool(aps_submission)
    acquired = _postgres_store().mark_execution_started(
        req.ledger_event_key,
        req.tenant_id,
        admission["lease_token"],
        aps_live=live_slot,
        max_concurrency=_aps_max_concurrency() if live_slot else 1,
        slot_lease_seconds=_aps_slot_lease_seconds() if live_slot else 900,
    )
    if not acquired:
        admission["capacity_wait"] = True
        raise ApsCapacityUnavailable()
    admission["execution_started"] = True


PLAN_READINESS_TTL_S = 60
_plan_readiness_cache: Optional[Tuple[float, Dict[str, Any]]] = None
_plan_readiness_lock = threading.Lock()


def _plan_activity_ready() -> Tuple[bool, Dict[str, Any]]:
    """Bound the Activity alias/version read to one call per cache interval."""
    global _plan_readiness_cache
    with _plan_readiness_lock:
        now = time.monotonic()
        if (_plan_readiness_cache is not None
                and now - _plan_readiness_cache[0] < PLAN_READINESS_TTL_S):
            result = _plan_readiness_cache[1]
        else:
            try:
                import mutation_apply
                result = mutation_apply.readiness()
                if not isinstance(result, dict):
                    raise ValueError("invalid mutation Activity readiness result")
            except Exception as exc:  # readiness failure must never enable a run
                result = {"ready": False, "mismatches": [str(exc)]}
            _plan_readiness_cache = (time.monotonic(), result)
        return result.get("ready") is True, result


def _execute_plan(req: BrokerPlanRunRequest, tool: Dict[str, Any], engine_op: str,
                  t0: float, entry: Dict[str, Any], *, quota_reserved: bool = False,
                  admission: Optional[Dict[str, Any]] = None,
                  run_token: Optional[str] = None):
    if tenant_disabled(req.tenant_id):
        return (err_envelope(
            ErrorCode.TENANT_DISABLED,
            f"tenant {req.tenant_id!r} is disabled by the kill-switch",
            retryable=False, tool=PLAN_TOOL_NAME,
        ), DEFAULT_HTTP_STATUS[ErrorCode.TENANT_DISABLED])
    if not write_loop.drawing_mutations_enabled():
        return (err_envelope(
            ErrorCode.APS_UNAVAILABLE,
            "drawing mutations are temporarily disabled for a storage cutover",
            retryable=True, tool=PLAN_TOOL_NAME,
        ), 503)
    if not quota_reserved:
        capped = _cap_preflight(req.tenant_id, PLAN_TOOL)
        if capped is not None:
            return capped

    required_cap = entitlements.tool_required_capability(PLAN_TOOL)
    tier = _tenant_tier(req.tenant_id)
    if not entitlements.entitlements_for(tier).get(required_cap, False):
        return (err_envelope(
            ErrorCode.ENTITLEMENT_REQUIRED,
            f"tier {tier!r} is not entitled to {required_cap!r} for tool {PLAN_TOOL_NAME!r}",
            retryable=False, tool=PLAN_TOOL_NAME,
        ), DEFAULT_HTTP_STATUS[ErrorCode.ENTITLEMENT_REQUIRED])
    if not quota_reserved:
        capped_runs = _run_quota_preflight(req.tenant_id, tier, PLAN_TOOL)
        if capped_runs is not None:
            return capped_runs
    if req.dwg_version != req.plan.parent_version:
        return _classified_bad_params(
            "plan_parent_version_mismatch",
            "dwg_version must equal plan.parent_version", tool=PLAN_TOOL_NAME,
        )

    _require_supported_live_completion_mode()
    ready, readiness = _plan_activity_ready()
    if not ready:
        mismatches = "; ".join(str(item) for item in readiness.get("mismatches", []))
        return (err_envelope(
            ErrorCode.APS_UNAVAILABLE, f"mutation Activity not ready: {mismatches}",
            retryable=True, tool=PLAN_TOOL_NAME,
        ), 503)
    activity_version = (readiness.get("activity") or {}).get("version")
    entry["activity_version"] = activity_version
    da = _get_da()
    if da is None or not hasattr(da, "run_tool"):
        return (err_envelope(
            ErrorCode.APS_UNAVAILABLE,
            "a live browser edit needs the APS client; there is no degraded writer for a data plan",
            retryable=True, tool=PLAN_TOOL_NAME,
        ), 503)
    backend = write_loop.default_backend(aps_live=True, da=da)
    _start_admitted_execution(req, admission, aps_submission=True)
    return write_loop.run_data_plan_live(
        req.plan.model_dump(), req.tenant_id, backend=backend, da=da, t0=t0,
        ledger_entry=entry, holder=req.checkout_holder, fence=req.checkout_fence,
        on_submitted=_submission_recorder(req, run_token),
    )


def _execute(req: BrokerRunRequest, tool: Dict[str, Any], engine_op: str, t0: float,
             entry: Dict[str, Any], *, quota_reserved: bool = False,
             admission: Optional[Dict[str, Any]] = None,
             run_token: Optional[str] = None):
    # 1) kill-switch FIRST — a disabled tenant never touches APS
    if tenant_disabled(req.tenant_id):
        env = err_envelope(ErrorCode.TENANT_DISABLED,
                           f"tenant {req.tenant_id!r} is disabled by the kill-switch",
                           retryable=False, tool=tool.get("name"))
        return env, DEFAULT_HTTP_STATUS[ErrorCode.TENANT_DISABLED]

    if write_loop.is_write_tool(tool) and not write_loop.drawing_mutations_enabled():
        return (err_envelope(
            ErrorCode.APS_UNAVAILABLE,
            "drawing mutations are temporarily disabled for a storage cutover",
            retryable=True,
            tool=tool.get("name"),
        ), 503)

    # 1a) HARD pre-flight cost cap — a tenant over its spend cap is rejected
    #     BEFORE any APS call (off unless a cap is configured for the tenant).
    if not quota_reserved:
        capped = _cap_preflight(req.tenant_id, tool)
        if capped is not None:
            return capped  # (quota_exceeded envelope, HTTP 402)

    if not tool.get("name"):
        return (err_envelope(ErrorCode.BAD_PARAMS, "tool package missing 'name'", retryable=False),
                DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS])

    if req.test_source is not None:
        if req.aps_live:
            return (err_envelope(
                ErrorCode.BAD_PARAMS,
                "staged test source is forbidden for APS_LIVE=1",
                retryable=False,
                tool=tool.get("name"),
            ), DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS])
        if not req.test_source.strip():
            return (err_envelope(
                ErrorCode.BAD_PARAMS,
                "staged test source must not be empty",
                retryable=False,
                tool=tool.get("name"),
            ), DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS])

    # Phase 0 deployed-posture gate: tracked builtins and APS-only tools remain
    # available, but a tenant-controlled Python file cannot load in this
    # credential-bearing process unless authored execution is enabled AND a
    # real out-of-process sandbox tier is engaged. Production additionally
    # pins the provider-based micro-VM tier. Rejected here as defense in
    # depth even if startup validation (the same floor) was bypassed by a
    # direct function call.
    if (
        _deployed_runtime()
        and not _is_blank_dwg_request(req, tool)
        and not is_trusted_builtin_tool(tool, req.tenant_id)
    ):
        engaged = _tool_sandbox_tier() in ("subprocess", "microvm")
        if (not _authored_execution_enabled() or not engaged
                or (_production_runtime() and not _sandbox_configured())):
            return (err_envelope(
                ErrorCode.TENANT_DISABLED,
                "tenant-authored execution is disabled in this deployed posture",
                retryable=False,
                tool=tool.get("name"),
            ), DEFAULT_HTTP_STATUS[ErrorCode.TENANT_DISABLED])

    # 1b) F10: re-check the tenant's TIER entitlement at the broker (defense-in-depth).
    #     A direct broker call must not bypass the app-side tier gate — a write tool or
    #     build the tier lacks is denied here too. Tier comes from a broker-TRUSTED source
    #     (never the request body); unknown tenant -> demo -> full (open-demo design).
    required_cap = entitlements.tool_required_capability(tool)
    tier = _tenant_tier(req.tenant_id)
    if not entitlements.entitlements_for(tier).get(required_cap, False):
        return (err_envelope(
            ErrorCode.ENTITLEMENT_REQUIRED,
            f"tier {tier!r} is not entitled to {required_cap!r} for tool {tool.get('name')!r}",
            retryable=False, tool=tool.get("name")),
            DEFAULT_HTTP_STATUS[ErrorCode.ENTITLEMENT_REQUIRED])

    # 1d) F12 + A4: coarse per-tenant DAILY RUN quota (tier-keyed, count-based) — a
    #     liability cap on the NUMBER of APS-money runs/tenant/UTC-day, standing
    #     ALONGSIDE the USD spend cap (1a). Only the APS_LIVE=1 path spends real money
    #     (spec §a: APS_LIVE=0 is un-metered/free), so the cap applies ONLY to live runs
    #     and is checked HERE — before the WorkItem is dispatched. Over-cap -> the §10
    #     quota_exceeded envelope (HTTP 429). Reuses the F10-resolved `tier`; a DIFFERENT
    #     tenant under its own cap is unaffected.
    if req.aps_live and not quota_reserved:
        capped_runs = _run_quota_preflight(req.tenant_id, tier, tool)
        if capped_runs is not None:
            return capped_runs  # (quota_exceeded envelope, HTTP 429)

    if _is_blank_dwg_request(req, tool):
        if not req.aps_live or req.test_source is not None:
            return _classified_bad_params(
                "blank_dwg_live_required",
                "blank DWG feasibility requires the live broker path",
                tool=tool.get("name"),
            )
        project_id = str((req.params or {}).get("project_id") or "")
        source_sha = str((req.params or {}).get("source_sha") or "")
        drawing_name = str((req.params or {}).get("drawing_name") or "")
        try:
            org_id = uuid.UUID(req.tenant_id)
            project_uuid = uuid.UUID(project_id)
        except (TypeError, ValueError):
            return _classified_bad_params(
                "blank_dwg_scope_invalid",
                "blank DWG scope is invalid",
                tool=tool.get("name"),
            )
        project = platform_link.platform_store().get_project(org_id, project_uuid)
        if project is None or project.status != "active":
            return _classified_bad_params(
                "blank_dwg_project_unavailable",
                "project is unavailable for this tenant",
                tool=tool.get("name"),
            )
        _require_supported_live_completion_mode()
        da = _get_da()
        if da is None:
            return (
                err_envelope(
                    ErrorCode.APS_UNAVAILABLE,
                    "APS client is unavailable",
                    retryable=False,
                    tool=tool.get("name"),
                ),
                DEFAULT_HTTP_STATUS[ErrorCode.APS_UNAVAILABLE],
            )
        _start_admitted_execution(req, admission, aps_submission=True)
        producer = _get_blank_dwg_producer()
        result = producer.run(
            da,
            tenant_id=req.tenant_id,
            source_sha=source_sha,
            read_tool=_blank_dwg_read_tool(),
            publish=functools.partial(
                _publish_blank_dwg,
                tenant_id=req.tenant_id,
                project_id=project_id,
                drawing_name=drawing_name,
            ),
            on_submitted=_submission_recorder(req, run_token),
        )
        cost = result.get("cost") if isinstance(result, dict) else None
        if isinstance(cost, dict):
            entry["engine_seconds"] = cost.get("engine_seconds")
            entry["usd_est"] = cost.get("usd_est")
        envelope = ok_envelope(
            tool=tool.get("name"),
            version=tool.get("version", "1.0.0"),
            result=result,
            overlay=None,
            timing_ms=int((time.perf_counter() - t0) * 1000),
            cost=cost,
        )
        envelope["degraded_mode"] = False
        return envelope, 200

    params = dict(req.params or {})
    if req.test_source is not None and write_loop.is_write_tool(tool):
        # A staged-source run is validation, never a drawing mutation. Enforce
        # this at the credential broker boundary even when a direct caller
        # omits or explicitly clears the harness's dry-run parameter.
        params["dry_run"] = True
    degraded = False
    run_dynamic = functools.partial(
        run_tool_dynamic, test_source=req.test_source)

    # QA latency hook is NOT a tool param — pull it out before validation so it never
    # reaches the tool or the schema check. F12: it is HONORED only when QA hooks are
    # enabled (LEAF_QA_HOOKS; non-live default ON) — otherwise it is IGNORED ENTIRELY
    # so a tenant can't starve the shared worker pool with a large sleep in prod.
    qa_sleep = params.pop("_qa_sleep_s", None)
    if qa_sleep is not None and not _qa_hooks_enabled():
        qa_sleep = None

    # 1b) PRE-VALIDATE params against the tool's own JSON Schema (§8.4 step 1).
    # A schema violation returns a BAD_PARAMS envelope and the tool body NEVER
    # runs — for BOTH the live and the mock paths.
    perrs = validate_params(tool, params)
    if perrs:
        return _classified_bad_params(
            "tool_params_invalid",
            "params schema: " + "; ".join(perrs),
            tool=tool.get("name"),
        )

    # 1c) WRITE BRANCH (M2): a drawing.write tool produces a NEW immutable store
    #     version (undo/redo-able). Read tools do NOT match here and take the
    #     unchanged live/mock paths below, so the read backbone is byte-identical.
    #     F2 / lane 2B: the mock write branch executes the tenant tool body via
    #     run_tool_dynamic (passed as run_tool_dynamic_fn) — so it inherits the SAME
    #     LEAF_SANDBOX=e2b out-of-process sandbox as the read path (no tenant-code exec in
    #     this credential-holding PID when sandboxed).
    if write_loop.is_write_tool(tool):
        # dwg_version pins the version this write branches FROM (parent); None -> "head"
        # (byte-identical to before this feature).
        base_version = req.dwg_version if req.dwg_version is not None else "head"
        if req.aps_live:
            # Live writes resolve HostDwg from the versioned drawing store in
            # run_write_live. A same-named broker-local DWG is not authoritative
            # for a project drawing and must not be required here.
            _require_supported_live_completion_mode()
            da = _get_da()
            if da is not None and hasattr(da, "run_tool"):
                backend = write_loop.default_backend(aps_live=True, da=da)
                _start_admitted_execution(req, admission, aps_submission=True)
                # A live WRITE submits its own WorkItem and polls it exactly like
                # a read does, so an abandoned write burns money the same way.
                # It needs the same correlation.
                return write_loop.run_write_live(tool, params, req.tenant_id,
                                                 backend=backend, da=da, t0=t0,
                                                 run_tool_dynamic_fn=run_dynamic,
                                                 ledger_entry=entry, version=base_version,
                                                 holder=req.checkout_holder,
                                                 fence=req.checkout_fence,
                                                 on_submitted=_submission_recorder(req, run_token))
            # requested live but no da client -> degraded pure-python write
            backend = write_loop.default_backend(aps_live=False)
            _start_admitted_execution(req, admission, aps_submission=False)
            return write_loop.run_write_mock(tool, params, req.tenant_id, backend=backend,
                                             t0=t0, run_tool_dynamic_fn=run_dynamic,
                                             degraded=True, version=base_version,
                                             holder=req.checkout_holder,
                                             fence=req.checkout_fence)
        backend = write_loop.default_backend(aps_live=False)
        _start_admitted_execution(req, admission, aps_submission=False)
        return write_loop.run_write_mock(tool, params, req.tenant_id, backend=backend,
                                         t0=t0, run_tool_dynamic_fn=run_dynamic,
                                         version=base_version,
                                         holder=req.checkout_holder,
                                         fence=req.checkout_fence)

    # 2) live path — the ONLY code path that touches da/client.py + the credential
    if req.aps_live:
        # A version pin selects the tenant-owned upload store. An unpinned read
        # keeps the curated broker-local registry contract.
        _require_supported_live_completion_mode()
        try:
            live_dwg, live_dwg_is_temporary = _resolve_live_read_dwg(req)
        except write_loop.ProofStateUnreadable as exc:
            return (err_envelope(ErrorCode.INTERNAL, str(exc), retryable=True,
                                 tool=tool.get("name")),
                    503)
        except LiveReadResolutionError as exc:
            env = err_envelope(
                ErrorCode.BAD_PARAMS,
                str(exc),
                retryable=False,
                tool=tool.get("name"),
            )
            env["error"]["reason_code"] = exc.reason_code
            return env, DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS]
        except ValueError as exc:
            return _classified_bad_params(
                "uploaded_resolution_invalid",
                str(exc),
                tool=tool.get("name"),
            )
        try:
            da = _get_da()
        except Exception:
            if live_dwg_is_temporary:
                live_dwg.unlink(missing_ok=True)
            raise
        if da is None or not hasattr(da, "run_tool"):
            if live_dwg_is_temporary:
                live_dwg.unlink(missing_ok=True)
            degraded = True  # fall back to the pure-python path, flagged
        elif not _live_script_is_nonempty(tool, da):
            # FAIL CLOSED (review 2026-07-22, HIGH, round 2): verified against the
            # REAL da/client.py:tool_activity_spec resolution, never a
            # re-implemented heuristic and never a fabricated live script. See
            # `_live_script_is_nonempty`'s docstring (the shipped-tool `.lsp` path
            # mismatch this guard originally caught was fixed in PR #15).
            if live_dwg_is_temporary:
                live_dwg.unlink(missing_ok=True)
            return _classified_bad_params(
                "live_activity_unavailable",
                f"tool {tool.get('name')!r} has no usable live (APS) implementation "
                f"(its resolved Activity script is empty/unreadable); live "
                f"(APS_LIVE=1) runs of this tool are not supported; run with "
                f"aps_live=false",
                tool=tool.get("name"),
            )
        else:
            try:
                # Validated before any live branch: app input is a drawing name,
                # never an arbitrary broker-local path.
                assert live_dwg is not None
                local = str(live_dwg)
                # provision the tool's DA Activity on demand (idempotent; 409 =
                # already exists) so a newly authored tool's LeafTool_<op> exists
                # before the WorkItem is submitted.
                if hasattr(da, "ensure_tool_activity"):
                    _start_admitted_execution(req, admission, aps_submission=True)
                    da.ensure_tool_activity(tool)
                else:
                    _start_admitted_execution(req, admission, aps_submission=True)
                # Register this run's WorkItem id against the caller's job_id the
                # instant APS accepts it, so a tab closed mid-poll has something
                # to cancel. No job_id -> no callback -> unchanged call.
                on_submitted = _submission_recorder(req, run_token)
                env = dict(_run_live_tool(da, local, tool, params,
                                          on_submitted=on_submitted) or {})
                env.setdefault("ok", True)
                env.setdefault("tool", tool.get("name"))
                env.setdefault("version", tool.get("version", "1.0.0"))
                env.setdefault("result", {})
                env.setdefault("overlay", None)
                env.setdefault("timing_ms", int((time.perf_counter() - t0) * 1000))
                env.setdefault("cost", None)
                env.setdefault("error", None)
                env.setdefault("degraded_mode", False)
                cost = env.get("cost") or {}
                if isinstance(cost, dict):
                    entry["engine_seconds"] = cost.get("engine_seconds")
                    entry["usd_est"] = cost.get("usd_est")
                if not env.get("ok"):
                    env["error"] = env.get("error") or error_obj(
                        ErrorCode.WORKITEM_FAILED,
                        "WorkItem did not succeed",
                        retryable=True,
                    )
                    return env, DEFAULT_HTTP_STATUS[ErrorCode.WORKITEM_FAILED]
                return env, 200
            except EgressBlocked as exc:
                return (err_envelope(ErrorCode.INTERNAL, str(exc), retryable=False,
                                     tool=tool.get("name")), 500)
            except FileNotFoundError as exc:  # creds missing
                # The message names broker-local paths (the credential file);
                # log it, never return it.
                print(f"[leaf-broker] live run unavailable: {exc}", file=sys.stderr)
                return (err_envelope(ErrorCode.APS_UNAVAILABLE,
                                     "APS credentials are unavailable in the broker",
                                     retryable=False, tool=tool.get("name")),
                        DEFAULT_HTTP_STATUS[ErrorCode.APS_UNAVAILABLE])
            except Exception as exc:  # noqa: BLE001
                # Class name only; full detail to the broker log (internal
                # paths and state must not reach the envelope).
                print(f"[leaf-broker] live run failed: {type(exc).__name__}: {exc}",
                      file=sys.stderr)
                return (err_envelope(ErrorCode.WORKITEM_FAILED,
                                     f"live run failed: {type(exc).__name__}",
                                     retryable=True, tool=tool.get("name")),
                        DEFAULT_HTTP_STATUS[ErrorCode.WORKITEM_FAILED])
            finally:
                if live_dwg_is_temporary:
                    live_dwg.unlink(missing_ok=True)

    # 3) mock / pure-python path (APS_LIVE=0, or degraded live fallback):
    #    run_tool_dynamic loads and executes the TOOL FILE the registry entry
    #    references (the FILE is the tool) — no hardcoded engine_op dispatch.
    if qa_sleep is not None:
        try:
            time.sleep(min(float(qa_sleep), QA_SLEEP_CAP_S))  # QA latency-simulation hook
        except (TypeError, ValueError):
            pass
    if _is_campaign_host_fixture(req):
        _start_admitted_execution(req, admission, aps_submission=False)
        return _run_campaign_host_fixture(req, tool, run_dynamic, t0)

    # Which STORE drawing does this run target? The public `dwg` default
    # ("rooftop_demo") maps to the tenant's well-known `demo` store drawing —
    # unchanged. ANY OTHER dwg now resolves through the tenant's own store
    # (§19 review round 1, BLOCKER): before this, `req.dwg` was IGNORED
    # offline, so a run against an uploaded drawing silently executed on the
    # cached DEMO geometry — fabricated results labeled as the user's own.
    # Now an uploaded/extracted drawing runs on ITS real intake, and an
    # unknown/unextracted one fails closed (ensure_demo_drawing's guards
    # raise -> honest BAD_PARAMS).
    store_dwg = drawing_identity.store_id(req.dwg)
    if req.dwg_version is not None or store_dwg != write_loop.DEMO_DRAWING_ID:
        # VERSION-PINNED read, or a NON-DEFAULT drawing at head: load through
        # the SAME versioned store the write branch uses (da/store.py) —
        # mirrors write_loop.run_write_mock's own read_intake call. Unknown
        # drawing/version raises (KeyError/ValueError) -> clean BAD_PARAMS.
        try:
            backend = write_loop.backend_for_tenant(req.tenant_id, aps_live=False)
            write_loop.ensure_demo_drawing(backend, req.tenant_id, store_dwg)
            _, intake = write_loop.read_intake(
                backend, req.tenant_id, store_dwg,
                req.dwg_version if req.dwg_version is not None else "head")
        except write_loop.ProofStateUnreadable as exc:
            # Transport-unreadable proof state: the version exists; retryable.
            return (err_envelope(ErrorCode.INTERNAL, str(exc),
                                 retryable=True, tool=tool.get("name")),
                    503)
        except (KeyError, ValueError) as exc:
            return _classified_bad_params(
                "uploaded_resolution_invalid",
                f"drawing/version unavailable: {exc}",
                tool=tool.get("name"),
            )
    else:
        # Default drawing, no pin: the UNCHANGED cached-intake path,
        # byte-identical to the pre-§19 demo.
        try:
            intake = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            return (err_envelope(ErrorCode.INTERNAL, f"cached intake unavailable: {exc}",
                                 retryable=False, tool=tool.get("name")), 500)
    # F2 / lane 2B: run_tool_dynamic executes the tenant tool FILE. When LEAF_SANDBOX=e2b it
    # runs that body OUT of this credential-holding broker PID, in a locked-down sandbox with
    # no APS credential / token reachability and no egress (tool_loader._run_in_sandbox). With
    # LEAF_SANDBOX unset this is the unchanged in-process path.
    _start_admitted_execution(req, admission, aps_submission=False)
    env = run_dynamic(tool, intake, params, aps_live=False, da=None, t0=t0,
                      tenant_id=req.tenant_id)
    if not env.get("ok"):
        code = (env.get("error") or {}).get("error_code", ErrorCode.INTERNAL)
        return env, DEFAULT_HTTP_STATUS.get(code, 500)
    if degraded:
        env["degraded_mode"] = True  # requested live but fell back to pure-python
    return env, 200


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("BROKER_PORT", "8140")))
