import { describe, expect, it } from 'vitest'

import { computeEntityDelta, scopeDeltaToHandle } from './entityDelta.js'

const base = { polylines: [{ handle: 'AB12', pts: [0, 0, 1, 1] }, { handle: 'CD34', pts: [2, 2] }] }

describe('computeEntityDelta', () => {
  it('returns null when either intake cannot be parsed', () => {
    expect(computeEntityDelta(null, base)).toBeNull()
    expect(computeEntityDelta(base, undefined)).toBeNull()
    expect(computeEntityDelta('not an object', base)).toBeNull()
  })

  it('reports zero touched entities for an identical intake', () => {
    const delta = computeEntityDelta(base, JSON.parse(JSON.stringify(base)))
    expect(delta.touched).toEqual([])
  })

  it('reports a modified handle when its payload changes', () => {
    const candidate = { polylines: [{ handle: 'AB12', pts: [0, 0, 9, 9] }, { handle: 'CD34', pts: [2, 2] }] }
    const delta = computeEntityDelta(base, candidate)
    expect(delta.touched).toEqual([{ key: 'polylines:h:AB12', handle: 'AB12', change: 'modified' }])
  })

  it('reports an added handle', () => {
    const candidate = { polylines: [...base.polylines, { handle: 'EF56', pts: [5, 5] }] }
    const delta = computeEntityDelta(base, candidate)
    expect(delta.touched).toEqual([{ key: 'polylines:h:EF56', handle: 'EF56', change: 'added' }])
  })

  it('reports a deleted handle', () => {
    const candidate = { polylines: [base.polylines[0]] }
    const delta = computeEntityDelta(base, candidate)
    expect(delta.touched).toEqual([{ key: 'polylines:h:CD34', handle: 'CD34', change: 'deleted' }])
  })

  it('falls back to a content hash for a handle-less entity, across polylines/inserts/faces3d', () => {
    const b = { inserts: [{ block: 'panel', pts: [0, 0] }] }
    const c = { inserts: [{ block: 'panel', pts: [1, 1] }] }
    const delta = computeEntityDelta(b, c)
    expect(delta.touched).toHaveLength(2)
    expect(delta.touched.every((t) => t.handle === null)).toBe(true)
    expect(new Set(delta.touched.map((t) => t.change))).toEqual(new Set(['added', 'deleted']))
  })
})

describe('scopeDeltaToHandle', () => {
  it('is vacuously scoped when nothing was touched', () => {
    expect(scopeDeltaToHandle({ touched: [] }, 'AB12')).toEqual({ scoped: true, touched: [], outside: [] })
  })

  it('is scoped when every touched entity carries the target handle', () => {
    const delta = { touched: [{ key: 'polylines:h:AB12', handle: 'AB12', change: 'modified' }] }
    expect(scopeDeltaToHandle(delta, 'AB12').scoped).toBe(true)
  })

  it('refuses when a touched entity carries a different handle', () => {
    const delta = {
      touched: [
        { key: 'polylines:h:AB12', handle: 'AB12', change: 'modified' },
        { key: 'polylines:h:CD34', handle: 'CD34', change: 'modified' },
      ],
    }
    const verdict = scopeDeltaToHandle(delta, 'AB12')
    expect(verdict.scoped).toBe(false)
    expect(verdict.outside).toEqual([{ key: 'polylines:h:CD34', handle: 'CD34', change: 'modified' }])
  })

  it('refuses a touched entity with no stable handle rather than assume it is the target', () => {
    const delta = { touched: [{ key: 'polylines:c:deadbeef', handle: null, change: 'added' }] }
    expect(scopeDeltaToHandle(delta, 'AB12').scoped).toBe(false)
  })

  it('refuses an uncomputable delta', () => {
    expect(scopeDeltaToHandle(null, 'AB12').scoped).toBe(false)
  })
})
