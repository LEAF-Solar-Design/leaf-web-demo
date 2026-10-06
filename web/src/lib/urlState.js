// S24 (A14): the URL keeps the studio's view state across a reload and Back.
//
// Four keys, and ONLY these four, are this module's (fork
// F-studio-rollback-storage): `tool` (the opened catalog tool), `drawer` (the
// open drawer), `sel` (the operator's own selection) and `cam` (the camera
// view: a `focus` preset or a `x,y,zoom` pose). Every other search key is
// somebody else's and survives every write BYTE FOR BYTE: the boot flags
// (?demo= including demo=off, ?fixture= ?ops= ?dev= ?surface= ?proof=), the
// drawing identity (?drawing= is DrawingIdentityProvider's), and the Auth0
// callback (?code=&state=). The search is edited as raw `&` segments, never
// round-tripped through URLSearchParams, because that re-encodes values
// (`%2F` and `+` come back different). The pathname and hash are kept as-is.
//
// Writes (fork A14-replace-vs-push): `replace` while interacting (selection,
// camera, a close), `push` on a commit (opening a drawer or a tool), so Back
// undoes the commit and nothing else floods the history. A patch naming an
// unknown key, or a value outside its key's grammar, is refused whole and
// writes nothing (fails closed). Reads fail closed too: a hand-edited or
// hostile value reads as absent.

import { useEffect, useRef, useSyncExternalStore } from 'react'

export const VIEW_PARAM_KEYS = Object.freeze(['tool', 'drawer', 'sel', 'cam'])
// The drawers a URL may name: App's studio drawers plus the session/account
// Details drawer. A run's provenance drawer is per-result, so never addressable.
export const VIEW_DRAWERS = Object.freeze(['nav', 'jobs', 'result', 'plan', 'details'])
export const VIEW_PARAM_EVENT = 'leaf:view-params'
export const CAM_FOCUS = 'focus'
// Camera writes trail the last camera move by this long (one replaceState per
// settled view, far under every browser's history rate limit).
export const CAM_WRITE_DELAY_MS = 400
// Frames to wait for a lazily mounted viewer before giving up on the camera.
const CAM_ATTACH_FRAMES = 600

const NAV_EVENT = 'leaf:navigate' // router.js navigate(): the path moved, the search did not
// URL-safe tokens only, so a written value is never percent-encoded and a
// read value never needs decoding to compare.
const TOKEN = /^[A-Za-z0-9_.:-]{1,128}$/
const CAM_POSE = /^(-?\d{1,12}(?:\.\d{1,6})?),(-?\d{1,12}(?:\.\d{1,6})?),(\d{1,9}(?:\.\d{1,12})?)$/

/** Whether `value` is in `key`'s grammar. Unknown keys are never valid. */
export function isViewParamValue(key, value) {
  if (typeof value !== 'string') return false
  switch (key) {
    case 'tool':
    case 'sel':
      return TOKEN.test(value)
    case 'drawer':
      return VIEW_DRAWERS.includes(value)
    case 'cam':
      return value === CAM_FOCUS || parseCamParam(value) !== null
    default:
      return false
  }
}

function segmentKey(segment) {
  const at = segment.indexOf('=')
  const raw = at === -1 ? segment : segment.slice(0, at)
  try { return decodeURIComponent(raw.replace(/\+/g, ' ')) } catch { return raw }
}

function segmentValue(segment) {
  const at = segment.indexOf('=')
  if (at === -1) return ''
  const raw = segment.slice(at + 1)
  try { return decodeURIComponent(raw.replace(/\+/g, ' ')) } catch { return null }
}

/** The validated value of one view key in `search`, or null (absent, unknown key, or out of grammar). */
export function readViewParam(key, search = typeof window === 'undefined' ? '' : window.location.search) {
  if (!VIEW_PARAM_KEYS.includes(key) || typeof search !== 'string') return null
  const body = search.startsWith('?') ? search.slice(1) : search
  if (body === '') return null
  for (const segment of body.split('&')) {
    if (segmentKey(segment) !== key) continue
    const value = segmentValue(segment)
    return isViewParamValue(key, value) ? value : null
  }
  return null
}

/**
 * The search string with `patch` applied, or null when the patch is refused.
 * A patch value of null or undefined removes the key; a string sets it in
 * place (first occurrence, duplicates dropped) or appends it. Every segment
 * that is not one of the patched keys is copied untouched, in order. An
 * unchanged result returns the input string itself.
 */
export function buildViewSearch(search, patch) {
  if (typeof search !== 'string' || !patch || typeof patch !== 'object' || Array.isArray(patch)) return null
  const entries = Object.entries(patch)
  if (entries.length === 0) return search
  for (const [key, value] of entries) {
    if (!VIEW_PARAM_KEYS.includes(key)) return null
    if (value != null && !isViewParamValue(key, value)) return null
  }
  const body = search.startsWith('?') ? search.slice(1) : search
  let parts = body === '' ? [] : body.split('&')
  let changed = false
  for (const [key, value] of entries) {
    const next = []
    let placed = false
    for (const segment of parts) {
      if (segmentKey(segment) !== key) { next.push(segment); continue }
      if (value == null || placed) { changed = true; continue }
      const written = `${key}=${value}`
      if (written !== segment) changed = true
      next.push(written)
      placed = true
    }
    if (value != null && !placed) { next.push(`${key}=${value}`); changed = true }
    parts = next
  }
  if (!changed) return search
  return parts.length ? `?${parts.join('&')}` : ''
}

/**
 * Apply `patch` to the live URL. `mode` is 'replace' (interacting) or 'push'
 * (a commit). Returns true when the URL holds the patch afterwards (written,
 * or already current), false when refused or the history call threw.
 */
export function writeViewParams(patch, { mode = 'replace' } = {}) {
  if (typeof window === 'undefined' || (mode !== 'replace' && mode !== 'push')) return false
  const { pathname, search, hash } = window.location
  const next = buildViewSearch(search, patch)
  if (next === null) return false
  if (next === search) return true
  const url = `${pathname}${next}${hash}`
  try {
    if (mode === 'push') window.history.pushState({}, '', url)
    else window.history.replaceState(window.history.state, '', url)
  } catch {
    return false
  }
  window.dispatchEvent(new Event(VIEW_PARAM_EVENT))
  return true
}

function subscribe(onChange) {
  window.addEventListener('popstate', onChange)
  window.addEventListener(VIEW_PARAM_EVENT, onChange)
  window.addEventListener(NAV_EVENT, onChange)
  return () => {
    window.removeEventListener('popstate', onChange)
    window.removeEventListener(VIEW_PARAM_EVENT, onChange)
    window.removeEventListener(NAV_EVENT, onChange)
  }
}

/** The live value of one view key; re-renders on popstate (Back/Forward) and on every write. */
export function useViewParam(key) {
  return useSyncExternalStore(subscribe, () => readViewParam(key), () => null)
}

/** A commit opens (push); clearing the key is a close (replace). */
export function pushOnOpen(_previous, next) {
  return next == null ? 'replace' : 'push'
}

/**
 * Seat one surface state on one view key, both ways.
 *
 *  - URL -> state: once `ready` (and `enabled`), and again on every Back or
 *    Forward, a URL value that differs from `value` goes to `onRestore(url)`.
 *    `onRestore` returns false to refuse (a tool no longer in the catalog);
 *    the key is then cleared with a replace so no stale param outlives it.
 *  - state -> URL: a CHANGE of `value` is written with `mode` ('replace',
 *    'push', or a (previous, next) => mode function). The value a surface
 *    mounts with is never written, so a boot restore still pending (the URL
 *    names a selection the drawing has not loaded yet) is not erased.
 *
 * `enabled` false (ToolCast mounted but not the active scene) neither reads
 * nor writes.
 */
export function useViewParamSeat(key, { value, onRestore, mode = 'replace', ready = true, enabled = true } = {}) {
  const urlValue = useViewParam(key)
  const own = isViewParamValue(key, value) ? value : null
  const ownRef = useRef(own)
  ownRef.current = own
  const lastOwnRef = useRef(own)
  const onRestoreRef = useRef(onRestore)
  onRestoreRef.current = onRestore
  const modeRef = useRef(mode)
  modeRef.current = mode

  useEffect(() => {
    if (!enabled || !ready || urlValue === ownRef.current) return
    // Restoration wins over a simultaneous local change (for example when a
    // mounted, inactive surface becomes active again).
    lastOwnRef.current = ownRef.current
    let accepted = false
    try { accepted = onRestoreRef.current?.(urlValue) !== false } catch { accepted = false }
    if (!accepted && urlValue != null) writeViewParams({ [key]: null }, { mode: 'replace' })
  }, [key, urlValue, ready, enabled])

  useEffect(() => {
    const previous = lastOwnRef.current
    if (previous === own) return
    lastOwnRef.current = own
    if (!enabled || !ready || readViewParam(key) === own) return
    const chosen = typeof modeRef.current === 'function' ? modeRef.current(previous, own) : modeRef.current
    writeViewParams({ [key]: own }, { mode: chosen === 'push' ? 'push' : 'replace' })
  }, [key, own, enabled, ready])

  return urlValue
}

function camNumber(value, digits) {
  const rounded = Number(value.toFixed(digits))
  return rounded === 0 ? '0' : rounded.toFixed(digits).replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, '')
}

/** A viewer pose ({target:[x,y,..], zoom}) as a `cam` value, or null when it does not fit the grammar. */
export function formatCamParam(pose) {
  const target = pose?.target
  const zoom = pose?.zoom
  if (!Array.isArray(target) || !Number.isFinite(target[0]) || !Number.isFinite(target[1])) return null
  if (!Number.isFinite(zoom) || zoom <= 0) return null
  const value = `${camNumber(target[0], 3)},${camNumber(target[1], 3)},${camNumber(Number(zoom.toPrecision(6)), 12)}`
  return CAM_POSE.test(value) && parseCamParam(value) !== null ? value : null
}

/** A `cam` pose value as the viewer's setView argument ({center:{x,y}, zoom}), or null. */
export function parseCamParam(value) {
  if (typeof value !== 'string') return null
  const match = CAM_POSE.exec(value)
  if (!match) return null
  const x = Number(match[1]), y = Number(match[2]), zoom = Number(match[3])
  if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(zoom) || zoom <= 0) return null
  return { center: { x, y }, zoom }
}

/**
 * Seat a viewer's camera on `cam`. On the first pose after `ready`, a boot
 * `cam` pose is applied once through `viewer.setView`; after that every
 * settled camera move is written with a replace (never a push: a pan is not a
 * commit). Back/Forward reapplies the URL pose and cancels pending writes.
 * `viewerRef.current` must expose subscribeCamera (Viewer.jsx); a
 * lazily mounted viewer is waited for, bounded to CAM_ATTACH_FRAMES frames.
 */
export function useCameraViewParam(viewerRef, { ready = true, enabled = true, delayMs = CAM_WRITE_DELAY_MS } = {}) {
  useEffect(() => {
    if (!enabled || !ready) return undefined
    let unsubscribe = null
    let timer = null
    let frame = 0
    let tries = 0
    let cancelled = false
    let pending = parseCamParam(readViewParam('cam'))
    let restoring = false
    let firstPose = true
    let lastPose = null
    const cancelWrite = () => {
      if (timer !== null) clearTimeout(timer)
      timer = null
    }
    const applyPending = () => {
      if (pending == null) return false
      const target = pending
      restoring = true
      try {
        if (viewerRef.current?.setView?.(target)) {
          pending = null
          // Viewer publishes on the next animation frame. That publication
          // is a restoration echo, not a new interaction to write back.
          lastPose = formatCamParam(viewerRef.current?.getPose?.())
            ?? (target === 'home' ? null : formatCamParam({ target: [target.center.x, target.center.y], zoom: target.zoom }))
        }
      } finally {
        restoring = false
      }
      return true
    }
    const restore = () => {
      cancelWrite()
      const value = readViewParam('cam')
      pending = value == null ? 'home' : parseCamParam(value)
      applyPending()
    }
    const onCamera = (snapshot) => {
      const pose = snapshot?.pose
      // A null snapshot means the drawing is rebuilding. Never let the old
      // scene's delayed pose overwrite this scene's URL.
      if (!pose) { cancelWrite(); firstPose = true; return }
      if (restoring) return
      const initial = firstPose
      firstPose = false
      if (applyPending()) return
      const value = formatCamParam(pose)
      if (value === lastPose) return
      lastPose = value
      if (initial) return
      cancelWrite()
      timer = setTimeout(() => {
        timer = null
        if (value) writeViewParams({ cam: value }, { mode: 'replace' })
      }, delayMs)
    }
    const attach = () => {
      if (cancelled) return
      const viewer = viewerRef.current
      if (viewer && typeof viewer.subscribeCamera === 'function') {
        unsubscribe = viewer.subscribeCamera(onCamera)
        return
      }
      tries += 1
      if (tries < CAM_ATTACH_FRAMES && typeof requestAnimationFrame === 'function') frame = requestAnimationFrame(attach)
    }
    window.addEventListener('popstate', restore)
    window.addEventListener(NAV_EVENT, restore)
    attach()
    return () => {
      cancelled = true
      if (frame && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(frame)
      cancelWrite()
      window.removeEventListener('popstate', restore)
      window.removeEventListener(NAV_EVENT, restore)
      if (typeof unsubscribe === 'function') unsubscribe()
    }
  }, [viewerRef, ready, enabled, delayMs])
}
