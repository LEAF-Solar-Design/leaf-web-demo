import { describe, expect, it } from 'vitest'
import { worldToScreen, screenToWorld, boundsOfRects, fitCameraToBounds } from './worldSpaceGeometry.js'

const viewport = Object.freeze({ width: 800, height: 600 })
const camera = Object.freeze({ x: 30, y: -20, zoom: 2 })

describe('world-space transforms', () => {
  it('uses screen-unit translation with negative world coordinates', () => {
    expect(worldToScreen({ x: -10, y: 5 }, camera)).toEqual({ x: 10, y: -10 })
  })

  it.each([0.25, 1, 2, 3.7])('inverts both transforms at zoom %s without mutation', zoom => {
    const view = Object.freeze({ x: -31.5, y: 48, zoom })
    const point = Object.freeze({ x: -123.25, y: 71.5 })
    for (const result of [screenToWorld(worldToScreen(point, view), view), worldToScreen(screenToWorld(point, view), view)]) {
      expect(result.x).toBeCloseTo(point.x)
      expect(result.y).toBeCloseTo(point.y)
    }
    expect(point).toEqual({ x: -123.25, y: 71.5 })
    expect(view).toEqual({ x: -31.5, y: 48, zoom })
  })

  it.each([NaN, Infinity, -Infinity, '1', null, undefined])('rejects invalid numbers %s', value => {
    for (const transform of [worldToScreen, screenToWorld]) {
      for (const field of ['x', 'y']) {
        expect(() => transform({ x: 0, y: 0, [field]: value }, camera)).toThrow()
        expect(() => transform({ x: 0, y: 0 }, { ...camera, [field]: value })).toThrow()
      }
      expect(() => transform({ x: 0, y: 0 }, { ...camera, zoom: value })).toThrow()
    }
  })

  it.each([0, -1])('rejects nonpositive zoom %s', zoom => {
    expect(() => worldToScreen({ x: 0, y: 0 }, { ...camera, zoom })).toThrow()
    expect(() => screenToWorld({ x: 0, y: 0 }, { ...camera, zoom })).toThrow()
  })
})

describe('bounds and camera fitting', () => {
  it('unions rectangles across negative coordinates without mutation', () => {
    const rects = Object.freeze([
      Object.freeze({ x: -30, y: -20, width: 10, height: 40 }),
      Object.freeze({ x: 5, y: -40, width: 15, height: 10 }),
    ])
    expect(boundsOfRects(rects)).toEqual({ x: -30, y: -40, width: 50, height: 60 })
    expect(rects[0]).toEqual({ x: -30, y: -20, width: 10, height: 40 })
  })

  it('returns null for empty rectangles and the default camera for empty bounds', () => {
    expect(boundsOfRects([])).toBeNull()
    for (const empty of [null, undefined, []]) {
      expect(fitCameraToBounds(empty, viewport, 20)).toEqual({ x: 0, y: 0, zoom: 1 })
    }
  })

  it('fits known bounds to the limiting dimension with padding', () => {
    const bounds = Object.freeze({ x: -100, y: -50, width: 200, height: 100 })
    expect(fitCameraToBounds(bounds, viewport, 100)).toEqual({ x: 400, y: 300, zoom: 3 })
    expect(fitCameraToBounds({ x: 10, y: 20, width: 100, height: 200 }, viewport, 100))
      .toEqual({ x: 280, y: 60, zoom: 2 })
    expect(bounds).toEqual({ x: -100, y: -50, width: 200, height: 100 })
    expect(viewport).toEqual({ width: 800, height: 600 })
  })

  it('centers a point at zoom one and fits each nonzero dimension of a line', () => {
    const point = { x: -20, y: 40, width: 0, height: 0 }
    expect(boundsOfRects([point])).toEqual(point)
    expect(fitCameraToBounds(point, viewport)).toEqual({ x: 420, y: 260, zoom: 1 })
    expect(fitCameraToBounds({ x: 10, y: -20, width: 0, height: 100 }, viewport, 50))
      .toEqual({ x: 350, y: 150, zoom: 5 })
    expect(fitCameraToBounds({ x: -20, y: 10, width: 100, height: 0 }, viewport, 50))
      .toEqual({ x: 190, y: 230, zoom: 7 })
  })

  it.each([NaN, Infinity, -Infinity, '1', null, undefined])('rejects invalid geometry numbers %s', value => {
    for (const field of ['x', 'y', 'width', 'height']) {
      const rect = { x: 0, y: 0, width: 1, height: 1, [field]: value }
      expect(() => boundsOfRects([rect])).toThrow()
      expect(() => fitCameraToBounds(rect, viewport, 0)).toThrow()
    }
    for (const field of ['width', 'height']) {
      expect(() => fitCameraToBounds(null, { ...viewport, [field]: value }, 0)).toThrow()
    }
    if (value !== undefined) expect(() => fitCameraToBounds(null, viewport, value)).toThrow()
  })

  it('rejects negative dimensions and invalid viewport or padding even for empty bounds', () => {
    for (const field of ['width', 'height']) {
      const rect = { x: 0, y: 0, width: 1, height: 1, [field]: -1 }
      expect(() => boundsOfRects([rect])).toThrow()
      expect(() => fitCameraToBounds(rect, viewport)).toThrow()
      for (const value of [0, -1]) {
        expect(() => fitCameraToBounds(null, { ...viewport, [field]: value })).toThrow()
      }
    }
    for (const padding of [-1, 300, 400]) expect(() => fitCameraToBounds(null, viewport, padding)).toThrow()
    expect(() => fitCameraToBounds(null, { width: 100, height: 600 }, 50)).toThrow()
    expect(() => boundsOfRects(null)).toThrow()
    expect(() => boundsOfRects([null])).toThrow()
    expect(() => fitCameraToBounds({}, viewport)).toThrow()
  })
})
