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
    expect(planGeometry(poly(11))).toEqual({ kind: 'LWPOLYLINE', layer: 'Panels', closed: true, pts: [[0, 0, 0], [2, 0, 0], [2, 2, 0], [0, 2, 0]] })
    expect(planGeometry(circle(193))).toEqual({ kind: 'CIRCLE', layer: 'Round', c: [10, 10, 0], r: 3 })
    expect(planGeometry(arc(209))).toEqual({ kind: 'ARC', layer: 'Round', c: [20, 0, 0], r: 2, start_deg: 0, end_deg: 90 })
    expect(planGeometry(text(12))).toBeNull()
    expect(planGeometry({ ...poly(11), type: 'POLYLINE', closed: false, vertices: [[0, 0], [1, 1]] })).toEqual({ kind: 'LWPOLYLINE', layer: 'Panels', closed: false, pts: [[0, 0, 0], [1, 1, 0]] })
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
