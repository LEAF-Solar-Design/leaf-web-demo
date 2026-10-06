import { describe, expect, it } from 'vitest'
import { SINGLE_KEY_SHORTCUTS_KEY, readSingleKeyShortcuts, writeSingleKeyShortcuts } from './singleKeyPreference.js'

function store() {
  const values = new Map()
  return { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) }
}

describe('single-key shortcut preference', () => {
  it('S25 defaults on with an empty or missing store', () => {
    expect(readSingleKeyShortcuts(store())).toBe(true)
    expect(readSingleKeyShortcuts(null)).toBe(true)
  })
  it('S25 reads off only from the exact stored value', () => {
    const storage = store()
    storage.setItem(SINGLE_KEY_SHORTCUTS_KEY, 'off')
    expect(readSingleKeyShortcuts(storage)).toBe(false)
  })
  it.each(['Off', 'off ', '0', 'false', 'on'])('S25 treats stored %s as on', (value) => {
    const storage = store()
    storage.setItem(SINGLE_KEY_SHORTCUTS_KEY, value)
    expect(readSingleKeyShortcuts(storage)).toBe(true)
  })
  it('S25 fails open (on) when storage throws', () => {
    expect(readSingleKeyShortcuts({ getItem() { throw new Error('Unavailable') } })).toBe(true)
  })
  it('S25 writes off and on and reads each back', () => {
    const storage = store()
    expect(writeSingleKeyShortcuts(false, storage)).toBe(true)
    expect(readSingleKeyShortcuts(storage)).toBe(false)
    expect(writeSingleKeyShortcuts(true, storage)).toBe(true)
    expect(readSingleKeyShortcuts(storage)).toBe(true)
  })
  it.each(['off', 0, null, undefined])('S25 ignores the non-boolean write %s', (value) => {
    const storage = store()
    expect(writeSingleKeyShortcuts(value, storage)).toBe(false)
    expect(storage.getItem(SINGLE_KEY_SHORTCUTS_KEY)).toBeNull()
  })
  it('S25 returns false when writing throws or there is no store', () => {
    expect(writeSingleKeyShortcuts(false, { setItem() { throw new Error('Unavailable') } })).toBe(false)
    expect(writeSingleKeyShortcuts(false, null)).toBe(false)
  })
})
