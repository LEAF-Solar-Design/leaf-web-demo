import { describe, expect, it, vi } from 'vitest'
import { editingStyleKey, readEditingStyle, writeEditingStyle } from './editingStylePreference.js'

describe('personal editing style', () => {
  it('uses distinct account buckets and leaves guests out of storage', () => {
    const rows = new Map(), storage = { getItem: vi.fn(key => rows.get(key)), setItem: vi.fn((key, value) => rows.set(key, value)) }
    const alice = 'account:["issuer","alice"]', bob = 'account:["issuer","bob"]'
    expect(editingStyleKey(alice)).toBe(`leaf.editingStyle.v1:${encodeURIComponent(alice)}`)
    expect(readEditingStyle(alice, storage)).toBe('sidebar')
    expect(writeEditingStyle(alice, 'environment', storage)).toBe(true)
    expect(readEditingStyle(alice, storage)).toBe('environment')
    expect(readEditingStyle(bob, storage)).toBe('sidebar')
    storage.getItem.mockClear(); storage.setItem.mockClear()
    for (const scope of [null, undefined, '', 'guest']) {
      expect(readEditingStyle(scope, storage)).toBe('sidebar')
      expect(writeEditingStyle(scope, 'environment', storage)).toBe(false)
    }
    expect(storage.getItem).not.toHaveBeenCalled()
    expect(storage.setItem).not.toHaveBeenCalled()
  })
  it('defaults invalid or unavailable values and contains storage failures', () => {
    const account = 'account:["issuer","alice"]'
    for (const value of [null, '', 'Environment', '{}', 'sidebar']) expect(readEditingStyle(account, { getItem: () => value })).toBe('sidebar')
    const fail = () => { throw new Error('Storage blocked') }
    expect(readEditingStyle(account, { getItem: fail })).toBe('sidebar')
    expect(writeEditingStyle(account, 'sidebar', { setItem: fail })).toBe(false)
    expect(writeEditingStyle(account, 'other', { setItem: fail })).toBe(false)
  })
})
