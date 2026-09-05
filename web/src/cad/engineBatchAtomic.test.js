// @vitest-environment node
//
// W4g-6: the worker's `batch` op on the REAL compiled engine (the wasm-pack
// pkg-node build), the contract the intersection verbs rest on: several steps
// in ONE turn with ONE write-back, and ATOMIC, so a step that refuses leaves
// the document byte-identical to what it was before the batch (the bytes
// before the first step are re-parsed), and a refused single create keeps the
// document held rather than dropping it. Runs the real worker module in a
// node subprocess, the device engineCreateRefusal uses (vite-node refuses to
// import a file outside web/).
//
// FENCE NOTE: the worker's vendored path is spelled ONLY inside the one
// literal `new Worker(new URL(...))` shape below; the on-disk path is derived
// from it through a throwaway Worker double.
import { execFileSync } from 'node:child_process'
import { existsSync, readdirSync } from 'node:fs'
import path from 'node:path'
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
const PKG_DIR = path.join(path.dirname(WORKER_PATH), 'pkg-node')
const GLUE = existsSync(PKG_DIR) ? readdirSync(PKG_DIR).find((name) => name.endsWith('_worker.js')) : null

// Two crossing lines and a circle, the fixture every batch below starts from.
const DXF = [
  '0', 'SECTION', '2', 'HEADER', '9', '$ACADVER', '1', 'AC1009', '0', 'ENDSEC',
  '0', 'SECTION', '2', 'ENTITIES',
  '0', 'LINE', '8', 'A', '10', '0.0', '20', '0.0', '30', '0.0', '11', '10.0', '21', '0.0', '31', '0.0',
  '0', 'LINE', '8', 'A', '10', '10.0', '20', '0.0', '30', '0.0', '11', '10.0', '21', '10.0', '31', '0.0',
  '0', 'CIRCLE', '8', 'A', '10', '0.0', '20', '0.0', '30', '0.0', '40', '5.0',
  '0', 'ENDSEC', '0', 'EOF',
].join('\n') + '\n'

const SCRIPT = [
  'import { createRequire } from "node:module"',
  'import { pathToFileURL } from "node:url"',
  'const [workerPath, gluePath, dxf] = process.argv.slice(1)',
  'const { handleMessage } = await import(pathToFileURL(workerPath).href)',
  'const engine = createRequire(import.meta.url)(gluePath)',
  'const bytes = new TextEncoder().encode(dxf)',
  'const out = {}',
  'const loaded = await handleMessage({ type: "loadDocument", documentId: "x.dxf", bytes }, engine)',
  'const ids = loaded.entities.map((e) => e.id)',
  'const [h, v, c] = ids',
  'const summary = (r) => ({ ok: r.ok, op: r.op, reason: r.reason ?? null, createdId: r.createdId ?? null, createdIds: r.createdIds ?? null, count: r.entityCount ?? null, entities: (r.entities || []).map((e) => ({ id: e.id, type: e.type, vertices: e.vertices, bulges: e.bulges, closed: e.closed, radius: e.radius, startDeg: e.startDeg, endDeg: e.endDeg })) })',
  // A fillet: both lines cut to their tangent points, one arc made, in one turn.
  'out.fillet = summary(await handleMessage({ type: "applyEdit", op: "batch", payload: { verb: "fillet", steps: [',
  '  { op: "setVertices", payload: { entityId: h, points: [0, 0, 8, 0], closed: false } },',
  '  { op: "setVertices", payload: { entityId: v, points: [10, 2, 10, 10], closed: false } },',
  '  { op: "createArc", payload: { cx: 8, cy: 2, radius: 2, startDeg: 270, endDeg: 0, layer: "A" } },',
  '] } }, engine))',
  'const afterFillet = out.fillet.entities',
  // An atomic refusal: the first step applies, the second refuses (a zero-length line), nothing sticks.
  'out.refused = summary(await handleMessage({ type: "applyEdit", op: "batch", payload: { verb: "trim", steps: [',
  '  { op: "setVertices", payload: { entityId: h, points: [0, 0, 4, 0], closed: false } },',
  '  { op: "setVertices", payload: { entityId: v, points: [3, 3, 3, 3], closed: false } },',
  '] } }, engine))',
  'out.afterRefusal = summary(await handleMessage({ type: "applyEdit", op: "setLayer", payload: { entityId: h, layer: "A" } }, engine))',
  // A circle trimmed to an arc: delete + create in one turn; the selection lands on the arc.
  'out.circle = summary(await handleMessage({ type: "applyEdit", op: "batch", payload: { verb: "trim", steps: [',
  '  { op: "delete", payload: { entityId: c } },',
  '  { op: "createArc", payload: { cx: 0, cy: 0, radius: 5, startDeg: 0, endDeg: 180, layer: "A" } },',
  '] } }, engine))',
  // An arc re-swept through setArc.
  'out.setArc = summary(await handleMessage({ type: "applyEdit", op: "setArc", payload: { entityId: out.circle.createdId, cx: 0, cy: 0, radius: 5, startDeg: 90, endDeg: 180 } }, engine))',
  // The bounds and the nesting refusal.
  'out.empty = summary(await handleMessage({ type: "applyEdit", op: "batch", payload: { verb: "trim", steps: [] } }, engine))',
  'out.tooMany = summary(await handleMessage({ type: "applyEdit", op: "batch", payload: { verb: "trim", steps: Array.from({ length: 5 }, () => ({ op: "setLayer", payload: { entityId: h, layer: "A" } })) } }, engine))',
  'out.nested = summary(await handleMessage({ type: "applyEdit", op: "batch", payload: { verb: "trim", steps: [{ op: "batch", payload: { steps: [] } }] } }, engine))',
  // A refused single create keeps the document held: the next edit still lands.
  'out.badCreate = summary(await handleMessage({ type: "applyEdit", op: "createLine", payload: { x1: 1, y1: 1, x2: 1, y2: 1, layer: "A" } }, engine))',
  'out.afterBadCreate = summary(await handleMessage({ type: "applyEdit", op: "setLayer", payload: { entityId: h, layer: "B" } }, engine))',
  // An op named after a prototype property is not a create and calls nothing.
  'out.proto = summary(await handleMessage({ type: "applyEdit", op: "constructor", payload: { entityId: h } }, engine))',
  'out.protoInBatch = summary(await handleMessage({ type: "applyEdit", op: "batch", payload: { verb: "trim", steps: [{ op: "hasOwnProperty", payload: { entityId: h } }] } }, engine))',
  // W4g-6d: a polyline's own corner fillet is ONE setVertices carrying a bulge; the projection reads it back
  // after the write + re-parse, a list of the wrong length is refused by the crate before anything changes,
  // and a straight rewrite with no list leaves every bulge at 0.
  'out.square = summary(await handleMessage({ type: "applyEdit", op: "createPolyline", payload: { points: [0, 0, 10, 0, 10, 10, 0, 10], closed: true, layer: "A" } }, engine))',
  'const sq = out.square.createdId',
  'out.cornerFillet = summary(await handleMessage({ type: "applyEdit", op: "batch", payload: { verb: "fillet", steps: [',
  '  { op: "setVertices", payload: { entityId: sq, points: [0, 0, 10, 0, 10, 8, 8, 10, 0, 10], closed: true, bulges: [0, 0, 0.414213562, 0, 0] } },',
  '] } }, engine))',
  'out.badBulges = summary(await handleMessage({ type: "applyEdit", op: "setVertices", payload: { entityId: sq, points: [0, 0, 10, 0, 10, 10], closed: true, bulges: [0, 0] } }, engine))',
  'out.straightAgain = summary(await handleMessage({ type: "applyEdit", op: "setVertices", payload: { entityId: sq, points: [0, 0, 10, 0, 10, 10], closed: true } }, engine))',
  'process.stdout.write(JSON.stringify({ ids, out }))',
].join('\n')

describe.skipIf(!GLUE)('the worker batch on the real engine', () => {
  it('applies several steps in one turn, refuses atomically, bounds the count, and keeps the document on a refused create', { timeout: 90_000 }, () => {
    const raw = execFileSync(process.execPath, ['--input-type=module', '-e', SCRIPT, WORKER_PATH, path.join(PKG_DIR, GLUE), DXF], {
      encoding: 'utf8',
      timeout: 90_000,
      maxBuffer: 64 * 1024 * 1024,
    })
    const { ids, out } = JSON.parse(raw)
    expect(ids).toHaveLength(3)
    const [h, v] = ids

    // W4g-6d: the corner fillet's bulge survives the engine's write + re-parse and comes back in the projection.
    expect(out.square.ok).toBe(true)
    expect(out.cornerFillet.ok).toBe(true)
    const rounded = out.cornerFillet.entities.find((e) => e.id === out.square.createdId)
    expect(rounded.type).toBe('LWPOLYLINE')
    expect(rounded.closed).toBe(true)
    expect(rounded.vertices).toEqual([[0, 0, 0], [10, 0, 0], [10, 8, 0], [8, 10, 0], [0, 10, 0]])
    expect(rounded.bulges).toHaveLength(5)
    expect(rounded.bulges[2]).toBeCloseTo(0.414213562, 9)
    expect(rounded.bulges.filter((b) => b === 0)).toHaveLength(4)
    expect(out.badBulges.ok).toBe(false)
    expect(out.badBulges.reason).toBe('bulges_not_per_vertex')
    const stillRounded = out.badBulges.entities?.find?.((e) => e.id === out.square.createdId)
    if (stillRounded) expect(stillRounded.vertices).toHaveLength(5)
    expect(out.straightAgain.ok).toBe(true)
    const straight = out.straightAgain.entities.find((e) => e.id === out.square.createdId)
    expect(straight.vertices).toHaveLength(3)
    expect(straight.bulges).toEqual([0, 0, 0])

    // The fillet: three steps, one reply, the arc selected, both lines cut.
    expect(out.fillet.ok).toBe(true)
    expect(out.fillet.op).toBe('batch')
    expect(out.fillet.count).toBe(4)
    expect(out.fillet.createdIds).toHaveLength(1)
    expect(out.fillet.createdId).toBe(out.fillet.createdIds[0])
    const byId = new Map(out.fillet.entities.map((e) => [e.id, e]))
    expect(byId.get(h).vertices).toEqual([[0, 0, 0], [8, 0, 0]])
    expect(byId.get(v).vertices).toEqual([[10, 2, 0], [10, 10, 0]])
    const arc = byId.get(out.fillet.createdId)
    expect(arc.type).toBe('ARC')
    expect(arc.radius).toBe(2)
    expect(arc.startDeg).toBeCloseTo(270, 9)
    expect(Math.abs(arc.endDeg) < 1e-9 || Math.abs(arc.endDeg - 360) < 1e-9).toBe(true)

    // Atomic: the refused batch names its step, and the FIRST step did not stick.
    expect(out.refused.ok).toBe(false)
    expect(out.refused.reason).toBe('step_1_setVertices:line_zero_length')
    expect(out.afterRefusal.ok).toBe(true)
    const held = new Map(out.afterRefusal.entities.map((e) => [e.id, e]))
    expect(held.get(h).vertices).toEqual([[0, 0, 0], [8, 0, 0]])
    expect(out.afterRefusal.count).toBe(4)

    // A circle to an arc: the circle gone, the arc selected.
    expect(out.circle.ok).toBe(true)
    expect(out.circle.count).toBe(4)
    expect(out.circle.entities.map((e) => e.type).filter((t) => t === 'CIRCLE')).toHaveLength(0)
    expect(out.circle.entities.find((e) => e.id === out.circle.createdId).type).toBe('ARC')
    // setArc, and the angles read back in degrees.
    expect(out.setArc.ok).toBe(true)
    const swept = out.setArc.entities.find((e) => e.id === out.circle.createdId)
    expect(swept.startDeg).toBeCloseTo(90, 9)
    expect(swept.endDeg).toBeCloseTo(180, 9)

    // The bounds, before any step runs.
    expect(out.empty).toMatchObject({ ok: false, reason: 'batch_empty' })
    expect(out.tooMany).toMatchObject({ ok: false, reason: 'batch_too_many_steps' })
    expect(out.nested).toMatchObject({ ok: false, reason: 'step_0_batch_nested' })

    // A refused create is a refusal, not a lost document.
    expect(out.badCreate).toMatchObject({ ok: false, op: 'createLine', reason: 'line_zero_length' })
    expect(out.afterBadCreate.ok).toBe(true)
    expect(out.afterBadCreate.count).toBe(4)
    // The op string off the boundary never reaches a prototype slot of the
    // create table: `constructor` is an unknown op, in a batch too.
    expect(out.proto).toMatchObject({ ok: false, reason: 'unknown_op:constructor' })
    expect(out.protoInBatch).toMatchObject({ ok: false, reason: 'step_0_hasOwnProperty:unknown_op:hasOwnProperty' })
  })
})
