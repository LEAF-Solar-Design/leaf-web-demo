import { describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { deliverVercelCallback, parseVercelCallback } from '../../public/vercel-callback.js'

const state = 's'.repeat(43)
const query = `?state=${state}&code=code_123&configurationId=icfg_123`
describe('standalone Vercel callback', () => {
  it('strips all callback material before broadcasting only the bounded outcome', () => {
    const calls = []
    const postMessage = vi.fn((value) => calls.push(['message', structuredClone(value)]))
    const close = vi.fn()
    const page = { location: { search: `${query}&next=https://evil.test&teamId=untrusted&source=other`, hash: '#secret' },
      history: { replaceState: vi.fn(() => calls.push(['strip'])) },
      BroadcastChannel: class { constructor(name) { calls.push(['channel', name]) } postMessage = postMessage; close = close },
      fetch: vi.fn(), localStorage: { setItem: vi.fn() }, sessionStorage: { setItem: vi.fn() } }
    expect(deliverVercelCallback(page)).toBe(true)
    expect(page.history.replaceState).toHaveBeenCalledWith(null, '', '/vercel-callback.html')
    expect(calls).toEqual([['strip'], ['channel', `studio-vercel-${state}`],
      ['message', { state, outcome: 'authorized', code: 'code_123', configurationId: 'icfg_123' }]])
    expect(close).toHaveBeenCalledOnce()
    expect(page.fetch).not.toHaveBeenCalled()
    expect(page.localStorage.setItem).not.toHaveBeenCalled()
    expect(page.sessionStorage.setItem).not.toHaveBeenCalled()
  })
  it.each([`${query}&state=${state}`, `${query}&code=other`, `${query}&configurationId=other`,
    '?state=short&code=c&configurationId=c', `?state=${state}&code=%0A&configurationId=c`,
    `?state=${state}&code=c&configurationId=bad%2Fid`, `${query}&code=${'x'.repeat(8192)}`])('rejects malformed or duplicate required fields', (value) => {
    expect(parseVercelCallback(value)).toBeNull()
  })
  it('reports denial without forwarding provider error text or credentials', () => {
    expect(parseVercelCallback(`${query}&error=access_denied&error_description=secret`)).toEqual({ state, outcome: 'denied' })
  })
  it('uses static no-referrer HTML and an external same-origin module', () => {
    const html = readFileSync('public/vercel-callback.html', 'utf8')
    expect(html).toContain('name="referrer" content="no-referrer"')
    expect(html).toContain('type="module" src="/vercel-callback.js"')
    expect(html).not.toMatch(/https?:|auth0|telemetry|localStorage|sessionStorage/i)
  })
})
