// Comparison key only: the server remains the authority for authentication.
export function readActiveScope(storage) {
  try {
    const target = storage === undefined ? globalThis.localStorage : storage
    const token = target?.getItem('leaf.jwt')
    const part = token?.split('.')[1]
    if (!part) return null
    const base64 = part.replace(/-/g, '+').replace(/_/g, '/')
    const claims = JSON.parse(globalThis.atob(base64.padEnd(Math.ceil(base64.length / 4) * 4, '=')))
    if (typeof claims?.sub !== 'string' || !claims.sub.trim()) return null
    const iss = typeof claims.iss === 'string' ? claims.iss : ''
    const org = target?.getItem('leaf.org_id')
      ?? (typeof claims.org_id === 'string' ? claims.org_id : '')
    return JSON.stringify([iss, claims.sub, org])
  } catch { return null }
}

export function subscribeActiveScope(listener) {
  if (typeof window === 'undefined') return () => {}
  const onStorage = (event) => {
    if (event.key === null || event.key === 'leaf.jwt' || event.key === 'leaf.org_id') listener()
  }
  window.addEventListener('storage', onStorage)
  window.addEventListener('leaf:active-scope', listener)
  return () => {
    window.removeEventListener('storage', onStorage)
    window.removeEventListener('leaf:active-scope', listener)
  }
}

export function notifyActiveScopeChanged() {
  if (typeof window !== 'undefined') window.dispatchEvent(new window.Event('leaf:active-scope'))
}
