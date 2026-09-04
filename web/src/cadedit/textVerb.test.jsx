// W4g-5d: single-line TEXT on the client. The engine adds a Text entity with
// the drafter's height and rotation as the DXF's own fields; the client reads
// the operands strictly with the same bounds the crate enforces, so a bad
// value refuses on the prompt and never round-trips. TEXT is a draw create
// (how it runs) seated in the reference's Annotation panel (where it sits),
// so every group gate admits it unchanged.
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import DraftingRibbon from '../site/DraftingRibbon.jsx'

import EngineRibbonClusters, { PROMPTS } from './EngineRibbonClusters.jsx'
import EngineSessionProvider from './EngineSessionProvider.jsx'
import { CREATE_OPS, MAX_TEXT_CHARS, buildCreatePayload } from './engineSession.js'
import { TEXT_ADVANCE, entityToPolyline } from './engineIntake.js'
import { PICK_SEQUENCES } from './pointPicking.js'
import { forGroup } from '../lib/actionRegistry.js'
import { parseDrawingCommand } from '../lib/commandWords.js'

describe('W4g-5d store: TEXT operand reading', () => {
  it('lowers point, height, rotation and value, trimming a trailing line break', () => {
    expect(buildCreatePayload('createText', { x: '10', y: '20', height: '2.5', rot: '30', text: 'Panel A', layer: 'Notes' }))
      .toEqual({ payload: { x: 10, y: 20, height: 2.5, rotationDeg: 30, text: 'Panel A', layer: 'Notes' } })
    // A pasted value often ends in a newline; that is not a control character
    // inside the text, it is the end of the line, and it is dropped.
    expect(buildCreatePayload('createText', { x: '0', y: '0', height: '1', rot: '0', text: 'abc\n' }).payload.text).toBe('abc')
    expect(CREATE_OPS).toContain('createText')
    expect(MAX_TEXT_CHARS).toBe(1024)
  })

  it('refuses each bad operand with the drafter\'s sentence', () => {
    const ok = { x: '0', y: '0', height: '1', rot: '0', text: 'x' }
    expect(buildCreatePayload('createText', { ...ok, x: 'a' }).refusal).toBe('Text refused: x and y must both be numbers.')
    expect(buildCreatePayload('createText', { ...ok, height: 'tall' }).refusal).toBe('Text refused: the height must be a number.')
    expect(buildCreatePayload('createText', { ...ok, height: '0' }).refusal).toBe('Text refused: the height must be greater than 0.')
    expect(buildCreatePayload('createText', { ...ok, height: '-1' }).refusal).toBe('Text refused: the height must be greater than 0.')
    expect(buildCreatePayload('createText', { ...ok, rot: 'ninety' }).refusal).toBe('Text refused: the rotation must be a number (degrees).')
    expect(buildCreatePayload('createText', { ...ok, text: '' }).refusal).toBe('Text refused: enter the text to place.')
    expect(buildCreatePayload('createText', { ...ok, text: '   ' }).refusal).toBe('Text refused: enter the text to place.')
    expect(buildCreatePayload('createText', { ...ok, text: '\n' }).refusal).toBe('Text refused: enter the text to place.')
    expect(buildCreatePayload('createText', { ...ok, text: 'a'.repeat(MAX_TEXT_CHARS + 1) }).refusal)
      .toBe(`Text refused: at most ${MAX_TEXT_CHARS} characters.`)
    expect(buildCreatePayload('createText', { ...ok, text: 'a'.repeat(MAX_TEXT_CHARS) }).payload.text).toHaveLength(MAX_TEXT_CHARS)
    // One line only: a DXF group value cannot carry a break or a tab.
    expect(buildCreatePayload('createText', { ...ok, text: 'line one\nline two' }).refusal)
      .toBe('Text refused: one line only, with no control characters.')
    expect(buildCreatePayload('createText', { ...ok, text: 'tab\there' }).refusal)
      .toBe('Text refused: one line only, with no control characters.')
    // A C1 control (NEL, U+0085) is refused here as the crate refuses it.
    expect(buildCreatePayload('createText', { ...ok, text: 'line\u0085next' }).refusal)
      .toBe('Text refused: one line only, with no control characters.')
  })
})

describe('W4g-5d surface: word, prompt, pick, seat', () => {
  it('the reference\'s word reaches it as a draw create', () => {
    expect(parseDrawingCommand('text')).toMatchObject({ group: 'draw', op: 'createText' })
    expect(parseDrawingCommand('t')).toMatchObject({ group: 'draw', op: 'createText' })
  })

  it('the prompt asks where, how tall, which way, then what; the pick is the start point', () => {
    expect(PROMPTS.createText.verb).toBe('TEXT')
    expect(PROMPTS.createText.steps.flatMap((s) => s.fields.map((f) => f[0]))).toEqual(['x', 'y', 'height', 'rot', 'text', 'layer'])
    // The value field is a text field, not a numeric one, so a word does not
    // read as a bad number.
    expect(PROMPTS.createText.steps.find((s) => s.fields[0][0] === 'text').fields[0][2]).toBe('text')
    expect(PICK_SEQUENCES.createText).toEqual([{ kind: 'point', keys: ['x', 'y'] }])
  })

  it('registers as a draw create whose panel is annotation', () => {
    const rec = forGroup('draw').find((a) => a.op === 'createText')
    expect(rec).toBeTruthy()
    expect(rec.id).toBe('draw:createText')
    expect(rec.group).toBe('draw')
    expect(rec.panel).toBe('annotation')
    // Every other draw record sits in its own panel.
    for (const a of forGroup('draw')) if (a.op !== 'createText') expect(a.panel).toBe('draw')
  })
})

describe('W4g-5d mapper: a TEXT draws as its outline box', () => {
  const text = { id: '1', type: 'TEXT', layer: 'N', vertices: [[10, 20, 0]], text: 'AB', height: 2, rotationDeg: 0 }

  it('is a closed 4-point box, TEXT_ADVANCE heights wide per character, from the insertion point', () => {
    const poly = entityToPolyline(text)
    expect(poly.closed).toBe(true)
    expect(poly.layer).toBe('N')
    const w = TEXT_ADVANCE * 2 * 2
    expect(poly.pts).toEqual([[10, 20, 0], [10 + w, 20, 0], [10 + w, 22, 0], [10, 22, 0]])
  })

  it('rotates about the insertion point', () => {
    const poly = entityToPolyline({ ...text, rotationDeg: 90 })
    const w = TEXT_ADVANCE * 2 * 2
    const near = (p, q) => p.every((v, i) => Math.abs(v - q[i]) < 1e-9)
    expect(near(poly.pts[0], [10, 20, 0])).toBe(true)
    expect(near(poly.pts[1], [10, 20 + w, 0])).toBe(true)
    expect(near(poly.pts[2], [10 - 2, 20 + w, 0])).toBe(true)
    expect(near(poly.pts[3], [10 - 2, 20, 0])).toBe(true)
  })

  it('refuses to invent a box for a text with no value or no usable height', () => {
    expect(entityToPolyline({ ...text, text: '' })).toBeNull()
    expect(entityToPolyline({ ...text, height: 0 })).toBeNull()
    expect(entityToPolyline({ ...text, vertices: [] })).toBeNull()
  })
})

class IdleWorker {
  constructor() { this.listeners = new Map() }
  addEventListener(type, fn) { this.listeners.set(type, fn) }
  removeEventListener(type) { this.listeners.delete(type) }
  postMessage() {}
  terminate() {}
}

afterEach(() => cleanup())

describe('W4g-5d the Annotation panel', () => {
  it('holds the real Text beside the two honest placeholders; the Draw panel does not carry it', () => {
    render(
      <EngineSessionProvider createWorker={vi.fn(() => new IdleWorker())}>
        <DraftingRibbon clusters={[]}>
          <EngineRibbonClusters importOpen={false} onToggleImport={() => {}} panels={['draw', 'modify', 'annotation']} />
        </DraftingRibbon>
      </EngineSessionProvider>,
    )
    const annotation = document.querySelector('.ribbon-cluster[data-group="annotation"]')
    expect(annotation).not.toBeNull()
    const ids = [...annotation.querySelectorAll('[data-tool]')].map((el) => el.dataset.tool)
    expect(ids).toEqual(['draw:createText', 'annotation:dimensions', 'annotation:leader'])
    const draw = document.querySelector('.ribbon-cluster[data-group="draw"]')
    expect([...draw.querySelectorAll('[data-tool]')].map((el) => el.dataset.tool)).not.toContain('draw:createText')
    // The panel order on the engine side is the reference's: Draw, Modify,
    // Annotation. (With no band slot in this DOM the File cluster renders
    // inline ahead of them, as it does in the old shell.)
    expect([...document.querySelectorAll('.ribbon-cluster[data-group]')].map((el) => el.dataset.group).slice(-3))
      .toEqual(['draw', 'modify', 'annotation'])
  })
})
