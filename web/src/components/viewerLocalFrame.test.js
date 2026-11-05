import { describe, expect, it } from 'vitest'
import { drawingOrigin, finiteXY, localPositions } from './viewerLocalFrame.js'

describe('drawing render frame', () => {
  it('derives a shared XY midpoint from polylines, inserts and faces without changing WCS', () => {
    const polys = [{ pts: [[1e9, -1e9, 17], [1e9 + 0.01, -1e9, 19]] }]
    const inserts = [{ pt: [1e9 + 0.04, -1e9 + 0.02, 44] }]
    const faces = [{ p1: [1e9 + 0.02, -1e9 + 0.06, -7] }]
    const before = JSON.stringify([polys, inserts, faces])
    expect(drawingOrigin(polys, inserts, faces)).toEqual({ x: 1e9 + 0.02, y: -1e9 + 0.03 })
    expect(JSON.stringify([polys, inserts, faces])).toBe(before)
  })

  it('subtracts before Float32 conversion and preserves every supplied Z', () => {
    const origin = { x: 1e9, y: -1e9 }
    const input = [1e9, -1e9, 7, 1e9 + 0.01, -1e9 + 0.01, -2]
    const local = new Float32Array(localPositions(input, origin))
    expect(new Float32Array(input)[0]).toBe(new Float32Array(input)[3])
    expect(local[3] - local[0]).toBeCloseTo(0.01, 6)
    expect(local[4] - local[1]).toBeCloseTo(0.01, 6)
    expect([local[2], local[5]]).toEqual([7, -2])
    expect(input[0]).toBe(1e9)
  })

  it('handles empty, degenerate and invalid points and avoids midpoint overflow', () => {
    expect(drawingOrigin()).toEqual({ x: 0, y: 0 })
    expect(drawingOrigin([{ pts: [[NaN, 1], [1, Infinity], null] }])).toEqual({ x: 0, y: 0 })
    expect(drawingOrigin([], [{ pt: [1e9, -1e9] }])).toEqual({ x: 1e9, y: -1e9 })
    expect(drawingOrigin([{ pts: [[1e308, 1e308], [1e308, 1e308]] }])).toEqual({ x: 1e308, y: 1e308 })
    for (const point of [null, [], [NaN, 0], [0, Infinity], ['1', 0]]) expect(finiteXY(point)).toBe(false)
    expect(localPositions([Infinity, 1, 2], { x: 0, y: 0 })).toEqual([])
    expect(localPositions([1e100, 1, 2], { x: 0, y: 0 })).toEqual([])
    expect(localPositions([], { x: 0, y: 0 })).toEqual([])
  })
})
