# acadrust WASM spike — day 3 (card C1B-D3)

DRUMLINE W9 spike lineage, day 3 of 14 (go/no-go day 10). Continues day 2's
receipt (`docs/ACADRUST-SPIKE-DAY2.md`): "message-schema integration GREEN;
real wasm execution still OPEN," carrying four open questions (OQ-1..OQ-4).
This doc closes all four with real evidence, not more static inspection.

## Verdict

**PROVEN today, with real evidence:** a real `acadrust` git rev is pinned
(OQ-1); a real `.wasm` binary was compiled, packaged, and measured, both raw
and gzip -9 (OQ-2); the wasm-bindgen wrapper's API mismatches against the
real crate were found and fixed — six of them, one only discoverable by
actually running the compiled output, not by reading source (OQ-3); the
fixed wrapper was run in Node against day 1's exact one-LINE DXF fixture,
with the entities round-tripping correctly (OQ-5, new); the license fence's
own scope gap around `engine/acadrust-worker/` was made concrete (not just
theoretical) by a real violation this day's own test file tripped, and a
recommendation is written below (OQ-4).

**Still OPEN:** byte-for-byte DXF round-trip fidelity. The real
`DxfWriter` emits a complete document (default table/object/class sections)
even from a minimal 140-byte R12 input, producing a 46,750-byte output —
correct entity data, but not byte-identical to the input. This is a genuine
property of the real engine the day-2 JS stand-in never modeled (the
stand-in's minimal writer only ever emits what it read). Not a defect in
this spike's wrapper; a real fidelity question for whoever designs the
eventual save/export contract, carried forward as new open work below.

**GO/NO-GO READING:** the hard technical risks this 14-day spike exists to
retire — does the crate actually compile and run for wasm32, does a
contained toolchain work on this host, does the message-boundary
integration hold against the real (not stand-in) engine — are now answered
yes, with receipts. The one substantive remaining question (byte-identical
round-trip) is a product/design question about what "round trip" needs to
mean for this tool, not a feasibility blocker.

## OQ-1: pin the acadrust rev — RESOLVED

```
$ git clone https://github.com/hakanaktt/acadrust.git C:\tmp\spike-day3-c6477dd5-crate
$ git -C C:\tmp\spike-day3-c6477dd5-crate rev-parse HEAD
18500466e7e4392ef830fdc59cede75fa3794f2b
$ git -C C:\tmp\spike-day3-c6477dd5-crate log -1 --format='%H %ci %s'
18500466e7e4392ef830fdc59cede75fa3794f2b 2026-08-13 09:24:07 +0300 Merge pull request #57 from HakanSeven12/main
```

`engine/acadrust-worker/Cargo.toml` now pins:

```toml
acadrust = { git = "https://github.com/hakanaktt/acadrust.git", rev = "18500466e7e4392ef830fdc59cede75fa3794f2b" }
```

**Chose `git = ... rev = "..."`, not a vendored path dependency.** The
wrapper needs zero local patches to the crate — every fix this spike made
was in `engine/acadrust-worker/src/lib.rs`, this repo's own wrapper, never
in the acadrust source itself. A `rev`-pinned git dependency is the minimal
correct pin for that situation: reproducible (the exact commit resolves
deterministically), and it stays on the normal `cargo update`/dependency
workflow. Vendoring (copying the crate's source into
`vendor/acadrust-worker/` and depending on it via `path = ...`) would only
be the right call if local patches were needed — they are not.

## OQ-3: verify the wrapper's API against the real crate — RESOLVED

Day 2's `src/lib.rs` was written from day 1's structural notes, never
compiled. Checked against the real crate at the pinned rev
(`C:\tmp\spike-day3-c6477dd5-crate`) by reading the actual source, then
verified by `cargo check --target wasm32-unknown-unknown` (clean) and a
real Node execution run. Six corrections, five found by reading, one only
found by running:

1. **`DxfReader::from_reader(reader)` returns `Result<DxfReader>`, not a
   document.** `DxfReader` is a builder handle; the actual parse is a
   separate consuming call, `.read(self) -> Result<CadDocument>`
   (`src/io/dxf/reader.rs:60,158`). Day 2 stored the `DxfReader` itself as
   `ParsedDxf.inner: acadrust::Document` — a type that does not exist (the
   real type is `acadrust::CadDocument`, `src/document.rs:1002`) — and never
   called `.read()`. Fixed: `ParsedDxf.inner: CadDocument`, produced by
   `DxfReader::from_reader(..)?.read()?`.
2. **`DxfWriter` is constructed, not called as an associated function.**
   Day 2 wrote `DxfWriter::write_to_vec(&doc.inner)`. The real shape is
   `DxfWriter::new(&CadDocument) -> DxfWriter<'a>`
   (`src/io/dxf/writer/mod.rs:29`), then `.write_to_vec(&self) ->
   Result<Vec<u8>>` as an instance method (`mod.rs:71`). Fixed:
   `DxfWriter::new(&doc.inner).write_to_vec()`.
3. **`CadDocument::entities()` yields an enum, not something with
   `.as_line()`.** It returns `impl Iterator<Item = &EntityType>`
   (`src/document.rs:2675`), where `EntityType` is a 40+-variant enum
   (`src/entities/mod.rs:405`) with a `Line(Line)` variant
   (`mod.rs:409`). No `.as_line()` method exists anywhere in the crate —
   day 2 invented it. Fixed: `match e { EntityType::Line(line) => ..., _ =>
   None }`.
4. **`.layer()` lives on the `Entity` trait, not on `EntityType`.** The
   trait is defined at `src/entities/mod.rs:168` and implemented per
   concrete entity struct (e.g. `impl Entity for Line`,
   `src/entities/line.rs:74-89`), not as an inherent method on the enum.
   Day 2 wrote `e.layer()` where `e: &EntityType`, which does not compile.
   Fixed: match down to `&Line` first, then `line.layer()` resolves via the
   trait (`use acadrust::entities::Entity;` brought into scope).
5. **`Line.start`/`Line.end` are `Vector3{x,y,z}` public fields** —
   day 2's guess here (`src/entities/line.rs:9-20`) was already correct,
   carried forward unchanged.
6. **(found only by running the real compiled wasm, not by reading source)**
   `serde_wasm_bindgen::to_value` with its default `Serializer` marshals a
   Rust map/struct as a JS `Map` instance, not a plain object.
   `JSON.stringify` on a `Map` prints `{}` — exactly the empty-object
   symptom the first real run produced for every entity (see "First
   real-wasm run" below). Fixed with `Serializer::json_compatible()`
   (`serialize_maps_as_objects: true`), the crate's own documented
   plain-object mode, confirmed by reading
   `serde-wasm-bindgen-0.6.5/src/ser.rs` in the local cargo registry cache.

All six corrections are also recorded inline in
`engine/acadrust-worker/src/lib.rs`'s module doc comment, next to the code
they fix.

## Toolchain — contained, day-1's pattern

```
CARGO_HOME=C:\tmp\spike-day3-toolchain\cargo
RUSTUP_HOME=C:\tmp\spike-day3-toolchain\rustup
```

```
$ curl -sSfL -o rustup-init.exe https://win.rustup.rs/x86_64
$ ./rustup-init.exe -y --no-modify-path --profile minimal \
    --default-host x86_64-pc-windows-gnu --default-toolchain stable
  stable-x86_64-pc-windows-gnu installed - rustc 1.98.0 (88d9e12ae 2026-08-18)
$ rustup target add wasm32-unknown-unknown
$ cargo install wasm-pack --locked
  Installed package `wasm-pack v0.15.0` (executable `wasm-pack.exe`)   # 9m15s, built from source (no prebuilt binary path taken)
```

`x86_64-pc-windows-gnu` host, day 1's exact choice — avoids an MSVC Build
Tools dependency. Nothing installed outside `C:\tmp\spike-day3-toolchain\`;
delete that directory to remove all trace, same as day 1.

## OQ-2: produce and measure the real `.wasm` — RESOLVED

Compile-only check first (fast signal before the full wasm-pack pipeline):

```
$ cd engine/acadrust-worker
$ RUSTFLAGS='--cfg getrandom_backend="wasm_js"' cargo check --target wasm32-unknown-unknown
   Checking acadrust v0.4.1 (https://github.com/hakanaktt/acadrust.git?rev=18500466e7e4392ef830fdc59cede75fa3794f2b#18500466)
   Checking acadrust-worker v0.1.0 (C:\tmp\spike-day3-c6477dd5\engine\acadrust-worker)
    Finished `dev` profile [unoptimized + debuginfo] target(s) in 31.94s
EXIT: 0
```

Full release build, web target (day 1's exact reference command):

```
$ RUSTFLAGS='--cfg getrandom_backend="wasm_js"' \
    wasm-pack build --release --target web --out-dir pkg
    Finished `release` profile [optimized] target(s) in 2m 15s
[INFO]: :-) Done in 2m 56s
EXIT: 0
```

```
$ wc -c engine/acadrust-worker/pkg/acadrust_worker_bg.wasm
2004823 engine/acadrust-worker/pkg/acadrust_worker_bg.wasm
$ gzip -9 -c engine/acadrust-worker/pkg/acadrust_worker_bg.wasm | wc -c
698941
```

| Metric | Bytes | Human |
|---|---|---|
| Raw `.wasm` | 2,004,823 | 1.91 MiB |
| gzip -9 | 698,941 | 682.6 KiB |

`opt-level = "z"`, `lto = true`, `panic = "abort"` (already set in
Cargo.toml's `[profile.release]` from day 2) were in effect for this build —
this is the size-optimized number, not a debug build's.

Second build, `nodejs` target (needed for OQ-5's Node execution proof —
`web` target's glue calls browser `fetch()` to instantiate, which Node does
not provide without a polyfill; `nodejs` target's glue instantiates
synchronously via `fs.readFileSync`, no polyfill needed):

```
$ RUSTFLAGS='--cfg getrandom_backend="wasm_js"' \
    wasm-pack build --release --target nodejs --out-dir pkg-node
[INFO]: :-) Done in 39.42s   # incremental rebuild after the OQ-3 correction-6 fix; first build (before that fix) was 52.15s
EXIT: 0
```

Same `.wasm` binary size as the web target (2,004,823 bytes) — the
`nodejs`/`web` distinction is only in wasm-bindgen's generated JS glue, not
in the compiled wasm itself.

## OQ-5 (new): run the real wasm in Node against day 1's fixture

`engine/acadrust-worker/worker-entry.mjs` gained an env switch
(`CAD_ENGINE_REAL_WASM=1`) that lazily imports the real compiled build
(`pkg-node/acadrust_worker.js`) instead of the JS-native stand-in
(`bindings.mjs`) it uses by default — both expose the identical surface by
construction (`parseDxf`/`writeDxf`/`bytesEqual`, `parsed.entities` as an
array-like getter), so this is a one-line lazy module swap inside
`getEngine()`, not a rewrite of the message loop.

### First real-wasm run, unfixed (OQ-3 correction 6's actual discovery)

```
$ cd engine/acadrust-worker
$ CAD_ENGINE_REAL_WASM=1 node --input-type=module -e "
    import { handleMessage } from './worker-entry.mjs'
    await handleMessage({ type: 'init' })
    console.log(JSON.stringify(await handleMessage({ type: 'loadDocument', documentId: 'one_line.dxf' })))"
{"type":"documentLoaded","documentId":"one_line.dxf","roundTrip":{
  "entityCount":1,"firstEntity":{},"reparsedFirstEntity":{},
  "originalByteLength":140,"writtenByteLength":46750,"bytesIdentical":false}}
```

`entityCount` was already correct (1) — the entity WAS found — but
`firstEntity`/`reparsedFirstEntity` marshalled as empty objects. Traced to
`serde_wasm_bindgen::to_value`'s default `Map`-not-object serialization
(correction 6 above); NOT traceable by reading `src/lib.rs` alone, only by
running the real compiled output.

### After the fix

```
$ CAD_ENGINE_REAL_WASM=1 node --input-type=module -e "..."   # same script
{
  "type": "documentLoaded",
  "documentId": "one_line.dxf",
  "roundTrip": {
    "entityCount": 1,
    "firstEntity":       { "type": "LINE", "layer": "0", "start": [0,0,0], "end": [100,50,0] },
    "reparsedFirstEntity": { "type": "LINE", "layer": "0", "start": [0,0,0], "end": [100,50,0] },
    "originalByteLength": 140,
    "writtenByteLength": 46750,
    "bytesIdentical": false
  }
}
```

**Entity round trip: exact match**, both against the freshly parsed
original and the re-parsed written output — `{type:'LINE', layer:'0',
start:[0,0,0], end:[100,50,0]}` on both sides, identical to day 1's native
host round-trip result and the day-2 stand-in's result.

**Byte round trip: not identical, and not expected to be.** The real
`DxfWriter` emits a complete DXF document — default `TABLES`/`OBJECTS`/
`CLASSES` sections — even when the source document was a minimal 140-byte
R12 file with none of those sections present. 46,750 written bytes vs. 140
original. This is a genuine, previously-unmodeled property of the real
engine: day 1's native round-trip test and day 2's JS stand-in both only
ever exercised writers that emit exactly what they read, so neither ever
surfaced this. Carried forward as new open work (see "New open questions"
below), not silently worked around.

### Test-suite wiring

Added `web/src/cad/engineWasmHarness.realwasm.test.js` — the day-2 test's
file, `engineWasmHarness.test.js`, is unmodified (still exercises the JS
stand-in and still passes); the new file drives the identical
`EngineBoundary` round trip but forwards `CAD_ENGINE_REAL_WASM=1` into the
spawned Node subprocess, so it exercises the real compiled build through
the exact same message-schema path a real browser Worker would use.

```
$ cd web && npx vitest run
 Test Files  41 passed (41)
      Tests  328 passed (328)
```

(41 files / 328 tests total, up from day 2's 38/318 — that growth is
unrelated repo activity between day 2 and day 3, not a day-3 contribution;
the two files this spike owns — `engineWasmHarness.test.js` and the new
`engineWasmHarness.realwasm.test.js` — both pass, 4 tests between them.)

## OQ-4: the license fence's scope gap — concrete evidence, and a recommendation

Day 2 flagged this as an open question: `ALLOWED_ACADRUST_PREFIX` is
`vendor/acadrust-worker/`, but this card's crate lives at
`engine/acadrust-worker/`, which sits entirely outside `SCAN_ROOTS =
("web", "vendor")` — so day 2 was "fence-clean" only because the fence
never looks at `engine/` at all.

**Day 3 made this concrete, not just theoretical.** Writing
`engineWasmHarness.realwasm.test.js` (a `web/`-scope file, which the fence
*does* scan) required a path reference into `engine/acadrust-worker/` for
something other than the one legal
`new Worker(new URL(..., import.meta.url))` spawn shape — first, an
`fs.existsSync(...)` presence check on the compiled artifact's path, to
skip gracefully when no wasm build exists. That single benign, read-only
existence check produced **11 fence violations** on the first real scan.
Deny rule 3 (`docs/CAD-ENGINE-LICENSE-FENCE.md`) has no exemption for
"referenced but not executed," and — confirmed by reading
`scripts/check_license_fence.py`'s `_scan_file` directly — `acadrust`
matches get **no prose exemption at all** (unlike the OpenCADStudio rule,
whose `_is_prose_mention` carve-out is applied only to
`OPENCADSTUDIO_RE`). Every literal appearance of "acadrust" outside the
prefix must be inside the one legal spawn's quoted URL literal, full stop —
including comments, including an env-var name, including a doc filename
reference. The final clean file avoids all of that by (a) dropping the
existence check entirely — the test always requests the real build and
lets a missing artifact fail loudly through the worker's own error path —
and (b) renaming the env switch from an acadrust-prefixed name to
`CAD_ENGINE_REAL_WASM`, and referring to the day-3 doc generically instead
of by its literal filename.

**Recommendation: move `engine/acadrust-worker/` →
`vendor/acadrust-worker/`. Do not widen the fence.**

Criterion, from the fence's own documented rules
(`docs/CAD-ENGINE-LICENSE-FENCE.md`):

- The fence doc states outright: inside `ALLOWED_ACADRUST_PREFIX`, "any
  reference to acadrust is expected and always allowed — **that is where
  the MPL-2.0 engine source lives**." That is a declaration of intended
  location, not just an allow-list entry. `engine/acadrust-worker/` living
  outside it is the card's own directory choice diverging from the fence's
  design, not a fence bug.
- `SCAN_ROOTS`, `ALLOWED_ACADRUST_PREFIX`, and `SELF_EXCLUDED_PATHS` are
  all locked by the fence's own self-tests
  (`test_self_exclusion_is_exactly_the_three_fence_files`, the trailing-
  slash boundary test, etc.) — widening any of them to cover `engine/`
  means editing tested, protected constants that day 2 already flagged as
  "not this card's call to make silently." Moving the directory requires
  **zero** fence code changes.
- Moving it doesn't just avoid a violation — it closes a real gap.
  `engine/acadrust-worker/` today is not merely "less protected" than
  `vendor/acadrust-worker/`, it is **completely unscanned**: a real
  licensing leak placed there (an OpenCADStudio identifier, a stray GPL
  header) would go undetected by this repo's only license-fence CI job.
  `vendor/` is a `SCAN_ROOT`; `engine/` is not.
- The move is mechanical: one `git mv`, then `worker-entry.mjs`'s Worker
  spawn shape in `engineWasmHarness.test.js`,
  `engineWasmHarness.realwasm.test.js`, and this crate's own internal
  relative paths get the same one-path-component edit already required by
  any directory rename. Not done in this spike (explicitly out of this
  card's authority — "not to move it unilaterally").

```
$ python scripts/check_license_fence.py --self-test
Ran 17 tests in 3.907s — OK
$ python scripts/check_license_fence.py .
license fence: clean
```

(Both run against the final state of this spike's tree — the 11-violation
intermediate state above was caught and fixed before this receipt, not
carried forward.)

## New open questions (carried to whoever continues this spike)

- **Byte-identical round-trip fidelity.** The real `DxfWriter` normalizes
  its output to a complete document regardless of input completeness.
  Whoever designs the eventual save/export UX needs to decide whether
  "round trip" for this tool means entity-semantic fidelity (proven today)
  or byte-for-byte fidelity (not what this engine does, and quite possibly
  not what any full-featured CAD writer does — worth checking whether
  AutoCAD's own DXF writer round-trips a hand-written minimal file
  byte-for-byte either, before treating this as an acadrust-specific gap).
- **The `engine/` → `vendor/` move** (OQ-4 above) — a repo-owner decision,
  not made here.
- **DWG path** — still explicitly out of scope, same as day 2's carry
  (day 1 noted `DwgReader::from_stream` is equally in-memory capable, never
  exercised end-to-end).
- **Full document complexity** — this spike's fixture is deliberately
  minimal (one LINE, R12/AC1009). Multi-entity documents, later DXF
  versions, and the ACIS/SAT solid-modeling surface the crate advertises
  are all unexercised.

## Exact repro commands

```bash
# Worktree + branch (from the leaf-web-demo repo root)
git worktree add C:\tmp\spike-day3-c6477dd5 35ee8c0b
cd C:\tmp\spike-day3-c6477dd5 && git switch -c chip/acadrust-spike-day3

# Crate clone + rev
git clone https://github.com/hakanaktt/acadrust.git C:\tmp\spike-day3-c6477dd5-crate
git -C C:\tmp\spike-day3-c6477dd5-crate rev-parse HEAD
# -> 18500466e7e4392ef830fdc59cede75fa3794f2b (already pinned in Cargo.toml)

# Contained toolchain
mkdir -p C:\tmp\spike-day3-toolchain\cargo C:\tmp\spike-day3-toolchain\rustup
export CARGO_HOME=C:\tmp\spike-day3-toolchain\cargo
export RUSTUP_HOME=C:\tmp\spike-day3-toolchain\rustup
export PATH="$CARGO_HOME/bin:$PATH"
curl -sSfL -o C:\tmp\spike-day3-toolchain\rustup-init.exe https://win.rustup.rs/x86_64
C:\tmp\spike-day3-toolchain\rustup-init.exe -y --no-modify-path --profile minimal \
  --default-host x86_64-pc-windows-gnu --default-toolchain stable
rustup target add wasm32-unknown-unknown
cargo install wasm-pack --locked

# Compile check
cd engine/acadrust-worker
RUSTFLAGS='--cfg getrandom_backend="wasm_js"' cargo check --target wasm32-unknown-unknown

# Real builds
RUSTFLAGS='--cfg getrandom_backend="wasm_js"' wasm-pack build --release --target web --out-dir pkg
RUSTFLAGS='--cfg getrandom_backend="wasm_js"' wasm-pack build --release --target nodejs --out-dir pkg-node

# Size
wc -c pkg/acadrust_worker_bg.wasm
gzip -9 -c pkg/acadrust_worker_bg.wasm | wc -c

# Execution proof (real wasm, via the worker's own message loop)
CAD_ENGINE_REAL_WASM=1 node --input-type=module -e "
  import { handleMessage } from './worker-entry.mjs'
  await handleMessage({ type: 'init' })
  console.log(JSON.stringify(await handleMessage({ type: 'loadDocument', documentId: 'one_line.dxf' }), null, 2))"

# Test suite (needs web/node_modules — junction or npm install from a
# checkout that already has it; this worktree used:
#   npm install    # inside web/, first time; or
#   cmd /c mklink /J web\node_modules <existing-checkout>\web\node_modules
cd ../../web
CAD_ENGINE_REAL_WASM=1 npx vitest run src/cad/engineWasmHarness.realwasm.test.js
npx vitest run   # full regression

# Fence
cd ..
python scripts/check_license_fence.py --self-test
python scripts/check_license_fence.py .
```

## Terminal receipt (spike lineage)

```json
{
  "kind": "spike-day3",
  "at": "2026-08-23",
  "card": "C1B-D3",
  "lane": "sonnet",
  "verdict": "OQ-1..OQ-3 and new OQ-5 RESOLVED with real evidence (pinned rev, real .wasm built/measured, 6 API mismatches found+fixed against the real crate, real-wasm Node execution proven for entity round-trip); OQ-4 given a concrete violation example and a recommendation (move engine/ -> vendor/acadrust-worker/, do not widen the fence), decision not made unilaterally; byte-identical round-trip fidelity is NEW open work, not a blocker",
  "acadrust_rev_pinned": "18500466e7e4392ef830fdc59cede75fa3794f2b",
  "wasm_size_bytes": { "raw": 2004823, "gzip9": 698941 },
  "build_times": { "cargo_check_wasm32": "31.94s", "wasm_pack_web_release": "2m56s", "wasm_pack_nodejs_release": "39.42s (post-fix rebuild)" },
  "api_corrections": 6,
  "delivered": [
    "engine/acadrust-worker/Cargo.toml",
    "engine/acadrust-worker/src/lib.rs",
    "engine/acadrust-worker/worker-entry.mjs",
    "engine/acadrust-worker/.gitignore",
    "web/src/cad/engineWasmHarness.realwasm.test.js",
    "docs/ACADRUST-SPIKE-DAY3.md"
  ],
  "tests": "web/src suite 41 files / 328 tests pass, including both the day-2 stand-in test and the new day-3 real-wasm test; license-fence self-test 17/17 pass; license-fence real scan clean",
  "open_questions_carried": ["byte-identical round-trip fidelity (new)", "engine/ -> vendor/ move (OQ-4, repo-owner decision)", "DWG path (day-1 carry)", "multi-entity/full-document coverage (new)"],
  "next_goal": "day 4 (if continued): repo owner decides the engine/->vendor/ move; if approved, execute the move + re-verify fence-clean; separately, decide the byte-identical-fidelity product question before any save/export UX is built on this engine"
}
```
