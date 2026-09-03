// W4f slice B: a typed command word, delivered as the ONE cockpit:command
// window event, arms the same prompt a ribbon click arms; ERASE runs on a live
// selection and does nothing without one; anything malformed is ignored.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { COCKPIT_COMMAND_EVENT } from '../lib/commandWords.js'

import CadEditSurface from './CadEditSurface.jsx'
import CommandLineArmer, { acceptsCommand } from './CommandLineArmer.jsx'
import EngineRibbonClusters from './EngineRibbonClusters.jsx'
import EngineSessionProvider from './EngineSessionProvider.jsx'
import DraftingRibbon from '../site/DraftingRibbon.jsx'

class ScriptedWorker {
  constructor() { this.posted = []; this.listeners = new Map(); this.terminated = false }
  addEventListener(type, fn) { this.listeners.set(type, fn) }
  removeEventListener(type) { this.listeners.delete(type) }
  postMessage(message) { this.posted.push(message) }
  terminate() { this.terminated = true }
  emit(data) { act(() => { this.listeners.get('message')?.({ data }) }) }
}

const LINE = { id: 'e1', type: 'LINE', layer: 'Panels', vertices: [[0, 0], [1, 1]] }

function fileOf(name = 'one.dxf') {
  const bytes = new TextEncoder().encode('0\nEOF\n')
  const file = new File([bytes], name, { type: 'application/dxf' })
  file.arrayBuffer = async () => bytes.buffer.slice(0)
  Object.defineProperty(file, 'size', { value: bytes.length })
  return file
}

let workers
function mount() {
  workers = []
  const createWorker = vi.fn(() => { const w = new ScriptedWorker(); workers.push(w); return w })
  render(
    <EngineSessionProvider createWorker={createWorker}>
      <DraftingRibbon clusters={[]}>
        <EngineRibbonClusters importOpen={false} onToggleImport={() => {}} />
        <CommandLineArmer />
      </DraftingRibbon>
      <CadEditSurface enabled />
    </EngineSessionProvider>,
  )
}

async function openAndLoad(entities = [LINE]) {
  await act(async () => {
    fireEvent.change(screen.getByLabelText('DXF file'), { target: { files: [fileOf()] } })
    await Promise.resolve()
    await Promise.resolve()
  })
  await waitFor(() => expect(workers.length).toBeGreaterThan(0))
  workers[0].emit({ type: 'documentLoaded', documentId: 'one.dxf', entities, entityCount: entities.length, unsupported: [] })
}

const command = (detail) => act(() => { window.dispatchEvent(new CustomEvent(COCKPIT_COMMAND_EVENT, { detail })) })
const promptEl = () => screen.queryByTestId('cockpit-prompt')

beforeEach(() => {
  globalThis.URL.createObjectURL = vi.fn(() => 'blob:cad-edit-test')
  globalThis.URL.revokeObjectURL = vi.fn()
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('CommandLineArmer (W4f slice B)', () => {
  it('a draw word arms its prompt like the ribbon click; a second word re-arms; malformed details are ignored', async () => {
    mount()
    await openAndLoad()
    expect(promptEl()).toBeNull()
    command({ group: 'draw', op: 'createLine' })
    expect(promptEl().getAttribute('data-op')).toBe('createLine')
    expect(promptEl().textContent).toContain('LINE')
    command({ group: 'draw', op: 'createCircle' })
    expect(promptEl().getAttribute('data-op')).toBe('createCircle')
    for (const bad of [null, 'createLine', { group: 'draw' }, { group: 'nope', op: 'createLine' }, { group: 'draw', op: 'format' }, { group: 'draw', op: 'constructor' }]) {
      command(bad)
      expect(promptEl().getAttribute('data-op')).toBe('createCircle')
    }
    expect(workers[0].posted.filter((m) => m.type === 'applyEdit')).toHaveLength(0)
  })

  it('ERASE runs at once on a live selection and does nothing without one', async () => {
    mount()
    await openAndLoad()
    const before = workers[0].posted.length
    command({ group: 'modify', op: 'delete' })
    expect(workers[0].posted.length).toBe(before)
    expect(promptEl()).toBeNull()
    fireEvent.click(screen.getByRole('radio'))
    command({ group: 'modify', op: 'delete' })
    const posted = workers[0].posted
    expect(posted[posted.length - 1]).toEqual({ type: 'applyEdit', op: 'delete', payload: { entityId: 'e1' } })
  })

  it('acceptsCommand is the fail-closed gate', () => {
    expect(acceptsCommand({ group: 'draw', op: 'createArc' })).toBe(true)
    expect(acceptsCommand({ group: 'modify', op: 'delete' })).toBe(true)
    expect(acceptsCommand({ group: 'modify', op: 'hasOwnProperty' })).toBe(false)
    expect(acceptsCommand({ group: 'draw', op: 'delete' })).toBe(true)
    expect(acceptsCommand({ group: 'view', op: 'fit' })).toBe(false)
    expect(acceptsCommand(undefined)).toBe(false)
  })
})
