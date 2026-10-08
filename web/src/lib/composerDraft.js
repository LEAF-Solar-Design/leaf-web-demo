import { guardedText } from './secretGuardTransport.js'

export const COMPOSER_DRAFT_PREFIX = 'leaf.composerDraft.v1:'
export const COMPOSER_DRAFT_MAX_CHARS = 4000
export const COMPOSER_DRAFT_DELAY_MS = 400

const activeDrafts = new Set()

export function composerDraftStorage() {
  try { return globalThis.localStorage || null } catch { return null }
}

export function composerDraftRoute() {
  try { return globalThis.window?.location?.pathname || null } catch { return null }
}

// Identity partitions local drafts; it is not an authentication decision. Use
// issuer + subject, never the bearer itself, and never share a signed-in
// fallback bucket when the token cannot be decoded.
export function composerDraftAccountScope(storage = composerDraftStorage()) {
  try {
    if (!storage) return null
    const token = storage.getItem('leaf.jwt')
    if (!token) return 'guest'
    const part = token.split('.')[1]
    if (!part) return null
    const base64 = part.replace(/-/g, '+').replace(/_/g, '/')
    const bytes = globalThis.atob(base64.padEnd(Math.ceil(base64.length / 4) * 4, '='))
    const payload = JSON.parse(decodeURIComponent(Array.from(bytes, (char) =>
      `%${char.charCodeAt(0).toString(16).padStart(2, '0')}`).join('')))
    if (typeof payload.sub !== 'string' || !payload.sub ||
        typeof payload.iss !== 'string' || !payload.iss) return null
    return `account:${JSON.stringify([payload.iss, payload.sub])}`
  } catch { return null }
}

export function composerDraftKey(accountScope, route) {
  if (typeof accountScope !== 'string' || !accountScope || typeof route !== 'string' || !route) return null
  return `${COMPOSER_DRAFT_PREFIX}${encodeURIComponent(accountScope)}:${encodeURIComponent(route)}`
}

export function createComposerDraft({
  storage = composerDraftStorage(),
  accountScope = composerDraftAccountScope(storage),
  route = composerDraftRoute(),
  document: visibilityDocument = globalThis.document,
} = {}) {
  const key = composerDraftKey(accountScope, route)
  let pending = null
  let timer = null
  let disposed = false

  const cancel = () => {
    if (timer !== null) clearTimeout(timer)
    timer = null
    pending = null
  }
  const clear = () => {
    cancel()
    try { if (key) storage?.removeItem(key) } catch { /* storage unavailable */ }
  }
  const flush = () => {
    if (disposed || pending === null) return
    const text = pending
    cancel()
    // This is the only draft write boundary. Storage never honours a send
    // override, and rechecks the capped value immediately before writing it.
    const verdict = guardedText(text)
    if (!verdict.ok) { clear(); return }
    try { if (key) storage?.setItem(key, verdict.text) } catch { /* storage unavailable */ }
  }
  const update = (text) => {
    if (disposed) return
    cancel()
    // Inspect the entire paste before capping: a credential beyond character
    // 4000 must refuse the draft, rather than save its innocent prefix.
    if (typeof text !== 'string' || !text || !guardedText(text).ok) { clear(); return }
    if (!key || !storage) return
    pending = text.slice(0, COMPOSER_DRAFT_MAX_CHARS)
    timer = setTimeout(flush, COMPOSER_DRAFT_DELAY_MS)
  }
  const restore = () => {
    try {
      if (!key || disposed) return ''
      const text = storage?.getItem(key)
      if (typeof text !== 'string' || !text) return ''
      const capped = text.slice(0, COMPOSER_DRAFT_MAX_CHARS)
      if (!guardedText(text).ok || !guardedText(capped).ok) { clear(); return '' }
      return capped
    } catch { return '' }
  }
  const onVisibility = () => {
    if (visibilityDocument?.visibilityState === 'hidden') flush()
  }
  const draft = {
    key, update, restore, flush, clear, cancel,
    dispose({ save = true } = {}) {
      if (save) flush()
      cancel()
      disposed = true
      visibilityDocument?.removeEventListener?.('visibilitychange', onVisibility)
      activeDrafts.delete(draft)
    },
  }
  visibilityDocument?.addEventListener?.('visibilitychange', onVisibility)
  activeDrafts.add(draft)
  return draft
}

export function clearComposerDrafts(storage = composerDraftStorage()) {
  // Cancel live timers before enumerating keys so logout cannot be undone by
  // a later debounce or visibility flush.
  for (const draft of activeDrafts) draft.cancel()
  try {
    const keys = []
    for (let index = 0; index < (storage?.length || 0); index += 1) {
      const key = storage.key(index)
      if (key?.startsWith(COMPOSER_DRAFT_PREFIX)) keys.push(key)
    }
    for (const key of keys) {
      try { storage.removeItem(key) } catch { /* continue clearing other scopes */ }
    }
  } catch { /* storage unavailable */ }
}
