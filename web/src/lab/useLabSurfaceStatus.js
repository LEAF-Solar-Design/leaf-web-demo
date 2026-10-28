import { useEffect, useSyncExternalStore } from 'react'
import { fetchLabStatus } from './labRoute.js'
import { setLabSurfaceStatus } from '../site/productSurfaces.js'
import { authConfigured, isAuthRedirectCallback, isSignedIn, subscribeBeforeLogout, subscribeTokenStored } from '../auth.js'
import { consoleKnownGuest } from '../controllers/session/consoleGate.js'
import { subscribeUnauthorized } from '../api.js'

let status = null
let started = false
let generation = 0
let liveUsers = 0
let unsubscribeAuthority = null
let authority
const readAuthority = () => {
  try { return localStorage.getItem('leaf.jwt') } catch { return null }
}
const listeners = new Set()
const subscribe = (listener) => {
  listeners.add(listener)
  return () => listeners.delete(listener)
}
const snapshot = () => status
const pending = () => null

function publish(next) {
  status = next
  setLabSurfaceStatus(next)
  for (const listener of listeners) listener()
}

function probeStatus() {
  if (started) return
  started = true
  // A known guest reads nothing: the status is the same signed-out a 401 would publish.
  if (consoleKnownGuest({ mock: false, authConfigured, isSignedIn, authCallback: isAuthRedirectCallback() })) {
    publish({ state: 'signed-out', posture: null })
    return
  }
  const probe = generation
  fetchLabStatus().then((next) => {
    if (probe === generation) publish(next)
  })
}

function refreshAuthority(signedOut = false) {
  authority = signedOut ? null : readAuthority()
  generation += 1
  started = signedOut
  publish(signedOut ? { state: 'signed-out', posture: null } : null)
  if (!signedOut) probeStatus()
}

function watchAuthority() {
  const current = readAuthority()
  if (authority !== undefined && authority !== current) refreshAuthority(!current)
  authority = current
  // Same-frame popup sign-in writes the token without a browser storage event.
  const offStored = subscribeTokenStored(() => refreshAuthority())
  const offLogout = subscribeBeforeLogout(() => refreshAuthority(true))
  const offUnauthorized = subscribeUnauthorized(() => refreshAuthority(true))
  const onStorage = (event) => {
    if (event.key === null || event.key === 'leaf.jwt') refreshAuthority(!readAuthority())
  }
  globalThis.addEventListener?.('storage', onStorage)
  return () => {
    offStored()
    offLogout()
    offUnauthorized()
    globalThis.removeEventListener?.('storage', onStorage)
  }
}

export function _resetLabSurfaceStatusForTests() {
  unsubscribeAuthority?.()
  unsubscribeAuthority = null
  liveUsers = 0
  authority = undefined
  generation += 1
  status = null
  started = false
  setLabSurfaceStatus(null)
  listeners.clear()
}

// One shared probe per authority for the shell, tab band and retained ground. Demo mode
// never invents an on rail and never starts a live request.
export function useLabSurfaceStatus(mock = false) {
  const current = useSyncExternalStore(subscribe, snapshot, pending)
  useEffect(() => {
    if (mock) return
    if (liveUsers++ === 0) unsubscribeAuthority = watchAuthority()
    probeStatus()
    return () => {
      if (--liveUsers === 0) {
        unsubscribeAuthority?.()
        unsubscribeAuthority = null
      }
    }
  }, [mock])
  return mock ? null : current
}
