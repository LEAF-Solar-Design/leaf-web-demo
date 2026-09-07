// W4g-3b: the browser edit as a plan (mutationDiff.js), pure rows.
import { describe, expect, it } from 'vitest'
import { COORDINATE_EPSILON, MAX_PLAN_OPERATIONS, diffPlan, planGeometry } from './mutationDiff.js'

// The worker's projection: decimal ids (the intake's hex "A" is 10, "B" 11, "C1" 193).
const line = (id, extra = {}) => ({ id: String(id), type: 'LINE', layer: '0', closed: false, vertices: [[0, 0, 0], [3, 4, 0]], radius: null, startDeg: null, endDeg: null, ...extra })
const poly = (id, extra = {}) => ({ id: String(id), type: 'LWPOLYLINE', layer: 'Panels', closed: true, vertices: [[0, 0, 0], [2, 0, 0], [2, 2, 0], [0, 2, 0]], radius: null, startDeg: null, endDeg: null, ...extra })
const circle = (id, extra = {}) => ({ id: String(id), type: 'CIRCLE', layer: 'Round', closed: true, vertices: [[10, 10, 0]], radius: 3, startDeg: null, endDeg: null, ...extra })
const arc = (id, extra = {}) => ({ id: String(id), type: 'ARC', layer: 'Round', closed: false, vertices: [[20, 0, 0]], radius: 2, startDeg: 0, endDeg: 90, ...extra })
const text = (id) => ({ id: String(id), type: 'TEXT', layer: '0', closed: false, vertices: [], radius: null, startDeg: null, endDeg: null })

describe('planGeometry', () => {
  it('reads each kind into the contract terms and leaves the rest out', () => {
    expect(planGeometry(line(10))).toEqual({ kind: 'LINE', layer: '0', pts: [[0, 0, 0], [3, 4, 0]] })
    // W4g-6d: a polyline's geometry also carries its bulges and the curved flag (straight here).
    expect(planGeometry(poly(11))).toMatchObject({ bulges: [0, 0, 0, 0], curved: false })
    expect(planGeometry(poly(11))).toMatchObject({ kind: 'LWPOLYLINE', layer: 'Panels', closed: true, pts: [[0, 0, 0], [2, 0, 0], [2, 2, 0], [0, 2, 0]] })
    expect(planGeometry(circle(193))).toEqual({ kind: 'CIRCLE', layer: 'Round', c: [10, 10, 0], r: 3 })
    expect(planGeometry(arc(209))).toEqual({ kind: 'ARC', layer: 'Round', c: [20, 0, 0], r: 2, start_deg: 0, end_deg: 90 })
    expect(planGeometry(text(12))).toBeNull()
    expect(planGeometry({ ...poly(11), type: 'POLYLINE', closed: false, vertices: [[0, 0], [1, 1]] })).toMatchObject({ kind: 'LWPOLYLINE', layer: 'Panels', closed: false, pts: [[0, 0, 0], [1, 1, 0]] })
  })

  it('refuses a malformed entity instead of guessing', () => {
    expect(planGeometry(null)).toBeNull()
    expect(planGeometry({ ...line(10), vertices: [[0, 0, 0]] })).toBeNull()
    expect(planGeometry({ ...line(10), vertices: [[0, 0, 0], [Number.NaN, 4, 0]] })).toBeNull()
    expect(planGeometry({ ...circle(193), radius: 0 })).toBeNull()
    expect(planGeometry({ ...arc(209), endDeg: undefined })).toBeNull()
    expect(planGeometry({ ...poly(11), vertices: [[0, 0, 0]] })).toBeNull()
  })
})

describe('diffPlan', () => {
  it('names nothing when nothing the contract sees changed, epsilon included', () => {
    const before = [line(10), poly(11), circle(193), arc(209), text(12)]
    const after = [line(10, { vertices: [[0, 0, 0], [3 + COORDINATE_EPSILON / 2, 4, 0]] }), poly(11), circle(193), arc(209), text(12)]
    expect(diffPlan(before, after)).toEqual({ mutations: {}, count: 0, reason: null })
  })

  it('emits every op kind, handles in hex, lists sorted', () => {
    const before = [line(10), poly(11), circle(193), arc(209), poly(12)]
    const after = [
      line(10, { layer: 'Moved' }),
      poly(11, { vertices: [[0, 0, 0], [4, 0, 0], [4, 4, 0], [0, 4, 0]] }),
      circle(193, { radius: 4, layer: 'Elsewhere' }),
      arc(209, { startDeg: 10, endDeg: 100 }),
      // 12 removed; 500 (1F4) and 400 (190) added
      circle(500, { vertices: [[1, 2, 0]], radius: 0.5 }),
      line(400, { vertices: [[5, 5, 0], [9, 9, 0]] }),
    ]
    const { mutations, count, reason } = diffPlan(before, after)
    expect(reason).toBeNull()
    // 2 adds, 1 removal, 2 relayers, 1 set_points, 1 set_circle, 1 set_arc.
    expect(count).toBe(8)
    expect(mutations).toEqual({
      added: [
        { handle: '190', kind: 'LINE', layer: '0', pts: [[5, 5, 0], [9, 9, 0]] },
        { handle: '1F4', kind: 'CIRCLE', layer: 'Round', c: [1, 2, 0], r: 0.5 },
      ],
      removed: ['C'],
      set_layer: [{ handle: 'A', layer: 'Moved' }, { handle: 'C1', layer: 'Elsewhere' }],
      set_points: [{ handle: 'B', closed: true, pts: [[0, 0, 0], [4, 0, 0], [4, 4, 0], [0, 4, 0]] }],
      set_circle: [{ handle: 'C1', c: [10, 10, 0], r: 4 }],
      set_arc: [{ handle: 'D1', c: [20, 0, 0], r: 2.5 - 0.5, start_deg: 10, end_deg: 100 }],
    })
  })

  it('reads a LINE replacement as a two-point open polyline and an added polyline as itself', () => {
    const before = [line(10)]
    const after = [line(10, { vertices: [[2, 3, 0], [7, 8, 0]] }), poly(600, { closed: false, vertices: [[0, 0, 0], [5, 5, 0], [10, 0, 0]] })]
    expect(diffPlan(before, after).mutations).toEqual({
      added: [{ handle: '258', layer: 'Panels', closed: false, pts: [[0, 0, 0], [5, 5, 0], [10, 0, 0]] }],
      set_points: [{ handle: 'A', closed: false, pts: [[2, 3, 0], [7, 8, 0]] }],
    })
  })

  it('refuses a handle that changed kind and a plan past the operation bound', () => {
    expect(diffPlan([circle(193)], [line(193)])).toEqual({
      mutations: null, count: 0, reason: 'entity C1 changed kind from CIRCLE to LINE, which the plan cannot express',
    })
    const many = Array.from({ length: MAX_PLAN_OPERATIONS + 1 }, (_, i) => line(1000 + i))
    const over = diffPlan([], many)
    expect(over.mutations).toBeNull()
    expect(over.count).toBe(MAX_PLAN_OPERATIONS + 1)
    expect(over.reason).toMatch(/over the 5000/)
  })

  it('leaves a duplicated handle out of the plan on both sides', () => {
    const before = [line(10), line(10, { layer: 'Twice' })]
    const after = [line(10, { layer: 'Moved' }), line(10)]
    expect(diffPlan(before, after)).toEqual({ mutations: {}, count: 0, reason: null })
  })
})

describe('W4g-6d: what the contract cannot carry is refused, never dropped', () => {
  const B = Math.tan(Math.PI / 8)
  it('TEXT and read-only foreign kinds remain opaque to the plan', () => {
    const t = (extra = {}) => ({ id: '12', type: 'TEXT', layer: '0', closed: false, vertices: [[1, 1, 0]], radius: null, startDeg: null, endDeg: null, text: 'hi', height: 2.5, rotationDeg: 0, editable: true, ...extra })
    expect(diffPlan([line(10)], [line(10), t()]).reason).toBe('entity C is a TEXT the plan cannot carry, and it was added')
    expect(diffPlan([line(10), t()], [line(10)]).reason).toBe('entity C is a TEXT the plan cannot carry, and it was removed')
    expect(diffPlan([line(10), t()], [line(10), t({ vertices: [[5, 5, 0]] })]).reason).toBe('entity C is a TEXT the plan cannot carry, and it changed')
    expect(diffPlan([line(10), t()], [line(10), t({ text: 'bye' })]).reason).toBe('entity C is a TEXT the plan cannot carry, and it changed')
    expect(diffPlan([line(10), t()], [line(10, { layer: 'Moved' }), t()])).toEqual({ mutations: { set_layer: [{ handle: 'A', layer: 'Moved' }] }, count: 1, reason: null })
    // Read-only references are still seen if a raw operation changes them.
    const insert = { id: '13', type: 'INSERT', layer: '0', closed: false, vertices: [], radius: null, startDeg: null, endDeg: null, editable: false }
    expect(diffPlan([line(10), insert], [line(10)]).reason).toBe('entity D is a INSERT the plan cannot carry, and it was removed')
  })

  it('a curved polyline: unchanged or relayered is fine, moved or filleted or added refuses, removed is a plain removal', () => {
    const curved = (extra = {}) => poly(11, { bulges: [0, B, 0, 0], ...extra })
    expect(diffPlan([curved()], [curved()])).toEqual({ mutations: {}, count: 0, reason: null })
    expect(diffPlan([curved()], [curved({ layer: 'Elsewhere' })])).toEqual({ mutations: { set_layer: [{ handle: 'B', layer: 'Elsewhere' }] }, count: 1, reason: null })
    expect(diffPlan([curved()], [curved({ vertices: [[1, 0, 0], [3, 0, 0], [3, 2, 0], [1, 2, 0]] })]).reason).toBe('polyline B has curved segments the plan cannot carry')
    // The corner fillet of W4g-6d: a straight square gains a bulge (and a vertex).
    const filleted = poly(11, { vertices: [[0, 0, 0], [2, 0, 0], [2, 1, 0], [1, 2, 0], [0, 2, 0]], bulges: [0, 0, B, 0, 0] })
    expect(diffPlan([poly(11)], [filleted]).reason).toBe('polyline B has curved segments the plan cannot carry')
    expect(diffPlan([poly(11)], [poly(11, { bulges: [0, 0, 0, 0] })])).toEqual({ mutations: {}, count: 0, reason: null })
    expect(diffPlan([], [curved()]).reason).toBe('polyline B has curved segments the plan cannot carry')
    expect(diffPlan([curved()], [])).toEqual({ mutations: { removed: ['B'] }, count: 1, reason: null })
    // A bulge list that does not match its points is curved for this purpose too (never read as straight).
    expect(diffPlan([poly(11)], [poly(11, { vertices: [[0, 0, 0], [3, 0, 0], [3, 3, 0], [0, 3, 0]], bulges: [0.1] })]).reason).toBe('polyline B has curved segments the plan cannot carry')
  })
})

describe('W4g-7b-01c: references and definitions are opaque', () => {
  const insert = { id: '1280', handle: '1280', type: 'INSERT', name: 'B', ip: [10, 20, 0], rotationDeg: 90, scale: [2, 3, 1], layer: '0', editable: false }
  const block = { name: 'B', base: [1, 2, 0], complete: true, children: [line(256, { editable: false })] }
  const projection = (entity = insert, definition = block) => ({ entities: [entity], blocks: [definition] })

  it('an unchanged document with a block and INSERT produces an empty plan', () => {
    expect(diffPlan(projection(), structuredClone(projection()))).toEqual({ mutations: {}, count: 0, reason: null })
    const list = Object.assign([insert], { blocks: [block] })
    expect(diffPlan(list, structuredClone(list))).toEqual({ mutations: {}, count: 0, reason: null })
  })

  it.each([
    { name: 'Other' }, { ip: [11, 20, 0] }, { rotationDeg: 180 },
    { scale: [-2, 3, 1] }, { layer: 'Elsewhere' },
    { columns: 2 }, { rows: 2 }, { columnSpacing: 10 }, { rowSpacing: 10 },
  ])('refuses changed INSERT fields: %j', (change) => {
    expect(diffPlan(projection(), projection({ ...insert, ...change }))).toEqual({
      mutations: null, count: 0, reason: 'entity 500 is a INSERT the plan cannot carry, and it changed',
    })
  })

  it.each([
    { base: [0, 0, 0] }, { complete: false },
    { children: [line(256, { editable: false, vertices: [[1, 1, 0], [2, 2, 0]] })] },
  ])('refuses a changed block definition: %j', (change) => {
    const result = diffPlan(projection(), projection(insert, { ...block, ...change }))
    expect(result.mutations).toBeNull()
    expect(result.reason).toBe('block B is a definition the plan cannot carry, and it was changed')
  })

  it('refuses added and removed definitions and unknown read-only kinds', () => {
    expect(diffPlan({ entities: [], blocks: [] }, { entities: [], blocks: [block] }).reason).toMatch(/cannot carry.*added/)
    expect(diffPlan({ entities: [], blocks: [block] }, { entities: [], blocks: [] }).reason).toMatch(/cannot carry.*removed/)
    const foreign = { id: '123', type: 'FUTURE', editable: false, vertices: [[0, 0, 0], [1, 1, 0]] }
    expect(diffPlan([foreign], [{ ...foreign, vertices: [[2, 2, 0], [3, 3, 0]] }]).reason).toMatch(/FUTURE.*cannot carry/)
    expect(diffPlan([foreign], [foreign])).toEqual({ mutations: {}, count: 0, reason: null })
  })

  it('refuses a changed digest even when listed children are unchanged', () => {
    const before = projection(insert, { ...block, complete: false, digest: 'a010000000000001' })
    const after = structuredClone(before)
    after.blocks[0].digest = 'a010000000000002'
    expect(before.blocks[0].children).toEqual(after.blocks[0].children)
    expect(diffPlan(before, after).reason).toMatch(/definition.*cannot carry/)
    expect(diffPlan(before, structuredClone(before))).toEqual({ mutations: {}, count: 0, reason: null })
  })

  it('uses the full digest rather than the bounded drawing catalogue', () => {
    const before = projection(insert, { ...block, complete: false, digest: 'a010000000000001' })
    const after = projection(insert, { ...before.blocks[0], children: [] })
    expect(diffPlan(before, after)).toEqual({ mutations: {}, count: 0, reason: null })
  })
})
