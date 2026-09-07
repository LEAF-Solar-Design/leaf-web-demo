// ---------------------------------------------------------------------------
// THE SURFACE-CONFIG OVERLAY (standardization slice 7b). productSurfaces.js
// stays the frozen contract defaults; this module fetches the tenant's
// GET /api/surface-config overlay ONCE per browser session and deep-merges
// only its DECLARED slots onto surfaceContract(id), so a tenant with no
// overlay (or an overlay that failed to load) renders BYTE-IDENTICAL to
// today: no server-side copy of the defaults is kept here either, the merge
// starts from the same frozen literal App.jsx/ToolCast.jsx have always read.
//
// ONE FETCH, SHARED. App.jsx, ToolCast.jsx and SurfaceFrame.jsx each mount
// `useSurfaceContract`/`useSurfaceConfigOverlay` independently; a per-hook
// fetch would mean three requests for one page load. `_startFetchOnce` is a
// module-level singleton (not per-consumer state), so the first consumer to
// render starts the ONE request and every other consumer's `useEffect`
// subscribes to its result instead of re-fetching.
// ---------------------------------------------------------------------------
import { useEffect, useMemo, useState } from 'react'

import { getSurfaceConfig } from '../api.js'
import { deepFreeze, surfaceContract } from './productSurfaces.js'

// Mirrors _vendor/mushy_fold/surface_config.py's `_SLOT_NAMES` (server) and
// contract/surface-config.v1.schema.json's `surfaceOverlay` properties. The
// server already rejects the WHOLE overlay file on an unknown slot (fails
// closed to `{}`), so this list is defense-in-depth against a malformed
// response reaching the client, never the enforcement boundary itself.
export const OVERLAY_SLOT_NAMES = Object.freeze([
  'chrome', 'toolbar', 'rails', 'commandLine', 'authoring', 'versions',
  'conversations', 'builds', 'contextMenu', 'groundMaterial',
])

function isPlainObject(value) {
  return !!value && typeof value === 'object' && !Array.isArray(value)
}

let _fetchStarted = false
let _fetchPromise = null
let _fetchGeneration = 0
let _overlay = {}
let _source = null
const _listeners = new Set()
// One console.warn per surface id, ever (not per render, not per overlay
// settle): the server already fails the WHOLE overlay file closed on an
// unknown slot, so a client seeing one at all means defense-in-depth fired,
// not a live incident worth repeating on every re-render of every consumer.
const _warnedUnknownSlotIds = new Set()

function _notify() {
  for (const listener of Array.from(_listeners)) listener()
}

function _startFetchOnce(mock) {
  if (_fetchStarted) return _fetchPromise
  _fetchStarted = true
  const generation = ++_fetchGeneration
  _fetchPromise = getSurfaceConfig(mock)
    .then((body) => {
      if (generation !== _fetchGeneration) return
      _overlay = isPlainObject(body?.surfaces) ? body.surfaces : {}
      _source = isPlainObject(body?.source) ? body.source : null
      _notify()
    })
    .catch(() => {
      if (generation !== _fetchGeneration) return
      // Fails closed to the frozen defaults, exactly like the server's own
      // fold: a broken fetch must not break the surface, only leave it
      // un-overlaid.
      _overlay = {}
      _source = null
      _notify()
    })
  return _fetchPromise
}

export function refreshSurfaceConfigOverlay() {
  _fetchStarted = false
  return _startFetchOnce(false)
}

export function isSurfaceConfigOverlay(value) {
  return isPlainObject(value) && Object.values(value).every((slots) => (
    isPlainObject(slots) && Object.keys(slots).every((slot) => OVERLAY_SLOT_NAMES.includes(slot))
  ))
}

// Test-only: clears the module singleton so one test file's fetch does not
// leak into the next. Not imported by any production module.
export function _resetSurfaceConfigOverlayForTests() {
  ++_fetchGeneration
  _fetchPromise = null
  _fetchStarted = false
  _overlay = {}
  _source = null
  _listeners.clear()
  _warnedUnknownSlotIds.clear()
}

/** The raw tenant overlay (`{surfaceId: {slot: value}}`), `{}` until the ONE
 * session fetch settles or when it failed. Re-renders subscribers exactly
 * once, when that fetch settles — never a poll, never a per-render refetch. */
export function useSurfaceConfigOverlay(mock = false) {
  const [, forceRender] = useState(0)
  useEffect(() => {
    const listener = () => forceRender((n) => n + 1)
    _listeners.add(listener)
    _startFetchOnce(mock)
    return () => { _listeners.delete(listener) }
  }, [mock])
  return _overlay
}

/** `{sha256, authored_at}` of the file the current overlay came from, or
 * `null` (no tenant file, or the fetch has not settled / failed). */
export function useSurfaceConfigSource(mock = false) {
  useSurfaceConfigOverlay(mock) // same subscription; re-renders when source lands too
  return _source
}

/** Non-hook read of the same `_source` `useSurfaceConfigSource` returns. For a
 * consumer that already subscribes via `useSurfaceConfigOverlay` (the overlay
 * and the source settle in the SAME callback, before the one `_notify()`), so
 * a second hook subscription here would be a redundant listener, not a more
 * current value. Safe to call in a loop (a plain function, not a hook). */
export function peekSurfaceConfigSource() {
  return _source
}

/** Pure merge, no hooks: `overlay[id]`'s declared slots (OVERLAY_SLOT_NAMES
 * only) deep-merged one level onto `surfaceContract(id)`. Returns the SAME
 * frozen object `surfaceContract(id)` returns — not a clone — when the
 * overlay carries nothing for this id, so a tenant with no overlay is
 * byte-identical (deep-equal AND referentially equal) to today. */
export function mergeSurfaceContract(id, overlay) {
  const base = surfaceContract(id)
  const patch = overlay?.[id]
  if (!isPlainObject(patch) || Object.keys(patch).length === 0) return base
  // Defense-in-depth only (see the module comment): the server already fails
  // the WHOLE file closed on an unknown slot, so this ignores it rather than
  // enforcing anything, and warns once so a drifted client is discoverable.
  const unknown = Object.keys(patch).filter((key) => !OVERLAY_SLOT_NAMES.includes(key))
  if (unknown.length > 0 && !_warnedUnknownSlotIds.has(id)) {
    _warnedUnknownSlotIds.add(id)
    // eslint-disable-next-line no-console
    console.warn(
      `surface-config overlay for "${id}" carries unknown slot(s) [${unknown.join(', ')}]; ignoring them.`,
    )
  }
  const merged = { ...base }
  for (const slot of OVERLAY_SLOT_NAMES) {
    if (!(slot in patch)) continue
    const value = patch[slot]
    merged[slot] = (isPlainObject(value) && isPlainObject(base[slot]))
      ? { ...base[slot], ...value }
      : value
  }
  return deepFreeze(merged)
}

/** The declared overlay slots for `id` (OVERLAY_SLOT_NAMES ∩ keys the tenant's
 * file actually set), `[]` when there is none — an unknown slot the server
 * already stripped by failing the whole file closed never appears here. */
export function touchedSurfaceConfigSlots(id, overlay) {
  const patch = overlay?.[id]
  if (!isPlainObject(patch)) return []
  return OVERLAY_SLOT_NAMES.filter((slot) => slot in patch)
}

/** Drop-in for `surfaceContract(id)`: fetches the tenant overlay once per
 * session (bounded — see `_startFetchOnce`), merges it, deep-freezes the
 * result. `mock` skips the network call entirely, matching every other
 * api.js mock branch. */
export function useSurfaceContract(id, mock = false) {
  const overlay = useSurfaceConfigOverlay(mock)
  return useMemo(() => mergeSurfaceContract(id, overlay), [id, overlay])
}

/** Provenance for the chip: which slots this surface's overlay touched, and
 * the authoring source's short sha, or `null` when the overlay touched
 * nothing on this surface (no chip to render). */
export function useSurfaceConfigProvenance(id, mock = false) {
  // ONE subscription: `_source` is set in the SAME fetch-settle callback that
  // sets `_overlay` (both before the one `_notify()`), so re-rendering on the
  // overlay changing already covers the source landing too.
  const overlay = useSurfaceConfigOverlay(mock)
  return useMemo(() => {
    const touchedSlots = touchedSurfaceConfigSlots(id, overlay)
    if (touchedSlots.length === 0) return null
    return {
      touchedSlots,
      sha8: typeof _source?.sha256 === 'string' ? _source.sha256.slice(0, 8) : null,
    }
  }, [id, overlay])
}
