import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useLayoutEffect } from 'react'
import { afterEach, expect, it } from 'vitest'
import useEngineSession, { SESSION_ERROR, buildSelectionEditPayload } from './engineSession.js'
import EngineSessionProvider, { useEngineSessionContext } from './EngineSessionProvider.jsx'
import CadEditSurface from './CadEditSurface.jsx'
import EngineDockProperties, { DOCK_PROPERTIES_SLOT_ID } from './EngineDockProperties.jsx'
import { MODIFY_REASONS, modifyReason, modifyOpReason, propertyReason } from '../lib/actionRegistry.js'
import { multiSelectionRefusal } from './selection.js'
import { diffPlan } from './mutationDiff.js'

const common = { layer: '0', editable: true, aci: 256, linetype: 'ByLayer', lineweight: -1 }
const D = [
  { ...common, id: '16', type: 'LINE', vertices: [[0, 0, 0], [10, 0, 0]] },
  { ...common, id: '17', type: 'LINE', vertices: [[0, 10, 0], [10, 10, 0]] },
  { ...common, id: '18', type: 'LINE', vertices: [[20, 0, 0], [20, 10, 0]] },
  { ...common, id: '19', type: 'CIRCLE', vertices: [[30, 10, 0]], radius: 5 },
]
D.linetypes = ['ByLayer', 'ByBlock', 'Continuous', 'DASHED']
const S = ['16', '19']
const batch = (op, ids, p) => ({ type: 'applyEdit', op: 'batch', payload: { verb: op, steps: ids.map((entityId) => ({ op, payload: { entityId, ...p } })) } })
const cases = [
  ['delete', {}, {}, [], [], { removed: ['10', '13'] }],
  ['move', { dx: '2', dy: '3' }, { dx: 2, dy: 3 }, [[2, 3, 0], [12, 3, 0]], [[32, 13, 0]], {
    set_points: [{ handle: '10', closed: false, pts: [[2, 3, 0], [12, 3, 0]] }],
    set_circle: [{ handle: '13', c: [32, 13, 0], r: 5 }],
  }],
  ['copy', { dx: '2', dy: '3' }, { dx: 2, dy: 3 }, [[2, 3, 0], [12, 3, 0]], [[32, 13, 0]], {
    added: [{ handle: '14', kind: 'LINE', layer: '0', pts: [[2, 3, 0], [12, 3, 0]] }, { handle: '15', kind: 'CIRCLE', layer: '0', c: [32, 13, 0], r: 5 }],
  }],
  ['rotate', { cx: '0', cy: '0', deg: '90' }, { cx: 0, cy: 0, deg: 90 }, [[0, 0, 0], [0, 10, 0]], [[-10, 30, 0]], {
    set_points: [{ handle: '10', closed: false, pts: [[0, 0, 0], [0, 10, 0]] }],
    set_circle: [{ handle: '13', c: [-10, 30, 0], r: 5 }],
  }],
  ['scale', { cx: '0', cy: '0', factor: '2' }, { cx: 0, cy: 0, factor: 2 }, [[0, 0, 0], [20, 0, 0]], [[60, 20, 0]], {
    set_points: [{ handle: '10', closed: false, pts: [[0, 0, 0], [20, 0, 0]] }],
    set_circle: [{ handle: '13', c: [60, 20, 0], r: 10 }],
  }],
  ['mirror', { x1: '0', y1: '0', x2: '0', y2: '1', keep: false }, { x1: 0, y1: 0, x2: 0, y2: 1, keep: false }, [[0, 0, 0], [-10, 0, 0]], [[-30, 10, 0]], {
    set_points: [{ handle: '10', closed: false, pts: [[0, 0, 0], [-10, 0, 0]] }],
    set_circle: [{ handle: '13', c: [-30, 10, 0], r: 5 }],
  }],
  ['mirror', { x1: '0', y1: '0', x2: '0', y2: '1', keep: 'true' }, { x1: 0, y1: 0, x2: 0, y2: 1, keep: true }, [[0, 0, 0], [-10, 0, 0]], [[-30, 10, 0]], {
    added: [{ handle: '14', kind: 'LINE', layer: '0', pts: [[0, 0, 0], [-10, 0, 0]] }, { handle: '15', kind: 'CIRCLE', layer: '0', c: [-30, 10, 0], r: 5 }],
  }],
]
// Derived geometry is scripted engine truth here; native geometry is checked separately.
function resultFor(index) {
  const [op, , p, line, circle] = cases[index]
  if (op === 'delete') return D.filter((entity) => !S.includes(entity.id))
  const copies = op === 'copy' || p.keep === true
  const changed = [{ ...D[0], id: copies ? '20' : '16', vertices: line }, { ...D[3], id: copies ? '21' : '19', vertices: circle, radius: op === 'scale' ? 10 : 5 }]
  return copies ? [...D, ...changed] : D.map((entity) => changed.find((next) => next.id === entity.id) || entity)
}
async function selectionSession(entities = D, ids = S) {
  const h = await mountSession()
  h.worker.emit(loadedMessage(entities))
  act(() => h.current.actions.selectReplace(ids))
  h.worker.posted.length = 0
  return h
}
function replyBatch(h, entities, extra = {}) {
  h.worker.emit({ ...editedMessage(entities), op: 'batch', ...extra })
}
function assertSet(h, ids) {
  expect(h.current.selectedIds).toEqual(ids)
  expect(h.current.selectedId).toBe(ids.length === 1 ? ids[0] : '')
}
async function exerciseCase(index) {
  const [op, inputs, p] = cases[index]
  const h = await selectionSession()
  const beforeBytes = h.current.savedBytes
  act(() => h.current.actions.applyEdit(op, inputs))
  expect(h.worker.posted).toEqual([batch(op, S, p)])
  expect(h.current.savedBytes).toBe(beforeBytes)
  expect(h.current.undoDepth).toBe(0)
  const entities = resultFor(index)
  const copies = op === 'copy' || p.keep === true
  replyBatch(h, entities, copies ? { createdId: '21', createdIds: ['20', '21'] } : {})
  expect(h.current.entities).toEqual(entities)
  assertSet(h, op === 'delete' ? [] : copies ? ['20', '21'] : S)
  if (copies) expect(h.current.entities.slice(0, 4)).toEqual(Array.from(D))
  expect(h.current.undoDepth).toBe(1)
  expect(h.current.redoDepth).toBe(0)
}

it('MS01 erase-set', async () => { await exerciseCase(0) })

it('MS02 move-set', async () => { await exerciseCase(1) })

it('MS03 copy-set', async () => { await exerciseCase(2) })

it('MS04 rotate-set', async () => { await exerciseCase(3) })

it('MS05 scale-set', async () => { await exerciseCase(4) })

it('MS06 mirror-replace', async () => { await exerciseCase(5) })

it('MS07 mirror-copy', async () => { await exerciseCase(6) })

it('MS08 delete-index-shift', async () => {
  const h = await selectionSession(D, ['16', '18'])
  act(() => h.current.actions.applyEdit('delete', {}))
  expect(h.worker.posted).toEqual([batch('delete', ['16', '18'], {})])
  replyBatch(h, [D[1], D[3]])
  expect(h.current.entities).toEqual([D[1], D[3]])
  assertSet(h, [])
})
it('MS09 duplicate-order', async () => {
  expect(buildSelectionEditPayload('move', ['19', '16', '19'], cases[1][1], D)).toEqual({ payload: batch('move', ['19', '16'], cases[1][2]).payload })
  const h = await selectionSession(D, ['19', '16', '19'])
  act(() => h.current.actions.applyEdit('move', cases[1][1]))
  expect(h.worker.posted).toEqual([batch('move', ['19', '16'], cases[1][2])])
  replyBatch(h, resultFor(1))
  assertSet(h, ['19', '16'])
})
it('MS10 bounded-set', async () => {
  for (const n of [5, 256, 257]) {
    const entities = Array.from({ length: n }, (_, i) => ({ ...common, id: String(16 + i), type: 'LINE', vertices: [[i, 0, 0], [i, 1, 0]] }))
    const ids = entities.map((entity) => entity.id)
    const h = await selectionSession(entities, ids)
    act(() => h.current.actions.applyEdit('move', cases[1][1]))
    if (n === 257) {
      expect(h.worker.posted).toEqual([])
      expect(h.current.status).toBe('Edit refused: select at most 256 objects.')
      expect(h.current.undoDepth).toBe(0)
    } else {
      expect(h.worker.posted).toEqual([batch('move', ids, cases[1][2])])
      replyBatch(h, entities.map((entity) => ({ ...entity, vertices: entity.vertices.map(([x, y, z]) => [x + 2, y + 3, z]) })))
      assertSet(h, ids)
    }
    cleanup()
  }
})
it('MS11 invalid-target-set', async () => {
  const h = await selectionSession()
  const bytes = h.current.savedBytes
  for (const [ids, refusal] of [
    [['16', '999'], 'Edit refused: a selected object is no longer in the document.'],
    [['16', 19], 'Edit refused: every selected object must have a valid entity id.'],
    [['16', ''], 'Edit refused: every selected object must have a valid entity id.'],
  ]) expect(buildSelectionEditPayload('move', ids, cases[1][1], D)).toEqual({ refusal })
  expect(h.worker.posted).toEqual([])
  expect(h.current.savedBytes).toBe(bytes)
  expect(h.current.undoDepth).toBe(0)
})
it('MS12 kind-preflight', async () => {
  for (const [type, refusal] of [
    ['INSERT', 'an INSERT is placed, not edited, in this round'],
    ['DIMENSION', 'a dimension is placed, not edited, in this round'],
    ['MLEADER', 'a mleader is placed, not edited, in this round'],
    ['XLINE', 'read-only entity kind'],
  ]) {
    const entities = [...D, { ...common, id: '20', type, editable: false }]
    const h = await selectionSession(entities, ['16', '20'])
    for (const index of [1, 2, 3, 4, 5]) {
      const [op, inputs] = cases[index]
      // Bad operands still lose to the later member's kind gate.
      expect(buildSelectionEditPayload(op, ['16', '20'], {}, entities)).toEqual({ refusal })
      act(() => h.current.actions.applyEdit(op, inputs))
      expect(h.current.status).toBe(refusal)
      expect(h.worker.posted).toEqual([])
      expect(h.current.undoDepth).toBe(0)
      assertSet(h, ['16', '20'])
    }
    cleanup()
  }
})
it('MS13 erase-placed', async () => {
  for (const type of ['INSERT', 'DIMENSION', 'MLEADER']) {
    const h = await selectionSession([...D, { ...common, id: '20', type, editable: false }], ['16', '20'])
    act(() => h.current.actions.applyEdit('delete', {}))
    expect(h.worker.posted).toEqual([batch('delete', ['16', '20'], {})])
    replyBatch(h, D.slice(1))
    expect(h.current.entities).toEqual(D.slice(1))
    assertSet(h, [])
    cleanup()
  }
})
it('MS14 operand-preflight', async () => {
  const h = await selectionSession()
  for (const [op, inputs, refusal] of [
    ['move', { dx: 'oops', dy: '3' }, 'Move refused: dx and dy must both be numbers.'],
    ['copy', { dx: 'oops', dy: '3' }, 'Copy refused: dx and dy must both be numbers.'],
    ['scale', { cx: '0', cy: '0', factor: '0' }, 'Scale refused: the factor must be greater than 0.'],
    ['mirror', { x1: '0', y1: '0', x2: '0', y2: '0' }, 'Mirror refused: the two points of the mirror line must differ.'],
  ]) {
    act(() => h.current.actions.applyEdit(op, inputs))
    expect(h.current.status).toBe(refusal)
    expect(h.worker.posted).toEqual([])
    expect(h.current.undoDepth).toBe(0)
    assertSet(h, S)
  }
})
it('MS15 single-and-empty', async () => {
  const h = await selectionSession(D, [])
  act(() => h.current.actions.applyEdit('move', cases[1][1]))
  expect(h.worker.posted).toEqual([])
  act(() => h.current.actions.selectReplace(['16']))
  act(() => h.current.actions.applyEdit('move', cases[1][1]))
  expect(h.worker.posted).toEqual([{ type: 'applyEdit', op: 'move', payload: { entityId: '16', dx: 2, dy: 3 } }])
  cleanup()
  const pinned = await selectionSession()
  act(() => pinned.current.actions.applyEdit('move', cases[1][1], { targetId: '16' }))
  expect(pinned.worker.posted).toEqual([{ type: 'applyEdit', op: 'move', payload: { entityId: '16', dx: 2, dy: 3 } }])
})
it('MS16 single-only', async () => {
  const h = await selectionSession()
  for (const op of ['moveVertex', 'addVertex', 'deleteVertex', 'offset', 'trim', 'extend', 'fillet', 'chamfer', 'matchprop', 'arrayRect', 'arrayPolar', 'explode']) {
    act(() => h.current.actions.applyEdit(op, {}))
    expect(h.current.status).toBe(multiSelectionRefusal(op, 2))
    expect(modifyOpReason(op, h.current)).toBe(MODIFY_REASONS.multiSelection)
  }
  for (const cut of [false, true]) {
    act(() => h.current.actions.copyToClipboard(cut))
    expect(h.current.status).toBe(multiSelectionRefusal(cut ? 'cutClip' : 'copyClip', 2))
  }
  act(() => h.current.actions.create('createBlock', {}))
  expect(h.current.status).toBe(multiSelectionRefusal('createBlock', 2))
  expect(h.worker.posted).toEqual([])
})
it('MS18 one-history-entry', async () => {
  const h = await selectionSession()
  const b0 = new TextEncoder().encode('0\nEOF\n')
  expect(h.worker.posted).toEqual([])
  act(() => h.current.actions.applyEdit('move', cases[1][1]))
  expect(h.current.savedBytes).toBeNull()
  expect(h.current.undoDepth).toBe(0)
  const b1 = new Uint8Array([1, 2, 3, 4])
  replyBatch(h, resultFor(1), { bytes: b1, byteLength: 4 })
  expect(h.current.savedBytes).toBe(b1)
  expect(h.current.undoDepth).toBe(1)
  expect(h.current.redoDepth).toBe(0)
  h.worker.posted.length = 0
  act(() => h.current.actions.undo())
  expect(h.worker.posted).toHaveLength(1)
  expect(h.worker.posted[0]).toMatchObject({ type: 'loadDocument', documentId: 'one.dxf' })
  // Compare bytes, not typed-array identity: TextEncoder under jsdom returns another realm's Uint8Array.
  expect(Array.from(h.worker.posted[0].bytes)).toEqual(Array.from(b0))
  h.worker.emit(loadedMessage(D))
  h.worker.posted.length = 0
  act(() => h.current.actions.redo())
  expect(h.worker.posted).toHaveLength(1)
  expect(h.worker.posted[0]).toMatchObject({ type: 'loadDocument', bytes: b1 })
})
it('MS28 save-contract', () => {
  for (let i = 0; i < cases.length; i += 1) {
    const plan = diffPlan(D, resultFor(i))
    expect(plan.reason).toBeNull()
    expect(plan.count).toBe(2)
    expect(plan.mutations).toEqual(cases[i][5])
    expect(JSON.stringify(plan.mutations)).not.toMatch(/"handle":"1[12]"/)
  }
})


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

it.each(['selectAdd', 'selectToggle'])('SSD1-24A-b %s replaces a ghost with singleton identity', async (action) => {
  const h = await mountSession()
  act(() => h.current.actions.select('missing'))
  act(() => h.current.actions[action]('7'))
  expectSelection(h, ['7'])
  expect(h.current.selected.id).toBe('7')
})
it('SSD1-24A-b Draw posts exactly one createLine with a set selected', async () => {
  const h = await mountSession()
  selectPair(h)
  h.worker.posted.length = 0
  act(() => h.current.actions.create('createLine', { x: '0', y: '0', x2: '2', y2: '3', layer: '0' }))
  expect(h.worker.posted).toEqual([{ type: 'applyEdit', op: 'createLine', payload: { x1: 0, y1: 0, x2: 2, y2: 3, layer: '0' } }])
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
it.each([['row7', 'move'], ['row8', 'delete']])('SSD1-24A %s %s posts once without changing saved bytes before reply', async (_row, op) => {
  const h = await mountSession()
  h.worker.emit(editedMessage())
  selectPair(h)
  const savedBytes = h.current.savedBytes
  h.worker.posted.length = 0
  act(() => h.current.actions.applyEdit(op, { dx: '1', dy: '0' }))
  expect(h.worker.posted).toEqual([batch(op, ['7', '9'], op === 'move' ? { dx: 1, dy: 0 } : {})])
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
  for (const op of ['delete', 'move', 'copy', 'rotate', 'scale', 'mirror']) expect(modifyOpReason(op, h.current)).toBe('')
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
