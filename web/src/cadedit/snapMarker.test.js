// B1b: the snap marker glyphs. Every row pins the measured segments at
// (10, 20), size 2, from the record's numerical contract (section 6): a path
// is listed point by point and every adjacent pair is one segment.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

import { snapMarkerSegments } from './snapMarker.js'

const AT = { x: 10, y: 20 }
const SIZE = 2
const segmentsOf = (...paths) => paths.flatMap((path) => path.slice(1).map((p, i) => [path[i], p]))

const RING_R = [
  [11, 20],
  [10.923879532511286, 20.38268343236509],
  [10.707106781186548, 20.707106781186546],
  [10.38268343236509, 20.923879532511286],
  [10, 21],
  [9.61731656763491, 20.923879532511286],
  [9.292893218813452, 20.707106781186546],
  [9.076120467488714, 20.38268343236509],
  [9, 20],
  [9.076120467488714, 19.61731656763491],
  [9.292893218813452, 19.292893218813454],
  [9.61731656763491, 19.076120467488714],
  [10, 19],
  [10.38268343236509, 19.076120467488714],
  [10.707106781186548, 19.292893218813454],
  [10.923879532511286, 19.61731656763491],
  [11, 20],
]

function expectSegments(actual, expected) {
  expect(actual).toHaveLength(expected.length)
  actual.forEach((segment, i) => {
    segment.forEach((point, j) => {
      expect(point[0]).toBeCloseTo(expected[i][j][0], 12)
      expect(point[1]).toBeCloseTo(expected[i][j][1], 12)
    })
  })
}

const CASES = [
  ['B1B-K01 endpoint', 'endpoint', [[[9, 19], [11, 19], [11, 21], [9, 21], [9, 19]]]],
  ['B1B-K02 midpoint', 'midpoint', [[[9, 19], [11, 19], [10, 21], [9, 19]]]],
  ['B1B-K03 centre', 'centre', [RING_R]],
  ['B1B-K04 quadrant', 'quadrant', [[[10, 21], [11, 20], [10, 19], [9, 20], [10, 21]]]],
  ['B1B-K05 intersection', 'intersection', [[[9, 19], [11, 21]], [[9, 21], [11, 19]]]],
  ['B1B-K06 perpendicular', 'perpendicular', [[[9, 21], [9, 19], [11, 19]], [[9, 20], [10, 20], [10, 19]]]],
  ['B1B-K07 tangent', 'tangent', [RING_R, [[9, 21], [11, 21]]]],
  ['B1B-K08 nearest', 'nearest', [[[9, 19], [11, 21], [9, 21], [11, 19], [9, 19]]]],
  ['B1B-K09 insertion', 'insertion', [[[9, 20], [11, 20]], [[10, 19], [10, 21]]]],
]

describe('snapMarkerSegments', () => {
  for (const [name, kind, paths] of CASES) {
    it(name, () => {
      expectSegments(snapMarkerSegments({ ...AT, kind }, SIZE), segmentsOf(...paths))
    })
  }

  it('B1B-K10 invalid marker clears', () => {
    expect([
      snapMarkerSegments(null),
      snapMarkerSegments({ x: 10, y: 20, kind: 'bad' }, 2),
      snapMarkerSegments({ x: 10, y: 20, kind: 'endpoint' }, 0),
    ]).toEqual([[], [], []])
    for (const [pt, size] of [
      [undefined, 1],
      [{ x: Number.NaN, y: 20, kind: 'endpoint' }, 1],
      [{ x: 10, y: Infinity, kind: 'endpoint' }, 1],
      [{ x: '10', y: 20, kind: 'endpoint' }, 1],
      [{ x: 10, y: 20 }, 1],
      [{ x: 10, y: 20, kind: 'endpoint' }, -1],
      [{ x: 10, y: 20, kind: 'endpoint' }, Infinity],
      [{ x: 10, y: 20, kind: 'endpoint' }, Number.NaN],
    ]) expect(snapMarkerSegments(pt, size)).toEqual([])
    // The default size is one world unit.
    expect(snapMarkerSegments({ x: 0, y: 0, kind: 'insertion' })).toEqual([[[-0.5, 0], [0.5, 0]], [[0, -0.5], [0, 0.5]]])
  })

  it('B1B-K11 Viewer consumes segment helper', () => {
    const source = readFileSync(join(process.cwd(), 'src/components/Viewer.jsx'), 'utf8')
    expect(source).toContain("import { snapMarkerSegments } from '../cadedit/snapMarker.js'")
    const start = source.indexOf('setSnapMarker: (pt, size = 1) => {')
    expect(start).toBeGreaterThan(-1)
    const end = source.indexOf('\n    },', start)
    expect(end).toBeGreaterThan(start)
    const body = source.slice(start, end)
    // Replaced geometry and material are disposed before anything is drawn.
    expect(body).toContain('child.geometry?.dispose?.(); child.material?.dispose?.()')
    expect(body.indexOf('g.clear()')).toBeLessThan(body.indexOf('snapMarkerSegments(pt, size)'))
    expect(body).toContain('snapMarkerSegments(pt, size)')
    // Each segment becomes two XYZ positions at the marker depth.
    expect(body).toContain('pos.push(x1, y1, 2, x2, y2, 2)')
    expect(body).toContain('new THREE.LineSegments(geo, mat)')
    // The selection colour, drawn over the drawing, above the rubber band.
    expect(body).toContain('new THREE.LineBasicMaterial({ color: s.tokens.select, depthTest: false })')
    expect(body).toContain('mesh.renderOrder = 11')
    // No hand-drawn square survives beside the helper.
    expect(body).not.toContain('x - h, y - h')
  })
})
