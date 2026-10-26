import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import WidgetsView from './WidgetsView.jsx'
import WidgetInspector from './WidgetInspector.jsx'
import { DESIGNER_DEFAULT_DOC } from './designerDefault.js'
import { FORMAT_NAMES } from './WidgetDesigner.jsx'
import { FORMATS, PRESETS } from './widgetCatalog.js'
import { migrate, validateWidgetDoc } from './widgetDoc.js'
import { createEditorState, createWidgetDoc, HANDLES, snap1, snapTargets, supportsPreset, transformElement, useWidgetEditor, widgetEditorReducer as reduce } from './useWidgetEditor.js'
import { loadGraphOutputs, loadWidget, resolveValues, saveWidget } from './widgetsClient.js'
import { manifests } from '../canvas/registry.js'

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers() })
const click = (name) => fireEvent.click(screen.getByRole('button', { name, exact: true }))
const edit = (label, value) => {
  const input = screen.getByLabelText(label)
  fireEvent.change(input, { target: { value } })
  if (input.type === 'number') fireEvent.blur(input)
}
const response = (body, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => body })
const selected = (state) => state.doc.formats[state.doc.active].els.find((e) => e.id === state.selectedId)
function seed(format = 'la') {
  let state = createEditorState()
  state = reduce(state, { type: 'format', format })
  state = reduce(state, { type: 'add' })
  return state
}
function inBounds(element, format, h = FORMATS[format].h) {
  expect(element.x).toBeGreaterThanOrEqual(0); expect(element.y).toBeGreaterThanOrEqual(0)
  expect(element.w).toBeGreaterThanOrEqual(6); expect(element.h).toBeGreaterThanOrEqual(6)
  expect(element.x + element.w).toBeLessThanOrEqual(FORMATS[format].w)
  expect(element.y + element.h).toBeLessThanOrEqual(h)
}

describe.each(Object.keys(FORMATS))('%s geometry', (format) => {
  it('adds, drags, clamps at all edges and preserves valid documents', () => {
    const state = seed(format)
    for (const [dx, dy] of [[3, 2], [-10000, 0], [10000, 0], [0, -10000], [0, 10000]]) {
      const next = reduce(state, { type: 'move', dx, dy, alt: true })
      inBounds(selected(next), format)
      expect(() => validateWidgetDoc(next.doc)).not.toThrow()
    }
    expect(selected(reduce(state, { type: 'move', dx: 1, dy: 1, alt: true })).x).not.toBe(selected(state).x)
  })
  it.each(HANDLES)('resizes %s, anchors the opposite edge, and holds minimum sizes', (handle) => {
    const state = reduce(seed(format), { type: 'edit', patch: { x: 8, y: 8, w: 12, h: 8 } })
    const original = selected(state)
    for (const delta of [-10000, -2, 2, 10000]) {
      const next = reduce(state, { type: 'resize', handle, dx: delta, dy: delta, alt: true })
      const e = selected(next)
      inBounds(e, format)
      if (handle.includes('w')) expect(e.x + e.w).toBe(original.x + original.w)
      if (handle.includes('n')) expect(e.y + e.h).toBe(original.y + original.h)
      if (handle.includes('e')) expect(e.x).toBe(original.x)
      if (handle.includes('s')) expect(e.y).toBe(original.y)
      expect(() => validateWidgetDoc(next.doc)).not.toThrow()
    }
  })
  it('snaps within four points, bypasses with Alt, and leaves five points alone', () => {
    const state = seed(format), doc = state.doc
    const original = { ...selected(state), x: 0, y: 0, w: 6, h: 6 }
    const margin = FORMATS[format].w > 200 ? 14 : 6
    const snapped = transformElement(doc, original, 'move', margin + 1, 0)
    expect(snapped.element.x).toBe(margin)
    expect(snapped.guides.x).toBe(margin)
    expect(transformElement(doc, original, 'move', margin + 1, 0, true).element.x).toBe(margin + 1)
    expect(snap1([5], [0])).toBeNull()
    expect(snap1([4], [0])).toEqual({ delta: -4, target: 0 })
  })
})

describe('document editing and history', () => {
  it('pins the frozen default snapshot to the migrated designer fixture and returns independent copies', () => {
    const fixture = JSON.parse(readFileSync(`${process.cwd()}/../contract/fixtures/widget/designer-v2-default.json`, 'utf8'))
    expect(DESIGNER_DEFAULT_DOC).toEqual(migrate(fixture))
    expect(Object.values(createEditorState().doc.formats).every((f) => f.els.length === 0 && f.ship)).toBe(true)
    const doc = createWidgetDoc(DESIGNER_DEFAULT_DOC), other = createWidgetDoc(DESIGNER_DEFAULT_DOC)
    expect(doc).toEqual(DESIGNER_DEFAULT_DOC)
    expect(Object.isFrozen(DESIGNER_DEFAULT_DOC.formats.la.els[0])).toBe(true)
    doc.formats.la.els[0].x += 1
    expect(other).toEqual(DESIGNER_DEFAULT_DOC)
  })
  it('opens absent catalog formats from a valid partial document and preserves undo', () => {
    const doc = createWidgetDoc()
    doc.formats = { la: doc.formats.la }
    let state = createEditorState(validateWidgetDoc(doc))
    expect(reduce(state, { type: 'format', format: 'la' })).toBe(state)
    expect(reduce(state, { type: 'format', format: '__proto__' })).toBe(state)
    state = reduce(state, { type: 'format', format: 'homeS' })
    expect(state.doc.active).toBe('homeS')
    expect(state.doc.formats.homeS.els).toEqual([])
    expect(() => validateWidgetDoc(state.doc)).not.toThrow()
    state = reduce(state, { type: 'add' })
    expect(state.doc.formats.homeS.els).toHaveLength(1)
    state = reduce(reduce(state, { type: 'undo' }), { type: 'undo' })
    expect(state.doc).toEqual(doc)
  })
  it('ignores format switches during a drag and restores the original on cancellation', () => {
    let state = seed()
    const original = state
    state = reduce(state, { type: 'begin' })
    state = reduce(state, { type: 'move', dx: 8, dy: 8, alt: true })
    state = reduce(state, { type: 'format', format: 'homeS' })
    expect(state.doc.active).toBe('la')
    state = reduce(state, { type: 'cancel' })
    expect(state).toEqual(original)
  })
  it('loads a document during a gesture without retaining its transaction or snapshot', () => {
    let state = seed()
    state = reduce(state, { type: 'begin' })
    state = reduce(state, { type: 'move', dx: 8, dy: 8, alt: true })
    const doc = seed('homeS').doc
    state = reduce(state, { type: 'load', doc })
    expect(state).toEqual(createEditorState(doc))
    expect(reduce(state, { type: 'cancel' })).toBe(state)
    state = reduce(state, { type: 'add' })
    state = reduce(state, { type: 'edit', patch: { text: 'After load' } })
    state = reduce(reduce(state, { type: 'undo' }), { type: 'undo' })
    expect(state.doc).toEqual(doc)
  })
  it('supports every catalog preset as a valid primitive on every surface', () => {
    for (const format of Object.keys(FORMATS)) for (const preset of Object.keys(PRESETS)) {
      const state = reduce(seed(format), { type: 'add', preset })
      expect(() => validateWidgetDoc(state.doc)).not.toThrow()
      expect(state.doc.formats[format].els).toHaveLength(supportsPreset(format, preset) ? 2 : 1)
    }
  })
  it('keeps whole-document snapshots across three edits and caps history at 150', () => {
    let state = seed(), docs = [state.doc]
    for (let n = 1; n <= 3; n += 1) { state = reduce(state, { type: 'edit', patch: { text: String(n) } }); docs.push(state.doc) }
    for (let n = 2; n >= 0; n -= 1) { state = reduce(state, { type: 'undo' }); expect(state.doc).toEqual(docs[n]) }
    for (let n = 1; n <= 3; n += 1) { state = reduce(state, { type: 'redo' }); expect(state.doc).toEqual(docs[n]) }
    state = reduce(state, { type: 'select', id: 'text1' })
    for (let n = 0; n < 160; n += 1) state = reduce(state, { type: 'edit', patch: { text: String(n) } })
    expect(state.past).toHaveLength(150)
    state = reduce(state, { type: 'undo' }); state = reduce(state, { type: 'add' })
    expect(state.future).toHaveLength(0)
  })
  it('duplicates with the first unused preset id and eight-point offset, hides, reorders and deletes', () => {
    let state = seed()
    const original = selected(state)
    state = reduce(state, { type: 'duplicate' })
    expect(selected(state)).toMatchObject({ id: 'text2', x: original.x + 8, y: original.y + 8 })
    state = reduce(state, { type: 'visibility' }); expect(state.selectedId).toBeNull()
    expect(state.doc.formats.la.els[1].visible).toBe(false)
    state = reduce(state, { type: 'select', id: 'text2' })
    state = reduce(state, { type: 'reorder', index: 0 }); expect(state.doc.formats.la.els[0].id).toBe('text2')
    state = reduce(state, { type: 'delete' }); state = reduce(state, { type: 'select', id: 'text1' }); state = reduce(state, { type: 'duplicate' })
    expect(selected(state).id).toBe('text2')
  })
  it('coalesces pointer gestures and nudge bursts, cancels a gesture, and clears redo on edits', () => {
    let state = seed(), original = state.doc
    state = reduce(state, { type: 'begin' })
    for (let n = 0; n < 20; n += 1) state = reduce(state, { type: 'move', dx: 1, dy: 0, alt: true })
    state = reduce(state, { type: 'cancel' }); expect(state.doc).toBe(original)
    const history = state.past.length
    state = reduce(state, { type: 'begin' })
    for (let n = 0; n < 20; n += 1) state = reduce(state, { type: 'move', dx: 1, dy: 0, alt: true })
    state = reduce(state, { type: 'end' }); expect(state.past).toHaveLength(history + 1)
    state = reduce(state, { type: 'undo' }); expect(state.doc).toBe(original)
    state = reduce(state, { type: 'select', id: 'text1' })
    for (const time of [0, 100, 300]) state = reduce(state, { type: 'nudge', dx: 1, dy: 0, time })
    expect(state.past).toHaveLength(history + 1)
    state = reduce(state, { type: 'nudge', dx: 8, dy: 0, time: 701 })
    expect(state.past).toHaveLength(history + 2); expect(state.future).toHaveLength(0)
  })
  it('includes cutout and visible peer edges/centres and resolves ties in reference order', () => {
    let state = seed('diExp')
    state = reduce(state, { type: 'duplicate' })
    const targets = snapTargets(state.doc, state.selectedId)
    expect(targets.xs).toContain(122.5); expect(targets.xs).toContain(248.5); expect(targets.ys).toContain(37)
    expect(targets.xs).toContain(state.doc.formats.diExp.els[0].x)
    expect(snap1([12], [14, 10])).toEqual({ delta: 2, target: 14 })
    expect(snap1([12, 16], [18, 10])).toEqual({ delta: -2, target: 10 })
    state = reduce(state, { type: 'visibility', id: 'text1' })
    expect(snapTargets(state.doc, 'text2').xs).not.toContain(state.doc.formats.diExp.els[0].x)
  })
  it.each(['x', 'y'])('keeps the first element anchor on %s snap ties and commits matching geometry', (axis) => {
    let state = seed()
    state = reduce(state, { type: 'edit', patch: { [axis]: 120, w: 18, h: 18 } })
    state = reduce(state, { type: 'add' })
    state = reduce(state, { type: 'edit', patch: { [axis]: 100, w: 18, h: 18 } })
    state = reduce(state, { type: 'add' })
    state = reduce(state, { type: 'edit', patch: { [axis]: 101, w: 18, h: 18 } })
    const original = selected(state)
    state = reduce(state, { type: 'begin' })
    state = reduce(state, { type: 'move', original, dx: 0, dy: 0 })
    expect(selected(state)[axis]).toBe(100)
    expect(state.guides[axis]).toBe(100)
    state = reduce(state, { type: 'end' })
    expect(selected(state)[axis]).toBe(100)
    expect(state.guides).toEqual({})
    expect(selected(reduce(state, { type: 'undo' }))).toBeUndefined()
    expect(reduce(state, { type: 'undo' }).doc.formats.la.els[2]).toEqual(original)
  })
  it('bounds surface edits, reclamps elements and keeps dividers one point high', () => {
    let state = reduce(seed(), { type: 'surface', patch: { h: 60, radius: 300, bg: 'blur', ship: false } })
    expect(state.doc.formats.la).toMatchObject({ h: 60, radius: 30, bg: 'blur', ship: false })
    expect(() => validateWidgetDoc(state.doc)).not.toThrow()
    state = reduce(state, { type: 'add', preset: 'divider' })
    state = reduce(state, { type: 'resize', handle: 'e', dx: -1000, dy: 1000, alt: true })
    expect(selected(state).h).toBe(1); expect(selected(state).w).toBe(6)
  })
  it('records p95 for 200 reducer moves with 32 elements', () => {
    let state = createEditorState()
    for (let n = 0; n < 32; n += 1) state = reduce(state, { type: 'add' })
    const original = selected(state), timings = []
    state = reduce(state, { type: 'begin' })
    for (let n = 0; n < 200; n += 1) {
      const started = performance.now()
      state = reduce(state, { type: 'move', original, dx: n % 40, dy: n % 30 })
      timings.push(performance.now() - started)
    }
    timings.sort((a, b) => a - b)
    const p95 = timings[Math.ceil(timings.length * 0.95) - 1]
    console.info(`Widget pointer reducer, 32 elements, 200 moves: p95=${p95.toFixed(3)}ms`)
    expect(p95).toBeLessThanOrEqual(4)
  })
})

describe('designer interactions', () => {
  it.each(['click', 'Enter', ' '])('reveals and selects a hidden layer via %s in one undoable step', (activation) => {
    render(<WidgetsView scopeKey="hidden-layer" />); click('Add text'); click('Hide text1')
    const surface = screen.getByTestId('widget-surface')
    const layer = screen.getByRole('button', { name: 'Text 1: Your text' })
    expect(surface.querySelector('[data-element-id="text1"]')).toBeNull()
    expect(surface.querySelector('[data-handle]')).toBeNull()
    if (activation === 'click') fireEvent.click(layer)
    else fireEvent.keyDown(layer, { key: activation })
    expect(layer).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByLabelText('Text')).toHaveValue('Your text')
    expect(screen.getByRole('button', { name: 'Hide text1' })).toBeInTheDocument()
    expect(surface.querySelector('[data-element-id="text1"]')).not.toBeNull()
    expect(surface.querySelectorAll('[data-handle]')).toHaveLength(8)
    fireEvent.click(layer)
    click('Undo')
    expect(screen.getByRole('button', { name: 'Show text1' })).toBeInTheDocument()
    expect(surface.querySelector('[data-element-id="text1"]')).toBeNull()
    expect(surface.querySelector('[data-handle]')).toBeNull()
    expect(screen.queryByLabelText('Text')).not.toBeInTheDocument()
    click('Undo')
    expect(surface.querySelector('[data-element-id="text1"]')).not.toBeNull()
  })
  it('ignores other pointers ending a drag and cancels the current gesture with Escape', () => {
    vi.stubGlobal('PointerEvent', class extends MouseEvent {
      constructor(type, options) { super(type, options); this.pointerId = options.pointerId ?? 1 }
    })
    render(<WidgetsView scopeKey="local" />); click('Add text')
    const surface = screen.getByTestId('widget-surface')
    vi.spyOn(surface, 'getBoundingClientRect').mockReturnValue({ width: FORMATS.la.w })
    const x = screen.getByLabelText('X').valueAsNumber
    fireEvent.pointerDown(surface.querySelector('[data-element-id]'), { clientX: 10, clientY: 10, pointerId: 1, button: 0 })
    fireEvent.pointerMove(surface, { clientX: 14, clientY: 10, pointerId: 1, altKey: true })
    fireEvent.pointerUp(surface, { pointerId: 2 })
    fireEvent.pointerMove(surface, { clientX: 18, clientY: 10, pointerId: 1, altKey: true })
    expect(screen.getByLabelText('X').valueAsNumber).toBe(x + 8)
    fireEvent.keyDown(surface, { key: 'Escape' })
    expect(screen.getByLabelText('X').valueAsNumber).toBe(x)
    fireEvent.pointerMove(surface, { clientX: 22, clientY: 10, pointerId: 1, altKey: true })
    expect(screen.getByLabelText('X').valueAsNumber).toBe(x)
  })
  it.each(Object.keys(FORMATS))('captures drag and all eight resize handles on %s at fit zoom', (format) => {
    vi.stubGlobal('PointerEvent', class extends MouseEvent {
      constructor(type, options) { super(type, options); this.pointerId = options.pointerId ?? 1 }
    })
    render(<WidgetsView scopeKey="local" />)
    click(FORMAT_NAMES[format]); click('Add text')
    const surface = screen.getByTestId('widget-surface')
    vi.spyOn(surface, 'getBoundingClientRect').mockReturnValue({ width: FORMATS[format].w / 2, height: FORMATS[format].h / 2, x: 0, y: 0, left: 0, top: 0, right: FORMATS[format].w / 2, bottom: FORMATS[format].h / 2 })
    surface.setPointerCapture = vi.fn()
    surface.releasePointerCapture = vi.fn()
    surface.hasPointerCapture = () => true
    const box = () => Object.fromEntries(['X', 'Y', 'W', 'H'].map((field) => [field.toLowerCase(), screen.getByLabelText(field).valueAsNumber]))
    const resetBox = () => { edit('W', '12'); edit('H', '8'); edit('X', '8'); edit('Y', '8') }
    resetBox()
    fireEvent.pointerDown(surface.querySelector('[data-element-id]'), { clientX: 20, clientY: 20, pointerId: 1, button: 0 })
    fireEvent.pointerMove(surface, { clientX: 21, clientY: 21, pointerId: 1, altKey: true })
    fireEvent.pointerUp(surface, { pointerId: 1 })
    expect(box()).toMatchObject({ x: 10, y: 10 })
    expect(surface.setPointerCapture).toHaveBeenCalledWith(1)
    expect(surface.releasePointerCapture).toHaveBeenCalledWith(1)
    expect(surface.querySelectorAll('[data-handle]')).toHaveLength(8)
    for (const handle of HANDLES) {
      resetBox()
      const original = box()
      fireEvent.pointerDown(surface.querySelector(`[data-handle="${handle}"]`), { clientX: 20, clientY: 20, pointerId: 1, button: 0 })
      fireEvent.pointerMove(surface, { clientX: 21, clientY: 21, pointerId: 1, altKey: true })
      fireEvent.pointerUp(surface, { pointerId: 1 })
      const next = box()
      inBounds(next, format)
      if (handle.includes('w')) expect(next.x + next.w).toBe(original.x + original.w)
      if (handle.includes('n')) expect(next.y + next.h).toBe(original.y + original.h)
      expect(next).not.toEqual(original)
    }
    const original = box()
    fireEvent.pointerDown(surface.querySelector('[data-element-id]'), { clientX: 20, clientY: 20, pointerId: 1, button: 0 })
    fireEvent.pointerMove(surface, { clientX: 22, clientY: 20, pointerId: 1, altKey: true })
    fireEvent.pointerCancel(surface, { pointerId: 1 })
    expect(box()).toEqual(original)
  })
  it('supports layers keyboard selection, visibility, order and deletion', () => {
    render(<WidgetsView scopeKey="local" />)
    click('Add text'); edit('Text', 'First'); click('Add text')
    const list = screen.getByRole('list', { name: 'Text elements' })
    fireEvent.keyDown(within(list).getByRole('button', { name: 'Text 1: First' }), { key: ' ' })
    expect(screen.getByLabelText('Text')).toHaveValue('First')
    click('Hide text1'); expect(screen.getByRole('button', { name: 'Show text1' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Preview' }).querySelectorAll('[data-element-id]')).toHaveLength(1)
    click('Move text1 later')
    expect(within(list).getAllByRole('button')[0]).toHaveTextContent('Text 1: Your text')
    fireEvent.keyDown(within(list).getByRole('button', { name: 'Text 2: First' }), { key: 'Enter' })
    click('Remove text1')
    expect(screen.queryByLabelText('Text')).not.toBeInTheDocument()
  })
  it('edits styles and preset controls, restricts divider handles and exposes accessory tint', () => {
    render(<WidgetsView scopeKey="local" />)
    click('Add text'); edit('Scale', '1.4'); edit('Weight', '700'); edit('Color', 'leaf'); edit('Alignment', 'right')
    expect(screen.getByLabelText('Scale')).toHaveValue('1.4')
    expect(screen.getByLabelText('Weight')).toHaveValue(700)
    expect(screen.getByLabelText('Color')).toHaveValue('leaf')
    expect(screen.getByLabelText('Alignment')).toHaveValue('right')
    edit('Element preset', 'pnl'); click('Add element'); edit('Rows', '2')
    fireEvent.click(screen.getByLabelText('Arrows'))
    expect(screen.getByLabelText('Rows')).toHaveValue(2)
    expect(screen.getByLabelText('Arrows')).not.toBeChecked()
    edit('Element preset', 'todayPnl'); click('Add element')
    fireEvent.click(screen.getByLabelText('Show %')); expect(screen.getByLabelText('Show %')).not.toBeChecked()
    edit('Element preset', 'history'); click('Add element'); edit('Entries', '3')
    expect(screen.getByLabelText('Entries')).toHaveValue(3)
    edit('Element preset', 'divider'); click('Add element'); edit('Opacity', '0.5')
    expect([...screen.getByTestId('widget-surface').querySelectorAll('[data-handle]')].map((e) => e.dataset.handle)).toEqual(['w', 'e'])
    click('Circular')
    expect(screen.getByLabelText('Preview one-color')).toBeChecked()
    fireEvent.click(screen.getByLabelText('Preview one-color')); fireEvent.click(screen.getByLabelText('Include this format'))
    expect(screen.getByLabelText('Preview one-color')).not.toBeChecked()
    expect(screen.getByLabelText('Include this format')).not.toBeChecked()
    click('Inline')
    expect(screen.queryByRole('option', { name: 'Divider' })).not.toBeInTheDocument()
  })
  it('does not edit a hidden designer with global shortcuts', () => {
    const { rerender } = render(<WidgetsView scopeKey="local" />)
    click('Add text')
    const x = screen.getByLabelText('X').valueAsNumber
    rerender(<WidgetsView scopeKey="local" hidden />)
    fireEvent.keyDown(document.body, { key: 'Delete' })
    fireEvent.keyDown(document.body, { key: 'ArrowRight' })
    rerender(<WidgetsView scopeKey="local" />)
    expect(screen.getByLabelText('X').valueAsNumber).toBe(x)
    expect(screen.getByLabelText('Text')).toHaveValue('Your text')
  })
  it.each(['move', 'se'])('cancels an unmounted %s gesture and restores the scoped draft and history', (handle) => {
    vi.stubGlobal('PointerEvent', class extends MouseEvent {
      constructor(type, options) { super(type, options); this.pointerId = options.pointerId ?? 1 }
    })
    const { rerender } = render(<WidgetsView scopeKey="first" />)
    click('Add text'); edit('Text', 'Original'); click('Add text'); click('Undo')
    fireEvent.click(screen.getByRole('button', { name: 'Text 1: Original', exact: true }))
    const surface = screen.getByTestId('widget-surface'), original = surface.innerHTML
    surface.setPointerCapture = vi.fn()
    surface.hasPointerCapture = () => true
    surface.releasePointerCapture = vi.fn()
    const target = surface.querySelector(handle === 'move' ? '[data-element-id]' : `[data-handle="${handle}"]`)
    fireEvent.pointerDown(target, { clientX: 20, clientY: 20, pointerId: 1, button: 0 })
    fireEvent.pointerMove(surface, { clientX: 40, clientY: 40, pointerId: 1, altKey: true })
    expect(surface.innerHTML).not.toBe(original)
    rerender(<WidgetsView scopeKey="second" />)
    expect(surface.releasePointerCapture).toHaveBeenCalledWith(1)
    click('Add text'); edit('Text', 'Other scope')
    rerender(<WidgetsView scopeKey="first" />)
    expect(screen.getByTestId('widget-surface').innerHTML).toBe(original)
    expect(screen.getByLabelText('Text')).toHaveValue('Original')
    expect(screen.getByRole('button', { name: 'Redo', exact: true })).toBeEnabled()
    click('Add text')
    expect(screen.getByTestId('widget-surface').querySelectorAll('[data-element-id]')).toHaveLength(2)
    edit('Text', 'After cancellation')
    expect(screen.getByLabelText('Text')).toHaveValue('After cancellation')
    click('Undo')
    fireEvent.click(screen.getByRole('button', { name: 'Text 2: Your text', exact: true }))
    expect(screen.getByLabelText('Text')).toHaveValue('Your text')
    click('Undo')
    fireEvent.click(screen.getByRole('button', { name: 'Text 1: Original', exact: true }))
    expect(screen.getByTestId('widget-surface').innerHTML).toBe(original)
  })
  it('supports arrows, shift arrows, escape, delete, duplicate, undo/redo outside inputs', () => {
    render(<WidgetsView scopeKey="local" />); click('Add text')
    const x = screen.getByLabelText('X').valueAsNumber
    fireEvent.keyDown(document.body, { key: 'ArrowRight' })
    expect(screen.getByLabelText('X').valueAsNumber).toBe(x + 1)
    fireEvent.keyDown(document.body, { key: 'ArrowRight', shiftKey: true })
    expect(screen.getByLabelText('X').valueAsNumber).toBe(x + 9)
    fireEvent.keyDown(screen.getByLabelText('Text'), { key: 'Backspace' })
    expect(screen.getByLabelText('Text')).toBeInTheDocument()
    fireEvent.keyDown(document.body, { key: 'd', ctrlKey: true })
    expect(screen.getByLabelText('X').valueAsNumber).toBe(x + 17)
    fireEvent.keyDown(document.body, { key: 'z', metaKey: true })
    expect(screen.getByRole('region', { name: 'Preview' }).querySelectorAll('[data-element-id]')).toHaveLength(1)
    fireEvent.keyDown(document.body, { key: 'z', metaKey: true, shiftKey: true })
    expect(screen.getByRole('region', { name: 'Preview' }).querySelectorAll('[data-element-id]')).toHaveLength(2)
    fireEvent.click(within(screen.getByRole('list', { name: 'Text elements' })).getByRole('button', { name: 'Text 2: Your text' }))
    fireEvent.keyDown(document.body, { key: 'Delete' })
    expect(screen.getByRole('region', { name: 'Preview' }).querySelectorAll('[data-element-id]')).toHaveLength(1)
    click('Add text'); fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(screen.queryByLabelText('Text')).not.toBeInTheDocument()
  })
  it('edits surface controls and requires a second reset click within three seconds', () => {
    render(<WidgetsView scopeKey="local" />)
    edit('Height', '80'); edit('Corners', '10'); edit('Background', 'blur')
    click('Add text'); click('Edit surface'); click('Reset this format')
    expect(screen.getByRole('button', { name: 'Click again to reset' })).toBeInTheDocument()
    click('Click again to reset'); expect(screen.getByLabelText('Height')).toHaveValue('160')
    expect(screen.getByRole('region', { name: 'Preview' }).querySelectorAll('[data-element-id]')).toHaveLength(DESIGNER_DEFAULT_DOC.formats.la.els.length)
    vi.useFakeTimers(); click('Reset this format')
    // Date-based guard also prevents a delayed second click from resetting.
    vi.setSystemTime(Date.now() + 3001); click('Click again to reset')
    expect(screen.getByRole('button', { name: 'Click again to reset' })).toBeInTheDocument()
  })
  it('provides both typed binding sources and prevents discovery calls in local preview', () => {
    const fetchImpl = vi.fn(); vi.stubGlobal('fetch', fetchImpl)
    render(<WidgetsView scopeKey="local" />); click('Add text')
    edit('Binding source', 'kashbot.board'); edit('Board field', 'winRate')
    expect(screen.getByLabelText('Board field')).toHaveValue('winRate')
    edit('Binding scale', 'fraction'); edit('Binding tone', 'leaf')
    edit('Binding source', 'graph.output'); edit('Graph document ID', 'strategy')
    expect(screen.getByRole('button', { name: 'Load outputs' })).toBeDisabled()
    expect(fetchImpl).not.toHaveBeenCalled()
  })
})

describe('SEL-207 critic regressions', () => {
  function tinyState() {
    const state = seed()
    Object.assign(selected(state), { x: 360, y: 159, w: 1, h: 1 })
    return state
  }
  it('G1 keeps all resize handles inside the surface when the available room is below the minimum', () => {
    const state = tinyState()
    for (const preset of ['text', 'divider']) for (const handle of HANDLES) for (const delta of [-1000, 0, 1000]) for (const alt of [false, true]) {
      const original = { ...selected(state), preset }
      const { element } = transformElement(state.doc, original, handle, delta, delta, alt)
      expect(element.x).toBeGreaterThanOrEqual(0); expect(element.y).toBeGreaterThanOrEqual(0)
      expect(element.w).toBeGreaterThan(0); expect(element.h).toBeGreaterThan(0)
      expect(element.x + element.w).toBeLessThanOrEqual(361)
      expect(element.y + element.h).toBeLessThanOrEqual(160)
    }
  })
  it('G2 preserves tiny loaded dimensions and unchanged coordinates across unrelated operations', () => {
    const state = tinyState(), original = selected(state)
    for (const action of [
      { type: 'nudge', dx: 1, dy: 1, time: 0 },
      { type: 'edit', patch: { color: 'leaf' } },
      { type: 'surface', patch: { bg: 'blur' } },
      { type: 'duplicate' },
    ]) {
      const next = reduce(state, action)
      expect(selected(next)).toMatchObject({ x: 360, y: 159, w: 1, h: 1 })
      expect(next.doc.formats.la.els[0]).toMatchObject({ x: original.x, y: original.y, w: original.w, h: original.h })
    }
    const moved = reduce(state, { type: 'edit', patch: { x: 10 } })
    expect(selected(moved)).toMatchObject({ x: 10, y: 159, w: 1, h: 1 })
  })
  it('G3 isolates pointer transactions and cancellation restores document selection and both history stacks', () => {
    vi.stubGlobal('PointerEvent', class extends MouseEvent {
      constructor(type, options) { super(type, options); this.pointerId = options.pointerId ?? 1 }
    })
    let initial = reduce(seed(), { type: 'duplicate' })
    initial = reduce(initial, { type: 'undo' })
    initial = reduce(initial, { type: 'select', id: null })
    let state = reduce(initial, { type: 'begin', id: 'text1' })
    state = reduce(state, { type: 'move', dx: 10, dy: 10, alt: true })
    for (const action of [
      { type: 'add' }, { type: 'duplicate' }, { type: 'delete' }, { type: 'reset' },
      { type: 'visibility' }, { type: 'format', format: 'homeS' }, { type: 'select', id: null },
      { type: 'undo' }, { type: 'redo' }, { type: 'nudge', dx: 1, dy: 0, time: 0 },
      { type: 'edit', patch: { color: 'leaf' } }, { type: 'surface', patch: { bg: 'blur' } },
    ]) expect(reduce(state, action)).toBe(state)
    expect(reduce(state, { type: 'cancel' })).toEqual(initial)
    render(<WidgetsView scopeKey="gesture" />); click('Add text')
    const surface = screen.getByTestId('widget-surface'), x = screen.getByLabelText('X').value
    fireEvent.pointerDown(surface.querySelector('[data-element-id]'), { clientX: 20, clientY: 20, pointerId: 1, button: 0 })
    fireEvent.pointerMove(surface, { clientX: 40, clientY: 40, pointerId: 1, altKey: true })
    click('Add text'); edit('Color', 'leaf'); click('Undo'); click('Small')
    fireEvent.keyDown(document.body, { key: 'Delete' })
    fireEvent.pointerCancel(surface, { pointerId: 1 })
    expect(surface.querySelectorAll('[data-element-id]')).toHaveLength(1)
    expect(screen.getByLabelText('X')).toHaveValue(Number(x))
    expect(screen.getByLabelText('Color')).toHaveValue('slate')
  })
  it('G4 ignores stale 422 validation and its pointer after the submitted document changes', async () => {
    let finish
    vi.stubGlobal('fetch', vi.fn(() => new Promise((resolve) => { finish = resolve })))
    render(<WidgetsView />); edit('Document ID', 'widget'); click('New'); click('Add text')
    click('Save'); edit('Text', 'Changed during save')
    finish(response({ detail: 'stale validation', pointer: '/formats/la/els/0' }, 422))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Local edits'))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()
  })
  it('G5 keeps server invalid status and Save disabled after no-op dispatches', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({ detail: 'bad binding', pointer: '/formats/la/els/0' }, 422)))
    render(<WidgetsView />); edit('Document ID', 'widget'); click('New'); click('Save')
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Invalid'))
    click('Lock screen'); edit('Background', 'black'); click('Edit surface')
    expect(screen.getByRole('status')).toHaveTextContent('Invalid')
    expect(screen.getByRole('alert')).toHaveTextContent('/formats/la/els/0')
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })
  it('G6 marks a missing saved graph output invalid without offering a manufactured output', async () => {
    const doc = seed().doc, manifest = manifests[0]
    doc.formats.la.els[0].binding = { source: 'graph.output', doc_id: 'strategy', output: 'removed', min_renderer: '1.0.0', scale: 'identity', tone: 'auto' }
    const fetchImpl = vi.fn().mockResolvedValueOnce(response({ graph: doc, kind: 'widget', revision: 1 }))
      .mockResolvedValue(response({ kind: 'graph', graph: { blocks: [{ id: 'node1', registry: { id: manifest.id, version: manifest.version.semver, code_sha256: manifest.version.code_sha256 } }], outputs: { answer: { node: 'node1', port: 'result' } } } }))
    vi.stubGlobal('fetch', fetchImpl)
    render(<WidgetsView />); edit('Document ID', 'widget'); click('Load')
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved'))
    click('Text 1: Your text'); click('Load outputs')
    await screen.findByRole('option', { name: 'Output not found' })
    expect(screen.queryByRole('option', { name: 'removed' })).not.toBeInTheDocument()
    expect(screen.queryByText(/^Output type:/)).not.toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Invalid')
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    edit('Graph output', 'answer')
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()
    edit('Graph output', ''); expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    edit('Binding source', 'none'); expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()
  })
  it('G7 preserves graph binding window and other properties when changing tone scale output or field', async () => {
    const doc = seed().doc, manifest = manifests[0]
    doc.formats.la.els[0].binding = { source: 'graph.output', doc_id: 'strategy', output: 'answer', field: 'old', window: 'today-utc', min_renderer: '1.0.0', scale: 'identity', tone: 'auto' }
    function BindingEditor() {
      const [state, dispatch] = useWidgetEditor(doc)
      return <><WidgetInspector doc={state.doc} selectedId="text1" dispatch={(action) => dispatch({ ...action, id: 'text1' })} local={false} /><pre data-testid="binding-state">{JSON.stringify(state.doc.formats.la.els[0].binding)}</pre></>
    }
    vi.stubGlobal('fetch', vi.fn(async () => response({ kind: 'graph', graph: { blocks: [{ id: 'node1', registry: { id: manifest.id, version: manifest.version.semver, code_sha256: manifest.version.code_sha256 } }], outputs: { answer: { node: 'node1', port: 'result' }, other: { node: 'node1', port: 'result' } } } })))
    render(<BindingEditor />); click('Load outputs')
    await screen.findByRole('option', { name: 'other — scalar<number>' })
    for (const [label, value, key] of [['Binding tone', 'leaf', 'tone'], ['Binding scale', 'fraction', 'scale'], ['Graph output', 'other', 'output'], ['Output field (optional)', 'new', 'field']]) {
      const before = JSON.parse(screen.getByTestId('binding-state').textContent)
      edit(label, value)
      expect(JSON.parse(screen.getByTestId('binding-state').textContent)).toEqual({ ...before, [key]: value })
    }
  })
  it('G8 resets each format to its starting layout and ship flag while preserving other formats', () => {
    for (const format of Object.keys(FORMATS)) {
      let state = seed(format)
      state = reduce(state, { type: 'surface', patch: { bg: 'blur', ship: !DESIGNER_DEFAULT_DOC.formats[format].ship } })
      const before = state.doc
      state = reduce(state, { type: 'reset' })
      expect(state.doc.formats[format]).toEqual(DESIGNER_DEFAULT_DOC.formats[format])
      for (const other of Object.keys(FORMATS).filter((id) => id !== format)) expect(state.doc.formats[other]).toBe(before.formats[other])
      expect(reduce(state, { type: 'undo' }).doc).toEqual(before)
    }
  })
  it('G9 starts a 404 load from the same defaults as New', async () => {
    const fetchImpl = vi.fn().mockResolvedValueOnce(response({}, 404)).mockResolvedValueOnce(response({ revision: 1 }))
    vi.stubGlobal('fetch', fetchImpl)
    render(<WidgetsView />); edit('Document ID', 'missing'); click('Load')
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Local edits'))
    click('Save')
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved revision 1'))
    expect(JSON.parse(fetchImpl.mock.calls[1][1].body)).toEqual({ graph: DESIGNER_DEFAULT_DOC, kind: 'widget', expected_head: null })
  })
  it('G10 leaves history and redo intact after blocked nudges and no-op inspector commits', () => {
    let state = tinyState()
    state = reduce(state, { type: 'edit', patch: { color: 'leaf' } })
    state = reduce(state, { type: 'undo' })
    state = reduce(state, { type: 'select', id: 'text1' })
    for (const action of [{ type: 'nudge', dx: 1, dy: 0, time: 0 }, { type: 'edit', patch: { color: 'slate' } }, { type: 'surface', patch: { bg: 'black' } }]) {
      expect(reduce(state, action)).toBe(state)
    }
    expect(selected(reduce(state, { type: 'redo' }))).toBeUndefined()
    expect(reduce(state, { type: 'redo' }).doc.formats.la.els[0].color).toBe('leaf')
  })
  it('G11 clears selection when the selected element is hidden from layers or inspector', () => {
    render(<WidgetsView scopeKey="visibility" />); click('Add text'); click('Hide text1')
    expect(screen.queryByLabelText('Text')).not.toBeInTheDocument()
    click('Show text1'); click('Text 1: Your text')
    fireEvent.click(screen.getByLabelText('Visible'))
    expect(screen.queryByLabelText('Text')).not.toBeInTheDocument()
    expect(screen.getByTestId('widget-surface').querySelector('[data-handle]')).toBeNull()
  })
  it('G12 focuses the next layer then previous layer then Add after layers and inspector removal', () => {
    render(<WidgetsView scopeKey="focus" />); click('Add text'); edit('Text', 'First')
    click('Add text'); edit('Text', 'Second'); click('Add text'); edit('Text', 'Third')
    click('Remove text2')
    expect(screen.getByRole('button', { name: 'Text 2: Third' })).toHaveFocus()
    click('Text 2: Third')
    click('Remove')
    expect(screen.getByRole('button', { name: 'Text 1: First' })).toHaveFocus()
    click('Text 1: First'); click('Remove')
    expect(screen.getByRole('button', { name: 'Add text' })).toHaveFocus()
  })
  it('G13 aligns new columns to the first visible column and clamps placement after the rightmost edge', () => {
    let state = reduce(createEditorState(), { type: 'add', preset: 'symbols' })
    state = reduce(state, { type: 'edit', patch: { x: 10, y: 20, w: 40, h: 70, maxRows: 2 } })
    state = reduce(state, { type: 'add', preset: 'pnl' })
    expect(selected(state)).toMatchObject({ x: 56, y: 20, h: 70, maxRows: 2 })
    state = reduce(state, { type: 'edit', patch: { x: 300, w: 50 } })
    state = reduce(state, { type: 'add', preset: 'oddsBar' })
    expect(selected(state)).toMatchObject({ x: 361 - selected(state).w, y: 20, h: 70, maxRows: 2 })
    state = reduce(state, { type: 'visibility', id: 'symbols1' })
    state = reduce(state, { type: 'select', id: 'pnl1' })
    state = reduce(state, { type: 'edit', patch: { y: 30, h: 60, maxRows: 1 } })
    state = reduce(state, { type: 'add', preset: 'symbols' })
    expect(selected(state)).toMatchObject({ y: 30, h: 60, maxRows: 1 })
  })
})

describe('widget store client and states', () => {
  it.each(['widget', 'other-widget'])('reloads %s mid-drag and permits Add, inspector edits and undo', async (id) => {
    vi.stubGlobal('PointerEvent', class extends MouseEvent {
      constructor(type, options) { super(type, options); this.pointerId = options.pointerId ?? 1 }
    })
    const doc = seed().doc
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(response({ graph: doc, revision: 1, kind: 'widget' }))
      .mockResolvedValueOnce(response({ graph: doc, revision: 2, kind: 'widget' }))
      .mockResolvedValueOnce(response({ revision: 3 }))
    vi.stubGlobal('fetch', fetchImpl)
    render(<WidgetsView />); edit('Document ID', 'widget'); click('Load')
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved revision 1'))
    const surface = screen.getByTestId('widget-surface')
    surface.setPointerCapture = vi.fn()
    surface.hasPointerCapture = () => true
    surface.releasePointerCapture = vi.fn()
    fireEvent.pointerDown(surface.querySelector('[data-element-id]'), { clientX: 20, clientY: 20, pointerId: 1, button: 0 })
    fireEvent.pointerMove(surface, { clientX: 40, clientY: 40, pointerId: 1, altKey: true })
    expect(screen.getByLabelText('X')).not.toHaveValue(doc.formats.la.els[0].x)
    edit('Document ID', id); click('Load')
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved revision 2'))
    expect(surface.releasePointerCapture).toHaveBeenCalledWith(1)
    click('Add text')
    expect(screen.getByTestId('widget-surface').querySelectorAll('[data-element-id]')).toHaveLength(2)
    edit('Text', 'After reload')
    expect(screen.getByLabelText('Text')).toHaveValue('After reload')
    click('Undo')
    fireEvent.click(screen.getByRole('button', { name: 'Text 2: Your text', exact: true }))
    expect(screen.getByLabelText('Text')).toHaveValue('Your text')
    click('Undo'); click('Save')
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved revision 3'))
    expect(fetchImpl.mock.calls[1][0]).toBe(`/api/lab/docs/${id}/head`)
    expect(JSON.parse(fetchImpl.mock.calls[2][1].body).graph).toEqual(doc)
  })
  it('New saves the migrated default document with all reference layouts and inclusion flags', async () => {
    const fetchImpl = vi.fn(async () => response({ revision: 1, sha256: 'default' }))
    vi.stubGlobal('fetch', fetchImpl)
    render(<WidgetsView />); edit('Document ID', 'default-widget'); click('New')
    expect(screen.getByRole('button', { name: 'Lock screen', exact: true })).toHaveAttribute('aria-pressed', 'true')
    click('Save')
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved revision 1'))
    const doc = JSON.parse(fetchImpl.mock.calls[0][1].body).graph
    expect(doc).toEqual(DESIGNER_DEFAULT_DOC)
    expect(Object.fromEntries(Object.entries(doc.formats).map(([id, f]) => [id, f.els.length]))).toEqual({
      la: 6, diExp: 6, diCompact: 2, diMin: 1, standby: 6, lockRect: 3, lockCirc: 1, lockInline: 2,
      homeS: 5, homeM: 7, homeL: 11, watchStack: 4, watchCorner: 1,
    })
    expect(Object.keys(doc.formats).filter((id) => !doc.formats[id].ship)).toEqual(['homeL', 'watchStack', 'watchCorner'])
  })
  it('follows binding history and loads outputs only for the current graph document', async () => {
    const initial = seed().doc
    initial.formats.la.els[0].binding = { source: 'graph.output', doc_id: 'A', output: 'answer', min_renderer: '1.0.0', scale: 'identity', tone: 'auto' }
    function PickerHistory() {
      const [state, dispatch] = useWidgetEditor(initial)
      function history(type) { dispatch({ type }); dispatch({ type: 'select', id: 'text1' }) }
      return <>
        <button onClick={() => history('undo')}>Undo binding</button>
        <button onClick={() => history('redo')}>Redo binding</button>
        <button onClick={() => { dispatch({ type: 'load', doc: initial }); dispatch({ type: 'select', id: 'text1' }) }}>Load binding</button>
        <WidgetInspector doc={state.doc} selectedId="text1" dispatch={(action) => dispatch({ ...action, id: 'text1' })} local={false} />
        <pre data-testid="binding-document">{JSON.stringify(state.doc)}</pre>
      </>
    }
    const manifest = manifests[0]
    const fetchImpl = vi.fn(async (url) => response({ kind: 'graph', graph: {
      blocks: [{ id: 'node1', registry: { id: manifest.id, version: manifest.version.semver, code_sha256: manifest.version.code_sha256 } }],
      outputs: { [url.includes('/B/') ? 'from-b' : 'from-a']: { node: 'node1', port: 'result' } },
    } }))
    vi.stubGlobal('fetch', fetchImpl)
    render(<PickerHistory />)
    expect(screen.getByLabelText('Graph document ID')).toHaveValue('A')
    edit('Graph document ID', 'B'); click('Load outputs')
    await screen.findByRole('option', { name: 'from-b — scalar<number>' })
    click('Undo binding')
    expect(screen.getByLabelText('Graph document ID')).toHaveValue('A')
    expect(screen.queryByRole('option', { name: 'from-b — scalar<number>' })).not.toBeInTheDocument()
    click('Redo binding')
    expect(screen.getByLabelText('Graph document ID')).toHaveValue('B')
    click('Undo binding')
    expect(screen.getByLabelText('Graph document ID')).toHaveValue('A')
    click('Load outputs')
    await screen.findByRole('option', { name: 'from-a — scalar<number>' })
    expect(fetchImpl.mock.calls.at(-1)[0]).toBe('/api/lab/docs/A/head')
    edit('Graph output', 'from-a')
    expect(JSON.parse(screen.getByTestId('binding-document').textContent).formats.la.els[0].binding).toMatchObject({ doc_id: 'A', output: 'from-a' })
    edit('Graph document ID', 'B'); click('Load binding')
    expect(screen.getByLabelText('Graph document ID')).toHaveValue('A')
    expect(screen.queryByRole('option', { name: 'from-a — scalar<number>' })).not.toBeInTheDocument()
  })
  it.each([409, 413, 503])('preserves HTTP %s when an error response is not JSON', async (status) => {
    const fetchImpl = vi.fn(async () => ({ ok: false, status, json: async () => { throw new Error('HTML response') } }))
    expect(await saveWidget('widget', createWidgetDoc(), null, { fetch: fetchImpl })).toMatchObject({ ok: false, status })
    expect(fetchImpl).toHaveBeenCalledTimes(1)
  })
  it('refuses unreadable or missing successful heads instead of overwriting with a new document', async () => {
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => { throw new Error('HTML response') } })
      .mockResolvedValueOnce(response({ revision: 2, kind: 'widget' }))
    expect(await loadWidget('widget', { fetch: fetchImpl })).toMatchObject({ ok: false, status: 502 })
    expect(await loadWidget('widget', { fetch: fetchImpl })).toMatchObject({ ok: false, status: 502 })
  })
  it('keeps edits made while saving and uses the returned head for the next save', async () => {
    let finishSave
    const fetchImpl = vi.fn()
      .mockImplementationOnce(() => new Promise((resolve) => { finishSave = resolve }))
      .mockResolvedValueOnce(response({ revision: 2, sha256: 'second' }))
    vi.stubGlobal('fetch', fetchImpl)
    render(<WidgetsView />); edit('Document ID', 'widget'); click('New'); click('Add text')
    edit('Text', 'First version'); click('Save'); edit('Text', 'Second version')
    finishSave(response({ revision: 1, sha256: 'first' }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Local edits'))
    expect(screen.getByLabelText('Text')).toHaveValue('Second version')
    click('Save')
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved revision 2'))
    const payload = JSON.parse(fetchImpl.mock.calls[1][1].body)
    expect(payload.expected_head).toBe(1)
    expect(payload.graph.formats.la.els.find((e) => e.id === 'text1').text).toBe('Second version')
  })
  it('loads missing widgets as new documents and saves with kind and expected_head', async () => {
    const fetchImpl = vi.fn().mockResolvedValueOnce(response({}, 404)).mockResolvedValueOnce(response({ revision: 3, sha256: 'abc' }))
    expect(await loadWidget('new-widget', { fetch: fetchImpl })).toMatchObject({ ok: true, doc: null, revision: null })
    const doc = createWidgetDoc()
    expect(await saveWidget('new-widget', doc, 2, { fetch: fetchImpl })).toMatchObject({ ok: true, revision: 3, sha256: 'abc' })
    expect(fetchImpl.mock.calls[0][0]).toBe('/api/lab/docs/new-widget/head')
    expect(JSON.parse(fetchImpl.mock.calls[1][1].body)).toEqual({ graph: doc, expected_head: 2, kind: 'widget' })
    expect(fetchImpl.mock.calls[1][1].credentials).toBe('same-origin')
  })
  it.each([409, 422, 413, 404, 503])('returns %s failures without retries and preserves pointers', async (status) => {
    const fetchImpl = vi.fn(async () => response({ detail: 'problem', pointer: '/formats/la/els/0' }, status))
    expect(await saveWidget('widget', createWidgetDoc(), null, { fetch: fetchImpl })).toMatchObject({ ok: false, status, pointer: '/formats/la/els/0' })
    expect(fetchImpl).toHaveBeenCalledTimes(1)
  })
  it('refuses invalid ids and bounds a stalled request', async () => {
    const fetchImpl = vi.fn(() => new Promise(() => {}))
    expect(await loadWidget('../wrong', { fetch: fetchImpl })).toMatchObject({ ok: false, status: 400 })
    expect(fetchImpl).not.toHaveBeenCalled()
    vi.useFakeTimers()
    const pending = loadWidget('widget', { fetch: fetchImpl, timeout: 10 })
    await vi.advanceTimersByTimeAsync(11)
    expect(await pending).toMatchObject({ ok: false, status: 0 })
    expect(fetchImpl.mock.calls[0][1].signal.aborted).toBe(true)
  })
  it('uses only registry-verified graph output types and posts bindings to the values route', async () => {
    const manifest = manifests[0]
    const graph = { blocks: [{ id: 'node1', registry: { id: manifest.id, version: manifest.version.semver, code_sha256: manifest.version.code_sha256 } }], outputs: { answer: { node: 'node1', port: 'result' } } }
    const fetchImpl = vi.fn(async () => response({ graph, kind: 'graph' }))
    expect(await loadGraphOutputs('strategy', { fetch: fetchImpl })).toMatchObject({ ok: true, outputs: [{ name: 'answer', type: 'scalar<number>' }] })
    graph.blocks[0].registry.code_sha256 = 'changed'
    expect(await loadGraphOutputs('strategy', { fetch: fetchImpl })).toMatchObject({ ok: false, status: 422 })
    await resolveValues('widget', [], { fetch: fetchImpl })
    expect(fetchImpl.mock.calls.at(-1)[0]).toBe('/api/lab/widgets/widget/values')
    expect(JSON.parse(fetchImpl.mock.calls.at(-1)[1].body)).toEqual({ bindings: [] })
  })
  it('shows loading, saved, saving, conflict retaining edits, reload and 422 validation pointer', async () => {
    let finishLoad, finishSave
    const doc = seed().doc
    const fetchImpl = vi.fn()
      .mockImplementationOnce(() => new Promise((resolve) => { finishLoad = resolve }))
      .mockImplementationOnce(() => new Promise((resolve) => { finishSave = resolve }))
      .mockResolvedValueOnce(response({ graph: doc, revision: 5, kind: 'widget' }))
      .mockResolvedValueOnce(response({ detail: 'bad_binding', pointer: '/formats/la/els/0/binding' }, 422))
    vi.stubGlobal('fetch', fetchImpl)
    render(<WidgetsView />); edit('Document ID', 'widget'); click('Load')
    expect(screen.getByRole('status')).toHaveTextContent('Loading')
    finishLoad(response({ graph: doc, revision: 4, sha256: 'sha', kind: 'widget' }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved revision 4'))
    fireEvent.click(within(screen.getByRole('list', { name: 'Text elements' })).getByRole('button', { name: 'Text 1: Your text' }))
    edit('Text', 'Keep my changes'); click('Save')
    expect(screen.getByRole('status')).toHaveTextContent('Saving')
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    finishSave(response({ detail: 'stale_expected_head' }, 409))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Conflict'))
    expect(screen.getByLabelText('Text')).toHaveValue('Keep my changes')
    expect(JSON.parse(fetchImpl.mock.calls[1][1].body).expected_head).toBe(4)
    click('Reload')
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved revision 5'))
    click('Save')
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('/formats/la/els/0/binding'))
    expect(screen.getByRole('status')).toHaveTextContent('Invalid')
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })
  it('shows successful save and error states and disables save for incomplete bindings', async () => {
    const fetchImpl = vi.fn().mockResolvedValueOnce(response({ revision: 1, sha256: 'sha' })).mockRejectedValueOnce(new Error('offline'))
    vi.stubGlobal('fetch', fetchImpl)
    render(<WidgetsView />); edit('Document ID', 'widget'); click('New')
    expect(screen.getByTestId('widget-surface').querySelectorAll('[data-element-id]')).toHaveLength(DESIGNER_DEFAULT_DOC.formats.la.els.length)
    click('Add text'); click('Save')
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved revision 1'))
    click('Save'); await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('request failed'))
    expect(screen.getByLabelText('Text')).toHaveValue('Your text')
    edit('Binding source', 'graph.output')
    expect(screen.getByRole('status')).toHaveTextContent('Invalid')
    expect(screen.getByRole('alert')).toHaveTextContent(`/formats/la/els/${DESIGNER_DEFAULT_DOC.formats.la.els.length}`)
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })
  it('shows the selected graph output type in the authenticated binding picker', async () => {
    const manifest = manifests[0]
    vi.stubGlobal('fetch', vi.fn(async () => response({ kind: 'graph', graph: { blocks: [{ id: 'node1', registry: { id: manifest.id, version: manifest.version.semver, code_sha256: manifest.version.code_sha256 } }], outputs: { answer: { node: 'node1', port: 'result' } } } })))
    render(<WidgetsView />); edit('Document ID', 'widget'); click('New'); click('Add text')
    edit('Binding source', 'graph.output'); edit('Graph document ID', 'strategy'); click('Load outputs')
    await screen.findByRole('option', { name: 'answer — scalar<number>' })
    edit('Graph output', 'answer')
    expect(screen.getByText('Output type: scalar<number>')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()
  })
})
