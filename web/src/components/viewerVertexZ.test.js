import { describe, expect, it } from 'vitest'
import { vertexZ, vertexSegments } from './viewerVertexZ.js'
import { bridgeToIntake } from '../localCad/bridgeIntake.js'
import { expandBulgedPolylines } from '../cadedit/engineIntake.js'

describe('bridge vertex elevation', () => {
  it.each([undefined, null, NaN, Infinity, -Infinity, '12', true, {}, [], Symbol('z')])('defaults malformed Z %s without coercion', z => {
    const p = [1, 2, z]
    expect(vertexZ(p)).toBe(0)
    const intake = bridgeToIntake({ lines: [{ pts: [p, [3, 4, z]] }] })
    expect(intake.polylines[0].pts[0][2]).toBe(vertexZ(p))
    expect(vertexSegments([p, [3, 4, z]], false)).toEqual([1, 2, 0, 3, 4, 0])
  })

  it('retains negative, large and zero Z without averaging or mutation', () => {
    const pts = Object.freeze([Object.freeze([1, 2, -125]), Object.freeze([3, 4, 1e7]), Object.freeze([5, 6, 0])])
    expect(vertexSegments(pts, false)).toEqual([1, 2, -125, 3, 4, 1e7, 3, 4, 1e7, 5, 6, 0])
    expect(vertexSegments(pts, true).slice(-6)).toEqual([5, 6, 0, 1, 2, -125])
    expect(pts.map(vertexZ)).toEqual([-125, 1e7, 0])
    expect(vertexSegments([], true)).toEqual([])
    expect(vertexSegments([], false)).toEqual([])
  })

  it('retains the existing nonplanar bulge sampler contract instead of interpolating Z', () => {
    const row = { handle: 'curve', layer: 'Curves', pts: [[0, 0, -4], [10, 0, 26]], bulges: [1, 0], closed: false }
    const sampled = expandBulgedPolylines([row])[0]
    expect(sampled.pts.length).toBeGreaterThan(3)
    expect(sampled.pts.slice(0, -1).every(p => p[2] === -4)).toBe(true)
    expect(sampled.pts.at(-1)[2]).toBe(26)
    const zs = vertexSegments(sampled.pts, false).filter((_, i) => i % 3 === 2)
    expect(zs.slice(0, -1).every(z => z === -4)).toBe(true)
    expect(zs.at(-1)).toBe(26)
    expect(sampled.handle).toBe(row.handle)
    expect(sampled.layer).toBe(row.layer)
    expect(row.pts).toEqual([[0, 0, -4], [10, 0, 26]])
  })
})
