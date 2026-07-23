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
  APS_CRED        (passed through to da/client.py; only read on live runs)
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

import hmac
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

SERVER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SERVER_DIR.parent
DATA_DIR = PROJECT_ROOT / "data"
DATA_FILE = DATA_DIR / "rooftop_demo.intake.json"
for p in (str(PROJECT_ROOT), str(SERVER_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import tools_fallback as fb  # noqa: E402,F401  (compat: builtins delegate here)
import entitlements  # noqa: E402  (F10: broker-side tier re-check; IMPORT/CALL only)
from envelopes import (  # noqa: E402
    DEFAULT_HTTP_STATUS,
    ErrorCode,
    err_envelope,
    install_error_handlers,
    ok_envelope,
    with_envelope_fields,
)
from tool_loader import run_tool_dynamic  # noqa: E402
from tool_validate import validate_params  # noqa: E402
import write_loop  # noqa: E402  (M2 write branch; never imports da.* at top)

LEDGER_PATH = Path(os.environ.get("BROKER_LEDGER", str(SERVER_DIR / "broker_ledger.jsonl")))
TENANTS_PATH = Path(os.environ.get("BROKER_TENANTS", str(SERVER_DIR / "broker_tenants.json")))
APS_ENDPOINT = "https://developer.api.autodesk.com"
QA_SLEEP_CAP_S = 30.0

_ledger_lock = threading.Lock()
_tenants_lock = threading.Lock()


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


def _ledger_append(entry: Dict[str, Any]) -> None:
    # allow_nan=False: if a future unconformed field ever carries a non-finite
    # number, fail LOUD here rather than write an unparseable ledger line.
    line = json.dumps(_conform_ledger_entry(entry), separators=(",", ":"), allow_nan=False)
    with _ledger_lock:
        with open(LEDGER_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


# --------------------------------------------------------------------------- #
# da/client.py loader (lazy — only a live run touches it; import failure must
# not break broker boot)
# --------------------------------------------------------------------------- #
_da_mod = None


def _get_da():
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


_usage_mod = None
_reaper_mod = None


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
    decision = usage.daily_run_quota_check(tenant_id, tier, LEDGER_PATH)
    if decision.get("ok"):
        return None
    # reject: coerce degraded_mode to a boolean for the schema-valid wire body
    env = dict(decision)
    env["degraded_mode"] = False
    return env, 429  # 429 Too Many Requests — daily run cap reached (distinct from 402 spend cap)


# --------------------------------------------------------------------------- #
# app
# --------------------------------------------------------------------------- #
app = FastAPI(title="Leaf APS broker v1", version="1.0.0")
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


def _broker_secret() -> Optional[str]:
    val = os.environ.get(BROKER_SECRET_ENV)
    return val if val else None


def _auth_live() -> bool:
    """Live/public mode — read at call time so one process can be toggled (mirrors
    deps.auth_live_enabled)."""
    return os.environ.get("LEAF_AUTH_LIVE", "0") == "1"


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


class BrokerRunRequest(BaseModel):
    tenant_id: str
    tool: Dict[str, Any]
    params: Dict[str, Any] = {}
    dwg: str = "rooftop_demo"
    aps_live: bool = False
    dwg_version: Optional[int] = None  # None -> head (unchanged); pin to a specific
    # immutable drawing version (da/store.py resolve_version) otherwise.


class BrokerExtractRequest(BaseModel):
    tenant_id: str
    dwg: str = "rooftop_demo"
    # Guest/account upload lane (CONTRACT-ADDENDUM §19): when true, `dwg`
    # resolves inside the UPLOADS staging area (data/uploads/) instead of the
    # curated library — via _resolve_upload_dwg, which applies the IDENTICAL
    # strictness. The two namespaces never cross-resolve.
    upload: bool = False


_DWG_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


def _resolve_live_dwg(dwg: str) -> Path:
    """Resolve a registry-style drawing name inside the broker-owned data root.

    The public contract is a bare drawing identifier, never a path and never a
    filename.  Keeping this check in the credential-holding broker means a
    compromised app process cannot use ``..``, platform-specific separators,
    absolute paths, symlinks, or a double/misleading suffix to make APS upload
    an arbitrary local file.
    """
    if not isinstance(dwg, str) or not _DWG_NAME_RE.fullmatch(dwg):
        raise ValueError("dwg must be a bare drawing name (letters, digits, '_' or '-')")

    root = DATA_DIR.resolve(strict=True)
    candidate = DATA_DIR / f"{dwg}.dwg"
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
    if not isinstance(name, str) or not _DWG_NAME_RE.fullmatch(name):
        raise ValueError("dwg must be a bare drawing name (letters, digits, '_' or '-')")
    if not isinstance(tenant_id, str) or not _DWG_NAME_RE.fullmatch(tenant_id):
        raise ValueError("tenant_id must be a bare id (letters, digits, '_' or '-')")
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
        "tenants_disabled": sorted(t for t in _tenants if tenant_disabled(t)),
    })


@app.post("/broker/tenants/{tid}/disable", dependencies=[Depends(require_broker_auth)])
def disable_tenant(tid: str) -> Dict[str, Any]:
    set_tenant_disabled(tid, True)
    return with_envelope_fields({"ok": True, "tenant_id": tid, "disabled": True})


@app.post("/broker/tenants/{tid}/enable", dependencies=[Depends(require_broker_auth)])
def enable_tenant(tid: str) -> Dict[str, Any]:
    set_tenant_disabled(tid, False)
    return with_envelope_fields({"ok": True, "tenant_id": tid, "disabled": False})


class BrokerReapRequest(BaseModel):
    # each record: {status, workitem_id, session_closed?|lease_expires?}
    records: List[Dict[str, Any]] = []
    live: Optional[bool] = None  # None -> reaper decides (APS_LIVE + BROKER_REAP_LIVE)


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
    reaped = reaper.sweep(req.records, cancel_client=cc)
    return JSONResponse(status_code=200, content=with_envelope_fields({
        "ok": True,
        "reaped": [r.get("workitem_id") for r in reaped],
        "count": len(reaped),
        "live": not isinstance(cc, reaper.StubCancelClient),
    }))


@app.post("/broker/extract", dependencies=[Depends(require_broker_auth)])
def broker_extract(req: BrokerExtractRequest) -> JSONResponse:
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
    try:
        return JSONResponse(
            status_code=200,
            content=with_envelope_fields({"intake": da.extract(str(local))}),
        )
    except EgressBlocked as exc:
        return JSONResponse(
            status_code=500,
            content=err_envelope(ErrorCode.INTERNAL, str(exc), retryable=False),
        )
    except FileNotFoundError as exc:
        return JSONResponse(
            status_code=DEFAULT_HTTP_STATUS[ErrorCode.APS_UNAVAILABLE],
            content=err_envelope(ErrorCode.APS_UNAVAILABLE, str(exc), retryable=False),
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=DEFAULT_HTTP_STATUS[ErrorCode.WORKITEM_FAILED],
            content=err_envelope(
                ErrorCode.WORKITEM_FAILED,
                f"{type(exc).__name__}: {exc}",
                retryable=True,
            ),
        )


@app.post("/broker/run", dependencies=[Depends(require_broker_auth)])
def broker_run(req: BrokerRunRequest) -> JSONResponse:
    """Run one tool for one tenant. Appends exactly ONE attribution line."""
    t0 = time.perf_counter()
    tool = req.tool or {}
    engine_op = tool.get("engine_op", "")
    entry: Dict[str, Any] = {
        "ts": time.time(),
        "tenant_id": req.tenant_id,
        "tool": tool.get("name"),
        "engine_op": engine_op,
        "aps_endpoint": APS_ENDPOINT,
        "aps_live": bool(req.aps_live),
        "engine_seconds": None,
        "usd_est": None,
        "status": "unknown",
    }
    try:
        env, status_code = _execute(req, tool, engine_op, t0, entry)
        entry["status"] = "ok" if env.get("ok") else (env.get("error") or {}).get("error_code", "error")
        return JSONResponse(status_code=status_code, content=env)
    except entitlements.EntitlementsError:
        entry["status"] = "INTERNAL"
        required = entitlements.tool_required_capability(tool)
        tier = _tenant_tier(req.tenant_id)
        return entitlements.policy_unavailable_response(required, tier)
    except Exception as exc:  # noqa: BLE001
        entry["status"] = "INTERNAL"
        env = err_envelope(ErrorCode.INTERNAL, f"{type(exc).__name__}: {exc}", retryable=False,
                           tool=tool.get("name"))
        return JSONResponse(status_code=DEFAULT_HTTP_STATUS[ErrorCode.INTERNAL], content=env)
    finally:
        _ledger_append(entry)


def _execute(req: BrokerRunRequest, tool: Dict[str, Any], engine_op: str, t0: float,
             entry: Dict[str, Any]):
    # 1) kill-switch FIRST — a disabled tenant never touches APS
    if tenant_disabled(req.tenant_id):
        env = err_envelope(ErrorCode.TENANT_DISABLED,
                           f"tenant {req.tenant_id!r} is disabled by the kill-switch",
                           retryable=False, tool=tool.get("name"))
        return env, DEFAULT_HTTP_STATUS[ErrorCode.TENANT_DISABLED]

    # 1a) HARD pre-flight cost cap — a tenant over its spend cap is rejected
    #     BEFORE any APS call (off unless a cap is configured for the tenant).
    capped = _cap_preflight(req.tenant_id, tool)
    if capped is not None:
        return capped  # (quota_exceeded envelope, HTTP 402)

    if not tool.get("name"):
        return (err_envelope(ErrorCode.BAD_PARAMS, "tool package missing 'name'", retryable=False),
                DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS])

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
    if req.aps_live:
        capped_runs = _run_quota_preflight(req.tenant_id, tier, tool)
        if capped_runs is not None:
            return capped_runs  # (quota_exceeded envelope, HTTP 429)

    params = dict(req.params or {})
    degraded = False

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
        return (err_envelope(ErrorCode.BAD_PARAMS, "params schema: " + "; ".join(perrs),
                             retryable=False, tool=tool.get("name")),
                DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS])

    live_dwg: Optional[Path] = None
    if req.aps_live:
        try:
            live_dwg = _resolve_live_dwg(req.dwg)
        except ValueError as exc:
            return (err_envelope(ErrorCode.BAD_PARAMS, str(exc), retryable=False,
                                 tool=tool.get("name")),
                    DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS])

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
            da = _get_da()
            if da is not None and hasattr(da, "run_tool"):
                backend = write_loop.default_backend(aps_live=True, da=da)
                return write_loop.run_write_live(tool, params, req.tenant_id,
                                                 backend=backend, da=da, t0=t0,
                                                 ledger_entry=entry, version=base_version)
            # requested live but no da client -> degraded pure-python write
            backend = write_loop.default_backend(aps_live=False)
            return write_loop.run_write_mock(tool, params, req.tenant_id, backend=backend,
                                             t0=t0, run_tool_dynamic_fn=run_tool_dynamic,
                                             degraded=True, version=base_version)
        backend = write_loop.default_backend(aps_live=False)
        return write_loop.run_write_mock(tool, params, req.tenant_id, backend=backend,
                                         t0=t0, run_tool_dynamic_fn=run_tool_dynamic,
                                         version=base_version)

    # 2) live path — the ONLY code path that touches da/client.py + the credential
    if req.aps_live:
        da = _get_da()
        if da is None or not hasattr(da, "run_tool"):
            degraded = True  # fall back to the pure-python path, flagged
        elif req.dwg_version is not None:
            # FAIL CLOSED (review 2026-07-22, HIGH): the live (APS) read path
            # resolves a single broker-owned DWG file via `_resolve_live_dwg` — it
            # has NO versioned-store wiring at all, so a `dwg_version` pin was being
            # silently ignored here (a pinned read against an aps_live=True run
            # executed the unpinned live DWG regardless of the pin, e.g.
            # dwg_version=999 still returned 200). A pin must never be silently
            # dropped: reject before any WorkItem submission rather than execute
            # against the wrong drawing. Follow-up: wire live reads through the
            # versioned store's DWG representation (mirrors the write path).
            return (err_envelope(
                ErrorCode.BAD_PARAMS,
                "dwg_version pinning is not yet supported for live (APS_LIVE=1) reads; "
                "omit dwg_version or run with aps_live=false",
                retryable=False, tool=tool.get("name")),
                DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS])
        elif not _live_script_is_nonempty(tool, da):
            # FAIL CLOSED (review 2026-07-22, HIGH, round 2): verified against the
            # REAL da/client.py:tool_activity_spec resolution, never a
            # re-implemented heuristic and never a fabricated live script. See
            # `_live_script_is_nonempty`'s docstring (the shipped-tool `.lsp` path
            # mismatch this guard originally caught was fixed in PR #15).
            return (err_envelope(
                ErrorCode.BAD_PARAMS,
                f"tool {tool.get('name')!r} has no usable live (APS) implementation "
                f"(its resolved Activity script is empty/unreadable) — live "
                f"(APS_LIVE=1) runs of this tool are not supported; run with "
                f"aps_live=false",
                retryable=False, tool=tool.get("name")),
                DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS])
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
                    da.ensure_tool_activity(tool)
                env = dict(da.run_tool(local, tool, params) or {})
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
                    env["error"] = env.get("error") or {
                        "error_code": ErrorCode.WORKITEM_FAILED,
                        "message": "WorkItem did not succeed", "retryable": True}
                    return env, DEFAULT_HTTP_STATUS[ErrorCode.WORKITEM_FAILED]
                return env, 200
            except EgressBlocked as exc:
                return (err_envelope(ErrorCode.INTERNAL, str(exc), retryable=False,
                                     tool=tool.get("name")), 500)
            except FileNotFoundError as exc:  # creds missing
                return (err_envelope(ErrorCode.APS_UNAVAILABLE, str(exc), retryable=False,
                                     tool=tool.get("name")),
                        DEFAULT_HTTP_STATUS[ErrorCode.APS_UNAVAILABLE])
            except Exception as exc:  # noqa: BLE001
                return (err_envelope(ErrorCode.WORKITEM_FAILED, f"{type(exc).__name__}: {exc}",
                                     retryable=True, tool=tool.get("name")),
                        DEFAULT_HTTP_STATUS[ErrorCode.WORKITEM_FAILED])

    # 3) mock / pure-python path (APS_LIVE=0, or degraded live fallback):
    #    run_tool_dynamic loads and executes the TOOL FILE the registry entry
    #    references (the FILE is the tool) — no hardcoded engine_op dispatch.
    if qa_sleep is not None:
        try:
            time.sleep(min(float(qa_sleep), QA_SLEEP_CAP_S))  # QA latency-simulation hook
        except (TypeError, ValueError):
            pass
    # Which STORE drawing does this run target? The public `dwg` default
    # ("rooftop_demo") maps to the tenant's well-known `demo` store drawing —
    # unchanged. ANY OTHER dwg now resolves through the tenant's own store
    # (§19 review round 1, BLOCKER): before this, `req.dwg` was IGNORED
    # offline, so a run against an uploaded drawing silently executed on the
    # cached DEMO geometry — fabricated results labeled as the user's own.
    # Now an uploaded/extracted drawing runs on ITS real intake, and an
    # unknown/unextracted one fails closed (ensure_demo_drawing's guards
    # raise -> honest BAD_PARAMS).
    store_dwg = (write_loop.DEMO_DRAWING_ID if req.dwg == "rooftop_demo" else req.dwg)
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
        except (KeyError, ValueError) as exc:
            return (err_envelope(ErrorCode.BAD_PARAMS, f"drawing/version unavailable: {exc}",
                                 retryable=False, tool=tool.get("name")),
                    DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS])
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
    env = run_tool_dynamic(tool, intake, params, aps_live=False, da=None, t0=t0,
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
