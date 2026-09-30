import { useRef, useState } from 'react'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import DrawingNavigationTools from './DrawingNavigationTools.jsx'
import { DrawingObjectsProvider, useDrawingObjects } from './DrawingObjectsContext.jsx'
import { StudioGroundContext } from './studioGround.js'
import { ViewCluster, useViewNavigation } from './DrawingCockpit.jsx'
import { createViewHistory } from '../lib/viewHistory.js'
import { buildDrawingObjectIndex } from '../lib/drawingObjectIndex.js'
import EngineSessionProvider, { useEngineSessionContext } from '../cadedit/EngineSessionProvider.jsx'
import EngineDocumentView from '../cadedit/EngineDocumentView.jsx'
import EngineObjectBridge from '../cadedit/EngineObjectBridge.jsx'

const surface = vi.hoisted(() => ({ contract: { ground: 'drawing' } }))
vi.mock('./SurfaceFrame.jsx', () => ({ useSurfaceFrame: () => surface }))
afterEach(() => { cleanup(); surface.contract.ground = 'drawing' })

class Worker {
  constructor() { this.listeners = new Map(); this.posted = [] }
  addEventListener(type, fn) { this.listeners.set(type, fn) }
  removeEventListener(type) { this.listeners.delete(type) }
  postMessage(message) { this.posted.push(message) }
  terminate() {}
  emit(data) { act(() => this.listeners.get('message')?.({ data })) }
}
const line = (id, layer = 'Panels') => ({ id, type: 'LINE', layer, editable: true,
  vertices: [[Number(id), 0, 0], [Number(id) + 5, 10, 0]], aci: 256, linetype: 'ByLayer', lineweight: -1 })
const entities = [line('42'), line('43'), line('44', 'Roof'), { id: '99', type: 'MLEADER', layer: 'Panels' }]
const solarGraph = { drawingKey: 'engine:guest.dxf', graph: {
  frames: [{ id: 'frame-north', kind: 'frame', name: 'North frame', panel_refs: ['panel-a', 'panel-b'] }],
  panels: [
    { id: 'panel-a', kind: 'panel', name: 'Module north', provenance: { source_handle: '2A' } },
    { id: 'panel-b', kind: 'panel', name: 'Module south', provenance: { source_handle: '2B' } },
  ],
} }
const initialPose = () => ({ target: [5, 6, 0], position: [5, 6, 100], zoom: 4, worldPerPixel: 0.25 })

function setup() {
  const worker = new Worker(), history = createViewHistory(), probe = {}, scene = {}
  let pose = initialPose(), frustum = 1
  const viewer = {
    getDrawingScene: () => scene,
    applyVersion: vi.fn((intake) => Object.assign(scene, { ready: !!intake, drawingKey: intake && `engine:${intake.documentId}`, intake })),
    canSetFocusMarker: vi.fn(() => true), setFocusMarker: vi.fn(() => true),
    getPose: () => pose,
    frame: vi.fn((bounds) => {
      // Mutate the old target too: Find must capture before focus touches it.
      pose.target[0] = bounds.minX
      pose = { ...pose, target: [bounds.minX, bounds.minY, 0], zoom: 2, worldPerPixel: frustum / 2 }
      return true
    }),
    setView: vi.fn((view) => {
      if (view === 'home') pose = { ...pose, target: [0, 0, 0], zoom: 1, worldPerPixel: frustum }
      else pose = { ...pose, target: [view.center.x, view.center.y, 0], zoom: view.zoom, worldPerPixel: frustum / view.zoom }
      return true
    }),
  }
  const viewerRef = { current: viewer }, createWorker = () => worker
  function Probe() { probe.objects = useDrawingObjects(); probe.engine = useEngineSessionContext(); return null }
  function Harness() {
    const source = useRef(null)
    const [intake, setIntake] = useState(null), [size, setSize] = useState(0)
    const [selectedHandle, setSelectedHandle] = useState(null)
    const [visibleLayers, setVisibleLayers] = useState({ Panels: true, Roof: false })
    const nav = useViewNavigation({ viewerRef, history, setHistorySize: setSize, navigationSource: source,
      selectedHandle, setSelectedHandle, selectedLayer: selectedHandle === '2C' ? 'Roof' : 'Panels',
      visibleLayers, setVisibleLayers, intake })
    Object.assign(probe, { nav, source, size, visibleLayers, setVisibleLayers, selectedHandle })
    return <StudioGroundContext.Provider value={{}}><DrawingObjectsProvider viewerRef={viewerRef}>
      <EngineSessionProvider createWorker={createWorker}>
        <Probe />
        <EngineDocumentView viewerRef={viewerRef} onShown={setIntake} selectedHandle={selectedHandle} onSelectedHandleChange={setSelectedHandle} />
        <EngineObjectBridge intake={intake} solarGraph={solarGraph} />
        <DrawingNavigationTools viewerRef={viewerRef} navigationSourceRef={source} navigation={nav} />
        <ViewCluster viewerRef={viewerRef} canBack={size > 0} onBack={nav.back} onUp={nav.up} onFit={nav.fit} announcement={nav.announcement} />
      </EngineSessionProvider>
    </DrawingObjectsProvider></StudioGroundContext.Provider>
  }
  const mounted = render(<Harness />)
  const load = (name = 'guest.dxf') => {
    act(() => probe.engine.session.actions.openBytes(new Uint8Array([0]), name))
    worker.emit({ type: 'documentLoaded', documentId: name, entities, entityCount: entities.length, unsupported: [] })
  }
  load()
  return { probe, worker, viewer, viewerRef, history, scene, load, ...mounted,
    resize: () => { frustum = 3; pose = { ...pose, worldPerPixel: frustum / pose.zoom } } }
}
const input = () => screen.getByRole('combobox', { name: 'Find in drawing' })
const type = (query) => fireEvent.change(input(), { target: { value: query } })
const enter = () => fireEvent.keyDown(input(), { key: 'Enter' })
const find = (query) => { type(query); enter() }
const back = () => fireEvent.click(screen.getByRole('button', { name: 'Back to the previous view' }))
const up = () => fireEvent.click(screen.getByRole('button', { name: 'Up one level' }))
const selectThree = (h) => act(() => h.probe.engine.session.actions.selectReplace(['42', '43', '44']))

it.each(['Module north', 'panel-a', '2A', 'zoom to Module north', 'go to 2A'])('finds %s on the guest engine drawing without console intake or selecting', (query) => {
  const h = setup()
  selectThree(h)
  h.viewer.frame.mockClear()
  find(query)
  expect(h.probe.objects.focusId).toBe('h:2A')
  expect(h.probe.engine.session.selectedIds).toEqual(['42', '43', '44'])
  expect(h.viewer.frame).toHaveBeenCalledTimes(1)
  expect(input().value).toBe(query)
  expect(h.history.peek().pose.target).toEqual([5, 6, 0])
  const label = screen.getByText('Focus: Module north')
  expect(document.getElementById(label.getAttribute('aria-describedby')).textContent).toBe(h.probe.objects.index.byId.get('h:2A').path)
  fireEvent.click(screen.getByRole('button', { name: 'Clear focus' }))
  expect(h.probe.objects.focusId).toBeNull()
  expect(h.probe.engine.session.selectedIds).toEqual(['42', '43', '44'])
})

it('offers full paths, Arrow keys and Enter; Escape closes without moving or clearing query', () => {
  const h = setup()
  find('Module')
  const options = screen.getAllByRole('option')
  expect(options.map((option) => option.textContent)).toEqual([
    'drawing / North frame / Module north', 'drawing / North frame / Module south',
  ])
  expect(options[0].getAttribute('aria-selected')).toBe('true')
  fireEvent.keyDown(input(), { key: 'ArrowDown' })
  expect(options[1].getAttribute('aria-selected')).toBe('true')
  fireEvent.keyDown(input(), { key: 'ArrowUp' })
  expect(options[0].getAttribute('aria-selected')).toBe('true')
  fireEvent.keyDown(input(), { key: 'Escape' })
  expect(screen.queryByRole('listbox')).toBeNull()
  expect(input().value).toBe('Module')
  expect(h.viewer.frame).not.toHaveBeenCalled()
  expect(h.history.size()).toBe(0)
  enter()
  fireEvent.keyDown(input(), { key: 'ArrowDown' }); enter()
  expect(h.probe.objects.focusId).toBe('h:2B')
  expect(input().value).toBe('Module')
})

it.each(['missing', 'hidden', 'no bounds', 'viewer not ready', "scene not showing this index's drawing", 'frame refused'])('a %s jump preserves camera, focus, selection and history', (reason) => {
  const h = setup()
  selectThree(h); find('2A')
  const pose = { ...h.viewer.getPose(), target: [...h.viewer.getPose().target] }
  h.viewer.frame.mockClear(); h.viewer.setFocusMarker.mockClear()
  let query = '2B', message = reason
  if (reason === 'missing') { query = 'absent object'; message = 'No matching object in this drawing.' }
  if (reason === 'hidden') { act(() => h.probe.setVisibleLayers({ Panels: false })); message = 'This object is hidden by its layers.' }
  if (reason === 'no bounds') query = '63'
  if (reason === 'viewer not ready') h.scene.ready = false
  if (reason === "scene not showing this index's drawing") h.scene.intake = {}
  if (reason === 'frame refused') h.viewer.frame.mockReturnValue(false)
  find(query)
  expect(screen.getByText(message)).toBeTruthy()
  expect(h.probe.objects.focusId).toBe('h:2A')
  expect(h.probe.engine.session.selectedIds).toEqual(['42', '43', '44'])
  expect(h.history.size()).toBe(1)
  expect(h.viewer.getPose()).toEqual(pose)
  expect(h.viewer.setFocusMarker).not.toHaveBeenCalled()
  expect(h.viewer.frame).toHaveBeenCalledTimes(reason === 'frame refused' ? 1 : 0)
})

it('Back restores focus, three hex handles as engine ids, query, layers and scale after resize', () => {
  const h = setup()
  selectThree(h); find('2A')
  const saved = { ...h.viewer.getPose(), target: [...h.viewer.getPose().target] }
  type('go to Module south'); enter()
  act(() => { h.probe.engine.session.actions.select('44'); h.probe.setVisibleLayers({ Panels: false, Roof: true }) })
  h.resize()
  type('later query')
  h.viewer.frame.mockImplementationOnce(() => {
    expect(h.probe.visibleLayers).toEqual({ Panels: true, Roof: false })
    h.viewer.setView({ center: { x: 100, y: 200 }, zoom: 2 })
    return true
  })
  back()
  expect(h.probe.objects.focusId).toBe('h:2A')
  expect(h.probe.engine.session.selectedIds).toEqual(['42', '43', '44'])
  expect(input().value).toBe('go to Module south')
  expect(h.viewer.getPose().target).toEqual(saved.target)
  expect(h.viewer.getPose().worldPerPixel).toBe(saved.worldPerPixel)
  expect(h.viewer.setView).toHaveBeenLastCalledWith({ center: { x: saved.target[0], y: saved.target[1] }, zoom: 3 / saved.worldPerPixel })
})

it('Back clears focus and selection when the saved snapshot has neither', () => {
  const h = setup()
  find('2A')
  act(() => h.probe.engine.session.actions.select('42'))
  back()
  expect(h.probe.objects.focusId).toBeNull()
  expect(h.probe.engine.session.selectedIds).toEqual([])
  expect(h.viewer.setFocusMarker).toHaveBeenLastCalledWith(null)
})

it('Up focuses the physical parent and the layer fallback while keeping edit targets', () => {
  const h = setup()
  selectThree(h); find('2A'); up()
  expect(h.probe.objects.focusId).toBe('g:frame-north')
  expect(h.probe.engine.session.selectedIds).toEqual(['42', '43', '44'])
  act(() => h.probe.setVisibleLayers({ Panels: true, Roof: true }))
  find('2C'); up()
  expect(h.probe.objects.focusId).toBe('layer:Roof')
  expect(h.probe.engine.session.selectedIds).toEqual(['42', '43', '44'])
})

it('rejects an entirely hidden parent before framing or pushing history', async () => {
  const h = setup()
  selectThree(h); find('2A')
  act(() => h.probe.setVisibleLayers({ Panels: false, Roof: true }))
  h.viewer.frame.mockClear()
  up()
  await act(async () => { await new Promise(requestAnimationFrame) })
  expect(screen.getByTestId('cockpit-view-live').textContent).toBe('This object is hidden by its layers.')
  expect(h.viewer.frame).not.toHaveBeenCalled()
  expect(h.history.size()).toBe(1)
  expect(h.probe.objects.focusId).toBe('h:2A')
  expect(h.probe.engine.session.selectedIds).toEqual(['42', '43', '44'])
})

it('a staged property target and value survive Find, Back, Up and Escape in Find', () => {
  const h = setup()
  selectThree(h)
  act(() => h.probe.engine.setPending({ op: 'setColor', value: 'red', targetId: '42', label: 'Module north' }))
  const pending = h.probe.engine.pending
  expect(pending).not.toBeNull()
  find('2A'); find('2B')
  act(() => h.probe.engine.session.actions.select('44'))
  back(); up()
  find('Module'); fireEvent.keyDown(input(), { key: 'Escape' })
  expect(h.probe.engine.pending).toBe(pending)
  expect(h.probe.engine.pending).toMatchObject({ targetId: '42', value: 'red' })
  expect(h.worker.posted.filter((message) => message.type === 'applyEdit')).toEqual([])
  expect(h.probe.engine.session.selectedIds).toEqual(['42', '43', '44'])
})

it('discards drawing-scoped snapshots when a new drawing reuses a handle', () => {
  const h = setup()
  selectThree(h); find('2A')
  expect(h.history.size()).toBe(1)
  h.load('other.dxf')
  expect(h.probe.objects.index.byHandle.has('2A')).toBe(true)
  expect(h.history.size()).toBe(0)
  expect(input().value).toBe('')
  h.viewer.frame.mockClear(); h.viewer.setView.mockClear()
  back()
  expect(h.viewer.frame).not.toHaveBeenCalled()
  expect(h.viewer.setView).not.toHaveBeenCalled()
  expect(h.probe.engine.session.selectedIds).toEqual([])
})

it('reports truncated results honestly and registers only the stable Find band as an occluder', () => {
  const h = setup()
  const intake = { polylines: Array.from({ length: 60 }, (_, i) => ({ handle: `X${i}`, layer: 'Batch', pts: [[i, 0], [i + 1, 1]] })) }
  const index = buildDrawingObjectIndex({ drawingKey: 'engine:guest.dxf', intake })
  act(() => h.probe.objects.publish('engine', { index }))
  find('LWPOLYLINE')
  expect(screen.getAllByRole('option')).toHaveLength(50)
  expect(screen.getByText('More matches; refine your search')).toBeTruthy()
  expect(input().closest('[data-nav-find]')).toBeTruthy()
  expect(screen.getByRole('listbox').closest('[data-nav-find]')).toBeNull()
  expect(h.viewer.frame).not.toHaveBeenCalled()
})

it('hides Find off the drawing surface and unregisters the source on unmount', () => {
  const h = setup(), source = h.probe.source
  expect(source.current).toBeTruthy()
  surface.contract.ground = 'board'
  act(() => h.probe.setVisibleLayers({}))
  expect(screen.queryByRole('combobox')).toBeNull()
  h.unmount()
  expect(source.current).toBeNull()
})
