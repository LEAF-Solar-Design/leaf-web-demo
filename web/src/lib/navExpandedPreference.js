// S20 (fork F-studio-rollback-storage): the studio nav rail's expanded posture
// is remembered across page loads under ONE key. Every read and write is
// wrapped, so a locked or throwing storage only costs the memory, never the
// render. No stored value (or anything unrecognised) keeps today's default:
// COLLAPSED, which is the spine posture on CAD and Solar.
export const NAV_EXPANDED_KEY = 'leaf.studio.navExpanded.v1'
export const NAV_EXPANDED_DEFAULT = false

export function readNavExpanded(storage) {
  try {
    const target = storage === undefined ? globalThis.localStorage : storage
    const value = target?.getItem(NAV_EXPANDED_KEY)
    if (value === '1') return true
    if (value === '0') return false
    return NAV_EXPANDED_DEFAULT
  } catch {
    return NAV_EXPANDED_DEFAULT
  }
}

export function writeNavExpanded(expanded, storage) {
  if (typeof expanded !== 'boolean') return false
  try {
    const target = storage === undefined ? globalThis.localStorage : storage
    if (!target) return false
    target.setItem(NAV_EXPANDED_KEY, expanded ? '1' : '0')
    return true
  } catch {
    return false
  }
}
