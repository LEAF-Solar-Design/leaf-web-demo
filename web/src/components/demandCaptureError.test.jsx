import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { DemandCaptureCard } from './SessionGate.jsx'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function fillAndSubmit() {
  render(<DemandCaptureCard />)
  fireEvent.change(screen.getByLabelText('Work email'), { target: { value: 'designer@example.com' } })
  fireEvent.change(screen.getByLabelText('What are you trying to automate?'), { target: { value: 'Rooftop layouts' } })
  fireEvent.click(screen.getByRole('button', { name: 'Notify me' }))
}

describe('demand capture failures', () => {
  it('shows the landing form sentence after a rejected fetch and allows a successful retry', async () => {
    const fetch = vi.fn()
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({}) })
    vi.stubGlobal('fetch', fetch)

    fillAndSubmit()

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not save your interest. Please try again.')
    expect(screen.queryByText(/Failed to fetch/)).toBeNull()
    const retry = screen.getByRole('button', { name: 'Notify me' })
    expect(retry).toBeEnabled()
    fireEvent.click(retry)

    expect(await screen.findByRole('status')).toHaveTextContent('Thanks, we’ll notify you.')
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('replaces a server failure with the same sentence and restores the submit button', async () => {
    let finishRequest
    vi.stubGlobal('fetch', vi.fn(() => new Promise((resolve) => { finishRequest = resolve })))

    fillAndSubmit()
    expect(screen.getByRole('button', { name: 'Saving…' })).toBeDisabled()

    finishRequest({
      ok: false,
      status: 503,
      json: async () => ({ error: { message: 'POST /api/demand -> 503 error_id: internal-123' } }),
    })

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not save your interest. Please try again.')
    expect(screen.queryByText(/POST|\/api\/|503|internal-123/)).toBeNull()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Notify me' })).toBeEnabled())
  })
})
