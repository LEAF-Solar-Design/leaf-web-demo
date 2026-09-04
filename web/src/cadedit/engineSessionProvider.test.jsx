// @vitest-environment jsdom
/**
 * The ONE engine-session mount (W4d Slice A) — the defined states the frozen
 * contract requires before any dock or ribbon work, driven THROUGH the
 * provider with BOTH consumers mounted (the ribbon's engine clusters and the
 * import pane), exactly as the cockpit wires them:
 *
 *   provider construction (one worker, spawned lazily, shared by every
 *   consumer) · worker lifetime (unmount tears it down) · save truth ·
 *   selection identity across an edit · scope reset on a drawing switch ·
 *   worker-crash recovery.
 *
 * The worker is a SCRIPTED TRANSPORT double: the test decides what it
 * answers, and everything asserted is the shipped state machine plus the
 * ribbon's honest gating over it. The real engine is exercised where it
 * already is (cadEditSurface.test.jsx, gated on the machine-local wasm).
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import DraftingRibbon from '../site/DraftingRibbon.jsx'
import {
  DRAWING_MODE_OPERATOR,
  DrawingIdentityProvider,
  useDrawingIdentity,
} from '../drawing/DrawingIdentityProvider.jsx'

import CadEditSurface from './CadEditSurface.jsx'
import EngineRibbonClusters, {
  DRAW_REASONS, MODIFY_REASONS, SAVE_REASONS, drawReason, modifyReason, saveReason,
} from './EngineRibbonClusters.jsx'
import EngineSessionProvider, { MAX_INPUT_CHARS, useEngineSessionContext } from './EngineSessionProvider.jsx'
import { SESSION_ERROR } from './engineSession.js'

afterEach(cleanup)

class ScriptedWorker {
  constructor() {
    this.posted = []
    this.listeners = { message: [], error: [], messageerror: [] }
    this.terminated = false
  }

  addEventListener(type, cb) {
    if (this.listeners[type]) this.listeners[type].push(cb)
  }

  removeEventListener() {}

  postMessage(data) { this.posted.push(data) }

  terminate() { this.terminated = true }

  emit(data) {
    act(() => { this.listeners.message.forEach((cb) => cb({ data })) })
  }

  die() {
    act(() => { this.listeners.error.forEach((cb) => cb({ type: 'error' })) })
  }
}

const LINE = { id: 'e1', type: 'LINE', layer: 'Panels', vertices: [[0, 0], [1, 1]] }
const POLY = { id: 'e2', type: 'LWPOLYLINE', layer: 'Outline', vertices: [[0, 0], [1, 0], [1, 1]] }
const OTHER = { id: 'e3', type: 'OTHER', layer: '0', editable: false, vertices: [] }

function loadedMessage(entities, documentId = 'one.dxf') {
  return { type: 'documentLoaded', documentId, entities, entityCount: entities.length, unsupported: [] }
}

function editApplied(op, entities, bytes = new Uint8Array([48, 10])) {
  return { type: 'editApplied', op, ok: true, entities, entityCount: entities.length, bytes, byteLength: bytes.length }
}

function fileOf(name = 'one.dxf') {
  const bytes = new TextEncoder().encode('0\nEOF\n')
  const file = new File([bytes], name, { type: 'application/dxf' })
  file.arrayBuffer = async () => bytes.buffer.slice(0)
  Object.defineProperty(file, 'size', { value: bytes.length })
  return file
}

// Both consumers under the ONE provider, the cockpit's exact shape.
function Cockpit({ onToggleImport = () => {} }) {
  return (
    <>
      <DraftingRibbon clusters={[]}>
        <EngineRibbonClusters importOpen={false} onToggleImport={onToggleImport} />
      </DraftingRibbon>
      <CadEditSurface enabled />
    </>
  )
}

function mount({ saveTarget = null, onSaved = null, withIdentity = false } = {}) {
  const workers = []
  const createWorker = vi.fn(() => {
    const worker = new ScriptedWorker()
    workers.push(worker)
    return worker
  })
  const handle = { workers, createWorker }

  function Probe() {
    handle.context = useEngineSessionContext()
    if (withIdentity) handle.identity = useDrawingIdentity()
    return null
  }

  const tree = (
    <EngineSessionProvider createWorker={createWorker} saveTarget={saveTarget} onSaved={onSaved}>
      <Probe />
      <Cockpit />
    </EngineSessionProvider>
  )
  const utils = render(withIdentity ? (
    <DrawingIdentityProvider
      mode={DRAWING_MODE_OPERATOR}
      search=""
      publicDemo={false}
      liveDemo={false}
      readLiveDrawingId={() => null}
      rememberDrawingId={() => true}
      readAuthToken={() => null}
      subscribeAuthChange={() => () => {}}
    >
      {tree}
    </DrawingIdentityProvider>
  ) : tree)
  handle.unmount = utils.unmount
  return handle
}

async function openAndLoad(studio, entities = [LINE], name = 'one.dxf') {
  await act(async () => {
    fireEvent.change(screen.getByLabelText('DXF file'), { target: { files: [fileOf(name)] } })
    // The store reads the file (async) before it spawns; let that settle.
    await Promise.resolve()
    await Promise.resolve()
  })
  await waitFor(() => expect(studio.workers.length).toBeGreaterThan(0))
  studio.workers[studio.workers.length - 1].emit(loadedMessage(entities, name))
}

const ribbonTool = (op) => document.querySelector(`.drafting-ribbon [data-tool="modify:${op}"]`)
const modifyNote = () => document.querySelector('.drafting-ribbon [data-group="modify"] .ribbon-note')
const saveTool = () => document.querySelector('.drafting-ribbon [data-tool="save-version"]')
// W4e slice H: a tool with operands ARMS on click and the command line
// prompts for them; Run (or Enter in a field) fires the edit. `delete` has
// no operands and still runs on click.
const runPrompt = () => fireEvent.click(screen.getByTestId('cockpit-prompt-run'))
const armAndRun = (op) => { fireEvent.click(ribbonTool(op)); runPrompt() }

beforeEach(() => {
  globalThis.URL.createObjectURL = vi.fn(() => 'blob:cad-edit-test')
  globalThis.URL.revokeObjectURL = vi.fn()
})

describe('provider construction: one session, one worker, every consumer', () => {
  it('spawns nothing at mount and states the Modify group is unavailable until a DXF is imported', () => {
    const studio = mount()
    expect(studio.createWorker).not.toHaveBeenCalled()
    expect(modifyNote().textContent).toBe(MODIFY_REASONS.noDocument)
    for (const op of ['delete', 'move', 'moveVertex', 'addVertex', 'deleteVertex', 'setLayer']) {
      const btn = ribbonTool(op)
      expect(btn.disabled).toBe(true)
      expect(btn.getAttribute('aria-label')).toContain(`(unavailable: ${MODIFY_REASONS.noDocument})`)
      expect(btn.title).toBe(MODIFY_REASONS.noDocument)
    }
    expect(saveTool().disabled).toBe(true)
    expect(saveTool().title).toBe(SAVE_REASONS.noDocument)
  })

  it('the import pane and the ribbon share ONE worker; a ribbon edit rides the same session', async () => {
    const studio = mount()
    await openAndLoad(studio, [LINE, POLY])
    expect(studio.createWorker).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('cad-edit-entity-count').textContent).toBe('2')
    // Loaded, nothing selected: the ribbon names the next thing missing.
    expect(modifyNote().textContent).toBe(MODIFY_REASONS.noSelection)
    fireEvent.click(screen.getAllByRole('radio')[0])
    expect(modifyNote()).toBeNull()
    expect(ribbonTool('delete').disabled).toBe(false)
    fireEvent.click(ribbonTool('delete'))
    const posted = studio.workers[0].posted
    expect(posted[posted.length - 1]).toEqual({ type: 'applyEdit', op: 'delete', payload: { entityId: 'e1' } })
    expect(studio.createWorker).toHaveBeenCalledTimes(1)
  })

  it('the operator inputs are ONE record: the ribbon field and the pane field agree, and edits carry it', async () => {
    const studio = mount()
    await openAndLoad(studio, [LINE])
    fireEvent.click(screen.getByRole('radio'))
    // The pane's fields by their own aria-labels (the prompt's carry a
    // "ribbon " prefix, and both sets are live at once by design once the
    // move is armed).
    const paneInput = (name) => document.querySelector(`.cad-edit-workbench-ops input[aria-label="${name}"]`)
    fireEvent.click(ribbonTool('move'))
    fireEvent.change(screen.getByLabelText('ribbon dx'), { target: { value: '7' } })
    fireEvent.change(paneInput('dy'), { target: { value: '3' } })
    expect(paneInput('dx').value).toBe('7')
    expect(screen.getByLabelText('ribbon dy').value).toBe('3')
    runPrompt()
    const posted = studio.workers[0].posted
    expect(posted[posted.length - 1]).toEqual({ type: 'applyEdit', op: 'move', payload: { entityId: 'e1', dx: 7, dy: 3 } })
    // Bounded: a paste of a whole file into dx costs a slice, never a 16 MB render.
    fireEvent.change(screen.getByLabelText('ribbon dx'), { target: { value: 'x'.repeat(MAX_INPUT_CHARS * 4) } })
    expect(paneInput('dx').value).toHaveLength(MAX_INPUT_CHARS)
  })

  it('a read-only entity kind is named as the reason, not greyed silently', async () => {
    const studio = mount()
    await openAndLoad(studio, [OTHER])
    act(() => { studio.context.session.actions.select('e3') })
    expect(modifyNote().textContent).toBe(MODIFY_REASONS.readOnlyKind)
  })
})

describe('worker lifetime', () => {
  it('unmounting the provider terminates the worker', async () => {
    const studio = mount()
    await openAndLoad(studio)
    expect(studio.workers[0].terminated).toBe(false)
    studio.unmount()
    expect(studio.workers[0].terminated).toBe(true)
  })

  it('a busy engine names itself as the reason while an edit is in flight', async () => {
    const studio = mount()
    await openAndLoad(studio)
    fireEvent.click(screen.getByRole('radio'))
    armAndRun('move')
    expect(modifyNote().textContent).toBe(MODIFY_REASONS.busy)
    // The prompt says so too, and refuses a second run while in flight.
    expect(screen.getByTestId('cockpit-prompt').textContent).toContain(MODIFY_REASONS.busy)
    expect(screen.getByTestId('cockpit-prompt-run').disabled).toBe(true)
    studio.workers[0].emit(editApplied('move', [LINE]))
    expect(modifyNote()).toBeNull()
    expect(screen.getByTestId('cockpit-prompt-run').disabled).toBe(false)
  })
})

describe('save truth', () => {
  it('save-version is unavailable until the engine wrote bytes, then calls the target with the digest', async () => {
    const save = vi.fn(async (bytes, parent, digest) => ({
      new_version: { drawing_id: 'rooftop', version: 5, parent: 4 }, head: 5, source_sha256: digest,
    }))
    const onSaved = vi.fn()
    const studio = mount({ saveTarget: { drawingId: 'rooftop', headVersion: 4, save }, onSaved })
    await openAndLoad(studio)
    expect(saveTool().title).toBe(SAVE_REASONS.nothingEdited)
    fireEvent.click(screen.getByRole('radio'))
    armAndRun('move')
    const bytes = new Uint8Array([49, 10, 50, 10])
    studio.workers[0].emit(editApplied('move', [LINE], bytes))
    expect(saveTool().disabled).toBe(false)
    await act(async () => { fireEvent.click(saveTool()) })
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Saved as version 5'))
    expect(save).toHaveBeenCalledTimes(1)
    const [sentBytes, parent, digest] = save.mock.calls[0]
    expect(sentBytes).toBe(bytes)
    expect(parent).toBe(4)
    expect(digest).toMatch(/^[0-9a-f]{64}$/)
    expect(onSaved).toHaveBeenCalledTimes(1)
  })

  it('without a project target the ribbon says download-only rather than offering a dead button', async () => {
    const studio = mount()
    await openAndLoad(studio)
    fireEvent.click(screen.getByRole('radio'))
    armAndRun('move')
    studio.workers[0].emit(editApplied('move', [LINE]))
    expect(saveTool().disabled).toBe(true)
    expect(saveTool().title).toBe(SAVE_REASONS.noTarget)
    expect(screen.queryByTestId('cad-edit-save-version')).toBeNull()
  })
})

describe('selection identity across an edit', () => {
  it('survives when the entity survives the re-parse, clears when it does not — and the ribbon follows', async () => {
    const studio = mount()
    await openAndLoad(studio, [LINE, POLY])
    fireEvent.click(screen.getAllByRole('radio')[0])
    armAndRun('move')
    studio.workers[0].emit(editApplied('move', [{ ...LINE, vertices: [[7, 3], [8, 4]] }, POLY]))
    expect(studio.context.session.selectedId).toBe('e1')
    expect(ribbonTool('delete').disabled).toBe(false)
    fireEvent.click(ribbonTool('delete'))
    studio.workers[0].emit(editApplied('delete', [POLY]))
    expect(studio.context.session.selectedId).toBe('')
    expect(modifyNote().textContent).toBe(MODIFY_REASONS.noSelection)
    expect(ribbonTool('delete').disabled).toBe(true)
  })
})

describe('scope reset on a drawing switch', () => {
  it('a drawing-identity move resets the session, tears the worker down, and the ribbon reads honest-empty again', async () => {
    const studio = mount({ withIdentity: true })
    await openAndLoad(studio, [LINE], 'before.dxf')
    fireEvent.click(screen.getByRole('radio'))
    expect(ribbonTool('delete').disabled).toBe(false)
    act(() => { studio.identity.setFromUpload({ drawing_id: 'u-guest-1', tenant_kind: 'guest' }) })
    expect(studio.workers[0].terminated).toBe(true)
    expect(studio.context.session.documentId).toBe('')
    expect(studio.context.session.engineParsed).toBe(false)
    expect(modifyNote().textContent).toBe(MODIFY_REASONS.noDocument)
    // A reply that raced the switch never seats state under the new drawing.
    studio.workers[0].emit(loadedMessage([LINE, POLY], 'before.dxf'))
    expect(studio.context.session.entityCount).toBe(0)
  })
})

describe('worker-crash recovery', () => {
  it('a dead worker is named as the reason, and the next open spawns a fresh one', async () => {
    const studio = mount()
    await openAndLoad(studio)
    fireEvent.click(screen.getByRole('radio'))
    studio.workers[0].die()
    expect(studio.context.session.errorKind).toBe(SESSION_ERROR.CRASHED)
    expect(studio.context.session.recoverable).toBe(true)
    expect(modifyNote().textContent).toBe(MODIFY_REASONS.crashed)
    expect(ribbonTool('delete').disabled).toBe(true)
    await openAndLoad(studio, [LINE], 'again.dxf')
    expect(studio.createWorker).toHaveBeenCalledTimes(2)
    expect(studio.workers[1].terminated).toBe(false)
    expect(modifyNote().textContent).toBe(MODIFY_REASONS.noSelection)
  })
})

describe('the Draw group (W4d Slice B): creation from the ribbon, selection lands on what was drawn', () => {
  const drawTool = (op) => document.querySelector(`.drafting-ribbon [data-tool="draw:${op}"]`)
  const drawNote = () => document.querySelector('.drafting-ribbon [data-group="draw"] .ribbon-note')

  it('is unavailable with the reason until a DXF is imported, then live without any selection', async () => {
    const studio = mount()
    expect(drawNote().textContent).toBe(DRAW_REASONS.noDocument)
    for (const op of ['createLine', 'createPolyline', 'createCircle', 'createArc']) {
      expect(drawTool(op).disabled).toBe(true)
      expect(drawTool(op).title).toBe(DRAW_REASONS.noDocument)
    }
    // Nothing armed, nothing asked for: the command line carries no prompt.
    expect(screen.queryByTestId('cockpit-prompt')).toBeNull()
    expect(screen.queryByLabelText('ribbon x')).toBeNull()
    await openAndLoad(studio, [LINE])
    expect(drawNote()).toBeNull()
    expect(drawTool('createLine').disabled).toBe(false)
    fireEvent.click(drawTool('createLine'))
    expect(screen.getByLabelText('ribbon x').disabled).toBe(false)
    // Modify still wants a selection; Draw does not — different ladders.
    expect(modifyNote().textContent).toBe(MODIFY_REASONS.noSelection)
  })

  it('posts the create with the typed operands, and the reply seats the selection on the new entity', async () => {
    const studio = mount()
    await openAndLoad(studio, [LINE])
    fireEvent.click(drawTool('createLine'))
    fireEvent.change(screen.getByLabelText('ribbon x2'), { target: { value: '40' } })
    fireEvent.change(screen.getByLabelText('ribbon y2'), { target: { value: '30' } })
    fireEvent.change(screen.getByLabelText('ribbon layer'), { target: { value: 'Sketch' } })
    fireEvent.keyDown(screen.getByLabelText('ribbon layer'), { key: 'Enter' })
    const posted = studio.workers[0].posted
    expect(posted[posted.length - 1]).toEqual({
      type: 'applyEdit', op: 'createLine', payload: { x1: 0, y1: 0, x2: 40, y2: 30, layer: 'Sketch' },
    })
    expect(drawNote().textContent).toBe(DRAW_REASONS.busy)
    // In flight: the prompt's fields and Run are off with the same sentence.
    expect(screen.getByLabelText('ribbon x2').disabled).toBe(true)
    expect(screen.getByTestId('cockpit-prompt').textContent).toContain(DRAW_REASONS.busy)
    const drawn = { id: 'e9', type: 'LINE', layer: 'Sketch', vertices: [[0, 0], [40, 30]] }
    studio.workers[0].emit({ ...editApplied('createLine', [LINE, drawn]), createdId: 'e9' })
    expect(studio.context.session.selectedId).toBe('e9')
    expect(screen.getAllByRole('radio')[1].checked).toBe(true)
    // Drawn, selected: Modify is live on it immediately; the command stays
    // armed for the next segment, fields live again.
    expect(modifyNote()).toBeNull()
    expect(ribbonTool('delete').disabled).toBe(false)
    expect(screen.getByRole('status').textContent).toMatch(/entity e9 drawn/)
    expect(screen.getByTestId('cockpit-prompt').getAttribute('data-op')).toBe('createLine')
    expect(screen.getByLabelText('ribbon x2').disabled).toBe(false)
  })

  it('the closed flag and the point list ride the polyline create; the circle and arc take the radius and angles', async () => {
    const studio = mount()
    await openAndLoad(studio, [LINE])
    fireEvent.click(drawTool('createPolyline'))
    fireEvent.change(screen.getByLabelText('ribbon points'), { target: { value: '0,0 10,0 10,4' } })
    fireEvent.click(screen.getByLabelText('ribbon closed'))
    runPrompt()
    let posted = studio.workers[0].posted
    expect(posted[posted.length - 1].payload).toEqual({ points: [0, 0, 10, 0, 10, 4], closed: true, layer: '' })
    studio.workers[0].emit({ ...editApplied('createPolyline', [LINE, POLY]), createdId: 'e2' })
    fireEvent.click(drawTool('createCircle'))
    fireEvent.change(screen.getByLabelText('ribbon r'), { target: { value: '2.5' } })
    runPrompt()
    posted = studio.workers[0].posted
    expect(posted[posted.length - 1].payload).toEqual({ cx: 0, cy: 0, radius: 2.5, layer: '' })
    studio.workers[0].emit({ ...editApplied('createCircle', [LINE, POLY]), createdId: 'e1' })
    fireEvent.click(drawTool('createArc'))
    fireEvent.change(screen.getByLabelText('ribbon end'), { target: { value: '180' } })
    runPrompt()
    posted = studio.workers[0].posted
    expect(posted[posted.length - 1].payload).toEqual({ cx: 0, cy: 0, radius: 2.5, startDeg: 0, endDeg: 180, layer: '' })
  })

  it('a malformed operand is a sentence on the prompt, never a message on the wire', async () => {
    const studio = mount()
    await openAndLoad(studio, [LINE])
    const before = studio.workers[0].posted.length
    fireEvent.click(drawTool('createCircle'))
    fireEvent.change(screen.getByLabelText('ribbon r'), { target: { value: '0' } })
    // W4f-6: the store's sentence shows as the operand is typed and Run
    // waits; a click on Run (or Enter) posts nothing.
    expect(screen.getByTestId('cockpit-prompt-note').textContent).toMatch(/Circle refused: r must be greater than 0/)
    expect(screen.getByTestId('cockpit-prompt-run').disabled).toBe(true)
    runPrompt()
    fireEvent.keyDown(screen.getByLabelText('ribbon r'), { key: 'Enter' })
    expect(studio.workers[0].posted.length).toBe(before)
    expect(drawTool('createCircle').disabled).toBe(false)
  })
})

describe('the command prompt (W4e slice H): a tool arms, the command line asks in the reference grammar, Enter runs, Esc cancels', () => {
  const drawTool = (op) => document.querySelector(`.drafting-ribbon [data-tool="draw:${op}"]`)
  const promptEl = () => screen.queryByTestId('cockpit-prompt')

  it('arms on click with the verb and its "Specify" steps, toggles off on a second click, Esc cancels back to the tool', async () => {
    const studio = mount()
    await openAndLoad(studio, [LINE])
    expect(promptEl()).toBeNull()
    fireEvent.click(drawTool('createLine'))
    const row = promptEl()
    expect(row).not.toBeNull()
    expect(row.getAttribute('data-op')).toBe('createLine')
    expect(row.textContent).toContain('LINE')
    expect(row.textContent).toContain('Specify first point:')
    expect(row.textContent).toContain('Specify next point:')
    // The armed tool owns the prompt (aria-expanded + aria-controls); the
    // others are plain collapsed commands.
    expect(drawTool('createLine').getAttribute('aria-expanded')).toBe('true')
    expect(drawTool('createLine').getAttribute('aria-controls')).toBe('cockpit-prompt')
    expect(drawTool('createCircle').getAttribute('aria-expanded')).toBe('false')
    expect(drawTool('createCircle').getAttribute('aria-controls')).toBeNull()
    // Only the armed command's operands are asked for, and the caret is in
    // the first one the way a command line takes typing at once.
    expect(screen.queryByLabelText('ribbon r')).toBeNull()
    expect(document.activeElement).toBe(screen.getByLabelText('ribbon x'))
    // Another tool re-arms; the same tool toggles off.
    fireEvent.click(drawTool('createCircle'))
    expect(promptEl().getAttribute('data-op')).toBe('createCircle')
    expect(promptEl().textContent).toContain('CIRCLE')
    expect(promptEl().textContent).toContain('Specify radius:')
    expect(screen.getByLabelText('ribbon r')).toBeTruthy()
    fireEvent.click(drawTool('createCircle'))
    expect(promptEl()).toBeNull()
    expect(drawTool('createCircle').getAttribute('aria-expanded')).toBe('false')
    // Esc cancels and hands focus back to the tool that armed the command,
    // and the prompt OWNS that Esc: App's window-level Esc rung (drawers,
    // routes) never sees the same keypress.
    fireEvent.click(drawTool('createArc'))
    const windowEsc = vi.fn()
    window.addEventListener('keydown', windowEsc)
    fireEvent.keyDown(screen.getByLabelText('ribbon r'), { key: 'Escape' })
    window.removeEventListener('keydown', windowEsc)
    expect(windowEsc).not.toHaveBeenCalled()
    expect(promptEl()).toBeNull()
    expect(document.activeElement).toBe(drawTool('createArc'))
    // Arming and cancelling never touched the engine.
    expect(studio.workers[0].posted.filter((message) => message.type === 'applyEdit')).toHaveLength(0)
  })

  it('Esc cancels the armed command from anywhere but a foreign text field, and the caret returns to the prompt after a run (W4f-2)', async () => {
    const studio = mount()
    await openAndLoad(studio, [LINE])
    fireEvent.click(drawTool('createLine'))
    expect(document.activeElement).toBe(screen.getByLabelText('ribbon x'))
    // Focus wandered to the body (a click on the drawing, a button that went
    // disabled): Esc still cancels, and App's window-level rung never sees it.
    screen.getByLabelText('ribbon x').blur()
    expect(document.activeElement).toBe(document.body)
    const windowEsc = vi.fn()
    window.addEventListener('keydown', windowEsc)
    fireEvent.keyDown(document.body, { key: 'Escape' })
    window.removeEventListener('keydown', windowEsc)
    expect(windowEsc).not.toHaveBeenCalled()
    expect(promptEl()).toBeNull()
    // With nothing armed the rung is gone: a stray Esc reaches the window.
    fireEvent.keyDown(document.body, { key: 'Escape' })
    // A text field outside the prompt keeps its own Esc (the Command bar
    // clearing itself): the armed command stays.
    fireEvent.click(drawTool('createLine'))
    const foreign = document.createElement('input')
    document.body.appendChild(foreign)
    foreign.focus()
    fireEvent.keyDown(foreign, { key: 'Escape' })
    expect(promptEl()).not.toBeNull()
    foreign.remove()
    // An open dialog owns its Esc too (the DetailsDrawer parks focus on its
    // close BUTTON and closes on Esc): the key reaches the dialog's own
    // handler and the armed command stays (kimi blocker on #976).
    const layer = document.createElement('div')
    layer.className = 'drawer-layer'
    const dialog = document.createElement('aside')
    dialog.setAttribute('role', 'dialog')
    dialog.setAttribute('aria-modal', 'true')
    const close = document.createElement('button')
    close.type = 'button'
    dialog.appendChild(close)
    layer.appendChild(dialog)
    document.body.appendChild(layer)
    const dialogEsc = vi.fn()
    dialog.addEventListener('keydown', dialogEsc)
    close.focus()
    fireEvent.keyDown(close, { key: 'Escape' })
    expect(dialogEsc).toHaveBeenCalledTimes(1)
    expect(dialogEsc.mock.calls[0][0].defaultPrevented).toBe(false)
    expect(promptEl()).not.toBeNull()
    layer.remove()
    // A run: Enter in a field posts the edit; the engine is busy (fields and
    // Run disabled) and the browser drops focus; the reply brings the caret
    // back to the prompt. W4f-3: LINE chains, so the segment's end becomes
    // the next first point, the armed command carries it as the chain point
    // (the picker's rubber band starts there), and the caret waits in x2.
    fireEvent.change(screen.getByLabelText('ribbon x2'), { target: { value: '5' } })
    fireEvent.change(screen.getByLabelText('ribbon y2'), { target: { value: '7' } })
    fireEvent.keyDown(screen.getByLabelText('ribbon x2'), { key: 'Enter' })
    expect(studio.context.session.busy).toBe(true)
    expect(screen.getByLabelText('ribbon x').disabled).toBe(true)
    expect(screen.getByTestId('cockpit-prompt-run').disabled).toBe(true)
    screen.getByLabelText('ribbon x2').blur()
    expect(document.activeElement).toBe(document.body)
    // (a create reports what it drew by id; without it the store reads the
    // create as lost and, rightly, nothing chains)
    studio.workers[0].emit({ ...editApplied('createLine', [LINE, { ...LINE, id: 'e9' }]), createdId: 'e9' })
    expect(studio.context.session.busy).toBe(false)
    expect(promptEl().getAttribute('data-op')).toBe('createLine')
    expect(screen.getByLabelText('ribbon x').value).toBe('5')
    expect(screen.getByLabelText('ribbon y').value).toBe('7')
    expect(studio.context.armed).toEqual({ group: 'draw', op: 'createLine', from: [5, 7] })
    expect(document.activeElement).toBe(screen.getByLabelText('ribbon x2'))
    // The next point is not given yet: empty fields, Run waits quietly with
    // the step's ask, no sentence (W4f-6).
    expect(screen.getByLabelText('ribbon x2').value).toBe('')
    expect(screen.getByLabelText('ribbon y2').value).toBe('')
    expect(screen.queryByTestId('cockpit-prompt-note')).toBeNull()
    expect(screen.getByTestId('cockpit-prompt-run').disabled).toBe(true)
    expect(screen.getByTestId('cockpit-prompt-run').title).toBe('Specify next point:')
    fireEvent.change(screen.getByLabelText('ribbon y2'), { target: { value: '7' } })
    // The Command bar (any field outside the prompt) holding the focus when
    // the engine answers keeps it; the chain still applies.
    fireEvent.change(screen.getByLabelText('ribbon x2'), { target: { value: '9' } })
    fireEvent.keyDown(screen.getByLabelText('ribbon x2'), { key: 'Enter' })
    expect(studio.context.session.busy).toBe(true)
    const bar = document.createElement('input')
    document.body.appendChild(bar)
    bar.focus()
    studio.workers[0].emit({ ...editApplied('createLine', [LINE, { ...LINE, id: 'e9' }, { ...LINE, id: 'e10' }]), createdId: 'e10' })
    expect(document.activeElement).toBe(bar)
    expect(screen.getByLabelText('ribbon x').value).toBe('9')
    expect(studio.context.armed.from).toEqual([9, 7])
    bar.remove()
  })

  it('a refused LINE chains nothing, a CIRCLE never chains, and a malformed chain point is dropped (W4f-3)', async () => {
    const studio = mount()
    await openAndLoad(studio, [LINE])
    fireEvent.click(drawTool('createLine'))
    fireEvent.change(screen.getByLabelText('ribbon x2'), { target: { value: '5' } })
    fireEvent.keyDown(screen.getByLabelText('ribbon x2'), { key: 'Enter' })
    studio.workers[0].emit({ type: 'editApplied', op: 'createLine', ok: false, reason: 'degenerate' })
    expect(studio.context.session.busy).toBe(false)
    expect(screen.getByLabelText('ribbon x').value).toBe('0')
    expect(studio.context.armed).toEqual({ group: 'draw', op: 'createLine' })
    expect(document.activeElement).toBe(screen.getByLabelText('ribbon x'))
    // A circle run leaves its centre alone.
    fireEvent.click(drawTool('createCircle'))
    fireEvent.change(screen.getByLabelText('ribbon r'), { target: { value: '4' } })
    fireEvent.keyDown(screen.getByLabelText('ribbon r'), { key: 'Enter' })
    studio.workers[0].emit({ ...editApplied('createCircle', [LINE, { id: 'c1', type: 'CIRCLE', layer: '0', vertices: [[0, 0]] }]), createdId: 'c1' })
    expect(studio.context.session.errorKind).toBeNull()
    expect(studio.context.armed).toEqual({ group: 'draw', op: 'createCircle' })
    expect(document.activeElement).toBe(screen.getByLabelText('ribbon x'))
    // The provider stores only a well-formed chain point.
    act(() => { studio.context.setArmed({ group: 'draw', op: 'createLine', from: ['1', 2] }) })
    expect(studio.context.armed).toEqual({ group: 'draw', op: 'createLine' })
    act(() => { studio.context.setArmed({ group: 'draw', op: 'createLine', from: [1, Infinity] }) })
    expect(studio.context.armed).toEqual({ group: 'draw', op: 'createLine' })
    act(() => { studio.context.setArmed({ group: 'draw', op: 'createLine', from: [1.5, -2] }) })
    expect(studio.context.armed).toEqual({ group: 'draw', op: 'createLine', from: [1.5, -2] })
  })

  it('the prompt carries the ORTHO toggle: pressed state from the provider, a click flips it, the setter takes only true (W4f-4)', async () => {
    const studio = mount()
    await openAndLoad(studio, [LINE])
    fireEvent.click(drawTool('createLine'))
    const chip = screen.getByTestId('cockpit-ortho')
    expect(chip.getAttribute('aria-pressed')).toBe('false')
    expect(studio.context.ortho).toBe(false)
    fireEvent.click(chip)
    expect(studio.context.ortho).toBe(true)
    expect(screen.getByTestId('cockpit-ortho').getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByTestId('cockpit-ortho').title).toContain('Ortho on')
    // The mode outlives the command: cancel, re-arm, still on.
    fireEvent.keyDown(screen.getByLabelText('ribbon x'), { key: 'Escape' })
    fireEvent.click(drawTool('createCircle'))
    expect(screen.getByTestId('cockpit-ortho').getAttribute('aria-pressed')).toBe('true')
    act(() => { studio.context.setOrtho('yes') })
    expect(studio.context.ortho).toBe(false)
    act(() => { studio.context.setOrtho(true) })
    expect(studio.context.ortho).toBe(true)
    // Clicking the chip never runs or cancels the command.
    fireEvent.click(screen.getByTestId('cockpit-ortho'))
    expect(promptEl().getAttribute('data-op')).toBe('createCircle')
    expect(studio.workers[0].posted.filter((message) => message.type === 'applyEdit')).toHaveLength(0)
  })

  it('the prompt carries the OSNAP toggle too: pressed state from the provider, a click flips it, the setter takes only true (W4f-5)', async () => {
    const studio = mount()
    await openAndLoad(studio, [LINE])
    fireEvent.click(drawTool('createLine'))
    const chip = screen.getByTestId('cockpit-osnap')
    expect(chip.getAttribute('aria-pressed')).toBe('false')
    expect(studio.context.osnap).toBe(false)
    fireEvent.click(chip)
    expect(studio.context.osnap).toBe(true)
    expect(screen.getByTestId('cockpit-osnap').getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByTestId('cockpit-osnap').title).toContain('Object snap on')
    act(() => { studio.context.setOsnap('yes') })
    expect(studio.context.osnap).toBe(false)
    // Independent of ORTHO.
    act(() => { studio.context.setOsnap(true) })
    expect(studio.context.ortho).toBe(false)
    expect(promptEl().getAttribute('data-op')).toBe('createLine')
  })

  it('the prompt validates as you type with the store\'s own sentence: the bad field is outlined, Run waits, Enter does nothing, a fix releases it (W4f-6)', async () => {
    const studio = mount()
    await openAndLoad(studio, [LINE, POLY])
    fireEvent.click(drawTool('createLine'))
    const note = () => screen.queryByTestId('cockpit-prompt-note')
    const run = () => screen.getByTestId('cockpit-prompt-run')
    // Defaults read as a line: nothing to say, Run live.
    fireEvent.change(screen.getByLabelText('ribbon x2'), { target: { value: '40' } })
    expect(note()).toBeNull()
    expect(run().disabled).toBe(false)
    // A word where a number belongs: the store's refusal, live, on the
    // field and on Run; Enter posts nothing.
    fireEvent.change(screen.getByLabelText('ribbon x'), { target: { value: 'abc' } })
    expect(note().textContent).toBe('Line refused: x, y, x2 and y2 must all be numbers.')
    expect(screen.getByLabelText('ribbon x').getAttribute('aria-invalid')).toBe('true')
    expect(screen.getByLabelText('ribbon y').getAttribute('aria-invalid')).toBeNull()
    expect(run().disabled).toBe(true)
    expect(run().getAttribute('aria-label')).toBe('Run (unavailable: Line refused: x, y, x2 and y2 must all be numbers.)')
    fireEvent.keyDown(screen.getByLabelText('ribbon x'), { key: 'Enter' })
    expect(studio.workers[0].posted.filter((message) => message.type === 'applyEdit')).toHaveLength(0)
    // Numbers that make a degenerate line: the sentence names it, no field
    // is outlined (they all read as numbers).
    fireEvent.change(screen.getByLabelText('ribbon x'), { target: { value: '40' } })
    fireEvent.change(screen.getByLabelText('ribbon y'), { target: { value: '30' } })
    fireEvent.change(screen.getByLabelText('ribbon y2'), { target: { value: '30' } })
    expect(note().textContent).toBe('Line refused: the two points must differ.')
    expect(screen.getByLabelText('ribbon x').getAttribute('aria-invalid')).toBeNull()
    expect(run().disabled).toBe(true)
    // An empty numeric field is a step still waiting, not a mistake: no
    // sentence, no outline, Run waits with that step's ask.
    fireEvent.change(screen.getByLabelText('ribbon x2'), { target: { value: '' } })
    expect(note()).toBeNull()
    expect(screen.getByLabelText('ribbon x2').getAttribute('aria-invalid')).toBeNull()
    expect(run().disabled).toBe(true)
    expect(run().title).toBe('Specify next point:')
    fireEvent.keyDown(screen.getByLabelText('ribbon x2'), { key: 'Enter' })
    expect(studio.workers[0].posted.filter((message) => message.type === 'applyEdit')).toHaveLength(0)
    // The fix releases Run and Enter runs.
    fireEvent.change(screen.getByLabelText('ribbon x2'), { target: { value: '50' } })
    expect(note()).toBeNull()
    expect(run().disabled).toBe(false)
    fireEvent.keyDown(screen.getByLabelText('ribbon x2'), { key: 'Enter' })
    const posted = studio.workers[0].posted
    expect(posted[posted.length - 1].type).toBe('applyEdit')
    expect(posted[posted.length - 1].op).toBe('createLine')
    studio.workers[0].emit({ ...editApplied('createLine', [LINE, POLY, { ...LINE, id: 'e9' }]), createdId: 'e9' })
    // A modify command judged the same way (the create selected what it
    // drew, so the group is live): the operand sentence, live.
    fireEvent.keyDown(screen.getByLabelText('ribbon x2'), { key: 'Escape' })
    act(() => { studio.context.setArmed({ group: 'modify', op: 'move' }) })
    expect(studio.context.session.selectedId).toBe('e9')
    expect(note()).toBeNull()
    fireEvent.change(screen.getByLabelText('ribbon dx'), { target: { value: 'zz' } })
    expect(note().textContent).toBe('Move refused: dx and dy must both be numbers.')
    expect(screen.getByLabelText('ribbon dx').getAttribute('aria-invalid')).toBe('true')
    expect(run().disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('ribbon dx'), { target: { value: '2' } })
    expect(note()).toBeNull()
    expect(run().disabled).toBe(false)
  })

  it('an open dialog owns Esc even when its opener kept focus outside the layer', async () => {
    const studio = mount()
    await openAndLoad(studio, [LINE])
    fireEvent.click(drawTool('createLine'))
    expect(promptEl()?.getAttribute('data-op')).toBe('createLine')

    // Version history opens without moving focus into its dialog. Model that
    // exact integration shape: the opener remains the key target while App's
    // bubble listener owns closing the visible layer.
    const opener = document.createElement('button')
    document.body.appendChild(opener)
    opener.focus()
    const dialog = document.createElement('aside')
    dialog.setAttribute('role', 'dialog')
    dialog.setAttribute('aria-label', 'Version history')
    dialog.setAttribute('data-escape-owner', '')
    document.body.appendChild(dialog)
    const appEsc = vi.fn(() => dialog.remove())
    window.addEventListener('keydown', appEsc)

    fireEvent.keyDown(opener, { key: 'Escape' })

    window.removeEventListener('keydown', appEsc)
    expect(appEsc).toHaveBeenCalledTimes(1)
    expect(dialog.isConnected).toBe(false)
    expect(promptEl()?.getAttribute('data-op')).toBe('createLine')

    // A nonmodal dialog such as the guided tour does not claim the ladder.
    // Its role alone must not make an armed command impossible to cancel.
    const tour = document.createElement('aside')
    tour.setAttribute('role', 'dialog')
    tour.setAttribute('aria-modal', 'false')
    document.body.appendChild(tour)
    opener.focus()
    fireEvent.keyDown(opener, { key: 'Escape' })
    expect(promptEl()).toBeNull()
    tour.remove()
    opener.remove()
  })

  it('a Modify tool armed with nothing selected keeps its fields live and gates only Run; a pick releases Run', async () => {
    const studio = mount()
    await openAndLoad(studio, [LINE])
    // Nothing selected: the tool itself is disabled (its own ladder), so
    // arm through the provider the way a keyboard command would.
    expect(ribbonTool('move').disabled).toBe(true)
    act(() => { studio.context.setArmed({ group: 'modify', op: 'move' }) })
    const row = screen.getByTestId('cockpit-prompt')
    expect(row.getAttribute('data-op')).toBe('move')
    expect(row.textContent).toContain(MODIFY_REASONS.noSelection)
    // The operands can be typed before the pick (the documented ladder).
    expect(screen.getByLabelText('ribbon dx').disabled).toBe(false)
    expect(screen.getByLabelText('ribbon dy').disabled).toBe(false)
    fireEvent.change(screen.getByLabelText('ribbon dx'), { target: { value: '7' } })
    expect(screen.getByLabelText('ribbon dx').value).toBe('7')
    // Run is what waits for the entity, and Enter posts nothing meanwhile.
    expect(screen.getByTestId('cockpit-prompt-run').disabled).toBe(true)
    const before = studio.workers[0].posted.length
    fireEvent.keyDown(screen.getByLabelText('ribbon dx'), { key: 'Enter' })
    expect(studio.workers[0].posted.length).toBe(before)
    // Pick: Run releases, the sentence leaves, Enter runs with the typed dx.
    fireEvent.click(screen.getByRole('radio'))
    expect(screen.getByTestId('cockpit-prompt-run').disabled).toBe(false)
    expect(row.textContent).not.toContain(MODIFY_REASONS.noSelection)
    fireEvent.keyDown(screen.getByLabelText('ribbon dx'), { key: 'Enter' })
    const posted = studio.workers[0].posted
    expect(posted[posted.length - 1]).toEqual({ type: 'applyEdit', op: 'move', payload: { entityId: 'e1', dx: 7, dy: 0 } })
    // Busy still disables the fields themselves (not a selection matter).
    expect(screen.getByLabelText('ribbon dx').disabled).toBe(true)
  })

  it('Enter runs the armed command once, a dead engine disarms it, and setArmed drops anything malformed', async () => {
    const studio = mount()
    await openAndLoad(studio, [LINE])
    fireEvent.click(drawTool('createCircle'))
    const before = studio.workers[0].posted.length
    fireEvent.keyDown(screen.getByLabelText('ribbon r'), { key: 'Enter' })
    expect(studio.workers[0].posted.length).toBe(before + 1)
    expect(studio.workers[0].posted[before].op).toBe('createCircle')
    // In flight: Enter again queues nothing (the fields and Run are off).
    expect(screen.getByLabelText('ribbon r').disabled).toBe(true)
    expect(screen.getByTestId('cockpit-prompt-run').disabled).toBe(true)
    fireEvent.keyDown(screen.getByTestId('cockpit-prompt'), { key: 'Enter' })
    expect(studio.workers[0].posted.length).toBe(before + 1)
    studio.workers[0].emit({ ...editApplied('createCircle', [LINE, POLY]), createdId: 'e2' })
    expect(screen.getByLabelText('ribbon r').disabled).toBe(false)
    expect(promptEl().getAttribute('data-op')).toBe('createCircle')
    // Enter ON A BUTTON is the button's own activation, never the row's
    // run: keydown alone posts nothing (jsdom synthesizes no click), the
    // click that a browser then fires is the one action. Cancel cancels.
    const posted = () => studio.workers[0].posted.length
    const afterCircle = posted()
    fireEvent.keyDown(screen.getByRole('button', { name: 'Cancel' }), { key: 'Enter' })
    expect(posted()).toBe(afterCircle)
    expect(promptEl().getAttribute('data-op')).toBe('createCircle')
    fireEvent.keyDown(screen.getByTestId('cockpit-prompt-run'), { key: 'Enter' })
    expect(posted()).toBe(afterCircle)
    fireEvent.click(screen.getByTestId('cockpit-prompt-run'))
    expect(posted()).toBe(afterCircle + 1)
    studio.workers[0].emit({ ...editApplied('createCircle', [LINE, POLY]), createdId: 'e2' })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(promptEl()).toBeNull()
    expect(posted()).toBe(afterCircle + 1)
    fireEvent.click(drawTool('createCircle'))
    // Bounded writes: a malformed record is ignored (the prompt stays on
    // the circle), null disarms.
    act(() => { studio.context.setArmed({ group: 'nope', op: 'createLine' }) })
    act(() => { studio.context.setArmed({ group: 'draw', op: 'create Line' }) })
    act(() => { studio.context.setArmed('createLine') })
    act(() => { studio.context.setArmed({ group: 'draw' }) })
    expect(promptEl().getAttribute('data-op')).toBe('createCircle')
    act(() => { studio.context.setArmed({ group: 'modify', op: 'move' }) })
    expect(promptEl().getAttribute('data-op')).toBe('move')
    expect(promptEl().textContent).toContain('Specify displacement:')
    // A regex-valid op the table does not know renders no prompt and
    // leaves no tool claiming one (no dangling aria-controls).
    act(() => { studio.context.setArmed({ group: 'modify', op: 'delete' }) })
    expect(promptEl()).toBeNull()
    expect(document.querySelectorAll('.drafting-ribbon [aria-controls="cockpit-prompt"]')).toHaveLength(0)
    act(() => { studio.context.setArmed(null) })
    expect(promptEl()).toBeNull()
    // Opening ANOTHER document cancels the running command.
    fireEvent.click(drawTool('createLine'))
    expect(promptEl().getAttribute('data-op')).toBe('createLine')
    await openAndLoad(studio, [LINE, POLY], 'two.dxf')
    expect(studio.context.session.documentId).toBe('two.dxf')
    expect(promptEl()).toBeNull()
    // The worker dies: no document to prompt for, so the prompt goes too.
    act(() => { studio.context.setArmed({ group: 'draw', op: 'createArc' }) })
    expect(promptEl().getAttribute('data-op')).toBe('createArc')
    act(() => { studio.workers[0].die() })
    expect(studio.context.session.errorKind).toBe(SESSION_ERROR.CRASHED)
    expect(promptEl()).toBeNull()
  })
})

describe('the band\'s Undo edit / Redo edit (W4f slice F)', () => {
  // No band slot in this mount, so the tools render inline in the File panel
  // (the band gets the same records as quick-access buttons).
  const quick = (id) => document.querySelector(`.drafting-ribbon [data-tool="${id.replace(/^quick-/, '')}"]`)

  it('are disabled with the reason until an edit exists, then Undo re-loads the bytes before it and Redo steps forward', async () => {
    const studio = mount()
    expect(quick('quick-undo-edit').disabled).toBe(true)
    expect(quick('quick-undo-edit').title).toBe('opens on an imported DXF')
    await openAndLoad(studio, [LINE])
    expect(quick('quick-undo-edit').title).toBe('nothing to undo')
    expect(quick('quick-redo-edit').title).toBe('nothing to redo')
    fireEvent.click(screen.getByRole('radio'))
    armAndRun('move')
    const bytes = new Uint8Array([9, 9])
    studio.workers[0].emit(editApplied('move', [LINE], bytes))
    expect(quick('quick-undo-edit').disabled).toBe(false)
    expect(quick('quick-undo-edit').title).toContain('1 to undo')
    fireEvent.click(quick('quick-undo-edit'))
    const posted = studio.workers[0].posted
    expect(posted[posted.length - 1].type).toBe('loadDocument')
    expect(posted[posted.length - 1].documentId).toBe('one.dxf')
    studio.workers[0].emit(loadedMessage([LINE]))
    expect(studio.context.session.status).toMatch(/^Undid move:/)
    expect(quick('quick-undo-edit').title).toBe('nothing to undo')
    expect(quick('quick-redo-edit').disabled).toBe(false)
    fireEvent.click(quick('quick-redo-edit'))
    expect(studio.workers[0].posted[studio.workers[0].posted.length - 1].bytes).toBe(bytes)
  })
})

describe('the reason ladders are pure and total', () => {
  it('modifyReason resolves in the order a user clears them', () => {
    expect(modifyReason(null)).toBe(MODIFY_REASONS.noDocument)
    expect(modifyReason({ errorKind: SESSION_ERROR.CRASHED, engineParsed: true })).toBe(MODIFY_REASONS.crashed)
    expect(modifyReason({ engineParsed: false })).toBe(MODIFY_REASONS.noDocument)
    expect(modifyReason({ engineParsed: true, busy: true })).toBe(MODIFY_REASONS.busy)
    expect(modifyReason({ engineParsed: true, selected: null })).toBe(MODIFY_REASONS.noSelection)
    expect(modifyReason({ engineParsed: true, selected: { editable: false } })).toBe(MODIFY_REASONS.readOnlyKind)
    expect(modifyReason({ engineParsed: true, selected: { id: 'e1' } })).toBe('')
  })

  it('drawReason needs a parsed document and a live engine, never a selection', () => {
    expect(drawReason(null)).toBe(DRAW_REASONS.noDocument)
    expect(drawReason({ errorKind: SESSION_ERROR.CRASHED, engineParsed: true })).toBe(DRAW_REASONS.crashed)
    expect(drawReason({ engineParsed: false })).toBe(DRAW_REASONS.noDocument)
    expect(drawReason({ engineParsed: true, busy: true })).toBe(DRAW_REASONS.busy)
    expect(drawReason({ engineParsed: true, selected: null })).toBe('')
  })

  it('saveReason names the missing precondition', () => {
    expect(saveReason(null, true)).toBe(SAVE_REASONS.noDocument)
    expect(saveReason({ engineParsed: true, savedBytes: null }, true)).toBe(SAVE_REASONS.nothingEdited)
    expect(saveReason({ engineParsed: true, savedBytes: new Uint8Array(1) }, false)).toBe(SAVE_REASONS.noTarget)
    expect(saveReason({ engineParsed: true, savedBytes: new Uint8Array(1), busy: true }, true)).toBe(SAVE_REASONS.busy)
    expect(saveReason({ engineParsed: true, savedBytes: new Uint8Array(1) }, true)).toBe('')
  })
})

describe('the consumer contract', () => {
  it('the import pane refuses to render outside the ONE mount (never a silent second session)', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    expect(() => render(<CadEditSurface enabled />)).toThrow(/EngineSessionProvider/)
    spy.mockRestore()
  })

  it('flag off, the pane renders nothing even without a provider', () => {
    render(<CadEditSurface enabled={false} />)
    expect(screen.queryByTestId('cad-edit-workbench')).toBeNull()
  })
})
