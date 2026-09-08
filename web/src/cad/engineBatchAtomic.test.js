// @vitest-environment jsdom
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
import { existsSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, describe, expect, it } from 'vitest'
import { act, cleanup, renderHook } from '@testing-library/react'
import useEngineSession, { buildCreatePayload } from '../cadedit/engineSession.js'
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
afterEach(cleanup)

// Each transport turn reconstructs the real worker from its last written
// bytes. Undo's bytes must come from the store's posted loadDocument message.
function realWorkerTransport() {
  const listeners = new Set()
  const worker = { posted: [], writes: [], bytes: null, documentId: null,
    addEventListener(type, listener) { if (type === 'message') listeners.add(listener) },
    removeEventListener(type, listener) { if (type === 'message') listeners.delete(listener) },
    terminate() { listeners.clear() },
    postMessage(message) {
      worker.posted.push(message)
      if (message.type === 'dispose') return
      queueMicrotask(() => {
        const source = [
          'import { createRequire } from "node:module"',
          'import { pathToFileURL } from "node:url"',
          'import { readFileSync } from "node:fs"',
          'const [workerPath, gluePath, inputPath] = process.argv.slice(1)',
          'const { handleMessage } = await import(pathToFileURL(workerPath).href)',
          'const native = createRequire(import.meta.url)(gluePath)',
          'let writes = 0',
          'const engine = { ...native, writeDxf(doc) { writes++; return native.writeDxf(doc) } }',
          'const { message, previous, documentId } = JSON.parse(readFileSync(inputPath, "utf8"))',
          'if (previous && message.type === "applyEdit") await handleMessage({ type: "loadDocument", documentId, bytes: Uint8Array.from(previous) }, engine)',
          'if (message.bytes) message.bytes = Uint8Array.from(message.bytes)',
          'writes = 0',
          'const reply = await handleMessage(message, engine)',
          'if (reply?.bytes) reply.bytes = Array.from(reply.bytes)',
          'process.stdout.write(JSON.stringify({ reply, writes }))',
        ].join('\n')
        const input = JSON.stringify({ message: { ...message, ...(message.bytes ? { bytes: Array.from(message.bytes) } : {}) }, previous: worker.bytes, documentId: worker.documentId })
        const inputDir = mkdtempSync(path.join(tmpdir(), 'engine-batch-'))
        const inputPath = path.join(inputDir, 'input.json')
        let response
        try {
          writeFileSync(inputPath, input, 'utf8')
          response = JSON.parse(execFileSync(process.execPath, ['--input-type=module', '-e', source, WORKER_PATH, path.join(PKG_DIR, GLUE), inputPath], { encoding: 'utf8', timeout: 90_000, maxBuffer: 16 * 1024 * 1024 }))
        } finally {
          rmSync(inputDir, { recursive: true, force: true })
        }
        const { reply, writes } = response
        worker.writes.push({ type: message.type, op: message.op, count: writes })
        if (message.type === 'loadDocument') { worker.bytes = Array.from(message.bytes); worker.documentId = message.documentId }
        if (reply?.bytes) { worker.bytes = reply.bytes; reply.bytes = Uint8Array.from(reply.bytes) }
        if (reply) listeners.forEach((listener) => listener({ data: reply }))
      })
    },
  }
  return worker
}

describe.skipIf(!GLUE)('Create Block on the real engine', () => {
  it('carries native polyline widths to the builder before posting a create', { timeout: 90_000 }, async () => {
    const worker = realWorkerTransport()
    const { result } = renderHook(() => useEngineSession({ createWorker: () => worker }))
    const bytes = new TextEncoder().encode('0\nSECTION\n2\nENTITIES\n0\nLWPOLYLINE\n5\n10\n8\n0\n90\n2\n70\n0\n43\n2\n10\n0\n20\n0\n40\n3\n41\n4\n10\n5\n20\n0\n0\nENDSEC\n0\nEOF\n')
    await act(async () => { result.current.actions.openBytes(bytes, 'widths.dxf', { committed: true }) })
    expect(result.current.entities[0]).toMatchObject({ constantWidth: 2, startWidths: [3, 0], endWidths: [4, 0] })
    act(() => { result.current.actions.select('16') })
    const posted = worker.posted.length
    act(() => { result.current.actions.create('createBlock', { name: 'B', x: '0', y: '0' }) })
    expect(result.current.status).toContain('polyline widths must be zero')
    expect(worker.posted).toHaveLength(posted)
  })
  it('refuses DIMASSOC members using definingHandles from the real worker projection', { timeout: 90_000 }, async () => {
    const worker = realWorkerTransport()
    const { result } = renderHook(() => useEngineSession({ createWorker: () => worker }))
    const bytes = new TextEncoder().encode("0\nSECTION\n2\nHEADER\n9\n$ACADVER\n1\nAC1027\n0\nENDSEC\n0\nSECTION\n2\nENTITIES\n0\nLINE\n5\n10\n102\n{ACAD_REACTORS\n330\n30\n102\n}\n8\n0\n10\n12\n20\n23\n11\n17\n21\n23\n0\nDIMENSION\n5\n20\n8\n0\n70\n0\n10\n0\n20\n5\n13\n0\n23\n0\n14\n5\n24\n0\n0\nENDSEC\n0\nSECTION\n2\nOBJECTS\n0\nDIMASSOC\n5\n30\n100\nAcDbDimAssoc\n330\n20\n90\n1\n70\n0\n71\n0\n1\nAcDbOsnapPointRef\n72\n1\n331\n10\n73\n1\n91\n0\n40\n0\n10\n12\n20\n23\n30\n0\n75\n0\n0\nENDSEC\n0\nEOF\n");
    await act(async () => { result.current.actions.openBytes(bytes, 'dimassoc.dxf', { committed: true }) })
    const entities = result.current.entities
    const dimension = entities.find((e) => e.type === 'DIMENSION')
    expect(dimension.definingHandles).toContain('16')
    // Exercise the producer's definingHandles, independently of its member flag.
    const fromProjection = entities.map(({ dimensionDefined, ...entity }) => entity)
    const built = buildCreatePayload('createBlock', { name: 'B', x: '10', y: '20', selectedId: '16' }, [], [], { entities: fromProjection, committedEntities: fromProjection })
    expect(built.refusal).toContain('a dimension defining entity cannot become a block child')
    act(() => { result.current.actions.select('16') })
    const posted = worker.posted.length
    act(() => { result.current.actions.create('createBlock', { name: 'B', x: '10', y: '20' }) })
    expect(result.current.status).toContain('dimension defining')
    expect(worker.posted).toHaveLength(posted)
  })
  it('undoes a real create through the store action and restores the original handles and bytes', { timeout: 90_000 }, async () => {
    const worker = realWorkerTransport()
    const { result } = renderHook(() => useEngineSession({ createWorker: () => worker }))
    const bytes = new TextEncoder().encode('0\nSECTION\n2\nENTITIES\n0\nLINE\n5\n10\n8\n0\n10\n12\n20\n23\n11\n17\n21\n23\n0\nCIRCLE\n5\n11\n8\n0\n10\n11\n20\n24\n40\n2\n0\nENDSEC\n0\nEOF\n')
    await act(async () => { result.current.actions.openBytes(bytes, 'block.dxf', { committed: true }) })
    const before = JSON.parse(JSON.stringify(Array.from(result.current.entities)))
    const digest = (value) => createHash('sha256').update(Uint8Array.from(value)).digest('hex')
    const beforeDigest = digest(bytes)
    expect(before.map((e) => e.id)).toEqual(['16', '17'])
    act(() => { result.current.actions.select('16') })
    await act(async () => { result.current.actions.create('createBlock', { name: 'B', x: '10', y: '20', members: '17' }) })
    expect(result.current.errorKind).toBeNull()
    expect(result.current.entities).toHaveLength(1)
    expect(result.current.undoDepth).toBe(1)
    expect(worker.writes.filter((entry) => entry.op === 'createBlock')).toEqual([{ type: 'applyEdit', op: 'createBlock', count: 1 }])
    await act(async () => { expect(result.current.actions.undo()).toBe(true) })
    expect(worker.posted.at(-1).type).toBe('loadDocument')
    expect(Array.from(worker.posted.at(-1).bytes)).toEqual(Array.from(bytes))
    expect(Array.from(result.current.entities)).toEqual(before)
    expect(digest(worker.bytes)).toBe(beforeDigest)
    expect(result.current.entities.blocks).toEqual([])
    expect(result.current.undoDepth).toBe(0)
    expect(result.current.redoDepth).toBe(1)
  })
  it('preserves original child coordinates and transforms both children on reinsertion', { timeout: 90_000 }, () => {
    const source = [
      'import { createRequire } from "node:module"',
      'import { pathToFileURL } from "node:url"',
      'const [workerPath, gluePath] = process.argv.slice(1)',
      'const { handleMessage } = await import(pathToFileURL(workerPath).href)',
      'const engine = createRequire(import.meta.url)(gluePath)',
      'const bytes = new TextEncoder().encode("0\\nSECTION\\n2\\nENTITIES\\n0\\nLINE\\n5\\n10\\n8\\n0\\n10\\n12\\n20\\n23\\n11\\n17\\n21\\n23\\n0\\nCIRCLE\\n5\\n11\\n8\\n0\\n10\\n11\\n20\\n24\\n40\\n2\\n0\\nENDSEC\\n0\\nEOF\\n")',
      'const before = await handleMessage({ type: "loadDocument", documentId: "block.dxf", bytes }, engine)',
      'const made = await handleMessage({ type: "applyEdit", op: "createBlock", payload: { name: "B", x: 10, y: 20, members: before.entities.map(e => e.id) } }, engine)',
      'const collision = await handleMessage({ type: "applyEdit", op: "createBlock", payload: { name: "b", x: 10, y: 20, members: [made.createdId] } }, engine)',
      'const reinserted = await handleMessage({ type: "applyEdit", op: "createInsert", payload: { name: "B", x: 100, y: 200, rotationDeg: 90, sx: 2, sy: 3, sz: 1, layer: "0" } }, engine)',
      'process.stdout.write(JSON.stringify({ before, made, collision, reinserted }))',
    ].join('\n')
    const out = JSON.parse(execFileSync(process.execPath, ['--input-type=module', '-e', source, WORKER_PATH, path.join(PKG_DIR, GLUE)], { encoding: 'utf8', timeout: 90_000, maxBuffer: 16 * 1024 * 1024 }))
    expect(out.made.ok).toBe(true)
    expect(out.made.entities).toHaveLength(1)
    expect(out.made.entities[0]).toMatchObject({ id: out.made.createdId, type: 'INSERT', name: 'B', ip: [10, 20, 0], layer: '0', scale: [1, 1, 1] })
    expect(out.made.blocks[0]).toMatchObject({ name: 'B', base: [10, 20, 0], complete: true })
    expect(out.made.blocks[0].children.find(e => e.type === 'LINE').vertices).toEqual([[12, 23, 0], [17, 23, 0]])
    expect(out.made.blocks[0].children.find(e => e.type === 'CIRCLE')).toMatchObject({ vertices: [[11, 24, 0]], radius: 2 })
    expect(out.collision.ok).toBe(false)
    expect(out.collision.reason).toContain('block_name_exists')
    expect(out.reinserted.ok).toBe(true)
    const expanded = engineIntake(out.reinserted).polylines.filter((p) => p.sourceHandle === hexHandle(out.reinserted.createdId))
    const expandedLine = expanded.find((p) => p.pts.length === 2)
    expect(expandedLine.pts[0][0]).toBeCloseTo(91, 9)
    expect(expandedLine.pts[0][1]).toBeCloseTo(204, 9)
    expect(expandedLine.pts[1][0]).toBeCloseTo(91, 9)
    expect(expandedLine.pts[1][1]).toBeCloseTo(214, 9)
    const expandedCircle = expanded.find((p) => p.pts.length > 2)
    const xs = expandedCircle.pts.map((p) => p[0])
    const ys = expandedCircle.pts.map((p) => p[1])
    expect((Math.min(...xs) + Math.max(...xs)) / 2).toBeCloseTo(88, 6)
    expect((Math.min(...ys) + Math.max(...ys)) / 2).toBeCloseTo(202, 6)
    expect(Math.max(...xs) - Math.min(...xs)).toBeCloseTo(12, 6)
    expect(Math.max(...ys) - Math.min(...ys)).toBeCloseTo(8, 6)
    const committed = Object.assign(out.before.entities, { blocks: out.before.blocks })
    const current = Object.assign(out.made.entities, { blocks: out.made.blocks })
    expect(diffPlan(committed, current).mutations.block_defs).toEqual([{ name: 'B', base: [10, 20, 0], members: ['10', '11'], insert: 0 }])
  })
})

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
  'const ltypeNames = ["ByLayer", "ByBlock", "Continuous", ...Array.from({ length: 201 }, (_, i) => "L" + String(i + 1).padStart(3, "0"))]',
  'const ltypeTable = ["0", "SECTION", "2", "TABLES", "0", "TABLE", "2", "LTYPE", "70", String(ltypeNames.length), ...ltypeNames.flatMap((name) => ["0", "LTYPE", "2", name, "70", "0", "3", name, "72", "65", "73", "0", "40", "0"]), "0", "ENDTAB", "0", "ENDSEC"].join("\\n") + "\\n"',
  'const manyBytes = new TextEncoder().encode(dxf.replace("0\\nSECTION\\n2\\nENTITIES\\n", ltypeTable + "0\\nSECTION\\n2\\nENTITIES\\n"))',
  'const manyDoc = engine.parseDxf(manyBytes)',
  'const rawMany = manyDoc.editableEntities()',
  'out.rawManyCatalogue = { hasFlag: Object.prototype.hasOwnProperty.call(rawMany, "linetypesTruncated"), linetypes: rawMany.linetypes, linetypesTruncated: rawMany.linetypesTruncated }',
  'manyDoc.free()',
  'const manyLoaded = await handleMessage({ type: "loadDocument", documentId: "many-linetypes.dxf", bytes: manyBytes }, engine)',
  'out.manyCatalogue = { linetypes: manyLoaded.linetypes, linetypesTruncated: manyLoaded.linetypesTruncated }',
  'const ordinaryDoc = engine.parseDxf(bytes)',
  'const rawOrdinary = ordinaryDoc.editableEntities()',
  'out.rawOrdinaryCatalogue = { hasFlag: Object.prototype.hasOwnProperty.call(rawOrdinary, "linetypesTruncated"), linetypesTruncated: rawOrdinary.linetypesTruncated }',
  'ordinaryDoc.free()',
  'const loaded = await handleMessage({ type: "loadDocument", documentId: "x.dxf", bytes }, engine)',
  'out.loadedCatalogue = { linetypes: loaded.linetypes, linetypesTruncated: loaded.linetypesTruncated }',
  'const ids = loaded.entities.map((e) => e.id)',
  'const [h, v, c] = ids',
  'const summary = (r) => ({ ok: r.ok, op: r.op, reason: r.reason ?? null, createdId: r.createdId ?? null, createdIds: r.createdIds ?? null, count: r.entityCount ?? null, entities: (r.entities || []).map((e) => ({ id: e.id, type: e.type, layer: e.layer, editable: e.editable, vertices: e.vertices, bulges: e.bulges, closed: e.closed, radius: e.radius, startDeg: e.startDeg, endDeg: e.endDeg, majorAxis: e.majorAxis, ratio: e.ratio, aci: e.aci, trueColor: e.trueColor ?? null, linetype: e.linetype, lineweight: e.lineweight })), linetypes: r.linetypes ?? null, linetypesTruncated: r.linetypesTruncated })',
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
  // W4g-7b-03c: the three property setters on the real engine. Each reads
  // back after the write + re-parse; a refused set leaves the entity's
  // prior value in place (untouched, never a partial write).
  'out.setColor = summary(await handleMessage({ type: "applyEdit", op: "setColor", payload: { entityId: h, aci: 1 } }, engine))',
  'out.setColorRefused = summary(await handleMessage({ type: "applyEdit", op: "setColor", payload: { entityId: h, aci: 999 } }, engine))',
  'out.setLinetype = summary(await handleMessage({ type: "applyEdit", op: "setLinetype", payload: { entityId: h, linetype: "byblock" } }, engine))',
  'out.setLinetypeRefused = summary(await handleMessage({ type: "applyEdit", op: "setLinetype", payload: { entityId: h, linetype: "NoSuchLinetype" } }, engine))',
  'out.setLineweight = summary(await handleMessage({ type: "applyEdit", op: "setLineweight", payload: { entityId: h, lineweight: 25 } }, engine))',
  'out.setLineweightRefused = summary(await handleMessage({ type: "applyEdit", op: "setLineweight", payload: { entityId: h, lineweight: 26 } }, engine))',
  // A harmless read-back (h is already on layer A): proves every refusal
  // above left the prior value in place, never a partial write.
  'out.afterProps = summary(await handleMessage({ type: "applyEdit", op: "setLayer", payload: { entityId: h, layer: "A" } }, engine))',
  // W4g-7b-03c required row: a DXF with explicit 62/6/370/420 groups on
  // entities this session never touches round-trips them through parse ->
  // one unrelated edit elsewhere -> write -> re-parse. $ACADVER AC1027 (the
  // demo DWG's own converted version) is AC1018+, so the writer emits 420.
  'const propsDxf = "0\\nSECTION\\n2\\nHEADER\\n9\\n$ACADVER\\n1\\nAC1027\\n0\\nENDSEC\\n0\\nSECTION\\n2\\nENTITIES\\n0\\nLINE\\n5\\n64\\n8\\n0\\n6\\nContinuous\\n62\\n3\\n370\\n25\\n10\\n20.0\\n20\\n0.0\\n30\\n0.0\\n11\\n25.0\\n21\\n0.0\\n31\\n0.0\\n0\\nLINE\\n5\\n65\\n8\\n0\\n420\\n660510\\n10\\n30.0\\n20\\n0.0\\n30\\n0.0\\n11\\n35.0\\n21\\n0.0\\n31\\n0.0\\n0\\nENDSEC\\n0\\nEOF\\n"',
  'const propsLoaded = await handleMessage({ type: "loadDocument", documentId: "props.dxf", bytes: new TextEncoder().encode(propsDxf) }, engine)',
  'out.propsBefore = summary(propsLoaded)',
  'const [untouchedA, untouchedB] = propsLoaded.entities.map((e) => e.id)',
  // ONE unrelated edit, touching neither line above.
  'out.propsAfter = summary(await handleMessage({ type: "applyEdit", op: "createLine", payload: { x1: 90, y1: 0, x2: 91, y2: 0, layer: "0" } }, engine))',
  'out.untouchedA = untouchedA',
  'out.untouchedB = untouchedB',
  // Declared residual: the identical fixture under $ACADVER AC1015 (2000,
  // pre-AC1018). Reading is version-agnostic (the 420 group reads back on
  // the initial parse), but the WRITER refuses to emit 420 below AC1018, so
  // the same round-trip (write + re-parse) honestly drops the true colour.
  'const propsDxfLegacy = "0\\nSECTION\\n2\\nHEADER\\n9\\n$ACADVER\\n1\\nAC1015\\n0\\nENDSEC\\n0\\nSECTION\\n2\\nENTITIES\\n0\\nLINE\\n5\\n64\\n8\\n0\\n6\\nContinuous\\n62\\n3\\n370\\n25\\n10\\n20.0\\n20\\n0.0\\n30\\n0.0\\n11\\n25.0\\n21\\n0.0\\n31\\n0.0\\n0\\nLINE\\n5\\n65\\n8\\n0\\n420\\n660510\\n10\\n30.0\\n20\\n0.0\\n30\\n0.0\\n11\\n35.0\\n21\\n0.0\\n31\\n0.0\\n0\\nENDSEC\\n0\\nEOF\\n"',
  'const propsLegacyLoaded = await handleMessage({ type: "loadDocument", documentId: "props-legacy.dxf", bytes: new TextEncoder().encode(propsDxfLegacy) }, engine)',
  'out.propsLegacyBefore = summary(propsLegacyLoaded)',
  'const [untouchedLegacyA, untouchedLegacyB] = propsLegacyLoaded.entities.map((e) => e.id)',
  // ONE unrelated edit, touching neither legacy line above.
  'out.propsLegacyAfter = summary(await handleMessage({ type: "applyEdit", op: "createLine", payload: { x1: 92, y1: 0, x2: 93, y2: 0, layer: "0" } }, engine))',
  'out.untouchedLegacyA = untouchedLegacyA',
  'out.untouchedLegacyB = untouchedLegacyB',
  'const groupLoaded = await handleMessage({ type: "loadDocument", documentId: "groups.dxf", bytes }, engine)',
  'const groupMembers = [groupLoaded.entities[0].id, groupLoaded.entities[2].id]',
  'out.groupBefore = groupLoaded.entities',
  'out.groupInvalid = []',
  'for (const name of ["A/B", "RA*CK"]) out.groupInvalid.push(await handleMessage({ type: "applyEdit", op: "createGroup", payload: { name, members: groupMembers } }, engine))',
  'out.groupCreated = await handleMessage({ type: "applyEdit", op: "createGroup", payload: { name: "rack", members: groupMembers } }, engine)',
  'out.groupMoved = await handleMessage({ type: "applyEdit", op: "move", payload: { entityId: groupMembers[0], dx: 2, dy: -1 } }, engine)',
  'out.groupRemoved = await handleMessage({ type: "applyEdit", op: "ungroup", payload: { name: "rack" } }, engine)',
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
    for (const result of out.groupInvalid) {
      expect(result.ok).toBe(false)
      expect(result.reason).toContain('group_name_invalid')
    }
    expect(out.groupCreated.ok).toBe(true)
    expect(out.groupCreated.entities).toEqual(out.groupBefore)
    expect(out.groupCreated.groups).toMatchObject([{ name: 'RACK', memberIds: [out.groupBefore[0].id, out.groupBefore[2].id] }])
    expect(out.groupCreated.createdId).toBe(out.groupCreated.groups[0].id)
    expect(out.groupMoved.ok).toBe(true)
    expect(out.groupMoved.groups).toEqual(out.groupCreated.groups)
    expect(out.groupMoved.entities[0].vertices).toEqual([[2, -1, 0], [12, -1, 0]])
    expect(out.groupRemoved.ok).toBe(true)
    expect(out.groupRemoved.groups).toEqual([])
    expect(out.groupRemoved.entities).toEqual(out.groupMoved.entities)
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

    // W4g-7b-03c: the three property setters on the real engine, each read
    // back after the write + re-parse; a refused set leaves the entity's
    // prior value in place, never a partial write.
    expect(out.setColor.ok).toBe(true)
    expect(out.setColor.entities.find((e) => e.id === h).aci).toBe(1)
    expect(out.setColorRefused).toMatchObject({ ok: false, op: 'setColor', reason: 'color_index_out_of_range' })
    expect(out.setLinetype.ok).toBe(true)
    expect(out.loadedCatalogue.linetypesTruncated).toBe(false)
    expect(out.rawOrdinaryCatalogue).toEqual({ hasFlag: true, linetypesTruncated: false })
    expect(out.rawManyCatalogue.hasFlag).toBe(true)
    expect(out.rawManyCatalogue.linetypesTruncated).toBe(true)
    expect(out.rawManyCatalogue.linetypes).toHaveLength(200)
    expect(out.manyCatalogue.linetypesTruncated).toBe(true)
    expect(out.manyCatalogue.linetypes).toHaveLength(200)
    expect(out.manyCatalogue.linetypes).toEqual(out.rawManyCatalogue.linetypes)
    expect(out.manyCatalogue.linetypes).toEqual(expect.arrayContaining(['ByLayer', 'ByBlock', 'Continuous', 'L001']))
    expect(out.loadedCatalogue.linetypes).toEqual(expect.arrayContaining(['ByLayer', 'ByBlock', 'Continuous']))
    expect(out.setLinetype.entities.find((e) => e.id === h).linetype).toBe('ByBlock')
    expect(out.setLinetypeRefused).toMatchObject({ ok: false, op: 'setLinetype', reason: 'linetype_not_loaded:NoSuchLinetype' })
    expect(out.setLineweight.ok).toBe(true)
    expect(out.setLineweight.entities.find((e) => e.id === h).lineweight).toBe(25)
    expect(out.setLineweightRefused).toMatchObject({ ok: false, op: 'setLineweight', reason: 'lineweight_not_valid:26' })
    // Every refusal above left h's colour, linetype and lineweight exactly as they were.
    const propsHeld = out.afterProps.entities.find((e) => e.id === h)
    expect(propsHeld).toMatchObject({ aci: 1, linetype: 'ByBlock', lineweight: 25 })
    expect(out.afterProps.linetypes).toEqual(expect.arrayContaining(['ByLayer', 'ByBlock', 'Continuous']))
    expect(out.afterProps.linetypesTruncated).toBe(false)

    // W4g-7b-03c required row: entities the session never touches keep their
    // explicit 62/6/370/420 groups through parse -> one unrelated edit
    // elsewhere -> write -> re-parse; a true colour (420) survives untouched.
    expect(out.propsBefore.entities).toHaveLength(2)
    const beforeA = out.propsBefore.entities.find((e) => e.id === out.untouchedA)
    expect(beforeA).toMatchObject({ aci: 3, linetype: 'Continuous', lineweight: 25, trueColor: null })
    const beforeB = out.propsBefore.entities.find((e) => e.id === out.untouchedB)
    expect(beforeB.trueColor).toEqual([10, 20, 30])
    expect(out.propsAfter.ok).toBe(true)
    expect(out.propsAfter.entities).toHaveLength(3)
    const afterA = out.propsAfter.entities.find((e) => e.id === out.untouchedA)
    expect(afterA).toMatchObject({ aci: 3, linetype: 'Continuous', lineweight: 25, trueColor: null })
    const afterB = out.propsAfter.entities.find((e) => e.id === out.untouchedB)
    expect(afterB.trueColor).toEqual([10, 20, 30])

    // W4g-7b-03c-c declared residual: under $ACADVER AC1015 (pre-AC1018),
    // the initial parse still reads the explicit 420 group (reading is
    // version-agnostic), but the write + re-parse round trip honestly drops
    // it, because the vendored crate's DXF writer emits group 420 only for
    // AC1018+. A pre-2004 head cannot round-trip a true colour through the
    // browser; the plan route's dense-EP preflight then refuses that save.
    expect(out.propsLegacyBefore.entities).toHaveLength(2)
    const legacyBefore = out.propsLegacyBefore.entities.find((e) => e.id === out.untouchedLegacyB)
    expect(legacyBefore.trueColor).toEqual([10, 20, 30])
    expect(out.propsLegacyAfter.ok).toBe(true)
    const legacyAfter = out.propsLegacyAfter.entities.find((e) => e.id === out.untouchedLegacyB)
    expect(legacyAfter.trueColor).toBeNull()
  })
})

const MLEADER_SCRIPT = [
  'import { createRequire } from "node:module"',
  'import { pathToFileURL } from "node:url"',
  'const [workerPath, gluePath, dxf] = process.argv.slice(1)',
  'const { handleMessage } = await import(pathToFileURL(workerPath).href)',
  'const engine = createRequire(import.meta.url)(gluePath)',
  'const bytes = new TextEncoder().encode(dxf)',
  'const loaded = await handleMessage({ type: "loadDocument", documentId: "leader.dxf", bytes }, engine)',
  'const created = await handleMessage({ type: "applyEdit", op: "batch", payload: { steps: [{ op: "createMleader", payload: { x: 0, y: 0, x2: 3, y2: 4, text: "Valve", style: "Standard", layer: "0" } }] } }, engine)',
  'const entity = created.entities.find((e) => e.type === "MLEADER")',
  'const parsed = engine.parseDxf(created.bytes)',
  'const roundtrip = parsed.editableEntities().find((e) => e.type === "MLEADER")',
  'parsed.free()',
  'const refused = []',
  'for (const [op, payload] of [["move", { dx: 1, dy: 2 }], ["rotate", { cx: 0, cy: 0, deg: 90 }], ["explode", {}], ["setLayer", { layer: "Other" }], ["setColor", { aci: 1 }]]) {',
  '  refused.push(await handleMessage({ type: "applyEdit", op, payload: { entityId: entity.id, ...payload } }, engine))',
  '}',
  'const erased = await handleMessage({ type: "applyEdit", op: "delete", payload: { entityId: entity.id } }, engine)',
  'process.stdout.write(JSON.stringify({ loaded: { count: loaded.entityCount, mlstyles: loaded.mlstyles }, created: { ok: created.ok, createdId: created.createdId, createdIds: created.createdIds }, entity, roundtrip, refused: refused.map((r) => ({ ok: r.ok, reason: r.reason })), erased: { ok: erased.ok, count: erased.entityCount, entities: erased.entities } }))',
].join('\n')

// Exact group order from server/intake_dxf.py _emit_mleader/_emit_mlstyle.
// The arrow is the sole LEADER_LINE vertex; the landing belongs to LEADER.
const mlPairs = (pairs) => pairs.flatMap(([code, value]) => Array.isArray(value)
  ? value.flatMap((v, i) => [String(code + i * 10), String(v)]) : [String(code), String(value)])
const servedStyle = (name, handle, segments) => mlPairs([
  [0, 'MLEADERSTYLE'], [5, handle], [102, '{ACAD_REACTORS'], [330, '21'],
  [102, '}'], [330, '21'], [100, 'AcDbMLeaderStyle'], [179, 2], [170, 2],
  [171, 1], [172, 0], [90, 2], [40, 0], [41, 0], [173, segments],
  [91, -1056964608], [340, '11'], [92, -2], [290, 1], [42, 0.09],
  [291, 1], [43, 0.36], [3, name], [341, '0'], [44, 0.18],
  [300, ''], [342, '10'], [174, 1], [178, 1], [175, 1], [176, 0],
  [93, -1056964608], [45, 1], [292, 0], [297, 0], [46, 0.18],
  [343, '0'], [94, -1056964608], [47, 1], [49, 1], [140, 1],
  [293, 1], [141, 0], [294, 1], [177, 0], [142, 1], [295, 0],
  [296, 0], [143, 0.125], [271, 0], [272, 9], [273, 9], [298, 0],
])
const SERVED_MLEADER_DXF = [
  ...mlPairs([[0, 'SECTION'], [2, 'HEADER'], [9, '$ACADVER'], [1, 'AC1027'], [0, 'ENDSEC'],
    [0, 'SECTION'], [2, 'TABLES'], [0, 'TABLE'], [2, 'STYLE'], [70, 1],
    [0, 'STYLE'], [5, '10'], [2, 'Standard'], [70, 0], [40, 0], [41, 1],
    [0, 'ENDTAB'], [0, 'ENDSEC'], [0, 'SECTION'], [2, 'ENTITIES']]),
  ...mlPairs([
    [0, 'MULTILEADER'], [330, '1F'], [5, '100'], [100, 'AcDbEntity'],
    [67, 0], [410, 'Model'], [8, '0'], [100, 'AcDbMLeader'], [270, 2],
    [300, 'CONTEXT_DATA{'], [40, 1], [10, [35.36, 26, 0]], [41, 1],
    [140, 0.18], [145, 0.09], [174, 1], [175, 1], [176, 0], [177, 0],
    [290, 1], [304, 'Valve'], [11, [0, 0, 1]], [340, '10'],
    [12, [35.45, 26.5, 0]], [13, [1, 0, 0]], [42, 0], [43, 0],
    [44, 0], [45, 1], [170, 1], [90, -1073741824],
    [171, 1], [172, 5], [91, -1073741824], [141, 0],
    [92, 0], [291, 0], [292, 0], [173, 0], [293, 0], [142, 0],
    [143, 0], [294, 0], [295, 0], [296, 0], [110, [0, 0, 0]],
    [111, [1, 0, 0]], [112, [0, 1, 0]], [297, 0],
    [302, 'LEADER{'], [290, 1], [291, 1], [10, [35, 26, 0]],
    [11, [1, 0, 0]], [90, 0], [40, 0.36], [304, 'LEADER_LINE{'],
    [10, [30, 23, 0]],
    [91, 0], [170, 1], [92, -1056964608], [340, '0'], [171, -2],
    [40, 0], [341, '0'], [93, 0], [305, '}'], [271, 0], [303, '}'],
    [272, 9], [273, 9], [301, '}'], [340, '30'], [90, 279552],
    [170, 1], [91, -1056964608], [341, '11'], [171, -2], [290, 1],
    [291, 1], [41, 0.36], [42, 0.18], [172, 2], [343, '10'],
    [173, 1], [95, 1], [174, 1], [175, 0], [92, -1056964608], [292, 0],
    [93, -1056964608], [10, [1, 1, 1]], [43, 0], [176, 0],
    [293, 0], [294, 0], [178, 0], [179, 1], [45, 1], [271, 0],
    [272, 9], [273, 9], [295, 0],
    [0, 'ENDSEC'], [0, 'SECTION'], [2, 'OBJECTS'],
    [0, 'DICTIONARY'], [5, '20'], [330, '0'], [100, 'AcDbDictionary'], [281, 1],
    [3, 'ACAD_MLEADERSTYLE'], [350, '21'],
    [0, 'DICTIONARY'], [5, '21'], [102, '{ACAD_REACTORS'], [330, '20'],
    [102, '}'], [330, '20'], [100, 'AcDbDictionary'], [280, 0], [281, 1],
    [3, 'ValveStyle'], [350, '30'], [3, 'TwoSegments'], [350, '31'],
  ]),
  ...servedStyle('ValveStyle', '30', 1), ...servedStyle('TwoSegments', '31', 2),
  ...mlPairs([[0, 'ENDSEC'], [0, 'EOF']]),
].join('\n') + '\n'

describe.skipIf(!GLUE)('MLEADER through the rebuilt engine and worker batch', () => {
  it('retains source segment counts across consecutive worker write and reparse turns', { timeout: 90_000 }, () => {
    const source = SERVED_MLEADER_DXF.replace('173\n2\n', '173\n3\n')
    const script = [
      'import { createRequire } from "node:module"',
      'import { pathToFileURL } from "node:url"',
      'const [workerPath, gluePath, source] = process.argv.slice(1)',
      'const { handleMessage } = await import(pathToFileURL(workerPath).href)',
      'const engine = createRequire(import.meta.url)(gluePath)',
      'const loaded = await handleMessage({ type: "loadDocument", documentId: "segments", bytes: new TextEncoder().encode(source) }, engine)',
      'const replies = [loaded]',
      'for (let i = 0; i < 2; i++) replies.push(await handleMessage({ type: "applyEdit", op: "createLine", payload: { x1: i, y1: 0, x2: i + 1, y2: 1, layer: "0" } }, engine))',
      'process.stdout.write(JSON.stringify(replies.map(r => ({ ok: r.ok, mlstyles: r.mlstyles }))))',
    ].join('\n')
    const replies = JSON.parse(execFileSync(process.execPath, ['--input-type=module', '-e', script, WORKER_PATH, path.join(PKG_DIR, GLUE), source], {
      encoding: 'utf8', timeout: 90_000, maxBuffer: 8 * 1024 * 1024,
    }))
    for (const reply of replies) expect(reply.mlstyles).toEqual(expect.arrayContaining([expect.objectContaining({ name: 'TwoSegments', segments: 3 })]))
    for (const reply of replies.slice(1)) expect(reply.ok).toBe(true)
  })
  it('projects a served leader and the source segment counts of two styles', { timeout: 90_000 }, async () => {
    const worker = realWorkerTransport()
    const { result } = renderHook(() => useEngineSession({ createWorker: () => worker }))
    await act(async () => { result.current.actions.openBytes(new TextEncoder().encode(SERVED_MLEADER_DXF), 'served-leader.dxf') })
    const leader = result.current.entities.find((e) => e.type === 'MLEADER')
    expect(leader).toMatchObject({ vertices: [[30, 23, 0], [35, 26, 0]], height: 1, text: 'Valve' })
    expect(result.current.entities.mlstyles).toEqual(expect.arrayContaining([
      expect.objectContaining({ name: 'ValveStyle', segments: 1 }),
      expect.objectContaining({ name: 'TwoSegments', segments: 2 }),
    ]))
    const intake = engineIntake(result.current.entities)
    expect(intake.polylines.filter((p) => p.handle === hexHandle(leader.handle))).toHaveLength(4)
    expect(intake.polylines[0].pts).toEqual([[30, 23, 0], [35, 26, 0]])
  })
  it('creates and reparses the bounded leader, refuses edits by kind, and erases it', { timeout: 90_000 }, () => {
    const out = JSON.parse(execFileSync(process.execPath, ['--input-type=module', '-e', MLEADER_SCRIPT, WORKER_PATH, path.join(PKG_DIR, GLUE), DXF], {
      encoding: 'utf8', timeout: 90_000, maxBuffer: 8 * 1024 * 1024,
    }))
    expect(out.loaded.mlstyles).toEqual(expect.arrayContaining([expect.objectContaining({ name: 'Standard', segments: null })]))
    expect(out.created.ok).toBe(true)
    expect(out.created.createdIds).toContain(out.entity.id)
    expect(out.created.createdId).toBe(out.entity.id)
    expect(out.entity).toMatchObject({ type: 'MLEADER', editable: false, style: 'Standard', text: 'Valve', vertices: [[0, 0, 0], [3, 4, 0]] })
    expect(out.roundtrip).toMatchObject({ type: 'MLEADER', style: 'Standard', text: 'Valve', vertices: [[0, 0, 0], [3, 4, 0]] })
    for (const refusal of out.refused) expect(refusal).toEqual({ ok: false, reason: 'a mleader is placed, not edited, in this round' })
    expect(out.erased.ok).toBe(true)
    expect(out.erased.count).toBe(out.loaded.count)
    expect(out.erased.entities.some((e) => e.type === 'MLEADER')).toBe(false)
  })
})

const BLOCK_DXF = [
  '0', 'SECTION', '2', 'HEADER', '9', '$ACADVER', '1', 'AC1027', '0', 'ENDSEC',
  '0', 'SECTION', '2', 'BLOCKS',
  '0', 'BLOCK', '5', '40', '8', '0', '2', 'B', '70', '0', '10', '1', '20', '2', '30', '0',
  '0', 'LINE', '5', '100', '8', '0', '6', 'ByLayer', '10', '1', '20', '2', '30', '0', '11', '4', '21', '2', '31', '0',
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
  'const reply = (r) => r.ok === false ? r : ({ type: r.type, op: r.op, ok: r.ok, reason: r.reason, refusal: r.refusal, writable: r.writable, blockBasePatched: r.blockBasePatched, ...projection(r.entities, r.blocks) })',
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
  'for (const op of ["move", "copy"]) {',
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
  // W4g-7b-05c-2: ERASE stays allowed on an INSERT, unlike every verb above;
  // run it LAST, after every other assertion that still needs entity 1280.
  'out.insertDeleted = reply(await handleMessage({ type: "applyEdit", op: "delete", payload: { entityId: "1280" } }, engine))',
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
  'const arraySource = onlyLine.replace("11\\n4\\n", "11\\n2\\n").replace("41\\n2\\n42\\n3\\n", "41\\n2\\n42\\n1\\n")',
  'out.array = await load(arraySource.replace("50\\n90\\n", "50\\n0\\n70\\n2\\n71\\n1\\n44\\n10\\n45\\n4\\n"))',
  'out.arrayOne = await load(arraySource.replace("50\\n90\\n", "50\\n0\\n70\\n1\\n71\\n1\\n44\\n10\\n45\\n4\\n"))',
  'const extraBlock = "0\\nBLOCK\\n5\\n42\\n8\\n0\\n2\\nb\\n70\\n0\\n10\\n1\\n20\\n2\\n30\\n0\\n" + childLine.replace("5\\n100\\n", "5\\n102\\n") + "0\\nENDBLK\\n5\\n43\\n8\\n0\\n"',
  'const colliding = dxf.replace("0\\nENDSEC\\n0\\nSECTION\\n2\\nENTITIES", extraBlock + "0\\nENDSEC\\n0\\nSECTION\\n2\\nENTITIES")',
  'out.collision = await load(colliding)',
  'out.caretCollision = await load(colliding.replaceAll("2\\nB\\n", "2\\nB^ B\\n").replace("2\\nb\\n", "2\\nB^B\\n"))',
  'out.caret = await load(onlyLine.replaceAll("2\\nB\\n", "2\\nB^ B\\n"))',
  'const latin1Bytes = Buffer.from(onlyLine.replace("2\\nB\\n70\\n", "2\\nB\\n4\\ncaf\\u00e9\\n70\\n").replace("41\\n2\\n42\\n3\\n", "41\\n1\\n42\\n1\\n").replace("50\\n90\\n", "50\\n0\\n"), "latin1")',
  'out.latin1 = await load(latin1Bytes)',
  'const latin1Doc = engine.parseDxf(latin1Bytes)',
  'out.latin1Back = await load(engine.writeDxf(latin1Doc)); latin1Doc.free()',
  'const otherInsert = onlyLine.slice(onlyLine.indexOf("0\\nINSERT\\n"), onlyLine.lastIndexOf("0\\nENDSEC\\n")).replace("5\\n500\\n", "5\\n501\\n").replace("2\\nB\\n", "2\\nC\\n")',
  'const unmatched = onlyLine.replace("0\\nBLOCK\\n5\\n40\\n", "0\\nBLOCK\\n").replace("0\\nENDSEC\\n0\\nSECTION\\n2\\nENTITIES", extraBlock.replace("2\\nb\\n", "2\\nC\\n") + "0\\nENDSEC\\n0\\nSECTION\\n2\\nENTITIES").replace("0\\nENDSEC\\n0\\nEOF", otherInsert + "0\\nENDSEC\\n0\\nEOF")',
  'out.unmatched = await load(unmatched)',
  'out.unmatchedEdited = reply(await handleMessage({ type: "applyEdit", op: "createLine", payload: { x1: 0, y1: 0, x2: 1, y2: 0, layer: "0" } }, engine))',
  'const binary = (text, encoding = "utf8") => {',
  '  const chunks = [Buffer.from("AutoCAD Binary DXF\\r\\n\\x1a\\x00")]',
  '  const pairs = text.trimEnd().split("\\n")',
  '  for (let i = 0; i < pairs.length; i += 2) {',
  '    const code = Number(pairs[i]); const codeBytes = Buffer.alloc(2); codeBytes.writeInt16LE(code); chunks.push(codeBytes)',
  '    if (code >= 10 && code <= 59) { const value = Buffer.alloc(8); value.writeDoubleLE(Number(pairs[i + 1])); chunks.push(value) }',
  '    else if (code >= 60 && code <= 79) { const value = Buffer.alloc(2); value.writeInt16LE(Number(pairs[i + 1])); chunks.push(value) }',
  '    else chunks.push(Buffer.from(pairs[i + 1] + "\\x00", encoding))',
  '  }',
  '  return new Uint8Array(Buffer.concat(chunks))',
  '}',
  'out.binaryCollision = await load(binary(colliding))',
  'out.latin1Collision = await load(binary(colliding.replaceAll("2\\nB\\n", "2\\n\\u00e9\\n").replace("2\\nb\\n", "2\\n\\u00e8\\n"), "latin1"))',
  'out.binary = await load(binary(onlyLine))',
  'out.binaryEdited = reply(await handleMessage({ type: "applyEdit", op: "createLine", payload: { x1: 0, y1: 0, x2: 1, y2: 0, layer: "0" } }, engine))',
  // W4g-7b-02c: createInsert on the real engine, from a fresh load of the
  // original block document. A lower-cased name still resolves (01c-d);
  // a second identical insert gets a distinct handle; an undefined block
  // refuses with the four-key shape and leaves the document untouched.
  'out.insertBefore = await load(dxf)',
  'out.insert = reply(await handleMessage({ type: "applyEdit", op: "createInsert", payload: { name: "b", x: 10, y: 20, rotationDeg: 90, sx: 2, sy: 3, sz: 1, layer: "Refs" } }, engine))',
  'out.insertAgain = reply(await handleMessage({ type: "applyEdit", op: "createInsert", payload: { name: "B", x: 10, y: 20, rotationDeg: 90, sx: 2, sy: 3, sz: 1, layer: "Refs" } }, engine))',
  'out.insertUndefined = reply(await handleMessage({ type: "applyEdit", op: "createInsert", payload: { name: "Nope", x: 0, y: 0, rotationDeg: 0, sx: 1, sy: 1, sz: 1, layer: "" } }, engine))',
  'out.insertIncomplete = reply(await handleMessage({ type: "applyEdit", op: "createInsert", payload: { name: "B", x: 0, y: 0, rotationDeg: 0, sx: 0, sy: 1, sz: 1, layer: "" } }, engine))',
  'process.stdout.write(JSON.stringify(out))',
].join('\n')

describe.skipIf(!GLUE)('W4g-7b-01c blocks through the rebuilt wasm and worker', () => {
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
    for (const op of ['move', 'copy']) expect(out[op]).toMatchObject({ ok: false, reason: 'an INSERT is placed, not edited, in this round' })
    expect(out.move).toEqual({ type: 'editApplied', op: 'move', ok: false, reason: 'an INSERT is placed, not edited, in this round' })
    expect(out.batch).toMatchObject({ ok: false, reason: 'step_1_move:an INSERT is placed, not edited, in this round' })
    // W4g-7b-05c-2: ERASE stays allowed on an INSERT, unlike every verb above.
    expect(out.insertDeleted).toMatchObject({ type: 'editApplied', op: 'delete', ok: true })
    expect(out.insertDeleted.entities.find((entity) => entity.type === 'INSERT')).toBeUndefined()
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
    expect(out.batchRoundtrip.blocks[0].digest).toBe(out.initial.blocks[0].digest)
    expect(diffPlan(out.created, out.batched)).toMatchObject({ count: 1, reason: null })
    expect(diffPlan(out.batched, out.batchRoundtrip)).toEqual({ mutations: {}, count: 0, reason: null })
    // Child 100 has an explicit ByLayer linetype in BLOCK_DXF. The writer
    // omits that default, so equality here must come from its canonical form.
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
    expect(out.array.entities[0]).toMatchObject({ columns: 2, rows: 1, columnSpacing: 10, rowSpacing: 4, scale: [2, 1, 1] })
    expect(engineIntake(out.array).polylines.map((p) => p.pts)).toEqual([
      [[10, 20, 0], [12, 20, 0]], [[20, 20, 0], [22, 20, 0]],
    ])
    expect(diffPlan(out.arrayOne, out.array).reason).toMatch(/INSERT.*cannot carry/)
    for (const collision of [out.collision, out.binaryCollision, out.caretCollision, out.latin1Collision]) {
      expect(collision).toMatchObject({ type: 'documentLoaded', writable: false, refusal: 'block definitions collapsed on load: 2 in the file, 1 retained', entities: [], blocks: [] })
    }
    expect(out.caret.entities).toHaveLength(1)
    expect(out.caret.entities[0]).toMatchObject({ type: 'INSERT', name: 'B^B' })
    expect(out.caret.blocks[0]).toMatchObject({ name: 'B^B', base: [1, 2, 0], baseUnknown: false, complete: true })
    expect(engineIntake(out.caret).polylines[0].pts[0]).toEqual([10, 20, 0])
    expect(out.latin1.blocks[0]).toMatchObject({ base: [1, 2, 0], baseUnknown: false, complete: true })
    expect(engineIntake(out.latin1).polylines[0].pts).toEqual([[10, 20, 0], [13, 20, 0]])
    expect(diffPlan(out.latin1, out.latin1Back)).toEqual({ mutations: {}, count: 0, reason: null })
    expect(out.unmatchedEdited).toMatchObject({ ok: true, blockBasePatched: false })
    for (const result of [out.unmatched, out.unmatchedEdited]) {
      expect(result.blocks.find((b) => b.name === 'B')).toMatchObject({ baseUnknown: true, complete: false })
      expect(result.blocks.find((b) => b.name === 'C')).toMatchObject({ base: [1, 2, 0], baseUnknown: false, complete: true })
      const intake = engineIntake(result)
      expect(intake.polylines.filter((p) => p.sourceHandle != null)).toMatchObject([{ sourceHandle: '501' }])
      expect(intake.inserts).toMatchObject([{ handle: '500', incomplete: true }, { handle: '501', incomplete: false }])
    }
    expect(out.binary.blocks[0]).toMatchObject({ baseUnknown: true, complete: false })
    expect(engineIntake(out.binary).polylines).toEqual([])
    expect(engineIntake(out.binary).inserts).toMatchObject([{ handle: '500', incomplete: true }])
    expect(out.binaryEdited).toMatchObject({ ok: true, blockBasePatched: false })
    expect(out.binaryEdited.blocks[0]).toMatchObject({ baseUnknown: true, complete: false })
    expect(engineIntake(out.binaryEdited).polylines.every((p) => p.sourceHandle == null)).toBe(true)
  })
})
