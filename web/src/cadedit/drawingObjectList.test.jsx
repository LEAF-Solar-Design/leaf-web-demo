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

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
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

it('uses one native checkbox activation to toggle, including clicks through its label', () => {
  const h = mount(), toggle = vi.spyOn(h.engine().session.actions, 'selectToggle')
  const checkbox = control('10', 'checkbox', 'Add to selection')
  checkbox.focus()
  // jsdom does not synthesize the browser's Space default action. Key events
  // alone must not toggle; the native click following keyup toggles once.
  fireEvent.keyDown(checkbox, { key: ' ' }); fireEvent.keyUp(checkbox, { key: ' ' })
  expect(toggle).not.toHaveBeenCalled()
  fireEvent.click(checkbox, { detail: 0 })
  expect(toggle).toHaveBeenCalledTimes(1)
  expect(toggle).toHaveBeenLastCalledWith('10')
  expect(checkbox).toBeChecked()
  fireEvent.click(checkbox.closest('label'), { ctrlKey: true })
  expect(toggle).toHaveBeenCalledTimes(2)
  expect(checkbox).not.toBeChecked()
  expect(h.api.frame).not.toHaveBeenCalled()
})

it.each(['shiftKey', 'ctrlKey', 'metaKey'])('preserves %s radio and row toggles without toggling Focus', (key) => {
  const h = mount()
  fireEvent.click(control('10', 'radio', /Select only/))
  fireEvent.click(control('11', 'radio', /Select only/), { [key]: true })
  expect(h.engine().session.selectedIds).toEqual(['10', '11'])
  fireEvent.click(row('10'), { [key]: true })
  expect(h.engine().session.selectedIds).toEqual(['11'])
  fireEvent.click(control('10', 'button', 'Focus'), { [key]: true })
  expect(h.engine().session.selectedIds).toEqual(['11'])
})

it('keeps read-only controls focusable and explained while Focus still works', () => {
  const h = mount(), focus = control('12', 'button', 'Focus')
  expect(row('12')).toHaveTextContent('Read-only')
  for (const role of ['radio', 'checkbox']) {
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
  fireEvent.click(control('10', 'checkbox', 'Add to selection'))
  fireEvent.click(control('11', 'checkbox', 'Add to selection'))
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
  expect(control('11', 'checkbox', 'Add to selection')).toBeChecked()
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
  const toggle = vi.fn()
  const bigintEntities = entities.map((entity) => ({ ...entity, id: BigInt(entity.id) }))
  function Publisher() {
    const objects = useDrawingObjects()
    return <button onClick={() => objects.publish('engine', {
      index: buildDrawingObjectIndex({ drawingKey: 'engine:roof.dxf', entities: bigintEntities }),
    })}>Publish</button>
  }
  render(<DrawingObjectsProvider viewerRef={{ current: h.api }}>
    <Publisher /><DrawingObjectList session={{ documentId: 'roof.dxf', entities: bigintEntities, selectedIds: [],
      actions: { selectToggle: toggle, selectReplace: vi.fn() } }} />
  </DrawingObjectsProvider>)
  fireEvent.click(screen.getByRole('button', { name: 'Publish' }))
  expect(row('9007199254740993')).toHaveTextContent('LINE · handle 20000000000001')
  fireEvent.click(control('9007199254740993', 'checkbox', 'Add to selection'))
  expect(toggle).toHaveBeenCalledWith(9007199254740993n)
})

it('keeps all 2,345 rooftop rows and their native controls reachable', () => {
  const entities = Array.from({ length: 2345 }, (_, i) => line(String(i + 1)))
  mount({ entities })
  const rows = document.querySelectorAll('.drawing-object-list > li')
  expect(rows).toHaveLength(2345)
  for (const item of rows) {
    const checkbox = item.querySelector('input[type="checkbox"]'), focus = item.querySelector('button')
    expect(checkbox.disabled).toBe(false)
    expect(checkbox.tabIndex).toBe(0)
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
