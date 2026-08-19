# acadrust WASM spike — day 2 (card C1B-D2)

DRUMLINE W9 spike lineage, day 2 of 14 (go/no-go day 10). Continues the
day-1 receipt (`leaf-plan` ledger, `kind: spike-day1`, 2026-08-19T13:38Z):
"all 3 day-1 goals GREEN" — acadrust (MPL-2.0,
https://github.com/hakanaktt/acadrust) round-trips a one-LINE DXF natively
and compiles clean for `wasm32-unknown-unknown`; day 1's own next-goal line
was **"wasm-bindgen wrapper + browser Worker round-trip of the same
fixture."** This doc records what day 2 delivered against that goal, in this
repo's file boundary (`engine/acadrust-worker/`,
`web/src/cad/engineWasmHarness.test.js`, this file).

## Verdict

**Message-schema integration proven GREEN; real wasm execution still
OPEN.** No Rust/wasm toolchain is present in this workdir (checked:
`cargo`, `rustc`, `wasm-pack` all absent from `PATH`). Day 1 hit the same
absence and worked around it with a rustup install fully contained inside
its own spike workdir, outside this repo checkout — not reachable from here,
and installing a new one is outside this card's file boundary (`engine/`,
one test file, one doc). So day 2 delivers:

1. A real wasm-bindgen wrapper crate (`engine/acadrust-worker/Cargo.toml`,
   `src/lib.rs`) — the day-2 source deliverable the oracle asks for, written
   against acadrust's documented in-memory DXF API
   (`DxfReader::from_reader`, `DxfWriter::write_to_vec`) and day 1's exact
   `getrandom`/`wasm_js` fix, carried forward verbatim into `Cargo.toml`.
   **Not compiled** (no toolchain here) — see OQ-1..OQ-3 below.
2. A JS-native stand-in for that crate's compiled output
   (`engine/acadrust-worker/bindings.mjs`) implementing the same DXF
   group-code round trip acadrust's DXF path handles, scoped to exactly the
   group codes day 1's fixture needs (HEADER `$ACADVER`, ENTITIES `LINE`).
   Function names mirror `src/lib.rs`'s `#[wasm_bindgen]` exports 1:1
   (`parseDxf`/`writeDxf`/`bytesEqual` via `js_name` overrides; `parsed
   .entities` is a getter on the Rust handle vs a plain array here, same
   element shape `{type, layer, start, end}`) so the swap to the real generated
   bindings is a one-line import change in `worker-entry.mjs`, not a
   rewrite.
3. A worker-side message loop (`engine/acadrust-worker/worker-entry.mjs`)
   speaking the exact same schema `web/src/cad/engineWorker.js`'s
   `EngineBoundary` already validates (`init`→`ready`,
   `loadDocument{documentId}`→`documentLoaded{documentId, ...}` — extra
   fields ride along freely since `validateBoundaryMessage` only checks
   `required` keys are present, never rejects extras).
4. A test (`web/src/cad/engineWasmHarness.test.js`) that drives a real
   browser-shaped round trip of day 1's exact fixture
   (`engine/acadrust-worker/fixtures/one_line.dxf` — same R12/AC1009, one
   LINE (0,0,0)→(100,50,0) content as day 1's
   `spike-harness/fixtures/one_line.dxf`) through `EngineBoundary`, loaded
   via the one legal `new Worker(new URL(...))` shape this repo's license
   fence allows (see "License fence" below), executed in a real `node`
   subprocess (day 1's own stated day-2 plan: "run the round trip in Node
   ... to prove the module actually executes" — not a workaround invented
   here). Parse → assert entity → write → re-parse → assert again (byte AND
   entity comparison), same shape as day 1's native round-trip test.

## What actually ran (evidence)

```
$ cd web && npx vitest run src/cad/
 ✓ src/cad/engineWasmHarness.test.js (3 tests)
 ✓ src/cad/engineBoundary.test.js (15 tests)
 ✓ src/cad/entry.test.jsx (9 tests)
 ✓ src/cad/editSurface.test.jsx (9 tests)
 Test Files  4 passed (4)
      Tests  36 passed (36)

$ npx vitest run   # full web/src suite, regression check
 Test Files  38 passed (38)
      Tests  318 passed (318)

$ python scripts/check_license_fence.py --self-test
 Ran 17 tests ... OK
$ python scripts/check_license_fence.py .
 license fence: clean
```

The three `engineWasmHarness.test.js` cases:

- Negative control: `cad_edit` off never instantiates the wasm engine
  worker (`globalThis.Worker` mock never called) — the existing
  `engineBoundary.test.js` negative controls stay green too, untouched.
- The round trip: `init`→`{type:'ready'}`, then
  `loadDocument{documentId:'one_line.dxf'}`→`documentLoaded` carrying
  `roundTrip.entityCount === 1`, `firstEntity`/`reparsedFirstEntity` both
  exactly `{type:'LINE', layer:'0', start:[0,0,0], end:[100,50,0]}`
  (**entity comparison**), and `bytesIdentical === true` with
  `writtenByteLength === originalByteLength` (**byte comparison**) —
  parsed-written bytes match the original fixture exactly.
- An unknown `documentId` returns a schema-valid `error` message, never
  throws into the boundary, `droppedCount` stays 0 in all three cases (the
  boundary's validate-or-drop path never fires — every message this spike
  sends is well-formed against the *existing*, unmodified schema).

## License fence

This repo's `docs/CAD-ENGINE-LICENSE-FENCE.md` (card C1-1) only allows a
`web/` file to reference the MPL-2.0 engine by path via the literal shape
`new Worker(new URL('<path>', import.meta.url))` — anywhere else, even a
comment, is a scanned violation (`scripts/check_license_fence.py`,
`ALLOWED_ACADRUST_PREFIX`, deny rule 3). Two consequences worth recording:

- **This card's `engine/acadrust-worker/` is outside the fence's own
  documented prefix** (`vendor/acadrust-worker/`). It is still fence-clean
  because the fence's `SCAN_ROOTS` is exactly `("web", "vendor")` — `engine/`
  is out of scope entirely, by design (see the fence doc's own "Scope"
  section). Carried forward as an open question below (OQ-4): if this spike
  graduates past day 2, either the prefix constant needs a real update to
  cover `engine/acadrust-worker/`, or the crate needs to move under
  `vendor/acadrust-worker/` to match the fence's existing contract. Neither
  is this card's call to make silently.
- `web/src/cad/engineWasmHarness.test.js` therefore references the engine
  worker's path in exactly one place — the `new Worker(new URL(...))` call
  in `createEngineWorker()` — and nowhere else, including comments. Verified
  by an actual fence run above, not just by construction.

## Bundle-size measurement

**OPEN — no `.wasm` artifact exists to measure.** No Rust/wasm toolchain is
present in this workdir; nothing here can run `wasm-pack build --release
--target web` (day 1's exact command, `RUSTFLAGS='--cfg
getrandom_backend="wasm_js"'`, is recorded in `Cargo.toml`'s trailing
comment for whoever runs it next). This was already unmeasured after day 1
(day 1 only proved compile-clean, never produced or sized an artifact), so
day 2 does not regress it — it explicitly carries it forward as:

**OQ-2: measure `.wasm` binary size (raw + gzip -9) once a toolchain is
available**, via:
```bash
RUSTFLAGS='--cfg getrandom_backend="wasm_js"' \
  wasm-pack build --release --target web engine/acadrust-worker
wc -c engine/acadrust-worker/pkg/acadrust_worker_bg.wasm
gzip -9 -c engine/acadrust-worker/pkg/acadrust_worker_bg.wasm | wc -c
```

## Day-1 open questions: answered or carried

| # | Day-1 open item | Day-2 status |
|---|---|---|
| OQ-1 | No pinned acadrust git rev (day 1 didn't record one) | **Carried.** `Cargo.toml` depends on the default branch; pin an exact commit SHA once a toolchain can resolve and lock it. |
| OQ-2 | No `.wasm` artifact / bundle size measured | **Carried**, with the exact reproduction command above (day 1 also never measured this). |
| OQ-3 | Exact acadrust method chain (`DxfReader`/`DxfWriter`, entity accessors) unconfirmed against the real crate source | **Carried.** `src/lib.rs` is written from day 1's structural notes (`document.rs`, `entities/` modules) — flagged inline for verification once the crate can actually be resolved and read. |
| OQ-4 | `ALLOWED_ACADRUST_PREFIX` (`vendor/acadrust-worker/`) doesn't cover this card's `engine/acadrust-worker/` path | **New, carried** (see "License fence" above) — not a day-1 item, surfaced by day 2's file boundary; flagging rather than silently editing the fence's protected constant. |
| — | DWG round-trip (day 1 noted `DwgReader::from_stream` is equally in-memory capable) | **Explicitly out of day-2 scope** — the oracle names "the same DXF fixture day 1 used," DXF only. Carried as future work alongside OQ-1..OQ-3. |
| — | Toolchain absence itself (day 1 worked around it with a workdir-contained rustup, outside this repo) | **Recurred identically on day 2**, in a different workdir. Not re-solved here: installing a toolchain is outside this card's file boundary (`engine/acadrust-worker/`, one test, one doc). Running the actual round trip in `node` (see "What actually ran") is the closest day 2 gets to day 1's stated goal without one. |

## Terminal receipt (spike lineage)

```json
{
  "kind": "spike-day2",
  "at": "2026-08-19",
  "card": "C1B-D2",
  "lane": "sonnet",
  "verdict": "message-schema integration GREEN (wasm-bindgen wrapper source + JS-native stand-in + worker message loop + browser-shaped round trip test, all passing); real wasm compile/execute still BLOCKED on toolchain absence, same wall day 1 hit",
  "delivered": [
    "engine/acadrust-worker/Cargo.toml",
    "engine/acadrust-worker/src/lib.rs",
    "engine/acadrust-worker/bindings.mjs",
    "engine/acadrust-worker/worker-entry.mjs",
    "engine/acadrust-worker/fixtures/one_line.dxf",
    "web/src/cad/engineWasmHarness.test.js",
    "docs/ACADRUST-SPIKE-DAY2.md"
  ],
  "tests": "web/src/cad/engineWasmHarness.test.js 3/3 pass; full web/src suite 318/318 pass; license-fence self-test 17/17 pass; license-fence real scan clean",
  "cad_edit_negative_control": "green — wasm engine worker never instantiated with the flag off, existing engineBoundary.test.js negative controls unmodified and still green",
  "open_questions_carried": ["OQ-1 (rev pin)", "OQ-2 (bundle size)", "OQ-3 (API verification)", "OQ-4 (license-fence prefix scope)"],
  "next_goal": "day 3 (if continued): resolve OQ-4's prefix-vs-path question with the repo owner, then either a contained toolchain install (day-1's pattern) or CI-side wasm build step to finally produce and size a real .wasm artifact"
}
```
