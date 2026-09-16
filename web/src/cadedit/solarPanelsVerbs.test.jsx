// C-04B: Solar is a seat over the shared drafting session and its builders.
import { useEffect, useRef, useState } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import EngineRibbonClusters from './EngineRibbonClusters.jsx'
import EngineSessionProvider, { useEngineSessionContext } from './EngineSessionProvider.jsx'
import { buildCreatePayload, buildEditPayload } from './engineSession.js'
import { profileEntryTab, profileRibbonTabs } from '../lib/ribbonClusters.js'
import { surfaceContract } from '../site/productSurfaces.js'
import DraftingRibbon from '../site/DraftingRibbon.jsx'

const outline = { id: '9', type: 'LWPOLYLINE', layer: 'Panels', closed: true,
  vertices: [[0, 0, 0], [2, 0, 0], [2, 1, 0], [0, 1, 0]] }
const grid = { rows: '2', cols: '3', rowGap: '2', colGap: '3' }
class ScriptedWorker {
  constructor() { this.posted = []; this.listeners = new Map() }
  addEventListener(type, fn) { this.listeners.set(type, fn) }
  removeEventListener(type) { this.listeners.delete(type) }
  postMessage(message) { this.posted.push(message) }
  terminate() {}
  emit(data) { act(() => this.listeners.get('message')?.({ data })) }
}
let context
let worker
function ProfileRibbon({ profile }) {
  context = useEngineSessionContext()
  const home = surfaceContract(profile).toolbar.home
  const mode = surfaceContract(profile).toolbar.profile
  const [tab, setTab] = useState(home)
  const previous = useRef(null)
  const tabs = profileRibbonTabs(mode)
  const entry = profileEntryTab(previous.current, mode, tab, home)
  const active = tabs.some((item) => item.id === entry) ? entry : home
  useEffect(() => { previous.current = mode; setTab(active) }, [mode, active])
  const clusters = (tabs.find((item) => item.id === active)?.clusters || []).map((cluster) =>
    cluster.id === 'solar-panels' ? { ...cluster, extra: <div id="cockpit-solar-panels-slot" /> } : cluster)
  return <>
    <button onClick={() => setTab('draw')}>Choose Draw</button>
    <button onClick={() => setTab('model')}>Choose Model</button>
    <DraftingRibbon tab={active} clusters={clusters}>
      <EngineRibbonClusters panels={active === 'solar' ? ['solar-panels'] : []} />
    </DraftingRibbon>
  </>
}
function mount(profile = 'solar') {
  worker = new ScriptedWorker()
  const createWorker = () => worker
  const tree = (next) => <EngineSessionProvider createWorker={createWorker}><ProfileRibbon profile={next} /></EngineSessionProvider>
  const view = render(tree(profile))
  return (next) => view.rerender(tree(next))
}
async function open(entities = [outline]) {
  const bytes = new TextEncoder().encode('0\nEOF\n')
  const file = new File([bytes], 'panels.dxf')
  file.arrayBuffer = async () => bytes.buffer.slice(0)
  await act(async () => { await context.session.actions.open(file) })
  worker.emit({ type: 'documentLoaded', documentId: 'panels.dxf', entities, entityCount: entities.length, unsupported: [] })
  if (entities.length) act(() => context.session.actions.select('9'))
}
afterEach(cleanup)

describe('Solar panel geometry', () => {
  it('row1 builds one closed four-vertex outline with area 2', async () => {
    const { payload } = buildCreatePayload('createRectangle', { x: '0', y: '0', x2: '2', y2: '1', layer: 'Panels' })
    expect(payload).toEqual({ points: [0, 0, 2, 0, 2, 1, 0, 1], closed: true, layer: 'Panels' })
    const points = Array.from({ length: 4 }, (_, i) => payload.points.slice(i * 2, i * 2 + 2))
    const area = Math.abs(points.reduce((sum, [x, y], i) => {
      const next = points[(i + 1) % points.length]
      return sum + x * next[1] - next[0] * y
    }, 0)) / 2
    expect(area).toBe(2)
    mount()
    await open([])
    act(() => context.session.actions.create('createRectangle', { x: '0', y: '0', x2: '2', y2: '1', layer: 'Panels' }))
    expect(worker.posted.filter((message) => message.type === 'applyEdit')).toEqual([
      { type: 'applyEdit', op: 'createPolyline', payload },
    ])
    worker.emit({ type: 'editApplied', op: 'createPolyline', ok: true, entities: [outline], entityCount: 1,
      createdId: '9', bytes: new Uint8Array([48, 10]), byteLength: 2 })
    expect(context.session.entities).toHaveLength(1)
    expect(context.session.entities[0]).toMatchObject({ type: 'LWPOLYLINE', closed: true, vertices: outline.vertices })
  })
  it('row2 refuses zero width before the engine', () => {
    expect(buildCreatePayload('createRectangle', { x: '0', y: '0', x2: '0', y2: '1' }))
      .toEqual({ refusal: 'Rectangle refused: the corners must differ in both x and y.' })
  })
  it('row3 a 2 by 3 array posts one edit and seats five copies with one undo snapshot', async () => {
    mount()
    await open()
    fireEvent.click(screen.getByRole('button', { name: 'Panel array', exact: true }))
    expect(context.armed).toMatchObject({ group: 'modify', op: 'arrayRect' })
    act(() => context.session.actions.applyEdit('arrayRect', grid))
    expect(worker.posted.filter((message) => message.type === 'applyEdit')).toEqual([
      { type: 'applyEdit', op: 'arrayRect', payload: { entityId: '9', rows: 2, cols: 3, rowGap: 2, colGap: 3 } },
    ])
    // Scripted engine reply: store proof, not a claim about WASM execution.
    const copies = [[3, 0], [6, 0], [0, 2], [3, 2], [6, 2]].map(([dx, dy], i) => ({
      ...outline, id: String(10 + i), vertices: outline.vertices.map(([x, y, z]) => [x + dx, y + dy, z]),
    }))
    worker.emit({ type: 'editApplied', op: 'arrayRect', ok: true, entities: [outline, ...copies], entityCount: 6,
      createdId: '10', createdIds: ['10', '11', '12', '13', '14'], bytes: new Uint8Array([48, 10]), byteLength: 2 })
    expect(context.session.entities).toHaveLength(6)
    expect(context.session.undoDepth).toBe(1)
  })
  it('row4 refuses the source-only 1 by 1 array', () => {
    expect(buildEditPayload('arrayRect', '9', { ...grid, rows: '1', cols: '1' }).refusal)
      .toBe('Array refused: 1 row by 1 column is the source alone, so there is nothing to copy.')
  })
  it('row5 refuses 1001 copies but admits the 1000-copy boundary', () => {
    expect(buildEditPayload('arrayRect', '9', { ...grid, rows: '1', cols: '1002' }).refusal).toBe('Array refused: that is more than 1000 copies.')
    expect(buildEditPayload('arrayRect', '9', { ...grid, rows: '1', cols: '1001' }).payload.cols).toBe(1001)
  })
  it('row6 refuses an INSERT selection by kind', () => {
    expect(buildEditPayload('arrayRect', '9', grid, [], [{ ...outline, type: 'INSERT' }]).refusal)
      .toBe('an INSERT is placed, not edited, in this round')
  })
  it('row12 selects Solar on each entry while keeping the engine document and undo state', async () => {
    const switchProfile = mount()
    await open()
    act(() => context.session.actions.applyEdit('move', { dx: '1', dy: '0' }))
    worker.emit({ type: 'editApplied', op: 'move', ok: true,
      entities: [{ ...outline, vertices: outline.vertices.map(([x, y, z]) => [x + 1, y, z]) }], entityCount: 1,
      bytes: new Uint8Array([48, 11]), byteLength: 2 })
    const identity = context.session.documentLoadIdentity
    const entities = context.session.entities
    const undoDepth = context.session.undoDepth
    expect(undoDepth).toBe(1)
    const band = () => screen.getByTestId('drafting-ribbon')
    expect(band().getAttribute('data-tab')).toBe('solar')
    fireEvent.click(screen.getByRole('button', { name: 'Choose Model' }))
    expect(band().getAttribute('data-tab')).toBe('model')
    switchProfile('cad')
    fireEvent.click(screen.getByRole('button', { name: 'Choose Draw' }))
    switchProfile('solar')
    expect(band().getAttribute('data-tab')).toBe('solar')
    switchProfile('cad')
    expect(band().getAttribute('data-tab')).toBe('draw')
    fireEvent.click(screen.getByRole('button', { name: 'Choose Model' }))
    switchProfile('solar')
    expect(band().getAttribute('data-tab')).toBe('solar')
    expect(context.session.documentLoadIdentity).toBe(identity)
    expect(context.session.entities).toBe(entities)
    expect(context.session.undoDepth).toBe(undoDepth)
    expect(surfaceContract('cad').toolbar.home).toBe('draw')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Panel outline', exact: true })).toBeTruthy())
    for (const [name, group, op] of [['Panel outline', 'draw', 'createRectangle'], ['Panel array', 'modify', 'arrayRect'], ['Move panel', 'modify', 'move'], ['Rotate panel', 'modify', 'rotate']]) {
      fireEvent.click(screen.getByRole('button', { name, exact: true }))
      expect(context.armed).toMatchObject({ group, op })
    }
  })
})
