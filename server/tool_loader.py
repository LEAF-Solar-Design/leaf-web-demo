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
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from functools import lru_cache
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Optional, Tuple, Union

from envelopes import ErrorCode, err_envelope, ok_envelope
from tool_validate import validate_envelope, validate_params

SERVER_DIR = Path(__file__).resolve().parent
AUTHORED_DIR = SERVER_DIR / "authored"
BUILTIN_DIR = SERVER_DIR / "builtins"
PLATFORM_PACKAGE_REGISTRY = SERVER_DIR.parent / "engine" / "registry.json"

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
    "string_panels": "string_panels.py",
}

_MOD_CACHE: Dict[str, Any] = {}


@lru_cache(maxsize=1)
def _platform_builtin_package_ids() -> frozenset[Tuple[str, str, str]]:
    """Execution identities owned by the immutable platform package registry.

    A builtin-looking path is not provenance. Tenant and seed registries may
    legitimately refer to ``server/builtins`` files, so production trust also
    requires the package name and operation to come from the platform-owned
    engine registry shipped in the image. Invalid or absent registry state
    fails closed.
    """
    try:
        raw = json.loads(PLATFORM_PACKAGE_REGISTRY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return frozenset()
    packages = raw.get("tools") if isinstance(raw, dict) else None
    if not isinstance(packages, list):
        return frozenset()

    trusted = set()
    for package in packages:
        if not isinstance(package, dict):
            continue
        name = package.get("name")
        engine_op = package.get("engine_op")
        filename = BUILTIN_OPS.get(engine_op)
        if isinstance(name, str) and isinstance(engine_op, str) and filename:
            trusted.add((name, engine_op, filename))
    return frozenset(trusted)


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


def _sandbox_tier() -> str:
    """Tri-state sandbox tier from LEAF_SANDBOX (read at CALL time, case-insensitive):
    ``e2b`` -> "subprocess" (the shipped v1 tier, meaning unchanged), ``e2b-microvm`` ->
    "microvm" (real E2B micro-VM via the Node helper -- `_run_in_sandbox_e2b`), anything
    else -> "off" (in-process, byte-identical to today). `_sandbox_enabled()` above keeps
    its exact v1 contract for its direct callers/tests."""
    provider = os.environ.get("LEAF_TOOL_SANDBOX_PROVIDER")
    if provider is not None:
        val = provider.strip().lower()
        if not val or val == "off":
            return "off"
        if val == "e2b":
            return "microvm"
        return "invalid"
    val = os.environ.get("LEAF_SANDBOX", "").strip().lower()
    if val == "e2b":
        return "subprocess"
    if val == "e2b-microvm":
        return "microvm"
    return "off"


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


# --------------------------------------------------------------------------- #
# e2b-microvm tier (opt-in v2): real E2B micro-VM via the PROVEN Node substrate.
# The broker shells out to harness/scripts/e2b-tool-exec.mjs (stdin job -> stdout
# result; schemas leaf.e2b.tool-exec-{job,result}.v1). The helper reuses the exact
# egress-locked Sandbox.create config proven in harness/scripts/e2b-vendor-eval.mjs
# (allowOut broker-host-only, denyOut 0.0.0.0/0, allowPublicTraffic false) and
# REFUSES to relay tool output when the egress receipt fails. `_SANDBOX_RUNNER`
# rides along as runner_py, so the PEP-578 jail applies INSIDE the VM too
# (defense-in-depth) and this file stays the single source of truth for the
# in-sandbox program. Fail-closed: node missing / key missing / boot / receipt /
# timeout => infra_error (INTERNAL). An explicitly selected security tier never
# silently downgrades to a weaker one.
# --------------------------------------------------------------------------- #

# Denied-egress probe targets the helper verifies from INSIDE the VM on every run.
# The helper also adds the configured broker host to this list for no-egress tool jobs.
_MICROVM_DENIED_TARGETS = [
    "http://127.0.0.1:8130/",
    "http://10.0.0.1/",
    "http://172.16.0.1/",
    "http://192.168.0.1/",
    "https://example.com/", "https://api.github.com/", "https://1.1.1.1/",
]
_MICROVM_METADATA_TARGET = "http://169.254.169.254/latest/meta-data/"
_SANDBOX_POLICY_VERSION = "leaf.sandbox-policy.v1"


class _MicrovmSuccess:
    """Trusted return value plus the verified, secret-free execution receipt."""

    def __init__(self, ret: Any, provenance: Dict[str, Any]) -> None:
        self.ret = ret
        self.provenance = provenance
_SANDBOX_TEMPLATE_VERSION = "leaf-python-2026-07-29-v2"
_SANDBOX_TEMPLATE_ID = "r0kto3ypd1sgylx4tkz4"
_SANDBOX_TEMPLATE_BUILD_ID = "273367ae-6a5b-47da-ba46-7782c2fa5d6b"
_SANDBOX_LIMITS = {
    "cpu_seconds": 30,
    "memory_bytes": 512 * 1024 * 1024,
    "processes": 32,
    "disk_bytes": 128 * 1024 * 1024,
    "files": 128,
    "source_bytes": 512 * 1024,
    "input_bytes": 8 * 1024 * 1024,
    "params_bytes": 256 * 1024,
    "output_bytes": 1024 * 1024,
    "wall_seconds": 45,
}


def canonical_json_bytes(value: Any) -> bytes:
    """Canonical UTF-8 JSON used by both launchers for evidence hashes."""
    def encode(item: Any) -> str:
        if item is None:
            return "null"
        if isinstance(item, bool):
            return "true" if item else "false"
        if isinstance(item, str):
            return json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        if isinstance(item, (int, float)):
            number = float(item)
            if not math.isfinite(number):
                raise ValueError("canonical JSON rejects non-finite numbers")
            if number == 0:
                number = 0.0
            mantissa, exponent = format(number, ".16e").split("e")
            return f"{mantissa}e{int(exponent):+d}"
        if isinstance(item, list):
            return "[" + ",".join(encode(entry) for entry in item) + "]"
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise TypeError("canonical JSON object keys must be strings")
            keys = sorted(item, key=lambda key: key.encode("utf-8"))
            return "{" + ",".join(
                f"{json.dumps(key, ensure_ascii=False)}:{encode(item[key])}"
                for key in keys
            ) + "}"
        raise TypeError(f"canonical JSON rejects {type(item).__name__}")

    return encode(value).encode("utf-8")


def _microvm_boot_budget_s() -> float:
    """Extra outer-timeout budget for VM boot + upload (env LEAF_SANDBOX_MICROVM_BOOT_BUDGET_S)."""
    try:
        return float(os.environ.get("LEAF_SANDBOX_MICROVM_BOOT_BUDGET_S", "60"))
    except (TypeError, ValueError):
        return 60.0


def _microvm_probe_budget_s() -> float:
    """Outer-timeout budget reserved for the helper's in-VM egress proof."""
    try:
        return float(os.environ.get("LEAF_SANDBOX_MICROVM_PROBE_BUDGET_S", "45"))
    except (TypeError, ValueError):
        return 45.0


def _microvm_env() -> Dict[str, str]:
    """The Node helper's env: the SAME default-deny allowlist as the subprocess tier PLUS the
    E2B key vars -- the helper is the trusted LAUNCHER (it needs the key to boot the VM); the
    key stops there and is never written into the VM (the helper passes no `envs` to the VM).
    No APS/broker secret is ever included."""
    env = _sandbox_env()
    for k in ("E2B_API_KEY", "E2B_API_KEY_FILE"):
        if k in os.environ:
            env[k] = os.environ[k]
    return env


def _microvm_helper_path() -> Path:
    """Helper script path: LEAF_E2B_HELPER override (deploy bakes /app/harness/scripts/...)
    else the in-repo default beside the proven vendor eval."""
    override = os.environ.get("LEAF_E2B_HELPER", "").strip()
    if override:
        return Path(override)
    return SERVER_DIR.parent / "harness" / "scripts" / "e2b-tool-exec.mjs"


def _microvm_cmd() -> Optional[List[str]]:
    """argv for the Node helper, or None when node is not resolvable. THE monkeypatch seam
    for hermetic tests (substitute a Python fake helper speaking the result schema)."""
    node = shutil.which("node")
    if not node:
        return None
    return [node, str(_microvm_helper_path())]


# The in-sandbox program. Standard-library only. Reads the job from stdin, installs the guard,
# runs run(intake, params), and prints a JSON-tagged return. NEVER receives the broker env.
_SANDBOX_RUNNER = r'''
import sys, json, os

_WIRE_OUT = sys.stdout
_OUTPUT_LIMIT = 1048576

def _emit(obj):
    encoded = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _OUTPUT_LIMIT:
        encoded = json.dumps({
            "error": {
                "type": "SandboxOutputLimit",
                "msg": "sandbox output exceeded 1048576 bytes",
            }
        }, separators=(",", ":")).encode("utf-8")
    _WIRE_OUT.buffer.write(encoded)
    _WIRE_OUT.buffer.flush()

class _BoundedText:
    def __init__(self):
        self.count = 0
    def write(self, value):
        size = len(str(value).encode("utf-8", "replace"))
        self.count += size
        if self.count > _OUTPUT_LIMIT:
            raise RuntimeError("sandbox output exceeded 1048576 bytes")
        return len(value)
    def flush(self):
        return None

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
    if job.get("require_limits"):
        try:
            import resource

            def _cap_limit(kind, requested):
                soft, hard = resource.getrlimit(kind)
                bounds = [requested]
                if soft != resource.RLIM_INFINITY:
                    bounds.append(soft)
                if hard != resource.RLIM_INFINITY:
                    bounds.append(hard)
                target = min(bounds)
                resource.setrlimit(kind, (target, target))

            _cap_limit(resource.RLIMIT_CPU, 30)
            _cap_limit(resource.RLIMIT_AS, 536870912)
            _cap_limit(resource.RLIMIT_NPROC, 32)
            _cap_limit(resource.RLIMIT_FSIZE, 134217728)
            _cap_limit(resource.RLIMIT_NOFILE, 128)
        except (ImportError, ValueError, OSError) as exc:
            _emit({"error": {"type": "SandboxLimits", "msg": type(exc).__name__}})
            return

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
            mode = str(args[1]) if len(args) > 1 else "r"
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                raise PermissionError("sandbox: filesystem write denied")
            if path is not None and not isinstance(path, int) and _sensitive(path):
                raise PermissionError("sandbox: read of sensitive path denied")
        elif event in _NET:
            raise PermissionError("sandbox: network egress denied")
        elif event in _PROC:
            raise PermissionError("sandbox: subprocess/exec denied")

    sys.addaudithook(_hook)
    sys.stdout = _BoundedText()
    sys.stderr = _BoundedText()
    sys.__stdout__ = sys.stdout
    sys.__stderr__ = sys.stderr
    def _deny_os_write(*_args, **_kwargs):
        raise PermissionError("sandbox: raw descriptor output denied")
    os.write = _deny_os_write

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

_SANDBOX_WRAPPER = r'''
import json, os, subprocess, sys, threading

_LIMIT = 1048576
_CHILD = __CHILD_RUNNER__

def _fixed_error():
    payload = {
        "error": {
            "type": "SandboxOutputLimit",
            "msg": "sandbox output exceeded 1048576 bytes",
        }
    }
    sys.stdout.buffer.write(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sys.stdout.buffer.flush()

def _child_limits():
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        bounds = [_LIMIT]
        if soft != resource.RLIM_INFINITY:
            bounds.append(soft)
        if hard != resource.RLIM_INFINITY:
            bounds.append(hard)
        target = min(bounds)
        resource.setrlimit(resource.RLIMIT_FSIZE, (target, target))
    except ImportError:
        pass

def main():
    job = sys.stdin.buffer.read()
    try:
        require_network_namespace = (
            json.loads(job.decode("utf-8")).get("require_network_namespace") is True
        )
    except Exception:
        require_network_namespace = False
    stdout_path = os.path.abspath(".leaf-sandbox-stdout")
    stderr_path = os.path.abspath(".leaf-sandbox-stderr")
    preexec = _child_limits if os.name == "posix" else None
    child_cmd = [sys.executable, "-I", "-B", "-c", _CHILD]
    if os.name == "posix" and require_network_namespace:
        # The tenant process gets a fresh user and network namespace. It has no
        # interfaces or routes, including to E2B's link-local controller.
        child_cmd = ["unshare", "-Urn", "--", *child_cmd]
    with open(stdout_path, "w+b", buffering=0) as child_out, \
         open(stderr_path, "w+b", buffering=0) as child_err:
        proc = subprocess.Popen(
            child_cmd,
            stdin=subprocess.PIPE,
            stdout=child_out,
            stderr=child_err,
            preexec_fn=preexec,
        )
        breached = threading.Event()
        stopped = threading.Event()

        def monitor():
            while not stopped.wait(0.002):
                try:
                    if (os.path.getsize(stdout_path) > _LIMIT or
                            os.path.getsize(stderr_path) > _LIMIT):
                        breached.set()
                        proc.kill()
                        return
                except OSError:
                    return

        watcher = threading.Thread(target=monitor, daemon=True)
        watcher.start()
        proc.communicate(input=job)
        stopped.set()
        watcher.join(timeout=0.1)
        stdout_size = os.path.getsize(stdout_path)
        stderr_size = os.path.getsize(stderr_path)
        if (breached.is_set() or proc.returncode != 0 or
                stdout_size > _LIMIT or stderr_size > _LIMIT):
            _fixed_error()
            return
        child_out.seek(0)
        remaining = _LIMIT + 1
        chunks = []
        while remaining:
            chunk = child_out.read(min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > _LIMIT:
            _fixed_error()
            return
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()

main()
'''.replace("__CHILD_RUNNER__", repr(_SANDBOX_RUNNER))


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
    return _run_source_in_sandbox(source, local.name, intake, params)


def _run_source_in_sandbox(source: str, filename: str, intake: Dict[str, Any],
                           params: Dict[str, Any]) -> Tuple[str, Any]:
    """Execute already-validated source in the locked-down subprocess sandbox.

    This is the design-time staged-source seam. It never loads the source in the
    credential-holding broker process and applies the same fixed source bound as
    the E2B tier.
    """
    if len(source.encode("utf-8")) > _SANDBOX_LIMITS["source_bytes"]:
        return ("infra_error", "tool source exceeds fixed sandbox limit")
    job = json.dumps({"source": source, "intake": intake, "params": params,
                      "filename": filename}).encode("utf-8")
    timeout = _sandbox_timeout_s()
    try:
        with tempfile.TemporaryDirectory(prefix="leaf-sbx-") as jail:
            proc = subprocess.run(
                [sys.executable, "-I", "-B", "-c", _SANDBOX_WRAPPER],
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


def _run_in_sandbox_e2b(local: Path, intake: Dict[str, Any],
                        params: Dict[str, Any],
                        tenant_id: Optional[str] = None) -> Tuple[str, Any]:
    """Execute the tenant tool file inside a REAL egress-locked E2B micro-VM (tier v2).

    Same trust shape as `_run_in_sandbox`: the source is read HERE (trusted broker; ``local``
    is already 1F path-safety-resolved); source+intake+params go to the Node helper over ONE
    stdin JSON channel; the helper uploads them into the VM via files.write (the ~0.5MB+
    intake never touches an argv), runs `_SANDBOX_RUNNER` (audit-hook jail included) via
    stdin redirect, verifies the broker-only egress receipt (REFUSING to relay on failure),
    and relays the runner's JSON verbatim. Same return contract as `_run_in_sandbox`."""
    try:
        source = local.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return ("infra_error", f"could not read tool source {local.name}: {type(exc).__name__}: {exc}")
    return _run_source_in_sandbox_e2b(
        source, local.name, intake, params, tenant_id=tenant_id)


def _run_source_in_sandbox_e2b(source: str, filename: str,
                               intake: Dict[str, Any], params: Dict[str, Any],
                               tenant_id: Optional[str] = None) -> Tuple[str, Any]:
    """Execute already-validated source in the broker-owned E2B micro-VM."""
    cmd = _microvm_cmd()
    if cmd is None:
        return ("infra_error", "node executable not found; e2b-microvm tier unavailable")
    source_bytes = len(source.encode("utf-8"))
    intake_bytes = len(json.dumps(intake, separators=(",", ":")).encode("utf-8"))
    params_bytes = len(json.dumps(params, separators=(",", ":")).encode("utf-8"))
    if source_bytes > _SANDBOX_LIMITS["source_bytes"]:
        return ("infra_error", "tool source exceeds fixed sandbox limit")
    if intake_bytes > _SANDBOX_LIMITS["input_bytes"]:
        return ("infra_error", "tool intake exceeds fixed sandbox limit")
    if params_bytes > _SANDBOX_LIMITS["params_bytes"]:
        return ("infra_error", "tool params exceed fixed sandbox limit")
    timeout = min(_sandbox_timeout_s(), float(_SANDBOX_LIMITS["wall_seconds"]))
    broker_host = (os.environ.get("LEAF_SANDBOX_BROKER_HOST", "").strip()
                   or "httpbingo.org")
    sandbox_job = {"source": source, "intake": intake, "params": params,
                   "filename": filename, "require_limits": True,
                   "require_network_namespace": True}
    blob = json.dumps({
        "schema": "leaf.e2b.tool-exec-job.v1",
        "job": sandbox_job,
        "runner_py": _SANDBOX_WRAPPER,
        "timeout_s": timeout,
        "broker_host": broker_host,
        "denied_targets": _MICROVM_DENIED_TARGETS,
        "platform_metadata_target": _MICROVM_METADATA_TARGET,
        "probe_broker": False,
        "audit": {
            "tenant_hash": hashlib.sha256(
                str(tenant_id or "demo-tenant").encode("utf-8")).hexdigest(),
            "source_hash": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "input_hash": hashlib.sha256(json.dumps(
                {"intake": intake, "params": params}, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest(),
            "job_hash": hashlib.sha256(canonical_json_bytes(sandbox_job)).hexdigest(),
            "template_version": _SANDBOX_TEMPLATE_VERSION,
            "template_id": _SANDBOX_TEMPLATE_ID,
            "template_build_id": _SANDBOX_TEMPLATE_BUILD_ID,
            "policy_version": _SANDBOX_POLICY_VERSION,
            "limits": _SANDBOX_LIMITS,
        },
    }).encode("utf-8")
    outer = timeout + _microvm_probe_budget_s() + _microvm_boot_budget_s()
    try:
        with tempfile.TemporaryDirectory(prefix="leaf-mvm-") as jail:
            proc = subprocess.run(
                cmd,
                input=blob,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_microvm_env(),
                cwd=jail,
                timeout=outer,
            )
    except subprocess.TimeoutExpired:
        return ("infra_error", f"e2b-microvm helper timed out after {outer}s")
    except Exception as exc:  # noqa: BLE001
        return ("infra_error", f"e2b-microvm helper failed to start: {type(exc).__name__}: {exc}")
    out = (proc.stdout or b"").decode("utf-8", "replace").strip()
    if len(proc.stdout or b"") > _SANDBOX_LIMITS["output_bytes"]:
        return ("infra_error", "e2b-microvm helper output exceeded fixed limit")
    if not out:
        return ("infra_error", f"e2b-microvm helper produced no output (rc={proc.returncode})")
    try:
        parsed = json.loads(out)
    except Exception as exc:  # noqa: BLE001
        return ("infra_error", f"e2b-microvm helper output was not JSON: {type(exc).__name__}: {exc}")
    herr = parsed.get("helper_error")
    if herr:
        return ("infra_error",
                f"e2b-microvm {herr.get('stage', '?')} failure: "
                f"{herr.get('type', 'Error')}: {herr.get('msg', '')}")
    receipt = parsed.get("receipt") or {}
    if not receipt.get("passed"):
        # The security boundary: an unproven egress lock means the sandbox result is refused
        # by the HELPER (result:null) -- and re-refused here even if a result slipped through.
        return ("infra_error", "e2b-microvm egress receipt not passed; sandbox result refused")
    audit = json.loads(blob.decode("utf-8"))["audit"]
    if audit:
        required = (
            "tenantHash", "sourceHash", "inputHash", "jobHash",
            "templateVersion", "templateId", "templateBuildId", "policyVersion",
            "startedAt", "stoppedAt", "resourceUse", "resultHash",
            "configuredDenyAll", "configuredBrokerOnly",
            "configuredNoPublicTraffic", "configuredTemplate",
            "everyDeniedProbeBlocked",
            "deniedProbes", "brokerReached", "boundary", "network",
            "platformMetadata", "platformMetadataSafe",
            "tenantNetworkNamespace", "tenantNetworkNamespaceIsolated",
        )
        if any(key not in receipt for key in required):
            return ("infra_error", "e2b-microvm audit receipt incomplete; result refused")
        network = receipt.get("network")
        denied_probes = receipt.get("deniedProbes")
        expected_denied_targets = set(_MICROVM_DENIED_TARGETS)
        expected_denied_targets.add(f"https://{broker_host}/")
        platform_metadata = receipt.get("platformMetadata")
        expected_metadata_attempts = {
            "no_auth_get", "aws_token_put", "aws_invalid_token_get",
            "gcp_flavor_get", "e2b_invalid_access_token_get",
            "invalid_bearer_get",
        }
        metadata_attempts = (
            platform_metadata.get("attempts")
            if isinstance(platform_metadata, dict) else None
        )
        tenant_network_namespace = receipt.get("tenantNetworkNamespace")
        namespace_probes = (
            tenant_network_namespace.get("blocked")
            if isinstance(tenant_network_namespace, dict) else None
        )
        expected_namespace_targets = {
            *expected_denied_targets, _MICROVM_METADATA_TARGET,
        }
        no_egress_proven = (
            receipt.get("configuredDenyAll") is True
            and receipt.get("configuredTemplate") is True
            and receipt.get("configuredBrokerOnly") is True
            and receipt.get("configuredNoPublicTraffic") is True
            and receipt.get("everyDeniedProbeBlocked") is True
            and isinstance(denied_probes, dict)
            and set(denied_probes) == expected_denied_targets
            and all(isinstance(probe, dict) and probe.get("blocked") is True
                    for probe in denied_probes.values())
            and receipt.get("platformMetadataSafe") is True
            and receipt.get("tenantNetworkNamespaceIsolated") is True
            and isinstance(platform_metadata, dict)
            and platform_metadata.get("target") == _MICROVM_METADATA_TARGET
            and platform_metadata.get("credential_material_present") is False
            and isinstance(metadata_attempts, dict)
            and set(metadata_attempts) == expected_metadata_attempts
            and all(
                isinstance(attempt, dict)
                and (
                    attempt.get("blocked") is True
                    or (
                        attempt.get("blocked") is False
                        and isinstance(attempt.get("status"), int)
                    )
                )
                for attempt in metadata_attempts.values()
            )
            and isinstance(tenant_network_namespace, dict)
            and tenant_network_namespace.get("command_ok") is True
            and isinstance(namespace_probes, dict)
            and set(namespace_probes) == expected_namespace_targets
            and all(
                isinstance(probe, dict) and probe.get("blocked") is True
                for probe in namespace_probes.values()
            )
            and receipt.get("brokerReached") is None
            and receipt.get("boundary") == "tool"
            and isinstance(network, dict)
            and network.get("allowOut") in (None, [])
            and network.get("allowPublicTraffic") is False
            and "0.0.0.0/0" in (network.get("denyOut") or [])
        )
        if not no_egress_proven:
            return ("infra_error", "e2b-microvm no-egress receipt incomplete; result refused")
        if (receipt.get("tenantHash") != audit["tenant_hash"]
                or receipt.get("sourceHash") != audit["source_hash"]
                or receipt.get("inputHash") != audit["input_hash"]
                or receipt.get("jobHash") != audit["job_hash"]
                or receipt.get("templateVersion") != audit["template_version"]
                or receipt.get("templateId") != audit["template_id"]
                or receipt.get("templateBuildId") != audit["template_build_id"]
                or receipt.get("policyVersion") != audit["policy_version"]):
            return ("infra_error", "e2b-microvm audit receipt mismatch; result refused")
    result = parsed.get("result")
    if not isinstance(result, dict):
        return ("infra_error", "e2b-microvm helper returned no result")
    computed_result_hash = hashlib.sha256(canonical_json_bytes(result)).hexdigest()
    if receipt.get("resultHash") != computed_result_hash:
        return ("infra_error", "e2b-microvm result hash mismatch; result refused")
    if result.get("error"):
        e = result["error"]
        return ("tool_error", f"{e.get('type', 'Error')}: {e.get('msg', '')}")
    if not result.get("ok"):
        return ("infra_error", "e2b-microvm runner returned a non-ok result without an error")
    audit = json.loads(blob.decode("utf-8"))["audit"]
    provenance = {
        "contract": "leaf.tool-execution.v1",
        "provider": "e2b",
        "isolation": "microvm",
        "passed": True,
        "tenant_hash": receipt.get("tenantHash"),
        "source_sha256": receipt.get("sourceHash"),
        "input_sha256": audit["input_hash"],
        "result_sha256": receipt.get("resultHash"),
        "template_version": receipt.get("templateVersion"),
        "template_id": receipt.get("templateId"),
        "template_build_id": receipt.get("templateBuildId"),
        "policy_version": receipt.get("policyVersion"),
        "started_at": receipt.get("startedAt"),
        "stopped_at": receipt.get("stoppedAt"),
        "resource_use": receipt.get("resourceUse"),
    }
    return ("ok", _MicrovmSuccess(
        _decode_ret(result.get("ret") or {}),
        provenance,
    ))


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
    # Body = the vendored mushy-fold core (mushy-code extraction, 2026-08-06):
    # the F4 rule moved to the library; this wrapper keeps the name and docstring
    # every in-repo caller and test imports.
    from _vendor.mushy_fold.entry import is_unsafe_ref
    return is_unsafe_ref(ref)


def _resolve_within(root: Path, rel: str) -> Optional[Path]:
    """Join repo-relative ``rel`` onto an allowed ``root`` and return the resolved ``.py``
    file ONLY if it stays INSIDE ``root`` (symlink-safe) and is a regular file. Returns
    None otherwise — a second, belt-and-suspenders containment gate behind
    ``_is_unsafe_ref`` (defeats a symlink inside the repo that points back out, and any
    residual traversal that slipped a per-OS parse)."""
    # Body = the vendored mushy-fold core (mushy-code extraction, 2026-08-06),
    # same wrapper rationale as _is_unsafe_ref above.
    from _vendor.mushy_fold.entry import resolve_within
    return resolve_within(root, rel)


def _declares_local_python(tool: Dict[str, Any]) -> bool:
    """True if this package CLAIMS a local Python body, via ``entry`` or ``script``.

    The one place that decides what "declared a local implementation" means, so
    ``resolve_local_file`` (which must not substitute a different file for a missing
    one) and ``is_trusted_builtin_tool`` (which must not call a dangling package
    trusted) cannot drift apart. Case-insensitive: a ``.PY`` declaration is still a
    Python declaration, and on Windows it names the same file as ``.py``.
    """
    for declared in (tool.get("entry"), tool.get("script")):
        if isinstance(declared, str) and declared.strip().lower().endswith(".py"):
            return True
    return False


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
        # A DECLARED LOCAL PYTHON BODY RESOLVES EXACTLY, OR THE TOOL IS A MISS.
        #
        # Past this block the function retries the entry's BASENAME, then probes
        # `script`, then falls back on the BUILTIN_OPS compat table. For a package that
        # NAMED a local .py body, every one of those is a silent SUBSTITUTION when the
        # named file is gone, and it runs code the caller never asked for:
        #   * a tenant-scoped ``authored/<sha256(tenant_id)[:32]>/<name>.py`` whose body
        #     was missing (never re-authored after the pre-#131 upgrade, or a wiped
        #     volume) landed on a LEGACY FLAT ``authored/<name>.py`` that a DIFFERENT
        #     tenant wrote, and ran it under this tenant's identity, re-opening the exact
        #     cross-tenant hole the per-tenant layout closed;
        #   * a tenant-repo ``tools/<name>/tool.py`` with a lost body did the same; and
        #   * failing those, BUILTIN_OPS[engine_op] ran the PRE-CODED PRIMITIVE (an
        #     authored tool carries the engine_op it was templated from) and reported a
        #     result the tool's own code never produced. That is the re-dispatch this
        #     loader exists to prevent ("the FILE is the tool"), and BUILTIN_OPS is
        #     already documented as never serving authored tools.
        # So the rule keys on the DECLARATION, not on the shape of the path: any package
        # that claims a local .py body and cannot produce it returns None (=> APS path or
        # a visible failure). It is deliberately not special-cased to ``authored/`` or to
        # directory-bearing entries, since a bare ``<name>.py``, an entry ``_is_unsafe_ref``
        # rejected, and a missing ``script`` all substituted the same way.
        # A package that never claimed local Python (an APS ``engine_script``, an
        # appbundle, an opaque entry such as "solve_targets") is untouched: it keeps the
        # historical BUILTIN_OPS path because nothing of its own runs in this process.
        if len(PurePosixPath(entry.replace("\\", "/")).parts) == 1:
            roots.append((AUTHORED_DIR, name))  # authored/<name>
            roots.append((BUILTIN_DIR, name))   # builtins/<name>
        for root, rel in roots:
            hit = _resolve_within(root, rel)
            if hit is not None:
                return hit
    # A DECLARED `.py` ENTRY IS THE TOOL: if it did not resolve above, stop here rather
    # than letting `script` (or BUILTIN_OPS below) supply a stand-in. Probing on would be
    # the same substitution one level down, and `script` is tenant-controlled in a
    # tenant-repo registry, so it could name a body this package does not own.
    if isinstance(entry, str) and entry.strip().lower().endswith(".py"):
        return None
    script = tool.get("script")
    if script and str(script).lower().endswith(".py") and not _is_unsafe_ref(script):
        hit = _resolve_within(SERVER_DIR, script)
        if hit is not None:
            return hit
    if _declares_local_python(tool):
        return None   # named a local body and could not produce it: an honest miss
    op = tool.get("engine_op") or ""
    fname = BUILTIN_OPS.get(op)
    if fname:
        cand = BUILTIN_DIR / fname
        if cand.exists():
            return cand
    return None


def is_trusted_builtin_tool(tool: Dict[str, Any],
                            tenant_id: Optional[str] = None) -> bool:
    """Return whether this run cannot load tenant-controlled Python in the broker.

    A tool with no local Python implementation uses APS or fails normally, so it
    does not execute a file in this process. A local implementation is trusted only
    when resolution lands inside ``server/builtins`` AND its execution identity is
    declared by the platform-owned engine registry. Path location alone is not
    package provenance: agent-authored or seed packages can point at that directory
    without becoming platform builtins. Resolution checks the requesting tenant
    first, so a tenant file that shadows a builtin-looking path remains untrusted.
    """
    local = resolve_local_file(tool, tenant_id)
    if local is None:
        # "No local file" is only trustworthy when the package never CLAIMED one. A
        # package that declares a local Python implementation which does not resolve is
        # DANGLING, not APS-only, and must stay untrusted: before entry resolution became
        # exact-or-miss, such a package silently resolved onto a builtin and was
        # classified untrusted there (its identity is absent from the platform registry).
        # Returning True here instead would hand it the opposite classification and let
        # it past broker.py's production containment gate, which denies only UNtrusted
        # tools, so a dangling tenant package carrying drawing.write could reach the live
        # write Activity. A declaration that is not local Python at all (an APS
        # `engine_script`, an appbundle, an opaque `entry` like "solve_targets") never
        # executes a file in this process and stays trusted, as before.
        return not _declares_local_python(tool)
    try:
        local.resolve().relative_to(BUILTIN_DIR.resolve())
    except (OSError, ValueError):
        return False
    name = tool.get("name")
    engine_op = tool.get("engine_op")
    if not isinstance(name, str) or not isinstance(engine_op, str):
        return False
    return (name, engine_op, local.name) in _platform_builtin_package_ids()


def _needs_aps(tool: Dict[str, Any], tenant_id: Optional[str] = None) -> bool:
    """True when the tool has no local .py and must run on APS DA."""
    return resolve_local_file(tool, tenant_id) is None


def _test_source_filename(tool: Dict[str, Any]) -> Optional[str]:
    """Return the safe package filename for a staged design-time source."""
    entry = (tool or {}).get("entry")
    if not isinstance(entry, str) or _is_unsafe_ref(entry):
        return None
    filename = Path(entry).name
    if not filename or Path(filename).suffix.lower() != ".py":
        return None
    return filename


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
                     tenant_id: Optional[str] = None,
                     test_source: Optional[str] = None) -> Dict[str, Any]:
    """Execute the tool the registry entry references. Returns an extended §3 envelope.

    ``tenant_id`` scopes entry resolution to the requesting tenant's repo (wave 4). A
    None tenant_id keeps the legacy demo-tenant behaviour (honours $LEAF_TENANT_REPO).

    ``test_source`` is the exact trusted validate_tool output for a design-time
    staged test. It is accepted only for non-live execution inside a configured
    sandbox and never enters the broker's in-process module loader.
    """
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

    test_filename = None
    if test_source is not None:
        if aps_live:
            return err_envelope(
                ErrorCode.BAD_PARAMS,
                "staged test source is forbidden for APS_LIVE=1",
                retryable=False, tool=name, version=version, timing_ms=_ms())
        test_filename = _test_source_filename(tool)
        if test_filename is None:
            return err_envelope(
                ErrorCode.BAD_PARAMS,
                "staged test source requires a safe repo-relative Python entry",
                retryable=False, tool=name, version=version, timing_ms=_ms())
        if len(test_source.encode("utf-8")) > _SANDBOX_LIMITS["source_bytes"]:
            return err_envelope(
                ErrorCode.BAD_PARAMS,
                "staged test source exceeds fixed sandbox limit",
                retryable=False, tool=name, version=version, timing_ms=_ms())
        local = None
    else:
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
    if local is None and test_source is None:
        return err_envelope(
            ErrorCode.BAD_PARAMS,
            f"no local implementation resolvable for tool {name!r} "
            f"(engine_op={tool.get('engine_op')!r}) at APS_LIVE=0",
            retryable=False, tool=name, version=version, timing_ms=_ms())
    # F2 / lane 2B: when a sandbox tier is ENABLED (LEAF_SANDBOX=e2b -> subprocess tier;
    # LEAF_SANDBOX=e2b-microvm -> real E2B micro-VM tier), the tenant tool BODY runs OUT of
    # this credential-holding process — no `_load_module`/`exec_module` of tenant code in
    # this PID on either sandbox path. DEFAULT (unset) => the in-process `else` below runs
    # and is BYTE-IDENTICAL to today, so every in-process gate suite is unaffected.
    tier = _sandbox_tier()
    if tier == "invalid":
        return err_envelope(
            ErrorCode.INTERNAL,
            "unsupported LEAF_TOOL_SANDBOX_PROVIDER; execution refused",
            retryable=False, tool=name, version=version, timing_ms=_ms())
    if test_source is not None and tier == "off":
        return err_envelope(
            ErrorCode.TENANT_DISABLED,
            "staged test source requires configured sandbox execution",
            retryable=False, tool=name, version=version, timing_ms=_ms())
    execution_provenance = None
    if tier != "off":
        if test_source is not None:
            assert test_filename is not None
            if tier == "microvm":
                kind, payload = _run_source_in_sandbox_e2b(
                    test_source, test_filename, intake, params,
                    tenant_id=tenant_id)
            else:
                kind, payload = _run_source_in_sandbox(
                    test_source, test_filename, intake, params)
        elif tier == "microvm":
            assert local is not None
            kind, payload = _run_in_sandbox_e2b(
                local, intake, params, tenant_id=tenant_id)
        else:
            assert local is not None
            kind, payload = _run_in_sandbox(local, intake, params)
        if kind == "tool_error":
            return err_envelope(ErrorCode.INTERNAL,
                                f"tool {name!r} raised {payload}",
                                retryable=False, tool=name, version=version, timing_ms=_ms())
        if kind == "infra_error":
            return err_envelope(ErrorCode.INTERNAL,
                                f"sandbox execution failed for tool {name!r}: {payload}",
                                retryable=False, tool=name, version=version, timing_ms=_ms())
        if isinstance(payload, _MicrovmSuccess):
            ret = payload.ret
            execution_provenance = payload.provenance
        else:
            ret = payload
    else:
        assert local is not None
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
        if execution_provenance is not None:
            env["execution_provenance"] = execution_provenance
        return env
    result, overlay = coerced
    env = ok_envelope(name, version, result, overlay, timing_ms=_ms(), cost=None,
                      degraded_mode=degraded)
    if execution_provenance is not None:
        env["execution_provenance"] = execution_provenance

    # 4) POST-VALIDATE the §3 envelope — a broken tool surfaces as INTERNAL
    everrs = validate_envelope(env)
    if everrs:
        return err_envelope(ErrorCode.INTERNAL,
                            "tool output failed §3 envelope schema: " + "; ".join(everrs),
                            retryable=False, tool=name, version=version, timing_ms=env["timing_ms"])
    return env
