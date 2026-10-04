import { expect, it } from 'vitest'
import { blockDefinitions, MAX_GLYPH_CHARS, textGlyphPolylines } from './viewerIntake.js'

const glyphInput = { text: 'A', pt: [10, 20, 0], height: 2, handle: '1A', layer: 'Notes' }
const glyphPoints = (polylines) => polylines.flatMap((pl) => pl.pts)

it('W21D2-A-contours', () => {
  const input = { ...glyphInput, pt: Object.freeze([10, 20, 0]) }
  const contours = textGlyphPolylines(Object.freeze(input))
  expect(contours.map((pl) => pl.pts.length)).toEqual([9, 4])
  for (const pl of contours) {
    expect(pl).toMatchObject({ handle: '1A', layer: 'Notes', closed: false, strokeOnly: true })
    expect(pl.pts[pl.pts.length - 1]).toEqual(pl.pts[0])
  }
  expect(contours[0].pts[0][0]).toBeCloseTo(11.788746298124384, 9)
  expect(contours[0].pts[0].slice(1)).toEqual([20, 0])
  const ys = glyphPoints(contours).map((p) => p[1])
  expect(Math.min(...ys)).toBe(20)
  expect(Math.max(...ys)).toBeCloseTo(22, 9)
  expect(input).toEqual(glyphInput)
})

it('W21D2-rotation', () => {
  const flat = textGlyphPolylines(glyphInput)
  const rotated = textGlyphPolylines({ ...glyphInput, rotationDeg: 90 })
  expect(rotated[0].pts[0][0]).toBeCloseTo(10, 9)
  expect(rotated[0].pts[0][1]).toBeCloseTo(21.788746298124384, 9)
  for (let i = 0; i < flat.length; i++) {
    for (let j = 0; j < flat[i].pts.length; j++) {
      const [x, y] = flat[i].pts[j]
      expect(rotated[i].pts[j][0]).toBeCloseTo(10 - (y - 20), 9)
      expect(rotated[i].pts[j][1]).toBeCloseTo(20 + (x - 10), 9)
    }
  }
  const centered = glyphPoints(textGlyphPolylines({ ...glyphInput, centered: true, rotationDeg: 90 }))
  const ys = centered.map((p) => p[1])
  expect((Math.min(...ys) + Math.max(...ys)) / 2).toBeCloseTo(20, 9)
})

it('W21D2-AB-contours', () => {
  const contours = textGlyphPolylines({ ...glyphInput, text: 'AB' })
  expect(contours.map((pl) => pl.pts.length)).toEqual([9, 4, 36, 20, 20])
  expect(glyphPoints(contours)).toHaveLength(89)
})

it('W21D2-empty-and-invalid', () => {
  expect(textGlyphPolylines()).toEqual([])
  for (const patch of [{ text: '' }, { text: ' ' }, { text: 3 }, { pt: null }, { pt: [10] },
    { pt: [NaN, 20] }, { pt: [10, Infinity] }, { height: 0 }, { height: -1 },
    { height: Infinity }, { rotationDeg: NaN }, { rotationDeg: Infinity }]) {
    expect(textGlyphPolylines({ ...glyphInput, ...patch })).toEqual([])
  }
  for (const pt of [[10, 20], [10, 20, NaN]]) {
    expect(glyphPoints(textGlyphPolylines({ ...glyphInput, pt })).every((p) => p[2] === 0)).toBe(true)
  }
  const largeGlyphs = textGlyphPolylines({ ...glyphInput, height: Number.MAX_VALUE })
  expect(largeGlyphs).toHaveLength(2)
  expect(largeGlyphs.every(polyline => polyline.pts.every(point => point.every(Number.isFinite)))).toBe(true)
  expect(textGlyphPolylines({ ...glyphInput, pt: [Number.MAX_VALUE, 20, 0], height: Number.MAX_VALUE })).toEqual([])
})

it('W21D2-unsupported-fallback', () => {
  const fallback = textGlyphPolylines({ ...glyphInput, text: '?' })
  expect(fallback.map((pl) => pl.pts.length)).toEqual([59, 5])
  expect(textGlyphPolylines({ ...glyphInput, text: '\u{1F680}' })).toEqual(fallback)
  expect(textGlyphPolylines({ ...glyphInput, text: 'A A' })).toHaveLength(4)
})

it('W21D2-character-limit', () => {
  expect(MAX_GLYPH_CHARS).toBe(1024)
  const contours = textGlyphPolylines({ ...glyphInput, text: 'A'.repeat(1024) })
  expect(contours).toHaveLength(2048)
  expect(glyphPoints(contours)).toHaveLength(13312)
  expect(textGlyphPolylines({ ...glyphInput, text: 'A'.repeat(1025) })).toEqual([])
  expect(textGlyphPolylines({ ...glyphInput, text: '\u{1F680}'.repeat(1024) })).toHaveLength(2048)
  expect(textGlyphPolylines({ ...glyphInput, text: '\u{1F680}'.repeat(1025) })).toEqual([])
})

it('blockDefinitions reads the server object form without changing records', () => {
  const block = { base: [0, 0, 0], count: 2, complete: true, children: [] }
  const definitions = blockDefinitions({ blocks: { BLK: block } })
  expect(definitions.get('BLK')).toBe(block)
  expect(definitions.size).toBe(1)
})

it('blockDefinitions uppercases server keys', () => {
  const block = { base: [0, 0, 0] }
  expect(blockDefinitions({ blocks: { blk: block } }).get('BLK')).toBe(block)
})

it('blockDefinitions reads the engine array form', () => {
  const block = { name: 'blk', base: [0, 0, 0] }
  const definitions = blockDefinitions({ blocks: [block] })
  expect(definitions.get('BLK')).toBe(block)
  expect(definitions.size).toBe(1)
})

it('blockDefinitions accepts absent and empty blocks', () => {
  for (const intake of [undefined, null, {}, { blocks: null }, { blocks: {} }, { blocks: [] }]) {
    expect(blockDefinitions(intake).size).toBe(0)
  }
})

it('blockDefinitions ignores primitive blocks without throwing', () => {
  for (const blocks of ['blk', 3, true]) {
    expect(() => blockDefinitions({ blocks })).not.toThrow()
    expect(blockDefinitions({ blocks }).size).toBe(0)
  }
})

it('blockDefinitions skips object values that are not plain objects', () => {
  const block = {}
  const definitions = blockDefinitions({ blocks: { A: 'x', B: null, C: block, D: [] } })
  expect([...definitions]).toEqual([['C', block]])
})

it('blockDefinitions skips array items without usable names', () => {
  const block = { name: 'ok' }
  const definitions = blockDefinitions({ blocks: [{}, { name: '' }, { name: 3 }, block, null, [], 'x'] })
  expect([...definitions]).toEqual([['OK', block]])
})

it('blockDefinitions reads at most 500 object entries', () => {
  const blocks = Object.fromEntries(Array.from({ length: 600 }, (_, i) => [`block${i}`, {}]))
  const definitions = blockDefinitions({ blocks })
  expect(definitions.size).toBe(500)
  expect(definitions.has('BLOCK499')).toBe(true)
  expect(definitions.has('BLOCK500')).toBe(false)
})

it('blockDefinitions resolves the APS inspection regression shape', () => {
  // Regression for w6-viewer-block-definitions: da/intake_parse.py emits this
  // name-keyed shape, which made (intake.blocks || []).map(...) throw
  // TypeError: ... .map is not a function and crash the whole workspace.
  const block = { base: [0, 0, 0], count: 1, complete: false, children: [] }
  const intake = { blocks: { BLK: block } }
  expect(() => blockDefinitions(intake)).not.toThrow()
  expect(blockDefinitions(intake).get('BLK')).toBe(block)
})

it('blockDefinitions bounds array reads even when entries are invalid', () => {
  const blocks = Array.from({ length: 500 }, () => ({}))
  blocks.push({ name: 'beyond' })
  expect(blockDefinitions({ blocks }).size).toBe(0)
})

it('blockDefinitions ignores inherited object entries', () => {
  const blocks = Object.create({ inherited: {} })
  blocks.own = {}
  expect([...blockDefinitions({ blocks }).keys()]).toEqual(['OWN'])
})

it('blockDefinitions lets later case-insensitive duplicates overwrite earlier ones', () => {
  const first = { name: 'blk' }
  const last = { name: 'BLK' }
  for (const blocks of [[first, last], { blk: first, BLK: last }]) {
    const definitions = blockDefinitions({ blocks })
    expect(definitions.size).toBe(1)
    expect(definitions.get('BLK')).toBe(last)
  }
})
