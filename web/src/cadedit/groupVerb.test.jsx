import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import EngineSessionProvider, { useEngineSessionContext } from './EngineSessionProvider.jsx'
import EngineRibbonClusters from './EngineRibbonClusters.jsx'
import CanvasPointPicker from './CanvasPointPicker.jsx'
import CommandLineArmer from './CommandLineArmer.jsx'
import EngineDockProperties from './EngineDockProperties.jsx'
import { buildEditPayload } from './engineSession.js'
import { parseDrawingCommand, COCKPIT_COMMAND_EVENT } from '../lib/commandWords.js'

const line = (id, y) => ({ id, handle: id, type: 'LINE', layer: '0', editable: true, vertices: [[0, y, 0], [3, y, 0]] })
const entities = [line('10', 0), line('11', 10), line('12', 20)]
let context
function Probe() { context = useEngineSessionContext(); return null }
class Worker {
  posted = []
  listeners = new Map()
  addEventListener(type, listener) { this.listeners.set(type, listener) }
  removeEventListener(type) { this.listeners.delete(type) }
  postMessage(message) { this.posted.push(message) }
  terminate() {}
  emit(message) { act(() => this.listeners.get('message')({ data: message })) }
}
afterEach(() => { cleanup(); context = null })

describe('named GROUP and UNGROUP in the cockpit', () => {
  it('refuses invalid names, collisions, too few members and noneditable members before dispatch', () => {
    const list = Object.assign(entities.slice(), { groups: [{ name: 'RACK', memberIds: ['10', '11'] }] })
    const payload = (inputs) => buildEditPayload('group', '10', inputs, [], list)
    expect(payload({ groupName: 'OTHER', members: '11 11 10' }).payload).toEqual({ name: 'OTHER', members: ['10', '11'] })
    for (const groupName of ['', '*X', 'X|Y', 'X\nY', 'é', 'X'.repeat(256)]) expect(payload({ groupName, members: '11' }).refusal).toBeTruthy()
    expect(payload({ groupName: 'rack', members: '11' }).refusal).toContain('group_name_exists')
    expect(payload({ groupName: 'ONE', members: '10' }).refusal).toContain('group_needs_two_members')
    expect(payload({ groupName: 'BAD', members: '999' }).refusal).toContain('group_member_not_editable')
    expect(buildEditPayload('group', '10', { groupName: 'BAD', members: '11' }, [], [entities[0], { ...entities[1], blockChild: true }]).refusal).toContain('group_member_not_editable')
    expect(buildEditPayload('ungroup', '', { groupName: 'rack' }, [], list).payload).toEqual({ name: 'RACK' })
    expect(buildEditPayload('ungroup', '', { groupName: 'missing' }, [], list).refusal).toContain('group_not_found')
  })

  it('arms GROUP, appends two edge picks, posts once, highlights members and ungroups once', () => {
    const worker = new Worker()
    const ground = document.createElement('div')
    document.body.appendChild(ground)
    const viewer = { unproject: (x, y) => ({ x: x / 10, y: y / 10 }), setHighlight: vi.fn(), setRubberBand: vi.fn(), setSnapMarker: vi.fn() }
    const viewerRef = { current: viewer }
    render(<EngineSessionProvider createWorker={() => worker}>
      <Probe /><CommandLineArmer />
      <EngineRibbonClusters panels={['groups']} />
      <CanvasPointPicker ground={ground} viewerRef={viewerRef} />
      <div id="cockpit-dock-properties-slot" /><EngineDockProperties />
    </EngineSessionProvider>)
    act(() => context.session.actions.openBytes(new Uint8Array([48, 10]), 'groups.dxf'))
    worker.emit({ type: 'documentLoaded', documentId: 'groups.dxf', entities, entityCount: 3, groups: [] })
    act(() => context.session.actions.select('10'))
    expect(screen.queryByTestId('dock-groups')).toBeNull()
    act(() => window.dispatchEvent(new CustomEvent(COCKPIT_COMMAND_EVENT, { detail: parseDrawingCommand('GROUP') })))
    expect(screen.getByTestId('cockpit-prompt').dataset.op).toBe('group')
    const pick = (y) => act(() => {
      ground.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true, button: 0, clientX: 15, clientY: y }))
      ground.dispatchEvent(new MouseEvent('pointerup', { bubbles: true, button: 0, clientX: 15, clientY: y }))
    })
    pick(0); pick(100); pick(100); pick(200)
    expect(context.inputs.members).toBe('11 12')
    expect(screen.getByLabelText('ribbon members').textContent).toBe('3 objects')
    fireEvent.click(screen.getByTestId('cockpit-prompt-run'))
    expect(screen.getByLabelText('ribbon group name')).toBe(document.activeElement)
    fireEvent.change(screen.getByLabelText('ribbon group name'), { target: { value: 'RACK' } })
    fireEvent.keyDown(screen.getByLabelText('ribbon group name'), { key: 'Enter' })
    const posts = () => worker.posted.filter((message) => message.type === 'applyEdit')
    expect(posts()).toEqual([{ type: 'applyEdit', op: 'createGroup', payload: { name: 'RACK', members: ['10', '11', '12'] } }])
    const groups = [{ id: '240', name: 'RACK', memberIds: ['10', '11', '12'], selectable: true }]
    worker.emit({ type: 'editApplied', op: 'createGroup', ok: true, createdId: '240', entities, groups, entityCount: 3, bytes: new Uint8Array([1]), byteLength: 1 })
    expect(context.session.selectedId).toBe('10')
    expect(context.session.status).toContain('group 240 created.')
    expect(context.session.undoDepth).toBe(1)
    expect(screen.getByTestId('dock-groups').textContent).toBe('RACK')
    fireEvent.change(screen.getByLabelText('Select group'), { target: { value: 'RACK' } })
    expect(viewer.setHighlight).toHaveBeenLastCalledWith(['10', '11', '12'])
    act(() => context.setArmed({ group: 'groups', op: 'ungroup' }))
    fireEvent.change(screen.getByLabelText('ribbon group name'), { target: { value: 'RACK' } })
    fireEvent.keyDown(screen.getByLabelText('ribbon group name'), { key: 'Enter' })
    expect(posts()).toHaveLength(2)
    expect(posts()[1]).toEqual({ type: 'applyEdit', op: 'ungroup', payload: { name: 'RACK' } })
    worker.emit({ type: 'editApplied', op: 'ungroup', ok: true, entities, groups: [], entityCount: 3, bytes: new Uint8Array([2]), byteLength: 1 })
    expect(context.session.selectedId).toBe('10')
    expect(screen.queryByTestId('dock-groups')).toBeNull()
    act(() => context.session.actions.reset())
    expect(viewer.setHighlight).toHaveBeenLastCalledWith([])
    ground.remove()
  })
})
