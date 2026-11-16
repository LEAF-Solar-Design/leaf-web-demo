import React, { useState, useSyncExternalStore } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import createDrawingUploadController from '../controllers/upload/createDrawingUploadController.js'
import DrawingDependencyReport from './DrawingDependencyReport.jsx'
import ModuleSetPicker from './ModuleSetPicker.jsx'

afterEach(cleanup)

it('clears failed intake loading and binds metadata, report and choices only after successful retry', async () => {
  const source = { name: 'synthetic.dxf', size: 16 }
  const services = {
    policy: vi.fn().mockResolvedValue({ enabled: true, accepted: ['.dxf'], max_bytes: 1024 }),
    upload: vi.fn().mockResolvedValueOnce({ drawing_id: 'failed-A', status: 'ready', original_sha256: 'a'.repeat(64) })
      .mockResolvedValueOnce({ drawing_id: 'ready-B', status: 'ready', original_sha256: 'b'.repeat(64) }),
    intake: vi.fn().mockRejectedValueOnce(new Error('Synthetic intake unavailable'))
      .mockResolvedValueOnce({ dependency_report: { coverage_note: 'B observed records only.',
        dependencies: [{ kind: 'font', name: 'B.shx', disposition: 'font remapped' }] },
      result: { sets: [{ set_id: 'B-set', layer: 'B roof', block: 'Panel', count: 2 }] } }),
    status: vi.fn(), wait: vi.fn(),
  }
  const recorded = vi.fn()
  let showReady
  const controller = createDrawingUploadController({ services, onReady: (value) => showReady(value) })
  function Recovery() {
    const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot)
    const [ready, setReady] = useState(null)
    showReady = setReady
    return <>
      <button disabled={state.busy} onClick={() => { void controller.upload(source) }}>Upload synthetic drawing</button>
      <p role="status">{state.busy ? 'Loading' : state.phase}</p>
      {state.error && <p role="alert">{state.error}</p>}
      {ready && <section aria-label="Successful drawing">
        <p>{ready.receipt.drawing_id}</p><p>{ready.receipt.original_sha256}</p>
        <DrawingDependencyReport report={ready.view.dependency_report} />
        <ModuleSetPicker drawingKey={ready.receipt.drawing_id} data={ready.view.result}
          onSelect={(ids) => recorded({ drawing: ready.receipt.drawing_id, ids })} />
      </section>}
    </>
  }
  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(() => { throw new Error('No real network') })
  try {
    render(<Recovery />)
    await act(async () => { await controller.loadPolicy() })
    fireEvent.click(screen.getByRole('button', { name: 'Upload synthetic drawing' }))
    await waitFor(() => expect(screen.getByRole('status').textContent).toBe('failed'))
    expect(screen.getByRole('alert')).toBeTruthy()
    expect(screen.queryByRole('region', { name: 'Successful drawing' })).toBeNull()
    expect(screen.queryByText('failed-A')).toBeNull()
    expect(screen.getByRole('button').disabled).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Upload synthetic drawing' }))
    await waitFor(() => expect(screen.getByText('ready-B')).toBeTruthy())
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByText('b'.repeat(64))).toBeTruthy()
    expect(screen.queryByText('a'.repeat(64))).toBeNull()
    expect(screen.getByText('B.shx')).toBeTruthy()
    fireEvent.click(screen.getByRole('checkbox', { name: 'B roof / Panel: 2 candidates' }))
    fireEvent.click(screen.getByRole('button', { name: 'Count selected set' }))
    expect(recorded).toHaveBeenCalledExactlyOnceWith({ drawing: 'ready-B', ids: ['B-set'] })
    expect(controller.getSnapshot().busy).toBe(false)
    expect(services.upload).toHaveBeenCalledTimes(2)
    expect(services.intake.mock.calls.map((args) => args[0])).toEqual(['failed-A', 'ready-B'])
    expect(fetchSpy).not.toHaveBeenCalled()
  } finally { controller.dispose(); fetchSpy.mockRestore() }
})
