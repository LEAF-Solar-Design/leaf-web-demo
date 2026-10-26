import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import DraftingRibbon from '../site/DraftingRibbon.jsx'
import CanvasPointPicker from './CanvasPointPicker.jsx'
import EngineRibbonClusters, { evaluateDimension, saveReason } from './EngineRibbonClusters.jsx'
import EngineSessionProvider, { useEngineSessionContext } from './EngineSessionProvider.jsx'
import { buildEditPayload, cadMessage, drawingUnitFromBytes } from './engineSession.js'

const LINE = { id: '7F', type: 'LINE', editable: true, vertices: [[0, 0, 0], [10, 0, 0]] }
const EDGE = { ...LINE, id: '80', vertices: [[5, -5, 0], [5, 5, 0]] }
const ORIGINAL = new TextEncoder().encode('0\nSECTION\n2\nHEADER\n9\n$INSUNITS\n70\n2\n0\nENDSEC\n0\nEOF\n')
let ground

class ScriptedWorker {
  constructor() { this.listeners = new Map(); this.posted = [] }
  addEventListener(type, listener) { this.listeners.set(type, listener) }
  removeEventListener(type) { this.listeners.delete(type) }
  postMessage(message) { this.posted.push(message) }
  terminate() {}
  emit(data) { act(() => this.listeners.get('message')?.({ data })) }
}

function mount(saveTarget = null, entities = [LINE, EDGE]) {
  const handle = { current: null, worker: new ScriptedWorker() }
  ground = document.createElement('div')
  document.body.appendChild(ground)
  const viewerRef = { current: { unproject: (x, y) => ({ x: x / 10, y: y / 10 }) } }
  function Probe() {
    handle.current = useEngineSessionContext()
    return <output role="status">{handle.current.session.status}</output>
  }
  render(<EngineSessionProvider createWorker={() => handle.worker} saveTarget={saveTarget}>
    <Probe />
    <DraftingRibbon clusters={[]}>
      <EngineRibbonClusters panels={['file', 'draw', 'modify', 'groups']} />
    </DraftingRibbon>
    <CanvasPointPicker viewerRef={viewerRef} ground={ground} />
  </EngineSessionProvider>)
  act(() => handle.current.session.actions.openBytes(ORIGINAL, 'drawing.dxf'))
  handle.load = () => handle.worker.emit({ type: 'documentLoaded', documentId: 'drawing.dxf', entities, entityCount: entities.length, unsupported: [] })
  handle.load()
  handle.edit = (op = 'move', extra = {}) => handle.worker.emit({ type: 'editApplied', op, ok: true, entities, entityCount: entities.length, bytes: new Uint8Array([1, 2, 3]), byteLength: 3, ...extra })
  return handle
}

function click(x, y) {
  act(() => {
    ground.dispatchEvent(new MouseEvent('pointerdown', { clientX: x, clientY: y, button: 0, bubbles: true }))
    ground.dispatchEvent(new MouseEvent('pointerup', { clientX: x, clientY: y, button: 0, bubbles: true }))
  })
}

beforeEach(() => {
  vi.stubGlobal('URL', { createObjectURL: vi.fn(() => 'blob:drawing'), revokeObjectURL: vi.fn() })
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => { callback(); return 0 })
})
afterEach(() => { cleanup(); ground?.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('w3 CAD editing regressions', () => {
  it('preserves specific worker refusals through batch wrappers', () => {
    expect(cadMessage('step_2_move:bad_entity_id', 'move')).toBe('The selected object is no longer available. Select it again and retry.')
    expect(cadMessage('batch_empty', 'move')).toBe('There are no changes to apply. Choose a change and retry.')
    expect(cadMessage('batch_restore_failed:private_details', 'move')).toBe('The drawing could not be restored after the failed change. Open the drawing again.')
    expect(cadMessage('step_0_setVertices:radius_not_positive', 'trim')).toBe('The radius must be greater than zero. Enter another radius and retry.')
    expect(cadMessage('unrecognized_code:private_details', 'move')).toBe('Move could not be applied. Try again or reopen the drawing.')
    expect(cadMessage('step_0_setVertices:unrecognized_code', 'batch')).toBe('Shape change could not be applied. Try again or reopen the drawing.')
    expect(cadMessage('linetype_not_loaded:Roof_West', 'setLinetype')).toBe('Property refused: linetype Roof_West is not loaded in this drawing')
  })

  it('preserves readable geometry, point syntax and names supplied by the user', () => {
    const point = 'LINE refused: "10,5,6" is not a point: use x,y, @dx,dy, dist<angle or @dist<angle.'
    expect(cadMessage(point)).toBe(point)
    expect(cadMessage('Offset refused: click to one side of the entity.')).toBe('Offset refused: click to one side of the entity.')
    expect(cadMessage('Property refused: linetype Roof_West is not loaded in this drawing')).toBe('Property refused: linetype Roof_West is not loaded in this drawing')
    expect(cadMessage('Save refused: group RACK needs at least two members to save; ungroup it or add a member.')).toBe('Save refused: group RACK needs at least two members to save; ungroup it or add a member.')
  })

  it('names each placed object kind and removes implementation details', () => {
    expect(cadMessage('an INSERT is placed, not edited, in this round')).toBe('A block can be placed here but not edited.')
    expect(cadMessage('a dimension is placed, not edited, in this round')).toBe('A dimension can be placed here but not edited.')
    expect(cadMessage('a mleader is placed, not edited, in this round')).toBe('A multileader can be placed here but not edited.')
    expect(cadMessage('Edit refused (setColor): the boundary rejected the message.')).toBe('Colour change could not be sent. Try again or reopen the drawing.')
    expect(cadMessage('Failed to fetch')).toBe("Couldn't reach the service. Check the connection and retry.")
  })

  it('explains malformed combined changes without exposing operation names', () => {
    expect(cadMessage('Edit refused: step setVertices names no entity.', 'trim')).toBe('A change has no selected object. Select the object again and retry.')
    expect(cadMessage('Edit refused: a colour step has an invalid ACI.', 'matchprop')).toBe('Choose a colour from 1 to 255, ByLayer or ByBlock and retry.')
    expect(cadMessage('Edit refused: an arc step needs a radius greater than 0.', 'fillet')).toBe('The radius must be greater than zero. Enter another radius and retry.')
    expect(cadMessage('Undo refused: the boundary rejected the message.')).toBe('Undo could not be sent. Try again or reopen the drawing.')
    expect(cadMessage('Redo refused: the boundary rejected the message.')).toBe('Redo could not be sent. Try again or reopen the drawing.')
  })

  it('groups a preselected set and counts all members in the prompt', () => {
    const handle = mount()
    act(() => {
      handle.current.session.actions.selectReplace(['7F', '80'])
      handle.current.setArmed({ group: 'groups', op: 'group' })
    })
    expect(screen.getByLabelText('ribbon members').textContent).toBe('2 objects')
    act(() => {
      handle.current.setInput('membersDone', 'true')
      handle.current.setInput('groupName', 'Roof')
    })
    expect(screen.getByTestId('cockpit-prompt-run').disabled).toBe(false)
    fireEvent.click(screen.getByTestId('cockpit-prompt-run'))
    expect(handle.worker.posted.at(-1)).toEqual({ type: 'applyEdit', op: 'createGroup', payload: { name: 'ROOF', members: ['7F', '80'] } })
  })

  it('keeps Save available after saving then undoing to the opened file', async () => {
    const save = vi.fn(async () => ({ head: 5, new_version: { version: 5, parent: 4 } }))
    const handle = mount({ headVersion: 4, save })
    handle.edit()
    await act(async () => { await handle.current.session.actions.save() })
    expect(handle.current.session.dirty).toBe(false)
    expect(handle.current.session.status).toBe('Saved as version 5.')
    act(() => handle.current.session.actions.undo())
    handle.load()
    expect(handle.current.session.savedBytes).toBe(ORIGINAL)
    expect(handle.current.session.status).toBe('Undid the move.')
    expect(handle.current.session.dirty).toBe(true)
    expect(saveReason(handle.current.session, true)).toBe('')
    await act(async () => { await handle.current.session.actions.save() })
    expect(save.mock.calls[1][0]).toBe(ORIGINAL)
  })

  it('announces a created object without operation names, handles or byte counts', () => {
    const handle = mount()
    handle.edit('createLine', { createdId: '7F' })
    expect(screen.getByRole('status').textContent).toBe('Drawing applied: entity created.')
    expect(screen.getByRole('status').textContent).not.toMatch(/createLine|7F|bytes/)
  })

  it('explains a refused zero-length trim without exposing the worker code', () => {
    const handle = mount()
    handle.worker.emit({ type: 'editApplied', op: 'batch', ok: false, reason: 'step_0_setVertices:line_zero_length' })
    expect(screen.getByRole('status').textContent).toBe('The line needs two different endpoints. Choose another point and retry.')
  })

  it('explains invalid group names with readable correction guidance', () => {
    const result = buildEditPayload('group', '7F', { groupName: 'Roof/West', members: ['7F', '80'] }, [], [LINE, EDGE])
    expect(result.refusal).toBe('Use a group name of 1 to 255 letters, numbers or spaces.')
  })

  it('reads drawing units from the opened DXF and accepts a typed length', () => {
    const handle = mount()
    expect(handle.current.session.drawingUnit).toBe('ft')
    act(() => handle.current.setArmed({ group: 'draw', op: 'createCircle' }))
    fireEvent.change(screen.getByLabelText('ribbon r'), { target: { value: '12 ft' } })
    expect(screen.getByTestId('cockpit-prompt-run').disabled).toBe(false)
    fireEvent.click(screen.getByTestId('cockpit-prompt-run'))
    expect(handle.worker.posted.at(-1)).toEqual({ type: 'applyEdit', op: 'createCircle', payload: { cx: 0, cy: 0, radius: 12, layer: '' } })
    expect(evaluateDimension('144 in', { unit: handle.current.session.drawingUnit })).toEqual({ value: '12' })
  })

  it('reads spaced CRLF headers and leaves unitless drawings unknown', () => {
    const encode = (text) => new TextEncoder().encode(text)
    expect(drawingUnitFromBytes(encode('  0\r\nSECTION\r\n  2\r\nHEADER\r\n  9\r\n$INSUNITS\r\n 70\r\n4\r\n  0\r\nENDSEC\r\n'))).toBe('mm')
    expect(drawingUnitFromBytes(encode('0\nSECTION\n2\nHEADER\n9\n$INSUNITS\n70\n0\n0\nENDSEC\n'))).toBeNull()
    expect(drawingUnitFromBytes(encode('0\nSECTION\n2\nENTITIES\n9\n$INSUNITS\n70\n2\n0\nENDSEC\n'))).toBeNull()
  })

  it('downloads edited bytes from the ribbon without opening the import pane', async () => {
    const handle = mount()
    handle.edit()
    const downloads = []
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () { downloads.push({ href: this.href, name: this.download }) })
    const button = document.querySelector('[data-tool="save-version"]')
    expect(button.disabled).toBe(false)
    expect(button.textContent).toContain('Download DXF')
    fireEvent.click(button)
    expect(URL.createObjectURL).toHaveBeenCalledWith(expect.any(Blob))
    expect(downloads).toEqual([{ href: 'blob:drawing', name: 'drawing.dxf' }])
    await waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:drawing'))
  })

  it('announces a missed edge pick and allows another click to succeed', () => {
    const handle = mount()
    act(() => {
      handle.current.session.actions.select('7F')
      handle.current.setArmed({ group: 'modify', op: 'trim' })
    })
    click(400, 400)
    expect(screen.getByRole('status').textContent).toContain('No other edge found there.')
    expect(handle.current.inputs.edge).toBe('')
    click(50, 40)
    expect(handle.current.inputs.edge).toBe('80')
  })

  it('explains an excluded selection hit without advancing the edge step', () => {
    const handle = mount()
    act(() => {
      handle.current.session.actions.select('7F')
      handle.current.setArmed({ group: 'modify', op: 'trim' })
    })
    click(90, 0)
    expect(screen.getByRole('status').textContent).toContain('Click closer to a different edge and try again.')
    expect(handle.current.inputs.edge).toBe('')
    expect(handle.worker.posted.some((message) => message.type === 'applyEdit')).toBe(false)
  })
})
