import { describe, expect, it } from 'vitest'
import { marqueeMode, worldRect, marqueeHandles } from './marqueeSelection.js'

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
})
