// @vitest-environment node
import { describe, expect, it } from 'vitest'

import { entityGeometry, formatUnits, polyArea, polyLength } from './entityMetrics.js'

const SQUARE = [[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0]]

describe('polyArea (the mock engine now imports THIS copy)', () => {
  it('shoelace: unit square scaled', () => {
    expect(polyArea(SQUARE)).toBe(100)
  })
  it('triangle, orientation-independent (absolute)', () => {
    expect(polyArea([[0, 0], [4, 0], [0, 3]])).toBe(6)
    expect(polyArea([[0, 0], [0, 3], [4, 0]])).toBe(6)
  })
})

describe('polyLength', () => {
  it('open path measures its edges; closed adds the closing edge', () => {
    expect(polyLength(SQUARE, false)).toBe(30)
    expect(polyLength(SQUARE, true)).toBe(40)
  })
  it('degenerate inputs measure honestly', () => {
    expect(polyLength([[0, 0]], true)).toBe(0)
    expect(polyLength(null, false)).toBe(0)
    expect(polyLength([[0, 0], [3, 4]], false)).toBe(5)
  })
})

describe('entityGeometry', () => {
  it('closed polyline: vertices, perimeter, area', () => {
    const g = entityGeometry({ pts: SQUARE, closed: true }, 'polyline')
    expect(g).toEqual({ vertices: 4, closed: true, length: 40, area: 100 })
  })
  it('OPEN polyline gets NO area (an open path encloses nothing)', () => {
    const g = entityGeometry({ pts: SQUARE, closed: false }, 'polyline')
    expect(g.area).toBeNull()
    expect(g.length).toBe(30)
  })
  it('insert: position/rotation/scale, absent fields null', () => {
    expect(entityGeometry({ pt: [5, 6, 0], rot: 45, scale: [1, 1, 1] }, 'insert'))
      .toEqual({ position: [5, 6], rotation: 45, scale: [1, 1, 1] })
    expect(entityGeometry({}, 'insert')).toEqual({ position: null, rotation: null, scale: null })
  })
  it('3dface counts its corners; unknown kinds and null entities answer null', () => {
    expect(entityGeometry({ p1: [0, 0], p2: [1, 0], p3: [1, 1], p4: [0, 1] }, '3dface')).toEqual({ corners: 4 })
    expect(entityGeometry({ pts: SQUARE }, 'entity')).toBeNull()
    expect(entityGeometry(null, 'polyline')).toBeNull()
  })
})

describe('formatUnits', () => {
  it('fixed precision, em-dash on non-finite, never "-0.00"', () => {
    expect(formatUnits(1234.567)).toBe('1234.57')
    expect(formatUnits(NaN)).toBe('—')
    expect(formatUnits(Infinity)).toBe('—')
    expect(formatUnits(-0.001)).toBe('0.00')
    expect(formatUnits(-0)).toBe('0.00')
    expect(formatUnits(-1.5)).toBe('-1.50')
  })
})
