import { useEffect, useSyncExternalStore } from 'react'
import { fetchEstimatorStatus } from './estimatorRoute.js'
import { setEstimatorSurfaceStatus } from '../site/productSurfaces.js'
import { authConfigured, isAuthRedirectCallback, isSignedIn, subscribeBeforeLogout, subscribeTokenStored } from '../auth.js'
import { consoleKnownGuest } from '../controllers/session/consoleGate.js'
import { subscribeUnauthorized } from '../api.js'
import { authorityKey } from '../lib/serviceAuthSession.js'

let status = null
let started = false
let generation = 0
let liveUsers = 0
let unsubscribeAuthority = null
let authority
const listeners = new Set()
const subscribe = listener => { listeners.add(listener); return () => listeners.delete(listener) }
const snapshot = () => status
const pending = () => null

function publish(next) {
  status = next
  setEstimatorSurfaceStatus(next)
  for (const listener of listeners) listener()
}

function probeStatus() {
  if (started) return
  started = true
  if (consoleKnownGuest({ mock: false, authConfigured, isSignedIn, authCallback: isAuthRedirectCallback() })) {
    publish({ state: 'signed-out', bindings: [] })
    return
  }
  const probe = generation
  const proofAuthority = authorityKey()
  fetchEstimatorStatus().then(next => {
    if (probe === generation && proofAuthority === authorityKey()) publish(next)
  })
}

function refreshAuthority(signedOut = false) {
  authority = signedOut ? null : authorityKey()
  generation += 1
  started = signedOut
  publish(signedOut ? { state: 'signed-out', bindings: [] } : null)
  if (!signedOut) probeStatus()
}

function watchAuthority() {
  const current = authorityKey()
  if (authority !== undefined && authority !== current) refreshAuthority(!current)
  authority = current
  const offStored = subscribeTokenStored(() => refreshAuthority())
  const offLogout = subscribeBeforeLogout(() => refreshAuthority(true))
  const offUnauthorized = subscribeUnauthorized(() => refreshAuthority(true))
  const onStorage = event => {
    if (event.key === null || event.key === 'leaf.jwt') refreshAuthority(!authorityKey())
  }
  globalThis.addEventListener?.('storage', onStorage)
  return () => {
    offStored(); offLogout(); offUnauthorized()
    globalThis.removeEventListener?.('storage', onStorage)
  }
}

export function _resetEstimatorSurfaceStatusForTests() {
  unsubscribeAuthority?.()
  unsubscribeAuthority = null
  liveUsers = 0
  authority = undefined
  generation += 1
  started = false
  status = null
  setEstimatorSurfaceStatus(null)
  listeners.clear()
}

export function useEstimatorSurfaceStatus(mock = false) {
  const current = useSyncExternalStore(subscribe, snapshot, pending)
  useEffect(() => {
    if (mock) return
    if (liveUsers++ === 0) unsubscribeAuthority = watchAuthority()
    probeStatus()
    return () => {
      if (--liveUsers === 0) { unsubscribeAuthority?.(); unsubscribeAuthority = null }
    }
  }, [mock])
  return mock ? null : current
}
