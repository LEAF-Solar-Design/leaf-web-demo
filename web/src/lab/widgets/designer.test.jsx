import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import WidgetsView from './WidgetsView.jsx'
import { FORMAT_NAMES } from './WidgetDesigner.jsx'
import { FORMATS, PRESETS } from './widgetCatalog.js'
import { validateWidgetDoc } from './widgetDoc.js'
import { createEditorState, createWidgetDoc, HANDLES, snap1, snapTargets, supportsPreset, transformElement, widgetEditorReducer as reduce } from './useWidgetEditor.js'
import { loadGraphOutputs, loadWidget, resolveValues, saveWidget } from './widgetsClient.js'
import { manifests } from '../canvas/registry.js'

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers() })
const click = (name) => fireEvent.click(screen.getByRole('button', { name, exact: true }))
const edit = (label, value) => fireEvent.change(screen.getByLabelText(label), { target: { value } })
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
  it('ends a drag before switching formats so later cancellation cannot restore the old format', () => {
    let state = seed()
    state = reduce(state, { type: 'begin' })
    state = reduce(state, { type: 'move', dx: 8, dy: 8, alt: true })
    const moved = state.doc
    state = reduce(state, { type: 'format', format: 'homeS' })
    expect(state.transaction).toBeNull()
    state = reduce(state, { type: 'cancel' })
    expect(state.doc.active).toBe('homeS')
    expect(reduce(state, { type: 'undo' }).doc).toEqual(moved)
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
    state = reduce(state, { type: 'visibility' }); expect(selected(state).visible).toBe(false)
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
    expect(snap1([12, 16], [18, 10])).toEqual({ delta: 2, target: 18 })
    state = reduce(state, { type: 'visibility', id: 'text1' })
    expect(snapTargets(state.doc, 'text2').xs).not.toContain(state.doc.formats.diExp.els[0].x)
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
    expect(screen.getByRole('region', { name: 'Preview' }).querySelectorAll('[data-element-id]')).toHaveLength(0)
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

describe('widget store client and states', () => {
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
    expect(payload.graph.formats.la.els[0].text).toBe('Second version')
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
    expect(screen.getByText('Empty. Add an element to start your preview.')).toBeInTheDocument()
    click('Add text'); click('Save')
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved revision 1'))
    click('Save'); await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('request failed'))
    expect(screen.getByLabelText('Text')).toHaveValue('Your text')
    edit('Binding source', 'graph.output')
    expect(screen.getByRole('status')).toHaveTextContent('Invalid')
    expect(screen.getByRole('alert')).toHaveTextContent('/formats/la/els/0')
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
