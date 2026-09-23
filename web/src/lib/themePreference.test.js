import { describe, expect, it } from 'vitest'
import { BOARD_THEME_KEY, readBoardTheme, writeBoardTheme } from './themePreference.js'

function store() {
  const values = new Map()
  return { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) }
}

describe('board theme preference', () => {
  it('SSD1-24E theme-pref defaults to dark with an empty or missing store', () => {
    expect(readBoardTheme(store())).toBe('dark')
    expect(readBoardTheme(null)).toBe('dark')
  })
  it('SSD1-24E theme-pref reads light', () => {
    const storage = store()
    storage.setItem(BOARD_THEME_KEY, 'light')
    expect(readBoardTheme(storage)).toBe('light')
  })
  it.each(['Light', 'dark ', '1', 'true'])('SSD1-24E theme-pref rejects stored %s', (value) => {
    const storage = store()
    storage.setItem(BOARD_THEME_KEY, value)
    expect(readBoardTheme(storage)).toBe('dark')
  })
  it('SSD1-24E theme-pref reads dark when storage throws', () => {
    expect(readBoardTheme({ getItem() { throw new Error('Unavailable') } })).toBe('dark')
  })
  it('SSD1-24E theme-pref writes light and reads it back', () => {
    const storage = store()
    expect(writeBoardTheme('light', storage)).toBe(true)
    expect(readBoardTheme(storage)).toBe('light')
  })
  it('SSD1-24E theme-pref ignores invalid writes', () => {
    const storage = store()
    expect(writeBoardTheme('blue', storage)).toBe(false)
    expect(storage.getItem(BOARD_THEME_KEY)).toBeNull()
  })
  it('SSD1-24E theme-pref returns false when writing throws', () => {
    expect(writeBoardTheme('light', { setItem() { throw new Error('Unavailable') } })).toBe(false)
  })
})
