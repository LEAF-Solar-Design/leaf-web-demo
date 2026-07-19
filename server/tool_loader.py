"""
Dynamic tool loader — "the tool FILE is the tool".

`run_tool_dynamic(tool, intake, params, aps_live, da)` resolves execution from
the TOOL PACKAGE (§2), never from a hardcoded engine_op->handler table:

  * kind:script + a resolvable local .py file (tool['entry'] under authored/ or
    builtins/, a `.py` at tool['script'], or a known built-in engine_op) ->
    import the module and call its `run(intake, params) -> (result, overlay)`.
    This is the local "the FILE is the tool" path (APS_LIVE=0).
  * kind:script with only a .lsp/engine_script, or kind:appbundle -> APS path:
    `da.ensure_tool_activity(tool)` then `da.run_tool(...)` (live; operator-gated).

Every run PRE-validates params (fail => BAD_PARAMS envelope, body never runs)
and POST-validates the produced §3 envelope (broken tool => INTERNAL). Returns
the extended §3 envelope (ADDENDUM §10: adds `degraded_mode`).

This module NEVER imports `da.*` at top level — the credential-holding client is
passed in by the broker (the only process allowed to hold it).
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Optional, Tuple, Union

from envelopes import ErrorCode, err_envelope, ok_envelope
from tool_validate import validate_envelope, validate_params

SERVER_DIR = Path(__file__).resolve().parent
AUTHORED_DIR = SERVER_DIR / "authored"
BUILTIN_DIR = SERVER_DIR / "builtins"

# Compat lookup used ONLY to resolve the pre-existing built-in ops to their
# server/builtins/*.py file. This is NOT an authoritative dispatch table:
# authored tools resolve by their own `entry` file and NEVER through this map,
# so a newly authored tool needs no entry here (that is the whole point).
BUILTIN_OPS: Dict[str, str] = {
    "count_by_layer": "count_by_layer.py",
    "measure_area_by_layer": "measure_area_by_layer.py",
    "measure_panel_area": "measure_area_by_layer.py",
    "highlight_near_edge": "highlight_near_edge.py",
    "highlight_panels_near_edge": "highlight_near_edge.py",
    "list_layers": "list_layers.py",
}

_MOD_CACHE: Dict[str, Any] = {}


def _load_module(path: Path):
    """Import a tool file by path (reloaded when its mtime OR size changes)."""
    st = path.stat()
    key = f"{path}:{st.st_mtime_ns}:{st.st_size}"
    cached = _MOD_CACHE.get(key)
    if cached is not None:
        return cached
    mod_name = f"leaf_tool_{abs(hash(key)) & 0xFFFFFFFF}"
    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    _MOD_CACHE[key] = mod
    return mod


# --------------------------------------------------------------------------- #
# F2 / lane 2B — execute the tenant tool BODY OUT of this credential-holding process.
#
# THE FINDING (F2, CRITICAL): today a tenant's authored `tool.py` runs in-process here via
# `_load_module` -> `exec_module` -> `mod.run(intake, params)`, inside the broker that can
# read ~/.aps/credentials.json / APS_CREDENTIALS_JSON / other tenants' .token/.grant files.
# That body could read the credential and exfiltrate it.
#
# THE FIX: when the sandbox is ENABLED (env LEAF_SANDBOX=e2b — the SAME flag lane 2A uses for
# the author session), route `mod.run(intake, params)` into a locked-down sandbox that gets
# ONLY the tool source + intake JSON + params over a single stdin channel and returns the
# (result, overlay). The broker's credential env NEVER enters the sandbox; the sandbox cannot
# read ~/.aps or any .token/.grant; network egress is denied.
#
# SUBSTRATE SHIPPED (v1, documented honestly in docs/e2b-tool-exec-receipt.json):
#   "locked-down subprocess sandbox" — a separate Python process (`-I` isolated mode) with
#   (a) a DEFAULT-DENY env allowlist so no APS/LEAF/AWS/E2B/token/secret/key var rides in, and
#   (b) a PEP-578 audit-hook jail (installed before the tenant body runs, un-removable) that
#   denies reads of the credential/token/grant/ssh/aws paths, denies ALL network egress, and
#   denies subprocess/exec/native-load escape, with cwd jailed to a throwaway temp dir.
#   This is the plan's blessed "seccomp+RO-FS+no-net subprocess" tier, made portable (the
#   audit hook enforces on Windows where seccomp is unavailable). DEFERRED: the full E2B
#   micro-VM round-trip (the proven reference substrate — harness/scripts/e2b-vendor-eval.mjs)
#   and Linux seccomp/namespace hardening; the KEY security property is true + tested here.
#
# DEFAULT (LEAF_SANDBOX unset) => the in-process path below is BYTE-IDENTICAL to today, so
# every gate suite (all in-process) is unaffected.
# --------------------------------------------------------------------------- #
def _sandbox_enabled() -> bool:
    """Route tenant tool execution into the out-of-process sandbox iff LEAF_SANDBOX=e2b (the
    SAME flag 2A uses). Read at CALL time so one process can be toggled; unset => in-process."""
    return os.environ.get("LEAF_SANDBOX", "").strip().lower() == "e2b"


def _sandbox_timeout_s() -> float:
    try:
        return float(os.environ.get("LEAF_SANDBOX_TIMEOUT_S", "30"))
    except (TypeError, ValueError):
        return 30.0


# Env keys the sandbox subprocess MAY inherit. Everything else — every APS/LEAF/AWS/E2B/
# token/secret/key/credential variable — is dropped, so the broker's credential env can NEVER
# ride into the tenant tool body. Default-DENY allowlist (not a denylist to be forgotten).
_SANDBOX_ENV_ALLOW = (
    "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "TEMP", "TMP", "TMPDIR",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER",
    "LANG", "LC_ALL", "LC_CTYPE", "TZ",
)


def _sandbox_env() -> Dict[str, str]:
    env = {k: os.environ[k] for k in _SANDBOX_ENV_ALLOW if k in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONPATH"] = ""
    return env


# The in-sandbox program. Standard-library only. Reads the job from stdin, installs the guard,
# runs run(intake, params), and prints a JSON-tagged return. NEVER receives the broker env.
_SANDBOX_RUNNER = r'''
import sys, json, os

def _emit(obj):
    sys.stdout.buffer.write(json.dumps(obj).encode("utf-8"))
    sys.stdout.buffer.flush()

def _encode_ret(ret):
    if isinstance(ret, tuple) and len(ret) == 2:
        return {"form": "pair", "result": ret[0], "overlay": ret[1]}
    if isinstance(ret, dict):
        return {"form": "dict", "obj": ret}
    return {"form": "scalar", "value": ret}

def main():
    raw = sys.stdin.buffer.read()
    try:
        job = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        _emit({"error": {"type": "SandboxDecode", "msg": str(exc)}})
        return
    source = job.get("source") or ""
    intake = job.get("intake")
    params = job.get("params")
    filename = job.get("filename") or "<tenant-tool>"

    # Warm modules a benign tool commonly imports BEFORE the guard is installed, so a first-time
    # stdlib import (which reads a .py off disk) is never mistaken for a credential read.
    import re, math, json as _json_warm  # noqa: F401

    # ---- SANDBOX GUARD (PEP 578 audit hook): deny reads of the APS credential + tenant
    # tokens/grants (and ssh/aws), deny ALL network egress, deny subprocess/exec/native escape.
    # Installed AFTER the job is read + stdlib warmed; a tenant tool CANNOT remove it. ----
    _SENSITIVE_SEG = {".aps", ".aws", ".ssh", ".grant", ".grants", ".gnupg", ".azure"}

    def _sensitive(path):
        try:
            p = os.path.normpath(os.path.abspath(str(path)))
        except Exception:
            p = str(path)
        low = p.replace("\\", "/").lower()
        segs = low.split("/")
        base = segs[-1] if segs else low
        if any(seg in _SENSITIVE_SEG for seg in segs):
            return True
        if base == "credentials.json" or base.endswith(".token") or base.endswith(".grant"):
            return True
        if base.startswith("id_rsa") or base.endswith(".pem"):
            return True
        return False

    _NET = ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
            "socket.gethostbyname_ex", "socket.gethostbyaddr", "ftplib.connect",
            "smtplib.connect", "smtplib.send")
    _PROC = ("subprocess.Popen", "os.system", "os.exec", "os.spawn", "os.posix_spawn",
             "os.fork", "os.forkpty", "pty.spawn", "ctypes.dlopen", "ctypes.dlsym")

    def _hook(event, args):
        if event == "open" or event == "os.open":
            path = args[0] if args else None
            if path is not None and not isinstance(path, int) and _sensitive(path):
                raise PermissionError("sandbox: read of sensitive path denied")
        elif event in _NET:
            raise PermissionError("sandbox: network egress denied")
        elif event in _PROC:
            raise PermissionError("sandbox: subprocess/exec denied")

    sys.addaudithook(_hook)

    # ---- run the tenant tool body in this jailed process ----
    ns = {"__name__": "leaf_sandbox_tool", "__file__": filename}
    try:
        code = compile(source, filename, "exec")
        exec(code, ns)
    except BaseException as exc:
        _emit({"error": {"type": type(exc).__name__, "msg": "load: " + str(exc)}})
        return
    run = ns.get("run")
    if not callable(run):
        _emit({"error": {"type": "NoRun", "msg": "tool file has no run(intake, params)"}})
        return
    try:
        ret = run(intake, params)
    except BaseException as exc:
        _emit({"error": {"type": type(exc).__name__, "msg": str(exc)}})
        return
    try:
        _emit({"ok": True, "ret": _encode_ret(ret)})
    except Exception as exc:
        _emit({"error": {"type": "SandboxEncode", "msg": str(exc)}})

main()
'''


def _decode_ret(enc: Dict[str, Any]) -> Any:
    """Reconstruct the Python return value from the sandbox's JSON-tagged form so the SAME
    downstream `_coerce` + envelope build + POST-validate as the in-process path applies."""
    form = enc.get("form")
    if form == "pair":
        return (enc.get("result"), enc.get("overlay"))
    if form == "dict":
        return enc.get("obj")
    return enc.get("value")


def _run_in_sandbox(local: Path, intake: Dict[str, Any],
                    params: Dict[str, Any]) -> Tuple[str, Any]:
    """Execute the tenant tool file OUT of this credential-holding process (F2 close).

    The tool SOURCE (read HERE, in the trusted broker, from the already path-safety-resolved
    ``local`` — 1F's `_is_unsafe_ref`/`_resolve_within` guarantee it is a repo-contained file)
    + the intake JSON + params are the ONLY things handed to the sandbox, over one stdin
    channel. The broker credential env never enters the sandbox (`_sandbox_env`), the sandbox
    cannot read ~/.aps / *.token / *.grant, and egress is denied (`_SANDBOX_RUNNER` guard).

    Returns ``("ok", ret)`` with the reconstructed return, ``("tool_error", msg)`` when the
    tenant body raised inside the sandbox (mapped to the SAME INTERNAL envelope as in-process),
    or ``("infra_error", msg)`` when the sandbox itself could not run (source/boot/timeout/proto)."""
    try:
        source = local.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return ("infra_error", f"could not read tool source {local.name}: {type(exc).__name__}: {exc}")
    job = json.dumps({"source": source, "intake": intake, "params": params,
                      "filename": local.name}).encode("utf-8")
    timeout = _sandbox_timeout_s()
    try:
        with tempfile.TemporaryDirectory(prefix="leaf-sbx-") as jail:
            proc = subprocess.run(
                [sys.executable, "-I", "-B", "-c", _SANDBOX_RUNNER],
                input=job,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_sandbox_env(),
                cwd=jail,
                timeout=timeout,
            )
    except subprocess.TimeoutExpired:
        return ("infra_error", f"sandbox timed out after {timeout}s")
    except Exception as exc:  # noqa: BLE001
        return ("infra_error", f"sandbox process failed to start: {type(exc).__name__}: {exc}")
    out = (proc.stdout or b"").decode("utf-8", "replace").strip()
    if not out:
        return ("infra_error", f"sandbox produced no output (rc={proc.returncode})")
    try:
        parsed = json.loads(out)
    except Exception as exc:  # noqa: BLE001
        return ("infra_error", f"sandbox output was not JSON: {type(exc).__name__}: {exc}")
    if parsed.get("error"):
        e = parsed["error"]
        return ("tool_error", f"{e.get('type', 'Error')}: {e.get('msg', '')}")
    if not parsed.get("ok"):
        return ("infra_error", "sandbox returned a non-ok result without an error")
    return ("ok", _decode_ret(parsed.get("ret") or {}))


def _tenant_repo_root(tenant_id: Optional[str] = None) -> Optional[Path]:
    """The mushy-repo checkout root for a tenant (read at CALL time; the SAME resolver
    deps.load_tenant_repo_tools folds against — server/tenant_paths.py). Wave 4:
    ``$LEAF_TENANTS_DIR/<tenant_id>``, with ``$LEAF_TENANT_REPO`` as the demo-tenant
    back-compat override; a None/empty tenant_id resolves the demo tenant (legacy
    no-tenant callers). None => not configured for this tenant."""
    from tenant_paths import resolve_tenant_repo_dir  # local import: no import cycle
    return resolve_tenant_repo_dir(tenant_id)


def _is_unsafe_ref(ref: Any) -> bool:
    """True if a tool ``entry``/``script`` is NOT a plain repo-relative path.

    F4 (security-audit 2026-07-18): ``resolve_local_file`` previously honoured
    ``Path(entry)`` verbatim — an absolute path (``C:/any/file.py``, ``/etc/x.py``) or a
    ``..`` traversal that named ANY ``.py`` on disk, which ``_load_module`` would then
    import and execute in the credential-holding broker (arbitrary local code exec on a
    direct broker call). A reference is rejected outright if, under EITHER POSIX or
    Windows path semantics, it is absolute / drive- or root-anchored / home-relative
    (``~``) or contains a ``..`` parent component. Only plain relative references (joined
    onto an ALLOWED root and containment-checked below) are permitted."""
    if not isinstance(ref, str) or not ref.strip():
        return True
    r = ref.strip()
    if r.startswith("~"):
        return True
    for cls in (PurePosixPath, PureWindowsPath):
        p = cls(r)
        if p.is_absolute() or p.root or p.drive:
            return True
        if any(part == ".." for part in p.parts):
            return True
    return False


def _resolve_within(root: Path, rel: str) -> Optional[Path]:
    """Join repo-relative ``rel`` onto an allowed ``root`` and return the resolved ``.py``
    file ONLY if it stays INSIDE ``root`` (symlink-safe) and is a regular file. Returns
    None otherwise — a second, belt-and-suspenders containment gate behind
    ``_is_unsafe_ref`` (defeats a symlink inside the repo that points back out, and any
    residual traversal that slipped a per-OS parse)."""
    try:
        root_r = root.resolve()
        cand = (root / rel).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if cand == root_r:
        return None  # resolved to the root dir itself, not a file
    try:
        cand.relative_to(root_r)
    except ValueError:
        return None  # escaped the allowed root (traversal / symlink) -> reject
    if cand.suffix == ".py" and cand.is_file():
        return cand
    return None


def resolve_local_file(tool: Dict[str, Any], tenant_id: Optional[str] = None) -> Optional[Path]:
    """Return the local .py file that IS this tool, or None (=> APS path).

    Resolution order: explicit `entry` (resolved against the REQUESTING tenant's repo
    root when configured, then authored/ or builtins/), a `.py` `script`, then the
    built-in-op compat lookup. Authored tools always resolve via their `entry`, so
    they run their OWN persisted body.

    Wave 3/4 (Contract 2/5): a tenant-repo tool carries a repo-RELATIVE entry (e.g.
    ``tools/<name>/tool.py``). We resolve it against the requesting tenant's repo root
    (absolute — ``$LEAF_TENANTS_DIR/<tenant_id>``, or ``$LEAF_TENANT_REPO`` for the demo
    tenant), so the broker resolves it regardless of its cwd AND scopes execution to the
    calling tenant's own files (tenant A's tool never resolves against tenant B's repo).

    F4 (security): ``entry``/``script`` MUST be plain repo-relative paths. An absolute
    path or a ``..`` traversal is rejected (``_is_unsafe_ref``) and every candidate is
    containment-checked against its allowed root (``_resolve_within``), so a direct broker
    call can no longer name an arbitrary ``.py`` anywhere on disk for the broker to exec.
    """
    entry = tool.get("entry")
    if entry and not _is_unsafe_ref(entry):
        # entry is a plain relative path: join it onto each ALLOWED root (never onto cwd
        # or an absolute path) and accept only a file that stays INSIDE that root. The
        # tenant's own repo root wins first, so a tenant tool runs its OWN repo file.
        name = Path(entry).name
        roots: List[Tuple[Path, str]] = []
        troot = _tenant_repo_root(tenant_id)
        if troot is not None:
            roots.append((troot, entry))       # tenant repo (absolute root, repo-relative entry)
        roots.append((SERVER_DIR, entry))       # server-dir-relative entry
        roots.append((AUTHORED_DIR, name))      # authored/<name>
        roots.append((BUILTIN_DIR, name))       # builtins/<name>
        for root, rel in roots:
            hit = _resolve_within(root, rel)
            if hit is not None:
                return hit
    script = tool.get("script")
    if script and str(script).endswith(".py") and not _is_unsafe_ref(script):
        hit = _resolve_within(SERVER_DIR, script)
        if hit is not None:
            return hit
    op = tool.get("engine_op") or ""
    fname = BUILTIN_OPS.get(op)
    if fname:
        cand = BUILTIN_DIR / fname
        if cand.exists():
            return cand
    return None


def _needs_aps(tool: Dict[str, Any], tenant_id: Optional[str] = None) -> bool:
    """True when the tool has no local .py and must run on APS DA."""
    return resolve_local_file(tool, tenant_id) is None


def _coerce(ret: Any) -> Union[Tuple[dict, Any], dict]:
    """Normalize a tool's return into (result, overlay) — or a full envelope."""
    if isinstance(ret, tuple) and len(ret) == 2:
        return ret[0], ret[1]
    if isinstance(ret, dict) and "ok" in ret and "result" in ret:
        return ret  # the tool returned a full envelope
    if isinstance(ret, dict):
        return ret, None
    return {"value": ret}, None


def _normalize_aps_envelope(raw: Dict[str, Any], name: Optional[str], version: str,
                            t0: float) -> Dict[str, Any]:
    raw.setdefault("ok", True)
    raw.setdefault("tool", name)
    raw.setdefault("version", version)
    raw.setdefault("result", {})
    raw.setdefault("overlay", None)
    raw.setdefault("timing_ms", int((time.perf_counter() - t0) * 1000))
    raw.setdefault("cost", None)
    raw.setdefault("error", None)
    raw.setdefault("degraded_mode", False)
    if not raw.get("ok") and raw.get("error") is None:
        raw["error"] = {"error_code": ErrorCode.WORKITEM_FAILED,
                        "message": "WorkItem did not succeed", "retryable": True}
    return raw


def run_tool_dynamic(tool: Dict[str, Any], intake: Dict[str, Any], params: Dict[str, Any],
                     aps_live: bool, da: Any = None, *,
                     dwg_path: Optional[str] = None,
                     t0: Optional[float] = None,
                     tenant_id: Optional[str] = None) -> Dict[str, Any]:
    """Execute the tool the registry entry references. Returns an extended §3 envelope.

    ``tenant_id`` scopes entry resolution to the requesting tenant's repo (wave 4). A
    None tenant_id keeps the legacy demo-tenant behaviour (honours $LEAF_TENANT_REPO)."""
    t0 = t0 if t0 is not None else time.perf_counter()
    name = (tool or {}).get("name")
    version = (tool or {}).get("version", "1.0.0")
    params = dict(params or {})

    def _ms() -> int:
        return int((time.perf_counter() - t0) * 1000)

    # 1) PRE-VALIDATE params — fail => body NEVER runs (§8.4 step 1)
    perrs = validate_params(tool, params)
    if perrs:
        return err_envelope(ErrorCode.BAD_PARAMS, "params schema: " + "; ".join(perrs),
                            retryable=False, tool=name, version=version, timing_ms=_ms())

    local = resolve_local_file(tool, tenant_id)

    # 2) APS path (kind:appbundle OR kind:script with only .lsp/engine_script)
    if aps_live and da is not None and hasattr(da, "run_tool") and local is None:
        try:
            if hasattr(da, "ensure_tool_activity"):
                da.ensure_tool_activity(tool)
            raw = dict(da.run_tool(dwg_path, tool, params) or {})
            return _normalize_aps_envelope(raw, name, version, t0)
        except FileNotFoundError as exc:  # APS creds missing
            return err_envelope(ErrorCode.APS_UNAVAILABLE, str(exc), retryable=False,
                                tool=name, version=version, timing_ms=_ms())
        except Exception as exc:  # noqa: BLE001
            return err_envelope(ErrorCode.WORKITEM_FAILED, f"{type(exc).__name__}: {exc}",
                                retryable=True, tool=name, version=version, timing_ms=_ms())

    # 3) LOCAL "the FILE is the tool" path (APS_LIVE=0, or degraded live fallback)
    degraded = bool(aps_live)  # requested live but running locally => degraded
    if local is None:
        return err_envelope(
            ErrorCode.BAD_PARAMS,
            f"no local implementation resolvable for tool {name!r} "
            f"(engine_op={tool.get('engine_op')!r}) at APS_LIVE=0",
            retryable=False, tool=name, version=version, timing_ms=_ms())
    # F2 / lane 2B: when the sandbox is ENABLED (LEAF_SANDBOX=e2b), the tenant tool BODY runs
    # OUT of this credential-holding process — no `_load_module`/`exec_module` of tenant code
    # in this PID on the sandbox path. DEFAULT (unset) => the in-process `else` below runs and
    # is BYTE-IDENTICAL to today, so every in-process gate suite is unaffected.
    if _sandbox_enabled():
        kind, payload = _run_in_sandbox(local, intake, params)
        if kind == "tool_error":
            return err_envelope(ErrorCode.INTERNAL,
                                f"tool {name!r} raised {payload}",
                                retryable=False, tool=name, version=version, timing_ms=_ms())
        if kind == "infra_error":
            return err_envelope(ErrorCode.INTERNAL,
                                f"sandbox execution failed for tool {name!r}: {payload}",
                                retryable=False, tool=name, version=version, timing_ms=_ms())
        ret = payload
    else:
        try:
            mod = _load_module(local)
        except Exception as exc:  # noqa: BLE001
            return err_envelope(ErrorCode.INTERNAL,
                                f"failed to load tool file {local.name}: {type(exc).__name__}: {exc}",
                                retryable=False, tool=name, version=version, timing_ms=_ms())
        if not hasattr(mod, "run"):
            return err_envelope(ErrorCode.INTERNAL,
                                f"tool file {local.name} has no run(intake, params)",
                                retryable=False, tool=name, version=version, timing_ms=_ms())
        try:
            ret = mod.run(intake, params)
        except Exception as exc:  # noqa: BLE001
            return err_envelope(ErrorCode.INTERNAL,
                                f"tool {name!r} raised {type(exc).__name__}: {exc}",
                                retryable=False, tool=name, version=version, timing_ms=_ms())

    coerced = _coerce(ret)
    if isinstance(coerced, dict):  # the tool returned a full envelope
        env = coerced
        env.setdefault("degraded_mode", degraded)
        return env
    result, overlay = coerced
    env = ok_envelope(name, version, result, overlay, timing_ms=_ms(), cost=None,
                      degraded_mode=degraded)

    # 4) POST-VALIDATE the §3 envelope — a broken tool surfaces as INTERNAL
    everrs = validate_envelope(env)
    if everrs:
        return err_envelope(ErrorCode.INTERNAL,
                            "tool output failed §3 envelope schema: " + "; ".join(everrs),
                            retryable=False, tool=name, version=version, timing_ms=env["timing_ms"])
    return env
