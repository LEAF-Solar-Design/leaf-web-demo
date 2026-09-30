import { describe, expect, it } from 'vitest'
import { aggregateOverview, fromOverviewPoint, overviewBounds, overviewMap, overviewRect, toOverviewPoint } from './cadOverviewMath.js'

const bounds = (minX, minY, maxX, maxY) => ({ minX, minY, maxX, maxY })

describe('drawing overview coordinates', () => {
  it('preserves aspect ratio with letterboxing on either axis', () => {
    expect(overviewMap(bounds(0, 0, 200, 100), 200, 200, 0)).toMatchObject({ x: 0, y: 50, width: 200, height: 100, scale: 1 })
    expect(overviewMap(bounds(0, 0, 100, 200), 200, 200, 0)).toMatchObject({ x: 50, y: 0, width: 100, height: 200, scale: 1 })
  })

  it('round trips negative drawing coordinates, reversing the SVG Y axis', () => {
    const map = overviewMap(bounds(-300, -100, -100, 0))
    const point = { x: -260, y: -20 }
    const projected = toOverviewPoint(map, point)
    const inverted = fromOverviewPoint(map, projected)
    expect(inverted.x).toBeCloseTo(point.x)
    expect(inverted.y).toBeCloseTo(point.y)
    expect(toOverviewPoint(map, { x: -300, y: 0 })).toEqual({ x: map.x, y: map.y })
    expect(fromOverviewPoint(map, { x: -1000, y: 1000 })).toEqual({ x: -300, y: -100 })
  })

  it.each([bounds(5, 5, 5, 5), bounds(-5, -10, -5, 10), bounds(-10, -5, 10, -5)])('pads degenerate extents without changing the source (%j)', (b) => {
    const original = { ...b }, map = overviewMap(b)
    expect(map.scale).toBeGreaterThan(0)
    expect(map.width).toBeGreaterThan(0)
    expect(map.height).toBeGreaterThan(0)
    expect(Object.values(overviewRect(map, b)).every(Number.isFinite)).toBe(true)
    expect(b).toEqual(original)
  })

  it('clips partial and wholly off-drawing viewports to a visible edge rectangle', () => {
    const map = overviewMap(bounds(0, 0, 100, 100), 100, 100, 0)
    expect(overviewRect(map, bounds(-10, 80, 20, 120))).toEqual({ x: 0, y: 0, width: 20, height: 20 })
    expect(overviewRect(map, bounds(200, 200, 250, 250))).toEqual({ x: 98, y: 0, width: 2, height: 2 })
    expect(overviewRect(map, bounds(-250, -250, -200, -200))).toEqual({ x: 0, y: 98, width: 2, height: 2 })
    expect(overviewRect(map, bounds(-100, -100, 200, 200))).toEqual({ x: 0, y: 0, width: 100, height: 100 })
  })

  it('rejects missing or invalid geometry and invalid overview dimensions', () => {
    for (const b of [null, undefined, {}, bounds(NaN, 0, 10, 10), bounds(10, 0, 0, 10), bounds(0, 0, Infinity, 10)]) {
      expect(overviewMap(b)).toBeNull()
      expect(overviewRect(overviewMap(bounds(0, 0, 10, 10)), b)).toBeNull()
    }
    expect(overviewBounds([{ bounds: null }, { bounds: {} }])).toBeNull()
    expect(overviewMap(bounds(0, 0, 10, 10), 0, 100)).toBeNull()
    expect(overviewMap(bounds(0, 0, 10, 10), 100, NaN)).toBeNull()
    expect(toOverviewPoint(null, { x: 0, y: 0 })).toBeNull()
    expect(fromOverviewPoint(null, { x: 0, y: 0 })).toBeNull()
    expect(fromOverviewPoint(overviewMap(bounds(0, 0, 10, 10)), { x: NaN, y: 0 })).toBeNull()
    expect(aggregateOverview([], null)).toEqual([])
  })

  it('unions all valid bounds and keeps a 2,345-object context within 64 cells', () => {
    const records = Array.from({ length: 2345 }, (_, i) => ({ bounds: bounds(i % 64 - 32, Math.floor(i / 64) - 20, i % 64 - 31, Math.floor(i / 64) - 19) }))
    const union = overviewBounds([...records, { bounds: null }])
    expect(union).toEqual(bounds(-32, -20, 32, 17))
    const cells = aggregateOverview(records, overviewMap(union))
    expect(cells).toHaveLength(64)
    expect(cells.every(({ key, ...rect }) => Object.values(rect).every(Number.isFinite))).toBe(true)
    expect(new Set(cells.map(({ key }) => key)).size).toBe(64)
  })
})
