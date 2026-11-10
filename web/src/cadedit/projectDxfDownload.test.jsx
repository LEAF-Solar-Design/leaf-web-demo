// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import DraftingRibbon from '../site/DraftingRibbon.jsx'
import EngineRibbonClusters, { projectDxfFilename } from './EngineRibbonClusters.jsx'
import EngineSessionProvider, { useEngineSessionContext } from './EngineSessionProvider.jsx'

// A saved project version downloaded from the File panel, beside Save version.
const VERSION = '44444444-4444-4444-8444-444444444444'
const OTHER = '55555555-5555-5555-8555-555555555555'
const SAVED = new Uint8Array([48, 10, 83, 69, 67, 84, 73, 79, 78, 10])
const LINE = { id: 'e1', type: 'LINE', layer: '0', vertices: [[0, 0], [10, 10]] }

class ScriptedWorker {
  constructor() { this.posted = []; this.listeners = new Map() }
  addEventListener(type, listener) { this.listeners.set(type, listener) }
  removeEventListener(type) { this.listeners.delete(type) }
  postMessage(message) { this.posted.push(message) }
  terminate() {}
  emit(data) { act(() => this.listeners.get('message')?.({ data })) }
}

function deferred() {
  let resolve
  let reject
  const promise = new Promise((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

function mount({ download, saveTarget = { drawingId: 'd1', headVersion: VERSION, save: vi.fn() } } = {}) {
  const worker = new ScriptedWorker()
  const handle = { worker, saveTarget }
  function Probe() { handle.context = useEngineSessionContext(); return null }
  const tree = (projectDownload) => (
    <EngineSessionProvider createWorker={() => worker} saveTarget={saveTarget}>
      <Probe />
      <DraftingRibbon clusters={[]}>
        <EngineRibbonClusters panels={['draw', 'modify', 'annotation', 'block']} projectDownload={projectDownload} />
      </DraftingRibbon>
    </EngineSessionProvider>
  )
  const view = render(tree(download))
  handle.rerender = (next) => view.rerender(tree(next))
  act(() => { handle.context.session.actions.openBytes(SAVED, 'project.dxf', { committed: true, version: 1 }) })
  worker.emit({ type: 'documentLoaded', documentId: 'project.dxf', entities: [LINE], entityCount: 1, unsupported: [] })
  return handle
}

const tool = (id) => document.querySelector(`.drafting-ribbon [data-tool="${id}"]`)

function readBlob(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(new Uint8Array(reader.result))
    reader.onerror = () => reject(reader.error)
    reader.readAsArrayBuffer(blob)
  })
}

function captureDownloads() {
  const clicked = []
  const blobs = []
  // jsdom has no object URLs to spy on.
  if (typeof URL.createObjectURL !== 'function') URL.createObjectURL = () => ''
  if (typeof URL.revokeObjectURL !== 'function') URL.revokeObjectURL = () => {}
  const create = vi.spyOn(URL, 'createObjectURL').mockImplementation((blob) => { blobs.push(blob); return 'blob:saved' })
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function click() {
    clicked.push({ name: this.download, href: this.href })
  })
  return { clicked, blobs, create, click }
}

afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('project saved-version Download DXF', () => {
  it('downloads the exact saved bytes through the verified read, with no save, checkout or edit', async () => {
    const saved = captureDownloads()
    const read = vi.fn(async () => ({ bytes: SAVED, documentId: 'roof-plan', source_sha256: 'a'.repeat(64) }))
    const studio = mount({ download: { key: `p|${VERSION}`, versionId: VERSION, read } })
    const button = tool('download-dxf')
    expect(button.disabled).toBe(false)
    expect(tool('save-version')).toBeTruthy()
    const posted = studio.worker.posted.length
    await act(async () => { fireEvent.click(button) })
    await waitFor(() => expect(saved.clicked).toHaveLength(1))
    expect(read).toHaveBeenCalledTimes(1)
    expect(read).toHaveBeenCalledWith(VERSION)
    expect(saved.clicked[0]).toEqual({ name: 'roof-plan.dxf', href: 'blob:saved' })
    expect(saved.blobs[0].type).toBe('application/dxf')
    expect(await readBlob(saved.blobs[0])).toEqual(SAVED)
    expect(studio.saveTarget.save).not.toHaveBeenCalled()
    expect(studio.worker.posted).toHaveLength(posted)
  })

  it('is available while another session holds the checkout, since it needs none', () => {
    const read = vi.fn()
    const studio = mount({ download: { key: 'k', versionId: VERSION, read } })
    expect(studio.context.canSave).toBe(true)
    expect(tool('download-dxf').disabled).toBe(false)
    expect(read).not.toHaveBeenCalled()
  })

  it('is disabled with a clear reason while the engine holds unsaved edits', async () => {
    const saved = captureDownloads()
    const read = vi.fn(async () => ({ bytes: SAVED, documentId: 'project.dxf' }))
    const studio = mount({ download: { key: 'k', versionId: VERSION, read } })
    act(() => { studio.context.session.actions.select('e1') })
    studio.worker.emit({ type: 'editApplied', op: 'move', ok: true, entities: [LINE], entityCount: 1, bytes: new Uint8Array([49, 10]), byteLength: 2 })
    const button = tool('download-dxf')
    expect(studio.context.session.dirty).toBe(true)
    expect(button.disabled).toBe(true)
    expect(button.title).toBe('Save your edits as a version first, then download the saved version.')
    fireEvent.click(button)
    expect(read).not.toHaveBeenCalled()
    expect(saved.clicked).toHaveLength(0)
  })

  it('refuses a result that finishes after the project or version changed', async () => {
    const saved = captureDownloads()
    const pending = deferred()
    const read = vi.fn(() => pending.promise)
    const studio = mount({ download: { key: `p|${VERSION}`, versionId: VERSION, read } })
    await act(async () => { fireEvent.click(tool('download-dxf')) })
    studio.rerender({ key: `p|${OTHER}`, versionId: OTHER, read: vi.fn() })
    await act(async () => { pending.resolve({ bytes: SAVED, documentId: 'project.dxf' }) })
    expect(saved.clicked).toHaveLength(0)
    expect(saved.create).not.toHaveBeenCalled()
  })

  it('shows nothing extra without a project download source, so standalone is unchanged', () => {
    mount({ download: null })
    expect(tool('download-dxf')).toBeNull()
    expect(tool('save-version')).toBeTruthy()
  })

  it('a failed read downloads nothing and re-enables the button', async () => {
    const saved = captureDownloads()
    const read = vi.fn(async () => { throw Object.assign(new Error('The project drawing bytes could not be verified.'), { status: 409 }) })
    mount({ download: { key: 'k', versionId: VERSION, read } })
    await act(async () => { fireEvent.click(tool('download-dxf')) })
    await waitFor(() => expect(tool('download-dxf').disabled).toBe(false))
    expect(saved.clicked).toHaveLength(0)
  })

  it('names the file after the document with a .dxf extension', () => {
    expect(projectDxfFilename('roof-plan')).toBe('roof-plan.dxf')
    expect(projectDxfFilename('Roof.DXF')).toBe('Roof.DXF')
    expect(projectDxfFilename('')).toBe('project.dxf')
    expect(projectDxfFilename(undefined)).toBe('project.dxf')
    expect(projectDxfFilename('a/b\\c:d')).toBe('a_b_c_d.dxf')
  })
})
