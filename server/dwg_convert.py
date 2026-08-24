"""
Local DWG -> ASCII DXF conversion (the APS-free guest DWG read lane).

Runs GNU libredwg's ``dwg2dxf`` as a CAGED SUBPROCESS. Two tiers, kept
distinct because conflating them is what made an earlier version of this
docstring overstate its own guarantees:

  * Bounds MISUSE and DoS: fixed argv (no shell), stdin closed, output confined
    to a fresh scratch directory that is always deleted, a wall-clock timeout
    that kills the child, and a size cap on the converted output.
  * Bounds a MEMORY-CORRUPTION exploit: hard rlimits (address space, CPU, file
    size, core, fds, procs) and ``no_new_privs`` with no inheritable
    capabilities, applied by prlimit(1)/setpriv(1) around the exec. See "the
    cage" below for why it is spelled that way and not as a preexec_fn.

This matters because the bytes are attacker-controlled and UNAUTHENTICATED by
design (guest sandbox front door, CONTRACT-ADDENDUM §19), and the parser is C.
The cage is defense in depth UNDER a current libredwg pin, never a substitute
for it: keep deploy/Dockerfile.app's version current.

The converted DXF feeds the EXISTING dxf_intake parser unchanged — this module
produces a file, never intake, so the honesty rule
(geometry only ever comes from the user's actual bytes) is inherited from the
parser rather than re-implemented here.

Every failure is a structured ConvertError (error_code from the frozen §10
enum, honest message, retryable flag) so the extraction worker can land it in
the upload marker as-is. Malformed or hostile DWG bytes fail CLOSED: a clean
rejection, no crash, no partial intake.

LICENSE NOTE (load-bearing, do not "clean up"): libredwg is GPL-3.0+. It is
invoked strictly as a separate executable via subprocess — never linked,
imported, or bound into this Python process (no ctypes/cffi/bindings). Its
output is the user's own drawing converted to another format, not a derivative
of libredwg. The binary ships only inside the server container image (built
from pinned GNU source in deploy/Dockerfile.app, provenance recorded there)
and is never distributed to end users, so server-side execution is clean under
GPL-3. Keep it that way: subprocess only.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator, Optional


def dwg2dxf_bin() -> Optional[str]:
    """The converter executable, or None when this deployment has none.

    ``LEAF_DWG2DXF_BIN`` (an explicit path — must exist, no PATH search) wins;
    otherwise ``dwg2dxf`` is looked up on PATH. Read at call time so tests and
    subprocess env overrides apply."""
    explicit = os.environ.get("LEAF_DWG2DXF_BIN", "").strip()
    if explicit:
        return explicit if Path(explicit).is_file() else None
    return shutil.which("dwg2dxf")


def available() -> bool:
    """Whether the local DWG engine can run here (policy + routing read this)."""
    return dwg2dxf_bin() is not None


def convert_timeout_s() -> float:
    try:
        return float(os.environ.get("LEAF_DWG_CONVERT_TIMEOUT_S", "120"))
    except ValueError:
        return 120.0


def max_output_bytes() -> int:
    """Cap on the CONVERTED DXF (ASCII DXF inflates well beyond the 25 MB DWG
    upload cap; this bounds what the parser is then asked to read). The child
    can transiently write past it before the post-run check deletes the scratch
    dir — the timeout is what bounds that window."""
    try:
        return int(os.environ.get("LEAF_DWG_CONVERT_MAX_OUTPUT_BYTES",
                                  str(128 * 1024 * 1024)))
    except ValueError:
        return 128 * 1024 * 1024


class ConvertError(Exception):
    """Structured conversion failure — lands in the upload marker verbatim."""

    def __init__(self, error_code: str, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.retryable = retryable


# --------------------------------------------------------------------------- #
# the cage: hard bounds on the NATIVE parser child
# --------------------------------------------------------------------------- #
# WHY THIS EXISTS. dwg2dxf is a C parser with a long OSS-Fuzz history, and the
# bytes it parses arrive from an UNAUTHENTICATED upload by design (guest sandbox
# front door, CONTRACT-ADDENDUM §19). The controls above this line — fixed argv,
# closed stdin, scratch dir, wall-clock timeout, output cap — bound MISUSE and
# DoS. They do not bound a controlled memory-corruption exploit. These do:
# hard rlimits so a hostile DWG cannot groom an unbounded heap, plus
# no_new_privs so a corrupted child can never gain privilege it did not start
# with, plus a seccomp-bpf syscall DENYLIST (below) that removes a corrupted
# child's route to a shell or a callback outright. Defense in depth that
# survives the NEXT parser CVE, not just today's.
#
# NOT preexec_fn. The obvious spelling — resource.setrlimit in a
# subprocess preexec_fn — is documented-unsafe here: this process is a threaded
# ASGI server, and CPython states preexec_fn "is not thread-safe" and "may lead
# to deadlock in the child process before exec is called" (a fork that inherits
# a held malloc lock hangs the conversion worker, and a hung worker is the DoS
# the cage was supposed to prevent). prlimit(1) and setpriv(1) set the same
# limits from OUTSIDE the interpreter and then exec, so there is no fork window
# to deadlock in.
#
# ONE PROCESS, still. prlimit and setpriv each exec their argument rather than
# forking it, so the whole chain collapses into the single child that
# subprocess.run holds — the wall-clock timeout still kills the real parser, and
# no grandchild can outlive it.
#
# Both tools ship in the base image's util-linux (deploy/Dockerfile.app), which
# is why this needs no new dependency.

_CAGE_TOOLS = ("prlimit", "setpriv")

# Default location deploy/Dockerfile.app bakes the compiled syscall denylist
# into (deploy/gen_seccomp_filter.c, run at build time against libseccomp-dev
# and then purged — the file itself has no runtime library dependency). A
# fixed path, not derived from this module's location, so the build step does
# not need /app/server to exist yet when it generates the filter.
_DEFAULT_SECCOMP_FILTER_PATH = "/usr/local/etc/leaf/seccomp-dwg2dxf.bpf"


def _int_env(name: str, default: int) -> int:
    """A non-negative integer from the environment, or ``default``. Never raises:
    a malformed operator value must not take the upload lane down, and the
    default is the SAFE value in every case here."""
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value >= 0 else default


def convert_mem_limit_bytes() -> int:
    """RLIMIT_AS for the parser child — the load-bearing one. Bounds the address
    space a memory-corruption defect can groom. 0 disables ONLY this limit (the
    CPU/FSIZE/CORE/NOFILE/NPROC caps still apply)."""
    return _int_env("LEAF_DWG_CONVERT_MEM_BYTES", 2 * 1024 * 1024 * 1024)


def convert_nofile_limit() -> int:
    """RLIMIT_NOFILE. dwg2dxf opens the input, the output and its libraries; a
    payload that wants sockets wants far more than this."""
    return _int_env("LEAF_DWG_CONVERT_NOFILE", 64)


def convert_nproc_limit() -> int:
    """RLIMIT_NPROC. dwg2dxf never forks, so this costs the honest path nothing
    and denies a payload the fork/exec it needs to become anything else.
    Enforced at fork(), never at exec(), so it cannot break the cage's own
    prlimit -> setpriv -> dwg2dxf exec chain."""
    return _int_env("LEAF_DWG_CONVERT_NPROC", 16)


def cage_required() -> bool:
    """Whether a missing cage is a REFUSAL rather than a downgrade.

    Production sets this (deploy/Dockerfile.app) so the deployed image can never
    silently lose its sandbox and keep parsing hostile bytes bare — that failure
    would be invisible in every green health check. Dev hosts without
    util-linux (macOS, Windows) leave it unset and run uncaged."""
    return os.environ.get("LEAF_DWG_CONVERT_REQUIRE_CAGE", "0") == "1"


def cage_available() -> bool:
    """Whether both cage tools are on PATH. Resolved at call time so tests and
    per-deployment images decide it, not import order."""
    return all(shutil.which(tool) for tool in _CAGE_TOOLS)


def seccomp_filter_path() -> Optional[str]:
    """Path to the compiled dwg2dxf syscall denylist, or None when this
    deployment has none. ``LEAF_DWG_CONVERT_SECCOMP_FILE`` (an explicit path —
    must exist) wins; otherwise the path deploy/Dockerfile.app bakes the
    filter into is used if present. Resolved at call time, same reasoning as
    dwg2dxf_bin(): tests and per-deployment images decide it, not import
    order."""
    explicit = os.environ.get("LEAF_DWG_CONVERT_SECCOMP_FILE", "").strip()
    if explicit:
        return explicit if Path(explicit).is_file() else None
    return (_DEFAULT_SECCOMP_FILTER_PATH
            if Path(_DEFAULT_SECCOMP_FILTER_PATH).is_file() else None)


def _cage_prefix() -> list[str]:
    """The argv prefix that cages the parser, or [] when this host has no cage.

    Fixed tokens and integers only — nothing here is attacker-influenced, and
    there is no shell, so the no-shell property of the argv is preserved."""
    if not cage_available():
        return []
    # FSIZE gets HEADROOM above the output cap, deliberately. Setting it AT the
    # cap makes the kernel kill the writer one byte early, which turns the
    # post-run size check below into dead code and downgrades its precise
    # "exceeds the N byte conversion output cap" rejection into a generic
    # "unreadable" one. With headroom the child can overshoot slightly, the
    # post-run check reports the honest reason, and a RUNAWAY write is still
    # hard-stopped by the kernel a few KiB later instead of filling the disk.
    fsize = max_output_bytes() + 4096
    limits = [
        "prlimit",
        f"--cpu={max(1, int(convert_timeout_s()) + 1)}",
        f"--fsize={fsize}",
        "--core=0",
        f"--nofile={convert_nofile_limit()}",
        f"--nproc={convert_nproc_limit()}",
    ]
    mem = convert_mem_limit_bytes()
    if mem > 0:
        limits.append(f"--as={mem}")
    # --no-new-privs: a corrupted child cannot gain privilege through any
    # setuid/setcap binary it manages to exec. --inh-caps=-all: it inherits no
    # capability either. Both are unprivileged operations, so this works
    # unchanged once the image drops root.
    setpriv_argv = ["setpriv", "--no-new-privs", "--inh-caps=-all"]
    # --seccomp-filter is additive and OPTIONAL here even when the rest of the
    # cage is present: a dev host with prlimit/setpriv but no compiled filter
    # still gets the base cage. Production cannot silently lose JUST this
    # layer, though — see the cage_required() check in converted_dxf(), which
    # refuses to run bare when the filter is missing and the deployment
    # declared itself caged.
    seccomp = seccomp_filter_path()
    if seccomp is not None:
        setpriv_argv.append(f"--seccomp-filter={seccomp}")
    return limits + ["--"] + setpriv_argv + ["--"]


@contextlib.contextmanager
def converted_dxf(source: Path) -> Iterator[Path]:
    """Convert ``source`` (a staged .dwg) to ASCII DXF; yield the DXF path.

    The scratch directory (and therefore the converted file) is deleted on
    exit, success or failure — callers must consume the file inside the
    ``with`` block. Raises ConvertError for every failure mode."""
    binary = dwg2dxf_bin()
    if binary is None:
        raise ConvertError(
            "INTERNAL",
            "local DWG conversion is not available on this deployment",
            False)
    cage = _cage_prefix()
    if cage_required() and (not cage or seccomp_filter_path() is None):
        # FAIL CLOSED. The deployment declared that hostile bytes are only ever
        # parsed inside the FULL cage — prlimit/setpriv AND its seccomp filter,
        # gated behind the same flag on purpose (RECEIPT-04): an image that
        # kept prlimit/setpriv but lost the compiled filter would otherwise
        # pass every other check and silently parse hostile bytes one defense
        # layer thinner, with no health check able to see it. Refuse rather
        # than parse degraded. Not retryable: a retry hits the same image with
        # the same missing tools.
        raise ConvertError(
            "INTERNAL",
            "local DWG conversion is unavailable: its sandbox is not present "
            "on this deployment",
            False)
    scratch = tempfile.mkdtemp(prefix="dwg2dxf-")
    try:
        out = Path(scratch) / "converted.dxf"
        argv = cage + [binary, "-y", "-o", str(out), str(source)]
        try:
            proc = subprocess.run(
                argv, cwd=scratch, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=convert_timeout_s())
        except subprocess.TimeoutExpired:
            # subprocess.run kills the child before raising, so nothing keeps
            # writing into the scratch dir behind this rejection. Deterministic
            # input -> deterministic overrun: not retryable.
            raise ConvertError(
                "TIMEOUT",
                f"DWG conversion did not finish within its "
                f"{convert_timeout_s():g} s budget", False) from None
        except OSError as exc:
            raise ConvertError(
                "INTERNAL",
                f"could not run the DWG converter: {type(exc).__name__}",
                False) from exc
        if proc.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
            # Converter output (not the user-facing message) may name scratch
            # paths; keep it in the server log only.
            tail = (proc.stdout or b"")[-400:].decode("utf-8", errors="replace")
            print(f"[dwg-convert] dwg2dxf failed (exit {proc.returncode}): "
                  f"{tail!r}", flush=True)
            raise ConvertError(
                "BAD_PARAMS",
                "the DWG could not be converted to DXF locally — the file is "
                "unreadable or uses an unsupported DWG feature", False)
        if out.stat().st_size > max_output_bytes():
            raise ConvertError(
                "BAD_PARAMS",
                f"the converted DXF exceeds the {max_output_bytes()} byte "
                "conversion output cap", False)
        yield out
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
