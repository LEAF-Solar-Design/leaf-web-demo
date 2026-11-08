import { placement, TRUSTED_BLOCK_IDS } from '../runtime/placement.js'

export const CATEGORIES = Object.freeze([
  { id: 'recent', title: 'Recent' }, { id: 'kashbot-v2', title: 'Kashbot v2' },
  { id: 'math', title: 'Math and data' }, { id: 'sources', title: 'Sources' },
  { id: 'tools', title: 'Tools' }, { id: 'kashbot-policy', title: 'Old Kashbot policy' },
  { id: 'other', title: 'Other' },
])

export function categoryOf(manifest, { isTool = false } = {}) {
  if (isTool) return 'tools'
  if (/^(kashbot2-|kb2-)/.test(manifest.id)) return 'kashbot-v2'
  if (manifest.kind === 'builtin' && ['here', 'browser'].includes(placement(manifest))) return 'math'
  if (['studio-jobs', 'studio-capabilities', 'host-source'].includes(manifest.id)) return 'sources'
  return TRUSTED_BLOCK_IDS.has(manifest.id) ? 'kashbot-policy' : 'other'
}

export function entryMeta(manifest) {
  return {
    idVersion: `${manifest.id}@${manifest.version.semver}`,
    effect: { pure: 'Pure', read: 'Read', write: 'Write', trade: 'Trade' }[manifest.effect],
    where: { here: 'Runs here', browser: 'Runs in your browser', server: 'Runs on the Lab server',
      bot: 'Runs on the bot', host: 'Reads from the host' }[placement(manifest)],
    placeholder: manifest.version.code_sha256 === '0'.repeat(64),
  }
}

export const RECENT_KEY = 'leaf.lab.library.recent.v1'
export function readRecent(storage) {
  try {
    const ids = JSON.parse(storage.getItem(RECENT_KEY))
    return Array.isArray(ids) && ids.every(id => typeof id === 'string' && id.length)
      ? [...new Set(ids)].slice(0, 8) : []
  } catch { return [] }
}
export function pushRecent(storage, id) {
  try {
    if (typeof id === 'string' && id.length) storage.setItem(RECENT_KEY,
      JSON.stringify([id, ...readRecent(storage).filter(previous => previous !== id)].slice(0, 8)))
  } catch { /* Storage is a per-viewer convenience. */ }
}
