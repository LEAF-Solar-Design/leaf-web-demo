import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

let streamHandlers
const closeStream = vi.fn()
vi.mock('./converse.js', () => ({
  openStream: vi.fn((_sessionId, _after, handlers) => {
    streamHandlers = handlers
    return { close: closeStream }
  }),
}))
vi.mock('./api.js', () => ({
  config: { apiBase: '', tenant: 'tenant-from-config' },
  authHeaders: () => ({ Authorization: 'Bearer test' }),
  noteUnauthorized: (res) => res,
}))

const { useAnnotations } = await import('./useAnnotations.js')

const H40 = 'a'.repeat(40)
const H64 = 'b'.repeat(64)
const projection = (over = {}) => ({
  decision_copy: 'Review 2 annotation changes.',
  batch_id: 'batch-1', revision: 1, state: 'pending', kind: 'apply',
  payload_digest: H64, payload_count: 2,
  base_version: 3, base_commit: H40, base_tree: H40,
  preview_commit: H40, preview_tree: H40,
  retry_of_batch_id: null, reverses_batch_id: null,
  reverses_commit: null, reverses_tree: null, applied_version: null,
  target_version: 3, target_commit: H40, target_tree: H40,
  ...over,
})
const response = (body, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
})

function Probe({ sessionId = 'session-1', enabled = true }) {
  const state = useAnnotations(sessionId, { enabled })
  return (
    <div>
      <span data-testid="batch">{state.annotation?.batchId || 'none'}</span>
      <span data-testid="busy">{String(state.busy)}</span>
      <span data-testid="error">{state.error || ''}</span>
      <button type="button" onClick={state.accept}>accept</button>
      <button type="button" onClick={state.retry}>retry</button>
    </div>
  )
}

beforeEach(() => {
  streamHandlers = null
  closeStream.mockClear()
  globalThis.fetch = vi.fn()
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('authoritative annotation projection', () => {
  it('reads on mount and rereads an annotation event without panel state', async () => {
    globalThis.fetch
      .mockResolvedValueOnce(response(projection()))
      .mockResolvedValueOnce(response(projection({ batch_id: 'batch-2', revision: 2 })))
    render(<Probe />)
    await waitFor(() => expect(screen.getByTestId('batch').textContent).toBe('batch-1'))
    streamHandlers.onEvent({ type: 'annotation_retry_previewed' })
    await waitFor(() => expect(screen.getByTestId('batch').textContent).toBe('batch-2'))
  })

  it('latches a double tap, sends only a fresh decision key, then rereads', async () => {
    let finishAction
    globalThis.fetch.mockImplementation(async (_url, init = {}) => {
      if (init.method === 'POST') {
        await new Promise((resolve) => { finishAction = resolve })
        return response(projection({ state: 'accepted', applied_version: 4, target_version: 4 }))
      }
      return response(projection())
    })
    render(<Probe />)
    await waitFor(() => expect(screen.getByTestId('batch').textContent).toBe('batch-1'))
    fireEvent.click(screen.getByRole('button', { name: 'accept' }))
    fireEvent.click(screen.getByRole('button', { name: 'accept' }))
    await waitFor(() => expect(screen.getByTestId('busy').textContent).toBe('true'))
    expect(globalThis.fetch.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
    const [, init] = globalThis.fetch.mock.calls.find(([, options]) => options?.method === 'POST')
    const body = JSON.parse(init.body)
    expect(Object.keys(body)).toEqual(['decision_key'])
    expect(body.decision_key).toMatch(/^annotation-decision-/)
    expect(JSON.stringify(body)).not.toMatch(/tenant|project|drawing|repo|commit|tree|payload|actor|path|ref/i)
    finishAction()
    await waitFor(() => expect(screen.getByTestId('busy').textContent).toBe('false'))
    expect(globalThis.fetch.mock.calls.filter(([, options]) => options?.method === 'GET')).toHaveLength(2)
  })

  it('keeps the authoritative card and one safe error when an action fails', async () => {
    globalThis.fetch
      .mockResolvedValueOnce(response(projection()))
      .mockResolvedValueOnce(response({ detail: 'private repository failure' }, 404))
    render(<Probe />)
    await waitFor(() => expect(screen.getByTestId('batch').textContent).toBe('batch-1'))
    fireEvent.click(screen.getByRole('button', { name: 'accept' }))
    await waitFor(() => expect(screen.getByTestId('error').textContent).toMatch(/nothing changed/i))
    expect(screen.getByTestId('batch').textContent).toBe('batch-1')
    expect(screen.getByTestId('error').textContent).not.toContain('repository')
  })

  it('does not mount or let a late read cross an enablement fence', async () => {
    let finish
    globalThis.fetch.mockImplementation(() => new Promise((resolve) => { finish = resolve }))
    const { rerender } = render(<Probe enabled />)
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledTimes(1))
    rerender(<Probe enabled={false} />)
    finish(response(projection()))
    await new Promise((resolve) => setTimeout(resolve, 10))
    expect(screen.getByTestId('batch').textContent).toBe('none')
    expect(closeStream).toHaveBeenCalled()
    const before = globalThis.fetch.mock.calls.length
    rerender(<Probe enabled={false} sessionId="session-2" />)
    expect(globalThis.fetch.mock.calls).toHaveLength(before)
  })
})
