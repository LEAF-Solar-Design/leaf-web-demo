// W4g-7a SCRIPT: the runner over the session. Each line posts exactly the
// edit the prompt would, one at a time, waiting for the engine's answer; the
// first refusal (the store's before any post, or the engine's) stops the
// script with the line number and the sentence, and the lines before it stay.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

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
})
