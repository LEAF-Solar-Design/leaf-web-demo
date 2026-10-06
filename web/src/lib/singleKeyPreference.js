// S25: the off switch for single-key shortcuts (WCAG 2.1.4 Character Key
// Shortcuts). Bare R (retry) and Shift+? (the shortcut sheet) fire on one
// printable key, so a speech-input or one-finger user can trigger them by
// accident; the ShortcutSheet switch turns them off. Mod chords are not
// single-key shortcuts and are never governed here.
//
// Mirrors themePreference.js: storage is read and written inside try/catch, so
// a blocked or absent localStorage fails OPEN to the default (on), which is the
// behavior the shell had before this switch existed. Readers read at keystroke
// time, never at mount, so a flip in the sheet takes effect on the next key
// with no subscription to keep in sync.
export const SINGLE_KEY_SHORTCUTS_KEY = 'leaf.singleKeyShortcuts'

/** True unless the stored value is exactly 'off'. Fails open on any storage error. */
export function readSingleKeyShortcuts(storage) {
  try {
    const target = storage === undefined ? globalThis.localStorage : storage
    return target?.getItem(SINGLE_KEY_SHORTCUTS_KEY) !== 'off'
  } catch {
    return true
  }
}

/** Persist the switch. Returns false (and changes nothing) on a non-boolean or a storage error. */
export function writeSingleKeyShortcuts(enabled, storage) {
  if (typeof enabled !== 'boolean') return false
  try {
    const target = storage === undefined ? globalThis.localStorage : storage
    if (!target) return false
    target.setItem(SINGLE_KEY_SHORTCUTS_KEY, enabled ? 'on' : 'off')
    return true
  } catch {
    return false
  }
}
