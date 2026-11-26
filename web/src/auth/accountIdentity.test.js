// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  ACCOUNT_EMAIL_KEY, clearAccountIdentity, formatAccountLine, readAccountIdentity,
  setAccountEmail, setAccountWorkspace, subscribeAccountIdentity,
} from './accountIdentity.js'
import { serviceSession } from '../lib/serviceAuthSession.js'

const tokenFor = sub => `header.${btoa(JSON.stringify({ sub })).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')}.signature`

beforeEach(() => {
  serviceSession().human()
  localStorage.setItem('leaf.jwt', tokenFor('account-a'))
  clearAccountIdentity()
})
afterEach(() => {
  vi.restoreAllMocks()
  serviceSession().human()
  clearAccountIdentity()
  localStorage.removeItem('leaf.jwt')
})

it('stores a trimmed email and reads it back', () => {
  setAccountEmail('  a@example.com  ')
  expect(JSON.parse(localStorage.getItem(ACCOUNT_EMAIL_KEY))).toEqual({ sub: 'account-a', email: 'a@example.com' })
  expect(readAccountIdentity().email).toBe('a@example.com')
})

it('drops the email when the current token belongs to another subject', () => {
  setAccountEmail('a@example.com')
  setAccountWorkspace({ id: '1b60bef6-1234', name: 'Leaf Automation' })
  const before = readAccountIdentity()
  localStorage.setItem('leaf.jwt', tokenFor('account-b'))
  const after = readAccountIdentity()
  expect(after.email).toBeNull()
  expect(formatAccountLine(after)).toBe('Leaf Automation (1b60bef6)')
  expect(after).not.toBe(before)
  expect(readAccountIdentity()).toBe(after)
})

it.each([null, 'invalid-token', 'header.invalid.signature', tokenFor(null), tokenFor(' ')])
  ('hides and removes email without a valid token subject: %j', token => {
    setAccountEmail('a@example.com')
    if (token === null) localStorage.removeItem('leaf.jwt')
    else localStorage.setItem('leaf.jwt', token)
    expect(readAccountIdentity().email).toBeNull()
    setAccountEmail('b@example.com')
    expect(localStorage.getItem(ACCOUNT_EMAIL_KEY)).toBeNull()
  })

it.each(['a@example.com', '{invalid', JSON.stringify({ sub: 'account-a', email: 'invalid' })])
  ('ignores legacy or invalid stored email: %j', stored => {
    localStorage.setItem(ACCOUNT_EMAIL_KEY, stored)
    expect(readAccountIdentity().email).toBeNull()
  })

it('hides human email in service mode', () => {
  setAccountEmail('a@example.com')
  serviceSession().select('11111111-1111-4111-8111-111111111111')
  expect(readAccountIdentity().email).toBeNull()
  expect(formatAccountLine(readAccountIdentity())).toBeNull()
})

it.each(['no-at-sign', 'a@b\u0000', 'a@b\n', 'a@b\u007f', `a@${'b'.repeat(298)}`, 123, null, '@'])
  ('removes a stored email for invalid value %j', value => {
    setAccountEmail('a@example.com')
    setAccountEmail(value)
    expect(localStorage.getItem(ACCOUNT_EMAIL_KEY)).toBeNull()
    expect(readAccountIdentity().email).toBeNull()
  })

it('keeps a valid workspace in memory and normalizes its name', () => {
  setAccountWorkspace({ id: '1b60bef6-1234', name: '  Leaf Automation  ' })
  expect(readAccountIdentity().workspace).toEqual({ id: '1b60bef6-1234', name: 'Leaf Automation' })
  expect(localStorage.getItem(ACCOUNT_EMAIL_KEY)).toBeNull()
  setAccountWorkspace({ id: 'id', name: null })
  expect(readAccountIdentity().workspace).toEqual({ id: 'id', name: null })
  setAccountWorkspace({ id: 'id', name: '   ' })
  expect(readAccountIdentity().workspace.name).toBeNull()
  setAccountWorkspace({ id: 'id', name: `  ${'x'.repeat(121)}  ` })
  expect(readAccountIdentity().workspace.name).toBe('x'.repeat(120))
})

it.each([null, 'id', {}, { id: '' }, { id: ' ' }, { id: 1 }, { id: 'x'.repeat(65) }])
  ('clears the workspace for invalid value %j', value => {
    setAccountWorkspace({ id: 'valid' })
    setAccountWorkspace(value)
    expect(readAccountIdentity().workspace).toBeNull()
  })

describe('formatAccountLine', () => {
  const named = { id: '1b60bef6-1234', name: 'Leaf Automation' }
  const unnamed = { id: '1b60bef6-1234', name: null }
  it.each([
    ['a@example.com', named, 'a@example.com · Leaf Automation (1b60bef6)'],
    ['a@example.com', unnamed, 'a@example.com · workspace 1b60bef6'],
    [null, named, 'Leaf Automation (1b60bef6)'],
    [null, unnamed, 'workspace 1b60bef6'],
    ['a@example.com', null, 'a@example.com'],
    [null, null, null],
  ])('formats email %j and workspace %j', (email, workspace, expected) => {
    expect(formatAccountLine({ email, workspace })).toBe(expected)
  })
})

it('keeps snapshot identity stable until the contents change', () => {
  const empty = readAccountIdentity()
  expect(readAccountIdentity()).toBe(empty)
  setAccountEmail('a@example.com')
  const email = readAccountIdentity()
  expect(email).not.toBe(empty)
  expect(readAccountIdentity()).toBe(email)
  setAccountEmail('a@example.com')
  expect(readAccountIdentity()).toBe(email)
  setAccountWorkspace({ id: 'id' })
  const workspace = readAccountIdentity()
  expect(workspace).not.toBe(email)
  expect(readAccountIdentity()).toBe(workspace)
})

it('clears both fields and notifies subscribers until they unsubscribe', () => {
  const listener = vi.fn()
  const off = subscribeAccountIdentity(listener)
  try {
    setAccountEmail('a@example.com')
    setAccountWorkspace({ id: 'id' })
    clearAccountIdentity()
    expect(readAccountIdentity()).toEqual({ email: null, workspace: null })
    expect(localStorage.getItem(ACCOUNT_EMAIL_KEY)).toBeNull()
    expect(listener).toHaveBeenCalledTimes(3)
    off()
    setAccountEmail('b@example.com')
    expect(listener).toHaveBeenCalledTimes(3)
  } finally { off() }
})

it('re-reads email on relevant storage events and ignores unrelated keys', () => {
  const listener = vi.fn()
  const off = subscribeAccountIdentity(listener)
  try {
    const before = readAccountIdentity()
    localStorage.setItem(ACCOUNT_EMAIL_KEY, JSON.stringify({ sub: 'account-a', email: 'b@example.com' }))
    window.dispatchEvent(new StorageEvent('storage', { key: ACCOUNT_EMAIL_KEY }))
    expect(listener).toHaveBeenCalledOnce()
    expect(readAccountIdentity()).not.toBe(before)
    expect(readAccountIdentity().email).toBe('b@example.com')
    window.dispatchEvent(new StorageEvent('storage', { key: 'unrelated' }))
    expect(listener).toHaveBeenCalledOnce()
    localStorage.removeItem(ACCOUNT_EMAIL_KEY)
    window.dispatchEvent(new StorageEvent('storage', { key: null }))
    expect(listener).toHaveBeenCalledTimes(2)
    expect(readAccountIdentity().email).toBeNull()
    off()
    window.dispatchEvent(new StorageEvent('storage', { key: ACCOUNT_EMAIL_KEY }))
    expect(listener).toHaveBeenCalledTimes(2)
  } finally { off() }
})

it('re-reads on JWT storage events and drops email after an account switch', () => {
  setAccountEmail('a@example.com')
  const listener = vi.fn()
  const off = subscribeAccountIdentity(listener)
  try {
    localStorage.setItem('leaf.jwt', tokenFor('account-b'))
    window.dispatchEvent(new StorageEvent('storage', { key: 'leaf.jwt' }))
    expect(listener).toHaveBeenCalledOnce()
    expect(readAccountIdentity().email).toBeNull()
    expect(formatAccountLine(readAccountIdentity())).toBeNull()
    localStorage.setItem('leaf.jwt', tokenFor('account-a'))
    window.dispatchEvent(new StorageEvent('storage', { key: 'leaf.jwt' }))
    expect(listener).toHaveBeenCalledTimes(2)
    expect(readAccountIdentity().email).toBe('a@example.com')
  } finally { off() }
})

it('swallows storage failures', () => {
  for (const method of ['getItem', 'setItem', 'removeItem']) {
    vi.spyOn(Storage.prototype, method).mockImplementation(() => { throw new Error('unavailable') })
  }
  expect(() => setAccountEmail('a@example.com')).not.toThrow()
  expect(() => setAccountEmail(null)).not.toThrow()
  expect(() => setAccountWorkspace({ id: 'id' })).not.toThrow()
  expect(() => clearAccountIdentity()).not.toThrow()
  expect(readAccountIdentity()).toEqual({ email: null, workspace: null })
})
