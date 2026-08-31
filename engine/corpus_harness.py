"""ENG1 slice: DXF round-trip corpus harness (card ENG-CORPUS).

Round-trips every fixture in engine/corpus/ through an EngineAdapter behind a
stable `round_trip(dxf_bytes) -> dxf_bytes` interface and reports a per-fixture
receipt: a fidelity table (byte + entity-level comparison, computed with the
existing python DXF path server/dxf_intake.py -- never a reimplemented
parser) and a rollback assertion (proof the on-disk fixture is never mutated,
identity or not).

Adapters: `IdentityAdapter` (default) proves the corpus + comparison
machinery; `--adapter=acadrust` (card F-1, ENG2) runs the REAL compiled
acadrust wasm through `engine/acadrust_adapter.py`. This switch is harness
TOOLING only -- the harness still has no flag, no production wiring, and no
selector; the production engine selector is the tenant capability contract
(card F-4). Acceptance bar per receipt: entity-level parity required,
byte_identical recorded-not-required (amended by F-1, see run_fixture).

Run:  python engine/corpus_harness.py [--adapter=identity|acadrust]
Exits 0 only if every corpus fixture's receipt is ok (bounded runtime, full
fidelity, source untouched); nonzero and a stderr line per failing fixture
otherwise.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
SERVER_DIR = PROJECT_ROOT / "server"
CORPUS_DIR = HERE / "corpus"

# Same insertion trick as engine/selfcheck.py: makes the REAL existing DXF
# path importable without turning PROJECT_ROOT itself into a shadow of the
# stdlib `platform` package.
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from dxf_intake import DxfParseError, parse_dxf_bytes  # noqa: E402

# Per-fixture wall-clock bound (milliseconds). Generous for the identity
# adapter and any bounded-corpus fixture; documented in docs/ENGINE-CORPUS.md
# as the harness's declared per-fixture runtime bound.
FIXTURE_TIMEOUT_MS = 1000.0

# The exact receipt shape (frozen field names) an enabled engine adapter must
# produce through this same harness. Kept as explicit constants -- not just
# prose in the docstring -- so a schema drift is a diffable code change.
FIDELITY_KEYS = (
    "byte_identical",
    "entity_count_before", "entity_count_after", "entity_count_match",
    "layers_before", "layers_after", "layers_match",
    "vertex_count_before", "vertex_count_after", "vertex_count_match",
    "score",
)
ROLLBACK_KEYS = ("source_sha256_before", "source_sha256_after", "source_untouched")
RECEIPT_KEYS = ("fixture", "adapter", "ok", "timing_ms", "fidelity", "rollback", "error")


class EngineAdapter:
    """Stable interface a CAD engine adapter round-trips a DXF fixture through.

    A subclass overrides `name` (identifies the adapter in each receipt) and
    `round_trip`. The base class deliberately implements neither so a future
    real engine adapter cannot accidentally inherit a silent no-op.
    """

    name: str = "unnamed-adapter"

    def round_trip(self, dxf_bytes: bytes) -> bytes:
        raise NotImplementedError("subclasses must implement round_trip()")


class IdentityAdapter(EngineAdapter):
    """Today's baseline: no engine is enabled, so round-trip is the
    mathematical identity (output bytes == input bytes). This is what proves
    the corpus + comparison machinery works before any real engine exists."""

    name = "identity"

    def round_trip(self, dxf_bytes: bytes) -> bytes:
        return dxf_bytes


def discover_corpus(corpus_dir: Optional[Path] = None) -> List[Path]:
    """Every `.dxf` fixture in the corpus, sorted for a stable, reproducible
    run order."""
    return sorted((corpus_dir or CORPUS_DIR).glob("*.dxf"))


def _entity_snapshot(dxf_bytes: bytes, *, source_name: str) -> Dict[str, Any]:
    """Parse via the REAL existing python DXF path and reduce to the counts a
    fidelity table compares. Raises DxfParseError on unparseable bytes --
    callers decide how to fold that into a receipt."""
    intake = parse_dxf_bytes(dxf_bytes, source_name=source_name)
    polylines = intake["polylines"]
    return {
        "layers": list(intake["layers"]),
        "entity_count": len(polylines),
        "vertex_count": sum(len(p["pts"]) for p in polylines),
        # Per-entity layer assignments (sorted multiset): the check that a
        # round trip reassigned no entity to another layer, independent of
        # LAYER-table normalization (see layers_match below).
        "entity_layers": sorted(p["layer"] for p in polylines),
    }


def compute_fidelity(before: bytes, after: bytes, *, source_name: str) -> Dict[str, Any]:
    """The fidelity table: byte-level plus entity-level (layers, polyline
    count, vertex count) comparison of before vs. after, both parsed through
    the same existing python DXF path so the comparison is honest -- never a
    second, divergent reader."""
    snap_before = _entity_snapshot(before, source_name=source_name)
    snap_after = _entity_snapshot(after, source_name=source_name)

    # layers_match, amended by card F-1 (measured against the real engine):
    # a conforming writer MATERIALIZES the spec-mandatory default layer "0"
    # in the LAYER table even when a hand-minimal fixture omits it — that is
    # normalization, not loss. The honest no-loss bar is therefore
    # (a) no source layer DROPPED (before ⊆ after) and (b) no entity
    # REASSIGNED (the per-entity layer multiset is unchanged). A real layer
    # named "0" carrying entities is still fully protected by (a) + (b).
    checks = {
        "byte_identical": before == after,
        "entity_count_match": snap_before["entity_count"] == snap_after["entity_count"],
        "layers_match": (
            set(snap_before["layers"]) <= set(snap_after["layers"])
            and snap_before["entity_layers"] == snap_after["entity_layers"]
        ),
        "vertex_count_match": snap_before["vertex_count"] == snap_after["vertex_count"],
    }
    score = sum(1 for ok in checks.values() if ok) / len(checks)

    fidelity = {
        "byte_identical": checks["byte_identical"],
        "entity_count_before": snap_before["entity_count"],
        "entity_count_after": snap_after["entity_count"],
        "entity_count_match": checks["entity_count_match"],
        "layers_before": snap_before["layers"],
        "layers_after": snap_after["layers"],
        "layers_match": checks["layers_match"],
        "vertex_count_before": snap_before["vertex_count"],
        "vertex_count_after": snap_after["vertex_count"],
        "vertex_count_match": checks["vertex_count_match"],
        "score": round(score, 3),
    }
    assert tuple(fidelity.keys()) == FIDELITY_KEYS
    return fidelity


def run_fixture(adapter: EngineAdapter, path: Path) -> Dict[str, Any]:
    """Round-trip one fixture through `adapter` and produce its full receipt.

    Rollback proof shape: the fixture is read into memory once, the adapter
    only ever sees bytes (never the path), and the on-disk file's sha256 is
    re-hashed after the round trip -- proving the source is untouched whether
    the adapter succeeds, produces divergent output, or raises.
    """
    t0 = time.perf_counter()
    original = path.read_bytes()
    sha_before = hashlib.sha256(original).hexdigest()

    error: Optional[str] = None
    fidelity: Optional[Dict[str, Any]] = None
    try:
        output = adapter.round_trip(original)
        fidelity = compute_fidelity(original, output, source_name=path.name)
    except DxfParseError as exc:
        error = f"unparseable round-trip output: {exc}"
    except Exception as exc:  # noqa: BLE001 -- any other adapter failure folds into the receipt, never raises past the harness
        error = f"{type(exc).__name__}: {exc}"

    timing_ms = round((time.perf_counter() - t0) * 1000, 3)

    sha_after = hashlib.sha256(path.read_bytes()).hexdigest()
    rollback = {
        "source_sha256_before": sha_before,
        "source_sha256_after": sha_after,
        "source_untouched": sha_before == sha_after,
    }
    assert tuple(rollback.keys()) == ROLLBACK_KEYS

    # ACCEPTANCE BAR (amended by card F-1, EXECUTION-PLAN v3.2 §4, resolving
    # the day-3 spike's open fidelity decision): entity-level parity is what
    # `ok` requires — entity count, layer list, vertex count. byte_identical
    # stays RECORDED (and still contributes to the informational `score`) but
    # is NOT required: a real engine's writer emits full default document
    # sections, so byte identity is unachievable by construction, and the
    # versioned-control storage model owns byte preservation (originals are
    # immutable versions), not the engine.
    entity_parity = (
        fidelity is not None
        and fidelity["entity_count_match"]
        and fidelity["layers_match"]
        and fidelity["vertex_count_match"]
    )
    ok = (
        error is None
        and rollback["source_untouched"]
        and timing_ms <= FIXTURE_TIMEOUT_MS
        and entity_parity
    )

    receipt = {
        "fixture": path.name,
        "adapter": adapter.name,
        "ok": ok,
        "timing_ms": timing_ms,
        "fidelity": fidelity,
        "rollback": rollback,
        "error": error,
    }
    assert tuple(receipt.keys()) == RECEIPT_KEYS
    return receipt


def run_corpus(adapter: EngineAdapter, corpus_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Receipts for every corpus fixture, in `discover_corpus` order."""
    return [run_fixture(adapter, path) for path in discover_corpus(corpus_dir)]


def _select_adapter(argv: List[str]) -> EngineAdapter:
    """Harness-tooling adapter selection (card F-1): `--adapter=acadrust`
    runs the real compiled engine; the default stays the identity baseline.
    This is TEST TOOLING selection only — the production engine selector is
    the tenant capability contract (card F-4), never this argv switch."""
    choice = "identity"
    for arg in argv:
        if arg.startswith("--adapter="):
            choice = arg.split("=", 1)[1].strip().lower()
    if choice == "identity":
        return IdentityAdapter()
    if choice == "acadrust":
        from acadrust_adapter import AcadrustAdapter  # noqa: PLC0415 — lazy: needs Node + the compiled build
        return AcadrustAdapter()
    raise SystemExit(f"unknown adapter {choice!r} (identity|acadrust)")


def main(argv: Optional[List[str]] = None) -> int:
    adapter = _select_adapter(argv if argv is not None else sys.argv[1:])
    receipts = run_corpus(adapter)
    print(json.dumps(receipts, indent=2))
    all_ok = all(r["ok"] for r in receipts)
    if not all_ok:
        for r in receipts:
            if not r["ok"]:
                print(f"  FAIL: {r['fixture']}: {r['error'] or r['fidelity']}", file=sys.stderr)
    print(f"\n# RESULT: {'ALL FIXTURES OK' if all_ok else 'FAILURES ABOVE'} "
          f"({len(receipts)} fixtures, adapter={adapter.name!r})")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
