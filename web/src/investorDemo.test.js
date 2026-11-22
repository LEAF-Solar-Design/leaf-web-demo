// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { INVESTOR_LINK_GLOBAL } from './lib/investorLinkCapture.js'
import { serviceSession } from './lib/serviceAuthSession.js'
import {
  bootstrapInvestorLink, consumeInvestorNotice, INVESTOR_ISSUER, INVESTOR_NOTICE_KEY,
  isInvestorSession, isInvestorToken,
} from './investorDemo.js'

const link = 'v1.1234abcd.1800000000.1800000050.' + 'a'.repeat(43)
const jwt = (iss, sub = 'visitor') => 'e30.' + btoa(JSON.stringify({ iss, sub })).replace(/=+$/, '') + '.c2ln'
const session = jwt(INVESTOR_ISSUER)
const org = '12345678-1234-1234-1234-123456789abc'
const body = () => ({ session, org_id: org, expires_at: new Date(Date.now() + 60000).toISOString() })
const response = (value = body()) => ({ status: 200, json: async () => value })
const capture = (value = link) => Object.defineProperty(window, INVESTOR_LINK_GLOBAL, { configurable: true, value })
let fetchImpl
const boot = (options = {}) => bootstrapInvestorLink({ fetchImpl, ...options })

beforeEach(() => {
  fetchImpl = vi.fn(async () => response())
  localStorage.clear()
  sessionStorage.clear()
})
afterEach(() => {
  try {
    for (const sink of [JSON.stringify(localStorage), JSON.stringify(sessionStorage), document.cookie]) {
      expect(sink).not.toContain(link)
    }
    expect(Object.hasOwn(window, INVESTOR_LINK_GLOBAL)).toBe(false)
  } finally {
    serviceSession().human()
    localStorage.clear()
    sessionStorage.clear()
    vi.useRealTimers()
  }
})

describe('investor issuer classification', () => {
  it.each([[session, true], [jwt(INVESTOR_ISSUER + 'extra'), false], [jwt('https://accounts.auth0.com/'), false],
    ['garbage', false], ['a.bnVsbA.c', false], [null, false], [42, false]])('classifies a compact token', (token, expected) => {
    expect(isInvestorToken(token)).toBe(expected)
  })
  it('handles unavailable storage', () => {
    expect(isInvestorSession({ getItem() { throw new Error() } })).toBe(false)
  })
  it('decodes each stored token once until it changes', () => {
    const decode = vi.spyOn(globalThis, 'atob')
    try {
      localStorage.setItem('leaf.jwt', jwt(INVESTOR_ISSUER, 'first'))
      expect(isInvestorSession()).toBe(true)
      expect(isInvestorSession()).toBe(true)
      expect(decode).toHaveBeenCalledTimes(1)
      localStorage.setItem('leaf.jwt', jwt('other'))
      expect(isInvestorSession()).toBe(false)
      expect(isInvestorSession()).toBe(false)
      expect(decode).toHaveBeenCalledTimes(2)
      localStorage.removeItem('leaf.jwt')
      expect(isInvestorSession()).toBe(false)
      expect(decode).toHaveBeenCalledTimes(2)
    } finally { decode.mockRestore() }
  })
})

it('does nothing when the page had no link', async () => {
  localStorage.setItem('leaf.jwt', session)
  sessionStorage.setItem(INVESTOR_NOTICE_KEY, '1')
  expect(await boot()).toBe('none')
  expect(fetchImpl).not.toHaveBeenCalled()
  expect(isInvestorSession()).toBe(true)
  expect(consumeInvestorNotice()).toBe(true)
})

it('uses the supplied link and only checks the window when the option is absent', async () => {
  capture('')
  try {
    expect(await boot({ link: undefined })).toBe('none')
    expect(await boot({ link })).toBe('signed-in')
    expect(window[INVESTOR_LINK_GLOBAL]).toBe('')
    expect(fetchImpl).toHaveBeenCalledOnce()
  } finally { delete window[INVESTOR_LINK_GLOBAL] }
})

it.each(['human', 'service'])('preserves an existing %s session', async mode => {
  capture()
  if (mode === 'human') localStorage.setItem('leaf.jwt', 'existing-human')
  else serviceSession().select('11111111-1111-4111-8111-111111111111')
  const before = [JSON.stringify(localStorage), JSON.stringify(sessionStorage)]
  expect(await boot()).toBe('kept-existing')
  expect(fetchImpl).not.toHaveBeenCalled()
  expect([JSON.stringify(localStorage), JSON.stringify(sessionStorage)]).toEqual(before)
})

it.each(['malformed', 401, 429, 503, 'network', 'wrong issuer', 'bad org', 'expired', 'foreign API'])(
  'removes a stale investor session and records one notice on %s', async failure => {
    localStorage.setItem('leaf.jwt', jwt(INVESTOR_ISSUER, 'stale'))
    localStorage.setItem('leaf.org_id', org)
    localStorage.setItem('leaf.investorDemo.org', org)
    capture(failure === 'malformed' ? '' : link)
    const value = body()
    if (failure === 'wrong issuer') value.session = jwt('other')
    if (failure === 'bad org') value.org_id = org.toUpperCase()
    if (failure === 'expired') value.expires_at = new Date(Date.now() - 1000).toISOString()
    fetchImpl.mockImplementation(async () => {
      if (failure === 'network') throw new Error('offline')
      return typeof failure === 'number' ? { status: failure } : response(value)
    })
    expect(await boot(failure === 'foreign API' ? { apiBase: 'https://other.invalid' } : {})).toBe('failed')
    expect(fetchImpl).toHaveBeenCalledTimes(['malformed', 'foreign API'].includes(failure) ? 0 : 1)
    expect(localStorage.getItem('leaf.jwt')).toBeNull()
    expect(localStorage.getItem('leaf.org_id')).toBeNull()
    expect(localStorage.getItem('leaf.investorDemo.org')).toBeNull()
    expect(consumeInvestorNotice()).toBe(true)
    expect(consumeInvestorNotice()).toBe(false)
  })

it('aborts after four seconds even if the body reader never resolves', async () => {
  vi.useFakeTimers()
  capture()
  fetchImpl.mockResolvedValue({ status: 200, json: () => new Promise(() => {}) })
  const pending = boot()
  const { signal } = fetchImpl.mock.calls[0][1]
  await vi.advanceTimersByTimeAsync(3999)
  expect(signal.aborted).toBe(false)
  await vi.advanceTimersByTimeAsync(1)
  expect(signal.aborted).toBe(true)
  expect(await pending).toBe('failed')
  expect(consumeInvestorNotice()).toBe(true)
})

it.each([false, true])('stores only the session and org after a single credential-free exchange (stale=%s)', async stale => {
  capture()
  if (stale) localStorage.setItem('leaf.jwt', jwt(INVESTOR_ISSUER, 'stale'))
  sessionStorage.setItem(INVESTOR_NOTICE_KEY, '1')
  expect(await boot({ apiBase: location.origin })).toBe('signed-in')
  expect(fetchImpl).toHaveBeenCalledExactlyOnceWith(location.origin + '/api/site/investor-demo/session', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'omit', cache: 'no-store',
    redirect: 'error', signal: expect.any(AbortSignal), body: JSON.stringify({ token: link }),
  })
  expect({ ...localStorage }).toEqual({ 'leaf.jwt': session, 'leaf.org_id': org, 'leaf.investorDemo.org': org })
  expect(sessionStorage.length).toBe(0)
})

it('keeps a human token stored while the exchange is in flight', async () => {
  capture()
  const pending = boot()
  localStorage.setItem('leaf.jwt', 'human')
  expect(await pending).toBe('kept-existing')
  expect({ ...localStorage }).toEqual({ 'leaf.jwt': 'human' })
})
