import { useEffect, useRef, useState } from 'react'

/**
 * Evaluate a loading cycle without timers. A null start means no work yet;
 * a null done means work is ongoing. All timestamps use the same clock.
 * The minimum display starts at startMs + showAfterMs, and the long threshold
 * is measured from startMs. Completed work cannot become long afterward.
 */
export function loadingPhaseAt(startMs, nowMs, doneMs = null, {
  showAfterMs = 200,
  minShowMs = 400,
  longAfterMs = 10000,
} = {}) {
  if (startMs == null) return 'hidden'

  const showMs = startMs + showAfterMs
  const finished = doneMs != null && doneMs <= nowMs
  if (finished && (doneMs < showMs || nowMs >= Math.max(doneMs, showMs + minShowMs))) {
    return 'done'
  }
  if (nowMs < showMs) return 'hidden'
  const elapsedMs = (finished ? doneMs : nowMs) - startMs
  return elapsedMs >= longAfterMs ? 'long' : 'shown'
}

/**
 * Delay the indicator, keep it visible for a minimum duration, then mark long
 * work. Unlike the pure helper's nominal display time, the hook records when
 * it actually shows so a delayed timer still gets the full minimum duration.
 */
export function useLoadingPhase(isLoading, {
  showAfterMs = 200,
  minShowMs = 400,
  longAfterMs = 10000,
} = {}) {
  const cycle = useRef({ startMs: null, doneMs: null, shownMs: null })
  const [phase, setPhase] = useState('hidden')

  useEffect(() => {
    const nowMs = Date.now()
    if (isLoading && (cycle.current.startMs == null || cycle.current.doneMs != null)) {
      // A restart during the minimum display must not hide an existing indicator.
      const previousShownMs = cycle.current.shownMs
      const shownMs = previousShownMs != null && nowMs < previousShownMs + minShowMs
        ? previousShownMs : null
      cycle.current = { startMs: nowMs, doneMs: null, shownMs }
    } else if (!isLoading && cycle.current.startMs != null && cycle.current.doneMs == null) {
      cycle.current.doneMs = nowMs
    }

    let timer
    const update = () => {
      const now = Date.now()
      const current = cycle.current
      let nextPhase
      let nextMs

      if (current.startMs == null) {
        nextPhase = 'hidden'
      } else if (current.doneMs != null) {
        // Work that never displayed must not flash after it finishes.
        if (current.shownMs == null || now >= current.shownMs + minShowMs) {
          nextPhase = 'done'
        } else {
          nextPhase = current.doneMs - current.startMs >= longAfterMs ? 'long' : 'shown'
          nextMs = current.shownMs + minShowMs
        }
      } else {
        nextPhase = loadingPhaseAt(current.startMs, now, null, {
          showAfterMs, minShowMs, longAfterMs,
        })
        if (nextPhase === 'hidden' && current.shownMs != null) nextPhase = 'shown'
        if (nextPhase === 'hidden') {
          nextMs = current.startMs + showAfterMs
        } else {
          if (current.shownMs == null) current.shownMs = now
          if (nextPhase === 'shown') nextMs = current.startMs + longAfterMs
        }
      }

      setPhase(nextPhase)
      if (nextMs != null) timer = setTimeout(update, Math.max(0, nextMs - now))
    }

    update()
    return () => clearTimeout(timer)
  }, [isLoading, showAfterMs, minShowMs, longAfterMs])

  return phase
}
