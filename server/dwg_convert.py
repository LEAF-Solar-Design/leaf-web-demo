"""
Local DWG -> ASCII DXF conversion (the APS-free guest DWG read lane).

Runs GNU libredwg's ``dwg2dxf`` as a SANDBOXED SUBPROCESS: fixed argv (no
shell), stdin closed, output confined to a fresh scratch directory that is
always deleted, a wall-clock timeout that kills the child, and a size cap on
the converted output. The converted DXF feeds the EXISTING dxf_intake parser
unchanged — this module produces a file, never intake, so the honesty rule
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
    scratch = tempfile.mkdtemp(prefix="dwg2dxf-")
    try:
        out = Path(scratch) / "converted.dxf"
        argv = [binary, "-y", "-o", str(out), str(source)]
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
