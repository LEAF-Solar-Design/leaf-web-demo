import { expect, it } from 'vitest'
import { bridgeToIntake } from './bridgeIntake.js'
import { textGlyphs } from './bridgeText.js'
import { textGlyphPolylines } from '../components/viewerIntake.js'
import { expandBulgedPolylines, intakeRoundPolylines } from '../cadedit/engineIntake.js'

const viewerPointCount = intake => [...expandBulgedPolylines(intake.polylines), ...intakeRoundPolylines(intake)]
  .reduce((n, row) => n + row.pts.length, intake.inserts.length)
const straightPoints = count => Array.from({ length: count }, (_, i) => [i, 0, 0])
const bulgeFixtures = [
  { name: 'negative semicircle', pts: [[0, 0], [2, 0]], bulges: [-1, 0] },
  { name: 'short bulge', pts: [[0, 0], [2, 0]], bulges: [0.01, 0] },
  { name: 'tiny bulges', pts: [[0, 0], [2, 0], [4, 0]], bulges: [1e-10, -1e-12, 0] },
  { name: 'terminal bulge', pts: [[0, 0], [2, 0]], bulges: [0, 1] },
  { name: 'coincident chord', pts: [[0, 0], [0, 0], [2, 0]], bulges: [1, 0, 0] },
  { name: 'degenerate short chord', pts: [[0, 0], [1e-13, 0]], bulges: [1, 0] },
  { name: 'terminal bulge with duplicates', pts: [[0, 0], [0, 0]], bulges: [0, 1] },
  { name: 'closed bulges', pts: [[0, 0], [2, 0], [2, 2]], bulges: [1, -0.5, 0.01], closed: true },
  { name: 'rounded samples', pts: [[1e16, 1e16], [1e16 + 2, 1e16]], bulges: [0.01, 0] },
  { name: 'invalid bulges', pts: [[0, 0], [2, 0], [4, 0]], bulges: [NaN, Infinity, '1'] },
]
const arcFixtures = [
  { name: 'wrapped arc', start_deg: 350, end_deg: 10 },
  { name: 'full arc', start_deg: 10, end_deg: 10 },
  { name: 'full positive turn', start_deg: 0, end_deg: 360 },
  { name: 'full negative turn', start_deg: 360, end_deg: 0 },
  { name: 'short arc', start_deg: 0, end_deg: 1 },
  { name: 'step boundary', start_deg: 0, end_deg: 60 },
  { name: 'past step boundary', start_deg: 0, end_deg: 60.00000001 },
  { name: 'multiple turns', start_deg: -720, end_deg: 725 },
  { name: 'huge angles', start_deg: -Number.MAX_VALUE, end_deg: Number.MAX_VALUE },
].map(row => ({ c: [0, 0], r: 1, ...row }))

it('matches viewerIntake glyph contours point for point without its shape machinery', () => {
  for (const text of ['AB', 'PV-01', 'Az 180°', '?']) {
    const row = { text, pt: [3, 7, 2], height: 2.5, handle: 'label', layer: 'Notes' }
    const actual = bridgeToIntake({ texts: [row] }, { textGlyphs }).polylines
    const expected = textGlyphPolylines(row)
    expect(actual).toHaveLength(expected.length)
    for (let i = 0; i < expected.length; i += 1) {
      expect(actual[i]).toMatchObject({ handle: row.handle, layer: row.layer, closed: false, strokeOnly: true })
      expect(actual[i].pts).toHaveLength(expected[i].pts.length)
      for (let j = 0; j < expected[i].pts.length; j += 1) {
        expect(Math.abs(actual[i].pts[j][0] - expected[i].pts[j][0])).toBeLessThanOrEqual(1e-6)
        expect(Math.abs(actual[i].pts[j][1] - expected[i].pts[j][1])).toBeLessThanOrEqual(1e-6)
        expect(actual[i].pts[j][2]).toBe(expected[i].pts[j][2])
      }
    }
  }
})

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
  const mapped = bridgeToIntake(input, { textGlyphs })
  expect(mapped).toMatchObject({ source: 'bridge', documentId: 'autocad:site.dwg', truncated: false })
  expect(mapped.polylines[0]).toEqual({ ...input.lines[0], closed: false, strokeOnly: true })
  expect(mapped.polylines[1]).toEqual({ ...input.polylines[0], strokeOnly: true })
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

it('marks bridge lines and both open and closed polylines as strokes', () => {
  const pts = [[0, 0, 0], [2, 0, 0], [2, 2, 0]]
  const mapped = bridgeToIntake({
    lines: [{ pts: pts.slice(0, 2) }],
    polylines: [{ pts, closed: false }, { pts }, { pts, closed: true }],
  })
  expect(mapped.source).toBe('bridge')
  expect(mapped.polylines[0]).toMatchObject({ closed: false, strokeOnly: true })
  expect(mapped.polylines[1]).toMatchObject({ closed: false, strokeOnly: true })
  expect(mapped.polylines[2]).toMatchObject({ closed: false, strokeOnly: true })
  expect(mapped.polylines[3]).toMatchObject({ closed: true, strokeOnly: true })
})

it('lays out newline and MTEXT paragraph glyphs on separate baselines without substitute glyphs', () => {
  const row = { pt: [3, 7, 2], height: 2.5, handle: 'label', layer: 'Notes' }
  const convert = text => bridgeToIntake({ texts: [{ ...row, text }] }, { textGlyphs }).polylines
  const first = convert('A')
  const second = convert('B')
  const spacing = row.height * 5 / 3
  for (const text of ['A\nB', 'A\\PB', 'A\r\nB']) {
    const actual = convert(text)
    expect(actual).toHaveLength(first.length + second.length)
    expect(actual.slice(0, first.length)).toEqual(first)
    for (let i = 0; i < second.length; i += 1) {
      const contour = actual[first.length + i]
      expect(contour.pts).toHaveLength(second[i].pts.length)
      for (let j = 0; j < contour.pts.length; j += 1) {
        expect(contour.pts[j][0]).toBe(second[i].pts[j][0])
        expect(second[i].pts[j][1] - contour.pts[j][1]).toBeCloseTo(spacing, 12)
        expect(contour.pts[j][2]).toBe(second[i].pts[j][2])
      }
    }
  }
})

it('removes MTEXT formatting while preserving spaces and escaped literals', () => {
  const row = { pt: [3, 7, 2], height: 2.5 }
  const convert = text => bridgeToIntake({ texts: [{ ...row, text }] }, { textGlyphs }).polylines
  expect(convert('{\\fArial|b0;Hi}')).toEqual(convert('Hi'))
  expect(convert('\\LHi\\l\\O!\\o\\K!\\k\\~AB')).toEqual(convert('Hi!! AB'))
  for (const [encoded, text] of [['\\\\P', '\\P'], ['\\{Hi\\}', '{Hi}']]) {
    const actual = convert(encoded)
    const expected = textGlyphPolylines({ ...row, text })
    expect(actual).toHaveLength(expected.length)
    for (let i = 0; i < expected.length; i += 1) {
      expect(actual[i].pts).toHaveLength(expected[i].pts.length)
      for (let j = 0; j < expected[i].pts.length; j += 1) {
        for (let axis = 0; axis < 3; axis += 1) {
          expect(actual[i].pts[j][axis]).toBeCloseTo(expected[i].pts[j][axis], 12)
        }
      }
    }
  }
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
  expect(bulged.truncated).toBe(false)
  expect(bulged.polylines).toHaveLength(1)
  expect(bulged.polylines[0].pts).toHaveLength(5000)
  expect(bulged.polylines[0].bulges).toEqual(Array(5000).fill(1))
  expect(bulged.points).toBeLessThan(200_000)
  expect(bulged.points).toBe(viewerPointCount(bulged))
})

it.each(bulgeFixtures)('matches Viewer sampling for $name', fixture => {
  const mapped = bridgeToIntake({ polylines: [{ ...fixture, handle: 'bulge', layer: 'Curves' }] })
  expect(mapped.truncated).toBe(false)
  expect(mapped.points).toBe(viewerPointCount(mapped))
  expect(mapped.polylines[0]).toMatchObject({ handle: 'bulge', layer: 'Curves', pts: fixture.pts.map(p => [...p, 0]) })
  expect(mapped.layers).toContain('Curves')
})

it.each(arcFixtures)('matches Viewer sampling for $name', fixture => {
  const mapped = bridgeToIntake({ arcs: [{ ...fixture, handle: 'arc', layer: 'Curves' }] })
  expect(mapped.truncated).toBe(false)
  expect(mapped.points).toBe(viewerPointCount(mapped))
  expect(mapped.arcs[0]).toMatchObject({ handle: 'arc', layer: 'Curves' })
})

it.each([
  ...bulgeFixtures.map(row => ({ name: row.name, kind: 'polylines', row })),
  ...arcFixtures.map(row => ({ name: row.name, kind: 'arcs', row })),
  { name: 'circle', kind: 'circles', row: { c: [0, 0], r: 1 } },
])('admits exactly the sampled cap and rejects cap plus one for $name', ({ kind, row }) => {
  const candidate = { ...row, handle: 'candidate', layer: 'Candidate' }
  const cost = viewerPointCount(bridgeToIntake({ [kind]: [candidate] }))
  for (const excess of [0, 1]) {
    const filler = { pts: straightPoints(200_000 - cost + excess), handle: 'filler' }
    const payload = kind === 'polylines' ? { polylines: [filler, candidate] }
      : { polylines: [filler], [kind]: [candidate] }
    const mapped = bridgeToIntake(payload)
    expect(mapped.truncated).toBe(excess === 1)
    expect(mapped.points).toBe(excess ? filler.pts.length : 200_000)
    expect(mapped.points).toBe(viewerPointCount(mapped))
    expect(mapped[kind].some(item => item.handle === 'candidate')).toBe(excess === 0)
    expect(mapped.layers.includes('Candidate')).toBe(excess === 0)
  }
})

it('budgets mixed entities cumulatively and continues after a rejected entity', () => {
  const geometry = {
    lines: [{ pts: [[0, 0], [1, 0]], handle: 'line' }],
    polylines: bulgeFixtures.map(row => ({ ...row })),
    circles: [{ c: [0, 0], r: 2 }], arcs: arcFixtures.map(row => ({ ...row })),
    inserts: [{ pt: [0, 0], name: 'Panel', handle: 'insert' }],
  }
  const sampled = viewerPointCount(bridgeToIntake(geometry))
  geometry.polylines.unshift({ pts: straightPoints(200_000 - sampled) })
  const exact = bridgeToIntake(geometry)
  expect(exact.truncated).toBe(false)
  expect(exact.points).toBe(200_000)
  expect(exact.points).toBe(viewerPointCount(exact))
  geometry.arcs.push({ c: [0, 0], r: 1, start_deg: 0, end_deg: 1, handle: 'over', layer: 'Rejected' })
  const over = bridgeToIntake(geometry)
  expect(over.truncated).toBe(true)
  expect(over.points).toBe(200_000)
  expect(over.points).toBe(viewerPointCount(over))
  expect(over.arcs.some(row => row.handle === 'over')).toBe(false)
  expect(over.layers).not.toContain('Rejected')
  expect(over.inserts[0].handle).toBe('insert')
})

it('counts duplicate vertices as the Viewer does when the raw row exceeds the cap', () => {
  const pts = Array.from({ length: 200_001 }, () => [0, 0])
  const mapped = bridgeToIntake({ polylines: [{ pts, bulges: [1] }] })
  expect(mapped.truncated).toBe(false)
  expect(mapped.points).toBe(1)
  expect(mapped.points).toBe(viewerPointCount(mapped))
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
  polylines: [null], circles: [null], arcs: [null], inserts: [null] }, { textGlyphs })
  expect(mapped.polylines.some(row => row.handle === 'valid')).toBe(true)
  expect(mapped.polylines.some(row => row.handle === 'text')).toBe(true)
  expect(mapped.truncated).toBe(false)
})

it('caps long text at 256 characters before expansion', () => {
  const text = { text: 'W'.repeat(100000), pt: [0, 0], height: 1 }
  const mapped = bridgeToIntake({ texts: [text] }, { textGlyphs })
  const capped = bridgeToIntake({ texts: [{ ...text, text: 'W'.repeat(256) }] }, { textGlyphs })
  expect(mapped.polylines).toEqual(capped.polylines)
  expect(mapped.points).toBe(capped.points)
  expect(mapped.truncated).toBe(true)
})

it('applies the text cap after removing MTEXT formatting', () => {
  const row = { pt: [0, 0], height: 1 }
  const convert = text => bridgeToIntake({ texts: [{ ...row, text }] }, { textGlyphs })
  expect(convert('{\\fArial|b0;Hi}'.repeat(128))).toEqual(convert('Hi'.repeat(128)))
})

it('reserves the remaining text budget without allocating a whole over-budget label', () => {
  const mapped = bridgeToIntake({ polylines: [{ pts: Array.from({ length: 199980 }, (_, i) => [i, 0]) }],
    texts: [{ text: 'B'.repeat(256), pt: [0, 0], height: 1 }] }, { textGlyphs })
  expect(mapped.points).toBeLessThanOrEqual(200000)
  expect(mapped.polylines.reduce((n, row) => n + row.pts.length, 0)).toBe(mapped.points)
  expect(mapped.truncated).toBe(true)
})

it('leaves text pending without a helper and preserves the geometry conversion', () => {
  const geometry = { lines: [{ handle: 'line', pts: [[0, 0], [1, 1]] }],
    circles: [{ c: [2, 2], r: 1 }] }
  const mapped = bridgeToIntake({ ...geometry, texts: [
    { text: 'TEXT', pt: [0, 0], height: 1 }, { text: 'MTEXT', pt: [1, 1], height: 2 },
  ] })
  expect(mapped).toEqual({ ...bridgeToIntake(geometry), textPending: 2 })
  expect(mapped.polylines).toHaveLength(1)
  expect(mapped.points).toBe(50)
})
