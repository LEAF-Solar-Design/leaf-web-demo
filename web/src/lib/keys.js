// Platform-aware keycap label. Visual only: it never changes which key the
// listener actually binds (that stays metaKey||ctrlKey everywhere in the
// app — see App.jsx's global key ladder), only what the UI prints for it.
// One module-scope read: navigator.platform doesn't change mid-session, so
// every caller shares the same computed label instead of re-testing it.
const isMac =
  typeof navigator !== 'undefined' && /Mac|iPhone|iPod|iPad/i.test(navigator.platform || '')

export const MOD_KEY = isMac ? '⌘' : 'Ctrl'
