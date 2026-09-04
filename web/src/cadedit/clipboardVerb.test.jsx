// W4g-5c: CUT / COPY / PASTE. The clipboard holds a frozen RECORD of the
// copied geometry, so it survives every edit and document switch that would
// have made a live entity stale, and a paste is one create at a base point
// through the path the Draw group already uses.
import { describe, expect, it } from 'vitest'

import { PROMPTS } from './EngineRibbonClusters.jsx'
import { MAX_CLIPBOARD_POINTS, anchorOf, clipboardRecord, describeRecord, pasteOp } from './clipboard.js'
import { PICK_SEQUENCES } from './pointPicking.js'
import { CLIPBOARD_REASONS, KNOWN_REASON_VALUES, clipboardReason, forGroup } from '../lib/actionRegistry.js'
import { parseDrawingCommand } from '../lib/commandWords.js'

const line = { type: 'LINE', layer: 'P', vertices: [[0, 0], [10, 5]] }
const poly = { type: 'LWPOLYLINE', layer: 'W', closed: true, vertices: [[0, 0], [4, 0], [4, 3]] }
const circle = { type: 'CIRCLE', layer: 'C', radius: 2, vertices: [[10, 10]] }
const arc = { type: 'ARC', layer: 'C', radius: 3, startDeg: 0, endDeg: 90, vertices: [[1, 2]] }

describe('W4g-5c the record: what a copy actually keeps', () => {
  it('keeps the geometry of each kind, frozen, with the layer', () => {
    const { record } = clipboardRecord(line)
    expect(record).toMatchObject({ type: 'LINE', layer: 'P', closed: false })
    expect(record.points).toEqual([[0, 0], [10, 5]])
    expect(Object.isFrozen(record)).toBe(true)
    expect(Object.isFrozen(record.points)).toBe(true)
    expect(clipboardRecord(poly).record).toMatchObject({ type: 'LWPOLYLINE', layer: 'W', closed: true })
    expect(clipboardRecord(circle).record).toMatchObject({ type: 'CIRCLE', cx: 10, cy: 10, radius: 2 })
    expect(clipboardRecord(arc).record).toMatchObject({ type: 'ARC', cx: 1, cy: 2, radius: 3, startDeg: 0, endDeg: 90 })
  })

  it('is a SNAPSHOT: mutating the source entity afterwards cannot change it', () => {
    const source = { type: 'LINE', layer: 'P', vertices: [[0, 0], [10, 5]] }
    const { record } = clipboardRecord(source)
    source.vertices[1][0] = 999
    source.layer = 'moved'
    expect(record.points).toEqual([[0, 0], [10, 5]])
    expect(record.layer).toBe('P')
  })

  it('refuses a kind it cannot carry, and says so on the COPY rather than at the paste', () => {
    expect(clipboardRecord({ type: 'TEXT', vertices: [[0, 0]] }).refusal)
      .toBe('Copy refused: a TEXT of this kind cannot go on the clipboard yet.')
    // The verb only names the sentence, so a cut refuses for the same reason.
    expect(clipboardRecord({ type: 'TEXT', vertices: [[0, 0]] }, 'Cut').refusal)
      .toBe('Cut refused: a TEXT of this kind cannot go on the clipboard yet.')
  })

  it('refuses geometry it cannot paste back', () => {
    expect(clipboardRecord({ type: 'LINE', vertices: [[0, 0]] }).refusal)
      .toBe('Copy refused: this entity has too little geometry to copy.')
    expect(clipboardRecord({ type: 'CIRCLE', radius: 0, vertices: [[0, 0]] }).refusal)
      .toBe('Copy refused: this entity has no usable radius.')
    expect(clipboardRecord({ type: 'ARC', radius: 1, startDeg: 0, vertices: [[0, 0]] }).refusal)
      .toBe('Copy refused: this arc has no usable angles.')
    expect(clipboardRecord({ type: 'LINE', vertices: [[0, 0], [Number.NaN, 1]] }).refusal)
      .toBe('Copy refused: this entity has too little geometry to copy.')
    const many = { type: 'LWPOLYLINE', vertices: Array.from({ length: MAX_CLIPBOARD_POINTS + 1 }, (_, i) => [i, 0]) }
    expect(clipboardRecord(many).refusal).toBe(`Copy refused: this entity has more than ${MAX_CLIPBOARD_POINTS} points.`)
  })
})

describe('W4g-5c the paste: one create at the base point', () => {
  it('puts the record ANCHOR on the base point and translates the rest with it', () => {
    // A polyline's anchor is its first vertex.
    const { record } = clipboardRecord(poly)
    expect(anchorOf(record)).toEqual([0, 0])
    const answer = pasteOp(record, 100, 50)
    expect(answer.op).toBe('createPolyline')
    expect(answer.inputs).toEqual({ pts: '100,50 104,50 104,53', closed: 'true', layer: 'W' })
  })

  it('a circle and an arc paste from their CENTRE, keeping radius and angles', () => {
    expect(pasteOp(clipboardRecord(circle).record, 0, 0).inputs).toEqual({ x: 0, y: 0, r: 2, layer: 'C' })
    const a = pasteOp(clipboardRecord(arc).record, 5, 5)
    expect(a.op).toBe('createArc')
    expect(a.inputs).toEqual({ x: 5, y: 5, r: 3, a0: 0, a1: 90, layer: 'C' })
  })

  it('a line pastes as a line, its second point carried by the same offset', () => {
    const answer = pasteOp(clipboardRecord(line).record, 3, 3)
    expect(answer.op).toBe('createLine')
    expect(answer.inputs).toEqual({ x: 3, y: 3, x2: 13, y2: 8, layer: 'P' })
  })

  it('refuses an empty clipboard and a base point that is not a point', () => {
    expect(pasteOp(null, 0, 0).refusal).toBe('Paste refused: the clipboard is empty.')
    expect(pasteOp(clipboardRecord(line).record, null, 0).refusal)
      .toBe('Paste refused: the base point x and y must both be numbers.')
    expect(pasteOp(clipboardRecord(line).record, 0, Number.NaN).refusal)
      .toBe('Paste refused: the base point x and y must both be numbers.')
  })

  it('pasting twice from one record draws the same geometry in two places', () => {
    const { record } = clipboardRecord(line)
    expect(pasteOp(record, 0, 0).inputs).toEqual({ x: 0, y: 0, x2: 10, y2: 5, layer: 'P' })
    expect(pasteOp(record, 20, 0).inputs).toEqual({ x: 20, y: 0, x2: 30, y2: 5, layer: 'P' })
  })
})

describe('W4g-5c the surface: ladder, panel, prompt, pick and words', () => {
  it('paste answers to the clipboard being empty, not to a selection', () => {
    const open = { engineParsed: true, selected: null, clipboard: null, busy: false, errorKind: null }
    expect(clipboardReason(open)).toBe(CLIPBOARD_REASONS.empty)
    // With something on the clipboard it is live, still with nothing selected.
    expect(clipboardReason({ ...open, clipboard: clipboardRecord(line).record })).toBe('')
    // And the document's own blockers come first.
    expect(clipboardReason({ ...open, engineParsed: false })).not.toBe(CLIPBOARD_REASONS.empty)
    expect(KNOWN_REASON_VALUES.has(CLIPBOARD_REASONS.empty)).toBe(true)
  })

  it('the panel is the reference\'s: Paste large, then cut and copy', () => {
    expect(forGroup('clipboard').map((a) => a.id)).toEqual([
      'clipboard:pasteClip', 'clipboard:cutClip', 'clipboard:copyClip',
    ])
    expect(forGroup('clipboard').map((a) => a.size)).toEqual(['large', 'small', 'small'])
  })

  it('only PASTE has a prompt and a pick; cut and copy run on click', () => {
    expect(PROMPTS.pasteClip.verb).toBe('PASTE')
    expect(PROMPTS.pasteClip.steps.flatMap((s) => s.fields.map((f) => f[0]))).toEqual(['x', 'y'])
    expect(PICK_SEQUENCES.pasteClip).toEqual([{ kind: 'point', keys: ['x', 'y'] }])
    expect(PROMPTS.copyClip).toBeUndefined()
    expect(PROMPTS.cutClip).toBeUndefined()
    expect(PICK_SEQUENCES.copyClip).toBeUndefined()
  })

  it('the reference\'s words reach all three', () => {
    expect(parseDrawingCommand('copyclip')).toMatchObject({ group: 'clipboard', op: 'copyClip' })
    expect(parseDrawingCommand('cutclip')).toMatchObject({ group: 'clipboard', op: 'cutClip' })
    expect(parseDrawingCommand('pasteclip')).toMatchObject({ group: 'clipboard', op: 'pasteClip' })
  })

  it('the status names what is on the clipboard', () => {
    expect(describeRecord(clipboardRecord(poly).record)).toBe('a lwpolyline of 3 points on layer W')
    expect(describeRecord(clipboardRecord(circle).record)).toBe('a circle on layer C')
    expect(describeRecord(null)).toBe('nothing')
  })
})
