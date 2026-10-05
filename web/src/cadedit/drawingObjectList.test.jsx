// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import DrawingObjectList from './DrawingObjectList.jsx'
import CadEditSurface from './CadEditSurface.jsx'
import EngineSessionProvider, { useEngineSessionContext } from './EngineSessionProvider.jsx'
import EngineObjectBridge from './EngineObjectBridge.jsx'
import EngineRibbonClusters from './EngineRibbonClusters.jsx'
import DraftingRibbon from '../site/DraftingRibbon.jsx'
import { DrawingObjectsProvider, useDrawingObjects } from '../site/DrawingObjectsContext.jsx'
import { buildDrawingObjectIndex } from '../lib/drawingObjectIndex.js'

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers() })
const line = (id) => ({ id, type: 'LINE', layer: 'Panels', editable: true,
  vertices: [[0, 0, 0], [10, 10, 0]], aci: 256, linetype: 'ByLayer', lineweight: -1 })
const ENTITIES = [line('10'), line('11'), { ...line('12'), type: 'OTHER', editable: false }]
const graph = { drawingKey: 'engine:roof.dxf', graph: { panels: [
  { id: 'panel-b', kind: 'panel', name: 'Panel B', provenance: { source_handle: 'B' } },
] } }
class Worker {
  listeners = new Map()
  posted = []
  addEventListener(type, fn) { this.listeners.set(type, fn) }
  removeEventListener(type) { this.listeners.delete(type) }
  postMessage(message) { this.posted.push(message) }
  terminate() {}
  emit(data) { act(() => this.listeners.get('message')?.({ data })) }
}
function mount({ entities = ENTITIES, surface = false, context = true } = {}) {
  const worker = new Worker(), createWorker = () => worker, announce = vi.fn(), beforeEdit = vi.fn()
  const snapshot = { pose: { target: [100, 200, 0], zoom: 4 }, viewport: { minX: 20, maxX: 100, minY: 30, maxY: 150 } }
  const api = {
    frame: vi.fn(() => true), setFocusMarker: vi.fn(() => true), canSetFocusMarker: () => true,
    getDrawingScene: () => ({ ready: true, drawingKey: 'engine:roof.dxf', intake: null }),
    subscribeCamera: vi.fn((listener) => { listener(snapshot); return () => {} }),
    setView: vi.fn(() => true),
  }
  const viewerRef = { current: api }
  let engine, objects
  function Contents({ visible }) {
    engine = useEngineSessionContext()
    objects = useDrawingObjects()
    return <>
      {context && <EngineObjectBridge solarGraph={graph} />}
      {surface ? <>
        <DraftingRibbon clusters={[{ id: 'properties', label: 'Properties', kind: 'group', tools: [],
          extra: <div id="cockpit-properties-slot" className="ribbon-slot" /> }]}>
          <EngineRibbonClusters importOpen={false} onToggleImport={() => {}} panels={['properties']} />
        </DraftingRibbon>
        <CadEditSurface enabled viewerRef={viewerRef} />
      </> : <DrawingObjectList session={{ ...engine.session,
        entities: visible ? visible.map((id) => engine.session.entities.find((entity) => entity.id === id)).filter(Boolean) : engine.session.entities,
      }} announce={announce} />}
    </>
  }
  const tree = (visible) => {
    const content = <EngineSessionProvider createWorker={createWorker} onBeforeEdit={beforeEdit}>
      <Contents visible={visible} />
    </EngineSessionProvider>
    return context ? <DrawingObjectsProvider viewerRef={viewerRef}>{content}</DrawingObjectsProvider> : content
  }
  const mounted = render(tree())
  act(() => engine.session.actions.openBytes(new Uint8Array([0]), 'roof.dxf'))
  worker.emit({ type: 'documentLoaded', documentId: 'roof.dxf', entities, entityCount: entities.length, unsupported: [] })
  return { ...mounted, rerender: (visible) => mounted.rerender(tree(visible)), worker, api, announce, beforeEdit,
    engine: () => engine, objects: () => objects }
}
const row = (id) => document.querySelector('.drawing-object-list [data-object-id="' + id + '"]')
const control = (id, role, name) => within(row(id)).getByRole(role, { name })
const selectedRows = () => [...document.querySelectorAll('.drawing-object-list > li[data-selected="true"]')].map((item) => item.dataset.objectId)
// jsdom may lack PointerEvent; React reads pointerType off whatever native event arrives.
function pointer(node, type, { pointerType = 'touch', ...init } = {}) {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX: 0, clientY: 0, ...init })
  Object.defineProperty(event, 'pointerType', { value: pointerType })
  fireEvent(node, event)
}
function tap(node, init) {
  pointer(node, 'pointerdown', init); pointer(node, 'pointerup', init); fireEvent.click(node)
}
function openObjects() {
  const details = screen.getByText('Objects', { selector: 'summary' }).parentElement
  details.open = true
  fireEvent(details, new Event('toggle'))
  return details
}
function animationFrames() {
  const frames = new Map()
  let next = 0
  vi.stubGlobal('requestAnimationFrame', vi.fn((fn) => { frames.set(++next, fn); return next }))
  vi.stubGlobal('cancelAnimationFrame', vi.fn((id) => frames.delete(id)))
  return { frames, flush: () => act(() => {
    const callbacks = [...frames.values()]; frames.clear(); callbacks.forEach((fn) => fn())
  }) }
}

it('shows names, paths and geometry; Focus never selects and selection never frames', () => {
  const h = mount()
  expect(within(row('11')).getByText('Panel B', { exact: true })).toBeInTheDocument()
  expect(row('11')).toHaveTextContent('drawing / Layers / Panels / Panel B')
  expect(row('11')).toHaveTextContent('LINE on layer Panels · 2 vertices (0,0 → 10,10)')
  fireEvent.click(control('10', 'radio', /Select only/))
  expect(h.engine().session.selectedIds).toEqual(['10'])
  expect(h.api.frame).not.toHaveBeenCalled()
  fireEvent.click(control('11', 'button', 'Focus'), { shiftKey: true })
  expect(h.api.frame).toHaveBeenCalledTimes(1)
  expect(h.objects().focusId).toBe('h:B')
  expect(h.engine().session.selectedIds).toEqual(['10'])
  expect(within(row('10')).getByText('Selected', { exact: true })).toBeInTheDocument()
  expect(within(row('11')).getByText('Focused', { exact: true })).toBeInTheDocument()
  expect(h.announce).toHaveBeenLastCalledWith('Focused Panel B')
  fireEvent.click(control('11', 'radio', /Select only/))
  expect(h.engine().session.selectedIds).toEqual(['11'])
  expect(h.api.frame).toHaveBeenCalledTimes(1)
  expect(h.api.setView).not.toHaveBeenCalled()
})

it('S7 renders no checkbox column: no checkbox input and no Add to selection control', () => {
  mount()
  expect(document.querySelectorAll('.drawing-object-list input[type="checkbox"]')).toHaveLength(0)
  expect(screen.queryAllByRole('checkbox')).toHaveLength(0)
  expect(screen.queryByText('Add to selection')).toBeNull()
  expect(document.querySelectorAll('[data-object-action="selection"]')).toHaveLength(0)
})

it('S7 X toggles the row holding keyboard focus, never chorded, and explains a read-only row', () => {
  const h = mount(), toggle = vi.spyOn(h.engine().session.actions, 'selectToggle')
  const radio = control('10', 'radio', /Select only/)
  radio.focus()
  // A handled X is cancelled so no global shortcut or type-to-bar sees it.
  expect(fireEvent.keyDown(radio, { key: 'x' })).toBe(false)
  expect(h.engine().session.selectedIds).toEqual(['10'])
  expect(selectedRows()).toEqual(['10'])
  fireEvent.keyDown(radio, { key: 'X', shiftKey: true })
  expect(h.engine().session.selectedIds).toEqual([])
  fireEvent.keyDown(radio, { key: 'x' })
  const focus = control('11', 'button', 'Focus')
  focus.focus()
  fireEvent.keyDown(focus, { key: 'x' })
  expect(h.engine().session.selectedIds).toEqual(['10', '11'])
  for (const chord of [{ ctrlKey: true }, { metaKey: true }, { altKey: true }, { repeat: true }]) {
    expect(fireEvent.keyDown(focus, { key: 'x', ...chord })).toBe(true)
  }
  expect(fireEvent.keyDown(focus, { key: 'c' })).toBe(true)
  expect(toggle).toHaveBeenCalledTimes(4)
  expect(h.engine().session.selectedIds).toEqual(['10', '11'])
  fireEvent.keyDown(control('12', 'radio', /Select only/), { key: 'x' })
  expect(h.announce).toHaveBeenLastCalledWith('This object is read-only and cannot be selected for editing.')
  expect(h.engine().session.selectedIds).toEqual(['10', '11'])
  expect(h.api.frame).not.toHaveBeenCalled()
})

it.each(['ctrlKey', 'metaKey'])('S7 %s click adds or removes one row without replacing, and never toggles Focus', (key) => {
  const h = mount()
  fireEvent.click(control('10', 'radio', /Select only/))
  fireEvent.click(control('11', 'radio', /Select only/), { [key]: true })
  expect(h.engine().session.selectedIds).toEqual(['10', '11'])
  fireEvent.click(row('10'), { [key]: true })
  expect(h.engine().session.selectedIds).toEqual(['11'])
  fireEvent.click(row('10'), { [key]: true })
  expect(h.engine().session.selectedIds).toEqual(['11', '10'])
  fireEvent.click(control('10', 'button', 'Focus'), { [key]: true })
  expect(h.engine().session.selectedIds).toEqual(['11', '10'])
  fireEvent.click(row('12'), { [key]: true })
  expect(h.engine().session.selectedIds).toEqual(['11', '10'])
})

it('S7 Shift click selects a range from the anchor in the shown entity order, skipping read-only rows', () => {
  const h = mount({ entities: [line('10'), line('11'), { ...line('12'), editable: false }, line('13')] })
  fireEvent.click(control('13', 'radio', /Select only/))
  fireEvent.click(row('10'), { shiftKey: true })
  expect(h.engine().session.selectedIds).toEqual(['10', '11', '13'])
  // The anchor stays put: a second Shift click re-spans from row 13.
  fireEvent.click(control('11', 'radio', /Select only/), { shiftKey: true })
  expect(h.engine().session.selectedIds).toEqual(['11', '13'])
  // Ctrl or Cmd with Shift adds the span to what is already selected.
  fireEvent.click(control('10', 'radio', /Select only/))
  fireEvent.click(row('11'), { shiftKey: true, metaKey: true })
  expect(h.engine().session.selectedIds).toEqual(['10', '11'])
  fireEvent.click(control('13', 'radio', /Select only/))
  fireEvent.click(row('11'), { shiftKey: true, ctrlKey: true })
  expect(h.engine().session.selectedIds).toEqual(['13', '11'])
  // Order is the list's own session.entities order, not click order or id order.
  h.rerender(['13', '10', '11'])
  fireEvent.click(control('13', 'radio', /Select only/))
  fireEvent.click(row('10'), { shiftKey: true })
  expect(h.engine().session.selectedIds).toEqual(['13', '10'])
  expect(h.api.frame).not.toHaveBeenCalled()
})

it('S7 Shift click with no anchor spans from the newest selected row, else selects the row alone', () => {
  const h = mount({ entities: [line('10'), line('11'), line('13')] })
  fireEvent.click(row('11'), { shiftKey: true })
  expect(h.engine().session.selectedIds).toEqual(['11'])
  h.unmount()
  const h2 = mount({ entities: [line('10'), line('11'), line('13')] })
  act(() => h2.engine().session.actions.selectReplace(['13', '10']))
  fireEvent.click(row('11'), { shiftKey: true })
  expect(h2.engine().session.selectedIds).toEqual(['10', '11'])
})

it('S7 a 500 ms touch press enters selection, then taps toggle until the selection is empty', () => {
  const h = mount()
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  pointer(row('11'), 'pointerdown')
  act(() => { vi.advanceTimersByTime(499) })
  expect(h.engine().session.selectedIds).toEqual([])
  act(() => { vi.advanceTimersByTime(1) })
  expect(h.engine().session.selectedIds).toEqual(['11'])
  // The release that ends the press is not a second tap.
  pointer(row('11'), 'pointerup'); fireEvent.click(row('11'))
  expect(h.engine().session.selectedIds).toEqual(['11'])
  tap(row('10'))
  expect(h.engine().session.selectedIds).toEqual(['11', '10'])
  // A tap on a row's radio toggles too, never replaces, while selecting.
  tap(control('11', 'radio', /Select only/))
  expect(h.engine().session.selectedIds).toEqual(['10'])
  tap(control('10', 'button', 'Focus'))
  expect(h.engine().session.selectedIds).toEqual(['10'])
  tap(row('10'))
  expect(h.engine().session.selectedIds).toEqual([])
  // Empty again: a plain tap on a radio selects only that row.
  tap(control('11', 'radio', /Select only/))
  expect(h.engine().session.selectedIds).toEqual(['11'])
  // A mouse press never starts one, a drift past the slop cancels one, and a
  // read-only row explains itself instead of joining.
  pointer(row('10'), 'pointerdown', { pointerType: 'mouse' })
  act(() => { vi.advanceTimersByTime(600) })
  pointer(row('10'), 'pointerdown')
  pointer(row('10'), 'pointermove', { clientX: 20, clientY: 0 })
  act(() => { vi.advanceTimersByTime(600) })
  expect(h.engine().session.selectedIds).toEqual(['11'])
  pointer(row('12'), 'pointerdown')
  act(() => { vi.advanceTimersByTime(500) })
  expect(h.announce).toHaveBeenLastCalledWith('This object is read-only and cannot be selected for editing.')
  expect(h.engine().session.selectedIds).toEqual(['11'])
  // A press still pending at unmount never fires.
  const toggle = vi.spyOn(h.engine().session.actions, 'selectToggle')
  pointer(row('10'), 'pointerdown')
  h.unmount()
  act(() => { vi.advanceTimersByTime(600) })
  expect(toggle).not.toHaveBeenCalled()
})

it('keeps read-only controls focusable and explained while Focus still works', () => {
  const h = mount(), focus = control('12', 'button', 'Focus')
  expect(row('12')).toHaveTextContent('Read-only')
  for (const role of ['radio']) {
    const input = within(row('12')).getByRole(role)
    input.focus()
    expect(input).toHaveFocus()
    expect(input).toHaveAttribute('aria-disabled', 'true')
    expect(input).toHaveAccessibleDescription('This object is read-only and cannot be selected for editing.')
    fireEvent.click(input)
    expect(input).not.toBeChecked()
  }
  focus.focus(); expect(focus).toHaveFocus()
  fireEvent.click(focus)
  expect(h.objects().focusId).toBe('h:C')
  expect(h.engine().session.selectedIds).toEqual([])
})

it('preserves selected identities through filtering and reorder, and keyboard focus through a reparse', () => {
  const h = mount()
  fireEvent.click(row('10'), { ctrlKey: true })
  fireEvent.click(row('11'), { metaKey: true })
  const focused = control('11', 'button', 'Focus')
  focused.focus()
  h.rerender(['12', '11', '10'])
  expect(focused).toHaveFocus()
  h.rerender(['11'])
  expect(focused).toHaveFocus()
  expect(h.engine().session.selectedIds).toEqual(['10', '11'])
  h.worker.emit({ type: 'editApplied', op: 'move', ok: true,
    entities: [ENTITIES[2], { ...line('11'), vertices: [[4, 4, 0], [14, 14, 0]] }],
    entityCount: 2, bytes: new Uint8Array([1]), byteLength: 1 })
  expect(control('11', 'button', 'Focus')).toBe(focused)
  expect(focused).toHaveFocus()
  expect(h.engine().session.selectedIds).toEqual(['11'])
  expect(row('11')).toHaveAttribute('data-selected', 'true')
  expect(control('11', 'radio', /Select only/)).toBeChecked()
})

it('renders and selects every entity without drawing context', () => {
  const h = mount({ context: false })
  expect(screen.getAllByRole('radio')).toHaveLength(3)
  fireEvent.click(control('11', 'radio', /LINE on layer Panels/))
  expect(h.engine().session.selectedIds).toEqual(['11'])
  const focus = control('11', 'button', 'Focus')
  expect(focus).toHaveAccessibleDescription('Focus is unavailable because this object has no navigation record for this drawing.')
  fireEvent.click(focus)
  expect(h.api.frame).not.toHaveBeenCalled()
  expect(h.announce).toHaveBeenLastCalledWith('Focus is unavailable because this object has no navigation record for this drawing.')
})

it('ignores another drawing scope and truthfully announces a rejected frame', () => {
  const h = mount()
  h.api.frame.mockReturnValue(false)
  fireEvent.click(control('11', 'button', 'Focus'))
  expect(h.announce).toHaveBeenLastCalledWith('Focus unavailable: frame refused')
  expect(h.objects().focusId).toBeNull()
  act(() => h.objects().publish('engine', { index: buildDrawingObjectIndex({ drawingKey: 'engine:other.dxf', entities: ENTITIES }) }))
  expect(row('11')).not.toHaveTextContent('Panel B')
  expect(control('11', 'button', 'Focus')).toHaveAttribute('aria-disabled', 'true')
  fireEvent.click(control('11', 'radio', /Select only/))
  expect(h.engine().session.selectedIds).toEqual(['11'])
})

it('retains rows missing from the index and explains records without bounds', () => {
  const h = mount({ entities: [line('10'), { ...line('11'), vertices: [] }] })
  const focus = control('11', 'button', 'Focus')
  expect(focus).toHaveAccessibleDescription('Focus is unavailable because this object has no drawing bounds.')
  fireEvent.click(focus)
  expect(h.api.frame).not.toHaveBeenCalled()
  act(() => h.objects().publish('engine', { index: buildDrawingObjectIndex({ drawingKey: 'engine:roof.dxf', entities: [] }) }))
  expect(screen.getAllByRole('radio')).toHaveLength(2)
  expect(control('10', 'button', 'Focus')).toHaveAccessibleDescription('Focus is unavailable because this object has no navigation record for this drawing.')
  fireEvent.click(control('10', 'radio', /Select only/))
  expect(h.engine().session.selectedIds).toEqual(['10'])
})

it('resolves adjacent u64 decimal strings and BigInts without Number rounding', () => {
  const entities = [line('9007199254740992'), line('9007199254740993')]
  const h = mount({ entities })
  fireEvent.click(control('9007199254740993', 'button', 'Focus'))
  expect(h.objects().focusId).toBe('h:20000000000001')
  expect(control('9007199254740992', 'radio', /Select only/).value).toBe('9007199254740992')
  h.unmount()
  const toggle = vi.fn(), replace = vi.fn()
  const bigintEntities = entities.map((entity) => ({ ...entity, id: BigInt(entity.id) }))
  function Publisher() {
    const objects = useDrawingObjects()
    return <button onClick={() => objects.publish('engine', {
      index: buildDrawingObjectIndex({ drawingKey: 'engine:roof.dxf', entities: bigintEntities }),
    })}>Publish</button>
  }
  render(<DrawingObjectsProvider viewerRef={{ current: h.api }}>
    <Publisher /><DrawingObjectList session={{ documentId: 'roof.dxf', entities: bigintEntities, selectedIds: [],
      actions: { selectToggle: toggle, selectReplace: replace } }} />
  </DrawingObjectsProvider>)
  fireEvent.click(screen.getByRole('button', { name: 'Publish' }))
  expect(row('9007199254740993')).toHaveTextContent('LINE · handle 20000000000001')
  fireEvent.click(row('9007199254740993'), { ctrlKey: true })
  expect(toggle).toHaveBeenCalledWith(9007199254740993n)
  fireEvent.click(row('9007199254740992'), { shiftKey: true })
  expect(replace).toHaveBeenLastCalledWith([9007199254740992n, 9007199254740993n])
})

it('keeps all 2,345 rooftop rows and their native controls reachable', () => {
  const entities = Array.from({ length: 2345 }, (_, i) => line(String(i + 1)))
  mount({ entities })
  const rows = document.querySelectorAll('.drawing-object-list > li')
  expect(rows).toHaveLength(2345)
  for (const item of rows) {
    const radio = item.querySelector('input[type="radio"]'), focus = item.querySelector('button')
    expect(item.querySelector('input[type="checkbox"]')).toBeNull()
    expect(radio.disabled).toBe(false)
    expect(radio.tabIndex).toBe(0)
    expect(focus.tabIndex).toBe(0)
  }
  const last = rows[2344].querySelector('button')
  last.focus(); expect(last).toHaveFocus()
  expect(rows[2344].querySelector('input[type="radio"]').value).toBe('2345')
}, 30_000)

it('keeps Objects outside the workbench and repeats messages in one permanent navigation region', () => {
  const clock = animationFrames(), h = mount({ surface: true }), panel = openObjects()
  expect(panel.closest('.cad-edit-workbench')).toBeNull()
  expect(panel).toHaveAttribute('data-nav-objects')
  const live = screen.getByTestId('drawing-navigation-live')
  expect(live.textContent).toBe('')
  const focus = control('11', 'button', 'Focus')
  fireEvent.click(focus); clock.flush()
  expect(live.textContent).toBe('Focused Panel B')
  fireEvent.click(focus)
  expect(live.textContent).toBe('')
  clock.flush()
  expect(live.textContent).toBe('Focused Panel B')
  fireEvent.click(screen.getByRole('button', { name: 'North' })); clock.flush()
  expect(live.textContent).toBe('View centre 100.00, 260.00 drawing units')
  fireEvent.click(screen.getByRole('button', { name: 'South' })); clock.flush()
  expect(live.textContent).toBe('View centre 100.00, 200.00 drawing units')
  fireEvent.click(screen.getByRole('button', { name: 'North' }))
  expect(live.textContent).toBe('')
  clock.flush()
  expect(live.textContent).toBe('View centre 100.00, 260.00 drawing units')
  panel.open = false; fireEvent(panel, new Event('toggle'))
  expect(screen.getByTestId('drawing-navigation-live')).toBe(live)
  openObjects()
  fireEvent.click(control('11', 'button', 'Focus'))
  expect(clock.frames.size).toBe(1)
  h.unmount()
  expect(clock.frames.size).toBe(0)
})

it('keeps the legacy and navigation radio groups checked consistently', () => {
  mount({ surface: true })
  openObjects()
  const legacy = within(screen.getByTestId('cad-edit-entity-list')).getAllByRole('radio')
  fireEvent.click(control('10', 'radio', /Select only/))
  expect(control('10', 'radio', /Select only/)).toBeChecked()
  expect(legacy[0]).toBeChecked()
  fireEvent.click(legacy[1])
  expect(legacy[1]).toBeChecked()
  expect(control('11', 'radio', /Select only/)).toBeChecked()
  expect(control('10', 'radio', /Select only/)).not.toBeChecked()
})

it.each([false, true])('stages on A, blurs to Focus B, pans, then applies only to A (buffered keyboard walk: %s)', (buffered) => {
  const clock = animationFrames(), h = mount({ surface: true })
  openObjects()
  fireEvent.click(control('10', 'radio', /Select only/))
  const color = screen.getByLabelText(/^Color/)
  color.focus()
  if (buffered) fireEvent.keyDown(color, { key: 'ArrowDown' })
  fireEvent.change(color, { target: { value: 'red' } })
  if (buffered) expect(h.engine().pending).toBeNull()
  else expect(h.engine().pending).toMatchObject({ targetId: '10', value: 'red' })
  const beforeFocus = h.engine().pending
  const focus = control('11', 'button', 'Focus')
  // Pointer activation blurs the property combo before clicking Focus.
  fireEvent.pointerDown(focus)
  act(() => focus.focus())
  fireEvent.pointerUp(focus); fireEvent.click(focus)
  expect(h.engine().pending).toMatchObject({ targetId: '10', value: 'red' })
  if (!buffered) expect(h.engine().pending).toBe(beforeFocus)
  const pending = h.engine().pending
  fireEvent.click(screen.getByRole('button', { name: 'East' }))
  clock.flush()
  expect(h.engine().pending).toBe(pending)
  expect(h.engine().session.selectedIds).toEqual(['10'])
  expect(h.objects().focusId).toBe('h:B')
  expect(h.worker.posted.filter((message) => message.type === 'applyEdit')).toEqual([])
  expect(h.beforeEdit).not.toHaveBeenCalled()
  fireEvent.click(screen.getByTestId('property-apply'))
  expect(h.worker.posted.filter((message) => message.type === 'applyEdit')).toEqual([
    { type: 'applyEdit', op: 'setColor', payload: { entityId: '10', aci: 1 } },
  ])
  expect(h.beforeEdit).toHaveBeenCalledTimes(1)
})
