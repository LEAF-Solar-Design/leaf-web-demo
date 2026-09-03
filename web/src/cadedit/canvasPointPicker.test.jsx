// W4f slice A1: a click on the ground answers the armed prompt's point
// steps through the viewer's unproject, the caret moves on, the rubber band
// follows the cursor, and nothing happens without an armed point command.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import DraftingRibbon from '../site/DraftingRibbon.jsx'

import CadEditSurface from './CadEditSurface.jsx'
import CanvasPointPicker from './CanvasPointPicker.jsx'
import EngineRibbonClusters from './EngineRibbonClusters.jsx'
import EngineSessionProvider, { useEngineSessionContext } from './EngineSessionProvider.jsx'

class ScriptedWorker {
  constructor() { this.posted = []; this.listeners = new Map(); this.terminated = false }
  addEventListener(type, fn) { this.listeners.set(type, fn) }
  removeEventListener(type) { this.listeners.delete(type) }
  postMessage(message) { this.posted.push(message) }
  terminate() { this.terminated = true }
  emit(data) { act(() => { this.listeners.get('message')?.({ data }) }) }
}

const LINE = { id: 'e1', type: 'LINE', layer: 'Panels', vertices: [[0, 0, 0], [100, 50, 0]] }

function fileOf(name = 'one.dxf') {
  const bytes = new TextEncoder().encode('0\nEOF\n')
  const file = new File([bytes], name, { type: 'application/dxf' })
  file.arrayBuffer = async () => bytes.buffer.slice(0)
  Object.defineProperty(file, 'size', { value: bytes.length })
  return file
}

let workers
let viewer
let ground
let onPicking
let context
function Probe() { context = useEngineSessionContext(); return null }
function mount() {
  workers = []
  // World = client / 10, so a click at (120, 30) is world (12, 3).
  viewer = { unproject: vi.fn((cx, cy) => ({ x: cx / 10, y: cy / 10 })), setRubberBand: vi.fn() }
  ground = document.createElement('div')
  document.body.appendChild(ground)
  onPicking = vi.fn()
  const createWorker = vi.fn(() => { const w = new ScriptedWorker(); workers.push(w); return w })
  render(
    <EngineSessionProvider createWorker={createWorker}>
      <Probe />
      <DraftingRibbon clusters={[]}>
        <EngineRibbonClusters importOpen={false} onToggleImport={() => {}} />
      </DraftingRibbon>
      <CanvasPointPicker viewerRef={{ current: viewer }} ground={ground} onPicking={onPicking} />
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

function click(x, y) {
  act(() => {
    ground.dispatchEvent(new MouseEvent('pointerdown', { clientX: x, clientY: y, button: 0, bubbles: true }))
    ground.dispatchEvent(new MouseEvent('pointerup', { clientX: x, clientY: y, button: 0, bubbles: true }))
  })
}

beforeEach(() => {
  globalThis.URL.createObjectURL = vi.fn(() => 'blob:cad-edit-test')
  globalThis.URL.revokeObjectURL = vi.fn()
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb) => { cb(); return 1 })
})
afterEach(() => { cleanup(); ground?.remove(); vi.restoreAllMocks() })

describe('CanvasPointPicker (W4f slice A1)', () => {
  it('nothing is picked, stamped or ghosted without an armed point command', async () => {
    mount()
    await openAndLoad()
    click(120, 30)
    expect(viewer.unproject).not.toHaveBeenCalled()
    expect(onPicking).not.toHaveBeenCalledWith(true)
    expect(screen.queryByTestId('cockpit-prompt')).toBeNull()
  })

  it('two clicks answer LINE\'s two point steps, move the caret, draw the rubber band, and stand the console selection aside', async () => {
    mount()
    await openAndLoad()
    fireEvent.click(document.querySelector('.drafting-ribbon [data-tool="draw:createLine"]'))
    expect(onPicking).toHaveBeenLastCalledWith(true)
    click(120, 30)
    expect(screen.getByLabelText('ribbon x').value).toBe('12')
    expect(screen.getByLabelText('ribbon y').value).toBe('3')
    expect(document.activeElement).toBe(screen.getByLabelText('ribbon x2'))
    // The band follows the cursor from the first point.
    act(() => { ground.dispatchEvent(new MouseEvent('pointermove', { clientX: 200, clientY: 80, bubbles: true })) })
    expect(viewer.setRubberBand).toHaveBeenLastCalledWith([[12, 3], [20, 8]], false)
    click(200, 80)
    expect(screen.getByLabelText('ribbon x2').value).toBe('20')
    expect(screen.getByLabelText('ribbon y2').value).toBe('8')
    expect(document.activeElement).toBe(screen.getByTestId('cockpit-prompt-run'))
    // A third click changes nothing: the sequence is complete.
    click(50, 50)
    expect(screen.getByLabelText('ribbon x').value).toBe('12')
    // Enter runs with the picked operands.
    fireEvent.keyDown(screen.getByLabelText('ribbon x2'), { key: 'Enter' })
    const posted = workers[0].posted
    expect(posted[posted.length - 1]).toEqual({ type: 'applyEdit', op: 'createLine', payload: { x1: 12, y1: 3, x2: 20, y2: 8, layer: '' } })
    // Cancelling clears the band and the stamp.
    fireEvent.keyDown(screen.getByLabelText('ribbon x2'), { key: 'Escape' })
    expect(onPicking).toHaveBeenLastCalledWith(false)
    expect(viewer.setRubberBand).toHaveBeenLastCalledWith(null)
  })

  it('a circle takes its centre and a radius point; a drag (pointer travel) is never a pick', async () => {
    mount()
    await openAndLoad()
    act(() => { context.setArmed({ group: 'draw', op: 'createCircle' }) })
    act(() => {
      ground.dispatchEvent(new MouseEvent('pointerdown', { clientX: 0, clientY: 0, button: 0, bubbles: true }))
      ground.dispatchEvent(new MouseEvent('pointerup', { clientX: 40, clientY: 0, button: 0, bubbles: true }))
    })
    expect(screen.getByLabelText('ribbon x').value).toBe('0')
    click(100, 100)
    expect(screen.getByLabelText('ribbon x').value).toBe('10')
    expect(screen.getByLabelText('ribbon y').value).toBe('10')
    click(130, 140)
    expect(screen.getByLabelText('ribbon r').value).toBe('5')
    expect(document.activeElement).toBe(screen.getByTestId('cockpit-prompt-run'))
  })
})
