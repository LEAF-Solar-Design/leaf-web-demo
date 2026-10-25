import { describe, expect, it } from 'vitest'
import { describeLockHolder } from './checkoutIdentity.js'

describe('describeLockHolder', () => {
  it('recognizes your own holder before hiding session ids', () => {
    expect(describeLockHolder('sess-mine', 'sess-mine')).toBe('you in another tab')
  })

  it('hides opaque session ids', () => {
    expect(describeLockHolder('sess-72d58f4d-36af-421b-a164-a98394156cfa', 'sess-mine')).toBe('another session')
  })

  it('describes a save holding the lock', () => {
    expect(describeLockHolder('cad-edit-save')).toBe('a save in progress')
  })

  it('preserves readable names and emails', () => {
    expect(describeLockHolder('Sam Designer')).toBe('Sam Designer')
    expect(describeLockHolder('sam@example.com')).toBe('sam@example.com')
  })

  it('describes missing holders without showing an empty label', () => {
    for (const holder of [null, undefined, '', '   ']) {
      expect(describeLockHolder(holder)).toBe('another session')
    }
  })
})
