/**
 * T1 overlay hook and client. Each test names the failure it prevents.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'

vi.mock('./api.js', () => ({
  config: { apiBase: '', tenant: 't1' },
  authHeaders: () => ({}),
  noteUnauthorized: () => {},
}))

const { fetchOverlay, decideOverlay } = await import('./overlayClient.js')
const { useOverlay } = await import('./useOverlay.js')

function Probe({ sessionId }) {
  const o = useOverlay(sessionId)
  return <div data-testid="v">{o.loaded ? `v${o.documentVersion}` : 'loading'}</div>
}

beforeEach(() => {
  globalThis.fetch = vi.fn()
})
afterEach(() => {
  cleanup()
  document.documentElement.removeAttribute('style')
  vi.restoreAllMocks()
})

const ok = (body) => ({ ok: true, status: 200, json: async () => body })

describe('the read on load', () => {
  it('applies the tenant overlay to the DOM', async () => {
    globalThis.fetch.mockResolvedValue(ok({
      tokens: { 'color.accent': '#123456' }, document_version: 3,
    }))
    render(<Probe sessionId="s1" />)
    await waitFor(() => expect(screen.getByTestId('v').textContent).toBe('v3'))
    expect(document.documentElement.style.getPropertyValue('--accent')).toBe('#123456')
  })

  it('leaves the committed defaults alone when the read FAILS', async () => {
    // A theme read is not worth breaking the app over. Throwing here would
    // turn a cosmetic outage into a blank page.
    globalThis.fetch.mockRejectedValue(new Error('network down'))
    render(<Probe sessionId="s1" />)
    await waitFor(() => expect(screen.getByTestId('v').textContent).toBe('v0'))
    expect(document.documentElement.getAttribute('style')).toBeNull()
  })

  it('survives a non-2xx without applying anything', async () => {
    globalThis.fetch.mockResolvedValue({ ok: false, status: 500, json: async () => null })
    const out = await fetchOverlay('s1')
    expect(out.tokens).toEqual({})
  })

  it('removes the overlay on unmount so it cannot outlive the component', async () => {
    globalThis.fetch.mockResolvedValue(ok({
      tokens: { 'color.accent': '#123456' }, document_version: 1,
    }))
    const { unmount } = render(<Probe sessionId="s1" />)
    await waitFor(() =>
      expect(document.documentElement.style.getPropertyValue('--accent')).toBe('#123456'))
    unmount()
    expect(document.documentElement.style.getPropertyValue('--accent')).toBe('')
  })
})

describe('deciding', () => {
  it('sends the CAS witness and the actor', async () => {
    globalThis.fetch.mockResolvedValue(ok({ state: 'approved', document_version: 4 }))
    await decideOverlay('p-1', {
      approve: true, decisionKey: 'k1234567', documentVersion: 3, actor: 'op@x',
    })
    const [, init] = globalThis.fetch.mock.calls[0]
    expect(JSON.parse(init.body).document_version).toBe(3)
    expect(init.headers['X-Actor']).toBe('op@x')
  })

  it('REFUSES to decide without a version', async () => {
    // A tap against a version the operator never saw is the exact defect a
    // review found in the server's deny path.
    await expect(decideOverlay('p-1', {
      approve: true, decisionKey: 'k1234567', documentVersion: undefined,
    })).rejects.toThrow(/documentVersion/)
    expect(globalThis.fetch).not.toHaveBeenCalled()
  })

  it('THROWS when the decision fails, unlike the read', async () => {
    // Silence here would tell the operator their approval landed when the
    // tenant never changed.
    globalThis.fetch.mockResolvedValue({
      ok: false, status: 409,
      json: async () => ({ error: { error_code: 'version_conflict' } }),
    })
    await expect(decideOverlay('p-1', {
      approve: true, decisionKey: 'k1234567', documentVersion: 1,
    })).rejects.toMatchObject({ status: 409, errorCode: 'version_conflict' })
  })

  it('treats a 200 carrying an error envelope as a failure', async () => {
    globalThis.fetch.mockResolvedValue(ok({ error: { error_code: 'already_decided' } }))
    await expect(decideOverlay('p-1', {
      approve: false, decisionKey: 'k1234567', documentVersion: 1,
    })).rejects.toMatchObject({ errorCode: 'already_decided' })
  })
})
