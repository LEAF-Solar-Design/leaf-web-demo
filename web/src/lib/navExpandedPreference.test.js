import { describe, expect, it } from 'vitest'
import { NAV_EXPANDED_KEY, readNavExpanded, writeNavExpanded } from './navExpandedPreference.js'
import { surfaceContract } from '../site/productSurfaces.js'

function store() {
  const values = new Map()
  return { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) }
}

describe('nav rail expanded preference', () => {
  it('S20 reads a stored expanded and collapsed posture', () => {
    const storage = store()
    storage.setItem(NAV_EXPANDED_KEY, '1')
    expect(readNavExpanded(storage)).toBe(true)
    storage.setItem(NAV_EXPANDED_KEY, '0')
    expect(readNavExpanded(storage)).toBe(false)
  })

  it.each(['true', 'yes', '', ' 1'])('S20 treats stored %j as no preference', (value) => {
    const storage = store()
    storage.setItem(NAV_EXPANDED_KEY, value)
    expect(readNavExpanded(storage)).toBe(false)
  })

  it('S20 writes the posture and reads it back', () => {
    const storage = store()
    expect(writeNavExpanded(true, storage)).toBe(true)
    expect(storage.getItem(NAV_EXPANDED_KEY)).toBe('1')
    expect(readNavExpanded(storage)).toBe(true)
    expect(writeNavExpanded(false, storage)).toBe(true)
    expect(storage.getItem(NAV_EXPANDED_KEY)).toBe('0')
    expect(readNavExpanded(storage)).toBe(false)
  })

  it('S20 refuses a non-boolean write', () => {
    const storage = store()
    expect(writeNavExpanded('1', storage)).toBe(false)
    expect(writeNavExpanded(undefined, storage)).toBe(false)
    expect(storage.getItem(NAV_EXPANDED_KEY)).toBeNull()
  })

  it('S20 survives throwing or missing storage', () => {
    const throwing = {
      getItem() { throw new Error('Unavailable') },
      setItem() { throw new Error('Unavailable') },
    }
    expect(readNavExpanded(throwing)).toBe(false)
    expect(writeNavExpanded(true, throwing)).toBe(false)
    expect(readNavExpanded(null)).toBe(false)
    expect(writeNavExpanded(true, null)).toBe(false)
  })

  it.each(['cad', 'solar'])('S20 no stored value keeps the %s rail collapsed to its spine', (surface) => {
    expect(surfaceContract(surface).rails.left).toBe('spine')
    expect(readNavExpanded(store())).toBe(false)
  })
})
