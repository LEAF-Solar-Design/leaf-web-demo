// The isolated worker's message loop for the day-2 acadrust wasm spike.
// Speaks the SAME message-schema vocabulary web/src/cad/engineWorker.js's
// EngineBoundary already validates both directions (REQUEST_SCHEMA /
// RESPONSE_SCHEMA: 'init' -> 'ready', 'loadDocument' -> 'documentLoaded'),
// so it plugs into the existing boundary without that file needing an edit.
// Loaded from the main bundle only via the one shape this repo's license
// fence allows a non-vendor file to reference this module by path
// (docs/CAD-ENGINE-LICENSE-FENCE.md, deny rule 3):
//   new Worker(new URL('.../worker-entry.mjs', import.meta.url))
// — see web/src/cad/engineWasmHarness.test.js for that exact call site.
//
// A real deployment would wire this into engineWorker.js's own worker-side
// branch (today an echo loop with no engine); that wiring is future work —
// this card's file boundary excludes editing engineWorker.js.
//
// Day 3: env switch (CAD_ENGINE_REAL_WASM=1) loads the REAL compiled
// wasm-bindgen output (pkg-node/acadrust_worker.js, produced by
//   RUSTFLAGS='--cfg getrandom_backend="wasm_js"' \
//     wasm-pack build --release --target nodejs engine/acadrust-worker --out-dir pkg-node
// — see docs/ACADRUST-SPIKE-DAY3.md) instead of the JS-native stand-in
// (bindings.mjs). Both expose the identical 1:1 surface (parseDxf/writeDxf/
// bytesEqual, `parsed.entities` as an array-like getter) by construction —
// see src/lib.rs's module doc — so this is a lazy module swap, not a
// rewrite of the message loop below. Lazy (dynamic import inside getEngine,
// not a static top-level import) so a workdir that never built pkg-node/
// still runs the default stand-in path without error.

import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const USE_REAL_WASM = process.env.CAD_ENGINE_REAL_WASM === '1'

let enginePromise = null
function getEngine() {
  if (enginePromise) return enginePromise
  enginePromise = USE_REAL_WASM
    ? import('./pkg-node/acadrust_worker.js')
    : import('./bindings.mjs')
  return enginePromise
}

// Fixture lookup by documentId. A real browser build would fetch or inline
// these instead of node:fs; node:fs is this Node-run spike's stand-in for
// "load bytes from somewhere" (day 1 loaded the same fixture the same way
// for its native round-trip test — see spike-harness/fixtures/one_line.dxf
// in the day-1 postmortem).
const FIXTURES = {
  'one_line.dxf': path.join(HERE, 'fixtures', 'one_line.dxf'),
}

export async function handleMessage(raw) {
  if (!raw || typeof raw !== 'object') {
    return { type: 'error', message: 'bad_message' }
  }

  if (raw.type === 'init') {
    return { type: 'ready' }
  }

  if (raw.type === 'loadDocument') {
    const fixturePath = FIXTURES[raw.documentId]
    if (!fixturePath) {
      return { type: 'error', message: `unknown_document:${raw.documentId}` }
    }
    const { parseDxf, writeDxf, bytesEqual } = await getEngine()
    const original = new Uint8Array(readFileSync(fixturePath))
    const parsed = parseDxf(original)
    const written = writeDxf(parsed)
    const reparsed = parseDxf(written)
    return {
      type: 'documentLoaded',
      documentId: raw.documentId,
      roundTrip: {
        entityCount: parsed.entities.length,
        firstEntity: parsed.entities[0] ?? null,
        reparsedFirstEntity: reparsed.entities[0] ?? null,
        originalByteLength: original.length,
        writtenByteLength: written.length,
        bytesIdentical: bytesEqual(original, written),
      },
    }
  }

  return { type: 'error', message: `unhandled_type:${raw.type}` }
}
