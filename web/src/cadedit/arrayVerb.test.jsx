// W4g-5b: ARRAY on the client. The engine does the whole array in ONE
// operation, so the client's job is to read the operands strictly, refuse a
// bad one with a sentence before any round trip, and post the exact payload
// the worker dispatch expects. These rows cover the store's reading, the
// command words, the prompts and the one pick a polar array has.
import { describe, expect, it } from 'vitest'

import { PROMPTS } from './EngineRibbonClusters.jsx'
import { CREATING_EDITS, MAX_ARRAY_COPIES, buildEditPayload } from './engineSession.js'
import { PICK_SEQUENCES } from './pointPicking.js'
import { parseDrawingCommand } from '../lib/commandWords.js'
import { forGroup } from '../lib/actionRegistry.js'

describe('W4g-5b store: ARRAY operand reading', () => {
  it('a rectangular array lowers rows, columns and both spacings', () => {
    expect(buildEditPayload('arrayRect', 'e1', { rows: '2', cols: '3', rowGap: '10', colGap: '5' }))
      .toEqual({ payload: { entityId: 'e1', rows: 2, cols: 3, rowGap: 10, colGap: 5 } })
    // A negative or fractional spacing is a real drawing, so it passes.
    expect(buildEditPayload('arrayRect', 'e1', { rows: '1', cols: '4', rowGap: '0', colGap: '-2.5' }).payload)
      .toEqual({ entityId: 'e1', rows: 1, cols: 4, rowGap: 0, colGap: -2.5 })
  })

  it('the counts are whole numbers only, so a typo refuses instead of drawing its numeric prefix', () => {
    // W4f-9's rule, applied to the counts: parseInt would read "3abc" as 3.
    expect(buildEditPayload('arrayRect', 'e1', { rows: '3abc', cols: '3', rowGap: '1', colGap: '1' }).refusal)
      .toBe('Array refused: rows and columns must be whole numbers.')
    expect(buildEditPayload('arrayRect', 'e1', { rows: '2.5', cols: '3', rowGap: '1', colGap: '1' }).refusal)
      .toBe('Array refused: rows and columns must be whole numbers.')
    expect(buildEditPayload('arrayRect', 'e1', { rows: '-1', cols: '3', rowGap: '1', colGap: '1' }).refusal)
      .toBe('Array refused: rows and columns must be whole numbers.')
    expect(buildEditPayload('arrayPolar', 'e1', { count: '4x', cx: '0', cy: '0', totalDeg: '360' }).refusal)
      .toBe('Polar array refused: the count must be a whole number.')
  })

  it('refuses the arrays that would draw nothing, or the same thing many times', () => {
    expect(buildEditPayload('arrayRect', 'e1', { rows: '1', cols: '1', rowGap: '1', colGap: '1' }).refusal)
      .toBe('Array refused: 1 row by 1 column is the source alone, so there is nothing to copy.')
    expect(buildEditPayload('arrayRect', 'e1', { rows: '2', cols: '2', rowGap: '0', colGap: '0' }).refusal)
      .toBe('Array refused: with no spacing every copy lands on the source.')
    expect(buildEditPayload('arrayPolar', 'e1', { count: '1', cx: '0', cy: '0', totalDeg: '90' }).refusal)
      .toBe('Polar array refused: the count includes the source, so it must be at least 2.')
    expect(buildEditPayload('arrayPolar', 'e1', { count: '4', cx: '0', cy: '0', totalDeg: '0' }).refusal)
      .toBe('Polar array refused: an angle of 0 puts every copy on the source.')
  })

  it('carries the engine\'s own copy bound, so a count the prompt refuses never reaches the document', () => {
    expect(MAX_ARRAY_COPIES).toBe(1000)
    // 1000 copies is the last one that fits: the source holds a position.
    expect(buildEditPayload('arrayRect', 'e1', { rows: '1', cols: '1001', rowGap: '1', colGap: '1' }).payload.cols).toBe(1001)
    expect(buildEditPayload('arrayRect', 'e1', { rows: '1', cols: '1002', rowGap: '1', colGap: '1' }).refusal)
      .toBe('Array refused: that is more than 1000 copies.')
    expect(buildEditPayload('arrayPolar', 'e1', { count: '1001', cx: '0', cy: '0', totalDeg: '360' }).payload.count).toBe(1001)
    expect(buildEditPayload('arrayPolar', 'e1', { count: '1002', cx: '0', cy: '0', totalDeg: '360' }).refusal)
      .toBe('Polar array refused: that is more than 1000 copies.')
  })

  it('a polar array wants a finite centre and reads the sweep as a number', () => {
    expect(buildEditPayload('arrayPolar', 'e1', { count: '4', cx: '10', cy: '-5', totalDeg: '180' }))
      .toEqual({ payload: { entityId: 'e1', count: 4, cx: 10, cy: -5, totalDeg: 180 } })
    expect(buildEditPayload('arrayPolar', 'e1', { count: '4', cx: '', cy: '0', totalDeg: '90' }).refusal)
      .toBe('Polar array refused: the centre x and y must both be numbers.')
    expect(buildEditPayload('arrayPolar', 'e1', { count: '4', cx: '0', cy: '0', totalDeg: 'round' }).refusal)
      .toBe('Polar array refused: the angle to fill must be a number (degrees).')
  })

  it('both forms report what they made, so the selection lands on the copies', () => {
    expect(CREATING_EDITS).toContain('arrayRect')
    expect(CREATING_EDITS).toContain('arrayPolar')
  })
})

describe('W4g-5b surface: words, prompts, picks and the ribbon', () => {
  it('the reference\'s words reach the two forms', () => {
    for (const word of ['array', 'arrayrect', 'ar']) {
      expect(parseDrawingCommand(word)).toMatchObject({ group: 'modify', op: 'arrayRect' })
    }
    for (const word of ['arraypolar', 'pa']) {
      expect(parseDrawingCommand(word)).toMatchObject({ group: 'modify', op: 'arrayPolar' })
    }
  })

  it('the prompts ask in the reference\'s order and name every operand the store reads', () => {
    const rect = PROMPTS.arrayRect
    expect(rect.verb).toBe('ARRAYRECT')
    expect(rect.steps.flatMap((s) => s.fields.map((f) => f[0]))).toEqual(['rows', 'cols', 'rowGap', 'colGap'])
    const polar = PROMPTS.arrayPolar
    expect(polar.verb).toBe('ARRAYPOLAR')
    expect(polar.steps.flatMap((s) => s.fields.map((f) => f[0]))).toEqual(['cx', 'cy', 'count', 'totalDeg'])
    // The counts are numeric fields, so the prompt outlines a typo the same
    // way the store refuses it.
    expect(rect.steps[0].fields.every((f) => f[2] === 'numeric')).toBe(true)
  })

  it('a polar array picks its centre; a rectangular one has nothing on the canvas to pick', () => {
    expect(PICK_SEQUENCES.arrayPolar).toEqual([{ kind: 'point', keys: ['cx', 'cy'] }])
    expect(PICK_SEQUENCES.arrayRect).toBeUndefined()
  })

  it('both forms sit in the Modify group', () => {
    const ids = forGroup('modify').map((a) => a.id)
    expect(ids).toContain('modify:arrayRect')
    expect(ids).toContain('modify:arrayPolar')
  })
})
