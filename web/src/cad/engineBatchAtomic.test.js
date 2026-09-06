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
import { engineIntake, hexHandle } from '../cadedit/engineIntake.js'
import { diffPlan } from '../cadedit/mutationDiff.js'

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
const PKG_NAMES = existsSync(PKG_DIR) ? readdirSync(PKG_DIR) : []
const GLUE = PKG_NAMES.includes('engine.js') ? 'engine.js' : PKG_NAMES.find((name) => name.endsWith('_worker.js'))

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
  'const summary = (r) => ({ ok: r.ok, op: r.op, reason: r.reason ?? null, createdId: r.createdId ?? null, createdIds: r.createdIds ?? null, count: r.entityCount ?? null, entities: (r.entities || []).map((e) => ({ id: e.id, type: e.type, layer: e.layer, editable: e.editable, vertices: e.vertices, bulges: e.bulges, closed: e.closed, radius: e.radius, startDeg: e.startDeg, endDeg: e.endDeg, majorAxis: e.majorAxis, ratio: e.ratio })) })',
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
  // W4g-4b: a POINT and an ELLIPSE through the worker's create table; the projection carries the
  // ellipse's axis (relative) and ratio after the engine's write + re-parse; MATCHPROP is one setLayer
  // step in a batch; the crate's refusals reach the boundary as codes.
  'out.point = summary(await handleMessage({ type: "applyEdit", op: "createPoint", payload: { x: 3, y: 4, layer: "P" } }, engine))',
  'out.ellipse = summary(await handleMessage({ type: "applyEdit", op: "createEllipse", payload: { cx: 10, cy: 0, ax: 5, ay: 0, ratio: 0.5, layer: "E" } }, engine))',
  'out.badEllipse = summary(await handleMessage({ type: "applyEdit", op: "createEllipse", payload: { cx: 10, cy: 0, ax: 0, ay: 0, ratio: 0.5, layer: "E" } }, engine))',
  'out.badRatio = summary(await handleMessage({ type: "applyEdit", op: "createEllipse", payload: { cx: 10, cy: 0, ax: 5, ay: 0, ratio: 2, layer: "E" } }, engine))',
  'out.movedPoint = summary(await handleMessage({ type: "applyEdit", op: "move", payload: { entityId: out.point.createdId, dx: 1, dy: 1 } }, engine))',
  'out.matched = summary(await handleMessage({ type: "applyEdit", op: "batch", payload: { verb: "matchprop", steps: [',
  '  { op: "setLayer", payload: { entityId: out.point.createdId, layer: "E" } },',
  '] } }, engine))',
  // W4g-6e: a created polyline carries its bulges through the worker's create table; the projection reads
  // them back after the write + re-parse; a list of the wrong length is refused by the crate and creates nothing.
  'out.curved = summary(await handleMessage({ type: "applyEdit", op: "createPolyline", payload: { points: [0, 0, 10, 0, 10, 10], closed: false, layer: "A", bulges: [1, 0, 0] } }, engine))',
  'out.badCreateBulges = summary(await handleMessage({ type: "applyEdit", op: "createPolyline", payload: { points: [0, 0, 10, 0, 10, 10], closed: false, layer: "A", bulges: [1] } }, engine))',
  'out.afterBadCreateBulges = summary(await handleMessage({ type: "applyEdit", op: "createLine", payload: { x1: 50, y1: 50, x2: 60, y2: 50, layer: "A" } }, engine))',
  // W4g-6e (record 0b): the worker honours a typed array, refuses a hole through the crate, and refuses
  // a value that is not a list, creating nothing in either refusal.
  'out.typedCurved = summary(await handleMessage({ type: "applyEdit", op: "createPolyline", payload: { points: [0, 0, 10, 0, 10, 10], closed: false, layer: "A", bulges: new Float64Array([1, 0, 0]) } }, engine))',
  'out.typedShort = summary(await handleMessage({ type: "applyEdit", op: "createPolyline", payload: { points: [0, 0, 10, 0, 10, 10], closed: false, layer: "A", bulges: new Float64Array([1]) } }, engine))',
  'out.holeBulge = summary(await handleMessage({ type: "applyEdit", op: "createPolyline", payload: { points: [0, 0, 10, 0, 10, 10], closed: false, layer: "A", bulges: [1, , 0] } }, engine))',
  'out.notAList = summary(await handleMessage({ type: "applyEdit", op: "createPolyline", payload: { points: [0, 0, 10, 0, 10, 10], closed: false, layer: "A", bulges: "1 0 0" } }, engine))',
  'out.afterRefusals = summary(await handleMessage({ type: "applyEdit", op: "createLine", payload: { x1: 70, y1: 70, x2: 80, y2: 70, layer: "A" } }, engine))',
  // W4g-6e (record 4): the worker reads bulges strictly: a boolean or null is not a number, a DataView is not a list.
  'out.boolBulge = summary(await handleMessage({ type: "applyEdit", op: "createPolyline", payload: { points: [0, 0, 10, 0], closed: false, layer: "A", bulges: [true, null] } }, engine))',
  'out.viewBulge = summary(await handleMessage({ type: "applyEdit", op: "createPolyline", payload: { points: [0, 0, 10, 0], closed: false, layer: "A", bulges: new DataView(new ArrayBuffer(16)) } }, engine))',
  'out.afterStrict = summary(await handleMessage({ type: "applyEdit", op: "createLine", payload: { x1: 90, y1: 90, x2: 95, y2: 90, layer: "A" } }, engine))',
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

    // W4g-4b: POINT and ELLIPSE are real entities to the engine, editable, with the ellipse's axis and ratio
    // in the projection; MATCHPROP's one-step batch relayers the destination; the refusals name their codes.
    expect(out.point.ok).toBe(true)
    const madePoint = out.point.entities.find((e) => e.id === out.point.createdId)
    expect(madePoint).toMatchObject({ type: 'POINT', layer: 'P', editable: true, vertices: [[3, 4, 0]], majorAxis: null, ratio: null })
    expect(out.ellipse.ok).toBe(true)
    const madeEllipse = out.ellipse.entities.find((e) => e.id === out.ellipse.createdId)
    expect(madeEllipse).toMatchObject({ type: 'ELLIPSE', layer: 'E', editable: true, vertices: [[10, 0, 0]], majorAxis: [5, 0], ratio: 0.5 })
    expect(out.badEllipse.ok).toBe(false)
    expect(out.badEllipse.reason).toBe('ellipse_axis_zero')
    expect(out.badRatio.ok).toBe(false)
    expect(out.badRatio.reason).toBe('ellipse_ratio_out_of_range')
    expect(out.movedPoint.ok).toBe(true)
    expect(out.movedPoint.entities.find((e) => e.id === out.point.createdId).vertices).toEqual([[4, 5, 0]])
    expect(out.matched.ok).toBe(true)
    expect(out.matched.op).toBe('batch')
    expect(out.matched.entities.find((e) => e.id === out.point.createdId).layer).toBe('E')

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

    // W4g-6e: the created polyline's bulges come back from the projection; a bad list creates nothing.
    expect(out.curved.ok).toBe(true)
    const curved = out.curved.entities.find((e) => e.id === out.curved.createdId)
    expect(curved.type).toBe('LWPOLYLINE')
    expect(curved.closed).toBe(false)
    expect(curved.vertices).toEqual([[0, 0, 0], [10, 0, 0], [10, 10, 0]])
    expect(curved.bulges).toHaveLength(3)
    expect(curved.bulges[0]).toBeCloseTo(1, 12)
    expect(curved.bulges[1]).toBe(0)
    expect(curved.bulges[2]).toBe(0)
    expect(out.badCreateBulges.ok).toBe(false)
    expect(out.badCreateBulges.reason).toBe('bulges_not_per_vertex')
    expect(out.afterBadCreateBulges.ok).toBe(true)
    expect(out.afterBadCreateBulges.entities).toHaveLength(out.curved.entities.length + 1)
    // W4g-6e (record 0b): a typed array is honoured; a short typed array, a hole and a non-list are refused with no create.
    expect(out.typedCurved.ok).toBe(true)
    const typed = out.typedCurved.entities.find((e) => e.id === out.typedCurved.createdId)
    expect(typed.bulges).toHaveLength(3)
    expect(typed.bulges[0]).toBeCloseTo(1, 12)
    expect(out.typedShort.ok).toBe(false)
    expect(out.typedShort.reason).toBe('bulges_not_per_vertex')
    expect(out.holeBulge.ok).toBe(false)
    expect(out.holeBulge.reason).toBe('bulge_not_finite')
    expect(out.notAList.ok).toBe(false)
    expect(out.notAList.reason).toBe('bulges_not_a_list')
    expect(out.afterRefusals.ok).toBe(true)
    expect(out.afterRefusals.entities).toHaveLength(out.typedCurved.entities.length + 1)
    // W4g-6e (record 4): strict bulge reading at the worker boundary.
    expect(out.boolBulge.ok).toBe(false)
    expect(out.boolBulge.reason).toBe('bulge_not_finite')
    expect(out.viewBulge.ok).toBe(false)
    expect(out.viewBulge.reason).toBe('bulges_not_a_list')
    expect(out.afterStrict.ok).toBe(true)
    expect(out.afterStrict.entities).toHaveLength(out.afterRefusals.entities.length + 1)
  })
})

const BLOCK_DXF = [
  '0', 'SECTION', '2', 'HEADER', '9', '$ACADVER', '1', 'AC1027', '0', 'ENDSEC',
  '0', 'SECTION', '2', 'BLOCKS',
  '0', 'BLOCK', '5', '40', '8', '0', '2', 'B', '70', '0', '10', '1', '20', '2', '30', '0',
  '0', 'LINE', '5', '100', '8', '0', '10', '1', '20', '2', '30', '0', '11', '4', '21', '2', '31', '0',
  '0', 'CIRCLE', '5', '101', '8', '0', '10', '1', '20', '2', '30', '0', '40', '1',
  '0', 'ENDBLK', '5', '41', '8', '0', '0', 'ENDSEC',
  '0', 'SECTION', '2', 'ENTITIES',
  '0', 'INSERT', '5', '500', '8', '0', '2', 'B', '10', '10', '20', '20', '30', '0',
  '41', '2', '42', '3', '43', '1', '50', '90',
  '0', 'ENDSEC', '0', 'EOF',
].join('\n') + '\n'

const BLOCK_SCRIPT = [
  'import { createRequire } from "node:module"',
  'import { pathToFileURL } from "node:url"',
  'const [workerPath, gluePath, dxf] = process.argv.slice(1)',
  'const engine = createRequire(import.meta.url)(gluePath)',
  'const { handleMessage } = await import(pathToFileURL(workerPath).href)',
  'const bytes = new TextEncoder().encode(dxf)',
  'const projection = (entities, blocks = entities?.blocks) => ({ entities: Array.from(entities || []), blocks })',
  'const reply = (r) => ({ type: r.type, op: r.op, ok: r.ok, reason: r.reason, refusal: r.refusal, writable: r.writable, blockBasePatched: r.blockBasePatched, ...projection(r.entities, r.blocks) })',
  'const doc = engine.parseDxf(bytes)',
  'const direct = projection(doc.editableEntities())',
  'const written = engine.writeDxf(doc)',
  'const patched = doc.blockBasePatched',
  'const back = engine.parseDxf(written)',
  'const roundtrip = projection(back.editableEntities())',
  'doc.free(); back.free()',
  'const loaded = await handleMessage({ type: "loadDocument", documentId: "blocks.dxf", bytes }, engine)',
  'const initial = reply(loaded)',
  'const out = { direct, patched, roundtrip, initial }',
  'for (const op of ["move", "copy", "delete"]) {',
  '  out[op] = reply(await handleMessage({ type: "applyEdit", op, payload: { entityId: "1280", dx: 1, dy: 0 } }, engine))',
  '}',
  'out.batch = reply(await handleMessage({ type: "applyEdit", op: "batch", payload: { steps: [',
  '  { op: "createLine", payload: { x1: 0, y1: 0, x2: 1, y2: 0, layer: "0" } },',
  '  { op: "move", payload: { entityId: "1280", dx: 1, dy: 0 } },',
  '] } }, engine))',
  'const created = await handleMessage({ type: "applyEdit", op: "createLine", payload: { x1: 5, y1: 5, x2: 6, y2: 5, layer: "0" } }, engine)',
  'out.created = reply(created)',
  'const batched = await handleMessage({ type: "applyEdit", op: "batch", payload: { steps: [',
  '  { op: "move", payload: { entityId: created.createdId, dx: 1, dy: 0 } },',
  '] } }, engine)',
  'out.batched = reply(batched)',
  'const batchBack = engine.parseDxf(batched.bytes)',
  'out.batchRoundtrip = projection(batchBack.editableEntities())',
  'batchBack.free()',
  // A raw document change bypasses the editor's refused verb, so the diff must
  // detect it independently. Re-load the changed insertion through the worker.
  'const movedBytes = new TextEncoder().encode(dxf.replace("10\\n10\\n20\\n20\\n", "10\\n11\\n20\\n20\\n"))',
  'const rawMoved = await handleMessage({ type: "loadDocument", documentId: "raw-moved.dxf", bytes: movedBytes }, engine)',
  'out.rawMoved = reply(rawMoved)',
  'const load = async (source) => reply(await handleMessage({ type: "loadDocument", documentId: "variant.dxf", bytes: typeof source === "string" ? new TextEncoder().encode(source) : source }, engine))',
  'const children = dxf.slice(dxf.indexOf("0\\nLINE\\n"), dxf.indexOf("0\\nENDBLK\\n"))',
  'const childLine = children.slice(0, children.indexOf("0\\nCIRCLE\\n"))',
  'const onlyLine = dxf.replace(children, childLine)',
  'const model = Array.from({ length: 3 }, (_, i) => ["0", "LINE", "5", (0x600 + i).toString(16), "8", "0", "10", 10 + i * 10, "20", 0, "30", 0, "11", 11 + i * 10, "21", 0, "31", 0].join("\\n") + "\\n").join("")',
  'const skewSource = onlyLine.slice(0, onlyLine.indexOf("0\\nINSERT\\n")) + model + "0\\nENDSEC\\n0\\nEOF\\n"',
  'out.skewBefore = await load(skewSource)',
  'out.skew = reply(await handleMessage({ type: "applyEdit", op: "batch", payload: { steps: [',
  '  { op: "delete", payload: { entityId: "1536" } },',
  '  { op: "move", payload: { entityId: "1538", dx: 5, dy: 0 } },',
  '] } }, engine))',
  'const many = Array.from({ length: 61 }, (_, i) => childLine.replace("5\\n100\\n", "5\\n" + (0x100 + i).toString(16) + "\\n"))',
  'const manySource = dxf.replace(children, many.join(""))',
  'out.many = await load(manySource)',
  'const manyDoc = engine.parseDxf(new TextEncoder().encode(manySource))',
  'out.manyBack = await load(engine.writeDxf(manyDoc)); manyDoc.free()',
  'many[60] = many[60].replace("10\\n1\\n", "10\\n6\\n")',
  'out.manyMoved = await load(dxf.replace(children, many.join("")))',
  'const point = "0\\nPOINT\\n5\\n100\\n8\\n0\\n10\\n1\\n20\\n2\\n30\\n0\\n"',
  'out.point = await load(dxf.replace(children, point))',
  'out.pointMoved = await load(dxf.replace(children, point.replace("10\\n1\\n", "10\\n6\\n")))',
  'out.lowercase = await load(dxf.replace("8\\n0\\n2\\nB\\n10\\n10\\n", "8\\n0\\n2\\nb\\n10\\n10\\n"))',
  'out.array = await load(onlyLine.replace("50\\n90\\n", "50\\n0\\n70\\n2\\n71\\n1\\n44\\n10\\n45\\n4\\n").replace("41\\n2\\n42\\n3\\n", "41\\n1\\n42\\n1\\n"))',
  'out.arrayOne = await load(onlyLine.replace("50\\n90\\n", "50\\n0\\n70\\n1\\n71\\n1\\n44\\n10\\n45\\n4\\n").replace("41\\n2\\n42\\n3\\n", "41\\n1\\n42\\n1\\n"))',
  'const extraBlock = "0\\nBLOCK\\n5\\n42\\n8\\n0\\n2\\nb\\n70\\n0\\n10\\n1\\n20\\n2\\n30\\n0\\n" + childLine.replace("5\\n100\\n", "5\\n102\\n") + "0\\nENDBLK\\n5\\n43\\n8\\n0\\n"',
  'const colliding = dxf.replace("0\\nENDSEC\\n0\\nSECTION\\n2\\nENTITIES", extraBlock + "0\\nENDSEC\\n0\\nSECTION\\n2\\nENTITIES")',
  'out.collision = await load(colliding)',
  'const binary = (text) => {',
  '  const chunks = [Buffer.from("AutoCAD Binary DXF\\r\\n\\x1a\\x00")]',
  '  const pairs = text.trimEnd().split("\\n")',
  '  for (let i = 0; i < pairs.length; i += 2) {',
  '    const code = Number(pairs[i]); const codeBytes = Buffer.alloc(2); codeBytes.writeInt16LE(code); chunks.push(codeBytes)',
  '    if (code >= 10 && code <= 59) { const value = Buffer.alloc(8); value.writeDoubleLE(Number(pairs[i + 1])); chunks.push(value) }',
  '    else if (code >= 60 && code <= 79) { const value = Buffer.alloc(2); value.writeInt16LE(Number(pairs[i + 1])); chunks.push(value) }',
  '    else chunks.push(Buffer.from(pairs[i + 1] + "\\x00"))',
  '  }',
  '  return new Uint8Array(Buffer.concat(chunks))',
  '}',
  'out.binaryCollision = await load(binary(colliding))',
  'out.binary = await load(binary(onlyLine))',
  'out.binaryEdited = reply(await handleMessage({ type: "applyEdit", op: "createLine", payload: { x1: 0, y1: 0, x2: 1, y2: 0, layer: "0" } }, engine))',
  'process.stdout.write(JSON.stringify(out))',
].join('\n')

describe('W4g-7b-01c blocks through the rebuilt wasm and worker', () => {
  it('preserves bases, ownership, parent picks, and metadata on load, edits, and batches', { timeout: 90_000 }, () => {
    expect(GLUE, 'the planner must rebuild pkg-node before this required row').toBeTruthy()
    const out = JSON.parse(execFileSync(process.execPath, ['--input-type=module', '-e', BLOCK_SCRIPT, WORKER_PATH, path.join(PKG_DIR, GLUE), BLOCK_DXF], {
      encoding: 'utf8', timeout: 90_000, maxBuffer: 16 * 1024 * 1024,
    }))
    expect(out.direct.entities).toHaveLength(1)
    const reference = { handle: '1280', type: 'INSERT', kind: 'REFERENCE', name: 'B', layer: '0', editable: false, ip: [10, 20, 0], rotationDeg: 90, scale: [2, 3, 1], columns: 1, rows: 1, columnSpacing: 0, rowSpacing: 0 }
    expect(out.direct.entities[0]).toMatchObject(reference)
    expect(out.direct.blocks).toMatchObject([{ name: 'B', base: [1, 2, 0], complete: true }])
    expect(out.direct.blocks[0].children).toHaveLength(2)
    expect(out.patched).toBe(true)
    expect(out.roundtrip.blocks[0].base).toEqual([1, 2, 0])
    expect(out.initial).toMatchObject({ type: 'documentLoaded', blockBasePatched: false })
    expect(out.initial.blocks).toEqual(out.direct.blocks)
    expect(out.initial.entities).toHaveLength(1)
    expect(out.initial.entities[0]).toMatchObject({ ...reference, id: '1280' })
    const canvas = engineIntake(out.initial)
    expect(canvas.polylines).toHaveLength(2)
    expect(canvas.polylines.map((p) => p.sourceHandle)).toEqual(['500', '500'])
    expect(canvas.polylines.every((p) => p.sourceHandle === hexHandle(out.initial.entities[0].handle))).toBe(true)
    expect(canvas.polylines[0].pts[0]).toEqual([10, 20, 0])
    expect(canvas.polylines[0].pts[1][0]).toBeCloseTo(10, 9)
    expect(canvas.polylines[0].pts[1][1]).toBeCloseTo(26, 9)
    for (const op of ['move', 'copy', 'delete']) expect(out[op]).toMatchObject({ ok: false, reason: 'INSERT is not editable in this round', blockBasePatched: false })
    expect(out.batch).toMatchObject({ ok: false, reason: 'step_1_move:INSERT is not editable in this round', blockBasePatched: false })
    for (const result of [out.move, out.copy, out.delete, out.batch]) {
      expect(result.entities).toHaveLength(1)
      expect(result.entities[0]).toMatchObject({ ...reference, id: '1280' })
      expect(result.blocks).toEqual(out.direct.blocks)
    }
    expect(out.created).toMatchObject({ type: 'editApplied', op: 'createLine', ok: true, blockBasePatched: true })
    expect(out.created.entities).toHaveLength(2)
    expect(out.created.entities.find((entity) => entity.type === 'INSERT')).toMatchObject({ ...reference, id: '1280' })
    expect(out.created.blocks).toEqual(out.direct.blocks)
    expect(out.batched).toMatchObject({ type: 'editApplied', op: 'batch', ok: true, blockBasePatched: true })
    expect(out.batched.entities).toHaveLength(2)
    expect(out.batched.entities.find((entity) => entity.type === 'INSERT')).toMatchObject({ ...reference, id: '1280' })
    expect(out.batched.entities.find((entity) => entity.type === 'LINE').vertices).toEqual([[6, 5, 0], [7, 5, 0]])
    expect(out.batched.blocks).toEqual(out.direct.blocks)
    expect(out.batchRoundtrip.blocks[0].base).toEqual([1, 2, 0])
    expect(diffPlan(out.initial, out.roundtrip)).toEqual({ mutations: {}, count: 0, reason: null })
    expect(diffPlan(out.initial, out.rawMoved).reason).toBe('entity 500 is a INSERT the plan cannot carry, and it changed')

    expect(out.skew).toMatchObject({ ok: true, blockBasePatched: true })
    expect(out.skew.entities.map((e) => e.handle).sort()).toEqual(['1537', '1538'])
    expect(out.skew.entities.find((e) => e.handle === '1537').vertices).toEqual([[20, 0, 0], [21, 0, 0]])
    expect(out.skew.entities.find((e) => e.handle === '1538').vertices).toEqual([[35, 0, 0], [36, 0, 0]])
    expect(out.skew.blocks).toEqual(out.skewBefore.blocks)
    for (const [before, after] of [[out.many, out.manyMoved], [out.point, out.pointMoved]]) {
      expect(before.blocks[0].children).toEqual(after.blocks[0].children)
      expect(before.blocks[0].digest).toMatch(/^[0-9a-f]{16,}$/)
      expect(before.blocks[0].digest).not.toBe(after.blocks[0].digest)
      expect(diffPlan(before, after).reason).toMatch(/definition.*cannot carry/)
    }
    expect(out.many.blocks[0].children).toHaveLength(60)
    expect(diffPlan(out.many, out.manyBack)).toEqual({ mutations: {}, count: 0, reason: null })
    expect(out.lowercase.entities[0].name).toBe('b')
    expect(engineIntake(out.lowercase).polylines).toHaveLength(2)
    expect(out.array.entities[0]).toMatchObject({ columns: 2, rows: 1, columnSpacing: 10, rowSpacing: 4 })
    expect(engineIntake(out.array).polylines.map((p) => p.pts[0])).toEqual([[10, 20, 0], [20, 20, 0]])
    expect(diffPlan(out.arrayOne, out.array).reason).toMatch(/INSERT.*cannot carry/)
    for (const collision of [out.collision, out.binaryCollision]) {
      expect(collision).toMatchObject({ type: 'documentLoaded', writable: false, refusal: 'block names collide case-insensitively: B, b', entities: [], blocks: [] })
    }
    expect(out.binary.blocks[0]).toMatchObject({ baseUnknown: true, complete: false })
    expect(engineIntake(out.binary).polylines).toEqual([])
    expect(engineIntake(out.binary).inserts).toMatchObject([{ handle: '500', incomplete: true }])
    expect(out.binaryEdited).toMatchObject({ ok: true, blockBasePatched: false })
    expect(out.binaryEdited.blocks[0]).toMatchObject({ baseUnknown: true, complete: false })
    expect(engineIntake(out.binaryEdited).polylines.every((p) => p.sourceHandle == null)).toBe(true)
  })
})
