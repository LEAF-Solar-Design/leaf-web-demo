import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useLayoutEffect } from 'react'
import { afterEach, expect, it } from 'vitest'
import useEngineSession, { SESSION_ERROR } from './engineSession.js'
import EngineSessionProvider, { useEngineSessionContext } from './EngineSessionProvider.jsx'
import CadEditSurface from './CadEditSurface.jsx'
import EngineDockProperties, { DOCK_PROPERTIES_SLOT_ID } from './EngineDockProperties.jsx'
import { MODIFY_REASONS, modifyReason, propertyReason } from '../lib/actionRegistry.js'

afterEach(cleanup)

// Scripted transport, with the real EngineBoundary validating each message.
class ScriptedWorker {
  posted = []
  listeners = { message: [], error: [], messageerror: [] }
  addEventListener(type, cb) { this.listeners[type]?.push(cb) }
  removeEventListener() {}
  postMessage(data) { this.posted.push(data) }
  terminate() {}
  emit(data) { act(() => this.listeners.message.forEach((cb) => cb({ data }))) }
}

const ENTITIES = ['7', '9', '11'].map((id) => ({
  id, type: 'LINE', layer: '0', vertices: [[0, 0], [1, 1]],
}))
const loadedMessage = (entities = ENTITIES) => ({
  type: 'documentLoaded', documentId: 'one.dxf', entities, entityCount: entities.length, unsupported: [],
})
const editedMessage = (entities = ENTITIES) => ({
  type: 'editApplied', op: 'move', ok: true, entities, entityCount: entities.length,
  bytes: new Uint8Array([1, 2, 3]), byteLength: 3,
})
function fileOf() {
  const file = new File(['0\nEOF\n'], 'one.dxf')
  file.arrayBuffer = async () => new TextEncoder().encode('0\nEOF\n').buffer
  return file
}
async function mountSession(ui = false, load = true) {
  const worker = new ScriptedWorker()
  const createWorker = () => worker
  const handle = { current: null, worker }
  function Host() {
    const session = useEngineSession({ createWorker })
    // Observe committed renders, not React's discarded no-op render attempts.
    useLayoutEffect(() => { handle.current = session })
    return null
  }
  function Consumer() {
    handle.current = useEngineSessionContext().session
    return <><div id={DOCK_PROPERTIES_SLOT_ID} /><CadEditSurface enabled /><EngineDockProperties /></>
  }
  render(ui ? <EngineSessionProvider createWorker={createWorker}><Consumer /></EngineSessionProvider> : <Host />)
  if (load) {
    await act(async () => { await handle.current.actions.open(fileOf()) })
    worker.emit(loadedMessage())
  }
  return handle
}
function selectPair(handle) {
  act(() => handle.current.actions.selectReplace(['7', '9']))
}
function expectSelection(handle, ids) {
  expect(handle.current.selectedIds).toEqual(ids)
  expect(handle.current.selectedId).toBe(ids.length === 1 ? ids[0] : '')
  expect(handle.current.selected).toEqual(ids.length === 1 ? ENTITIES.find((entity) => entity.id === ids[0]) : null)
}

it('SSD1-24A select-verbatim accepts an id before document load', async () => {
  const h = await mountSession(false, false)
  act(() => h.current.actions.select('42'))
  expect(h.current.selectedId).toBe('42')
  expect(h.current.selectedIds).toEqual(['42'])
  expect(h.current.selected).toBeNull()
})
it('SSD1-24A select-verbatim preserves an explicit null clear', async () => {
  const h = await mountSession()
  act(() => h.current.actions.select('7'))
  act(() => h.current.actions.select(null))
  expect(h.current.selectedId).toBeNull()
  expect(h.current.selectedIds).toEqual([])
  expect(h.current.selected).toBeNull()
})
it('SSD1-24A select-verbatim clears a set with an empty string', async () => {
  const h = await mountSession()
  selectPair(h)
  act(() => h.current.actions.select(''))
  expectSelection(h, [])
})
it('SSD1-24A select-verbatim repeating an id preserves the session object', async () => {
  const h = await mountSession()
  act(() => h.current.actions.select('7'))
  const before = h.current
  act(() => h.current.actions.select('7'))
  expect(h.current).toBe(before)
  expectSelection(h, ['7'])
})

it('SSD1-24A row1 replace then add holds a real set', async () => {
  const h = await mountSession()
  act(() => h.current.actions.selectReplace(['7']))
  act(() => h.current.actions.selectAdd('9'))
  expectSelection(h, ['7', '9'])
})
it('SSD1-24A row2 toggle restores the singleton identity', async () => {
  const h = await mountSession()
  selectPair(h)
  act(() => h.current.actions.selectToggle('7'))
  expectSelection(h, ['9'])
})
it('SSD1-24A row3 adding an existing member preserves the session object', async () => {
  const h = await mountSession()
  act(() => h.current.actions.selectReplace(['9']))
  const before = h.current
  act(() => h.current.actions.selectAdd('9'))
  expect(h.current).toBe(before)
})
it('SSD1-24A row4 unknown and non-string additions preserve the session object', async () => {
  const h = await mountSession()
  act(() => h.current.actions.selectReplace(['9']))
  const before = h.current
  act(() => h.current.actions.selectAdd('999'))
  act(() => h.current.actions.selectAdd(7))
  expect(h.current).toBe(before)
})
it('SSD1-24A row5 a reparse keeps only surviving ids', async () => {
  const h = await mountSession()
  selectPair(h)
  h.worker.emit(editedMessage(ENTITIES.slice(1)))
  expectSelection(h, ['9'])
})
it.each(['reset', 'open'])('SSD1-24A row6 %s clears the set', async (action) => {
  const h = await mountSession()
  selectPair(h)
  if (action === 'reset') act(() => h.current.actions.reset())
  else {
    await act(async () => { await h.current.actions.open(fileOf()) })
    h.worker.emit(loadedMessage())
  }
  expectSelection(h, [])
})
it.each([['row7', 'move', 'Move'], ['row8', 'delete', 'Delete']])('SSD1-24A %s %s refuses without posting or changing saved bytes', async (_row, op, label) => {
  const h = await mountSession()
  h.worker.emit(editedMessage())
  selectPair(h)
  const savedBytes = h.current.savedBytes
  h.worker.posted.length = 0
  act(() => h.current.actions.applyEdit(op, { dx: '1', dy: '0' }))
  expect(h.current.status).toBe(label + ' needs one object; 2 are selected.')
  expect(h.current.errorKind).toBe(SESSION_ERROR.REFUSED)
  expect(h.worker.posted).toEqual([])
  expect(h.current.savedBytes).toBe(savedBytes)
})
it.each([[false, 'Copy'], [true, 'Cut']])('SSD1-24A row9 clipboard cut=%s refuses and preserves its record', async (cut, label) => {
  const h = await mountSession()
  act(() => h.current.actions.select('11'))
  act(() => h.current.actions.copyToClipboard(false))
  const clipboard = h.current.clipboard
  expect(clipboard).not.toBeNull()
  selectPair(h)
  h.worker.posted.length = 0
  act(() => h.current.actions.copyToClipboard(cut))
  expect(h.current.status).toBe(label + ' needs one object; 2 are selected.')
  expect(h.current.errorKind).toBe(SESSION_ERROR.REFUSED)
  expect(h.current.clipboard).toBe(clipboard)
  expect(h.worker.posted).toEqual([])
})
it('SSD1-24A row10 Create Block refuses before validating inputs or posting', async () => {
  const h = await mountSession()
  selectPair(h)
  h.worker.posted.length = 0
  act(() => h.current.actions.create('createBlock', { name: 'B', x: '0', y: '0' }))
  expect(h.current.status).toBe('Create Block needs one object; 2 are selected.')
  expect(h.current.errorKind).toBe(SESSION_ERROR.REFUSED)
  expect(h.worker.posted).toEqual([])
})
it('SSD1-24A row11 a singleton move posts exactly one edit', async () => {
  const h = await mountSession()
  act(() => h.current.actions.selectReplace(['7']))
  h.worker.posted.length = 0
  act(() => h.current.actions.applyEdit('move', { dx: '1', dy: '0' }))
  expect(h.worker.posted).toEqual([{ type: 'applyEdit', op: 'move', payload: { entityId: '7', dx: 1, dy: 0 } }])
})
it('SSD1-24A row12 modify and property ladders name a set and allow a singleton', async () => {
  const h = await mountSession()
  selectPair(h)
  for (const reason of [modifyReason, propertyReason]) expect(reason(h.current)).toBe(MODIFY_REASONS.multiSelection)
  act(() => h.current.actions.selectReplace(['7']))
  for (const reason of [modifyReason, propertyReason]) expect(reason(h.current)).toBe('')
})
it.each(['shiftKey', 'ctrlKey', 'metaKey'])('SSD1-24A row13 %s click toggles the second list row', async (modifier) => {
  const h = await mountSession(true)
  const radios = screen.getAllByRole('radio')
  fireEvent.click(radios[0])
  fireEvent.click(radios[1], { [modifier]: true })
  expectSelection(h, ['7', '9'])
  expect(radios[0].closest('li').getAttribute('data-in-selection')).toBe('true')
  expect(radios[1].closest('li').getAttribute('data-in-selection')).toBe('true')
  expect(radios.every((radio) => !radio.checked)).toBe(true)
  expect(screen.getByTestId('cad-edit-selection-count').textContent).toBe('2 objects selected')
  fireEvent.click(radios[2])
  expectSelection(h, ['11'])
})
it('SSD1-24A row14 the dock reports the set instead of one entity properties', async () => {
  const h = await mountSession(true)
  selectPair(h)
  expect(screen.getByTestId('dock-selection-count').textContent).toBe('2 objects selected')
  expect(screen.getByTestId('dock-properties').querySelectorAll('dt')).toHaveLength(1)
})
it('SSD1-24A clear and identical replace preserve no-op identity', async () => {
  const h = await mountSession()
  selectPair(h)
  const before = h.current
  act(() => h.current.actions.selectReplace(['7', '9', '7']))
  expect(h.current).toBe(before)
  act(() => h.current.actions.selectClear())
  expectSelection(h, [])
  const empty = h.current
  act(() => h.current.actions.selectClear())
  expect(h.current).toBe(empty)
})
