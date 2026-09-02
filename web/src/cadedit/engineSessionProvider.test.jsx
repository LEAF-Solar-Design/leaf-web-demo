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
import EngineRibbonClusters, { MODIFY_REASONS, SAVE_REASONS, modifyReason, saveReason } from './EngineRibbonClusters.jsx'
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
    // The pane's fields by their own aria-labels (the ribbon's carry a
    // "ribbon " prefix, and both sets are live at once by design).
    const paneInput = (name) => document.querySelector(`.cad-edit-workbench-ops input[aria-label="${name}"]`)
    fireEvent.change(screen.getByLabelText('ribbon dx'), { target: { value: '7' } })
    fireEvent.change(paneInput('dy'), { target: { value: '3' } })
    expect(paneInput('dx').value).toBe('7')
    expect(screen.getByLabelText('ribbon dy').value).toBe('3')
    fireEvent.click(ribbonTool('move'))
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
    fireEvent.click(ribbonTool('move'))
    expect(modifyNote().textContent).toBe(MODIFY_REASONS.busy)
    studio.workers[0].emit(editApplied('move', [LINE]))
    expect(modifyNote()).toBeNull()
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
    fireEvent.click(ribbonTool('move'))
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
    fireEvent.click(ribbonTool('move'))
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
    fireEvent.click(ribbonTool('move'))
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
