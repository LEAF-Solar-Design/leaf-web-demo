"""ENG2 (card F-1): the first REAL engine adapter for the corpus harness.

Round-trips DXF bytes through the compiled acadrust wasm build by spawning
the bytes-in/bytes-out CLI that lives inside the license fence's allowed
vendor prefix (``vendor/acadrust-worker/roundtrip-cli.mjs`` importing the
wasm-pack ``pkg-node`` output). This module holds NO engine logic of its own:
the crate stays unmodified and rev-pinned (docs/CAD-ENGINE-LICENSE-REVIEW.md),
and every adaptation is the subprocess seam below.

Hardening contract: the subprocess is time-bounded, its output is size-capped
by the CLI itself, a nonzero exit raises with the CLI's one-line stderr (the
harness folds that into an ok=false receipt and proves the fixture untouched),
and an EMPTY stdout on exit 0 is treated as a failure, never as a zero-byte
document. Fails closed on a missing node executable.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from corpus_harness import EngineAdapter

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
_RUNNER = PROJECT_ROOT / "vendor" / "acadrust-worker" / "roundtrip-cli.mjs"
_COMPILED_BUILD = (
    PROJECT_ROOT / "vendor" / "acadrust-worker" / "pkg-node" / "acadrust_worker_bg.wasm"
)

# Wall-clock bound for one subprocess round trip. The harness's own
# per-fixture bound (FIXTURE_TIMEOUT_MS) stays the receipt-level authority;
# this is the hard kill so a wedged child can never hang the harness.
ROUND_TRIP_TIMEOUT_S = 30.0


def compiled_build_present() -> bool:
    """True when the documented wasm-pack build output exists — the same
    opt-in gate the realwasm vitest uses, exposed for test skip logic."""
    return _COMPILED_BUILD.is_file()


class AcadrustAdapter(EngineAdapter):
    """Real engine adapter: parse + write through the compiled acadrust wasm."""

    name = "acadrust"

    def __init__(self, node_executable: str | None = None) -> None:
        resolved = node_executable or shutil.which("node")
        if not resolved:
            raise RuntimeError("node executable not found; the acadrust adapter needs Node")
        self._node = resolved

    def round_trip(self, dxf_bytes: bytes) -> bytes:
        proc = subprocess.run(
            [self._node, str(_RUNNER)],
            input=dxf_bytes,
            capture_output=True,
            timeout=ROUND_TRIP_TIMEOUT_S,
            cwd=str(_RUNNER.parent),
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"acadrust round trip failed (exit {proc.returncode}): {stderr[:500]}"
            )
        if not proc.stdout:
            raise RuntimeError("acadrust round trip produced no output bytes")
        return proc.stdout
