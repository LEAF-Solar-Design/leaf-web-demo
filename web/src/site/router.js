// Minimal pushState router for the Website One Surface layer.
// Views: '/' (site cover) · '/try' (tool scene pre-set) · '/sheets' and
// '/sheets/<code>' (sheet pages; <code> normalized by consumers) · '/app'
// (the untouched console) · '/ty' (an alias for that same console).
// location.search is ALWAYS preserved by navigate()
// so the console's boot params (?demo= ?fixture= ?ops= ?dev= ?drawing= and the
// Auth0 ?code=&state= callback) survive every in-site navigation.
//
// EXACT exports (a sibling agent depends on them): useRoute(), navigate(path).

import { useSyncExternalStore } from 'react'

const NAV_EVENT = 'leaf:navigate'

function read() {
  return { path: window.location.pathname, hash: window.location.hash }
}

let snapshot = read()

function refresh() {
  const next = read()
  if (next.path !== snapshot.path || next.hash !== snapshot.hash) snapshot = next
}

function subscribe(onChange) {
  const handler = () => { refresh(); onChange() }
  window.addEventListener('popstate', handler)
  window.addEventListener(NAV_EVENT, handler)
  return () => {
    window.removeEventListener('popstate', handler)
    window.removeEventListener(NAV_EVENT, handler)
  }
}

// {path, hash} — subscribes to popstate + the custom event navigate() fires.
export function useRoute() {
  return useSyncExternalStore(subscribe, () => snapshot)
}

// pushState to `path` (may carry a '#hash'), preserving location.search, then
// notify all useRoute subscribers via the custom event.
export function navigate(path) {
  const hashAt = path.indexOf('#')
  const pathname = hashAt === -1 ? path : path.slice(0, hashAt)
  const hash = hashAt === -1 ? '' : path.slice(hashAt)
  const url = pathname + window.location.search + hash
  window.history.pushState({}, '', url)
  window.dispatchEvent(new Event(NAV_EVENT))
}
