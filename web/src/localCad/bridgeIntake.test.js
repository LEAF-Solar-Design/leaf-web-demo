import { expect, it } from 'vitest'
import { bridgeToIntake } from './bridgeIntake.js'
import { expandBulgedPolylines, intakeRoundPolylines } from '../cadedit/engineIntake.js'

it('maps every bridge entity into the existing Viewer conventions without mutating the input', () => {
  const input = { drawing: 'site.dwg', geometry: true,
    lines: [{ handle: '1', layer: 'Wire', pts: [[0, 0, 0], [2, 0, 0]] }],
    polylines: [{ handle: '2', layer: 'Roof', pts: [[0, 0, 0], [2, 0, 0]], bulges: [1, 0], closed: true }],
    circles: [{ handle: '3', layer: 'Roof', c: [3, 3, 0], r: 2 }],
    arcs: [{ handle: '4', layer: 'Roof', c: [4, 4, 0], r: 1, start_deg: 350, end_deg: 10 }],
    inserts: [{ handle: '5', layer: 'Equipment', name: 'Panel', pt: [1, 2, 0], rot_deg: 90, scale: [1, 2, 1] }],
    texts: [{ handle: '6', layer: 'Notes', pt: [2, 2, 0], text: 'PV', height: 1 }],
    layers: [{ name: 'Roof', count: 3 }, { name: 'Empty', count: 0 }] }
  const before = JSON.stringify(input)
  const mapped = bridgeToIntake(input)
  expect(mapped).toMatchObject({ source: 'bridge', documentId: 'autocad:site.dwg', truncated: false })
  expect(mapped.polylines[0]).toEqual({ ...input.lines[0], closed: false })
  expect(mapped.polylines[1]).toEqual(input.polylines[0])
  expect(mapped.circles).toEqual(input.circles)
  expect(mapped.arcs).toEqual(input.arcs)
  expect(mapped.inserts[0]).toEqual({ handle: '5', layer: 'Equipment', name: 'Panel', pt: [1, 2, 0], rot: 90, scale: [1, 2, 1] })
  expect(mapped.layers).toEqual(expect.arrayContaining(['Wire', 'Roof', 'Equipment', 'Notes', 'Empty']))
  expect(mapped.polylines.some(row => row.handle === '6' && row.strokeOnly)).toBe(true)
  const expanded = expandBulgedPolylines(mapped.polylines)
  expect(expanded[1].pts.length).toBeGreaterThan(2)
  expect(expanded[1].pts.some(p => Math.abs(p[1]) > 0.5)).toBe(true)
  expect(intakeRoundPolylines(mapped)).toHaveLength(2)
  expect(JSON.stringify(input)).toBe(before)
})

it('limits total points, including the sampling the Viewer will perform', () => {
  const pts = Array.from({ length: 200_000 }, (_, i) => [i, 0, 0])
  const mapped = bridgeToIntake({ polylines: [{ pts }, { pts: [[0, 0, 0], [1, 0, 0]] }],
    circles: [{ c: [0, 0, 0], r: 1 }] })
  expect(mapped.points).toBe(200_000)
  expect(mapped.truncated).toBe(true)
  expect(mapped.polylines).toHaveLength(1)
  expect(mapped.circles).toHaveLength(0)
  const bulged = bridgeToIntake({ polylines: [{ pts: pts.slice(0, 5000), bulges: Array(5000).fill(1), closed: true }] })
  expect(bulged.truncated).toBe(true)
  expect(bulged.polylines).toHaveLength(0)
})

it('skips malformed geometry and returns an empty, usable intake', () => {
  expect(bridgeToIntake().layers).toEqual([])
  const mapped = bridgeToIntake({ lines: [{ pts: [[NaN, 0], [1, 1]] }],
    circles: [{ c: [0, 0], r: -1 }], arcs: [{ c: [0, 0], r: 1 }], inserts: [{ pt: [] }] })
  expect(mapped.points).toBe(0)
})

it('skips null and malformed rows and continues to later valid geometry and text', () => {
  const mapped = bridgeToIntake({ lines: [null, { pts: 'ab' }, { pts: {} },
    { pts: [[0, 0], [1, 1]], handle: 'valid' }],
  texts: [null, {}, { text: 'bad', pt: [0, 0] }, { text: 'PV', pt: [0, 0], height: 1, handle: 'text' }],
  polylines: [null], circles: [null], arcs: [null], inserts: [null] })
  expect(mapped.polylines.some(row => row.handle === 'valid')).toBe(true)
  expect(mapped.polylines.some(row => row.handle === 'text')).toBe(true)
  expect(mapped.truncated).toBe(false)
})

it('caps long text at 256 characters before expansion', () => {
  const text = { text: 'W'.repeat(100000), pt: [0, 0], height: 1 }
  const mapped = bridgeToIntake({ texts: [text] })
  const capped = bridgeToIntake({ texts: [{ ...text, text: 'W'.repeat(256) }] })
  expect(mapped.polylines).toEqual(capped.polylines)
  expect(mapped.points).toBe(capped.points)
  expect(mapped.truncated).toBe(true)
})

it('reserves the remaining text budget without allocating a whole over-budget label', () => {
  const mapped = bridgeToIntake({ polylines: [{ pts: Array.from({ length: 199980 }, (_, i) => [i, 0]) }],
    texts: [{ text: 'B'.repeat(256), pt: [0, 0], height: 1 }] })
  expect(mapped.points).toBeLessThanOrEqual(200000)
  expect(mapped.polylines.reduce((n, row) => n + row.pts.length, 0)).toBe(mapped.points)
  expect(mapped.truncated).toBe(true)
})
