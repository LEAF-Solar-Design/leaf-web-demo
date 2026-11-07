// The public selector is a routing hint, never a bearer or an identity proof.
const MODE = 'leaf.service.mode'
const SELECTOR = 'leaf.service.selector'
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const validSelector = value => typeof value === 'string' && value.length === 36 && UUID.test(value)
const responses = new WeakMap()
const managedStorages = new WeakSet()
let authoritySequence = 0

function tabStorage() {
  try { return globalThis.sessionStorage } catch { return null }
}

export function createServiceAuthSession({ storage = tabStorage(), now = () => Date.now() } = {}) {
  const authority = ++authoritySequence
  let mode = false
  let selector = null
  try {
    mode = storage?.getItem(MODE) === 'service'
    const saved = storage?.getItem(SELECTOR)
    selector = validSelector(saved) ? saved : null
  } catch { /* fail closed */ }
  let identity = null
  let expiresAt = 0
  let generation = 0
  let timer
  const listeners = new Set()
  const publish = () => { for (const listener of listeners) { try { listener() } catch { /* observer */ } } }
  const persist = () => {
    try {
      if (mode) storage?.setItem(MODE, 'service')
      else storage?.removeItem(MODE)
      if (selector) storage?.setItem(SELECTOR, selector)
      else storage?.removeItem(SELECTOR)
    } catch { /* memory still fences this tab */ }
  }
  const clear = () => {
    clearTimeout(timer)
    identity = null
    expiresAt = 0
    generation += 1
  }
  const snapshot = () => {
    if (identity && expiresAt <= now()) { clear(); publish() }
    return { mode, selector, identity, expiresAt, generation, authority }
  }
  const invalidate = (expected) => {
    const current = snapshot()
    if (!expected || expected.authority !== authority || !current.mode
      || expected.selector !== selector || expected.generation !== generation) return false
    clear()
    selector = null
    persist()
    publish()
    return true
  }
  return {
    snapshot,
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener) },
    select(value) {
      clear()
      mode = true
      selector = validSelector(value) ? value : null
      persist()
      publish()
      return snapshot()
    },
    human() {
      if (!mode && !selector && !identity) return
      clear(); mode = false; selector = null; persist(); publish()
    },
    invalidate,
    logout() { const current = snapshot(); invalidate(current); return current },
    async probe({ fetchImpl = globalThis.fetch, apiBase = '', origin = globalThis.location?.origin, timeoutMs = 4000 } = {}) {
      const expected = snapshot()
      if (!expected.mode) return false
      try {
        if (!origin || new URL(apiBase || '/', origin).origin !== origin) throw new Error('Service browser login requires a same-origin API_BASE')
      } catch (error) { invalidate(expected); throw error }
      if (!expected.selector) { invalidate(expected); return false }
      const controller = new AbortController()
      let deadlineTimer
      try {
        const deadline = new Promise((_, reject) => {
          deadlineTimer = setTimeout(() => { controller.abort(); reject(new Error('Service session proof timed out')) }, timeoutMs)
        })
        const body = await Promise.race([(async () => {
          const response = await fetchImpl(`${apiBase}/api/auth/agent/session`, {
            credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
            headers: { Authorization: `LeafServiceSession ${expected.selector}` },
          })
          if (!response.ok) throw new Error(`Service session proof failed (${response.status})`)
          return response.json()
        })(), deadline])
        const current = snapshot()
        if (current.generation !== expected.generation || current.selector !== expected.selector || !current.mode) return false
        const value = body?.identity
        const expiry = Date.parse(body?.expires_at)
        if (body?.authenticated !== true || !value || value.sub !== value.subject
          || typeof value.sub !== 'string' || !value.sub || typeof value.iss !== 'string' || !value.iss
          || typeof value.tenant_id !== 'string' || !value.tenant_id
          || typeof value.principal_type !== 'string' || !value.principal_type
          || !(value.org_id === null || typeof value.org_id === 'string')
          || !validSelector(body.binding_id) || !Number.isFinite(expiry) || expiry <= now()) throw new Error('Invalid service identity proof')
        clear()
        identity = Object.freeze(Object.fromEntries(['subject', 'sub', 'iss', 'org_id', 'principal_type', 'tenant_id'].map(key => [key, value[key]])))
        expiresAt = expiry
        const verified = snapshot()
        timer = setTimeout(() => invalidate(verified), Math.min(expiry - now(), 2147483647))
        timer?.unref?.()
        publish()
        return true
      } catch { invalidate(expected); return false }
      finally { clearTimeout(deadlineTimer) }
    },
  }
}

let currentStorage
let currentSession
export function serviceSession() {
  const storage = tabStorage()
  if (!currentSession || storage !== currentStorage) {
    currentStorage = storage
    currentSession = createServiceAuthSession({ storage })
  }
  return currentSession
}

// Explicit foreign storage seams must never borrow the browser's identity.
export function serviceState(storage) {
  if (storage === null) return { mode: false, identity: null }
  if (storage !== undefined) {
    try { if (!managedStorages.has(storage) && storage !== globalThis.localStorage && storage !== tabStorage()) return { mode: false, identity: null } }
    catch { return { mode: false, identity: null } }
  }
  return serviceSession().snapshot()
}
export function serviceHeaders(apiBase = '') {
  const state = serviceState()
  if (!state.mode || !state.identity) return {}
  const origin = globalThis.location?.origin
  if (!origin || new URL(apiBase || '/', origin).origin !== origin) throw new Error('Service browser login requires a same-origin API_BASE')
  return { Authorization: `LeafServiceSession ${state.selector}`, 'X-Tenant-Id': state.identity.tenant_id }
}
export function serviceRequestAuthority() {
  const state = serviceState()
  if (state.mode && !state.identity) {
    const error = new Error('Service browser login needs server verification.')
    error.status = 401
    throw error
  }
  return state
}
export function authorityKey(storage) {
  const state = serviceState(storage)
  if (state.mode) return state.identity ? `service:${state.authority}:${state.selector}:${state.generation}` : null
  try { return (storage === undefined ? globalThis.localStorage : storage)?.getItem('leaf.jwt') || null } catch { return null }
}
export function serviceScope(storage) {
  const { identity } = serviceState(storage)
  return identity ? JSON.stringify([identity.iss, identity.sub, identity.org_id || '']) : null
}
export function rememberServiceResponse(response, authority) {
  if (response && authority?.mode && authority.identity) responses.set(response, authority)
  return response
}
export function invalidateServiceResponse(response, sentAuth, authority = responses.get(response)) {
  if (!authority?.identity || sentAuth !== `LeafServiceSession ${authority.selector}`) return false
  return serviceSession().invalidate(authority)
}
export function authorityStorage(storage) {
  if (storage !== undefined) return storage
  try {
    const state = serviceState()
    if (!state.mode) return globalThis.localStorage || null
    // Service persistence is both tab-local and principal/tenant partitioned.
    const prefix = state.identity ? `leaf.service.data:${JSON.stringify([state.identity.iss, state.identity.sub, state.identity.org_id, state.identity.tenant_id])}:` : null
    const target = tabStorage()
    const scoped = {
      getItem: key => prefix ? target?.getItem(prefix + key) : null,
      setItem: (key, value) => {
        const current = serviceState()
        const valuePrefix = current.identity ? `leaf.service.data:${JSON.stringify([current.identity.iss, current.identity.sub, current.identity.org_id, current.identity.tenant_id])}:` : null
        if (prefix && current.mode && valuePrefix === prefix) target?.setItem(prefix + key, value)
      },
      removeItem: key => { if (prefix) target?.removeItem(prefix + key) },
      get length() { return keys().length },
      key: index => keys()[index] || null,
    }
    const keys = () => {
      const found = []
      for (let i = 0; prefix && i < (target?.length || 0); i += 1) {
        const key = target.key(i)
        if (key?.startsWith(prefix)) found.push(key.slice(prefix.length))
      }
      return found
    }
    managedStorages.add(scoped)
    return scoped
  } catch { return null }
}
