"""Card F-1 (ENG2): the first REAL engine adapter through the corpus harness.

Two halves, gated separately so CI stays green without a Rust toolchain:

1. HERMETIC (always runs): the harness's amended acceptance bar — entity
   parity is required, byte identity is recorded-not-required, LAYER-table
   normalization (the writer materializing the spec-mandatory default layer
   "0") is not a failure, and a layer DROP or entity REASSIGNMENT still is.
   Proven with in-process fake adapters, no engine involved.

2. REAL ENGINE (opt-in, same gate family as engineWasmHarness.realwasm):
   skipped unless the documented wasm-pack build output exists on this
   machine (acadrust_adapter.compiled_build_present()). Where it runs, the
   compiled acadrust wasm must round-trip EVERY corpus fixture ok under the
   amended bar, with the source fixtures untouched.

Run:  cd server && python -m pytest tests/test_acadrust_adapter.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = PROJECT_ROOT / "engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

import acadrust_adapter  # noqa: E402
import corpus_harness as harness  # noqa: E402

FIXTURE_01 = ENGINE_DIR / "corpus" / "01_closed_lwpolyline_single_layer.dxf"


class _NormalizingFakeAdapter(harness.EngineAdapter):
    """Round-trips through the real intake parser and re-emits a MINIMAL but
    normalized document: same entities, same per-entity layers, same vertices,
    different bytes, plus the spec-mandatory default layer 0 materialized in
    the LAYER table — the exact shape the real engine's writer produces."""

    name = "normalizing-fake"

    def round_trip(self, dxf_bytes: bytes) -> bytes:
        intake_mod = __import__("dxf_intake")
        intake = intake_mod.parse_dxf_bytes(dxf_bytes, source_name="fake")
        out = ["0", "SECTION", "2", "TABLES", "0", "TABLE", "2", "LAYER"]
        for layer in ["0", *[l for l in intake["layers"] if l != "0"]]:
            out += ["0", "LAYER", "2", layer]
        out += ["0", "ENDTAB", "0", "ENDSEC", "0", "SECTION", "2", "ENTITIES"]
        for poly in intake["polylines"]:
            out += ["0", "LWPOLYLINE", "8", poly["layer"],
                    "90", str(len(poly["pts"])), "70", "1" if poly["closed"] else "0"]
            for x, y, _z in poly["pts"]:
                out += ["10", str(x), "20", str(y)]
        out += ["0", "ENDSEC", "0", "EOF"]
        return ("\n".join(out) + "\n").encode("ascii")


class _LayerDroppingAdapter(_NormalizingFakeAdapter):
    """Same as the normalizing fake but reassigns every entity to layer 0 —
    the data-loss shape the amended bar must still refuse."""

    name = "layer-dropping-fake"

    def round_trip(self, dxf_bytes: bytes) -> bytes:
        out = super().round_trip(dxf_bytes).decode("ascii").splitlines()
        for i, line in enumerate(out):
            if line == "8":
                out[i + 1] = "0"
        return ("\n".join(out) + "\n").encode("ascii")


def test_amended_bar_accepts_normalized_output_with_full_entity_parity():
    receipt = harness.run_fixture(_NormalizingFakeAdapter(), FIXTURE_01)
    assert receipt["fidelity"]["byte_identical"] is False
    assert receipt["fidelity"]["entity_count_match"] is True
    assert receipt["fidelity"]["layers_match"] is True
    assert receipt["fidelity"]["vertex_count_match"] is True
    assert receipt["ok"] is True, receipt
    assert receipt["rollback"]["source_untouched"] is True


def test_amended_bar_still_refuses_entity_reassignment():
    receipt = harness.run_fixture(_LayerDroppingAdapter(), FIXTURE_01)
    assert receipt["fidelity"]["layers_match"] is False
    assert receipt["ok"] is False


def test_adapter_failure_folds_into_the_receipt_and_never_raises():
    class _Exploding(harness.EngineAdapter):
        name = "exploding"

        def round_trip(self, dxf_bytes: bytes) -> bytes:
            raise RuntimeError("boom")

    receipt = harness.run_fixture(_Exploding(), FIXTURE_01)
    assert receipt["ok"] is False
    assert "boom" in (receipt["error"] or "")
    assert receipt["rollback"]["source_untouched"] is True


needs_real_engine = pytest.mark.skipif(
    not acadrust_adapter.compiled_build_present(),
    reason="compiled acadrust wasm build absent (run the documented wasm-pack build)",
)


@needs_real_engine
def test_real_acadrust_engine_round_trips_the_entire_corpus_ok():
    receipts = harness.run_corpus(acadrust_adapter.AcadrustAdapter())
    assert len(receipts) == 4
    for receipt in receipts:
        assert receipt["adapter"] == "acadrust"
        assert receipt["ok"] is True, receipt
        assert receipt["rollback"]["source_untouched"] is True
        # Recorded-not-required, and with THIS engine genuinely false: the
        # writer emits full default sections (spike day-3 finding).
        assert receipt["fidelity"]["byte_identical"] is False


@needs_real_engine
def test_real_engine_selection_via_the_harness_cli_switch():
    adapter = harness._select_adapter(["--adapter=acadrust"])
    assert adapter.name == "acadrust"
    assert isinstance(adapter, acadrust_adapter.AcadrustAdapter)
