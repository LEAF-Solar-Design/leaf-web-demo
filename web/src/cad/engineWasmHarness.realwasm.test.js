// @vitest-environment node
//
// Day-3 REAL-WASM variant of engineWasmHarness.test.js. Same fixture, same
// EngineBoundary message schema, same RecordingBrowserWorker double — the
// only difference from the day-2 file is one extra environment variable
// (CAD_ENGINE_REAL_WASM=1) forwarded into the child `node` process, which
// the isolated worker module's own env switch reads to dynamically import
// its real compiled build (produced by the day-3 wasm-pack step, see
// this repo's day-3 CAD engine spike doc for the exact build commands and artifact
// path) instead of the JS-native stand-in it uses by default.
//
// FENCE NOTE (this is itself an OQ-4 finding, not incidental): this file
// deliberately never spells the crate name outside the ONE literal
// `new Worker(new URL(...))` call below — the single shape
// docs/CAD-ENGINE-LICENSE-FENCE.md's deny rule 3 allows a web/ file to
// reference the isolated engine worker by path. An earlier draft of this
// file also did an `fs.existsSync(...)` presence check on the compiled
// artifact's path before deciding whether to run — that is a completely
// benign read-only check, and it STILL failed the fence's real scan (11
// violations of the fence's own outside-prefix rule), because deny rule 3
// has no exemption for "referenced but not executed" — literally any
// content match outside the one Worker-spawn shape is red, prose or code,
// existence-check or import. Dropped rather than special-cased: this file
// always requests the real build and lets a missing artifact fail loudly
// through the worker's own message-schema error path instead.
//
// OPT-IN, and that is load-bearing: the compiled artifact is produced by a
// wasm-pack build that needs a Rust toolchain, so it exists on a developer
// machine that ran the day-3 build and NOWHERE else - not on a CI runner, not
// in a fresh clone. Running unconditionally made every unrelated PR's gate red
// the moment this file landed on main (measured: gate-shard-2 web-vitest, 331
// tests, 1 failed, "Command failed: node --input-type=module"). The env switch
// this test already forwards to its child is therefore ALSO its own gate, so
// the assertion only runs where the artifact can exist. Gating on the artifact
// path instead would trip the fence (see the note above); gating on the switch
// spells no crate name, so it does not.
import { execFileSync } from 'node:child_process'
import { fileURLToPath, pathToFileURL } from 'node:url'

import { afterEach, describe, expect, it, vi } from 'vitest'

import { EngineBoundary } from './engineWorker.js'

// The engine worker module path is spelled ONLY inside this literal
// `new Worker(new URL(...))` call — the one shape this repo's license fence
// (docs/CAD-ENGINE-LICENSE-FENCE.md, deny rule 3) allows a web/ file to
// reference the day-2/day-3 wasm engine worker by path. Do not import that
// path any other way from this file or anywhere else under web/.
function createEngineWorker() {
  return new Worker(
    new URL('../../../vendor/acadrust-worker/worker-entry.mjs', import.meta.url),
    { type: 'module' },
  )
}

function runInWorkerProcess(modulePath, message) {
  const script = [
    `import { handleMessage } from ${JSON.stringify(pathToFileURL(modulePath).href)}`,
    `import { readFileSync } from 'node:fs'`,
    `const raw = JSON.parse(readFileSync(0, 'utf8'))`,
    `const response = await handleMessage(raw)`,
    `process.stdout.write(JSON.stringify(response ?? null))`,
  ].join('\n')
  const stdout = execFileSync(process.execPath, ['--input-type=module', '-e', script], {
    input: JSON.stringify(message),
    encoding: 'utf8',
    // The one line that differs from the day-2 stand-in test: propagate the
    // real-build env switch into the spawned node subprocess. execFileSync
    // inherits process.env by default; this only ADDS the one new key.
    env: { ...process.env, CAD_ENGINE_REAL_WASM: '1' },
  })
  return JSON.parse(stdout)
}

class RecordingBrowserWorker {
  constructor(url) {
    this._listeners = []
    this._modulePath = fileURLToPath(url)
  }

  addEventListener(type, cb) {
    if (type === 'message') this._listeners.push(cb)
  }

  removeEventListener() {}

  postMessage(data) {
    queueMicrotask(() => {
      const response = runInWorkerProcess(this._modulePath, data)
      if (response) {
        for (const cb of this._listeners) cb({ data: response })
      }
    })
  }

  terminate() {}
}

// Set CAD_ENGINE_REAL_WASM=1 to run this, after building the real artifact per
// the day-3 spike doc. Unset, the suite reports it as skipped, never as passed.
const REAL_BUILD_REQUESTED = process.env.CAD_ENGINE_REAL_WASM === '1'

describe.skipIf(!REAL_BUILD_REQUESTED)('CAD engine real-build worker (day-3 spike, OQ-2/OQ-3/OQ-5)', () => {
  afterEach(() => {
    delete globalThis.Worker
  })

  it('drives a browser-shaped DXF round trip through the boundary message schema, against the real compiled engine build', async () => {
    globalThis.Worker = RecordingBrowserWorker
    const boundary = new EngineBoundary({ flags: { cad_edit: true }, createWorker: createEngineWorker })
    const messages = []
    boundary.onMessage((message) => messages.push(message))

    expect(boundary.start()).toBe(true)
    expect(boundary.post({ type: 'init' })).toBe(true)
    expect(boundary.post({ type: 'loadDocument', documentId: 'one_line.dxf' })).toBe(true)

    await vi.waitFor(() => {
      if (messages.length < 2) throw new Error('waiting for worker replies')
    })

    expect(messages[0]).toEqual({ type: 'ready' })

    const loaded = messages[1]
    expect(loaded.type).toBe('documentLoaded')
    expect(loaded.documentId).toBe('one_line.dxf')

    // Entity comparison against the real compiled engine's parser/writer —
    // the load-bearing proof the real build round-trips the day-1 fixture's
    // single LINE entity correctly (see the day-3 spike doc's "First
    // real-wasm run" for the pre-fix failure this assertion caught:
    // entities marshalled as an empty `{}` before the serde_wasm_bindgen
    // json_compatible fix in the engine crate's src/lib.rs).
    const expectedLine = { type: 'LINE', layer: '0', start: [0, 0, 0], end: [100, 50, 0] }
    expect(loaded.roundTrip.entityCount).toBe(1)
    expect(loaded.roundTrip.firstEntity).toEqual(expectedLine)
    expect(loaded.roundTrip.reparsedFirstEntity).toEqual(expectedLine)

    // Byte comparison: DELIBERATELY NOT asserted as identical here. The
    // real engine's DXF writer emits a complete document (default table/
    // object/class sections) even for a minimal R12 input, so
    // writtenByteLength is real and far larger than originalByteLength —
    // see the day-3 spike doc for the exact byte counts. This is a
    // genuine behavioral difference from the JS stand-in's minimal writer,
    // not a wrapper bug; the entity-level assertions above are the
    // correctness oracle for the real engine, not byte identity.
    expect(loaded.roundTrip.originalByteLength).toBe(140)
    expect(loaded.roundTrip.writtenByteLength).toBeGreaterThan(loaded.roundTrip.originalByteLength)

    expect(boundary.droppedCount).toBe(0)
  })
})
