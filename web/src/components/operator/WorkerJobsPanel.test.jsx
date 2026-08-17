import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import WorkerJobsPanel from './WorkerJobsPanel.jsx'
import * as operatorClient from '../../operatorClient.js'

afterEach(cleanup)

beforeEach(() => {
  vi.spyOn(operatorClient, 'dispatchWorker').mockResolvedValue({ jobId: 'op-1', status: 'accepted' })
  vi.spyOn(operatorClient, 'isOperatorDenied').mockReturnValue(false)
})

describe('acceptance: worker job status and cancellation', () => {
  it('dispatches a job and renders the receipt', async () => {
    render(<WorkerJobsPanel />)
    fireEvent.change(screen.getByLabelText(/commands/i), { target: { value: 'echo hi\necho bye' } })
    fireEvent.click(screen.getByRole('button', { name: /dispatch job/i }))
    await waitFor(() => expect(operatorClient.dispatchWorker).toHaveBeenCalledWith(['echo hi', 'echo bye']))
    expect(await screen.findByText(/"jobId": "op-1"/)).toBeTruthy()
  })

  it('renders Cancel as a disabled control naming the reason, never a fake cancellation', async () => {
    render(<WorkerJobsPanel />)
    fireEvent.change(screen.getByLabelText(/commands/i), { target: { value: 'echo hi' } })
    fireEvent.click(screen.getByRole('button', { name: /dispatch job/i }))
    const cancel = await screen.findByRole('button', { name: /cancel/i })
    expect(cancel.disabled).toBe(true)
    expect(screen.getByText(/no worker-cancel endpoint is mounted/i)).toBeTruthy()
  })
})

describe('acceptance #1: commands field is never treated as a credential', () => {
  it('the commands textarea is not labeled secret/token/password/key', () => {
    render(<WorkerJobsPanel />)
    const field = screen.getByLabelText(/commands/i)
    expect(field.id).not.toMatch(/secret|token|password|key/i)
  })
})
