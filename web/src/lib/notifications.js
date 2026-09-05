// ---------------------------------------------------------------------------
// THE NOTIFICATION BUS (standardization slice 13a).
//
// ONE store for every notice the product raises. A bounded ring buffer keeps
// EVERY notice — its kind, its time and its action — for JobInbox; the
// newest notice alone drives the visible NT2 toast slot. Toast.jsx already
// owns that anatomy (bottom-centre, ~5s, one action, `role="status"` for the
// announcement) and does not change here — this module only feeds it a
// `{ id, text, action }` shape it already renders.
//
// App.jsx and ToolCast.jsx each hand-rolled their own showToast closure
// (toastSeqRef + setToast). Both become useToastBus() over this ONE store, so
// every one of the 24 existing `showToast({ text, action })` call sites keeps
// its signature — this is a seam change, not a call-site rewrite.
//
// Module-level singleton, not a React context: SiteRoot mounts App.jsx and
// ToolCast.jsx exclusively (site/SiteRoot.jsx swaps the whole scene), so a
// notice raised by one scene must survive being read by the other — a
// context tied to either tree cannot do that; a plain external store,
// subscribed to via useSyncExternalStore (the pattern this codebase already
// uses — see controllers/session/useSessionController.js), can.
// ---------------------------------------------------------------------------
import { useCallback, useSyncExternalStore } from 'react'

// Bounded: the ring can never grow past this many kept notices, which also
// caps JobInbox's render at a fixed O(n) with n <= RING_CAPACITY. 50 is sized
// well above a busy session's realistic burst across the 24 call sites (a few
// notices a minute) while still holding a full session's worth of "what did
// I miss" history for the inbox to show.
export const RING_CAPACITY = 50

/**
 * Factory so a test (or a future second surface) can hold an ISOLATED bus
 * instead of reaching into the shared module singleton below — no shared
 * mutable state between test cases, no reset-between-tests ceremony.
 */
export function createNotificationBus(capacity = RING_CAPACITY) {
  if (!Number.isInteger(capacity) || capacity < 1) {
    throw new Error(`createNotificationBus: capacity must be a positive integer, got ${capacity}`)
  }
  let seq = 0
  let ring = []        // newest-first; length bounded by `capacity` at every push
  let visibleId = null // id of the notice currently in the toast slot, or null
  const listeners = new Set()
  let snapshot = { ring, visibleId }

  function publish() {
    snapshot = { ring, visibleId }
    for (const fn of listeners) fn()
  }

  return {
    /** Subscribes to every push/dismiss; returns the unsubscribe. */
    subscribe(fn) {
      listeners.add(fn)
      return () => listeners.delete(fn)
    },
    /** Stable reference until the next push/dismiss (useSyncExternalStore's contract). */
    getSnapshot() {
      return snapshot
    },
    /** Live listener count — the bus's own proof that unsubscribe leaves zero. */
    listenerCount() {
      return listeners.size
    },
    /**
     * Mints one notice. It is kept in the ring (until it ages out past
     * `capacity`, oldest first) for the inbox, and becomes the visible toast
     * — newest replaces, exactly as both pre-slice showToast closures did.
     * `kind` defaults to 'info' since none of the 24 existing call sites pass
     * one today; it exists so a future caller (or JobInbox's own rendering)
     * can distinguish a plain notice from a success/error/warning one.
     */
    push({ text, kind = 'info', action = null } = {}) {
      seq += 1
      const notice = {
        id: seq,
        kind,
        text,
        action: action && typeof action.onClick === 'function' ? action : null,
        time: Date.now(),
      }
      ring = [notice, ...ring].slice(0, capacity)
      visibleId = notice.id
      publish()
      return notice.id
    },
    /**
     * Toast.jsx calls this when its own lifecycle timer fires or its action
     * ran — clears the VISIBLE slot only; the notice stays in the ring for
     * the inbox. A stale id (a toast already replaced by a newer one) is a
     * deliberate no-op, matching the pre-slice `cur.id === id ? null : cur`
     * guard both showToast closures wrote inline.
     */
    dismissVisible(id) {
      if (visibleId !== id) return
      visibleId = null
      publish()
    },
    /** Unconditionally clears the visible toast — App.jsx's mode/fixture
     *  reset effect wants this regardless of which notice is showing. */
    clearVisible() {
      if (visibleId === null) return
      visibleId = null
      publish()
    },
  }
}

export const notificationBus = createNotificationBus()

/**
 * The ONE hook both scenes' showToast becomes. Same call signature as both
 * pre-slice closures (`showToast({ text, action })`) and the same
 * `{ toast, onDone }` shape site/SurfaceFrame.jsx's FrameToast slot already
 * renders — that slot is unchanged by this module.
 */
export function useToastBus(bus = notificationBus) {
  const { ring, visibleId } = useSyncExternalStore(bus.subscribe, bus.getSnapshot, bus.getSnapshot)
  const showToast = useCallback((next) => bus.push(next), [bus])
  const onToastDone = useCallback((id) => bus.dismissVisible(id), [bus])
  const clearToast = useCallback(() => bus.clearVisible(), [bus])
  const toast = visibleId == null ? null : (ring.find((n) => n.id === visibleId) || null)
  return { toast, showToast, onToastDone, clearToast }
}

/** JobInbox's read of the full kept history — every notice, newest first, bounded at RING_CAPACITY. */
export function useNotices(bus = notificationBus) {
  const { ring } = useSyncExternalStore(bus.subscribe, bus.getSnapshot, bus.getSnapshot)
  return ring
}
