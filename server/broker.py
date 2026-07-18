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

import json
import os
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
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
def _load_tenants() -> Dict[str, Any]:
    if TENANTS_PATH.exists():
        try:
            return json.loads(TENANTS_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _save_tenants(t: Dict[str, Any]) -> None:
    TENANTS_PATH.write_text(json.dumps(t, indent=2), encoding="utf-8")


_tenants: Dict[str, Any] = _load_tenants()


def tenant_disabled(tid: str) -> bool:
    with _tenants_lock:
        return bool(_tenants.get(tid, {}).get("disabled"))


def set_tenant_disabled(tid: str, disabled: bool) -> None:
    with _tenants_lock:
        _tenants.setdefault(tid, {})["disabled"] = disabled
        _tenants[tid]["updated_at"] = time.time()
        _save_tenants(_tenants)


# --------------------------------------------------------------------------- #
# attribution ledger — exactly ONE line per /broker/run
# --------------------------------------------------------------------------- #
def _ledger_append(entry: Dict[str, Any]) -> None:
    line = json.dumps(entry, separators=(",", ":"))
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


# --------------------------------------------------------------------------- #
# app
# --------------------------------------------------------------------------- #
app = FastAPI(title="Leaf APS broker v1", version="1.0.0")
install_error_handlers(app)


class BrokerRunRequest(BaseModel):
    tenant_id: str
    tool: Dict[str, Any]
    params: Dict[str, Any] = {}
    dwg: str = "rooftop_demo"
    aps_live: bool = False


@app.get("/broker/health")
def health() -> Dict[str, Any]:
    return with_envelope_fields({
        "ok": True,
        "role": "aps-broker",
        "aps_endpoint": APS_ENDPOINT,
        "ledger": str(LEDGER_PATH),
        "tenants_disabled": sorted(t for t, v in _tenants.items() if v.get("disabled")),
    })


@app.post("/broker/tenants/{tid}/disable")
def disable_tenant(tid: str) -> Dict[str, Any]:
    set_tenant_disabled(tid, True)
    return with_envelope_fields({"ok": True, "tenant_id": tid, "disabled": True})


@app.post("/broker/tenants/{tid}/enable")
def enable_tenant(tid: str) -> Dict[str, Any]:
    set_tenant_disabled(tid, False)
    return with_envelope_fields({"ok": True, "tenant_id": tid, "disabled": False})


class BrokerReapRequest(BaseModel):
    # each record: {status, workitem_id, session_closed?|lease_expires?}
    records: List[Dict[str, Any]] = []
    live: Optional[bool] = None  # None -> reaper decides (APS_LIVE + BROKER_REAP_LIVE)


@app.post("/broker/reap")
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


@app.post("/broker/run")
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

    params = dict(req.params or {})
    degraded = False

    # QA latency hook is NOT a tool param — pull it out before validation.
    qa_sleep = params.pop("_qa_sleep_s", None)

    # 1b) PRE-VALIDATE params against the tool's own JSON Schema (§8.4 step 1).
    # A schema violation returns a BAD_PARAMS envelope and the tool body NEVER
    # runs — for BOTH the live and the mock paths.
    perrs = validate_params(tool, params)
    if perrs:
        return (err_envelope(ErrorCode.BAD_PARAMS, "params schema: " + "; ".join(perrs),
                             retryable=False, tool=tool.get("name")),
                DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS])

    # 1c) WRITE BRANCH (M2): a drawing.write tool produces a NEW immutable store
    #     version (undo/redo-able). Read tools do NOT match here and take the
    #     unchanged live/mock paths below, so the read backbone is byte-identical.
    if write_loop.is_write_tool(tool):
        if req.aps_live:
            da = _get_da()
            if da is not None and hasattr(da, "run_tool"):
                backend = write_loop.default_backend(aps_live=True, da=da)
                return write_loop.run_write_live(tool, params, req.tenant_id,
                                                 backend=backend, da=da, t0=t0,
                                                 ledger_entry=entry)
            # requested live but no da client -> degraded pure-python write
            backend = write_loop.default_backend(aps_live=False)
            return write_loop.run_write_mock(tool, params, req.tenant_id, backend=backend,
                                             t0=t0, run_tool_dynamic_fn=run_tool_dynamic,
                                             degraded=True)
        backend = write_loop.default_backend(aps_live=False)
        return write_loop.run_write_mock(tool, params, req.tenant_id, backend=backend,
                                         t0=t0, run_tool_dynamic_fn=run_tool_dynamic)

    # 2) live path — the ONLY code path that touches da/client.py + the credential
    if req.aps_live:
        da = _get_da()
        if da is None or not hasattr(da, "run_tool"):
            degraded = True  # fall back to the pure-python path, flagged
        else:
            try:
                local = str(DATA_DIR / f"{req.dwg}.dwg")
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
    try:
        intake = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return (err_envelope(ErrorCode.INTERNAL, f"cached intake unavailable: {exc}",
                             retryable=False, tool=tool.get("name")), 500)
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
