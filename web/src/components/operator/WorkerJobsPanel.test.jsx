import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import WorkerJobsPanel from './WorkerJobsPanel.jsx'
import * as operatorClient from '../../operatorClient.js'

afterEach(cleanup)

beforeEach(() => {
  vi.spyOn(operatorClient, 'dispatchWorker').mockResolvedValue({ jobId: 'op-1', worker_id: 'worker-1', run_id: 'run-1', status: 'running' })
  vi.spyOn(operatorClient, 'cancelWorker').mockResolvedValue({ worker_id: 'worker-1', run_id: 'run-1', status: 'cancelled' })
  vi.spyOn(operatorClient, 'isOperatorDenied').mockReturnValue(false)
})

describe('acceptance: worker job status and cancellation', () => {
  it('dispatches a job and renders the receipt', async () => {
    render(<WorkerJobsPanel />)
    fireEvent.change(screen.getByLabelText(/commands/i), { target: { value: 'echo hi\necho bye' } })
    fireEvent.click(screen.getByRole('button', { name: /dispatch job/i }))
    await waitFor(() => expect(operatorClient.dispatchWorker).toHaveBeenCalledWith(['echo hi', 'echo bye']))
    // W0#10: the receipt renders as labeled field rows, not a JSON dump —
    // assert on the row value, not the old `"jobId": "op-1"` JSON text.
    expect(await screen.findByText('jobId')).toBeTruthy()
    expect(screen.getByText('op-1')).toBeTruthy()
  })

  it('cancels only the exact active worker/run pair returned by the server', async () => {
    render(<WorkerJobsPanel />)
    fireEvent.change(screen.getByLabelText(/commands/i), { target: { value: 'echo hi' } })
    fireEvent.click(screen.getByRole('button', { name: /dispatch job/i }))
    const cancel = await screen.findByRole('button', { name: /cancel/i })
    expect(cancel.disabled).toBe(false)
    fireEvent.click(cancel)
    await waitFor(() => expect(operatorClient.cancelWorker).toHaveBeenCalledWith('worker-1', 'run-1'))
    // W0#10: labeled rows, not a JSON dump.
    expect(await screen.findByText('status')).toBeTruthy()
    expect(screen.getByText('cancelled')).toBeTruthy()
  })

  it('keeps Cancel disabled when a receipt lacks an exact active worker/run pair', async () => {
    operatorClient.dispatchWorker.mockResolvedValueOnce({ jobId: 'op-1', status: 'accepted' })
    render(<WorkerJobsPanel />)
    fireEvent.change(screen.getByLabelText(/commands/i), { target: { value: 'echo hi' } })
    fireEvent.click(screen.getByRole('button', { name: /dispatch job/i }))
    const cancel = await screen.findByRole('button', { name: /cancel/i })
    expect(cancel.disabled).toBe(true)
    expect(screen.getByText(/only while this receipt identifies/i)).toBeTruthy()
  })
})

describe('acceptance #1: commands field is never treated as a credential', () => {
  it('the commands textarea is not labeled secret/token/password/key', () => {
    render(<WorkerJobsPanel />)
    const field = screen.getByLabelText(/commands/i)
    expect(field.id).not.toMatch(/secret|token|password|key/i)
  })
})
