import { describe, expect, it } from 'vitest'
import { OrthographicCamera, Plane, Raycaster, Vector2, Vector3 } from 'three'
import { marqueeMode, worldRect, marqueeHandles, marqueeMapping } from './marqueeSelection.js'

const rect = worldRect({ x: 0, y: 0 }, { x: 10, y: 10 })
const line = (handle, pts, closed = false) => ({ handle, pts, closed })
describe('SSD1-24C directional geometry', () => {
  it.each([
    ['A', [[1, 1], [3, 1]], false, true, true],
    ['B', [[-2, 2], [12, 2]], false, false, true],
    ['C', [[11, 11], [12, 11]], false, false, false],
    ['D', [[-2, 9], [9, 20]], false, false, false],
    ['E', [[0, 5], [5, 5]], false, true, true],
    ['F', [[-1, -1], [11, -1], [11, 11], [-1, 11]], true, false, false],
    ['G', [[-5, 5], [-1, 5], [-1, 15]], false, false, false],
    ['H', [[-3, 5], [-3, 20], [15, 20]], true, false, true],
  ])('SSD1-24C %s window and crossing', (handle, pts, closed, window, crossing) => {
    const polys = [line(handle, pts, closed)]
    expect(marqueeHandles(polys, rect, 'window')).toEqual(window ? [handle] : [])
    expect(marqueeHandles(polys, rect, 'crossing')).toEqual(crossing ? [handle] : [])
  })
  it('SSD1-24C direction uses screen x including equal x', () => {
    expect(marqueeMode({ x: 0 }, { x: 1 })).toBe('window')
    expect(marqueeMode({ x: 1 }, { x: 0 })).toBe('crossing')
    expect(marqueeMode({ x: 1 }, { x: 1 })).toBe('window')
  })
  it('SSD1-24C normalises reversed world corners and refuses nonfinite coordinates', () => {
    expect(worldRect({ x: 10, y: 10 }, { x: 0, y: 0 })).toEqual(rect)
    expect(worldRect({ x: Infinity, y: 0 }, { x: 0, y: 1 })).toBeNull()
    expect(worldRect({ x: 0, y: 0 }, { x: 0, y: NaN })).toBeNull()
  })
  it('SSD1-24C zero-area rectangles select nothing', () => {
    const polys = [line('A', [[0, 0], [10, 10]])]
    expect(marqueeHandles(polys, { ...rect, maxX: 0 }, 'crossing')).toEqual([])
    expect(marqueeHandles(polys, { ...rect, maxY: 0 }, 'crossing')).toEqual([])
  })
  it('SSD1-24C skips hidden layers', () => {
    expect(marqueeHandles([{ ...line('A', [[1, 1]]), layer: 'hidden' }], rect, 'window', p => p.layer !== 'hidden')).toEqual([])
  })
  it('SSD1-24C skips missing handles, empty and nonfinite geometry', () => {
    expect(marqueeHandles([line(null, [[1, 1]]), line(42, [[1, 1]]), line('A', []), line('B', [[1, 1], [NaN, 2]]), line('C', [[1, 1, Infinity]])], rect, 'crossing')).toEqual([])
  })
  it('SSD1-24C deduplicates sampled pieces in first-seen order and freezes the result', () => {
    const result = marqueeHandles([line('circle', [[2, 1], [1, 2], [0, 1], [1, 0]], true), line('B', [[2, 2]]), line('circle', [[1, 1]])], rect, 'window')
    expect(result).toEqual(['circle', 'B'])
    expect(Object.isFrozen(result)).toBe(true)
  })
  it('SSD1-24C processes 2345 polylines in one call', () => {
    const polys = Array.from({ length: 2345 }, (_, i) => line(String(i), [[1, 1], [3, 1]]))
    expect(marqueeHandles(polys, rect, 'window')).toEqual(polys.map(p => p.handle))
  })
  it('requires every visible projected piece of a block inside a window', () => {
    const polys = [line('10', [[1, 1], [2, 1]]), line('20', [[3, 3], [4, 3]]),
      line('10', [[9, 9], [12, 9]])]
    expect(marqueeHandles(polys, rect, 'window')).toEqual(['20'])
    expect(marqueeHandles(polys, rect, 'crossing')).toEqual(['10', '20'])
    expect(marqueeHandles([...polys].reverse(), rect, 'window')).toEqual(['20'])
  })
  it('ignores hidden pieces when containing a visible entity', () => {
    const polys = [{ ...line('10', [[1, 1], [2, 1]]), layer: 'shown' },
      { ...line('10', [[9, 9], [12, 9]]), layer: 'hidden' }]
    expect(marqueeHandles(polys, rect, 'window', p => p.layer !== 'hidden')).toEqual(['10'])
  })
})

describe('client-space marquee', () => {
  const bounds = { left: 45, top: 32, width: 640, height: 320 }
  const start = { x: 280, y: 130 }, end = { x: 390, y: 210 }
  function cameraAt(x, y, z) {
    const camera = new OrthographicCamera(-32, 32, 16, -16, -200, 200)
    camera.position.set(x, y, z)
    camera.lookAt(2, -3, 0)
    camera.zoom = 1.4
    camera.updateProjectionMatrix()
    camera.updateMatrixWorld()
    return camera
  }

  it.each([[20, 12, 30], [-23, 18, 14], [6, -35, 9]])(
    'selects the drawn box at camera (%s, %s, %s)', (x, y, z) => {
      const camera = cameraAt(x, y, z)
      // Specify the oracle directly in client pixels. Cast rays onto the
      // drawing plane to construct world fixtures independently of the mapper.
      const fixture = (handle, pixels, closed) => line(handle, pixels.map(([u, v]) => {
        const ray = new Raycaster()
        ray.setFromCamera(new Vector2(2 * (u - bounds.left) / bounds.width - 1,
          1 - 2 * (v - bounds.top) / bounds.height), camera)
        return ray.ray.intersectPlane(new Plane(new Vector3(0, 0, 1), 0), new Vector3()).toArray()
      }), closed)
      const polys = [
        fixture('edge', [[280, 130], [280, 185], [340, 190]]),
        fixture('bend outside', [[260, 180], [260, 100], [340, 100]]),
        fixture('bend crosses', [[260, 180], [405, 180], [405, 220]]),
        fixture('closing edge', [[260, 175], [260, 100], [405, 100]], true),
        fixture('block', [[310, 155], [320, 170]]),
        fixture('block', [[420, 180], [430, 190]]),
        fixture('gap', [[250, 170], [260, 170]]),
        fixture('gap', [[405, 170], [415, 170]]),
        { ...fixture('edge', [[420, 180], [430, 190]]), layer: 'hidden' },
        line('invalid', [...fixture('invalid', [[310, 155]]).pts, [2, 3, NaN]]),
      ]
      for (const [a, b, expected] of [
        [start, end, ['edge']],
        [end, start, ['edge', 'bend crosses', 'closing edge', 'block']],
      ]) {
        const { rect, project } = marqueeMapping(camera, bounds, a, b)
        const visible = pl => pl.layer !== 'hidden'
        expect(marqueeHandles(polys, rect, marqueeMode(a, b), visible, project)).toEqual(expected)
      }
    })

  it('keeps the flat view in world coordinates and refuses unusable mappings', () => {
    const camera = cameraAt(2, -3, 30)
    const flat = marqueeMapping(camera, bounds, start, end)
    expect(flat.project).toBeUndefined()
    const polys = [line('inside', [[2, -3], [3, -3]]), line('through', [[-20, -3], [20, -3]])]
    expect(marqueeHandles(polys, flat.rect, 'window')).toEqual(['inside'])
    expect(marqueeHandles(polys, flat.rect, 'crossing')).toEqual(['inside', 'through'])
    expect(marqueeMapping(camera, { ...bounds, width: 0 }, start, end)).toBeNull()
    expect(marqueeMapping(camera, { ...bounds, height: NaN }, start, end)).toBeNull()
    expect(marqueeMapping(cameraAt(30, -3, 0), bounds, start, end)).toBeNull()
    expect(marqueeMapping(camera, bounds, start, { x: Infinity, y: 2 })).toBeNull()
    camera.left = camera.right
    camera.updateProjectionMatrix()
    expect(marqueeMapping(camera, bounds, start, end)).toBeNull()
  })
})
