export const BOARD_THEME_KEY = 'leaf.boardTheme'
export const BOARD_THEMES = Object.freeze(['dark', 'light'])

export function readBoardTheme(storage) {
  try {
    const target = storage === undefined ? globalThis.localStorage : storage
    return target?.getItem(BOARD_THEME_KEY) === 'light' ? 'light' : 'dark'
  } catch {
    return 'dark'
  }
}

export function writeBoardTheme(theme, storage) {
  if (!BOARD_THEMES.includes(theme)) return false
  try {
    const target = storage === undefined ? globalThis.localStorage : storage
    if (!target) return false
    target.setItem(BOARD_THEME_KEY, theme)
    return true
  } catch {
    return false
  }
}
