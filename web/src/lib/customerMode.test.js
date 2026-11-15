// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { isCustomerMode } from './customerMode.js'

afterEach(() => {
  vi.restoreAllMocks()
  window.localStorage.removeItem('leaf.devChrome')
  window.history.replaceState({}, '', '/')
})

describe('customer chrome', () => {
  it('defaults to customer mode, including ordinary signed-in plans', () => {
    expect(isCustomerMode()).toBe(true)
    expect(isCustomerMode({ tier: 'pro', role: 'member' })).toBe(true)
  })
  it.each(['admin', 'dev'])('exposes diagnostics for the %s plan or role', (value) => {
    expect(isCustomerMode(value)).toBe(false)
    expect(isCustomerMode({ plan: value })).toBe(false)
    expect(isCustomerMode({ role: value })).toBe(false)
    expect(isCustomerMode({ tier: value })).toBe(false)
  })
  it('honors the explicit development URL flag only when set to 1', () => {
    window.history.replaceState({}, '', '/?dev=0')
    expect(isCustomerMode()).toBe(true)
    window.history.replaceState({}, '', '/?dev=1')
    expect(isCustomerMode()).toBe(false)
  })
  it('honors the local development preference', () => {
    window.localStorage.setItem('leaf.devChrome', '1')
    expect(isCustomerMode()).toBe(false)
  })
  it('preserves an admin role when the signed-in plan is an ordinary plan', () => {
    expect(isCustomerMode({ tier: 'pro', role: 'admin' })).toBe(false)
  })
  it('stays usable when browser storage is unavailable', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('Storage unavailable') })
    expect(isCustomerMode()).toBe(true)
    window.history.replaceState({}, '', '/?dev=1')
    expect(isCustomerMode()).toBe(false)
  })
})
