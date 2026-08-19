// @vitest-environment node
//
// Node, not the workspace-default jsdom: jsdom serves `import.meta.url` as
// an http: URL from Vite's dev transform, and Node's dynamic import() can't
// load an http: URL (ERR_UNSUPPORTED_ESM_URL_SCHEME) — this override is
// file-local, the global jsdom default in vitest.config.js is untouched.
// This suite exercises message-schema plumbing and Node-loadable modules
// only, no DOM, so Node is also the more honest environment for it.
//
// Day-2 CAD engine WASM spike: proves a wasm-bindgen-shaped engine worker
// can be loaded inside the isolated worker boundary (card C1-5's
// EngineBoundary, unmodified — this file only imports it, per this card's
// file boundary), and drives a browser-shaped DXF round trip through that
// boundary's existing message schema. Day 1's receipt: the MPL-2.0 engine
// crate round-trips a one-LINE DXF natively and compiles clean for wasm32;
// day 2 closes the remaining half (see this repo's day-2 spike doc under
// docs/ for the full spike lineage receipt).
//
// No real Worker exists in jsdom (see engineBoundary.test.js's own
// `delete globalThis.Worker` / `globalThis.Worker = vi.fn()` pattern) so
// this mocks the global the same way. The mock runs the real worker module
// in a genuine `node` subprocess per message (see runInWorkerProcess below)
// instead of importing it into this vitest/vite-node process directly:
// vite-node's own module graph refuses to load a file outside its `web/`
// serving root (an ERR_MODULE_NOT_FOUND "does the file exist?" even though
// it does — Vite's default `server.fs.allow` search stops at `web/`, and
// widening it needs a vite.config.js/vitest.config.js edit, both outside
// this card's file boundary). A plain `node` subprocess has no such
// restriction and loads the real file:// module untouched — this is also
// exactly day 1's own stated day-2 plan ("run the round trip in Node ... to
// prove the module actually executes", not a workaround invented here).
// Nothing about the round-trip result below is asserted against a
// hand-duplicated copy of worker-entry.mjs/bindings.mjs's logic.
import { execFileSync } from 'node:child_process'
import { fileURLToPath, pathToFileURL } from 'node:url'

import { afterEach, describe, expect, it, vi } from 'vitest'

import { EngineBoundary } from './engineWorker.js'

// The engine worker module path is spelled ONLY inside this literal
// `new Worker(new URL(...))` call — the one shape this repo's license fence
// (docs/CAD-ENGINE-LICENSE-FENCE.md, deny rule 3) allows a web/ file to
// reference the day-2 wasm engine worker by path. Do not import that path
// any other way from this file or anywhere else under web/.
function createEngineWorker() {
  return new Worker(
    new URL('../../../engine/acadrust-worker/worker-entry.mjs', import.meta.url),
    { type: 'module' },
  )
}

// Runs the worker module's handleMessage(raw) in a fresh `node` process:
// module path and message travel in, not through this process's own module
// graph, so vite-node never sees the cross-directory specifier.
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
  })
  return JSON.parse(stdout)
}

// Test double standing in for the browser's real dedicated-Worker transport.
// Its constructor receives the exact URL createEngineWorker() built above.
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

describe('CAD engine wasm worker (day-2 spike)', () => {
  afterEach(() => {
    delete globalThis.Worker
  })

  it('never instantiates the wasm engine worker with cad_edit off (negative control)', () => {
    globalThis.Worker = vi.fn()
    const boundary = new EngineBoundary({ flags: { cad_edit: false }, createWorker: createEngineWorker })

    const started = boundary.start()

    expect(started).toBe(false)
    expect(boundary.instantiated).toBe(false)
    expect(globalThis.Worker).not.toHaveBeenCalled()
  })

  it('drives a browser-shaped DXF round trip through the boundary message schema', async () => {
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

    // Entity comparison: the same one-LINE fixture day 1 used, parsed twice
    // (once from the original bytes, once from the round-tripped bytes) and
    // required to agree on the entity data exactly.
    const expectedLine = { type: 'LINE', layer: '0', start: [0, 0, 0], end: [100, 50, 0] }
    expect(loaded.roundTrip.entityCount).toBe(1)
    expect(loaded.roundTrip.firstEntity).toEqual(expectedLine)
    expect(loaded.roundTrip.reparsedFirstEntity).toEqual(expectedLine)

    // Byte comparison: parse -> write -> compare against the original bytes.
    expect(loaded.roundTrip.bytesIdentical).toBe(true)
    expect(loaded.roundTrip.writtenByteLength).toBe(loaded.roundTrip.originalByteLength)

    // The boundary's own validate-or-drop contract never fired: every
    // message on both sides was well-formed against the existing schema.
    expect(boundary.droppedCount).toBe(0)
  })

  it('drops an unknown documentId as an error message, never throwing into the boundary', async () => {
    globalThis.Worker = RecordingBrowserWorker
    const boundary = new EngineBoundary({ flags: { cad_edit: true }, createWorker: createEngineWorker })
    const messages = []
    boundary.onMessage((message) => messages.push(message))

    boundary.start()
    expect(() => boundary.post({ type: 'loadDocument', documentId: 'missing.dxf' })).not.toThrow()

    await vi.waitFor(() => {
      if (messages.length < 1) throw new Error('waiting for worker reply')
    })

    expect(messages[0]).toEqual({ type: 'error', message: 'unknown_document:missing.dxf' })
    expect(boundary.droppedCount).toBe(0)
  })
})
