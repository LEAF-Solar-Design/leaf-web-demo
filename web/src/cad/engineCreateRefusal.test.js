// @vitest-environment node
//
// W4d Draw group, the stand-in contract: an engine that lacks a create
// method must be REFUSED explicitly by the browser worker (a typed reason
// through the boundary), never a TypeError read by the store as a crash and
// never a pretend success. Runs the REAL worker module in a genuine node
// subprocess with a scripted engine, the same device engineWasmHarness uses
// (vite-node refuses to import a file outside web/).
//
// FENCE NOTE: the worker's vendored path is spelled ONLY inside the one
// literal `new Worker(new URL(...))` shape below; the on-disk path is
// derived from it through a throwaway Worker double.
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

function createEditorWorker() {
  return new Worker(
    new URL('../../../vendor/acadrust-worker/worker-browser.mjs', import.meta.url),
    { type: 'module' },
  )
}

function captureWorkerPath() {
  class CapturingWorker {
    constructor(url) {
      const u = url instanceof URL ? url : new URL(String(url))
      CapturingWorker.captured = u.protocol === 'file:'
        ? fileURLToPath(u)
        : decodeURIComponent(u.pathname.replace(/^\/@fs\//, '/').replace(/^\/(?=[A-Za-z]:)/, ''))
    }
  }
  const previous = globalThis.Worker
  globalThis.Worker = CapturingWorker
  try {
    createEditorWorker()
  } finally {
    if (previous === undefined) delete globalThis.Worker
    else globalThis.Worker = previous
  }
  return CapturingWorker.captured
}

const WORKER_PATH = captureWorkerPath()

// A scripted engine with parse/write but NO create surface: the shape of the
// JS stand-in (bindings.mjs) once a document is held.
const SCRIPT = [
  'import { pathToFileURL } from "node:url"',
  'const { handleMessage } = await import(pathToFileURL(process.argv[1]).href)',
  'const calls = []',
  'const doc = {',
  '  editableEntities: () => [',
  '    { index: 0, handle: 7, type: "LINE", layer: "0", closed: false, editable: true, vertices: [[0,0],[1,1]] },',
  '    { index: 1, handle: 9, type: "LINE", layer: "0", closed: false, editable: true, vertices: [[2,2],[3,3]] },',
  '  ],',
  '  deleteEntity: (index) => { calls.push(["deleteEntity", index]) },',
  '}',
  'const engine = { parseDxf: () => doc, writeDxf: () => new Uint8Array([48, 10]) }',
  'const loaded = await handleMessage({ type: "loadDocument", documentId: "x.dxf", bytes: new Uint8Array([48]) }, engine)',
  'const out = {}',
  'for (const op of ["createLine", "createCircle", "createArc", "createPolyline"]) {',
  '  out[op] = await handleMessage({ type: "applyEdit", op, payload: { x1: 0, y1: 0, x2: 1, y2: 1 } }, engine)',
  '}',
  'out.edit = await handleMessage({ type: "applyEdit", op: "delete", payload: { entityId: "9" } }, engine)',
  'out.unknown = await handleMessage({ type: "applyEdit", op: "delete", payload: { entityId: "1" } }, engine)',
  'const unsafeDoc = { editableEntities: () => [{ index: 0, handle: 9007199254740992, type: "LINE", layer: "0", closed: false, editable: true, vertices: [[0,0],[1,1]] }]}',
  'const unsafeEngine = { parseDxf: () => unsafeDoc, writeDxf: () => new Uint8Array([48, 10]) }',
  'out.unsafe = await handleMessage({ type: "loadDocument", documentId: "unsafe.dxf", bytes: new Uint8Array([48]) }, unsafeEngine)',
  'process.stdout.write(JSON.stringify({ loaded: loaded, out, calls }))',
].join('\n')

describe('the browser worker refuses create ops an engine cannot perform', () => {
  // A real node child under host load: the bound is generous because a
  // MISSING reply still fails, only later (the 5 s default is a flake
  // generator when the full suite runs beside a build).
  it('names the missing create explicitly, per op, and leaves ordinary edits untouched', { timeout: 60_000 }, () => {
    const raw = execFileSync(process.execPath, ['--input-type=module', '-e', SCRIPT, WORKER_PATH], {
      encoding: 'utf8',
      timeout: 60_000,
    })
    const { loaded, out, calls } = JSON.parse(raw)
    expect(loaded.type).toBe('documentLoaded')
    // The surface's ids are HANDLES, not indexes.
    expect(loaded.entities.map((e) => e.id)).toEqual(['7', '9'])
    for (const op of ['createLine', 'createCircle', 'createArc', 'createPolyline']) {
      expect(out[op]).toEqual({ type: 'editApplied', op, ok: false, reason: `engine_lacks_create:${op}` })
    }
    // The stand-in still cannot re-parse its own write into an editable doc
    // here (a scripted parse), but the EDIT path reached the wrapper method
    // rather than a create refusal: the two dispatch arms are distinct.
    expect(out.edit.op).toBe('delete')
    expect(out.edit.ok).toBe(true)
    // Handle 9 resolved to index 1 at dispatch time: a renumbering edit can
    // never redirect the next edit onto a neighbour.
    expect(calls).toEqual([['deleteEntity', 1]])
    // "1" is a stale INDEX, not a handle: refused, never a guess.
    expect(out.unknown).toEqual({ type: 'editApplied', op: 'delete', ok: false, reason: 'bad_entity_id' })
    expect(out.unsafe).toEqual({ type: 'error', message: 'handle_precision_lost' })
  })
})
