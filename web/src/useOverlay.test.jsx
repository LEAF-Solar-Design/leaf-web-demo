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
    expect(document.documentElement.style.getPropertyValue('--primary')).toBe('#123456')
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
      expect(document.documentElement.style.getPropertyValue('--primary')).toBe('#123456'))
    unmount()
    expect(document.documentElement.style.getPropertyValue('--primary')).toBe('')
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

describe('concurrent reads (sol-critic PR #439 round 2)', () => {
  it('coalesces a replay burst into ONE settling read', async () => {
    // A remount replays the transcript from seq 0, so every historical overlay
    // event fires a refresh. Unbounded, that is N concurrent GETs whose
    // responses can land out of order.
    let resolveFirst
    const gate = new Promise((r) => { resolveFirst = r })
    let calls = 0
    globalThis.fetch = vi.fn(async () => {
      calls += 1
      if (calls === 1) await gate
      return ok({ tokens: {}, document_version: calls, pending_proposal_id: null })
    })

    let hook
    function Burst() {
      hook = useOverlay('s-1')
      return <div data-testid="v">{hook.loaded ? `v${hook.documentVersion}` : 'loading'}</div>
    }
    render(<Burst />)
    await waitFor(() => expect(calls).toBe(1))   // the mount read is in flight

    hook.reload(); hook.reload(); hook.reload(); hook.reload()  // the burst
    resolveFirst()

    // One settling read after the in-flight one, never four.
    await waitFor(() => expect(screen.getByTestId('v').textContent).toBe('v2'))
    expect(calls).toBe(2)
  })

  it('a stale session read cannot fence out the new session', async () => {
    // The coalescing coordinator must not span sessions: an in-flight read for
    // the OLD session used to re-fetch it after the switch, take the newest
    // seq, and leave the previous overlay applied.
    let resolveOld
    const oldGate = new Promise((r) => { resolveOld = r })
    globalThis.fetch = vi.fn(async (url) => {
      if (String(url).includes('s-old')) {
        await oldGate
        return ok({ tokens: { 'color.accent': '#010101' },
                    document_version: 1, pending_proposal_id: null })
      }
      return ok({ tokens: { 'color.accent': '#020202' },
                  document_version: 2, pending_proposal_id: null })
    })

    const { rerender } = render(<Probe sessionId="s-old" />)
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled())
    rerender(<Probe sessionId="s-new" />)
    await waitFor(() => expect(screen.getByTestId('v').textContent).toBe('v2'))

    resolveOld()   // the old session's read lands LAST
    await new Promise((r) => setTimeout(r, 10))

    expect(screen.getByTestId('v').textContent).toBe('v2')
    expect(document.documentElement.style.getPropertyValue('--primary')).toBe('#020202')
  })

  it('a coalesced burst on the OLD session cannot re-fetch it after a switch', async () => {
    // The reviewer's exact scenario, and the one the seq fence alone does NOT
    // cover: the burst flag makes the old session's coalescing loop run a
    // SECOND fetch. Without the generation check in that loop, that late read
    // takes the newest seq and fences out the new session's own read, leaving
    // the previous tenant's overlay on screen.
    const urls = []
    let resolveOld
    const oldGate = new Promise((r) => { resolveOld = r })
    globalThis.fetch = vi.fn(async (url) => {
      urls.push(String(url))
      if (String(url).includes('s-old') && urls.length === 1) {
        await oldGate
        return ok({ tokens: { 'color.accent': '#010101' },
                    document_version: 1, pending_proposal_id: null })
      }
      if (String(url).includes('s-old')) {
        return ok({ tokens: { 'color.accent': '#0a0a0a' },
                    document_version: 9, pending_proposal_id: null })
      }
      return ok({ tokens: { 'color.accent': '#020202' },
                  document_version: 2, pending_proposal_id: null })
    })

    let hook
    function Probe2({ sessionId }) {
      hook = useOverlay(sessionId)
      return <div data-testid="v">{hook.loaded ? `v${hook.documentVersion}` : 'loading'}</div>
    }
    const { rerender } = render(<Probe2 sessionId="s-old" />)
    await waitFor(() => expect(urls.length).toBe(1))
    hook.reload()                    // raises the burst flag on the OLD loop
    rerender(<Probe2 sessionId="s-new" />)
    await waitFor(() => expect(screen.getByTestId('v').textContent).toBe('v2'))
    resolveOld()                     // the old loop wakes AFTER the switch
    await new Promise((r) => setTimeout(r, 20))

    expect(urls.filter((u) => u.includes('s-old')).length)
      .toBe(1)                       // the queued old-session read never ran
    expect(screen.getByTestId('v').textContent).toBe('v2')
    expect(document.documentElement.style.getPropertyValue('--primary')).toBe('#020202')
  })
})

describe('a decision that outlives its session (round 3)', () => {
  it('cannot write session A state after a switch to B', async () => {
    // decide() captures its refresh on session A. If the session changes while
    // the decision is in flight, that A-era callback used to read the CURRENT
    // generation, fetch A, pass both fences, and replace B's state.
    let resolveDecide
    const decideGate = new Promise((r) => { resolveDecide = r })
    globalThis.fetch = vi.fn(async (url, init) => {
      const target = String(url)
      if ((init?.method || 'GET') === 'POST') {
        await decideGate
        return ok({ proposal_id: 'p-1', state: 'approved', document_version: 1 })
      }
      if (target.includes('s-A')) {
        return ok({ tokens: { 'color.accent': '#0a0a0a' },
                    document_version: 1, pending_proposal_id: null })
      }
      return ok({ tokens: { 'color.accent': '#0b0b0b' },
                  document_version: 7, pending_proposal_id: null })
    })

    let hook
    function P({ sessionId }) {
      hook = useOverlay(sessionId)
      return <div data-testid="v">{hook.loaded ? `v${hook.documentVersion}` : 'loading'}</div>
    }
    const { rerender } = render(<P sessionId="s-A" />)
    await waitFor(() => expect(screen.getByTestId('v').textContent).toBe('v1'))

    const pending = hook.decide('p-1', { approve: true, documentVersion: 1 })
    rerender(<P sessionId="s-B" />)
    await waitFor(() => expect(screen.getByTestId('v').textContent).toBe('v7'))

    resolveDecide()
    await pending.catch(() => {})
    await new Promise((r) => setTimeout(r, 10))

    expect(screen.getByTestId('v').textContent).toBe('v7')
    expect(document.documentElement.style.getPropertyValue('--primary')).toBe('#0b0b0b')
  })
})
