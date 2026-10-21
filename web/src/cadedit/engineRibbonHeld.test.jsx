// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import DraftingRibbon from '../site/DraftingRibbon.jsx'
import EngineRibbonClusters from './EngineRibbonClusters.jsx'
import EngineSessionProvider, { REACH_STATE, useEngineSessionContext } from './EngineSessionProvider.jsx'

const HOLD = 'save the edits to the previous drawing, or close the project to keep editing it, before editing this project'
const LINE = { id: 'e1', type: 'LINE', layer: '0', vertices: [[0, 0], [10, 10]] }
const BYTES = new Uint8Array([48, 10])

class ScriptedWorker {
  constructor() { this.posted = []; this.listeners = new Map() }
  addEventListener(type, listener) { this.listeners.set(type, listener) }
  removeEventListener(type) { this.listeners.delete(type) }
  postMessage(message) { this.posted.push(message) }
  terminate() {}
  emit(data) { act(() => this.listeners.get('message')?.({ data })) }
}

function mount() {
  const worker = new ScriptedWorker()
  const save = vi.fn(async () => ({ new_version: { drawing_id: 'legacy', version: 2, parent: 1 }, head: 2 }))
  const handle = { worker, save }
  function Probe() { handle.context = useEngineSessionContext(); return null }
  render(
    <EngineSessionProvider createWorker={() => worker} saveTarget={{ drawingId: 'legacy', headVersion: 1, save }}>
      <Probe />
      <DraftingRibbon clusters={[]}>
        <EngineRibbonClusters panels={['draw', 'modify', 'annotation', 'block']} />
      </DraftingRibbon>
    </EngineSessionProvider>,
  )
  act(() => { handle.context.session.actions.openBytes(BYTES, 'legacy-v1.dxf', { committed: true, version: 1 }) })
  worker.emit({ type: 'documentLoaded', documentId: 'legacy-v1.dxf', entities: [LINE], entityCount: 1, unsupported: [] })
  act(() => { handle.context.session.actions.select('e1') })
  worker.emit({ type: 'editApplied', op: 'move', ok: true, entities: [LINE], entityCount: 1, bytes: new Uint8Array([49, 10]), byteLength: 2 })
  handle.hold = () => act(() => { handle.context.setReach({ state: REACH_STATE.HELD, sentence: HOLD }) })
  return handle
}

const tool = (id) => document.querySelector(`.drafting-ribbon [data-tool="${id}"]`)

afterEach(cleanup)

describe('held legacy document ribbon', () => {
  it('refuses Draw and Modify arming with the hold sentence while Save still saves to the legacy target', async () => {
    const studio = mount()
    const savedBytes = studio.context.session.savedBytes
    studio.hold()
    const posted = studio.worker.posted.length
    for (const group of ['Draw', 'Modify', 'Annotation', 'Block']) {
      expect(screen.getByRole('group', { name: group }).querySelector('.ribbon-note').textContent).toBe(HOLD)
    }
    for (const id of ['draw:createLine', 'modify:move', 'modify:delete', 'draw:createText', 'draw:createBlock']) {
      const button = tool(id)
      expect(button.disabled).toBe(true)
      expect(button.title).toBe(HOLD)
      fireEvent.click(button)
    }
    expect(studio.context.armed).toBe(null)
    expect(studio.worker.posted).toHaveLength(posted)
    const save = tool('save-version')
    expect(save.disabled).toBe(false)
    expect(save.title).toContain('previous drawing')
    await act(async () => { fireEvent.click(save) })
    await waitFor(() => expect(studio.context.session.receipt).not.toBeNull())
    expect(studio.save).toHaveBeenCalledTimes(1)
    const [bytes, parent, digest] = studio.save.mock.calls[0]
    expect(bytes).toBe(savedBytes)
    expect(parent).toBe(1)
    expect(digest).toMatch(/^[0-9a-f]{64}$/)
    expect(studio.context.session.receipt.new_version.drawing_id).toBe('legacy')
    expect(studio.context.session.dirty).toBe(false)
  })

  it.each([['draw', 'createLine'], ['modify', 'move'], ['modify', 'mirror']])('holds a previously armed %s Run and its fields', (group, op) => {
    const studio = mount()
    act(() => { studio.context.setArmed({ group, op }) })
    const armed = studio.context.armed
    expect(screen.getByTestId('cockpit-prompt-run').disabled).toBe(false)
    studio.hold()
    const posted = studio.worker.posted.length
    expect(studio.context.armed).toBe(armed)
    expect(screen.getByTestId('cockpit-prompt-note').textContent).toBe(HOLD)
    expect(screen.getByTestId('cockpit-prompt-run').disabled).toBe(true)
    for (const input of screen.getByTestId('cockpit-prompt').querySelectorAll('input')) expect(input.disabled).toBe(true)
    fireEvent.click(screen.getByTestId('cockpit-prompt-run'))
    act(() => { window.dispatchEvent(new CustomEvent('cockpit:run', { detail: { armed } })) })
    expect(studio.worker.posted).toHaveLength(posted)
    act(() => { studio.context.setReach({ state: REACH_STATE.IDLE, sentence: '' }) })
    expect(screen.getByTestId('cockpit-prompt-run').disabled).toBe(false)
    fireEvent.click(screen.getByTestId('cockpit-prompt-run'))
    expect(studio.worker.posted).toHaveLength(posted + 1)
  })

  it('leaves the ordinary moved-head warning editable', () => {
    const studio = mount()
    act(() => { studio.context.setReach({ state: REACH_STATE.STALE, sentence: 'the drawing moved on the server' }) })
    expect(tool('draw:createLine').disabled).toBe(false)
    expect(tool('modify:move').disabled).toBe(false)
    fireEvent.click(tool('modify:move'))
    expect(studio.context.armed).toEqual({ group: 'modify', op: 'move' })
    expect(screen.getByTestId('cockpit-prompt-run').disabled).toBe(false)
  })
})
