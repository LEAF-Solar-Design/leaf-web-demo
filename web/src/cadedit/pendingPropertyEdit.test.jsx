// S1 (Apply boundary): the store's explicit-target path (`applyEdit(op,
// inputs, { targetId })`) and the App key ladder's view of an Esc that
// cancels a staged property change. The ribbon rows live in
// propertyVerbs.test.jsx; these pin the two seams under them.
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ladderListener } from '../lib/actionRegistry.js'
import DraftingRibbon from '../site/DraftingRibbon.jsx'
import EngineRibbonClusters from './EngineRibbonClusters.jsx'
import EngineSessionProvider, { useEngineSessionContext } from './EngineSessionProvider.jsx'

afterEach(() => cleanup())

class FakeWorker {
  constructor() { this.posted = []; this.listeners = new Map() }
  addEventListener(type, fn) { this.listeners.set(type, fn) }
  removeEventListener(type) { this.listeners.delete(type) }
  postMessage(message) { this.posted.push(message) }
  terminate() {}
  emit(data) { act(() => { this.listeners.get('message')?.({ data }) }) }
}

const common = { layer: 'A', aci: 256, linetype: 'ByLayer', lineweight: -1 }
const line = (id) => ({
  id, handle: id, type: 'LINE', closed: false, editable: true,
  vertices: [[0, 0, 0], [1, 1, 0]], radius: null, startDeg: null, endDeg: null, ...common,
})
const ENTITIES = [
  line('7'),
  line('9'),
  { id: 'm1', handle: 'M1', type: 'MLEADER', editable: false, ...common },
  { id: 'd1', handle: 'D1', type: 'DIMENSION', editable: false, ...common },
  { id: 'i1', handle: 'I1', type: 'INSERT', editable: false, name: 'Fixture', ...common },
]

function mount({ ribbon = false } = {}) {
  let context = null
  function Probe() { context = useEngineSessionContext(); return null }
  const workers = []
  const createWorker = vi.fn(() => { const w = new FakeWorker(); workers.push(w); return w })
  const seat = {
    id: 'properties', label: 'Properties', kind: 'group', tools: [],
    extra: <div id="cockpit-properties-slot" className="ribbon-slot" />,
  }
  render(
    <EngineSessionProvider createWorker={createWorker}>
      <Probe />
      {ribbon ? (
        <DraftingRibbon clusters={[seat]}>
          <EngineRibbonClusters importOpen={false} onToggleImport={() => {}} panels={['properties']} />
        </DraftingRibbon>
      ) : null}
    </EngineSessionProvider>,
  )
  act(() => { context.session.actions.openBytes(new Uint8Array([0]), 'x.dxf') })
  workers[0].emit({ type: 'documentLoaded', documentId: 'x.dxf', entities: ENTITIES, entityCount: ENTITIES.length, unsupported: [] })
  const edits = () => workers[0].posted.filter((message) => message.type === 'applyEdit')
  return { getContext: () => context, edits }
}

describe('applyEdit with an explicit { targetId }', () => {
  it('names the target in the payload whatever the live selection is', () => {
    const { getContext, edits } = mount()
    act(() => { getContext().session.actions.select('9') })
    act(() => { getContext().session.actions.applyEdit('setColor', { aci: 'red' }, { targetId: '7' }) })
    expect(edits()).toEqual([{ type: 'applyEdit', op: 'setColor', payload: { entityId: '7', aci: 1 } }])
  })

  it('skips the live multi-selection refusal and the empty-selection no-op', () => {
    const { getContext, edits } = mount()
    act(() => { getContext().session.actions.selectReplace(['7', '9']) })
    act(() => { getContext().session.actions.applyEdit('setColor', { aci: 'red' }, { targetId: '7' }) })
    act(() => { getContext().session.actions.selectClear() })
    act(() => { getContext().session.actions.applyEdit('setLineweight', { lineweight: 'Default' }, { targetId: '9' }) })
    expect(edits()).toEqual([
      { type: 'applyEdit', op: 'setColor', payload: { entityId: '7', aci: 1 } },
      { type: 'applyEdit', op: 'setLineweight', payload: { entityId: '9', lineweight: -3 } },
    ])
  })

  it('refuses a target the document does not hold', () => {
    const { getContext, edits } = mount()
    act(() => { getContext().session.actions.select('7') })
    act(() => { getContext().session.actions.applyEdit('setColor', { aci: 'red' }, { targetId: 'gone' }) })
    expect(edits()).toHaveLength(0)
    expect(getContext().session.status).toBe('Edit refused (setColor): the target entity is no longer in the document.')
  })

  it('judges the MLEADER and DIMENSION refusals on the target, not the selection', () => {
    const { getContext, edits } = mount()
    act(() => { getContext().session.actions.select('9') })
    act(() => { getContext().session.actions.applyEdit('setColor', { aci: 'red' }, { targetId: 'm1' }) })
    expect(getContext().session.status).toBe('a mleader is placed, not edited, in this round')
    act(() => { getContext().session.actions.applyEdit('setColor', { aci: 'red' }, { targetId: 'd1' }) })
    expect(getContext().session.status).toBe('a dimension is placed, not edited, in this round')
    expect(edits()).toHaveLength(0)
    // And the reverse: a DIMENSION selected, a LINE target, the edit posts.
    act(() => { getContext().session.actions.select('d1') })
    act(() => { getContext().session.actions.applyEdit('setColor', { aci: 'red' }, { targetId: '7' }) })
    expect(edits()).toEqual([{ type: 'applyEdit', op: 'setColor', payload: { entityId: '7', aci: 1 } }])
  })

  it('judges the INSERT refusal on the target for a geometry op; a property op on an INSERT still posts', () => {
    const { getContext, edits } = mount()
    act(() => { getContext().session.actions.select('9') })
    act(() => { getContext().session.actions.applyEdit('move', { dx: '1', dy: '0' }, { targetId: 'i1' }) })
    expect(getContext().session.status).toBe('an INSERT is placed, not edited, in this round')
    expect(edits()).toHaveLength(0)
    act(() => { getContext().session.actions.applyEdit('setColor', { aci: 'red' }, { targetId: 'i1' }) })
    expect(edits()).toEqual([{ type: 'applyEdit', op: 'setColor', payload: { entityId: 'i1', aci: 1 } }])
  })

  it.each(['offset', 'matchprop', 'group', 'ungroup', 'trim', 'fillet'])('refuses { targetId } on %s and posts nothing', (op) => {
    const { getContext, edits } = mount()
    act(() => { getContext().session.actions.select('7') })
    act(() => { getContext().session.actions.applyEdit(op, { dist: '5', x: '0', y: '0', edge: '9', groupName: 'G' }, { targetId: '7' }) })
    expect(edits()).toHaveLength(0)
    expect(getContext().session.status).toBe(`Edit refused (${op}): this command works on the selection, not a pinned target.`)
  })

  it('the call without a third argument is unchanged: the live selection, and the multi-selection refusal', () => {
    const { getContext, edits } = mount()
    act(() => { getContext().session.actions.select('9') })
    act(() => { getContext().session.actions.applyEdit('setColor', { aci: 'red' }) })
    expect(edits()).toEqual([{ type: 'applyEdit', op: 'setColor', payload: { entityId: '9', aci: 1 } }])
    act(() => { getContext().session.actions.selectReplace(['7', '9']) })
    act(() => { getContext().session.actions.applyEdit('setColor', { aci: 'green' }) })
    expect(edits()).toHaveLength(1)
    act(() => { getContext().session.actions.selectClear() })
    act(() => { getContext().session.actions.applyEdit('setColor', { aci: 'green' }) })
    expect(edits()).toHaveLength(1)
  })
})

describe('the App key ladder and a staged property change', () => {
  function installLadder() {
    const onClearSelection = vi.fn()
    const onCloseProject = vi.fn()
    const shell = { selectedHandle: '7', openProjectId: 'p1' }
    const listener = ladderListener(shell, (ctx) => ({ ...ctx, onClearSelection, onCloseProject }), () => {})
    window.addEventListener('keydown', listener)
    return { onClearSelection, onCloseProject, remove: () => window.removeEventListener('keydown', listener) }
  }

  it('with nothing staged, Esc in the Color combo still climbs the ladder (the control)', () => {
    const { getContext } = mount({ ribbon: true })
    act(() => { getContext().session.actions.select('7') })
    const ladder = installLadder()
    try {
      fireEvent.keyDown(screen.getByLabelText(/^Color/), { key: 'Escape' })
      expect(ladder.onClearSelection).toHaveBeenCalledTimes(1)
    } finally {
      ladder.remove()
    }
  })

  it('one Esc discards the staged change; the project stays open and the selection is unchanged', () => {
    const { getContext, edits } = mount({ ribbon: true })
    act(() => { getContext().session.actions.select('7') })
    fireEvent.change(screen.getByLabelText(/^Color/), { target: { value: 'red' } })
    expect(getContext().pending).toMatchObject({ op: 'setColor', targetId: '7' })
    const ladder = installLadder()
    try {
      fireEvent.keyDown(screen.getByLabelText(/^Color/), { key: 'Escape' })
      expect(getContext().pending).toBeNull()
      expect(ladder.onClearSelection).not.toHaveBeenCalled()
      expect(ladder.onCloseProject).not.toHaveBeenCalled()
      expect(getContext().session.selectedId).toBe('7')
      expect(edits()).toHaveLength(0)
    } finally {
      ladder.remove()
    }
  })

  it('an Esc on the strip itself cancels too, and never reaches the ladder', () => {
    const { getContext } = mount({ ribbon: true })
    act(() => { getContext().session.actions.select('7') })
    fireEvent.change(screen.getByLabelText(/^Linetype/), { target: { value: 'Continuous' } })
    const ladder = installLadder()
    try {
      fireEvent.keyDown(screen.getByRole('button', { name: /^Apply Linetype change/ }), { key: 'Escape' })
      expect(getContext().pending).toBeNull()
      expect(ladder.onClearSelection).not.toHaveBeenCalled()
      expect(ladder.onCloseProject).not.toHaveBeenCalled()
    } finally {
      ladder.remove()
    }
  })
})
