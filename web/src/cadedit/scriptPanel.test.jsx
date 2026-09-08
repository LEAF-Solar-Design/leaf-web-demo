// W4g-7a SCRIPT: the runner over the session. Each line posts exactly the
// edit the prompt would, one at a time, waiting for the engine's answer; the
// first refusal (the store's before any post, or the engine's) stops the
// script with the line number and the sentence, and the lines before it stay.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { DEFERRED_REASONS } from '../lib/actionRegistry.js'
import CadEditSurface from './CadEditSurface.jsx'
import EngineRibbonClusters from './EngineRibbonClusters.jsx'
import EngineSessionProvider, { useEngineSessionContext } from './EngineSessionProvider.jsx'
import { LINE_BUDGET_MS } from './ScriptPanel.jsx'
import DraftingRibbon from '../site/DraftingRibbon.jsx'

class ScriptedWorker {
  constructor() { this.posted = []; this.listeners = new Map(); this.terminated = false }
  addEventListener(type, fn) { this.listeners.set(type, fn) }
  removeEventListener(type) { this.listeners.delete(type) }
  postMessage(message) { this.posted.push(message) }
  terminate() { this.terminated = true }
  emit(data) { act(() => { this.listeners.get('message')?.({ data }) }) }
}
const H = { id: '7', handle: '7', index: 0, type: 'LINE', layer: 'A', closed: false, editable: true, vertices: [[0, 0, 0], [10, 0, 0]], radius: null, startDeg: null, endDeg: null }
const L2 = { ...H, id: '8', handle: '8', index: 1, vertices: [[0, 0, 0], [10, 10, 0]] }
const C3 = { id: '9', handle: '9', index: 2, type: 'CIRCLE', layer: 'Round', closed: true, editable: true, vertices: [[10, 10, 0]], radius: 5, startDeg: null, endDeg: null }
function fileOf(name = 'one.dxf') {
  const bytes = new TextEncoder().encode('0\nEOF\n')
  const file = new File([bytes], name, { type: 'application/dxf' })
  file.arrayBuffer = async () => bytes.buffer.slice(0)
  Object.defineProperty(file, 'size', { value: bytes.length })
  return file
}
let context = null
let workers = []
function Probe() { context = useEngineSessionContext(); return null }
function mount() {
  workers = []
  const createWorker = vi.fn(() => { const w = new ScriptedWorker(); workers.push(w); return w })
  render(
    <EngineSessionProvider createWorker={createWorker}>
      <Probe />
      {/* The View tab's seat, as App renders it: an empty cluster with the slot div. */}
      <DraftingRibbon clusters={[{ id: 'script', label: 'Script', kind: 'group', tools: [], extra: <div id="cockpit-script-slot" className="ribbon-cluster-tools" /> }]}>
        <EngineRibbonClusters importOpen={false} onToggleImport={() => {}} panels={['script']} />
      </DraftingRibbon>
      <CadEditSurface enabled />
    </EngineSessionProvider>,
  )
}
async function openAndLoad(entities) {
  await act(async () => {
    fireEvent.change(screen.getByLabelText('DXF file'), { target: { files: [fileOf()] } })
    await Promise.resolve(); await Promise.resolve()
  })
  await waitFor(() => expect(workers.length).toBe(1), { timeout: 5000 })
  workers[0].emit({ type: 'documentLoaded', documentId: 'one.dxf', entities, entityCount: entities.length, unsupported: [] })
}
const posts = () => workers[0].posted.filter((m) => m.type === 'applyEdit')
const status = () => screen.getByTestId('cockpit-script-status')
const runButton = () => screen.getByTestId('cockpit-script-run')
const setScript = (value) => fireEvent.change(screen.getByLabelText('ribbon script'), { target: { value } })
const reply = (op, entities, extra = {}) => workers[0].emit({
  type: 'editApplied', op, ok: true, entities, entityCount: entities.length, bytes: new Uint8Array([48, 10]), byteLength: 2, ...extra,
})

beforeEach(() => {
  globalThis.URL.createObjectURL = vi.fn(() => 'blob:script')
  globalThis.URL.revokeObjectURL = vi.fn()
})
afterEach(() => { cleanup(); context = null; vi.useRealTimers() })

describe('W4g-7a the script runner', () => {
  it('runs named GROUP and UNGROUP without a scalar selection', async () => {
    mount()
    await openAndLoad([H, L2])
    expect(context.session.selectedId).toBe('')
    setScript('GROUP RACK 7 8\nUNGROUP RACK')
    fireEvent.click(runButton())
    expect(posts()).toEqual([{ type: 'applyEdit', op: 'createGroup', payload: { name: 'RACK', members: ['7', '8'] } }])
    const groups = [{ id: '20', name: 'RACK', memberIds: ['7', '8'] }]
    reply('createGroup', [H, L2], { groups, createdId: '20' })
    await waitFor(() => expect(posts()).toHaveLength(2))
    expect(posts()[1]).toEqual({ type: 'applyEdit', op: 'ungroup', payload: { name: 'RACK' } })
    reply('ungroup', [H, L2], { groups: [] })
    expect(status().textContent).toContain('Script ran 2 commands')
  })
  it('holds Run without a document or a script, then runs two lines ONE AT A TIME and reports the count', async () => {
    mount()
    expect(runButton().disabled).toBe(true)
    expect(runButton().getAttribute('aria-label')).toBe('Run script (unavailable: no drawing in the browser engine yet)')
    await openAndLoad([H])
    expect(runButton().getAttribute('aria-label')).toBe('Run script (unavailable: enter or choose a script)')
    setScript('; a line, then a circle\nline 0,0 10,10\ncircle 10,10 5 Round')
    expect(runButton().disabled).toBe(false)
    fireEvent.click(runButton())
    // Line 2 posts only after the engine has answered line 1.
    expect(posts()).toHaveLength(1)
    // The layer left off keeps the prompt's default (the provider's '', the current layer).
    expect(posts()[0]).toEqual({ type: 'applyEdit', op: 'createLine', payload: { x1: 0, y1: 0, x2: 10, y2: 10, layer: '' } })
    // Source line 2 (line 1 is the comment), the first of two commands.
    expect(status().textContent).toBe('Running line 2: LINE (1 of 2)...')
    expect(status().getAttribute('data-phase')).toBe('running')
    expect(screen.getByLabelText('ribbon script').disabled).toBe(true)
    expect(runButton().getAttribute('aria-label')).toBe('Run script (unavailable: a script is running)')
    reply('createLine', [H, L2], { createdId: '8' })
    await waitFor(() => expect(posts()).toHaveLength(2), { timeout: 5000 })
    expect(posts()[1]).toEqual({ type: 'applyEdit', op: 'createCircle', payload: { cx: 10, cy: 10, radius: 5, layer: 'Round' } })
    expect(status().textContent).toBe('Running line 3: CIRCLE (2 of 2)...')
    reply('createCircle', [H, L2, C3], { createdId: '9' })
    await waitFor(() => expect(status().textContent).toBe('Script ran 2 commands.'), { timeout: 5000 })
    expect(status().getAttribute('data-phase')).toBe('done')
    expect(screen.getByLabelText('ribbon script').disabled).toBe(false)
    expect(context.session.entityCount).toBe(3)
    expect(context.session.undoDepth).toBe(2)
  })

  it('a line the STORE refuses stops the script before any post for that line, naming it; the lines before stay', async () => {
    mount()
    await openAndLoad([H])
    setScript('line 0,0 10,10\ncircle 10,10 abc\nline 0,0 5,5')
    fireEvent.click(runButton())
    expect(posts()).toHaveLength(1)
    reply('createLine', [H, L2], { createdId: '8' })
    await waitFor(() => expect(status().textContent).toBe('Script stopped at line 2: Circle refused: x, y and r must all be numbers.'), { timeout: 5000 })
    expect(posts()).toHaveLength(1)
    expect(status().getAttribute('data-phase')).toBe('stopped')
    expect(context.session.entityCount).toBe(2)
  })

  it('a line the ENGINE refuses stops the script with the engine\'s sentence', async () => {
    mount()
    await openAndLoad([H])
    setScript('line 0,0 10,10\nline 5,5 6,6')
    fireEvent.click(runButton())
    expect(posts()).toHaveLength(1)
    workers[0].emit({ type: 'editApplied', op: 'createLine', ok: false, reason: 'line_zero_length' })
    await waitFor(() => expect(status().textContent).toBe('Script stopped at line 1: Edit refused (createLine): line_zero_length'), { timeout: 5000 })
    expect(posts()).toHaveLength(1)
  })

  it('an unreadable script stops before running; a bare word obeys its group gate; a relative first point needs a previous one', async () => {
    mount()
    await openAndLoad([H])
    setScript('line 0,0 10,10\nfoo')
    fireEvent.click(runButton())
    expect(status().textContent).toBe('Script stopped before running: line 2: "foo" is not a command word.')
    expect(posts()).toHaveLength(0)
    setScript('e')
    fireEvent.click(runButton())
    expect(status().textContent).toBe('Script stopped at line 1: ERASE is unavailable (select an entity in the drawing).')
    // COPY and CUT are gated by the SELECTION, like the ribbon, never by the
    // clipboard's contents (kimi, #1049): no selection refuses, a selection
    // with an EMPTY clipboard copies.
    setScript('copyclip')
    fireEvent.click(runButton())
    expect(status().textContent).toBe('Script stopped at line 1: COPYCLIP is unavailable (select an entity in the drawing).')
    act(() => { context.session.actions.select('7') })
    expect(context.session.clipboard).toBeNull()
    setScript('copyclip')
    fireEvent.click(runButton())
    await waitFor(() => expect(status().textContent).toBe('Script ran 1 command.'), { timeout: 5000 })
    expect(context.session.clipboard).not.toBeNull()
    act(() => { context.session.actions.select(null) })
    setScript('u')
    fireEvent.click(runButton())
    expect(status().textContent).toBe('Script stopped at line 1: UNDO is unavailable (nothing to undo).')
    setScript('line @1,1 5,5')
    fireEvent.click(runButton())
    expect(status().textContent).toMatch(/^Script stopped at line 1: LINE refused: /)
    // An operand left off keeps the prompt's default (a radius is never empty
    // by default), but an edge has no default: the step is still waiting.
    act(() => { context.session.actions.select('7') })
    setScript('tr')
    fireEvent.click(runButton())
    expect(status().textContent).toBe('Script stopped at line 1: TRIM still needs "Select cutting edge:"')
    expect(posts()).toHaveLength(0)
  })

  // W4g-7b-05c: a deferred word (LEADER, BLOCK, GROUP, UNGROUP) is a real
  // command word the parser never refuses at; the runner stops AT that line
  // with its own frozen reason, and the LINE before it stays applied.
  it('a deferred word (LEADER) stops the script at its own line with its reason; the LINE before it stays', async () => {
    mount()
    await openAndLoad([H])
    setScript('line 0,0 3,4\nleader')
    fireEvent.click(runButton())
    expect(posts()).toHaveLength(1)
    reply('createLine', [H, L2], { createdId: '8' })
    await waitFor(() => expect(status().textContent).toBe(`Script stopped at line 2: LEADER ${DEFERRED_REASONS.leader}.`), { timeout: 5000 })
    expect(posts()).toHaveLength(1)
    expect(status().getAttribute('data-phase')).toBe('stopped')
    expect(context.session.entityCount).toBe(2)
  })

  it('COPYCLIP is answered the moment it returns, even when its sentence repeats; the same file can be chosen twice', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mount()
    await openAndLoad([H])
    act(() => { context.session.actions.select('7') })
    // The ribbon's own COPY first, so the script's first line repeats its sentence exactly.
    act(() => { context.session.actions.copyToClipboard(false) })
    const sentence = context.session.status
    expect(sentence).toMatch(/clipboard/i)
    setScript('copyclip\ncopyclip\nline 0,0 1,1')
    fireEvent.click(runButton())
    // Both copies answered without the engine, the line posted at once: no 60 s stall.
    await waitFor(() => expect(posts()).toHaveLength(1), { timeout: 5000 })
    expect(posts()[0].op).toBe('createLine')
    expect(status().textContent).toBe('Running line 3: LINE (3 of 3)...')
    expect(context.session.status).toBe(sentence)
    // The same File chosen twice reads twice (the input forgets its value).
    const file = new File(['circle 1,1 2\n'], 'a.scr', { type: 'text/plain' })
    file.text = async () => 'circle 1,1 2\n'
    reply('createLine', [H, L2], { createdId: '8' })
    await waitFor(() => expect(status().getAttribute('data-phase')).toBe('done'), { timeout: 5000 })
    const input = screen.getByLabelText('Script file')
    await act(async () => { fireEvent.change(input, { target: { files: [file] } }); await Promise.resolve(); await Promise.resolve() })
    await waitFor(() => expect(screen.getByLabelText('ribbon script').value).toBe('circle 1,1 2\n'), { timeout: 5000 })
    setScript('')
    await act(async () => { fireEvent.change(input, { target: { files: [file] } }); await Promise.resolve(); await Promise.resolve() })
    await waitFor(() => expect(screen.getByLabelText('ribbon script').value).toBe('circle 1,1 2\n'), { timeout: 5000 })
  })

  it.each([
    ['INSERT', 'an INSERT is placed, not edited, in this round'],
    ['DIMENSION', 'a dimension is placed, not edited, in this round'],
  ])('C1: repeated bare EXPLODE on %s stops immediately on both runs', async (type, sentence) => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mount()
    await openAndLoad([{ ...H, type, editable: false }])
    act(() => { context.session.actions.select('7') })
    const before = workers[0].posted.length
    const applyEdit = vi.spyOn(context.session.actions, 'applyEdit')
    setScript('explode')
    for (let run = 0; run < 2; run += 1) {
      fireEvent.click(runButton())
      expect(status().textContent).toBe(`Script stopped at line 1: ${sentence}`)
      expect(status().getAttribute('data-phase')).toBe('stopped')
      expect(runButton().disabled).toBe(false)
      expect(workers[0].posted).toHaveLength(before)
      expect(applyEdit).not.toHaveBeenCalled()
    }
    applyEdit.mockRestore()
  })

  it('a bare ERASE runs on the selection, and an engine that never answers is stopped by the line budget', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mount()
    await openAndLoad([H])
    act(() => { context.session.actions.select('7') })
    setScript('e\nline 0,0 1,1')
    fireEvent.click(runButton())
    expect(posts()).toHaveLength(1)
    expect(posts()[0]).toEqual({ type: 'applyEdit', op: 'delete', payload: { entityId: '7' } })
    act(() => { vi.advanceTimersByTime(LINE_BUDGET_MS + 10) })
    await waitFor(() => expect(status().textContent).toBe(`Script stopped at line 1: the engine did not answer within ${LINE_BUDGET_MS / 1000} s.`), { timeout: 5000 })
    expect(posts()).toHaveLength(1)
  })

  // W4g-7b: a scripted INSERT validates against the session's block catalogue
  // the same way the typed prompt does (engineSession.js buildCreatePayload),
  // so an undefined block stops the script before any post, exactly like a
  // refused typed command, and a defined one runs with its typed scale and
  // rotation, defaulting layer the way every other create op's line does.
  it('a scripted INSERT posts the typed name, point, scale and rotation, validated against the block catalogue', async () => {
    mount()
    await openAndLoad([H])
    workers[0].emit({
      type: 'documentLoaded', documentId: 'one.dxf', entities: [H], entityCount: 1, unsupported: [],
      blocks: [{ name: 'Fixture', base: [1, 2, 0], children: [{ type: 'LINE', vertices: [[1, 2, 0], [4, 2, 0]] }], complete: true, baseUnknown: false, digest: 'd1' }],
    })
    setScript('insert Fixture 10,20 2 3 90')
    fireEvent.click(runButton())
    expect(posts()).toHaveLength(1)
    expect(posts()[0]).toEqual({ type: 'applyEdit', op: 'createInsert', payload: { name: 'Fixture', x: 10, y: 20, rotationDeg: 90, sx: 2, sy: 3, sz: 1, layer: '' } })
    reply('createInsert', [H], { createdId: '20' })
    await waitFor(() => expect(status().textContent).toBe('Script ran 1 command.'), { timeout: 5000 })
  })

  it('a scripted INSERT never inherits the ribbon\'s own live scale for an omitted operand (record w4g-7b-02c-e F2)', async () => {
    mount()
    await openAndLoad([H])
    workers[0].emit({
      type: 'documentLoaded', documentId: 'one.dxf', entities: [H], entityCount: 1, unsupported: [],
      blocks: [{ name: 'Fixture', base: [1, 2, 0], children: [{ type: 'LINE', vertices: [[1, 2, 0], [4, 2, 0]] }], complete: true, baseUnknown: false, digest: 'd1' }],
    })
    // Type 2 into the ribbon's own X scale field, then run a script line that
    // never mentions a scale: the line must still take the prompt's default
    // (1), never the value sitting in the field.
    act(() => { context.setInput('sx', '2') })
    setScript('insert Fixture 10,20')
    fireEvent.click(runButton())
    expect(posts()).toHaveLength(1)
    expect(posts()[0]).toEqual({ type: 'applyEdit', op: 'createInsert', payload: { name: 'Fixture', x: 10, y: 20, rotationDeg: 0, sx: 1, sy: 1, sz: 1, layer: '' } })
  })

  it('a scripted INSERT naming a block absent from the catalogue stops before any post', async () => {
    mount()
    await openAndLoad([H])
    setScript('insert Nope 10,20')
    fireEvent.click(runButton())
    expect(posts()).toHaveLength(0)
    expect(status().textContent).toMatch(/^Script stopped at line 1: Insert refused: block Nope is not defined in this drawing/)
  })

  it('a canvas pick\'s aperture never reaches a scripted TRIM', async () => {
    mount()
    const V9 = { ...H, id: '9', handle: '9', index: 1, vertices: [[5, -5, 0], [5, 5, 0]] }
    const V11 = { ...H, id: '11', handle: '11', index: 2, vertices: [[6, -5, 0], [6, 5, 0]] }
    await openAndLoad([H, V9, V11])
    act(() => { context.session.actions.select('7') })
    act(() => {
      context.setInput('edge', '9')
      context.setInput('ex', '5')
      context.setInput('ey', '3')
      context.setInput('etol', '0.2')
    })
    expect(context.inputs.etol).toBe('0.2')
    setScript('trim 11 6.02,0')
    fireEvent.click(runButton())
    expect(posts()).toHaveLength(1)
    expect(posts()[0].op).toBe('batch')
    expect(posts()[0].payload.verb).toBe('trim')
    // The store lowers the kept points [[0,0],[6,0]] to a flat wire payload.
    expect(JSON.stringify(posts()[0])).toContain('"points":[0,0,6,0]')
    expect(status().textContent).toBe('Running line 1: TRIM (1 of 1)...')
    expect(context.inputs.etol).toBe('0.2')
  })

  // W4g-7b-04c-3 F1: the script runner dispatches the seat op the same way
  // the ribbon's run() does, so DAL's three points post createDimension, the
  // internal op the worker actually knows, never 'dimAligned' on the wire.
  it('a scripted DAL dispatches the seat op straight through to createDimension', async () => {
    mount()
    await openAndLoad([H])
    workers[0].emit({
      type: 'documentLoaded', documentId: 'one.dxf', entities: [H], entityCount: 1, unsupported: [], dimstyles: ['Standard'],
    })
    setScript('dal 0,0 3,4 1.5,6')
    fireEvent.click(runButton())
    expect(posts()).toHaveLength(1)
    expect(posts()[0]).toEqual({
      type: 'applyEdit', op: 'createDimension',
      payload: { dimtype: 'ALIGNED', x1: 0, y1: 0, x2: 3, y2: 4, dx: 1.5, dy: 6, rotationDeg: 0, style: 'Standard', layer: '' },
    })
  })
})

describe('W4g-7b-03c-g F1: the typed LT word validates against the LOADED linetype catalogue', () => {
  it('a typed lt + Continuous is live and posts, once the session\'s own catalogue lists it', async () => {
    mount()
    await openAndLoad([H])
    // A second documentLoaded, exactly the pattern the scripted-INSERT tests
    // above use to add a block catalogue: this one adds the linetype table.
    workers[0].emit({
      type: 'documentLoaded', documentId: 'one.dxf', entities: [H], entityCount: 1, unsupported: [],
      linetypes: ['ByLayer', 'ByBlock', 'Continuous'],
    })
    act(() => { context.session.actions.select('7') })
    act(() => { context.setArmed({ group: 'modify', op: 'setLinetype' }) })
    const field = screen.getByLabelText('ribbon linetype')
    fireEvent.change(field, { target: { value: 'Continuous' } })
    // Before the fix, the ribbon's live refusal called buildEditPayload with
    // no catalogue argument (defaulting to []), so even a loaded, spelled-
    // right name held Run refused forever.
    expect(screen.queryByTestId('cockpit-prompt-note')).toBeNull()
    const run = screen.getByTestId('cockpit-prompt-run')
    expect(run.disabled).toBe(false)
    fireEvent.click(run)
    expect(posts()).toHaveLength(1)
    expect(posts()[0]).toEqual({ type: 'applyEdit', op: 'setLinetype', payload: { entityId: '7', linetype: 'Continuous' } })
  })
})
