// S26: the ribbon's dimension fields take typed units and @ arithmetic,
// keep the raw text while focused, commit the canonical number on Enter or
// blur, step on the arrow keys and scrub on a horizontal label drag. Point
// expressions in a point step's first field keep the W4f-8 path unchanged,
// and a refused dimension keeps its outline and its reason.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import DraftingRibbon from '../site/DraftingRibbon.jsx'
import CadEditSurface from './CadEditSurface.jsx'
import EngineRibbonClusters, { SCRUB_PX, drawingUnitOf, evaluateDimension } from './EngineRibbonClusters.jsx'
import EngineSessionProvider from './EngineSessionProvider.jsx'

class ScriptedWorker {
  constructor() { this.posted = []; this.listeners = new Map(); this.terminated = false }
  addEventListener(type, fn) { this.listeners.set(type, fn) }
  removeEventListener(type) { this.listeners.delete(type) }
  postMessage(message) { this.posted.push(message) }
  terminate() { this.terminated = true }
  emit(data) { act(() => { this.listeners.get('message')?.({ data }) }) }
}

const LINE = { id: 'e1', type: 'LINE', layer: 'Panels', vertices: [[0, 0], [1, 1]] }

function fileOf(name = 'one.dxf') {
  const bytes = new TextEncoder().encode('0\nEOF\n')
  const file = new File([bytes], name, { type: 'application/dxf' })
  file.arrayBuffer = async () => bytes.buffer.slice(0)
  Object.defineProperty(file, 'size', { value: bytes.length })
  return file
}

let workers
function mount() {
  workers = []
  const createWorker = vi.fn(() => { const w = new ScriptedWorker(); workers.push(w); return w })
  render(
    <EngineSessionProvider createWorker={createWorker}>
      <DraftingRibbon clusters={[]}>
        <EngineRibbonClusters importOpen={false} onToggleImport={() => {}} />
      </DraftingRibbon>
      <CadEditSurface enabled />
    </EngineSessionProvider>,
  )
}

// The drawing unit rides the entity snapshot the way the catalogues do.
async function openDrawing(unit = null) {
  mount()
  const entities = unit ? Object.assign([LINE], { drawingUnit: unit }) : [LINE]
  await act(async () => {
    fireEvent.change(screen.getByLabelText('DXF file'), { target: { files: [fileOf()] } })
    await Promise.resolve()
    await Promise.resolve()
  })
  await waitFor(() => expect(workers.length).toBeGreaterThan(0))
  workers.at(-1).emit({ type: 'documentLoaded', documentId: 'one.dxf', entities, entityCount: entities.length, unsupported: [] })
}

const arm = (op) => fireEvent.click(document.querySelector(`.drafting-ribbon [data-tool="draw:${op}"]`))
const field = (name) => screen.getByLabelText(`ribbon ${name}`)
const note = () => screen.queryByTestId('cockpit-prompt-note')
const run = () => screen.getByTestId('cockpit-prompt-run')
const edits = () => workers.at(-1).posted.filter((message) => message.type === 'applyEdit')
const focus = (el) => act(() => el.focus())
// React's onBlur listens to focusout; fire it directly so the commit is
// exactly the one the browser would make on leaving the field.
const leave = (el) => fireEvent.focusOut(el)
const type = (el, value) => fireEvent.change(el, { target: { value } })
const enter = (el) => fireEvent.keyDown(el, { key: 'Enter' })

beforeEach(() => {
  globalThis.URL.createObjectURL = vi.fn(() => 'blob:cad-edit-test')
  globalThis.URL.revokeObjectURL = vi.fn()
})
afterEach(() => cleanup())

describe('S26 dimension grammar (pure)', () => {
  it('judges units and @ arithmetic, and leaves numbers, words and point expressions to their own paths', () => {
    expect(evaluateDimension('12 ft', { unit: 'in' })).toEqual({ value: '144' })
    expect(evaluateDimension('@+2', { current: '10', unit: 'in' })).toEqual({ value: '12' })
    expect(evaluateDimension('@*4', { current: '10' })).toEqual({ value: '40' })
    for (const raw of ['-5', '5', '1e3', '', '  ', 'abc', '3,4', '@1,2', '@10<90', '10<180', '@']) {
      expect(evaluateDimension(raw, { current: '10', unit: 'in' }), raw).toBeNull()
    }
    expect(evaluateDimension('@+2', { unit: 'in' }).reason).toMatch(/finite current value/)
    expect(evaluateDimension('12 ft').reason).toMatch(/drawing unit is unknown/)
    expect(evaluateDimension('90 ft', { unit: 'in', unitless: true }).reason).toMatch(/length unit does not apply/)
    expect(evaluateDimension('@*2', { current: '45', unitless: true })).toEqual({ value: '90' })
    expect(drawingUnitOf({ entities: Object.assign([], { drawingUnit: 'ft' }) })).toBe('ft')
    expect(drawingUnitOf({ entities: Object.assign([], { drawingUnit: 'yd' }) })).toBeNull()
    expect(drawingUnitOf({ entities: [] })).toBeNull()
  })
})

describe('S26 ribbon dimension fields', () => {
  it('"12 ft" stays raw while focused, shows the drawing unit, and commits 144 inches on Enter', async () => {
    await openDrawing('in')
    arm('createCircle')
    const r = field('r')
    expect(r.parentElement.querySelector('[data-unit-for="r"]').textContent).toBe('in')
    focus(r)
    type(r, '12 ft')
    expect(r.value).toBe('12 ft')
    expect(note()).toBeNull()
    expect(r.getAttribute('aria-invalid')).toBeNull()
    expect(run().disabled).toBe(false)
    enter(r)
    expect(edits()).toEqual([{ type: 'applyEdit', op: 'createCircle', payload: { cx: 0, cy: 0, radius: 144, layer: '' } }])
    expect(r.value).toBe('144')
  })

  it('"@+2" and "@*4" work on the field\'s own value and commit on blur', async () => {
    await openDrawing('mm')
    arm('createCircle')
    const r = field('r')
    expect(r.value).toBe('10')
    focus(r)
    type(r, '@')
    type(r, '@+2')
    expect(r.value).toBe('@+2')
    expect(note()).toBeNull()
    expect(run().disabled).toBe(false)
    leave(r)
    expect(r.value).toBe('12')
    focus(r)
    type(r, '@*4')
    expect(r.value).toBe('@*4')
    leave(r)
    expect(r.value).toBe('48')
    enter(r)
    expect(edits().at(-1).payload).toEqual({ cx: 0, cy: 0, radius: 48, layer: '' })
  })

  it('"-5" commits as minus 5, never as arithmetic on the current value', async () => {
    await openDrawing('mm')
    arm('createLine')
    const x = field('x')
    focus(x)
    type(x, '3')
    leave(x)
    expect(x.value).toBe('3')
    focus(x)
    type(x, '-5')
    leave(x)
    expect(x.value).toBe('-5')
    enter(x)
    expect(edits().at(-1).payload).toEqual({ x1: -5, y1: 0, x2: 100, y2: 0, layer: '' })
  })

  it('"3,4" in a point step\'s first field reaches the point-expression path unchanged', async () => {
    await openDrawing('mm')
    arm('createLine')
    const x2 = field('x2')
    focus(x2)
    type(x2, '3,4')
    leave(x2)
    expect(x2.value).toBe('3,4')
    expect(note()).toBeNull()
    enter(x2)
    expect(edits().at(-1).payload).toEqual({ x1: 0, y1: 0, x2: 3, y2: 4, layer: '' })
    expect(field('x2').value).toBe('3')
    expect(field('y2').value).toBe('4')
  })

  it('"@1,2" in a point step\'s first field measures from the previous point, unchanged', async () => {
    await openDrawing('mm')
    arm('createLine')
    type(field('x'), '10')
    type(field('y'), '20')
    const x2 = field('x2')
    focus(x2)
    type(x2, '@1,2')
    leave(x2)
    expect(x2.value).toBe('@1,2')
    expect(note()).toBeNull()
    enter(x2)
    expect(edits().at(-1).payload).toEqual({ x1: 10, y1: 20, x2: 11, y2: 22, layer: '' })
  })

  it('the arrow keys step a dimension field, Shift by 10 and Alt by 0.1, and leave a point expression alone', async () => {
    await openDrawing('mm')
    arm('createCircle')
    const r = field('r')
    focus(r)
    expect(fireEvent.keyDown(r, { key: 'ArrowUp' })).toBe(false)
    expect(r.value).toBe('11')
    fireEvent.keyDown(r, { key: 'ArrowDown', shiftKey: true })
    expect(r.value).toBe('1')
    fireEvent.keyDown(r, { key: 'ArrowUp', altKey: true })
    expect(r.value).toBe('1.1')
    type(r, '@+2')
    fireEvent.keyDown(r, { key: 'ArrowUp' })
    expect(r.value).toBe('4.1')
    const x = field('x')
    type(x, '3,4')
    expect(fireEvent.keyDown(x, { key: 'ArrowUp' })).toBe(true)
    expect(x.value).toBe('3,4')
  })

  it('a horizontal drag on a one-field step\'s label scrubs its value and stops on release', async () => {
    await openDrawing('mm')
    arm('createCircle')
    const r = field('r')
    const label = document.querySelector('[data-scrub="r"]')
    expect(label.textContent).toBe('Specify radius:')
    // A point step's label (two fields) does not scrub.
    expect(document.querySelector('[data-scrub="x"]')).toBeNull()
    act(() => { label.dispatchEvent(new MouseEvent('pointerdown', { clientX: 100, button: 0, bubbles: true, cancelable: true })) })
    act(() => { window.dispatchEvent(new MouseEvent('pointermove', { clientX: 100 + 5 * SCRUB_PX })) })
    expect(r.value).toBe('15')
    act(() => { window.dispatchEvent(new MouseEvent('pointermove', { clientX: 100 - 2 * SCRUB_PX })) })
    expect(r.value).toBe('8')
    act(() => { window.dispatchEvent(new MouseEvent('pointerup', { clientX: 100 - 2 * SCRUB_PX })) })
    act(() => { window.dispatchEvent(new MouseEvent('pointermove', { clientX: 400 })) })
    expect(r.value).toBe('8')
  })

  it('a refused dimension keeps its text, its outline and its reason until the next keystroke, and posts nothing', async () => {
    await openDrawing('in')
    arm('createCircle')
    const r = field('r')
    focus(r)
    type(r, '12 yd')
    enter(r)
    expect(edits()).toEqual([])
    expect(r.value).toBe('12 yd')
    expect(r.getAttribute('aria-invalid')).toBe('true')
    expect(note().textContent).toBe('CIRCLE refused: radius: The dimension unit is unknown; use mm, cm, m, in or ft.')
    expect(r.title).toContain('The dimension unit is unknown')
    expect(run().disabled).toBe(true)
    type(r, '12 in')
    expect(r.getAttribute('aria-invalid')).toBeNull()
    expect(note()).toBeNull()
    expect(run().disabled).toBe(false)
  })

  it('with no drawing unit a typed unit is refused, never guessed, while plain arithmetic still commits', async () => {
    await openDrawing(null)
    arm('createCircle')
    const r = field('r')
    expect(document.querySelector('[data-unit-for="r"]')).toBeNull()
    focus(r)
    type(r, '12 ft')
    leave(r)
    expect(r.value).toBe('12 ft')
    expect(r.getAttribute('aria-invalid')).toBe('true')
    expect(note().textContent).toContain('The drawing unit is unknown')
    type(r, '@+2')
    leave(r)
    expect(r.value).toBe('12')
    expect(r.getAttribute('aria-invalid')).toBeNull()
  })

  it('an angle takes arithmetic but refuses a length unit', async () => {
    await openDrawing('in')
    arm('createArc')
    const a0 = field('start')
    expect(document.querySelector('[data-unit-for="a0"]')).toBeNull()
    focus(a0)
    type(a0, '90 ft')
    leave(a0)
    expect(a0.getAttribute('aria-invalid')).toBe('true')
    expect(note().textContent).toContain('A length unit does not apply here')
    type(a0, '@+45')
    leave(a0)
    expect(a0.value).toBe('45')
  })
})
