# DXF round-trip corpus harness (card ENG-CORPUS)

ENG1 slice of the EXECUTION-PLAN DoD line: "round-trip corpus, rollback...
for any enabled engine." This slice ships the corpus, the harness, and the
receipt shape a real engine adapter must later produce -- **no flag, no
production wiring, no selector; test-and-tooling only.** `engine/` is
outside every scanned/wired production surface (server routers, `web/`
bundle, `cad_edit` flag paths); nothing here is imported by application code.

## Corpus

`engine/corpus/` -- 4 small, hand-authored, license-clean ASCII DXF
fixtures (no copied third-party CAD sample files). This is the full,
enumerated set; the corpus size is capped at **8 fixtures** and
`server/tests/test_engine_corpus_harness.py::test_corpus_is_bounded_and_matches_the_documented_enumeration`
locks both the cap and the exact file list below to this doc.

| File | Bytes | Entities | Exercises |
| --- | --- | --- | --- |
| `01_closed_lwpolyline_single_layer.dxf` | 132 | 1 closed `LWPOLYLINE`, 1 layer | baseline: single closed polyline, single layer |
| `02_open_lwpolyline_two_layers.dxf` | 215 | 1 open + 1 closed `LWPOLYLINE`, 2 layers | mixed open/closed flag, multi-layer ordering |
| `03_classic_polyline_vertex_seqend.dxf` | 170 | 1 classic `POLYLINE`/`VERTEX`/`SEQEND` | the legacy entity shape (not `LWPOLYLINE`) |
| `04_empty_entities_section.dxf` | 36 | 0 entities | honest-empty edge case (zero polylines, zero layers) |

Because the license fence (card C1-1, `docs/CAD-ENGINE-LICENSE-FENCE.md`)
scans only `web/` and `vendor/`, `engine/corpus/` is out of its scope by
construction; the corpus is additionally self-proven clean by
`test_every_fixture_is_license_clean_hand_authored_ascii_dxf`, which checks
each fixture's raw text for the fence's own deny markers (OpenCADStudio
identifiers, a GPL-3.0 SPDX/header match, an `acadrust` reference) directly,
so the "must stay green over the corpus" oracle line holds independent of
scan-root scope.

## Harness

`engine/corpus_harness.py` defines:

- **`EngineAdapter`** -- the stable interface any engine adapter (identity,
  today's baseline, or a real transforming engine later) round-trips a
  fixture through: `round_trip(dxf_bytes: bytes) -> bytes`. The base class
  raises `NotImplementedError` so nothing can silently no-op by inheriting
  an unimplemented method.
- **`IdentityAdapter`** -- today's only wired adapter. No engine is enabled,
  so `round_trip` returns its input bytes unchanged. This is what "the
  harness runs with NO engine enabled today" means concretely: the identity
  transform is the trivial, always-available case that proves the corpus +
  comparison machinery works end-to-end before a real engine exists.
- **`run_fixture(adapter, path)`** / **`run_corpus(adapter)`** -- round-trip
  one fixture / every corpus fixture through `adapter`, producing a receipt
  each (see schema below).
- Comparison is computed by parsing both the original and round-tripped
  bytes through the **real, existing python DXF path**,
  `server/dxf_intake.py::parse_dxf_bytes` -- never a second, reimplemented
  reader, so a fidelity match is honest about what this repo's own DXF
  intake actually sees.

Run it directly:

```
python engine/corpus_harness.py
```

Prints one JSON receipt per fixture and exits 0 only if every fixture's
receipt is `ok`; nonzero (with a stderr line per failing fixture) otherwise.

### Runtime bound

`FIXTURE_TIMEOUT_MS = 1000` (one second) -- the harness's declared
per-fixture wall-clock bound (`corpus_harness.FIXTURE_TIMEOUT_MS`). Each
receipt's `timing_ms` is measured from before the source file is read to
after the round trip and comparison complete; a fixture whose `timing_ms`
exceeds the bound is `ok: false` even if its fidelity table is otherwise
perfect. On the identity adapter over this corpus, every fixture completes
in well under 1 ms.

## Receipt shape

The exact fields an enabled engine adapter must later produce through this
same harness (frozen as `RECEIPT_KEYS` / `FIDELITY_KEYS` / `ROLLBACK_KEYS`
in `engine/corpus_harness.py`, and locked by
`test_receipt_shape_is_the_exact_fidelity_table_plus_rollback_assertion_schema`):

```jsonc
{
  "fixture": "01_closed_lwpolyline_single_layer.dxf",
  "adapter": "identity",
  "ok": true,
  "timing_ms": 0.37,
  "fidelity": {
    "byte_identical": true,
    "entity_count_before": 1, "entity_count_after": 1, "entity_count_match": true,
    "layers_before": ["Panels"], "layers_after": ["Panels"], "layers_match": true,
    "vertex_count_before": 4, "vertex_count_after": 4, "vertex_count_match": true,
    "score": 1.0
  },
  "rollback": {
    "source_sha256_before": "<sha256 hex>",
    "source_sha256_after": "<sha256 hex>",
    "source_untouched": true
  },
  "error": null
}
```

**Fidelity table**: byte-level (`byte_identical`) plus three entity-level
comparisons computed via `server/dxf_intake.py` -- entity count, layers, and
total vertex count across all polylines. `score` is the fraction of the four
checks that passed (`0.0`-`1.0`) and is informational.

**Acceptance bar (amended by card F-1, EXECUTION-PLAN v3.2 §4)**: `ok`
requires the three ENTITY-LEVEL checks; `byte_identical` is recorded, never
required -- a real engine's writer emits full default document sections, so
byte identity is unachievable by construction, and byte preservation is the
versioned-control store's guarantee (originals are immutable versions), not
the engine's. `layers_match` is correspondingly the no-loss form, measured
against the real engine: (a) no source layer dropped (`before ⊆ after`) and
(b) no entity reassigned (the per-entity layer multiset is unchanged); a
conforming writer materializing the spec-mandatory default layer `0` in the
LAYER table is normalization, not loss.

## ENG2: the real adapter (card F-1)

`engine/acadrust_adapter.py::AcadrustAdapter` round-trips through the
compiled acadrust wasm build by spawning
`vendor/acadrust-worker/roundtrip-cli.mjs` (bytes in/bytes out, inside the
license fence's allowed prefix, importing the wasm-pack `pkg-node` output
documented in `worker-entry.mjs`). Select it as harness tooling with:

```
python engine/corpus_harness.py --adapter=acadrust
```

Measured 2026-08-31 on the full corpus: ALL FIXTURES OK -- entity and vertex
counts identical on every fixture (including the classic
POLYLINE/VERTEX/SEQEND form), per-entity layers preserved, sources untouched,
`byte_identical` false on all four exactly as the day-3 spike predicted.
The compiled build stays machine-local (never committed); the real-engine
tests skip cleanly where it is absent
(`server/tests/test_acadrust_adapter.py`). The production selector remains
card F-4's, not this switch.

**Rollback assertion**: the adapter only ever receives bytes, never the
fixture's path, so the harness re-hashes the on-disk file after every round
trip (success, partial-fidelity, or a raised exception) and asserts
`source_sha256_before == source_sha256_after`. This is the "rollback proof
shape" this ENG1 slice ships: proof that round-tripping a fixture -- through
any adapter, including one that fails outright -- never mutates the
committed corpus. `test_rollback_assertion_holds_even_when_the_adapter_never_returns_bytes`
exercises the failure path directly: `rollback.source_untouched` stays
`true` even when `round_trip` raises and the receipt's `ok` is `false`.

## What this slice deliberately does not do

- No flag: `cad_edit` (the card's tracking flag) is not read, set, or
  branched on anywhere in `engine/corpus_harness.py` or its tests.
- No production wiring: nothing in `server/`, `web/`, or `da/` imports
  `engine/corpus_harness.py`; it is a standalone script plus its own test
  suite.
- No selector: there is no registry entry, route, or dispatch table that
  picks an adapter at request time. `IdentityAdapter` is instantiated
  directly by `main()`/tests; a future enabled engine adapter is wired in
  wherever the ENG2+ slice that adds a real engine lands, not here.
