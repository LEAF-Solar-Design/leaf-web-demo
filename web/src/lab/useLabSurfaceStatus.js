import { useEffect, useSyncExternalStore } from 'react'
import { fetchLabStatus } from './labRoute.js'
import { setLabSurfaceStatus } from '../site/productSurfaces.js'

let status = null
let started = false
let generation = 0
const listeners = new Set()
const subscribe = (listener) => {
  listeners.add(listener)
  return () => listeners.delete(listener)
}
const snapshot = () => status
const pending = () => null

export function _resetLabSurfaceStatusForTests() {
  generation += 1
  status = null
  started = false
  setLabSurfaceStatus(null)
  listeners.clear()
}

// One first probe for the shell, tab band and retained ground. Demo mode
// never invents an on rail and never starts a live request.
export function useLabSurfaceStatus(mock = false) {
  const current = useSyncExternalStore(subscribe, snapshot, pending)
  useEffect(() => {
    if (mock || started) return
    started = true
    const probe = generation
    fetchLabStatus().then((next) => {
      if (probe !== generation) return
      status = next
      setLabSurfaceStatus(next)
      for (const listener of listeners) listener()
    })
  }, [mock])
  return mock ? null : current
}
