import { describe, expect, it } from 'vitest'

import { ARC_STEP_DEG, CIRCLE_SEGMENTS, MAX_POINTS, MIN_ARC_POINTS, engineIntake, entityToPolyline, hexHandle } from './engineIntake.js'

const near = (a, b, eps = 1e-9) => Math.abs(a - b) < eps

describe('engineIntake (W4f slice A0): engine entities -> viewer intake', () => {
  it('W4g-1b: the intake handle is the DXF hex form of the worker\'s decimal id, so a canvas pick names the drawing\'s own handle', () => {
    expect(hexHandle('37986')).toBe('9462')
    expect(hexHandle('7')).toBe('7')
    expect(hexHandle('255')).toBe('FF')
    expect(hexHandle('18446744073709551615')).toBe('FFFFFFFFFFFFFFFF')
    expect(hexHandle('e1')).toBe('e1')
    expect(hexHandle('')).toBe('')
    expect(hexHandle(undefined)).toBe('')
    const line = entityToPolyline({ id: '37986', type: 'LINE', layer: 'Panels', closed: false, vertices: [[0, 0, 0], [1, 1, 0]] })
    expect(line.handle).toBe('9462')
  })

  it('maps a line and a polyline as they are, keyed by the engine handle, layer and closed flag', () => {
    const line = entityToPolyline({ id: '7', type: 'LINE', layer: 'Panels', closed: false, vertices: [[0, 0, 0], [100, 50, 0]] })
    expect(line).toEqual({ handle: '7', layer: 'Panels', pts: [[0, 0, 0], [100, 50, 0]], closed: false })
    const poly = entityToPolyline({ id: '8', type: 'LWPOLYLINE', layer: 'Outline', closed: true, vertices: [[0, 0], [4, 0], [4, 3]] })
    expect(poly).toEqual({ handle: '8', layer: 'Outline', pts: [[0, 0, 0], [4, 0, 0], [4, 3, 0]], closed: true })
  })

  it('draws a circle as a closed 48-gon on its centre and radius', () => {
    const pl = entityToPolyline({ id: '9', type: 'CIRCLE', layer: '0', vertices: [[3, 3, 0]], radius: 1.5, startDeg: null, endDeg: null })
    expect(pl.closed).toBe(true)
    expect(pl.pts).toHaveLength(CIRCLE_SEGMENTS)
    for (const [x, y] of pl.pts) expect(near(Math.hypot(x - 3, y - 3), 1.5)).toBe(true)
  })

  it('samples an arc counter-clockwise from start to end in degrees, wrapping through 360 when the end is below the start', () => {
    const quarter = entityToPolyline({ id: '10', type: 'ARC', layer: '0', vertices: [[0, 0, 0]], radius: 2, startDeg: 0, endDeg: 90 })
    expect(quarter.closed).toBe(false)
    expect(quarter.pts).toHaveLength(Math.max(MIN_ARC_POINTS, Math.ceil(90 / ARC_STEP_DEG) + 1))
    expect(near(quarter.pts[0][0], 2) && near(quarter.pts[0][1], 0)).toBe(true)
    const last = quarter.pts[quarter.pts.length - 1]
    expect(near(last[0], 0) && near(last[1], 2)).toBe(true)
    const wrap = entityToPolyline({ id: '11', type: 'ARC', layer: '0', vertices: [[0, 0, 0]], radius: 1, startDeg: 350, endDeg: 10 })
    expect(wrap.pts).toHaveLength(MIN_ARC_POINTS)
    const mid = wrap.pts[Math.floor(wrap.pts.length / 2)]
    expect(mid[0]).toBeGreaterThan(0.99)
  })

  it('skips what it cannot draw instead of throwing: bad points, missing radius, one-point lines, foreign kinds', () => {
    expect(entityToPolyline(null)).toBeNull()
    expect(entityToPolyline({ id: '1', type: 'LINE', vertices: [[0, 0]] })).toBeNull()
    expect(entityToPolyline({ id: '2', type: 'LINE', vertices: [[0, 'x'], [1, 1]] })).toBeNull()
    expect(entityToPolyline({ id: '3', type: 'CIRCLE', vertices: [[0, 0]], radius: 0 })).toBeNull()
    expect(entityToPolyline({ id: '4', type: 'CIRCLE', vertices: [[0, 0]] })).toBeNull()
    expect(entityToPolyline({ id: '5', type: 'ARC', vertices: [[0, 0]], radius: 1, startDeg: NaN, endDeg: 90 })).toBeNull()
    expect(entityToPolyline({ id: '6', type: 'OTHER', vertices: [] })).toBeNull()
  })

  it('builds the intake shape the viewer draws, counts points, and truncates honestly past the cap', () => {
    const intake = engineIntake([
      { id: '1', type: 'LINE', layer: 'A', vertices: [[0, 0], [1, 0]] },
      { id: '2', type: 'OTHER', vertices: [] },
      { id: '3', type: 'CIRCLE', layer: 'B', vertices: [[0, 0]], radius: 1 },
    ], 'one.dxf')
    expect(intake.source).toBe('engine')
    expect(intake.documentId).toBe('one.dxf')
    expect(intake.polylines.map((p) => p.handle)).toEqual(['1', '3'])
    expect(intake.inserts).toEqual([])
    expect(intake.faces3d).toEqual([])
    expect(intake.points).toBe(2 + CIRCLE_SEGMENTS)
    expect(intake.truncated).toBe(0)
    const many = Array.from({ length: Math.ceil(MAX_POINTS / 2) + 5 }, (_, i) => ({ id: String(i), type: 'LINE', vertices: [[i, 0], [i, 1]] }))
    const capped = engineIntake(many)
    expect(capped.points).toBeLessThanOrEqual(MAX_POINTS)
    expect(capped.truncated).toBe(5)
    expect(engineIntake(undefined)).toMatchObject({ polylines: [], points: 0, truncated: 0 })
  })
})
