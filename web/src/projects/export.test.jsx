/**
 * ExportDialog. Card B-U4 acceptance oracle, one describe block per clause:
 *   Export streams/downloads the sanitized artifact; progress bounded with
 *   timeout surfaced as an unknown outcome; receipt id shown.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import ExportDialog, { ExportTimeoutError } from './ExportDialog.jsx'

afterEach(cleanup)

beforeEach(() => {
  // jsdom has no real Blob URL registry — stub the two calls the component
  // makes so a real <a href> can be asserted against a known value.
  let counter = 0
  window.URL.createObjectURL = vi.fn(() => `blob:mock-url-${++counter}`)
  window.URL.revokeObjectURL = vi.fn()
})

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

describe('acceptance: export streams/downloads the sanitized artifact', () => {
  it('turns the resolved artifact into a real download link keyed to the receipt', async () => {
    const blob = new Blob(['sanitized-bytes'], { type: 'application/octet-stream' })
    const onExport = vi.fn().mockResolvedValue({
      receiptId: 'rcpt-123',
      blob,
      filename: 'project-export.zip',
    })
    render(<ExportDialog onExport={onExport} />)

    fireEvent.click(screen.getByRole('button', { name: /^export$/i }))
    await waitFor(() => expect(screen.getByText('rcpt-123')).toBeTruthy())

    expect(URL.createObjectURL).toHaveBeenCalledWith(blob)
    const link = screen.getByRole('link', { name: /download/i })
    expect(link).toHaveAttribute('href', expect.stringMatching(/^blob:mock-url/))
    expect(link).toHaveAttribute('download', 'project-export.zip')
  })

  it('says up front that the artifact is a sanitized copy, not a raw working file', () => {
    render(<ExportDialog onExport={vi.fn()} />)
    expect(screen.getByText(/sanitized copy/i)).toBeTruthy()
  })

  it('revokes the previous blob URL when the same dialog exports again', async () => {
    const first = new Blob(['a'])
    const second = new Blob(['b'])
    const onExport = vi.fn()
      .mockResolvedValueOnce({ receiptId: 'r1', blob: first, filename: 'a.zip' })
      .mockResolvedValueOnce({ receiptId: 'r2', blob: second, filename: 'b.zip' })
    render(<ExportDialog onExport={onExport} />)

    fireEvent.click(screen.getByRole('button', { name: /^export$/i }))
    await waitFor(() => expect(screen.getByText('r1')).toBeTruthy())
    const firstUrl = screen.getByRole('link', { name: /download/i }).getAttribute('href')

    fireEvent.click(screen.getByRole('button', { name: /export again/i }))
    await waitFor(() => expect(screen.getByText('r2')).toBeTruthy())
    const secondUrl = screen.getByRole('link', { name: /download/i }).getAttribute('href')

    expect(secondUrl).not.toBe(firstUrl)
    expect(URL.revokeObjectURL).toHaveBeenCalledWith(firstUrl)
  })
})

describe('acceptance: progress is indeterminate until measured and timeout has an unknown outcome', () => {
  it('renders live progress as the export reports it', async () => {
    const d = deferred()
    let onProgress
    const onExport = vi.fn((args) => { onProgress = args.onProgress; return d.promise })
    render(<ExportDialog onExport={onExport} />)

    fireEvent.click(screen.getByRole('button', { name: /^export$/i }))
    await waitFor(() => expect(onExport).toHaveBeenCalled())

    expect(screen.getByRole('progressbar')).not.toHaveAttribute('value')
    expect(screen.getByRole('progressbar')).not.toHaveAttribute('aria-valuenow')
    expect(screen.getByText('Preparing download...')).toBeTruthy()
    expect(screen.queryByText('0%')).toBeNull()

    act(() => { onProgress(0.5) })
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '50')

    act(() => { onProgress(1.4) }) // out-of-range input stays bounded to 100%
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '100')

    d.resolve({ receiptId: 'r-done', blob: new Blob(['x']), filename: 'x.zip' })
    await waitFor(() => expect(screen.getByText('r-done')).toBeTruthy())
  })

  it('aborts and explains the unknown outcome without offering retry when the export hangs', async () => {
    vi.useFakeTimers()
    try {
      let capturedSignal
      const onExport = vi.fn(({ signal }) => {
        capturedSignal = signal
        return new Promise(() => {}) // never settles on its own
      })
      render(<ExportDialog onExport={onExport} timeoutMs={5000} />)

      fireEvent.click(screen.getByRole('button', { name: /^export$/i }))
      await act(async () => { await vi.advanceTimersByTimeAsync(5000) })

      expect(capturedSignal.aborted).toBe(true)
      const alertEl = screen.getByRole('alert')
      expect(alertEl.textContent).toMatch(/could not confirm whether the export finished/i)
      expect(alertEl.textContent).toMatch(/check the project history before exporting again/i)
      expect(alertEl.textContent).not.toMatch(/5000|\bms\b|failed to fetch/i)
      expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('exports the timeout as a named error that does not authorize a retry', () => {
    const err = new ExportTimeoutError(1000)
    expect(err.name).toBe('ExportTimeoutError')
    expect(err.retryable).toBe(false)
    expect(err.timeoutMs).toBe(1000)
    expect(err.message).toMatch(/could not confirm whether the export finished/i)
    expect(err.message).not.toMatch(/1000 ms/)
  })

  it('keeps the unknown outcome after a late response without starting a duplicate export', async () => {
    vi.useFakeTimers()
    try {
      const pending = deferred()
      const onExport = vi.fn(() => pending.promise)
      render(<ExportDialog onExport={onExport} timeoutMs={1000} />)

      fireEvent.click(screen.getByRole('button', { name: /^export$/i }))
      await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
      expect(screen.getByRole('alert')).toBeTruthy()
      expect(screen.queryByRole('button', { name: /retry|export again|^export$/i })).toBeNull()

      await act(async () => {
        pending.resolve({ receiptId: 'r-late', blob: new Blob(['y']), filename: 'y.zip' })
      })
      expect(onExport).toHaveBeenCalledTimes(1)
      expect(screen.getByRole('alert')).toHaveTextContent('could not confirm whether the export finished')
      expect(screen.queryByText('r-late')).toBeNull()
      expect(screen.queryByRole('link', { name: /download/i })).toBeNull()
      expect(URL.createObjectURL).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('a non-timeout failure marked retryable:false renders without a retry affordance', async () => {
    const onExport = vi.fn().mockRejectedValue({ message: 'Workspace quota exceeded.', retryable: false })
    render(<ExportDialog onExport={onExport} />)

    fireEvent.click(screen.getByRole('button', { name: /^export$/i }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/quota exceeded/i))
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()
  })
})

describe('acceptance: the receipt id is shown', () => {
  it('renders the exact receipt id the server returned once the export completes', async () => {
    const onExport = vi.fn().mockResolvedValue({ receiptId: 'receipt-abc-999', blob: new Blob(['z']), filename: 'z.zip' })
    render(<ExportDialog onExport={onExport} />)

    fireEvent.click(screen.getByRole('button', { name: /^export$/i }))
    await waitFor(() => expect(screen.getByText('receipt-abc-999')).toBeTruthy())
  })

  it('treats a missing receipt id on an otherwise-successful response as a retryable error, never a silent blank', async () => {
    const onExport = vi.fn().mockResolvedValue({ blob: new Blob(['z']), filename: 'z.zip' })
    render(<ExportDialog onExport={onExport} />)

    fireEvent.click(screen.getByRole('button', { name: /^export$/i }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/without a receipt id/i))
    expect(screen.getByRole('button', { name: /retry/i })).toBeTruthy()
  })

  it('shows no receipt before an export has been run', () => {
    render(<ExportDialog onExport={vi.fn()} />)
    expect(screen.queryByText(/^Receipt:/)).toBeNull()
  })
})
